#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3_lifecycle - suspend/hotplug survival helpers shared by our daemons.

The three long-running tools (oxp3-homebtn.py, oxp3-rgbd.py, oxp3-voltap)
all face the same three problems:

  1. a device node that is re-numbered or disappears (suspend with USB
     re-enumeration, USB reset, hhd restart),
  2. an fd that turns stale without an obvious error (EOF, EBADF, ENODEV),
  3. an fd that never opens because the device is momentarily absent, so
     the daemon must retry with a bounded, logged delay instead of a tight
     loop or a crash that systemd restarts silently.

This module holds only the parts that are identical in all three, plus the
identity-based discovery they need:

  * ``Backoff``: deterministic delay ladder with a cap, reset on success.
  * ``error_reason`` / ``is_device_error``: one vocabulary for journal lines
    and for "is this worth a reopen or a real bug?".
  * ``DeviceGone``: raised by a reader when the fd is definitively dead
    (EOF from hidraw, ENODEV, EBADF, EPIPE) so callers reopen instead of
    spinning on a dead descriptor.
  * ``Identity`` + ``hidraw_identity`` / ``find_hidraw`` and
    ``input_identity`` / ``find_input_event``: rediscovery by vendor/product
    (and, when available, the stable hidraw ``HID_PHYS`` / evdev ``phys``),
    never by the ``N`` in ``hidrawN`` or ``eventN``.
  * ``ResumeDetector``: CLOCK_BOOTTIME minus CLOCK_MONOTONIC only grows
    across suspend; poll() reports one resume per jump and never twice.
  * ``Interval``: a monotonic timer that, unlike ``time.time()``, cannot
    fire early/late because the RTC jumped across a suspend or NTP step.

The journal vocabulary is part of the contract (docs/DAEMONS.md):
``device lost:``, ``retrying in``, ``reconnect #``, ``watchdog:``.

Stdlib only; no write outside the process (except os.write by callers).
"""

from __future__ import annotations

import errno
import os
import time

# Delays between reconnect attempts. Short at first (a hhd restart is over in
# a second), then long enough that a device that stays away does not fill the
# journal or wake the CPU.
DEFAULT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

# Errors that mean "that fd/node is gone, open it again": a stale hidraw or
# input fd after resume, a yanked USB device, a closed uinput device.
DEVICE_ERRNOS = frozenset({
    errno.ENODEV,
    errno.ENXIO,
    errno.ENOENT,
    errno.EIO,
    errno.EPIPE,
    errno.ESHUTDOWN,
    errno.EBADF,
    errno.EINVAL,   # write to a destroyed uinput fd
})

# Errors that no amount of retrying can fix; log once and keep the daemon up.
PERMANENT_ERRNOS = frozenset({
    errno.EACCES,
    errno.EPERM,
    errno.EROFS,
    errno.ENOSPC,
})


class DeviceGone(RuntimeError):
    """The device behind a valid-looking fd is gone; reopen by identity."""

    def __init__(self, reason: str, err: OSError | None = None):
        super().__init__(reason)
        self.reason = reason
        self.err = err


def error_reason(exc: BaseException) -> str:
    """``ENODEV (No such device)`` or ``ValueError: text`` - stable log text."""
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"{errno.errorcode.get(exc.errno, exc.errno)} ({os.strerror(exc.errno)})"
    if isinstance(exc, SystemExit):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def is_device_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in DEVICE_ERRNOS


def is_permanent_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in PERMANENT_ERRNOS


class Backoff:
    """Bounded retry ladder: 0.5, 1, 2, 4, 8, 15, 30, 30, ...

    ``next_delay()`` returns the delay to sleep *before* the next attempt and
    counts the attempt; ``reset()`` is called after any success so the next
    failure starts at the short end again. Deterministic (no jitter): the
    tests assert the exact sequence and the journal shows it.
    """

    def __init__(self, delays=DEFAULT_DELAYS):
        if not delays:
            raise ValueError("backoff needs at least one delay")
        self.delays = tuple(float(d) for d in delays)
        self.attempts = 0

    @property
    def total(self) -> int:
        return len(self.delays)

    def next_delay(self) -> float:
        i = min(self.attempts, len(self.delays) - 1)
        self.attempts += 1
        return self.delays[i]

    def peek_delay(self) -> float:
        return self.delays[min(self.attempts, len(self.delays) - 1)]

    def reset(self) -> None:
        self.attempts = 0

    def describe(self, reason: str = "") -> str:
        """``attempt 3, retrying in 2.0s: ENODEV (No such device)``"""
        delay = self.peek_delay()
        tail = f": {reason}" if reason else ""
        return f"attempt {self.attempts + 1}, retrying in {delay:.1f}s{tail}"


def sleep_interruptible(seconds: float, stop=None, clock=time) -> bool:
    """Sleep up to ``seconds`` in 100 ms slices; True when ``stop`` was set.

    A plain ``time.sleep(30)`` would delay SIGTERM handling by up to 30 s,
    which is exactly the kind of "daemon is active but deaf" state the
    lifecycle contract forbids.
    """
    end = clock.monotonic() + max(0.0, float(seconds))
    while True:
        if stop is not None and stop.is_set():
            return True
        left = end - clock.monotonic()
        if left <= 0:
            return False
        if stop is not None:
            if stop.wait(min(0.1, left)):
                return True
        else:
            time.sleep(min(0.1, left))


class Interval:
    """A monotonic periodic timer.

    ``time.monotonic()`` does not advance while the system is suspended, so a
    30 s watchdog fires 30 s of *awake* time after the last run - never
    several times in a burst just because the machine slept. CLOCK_REALTIME
    (``time.time()``) has no such guarantee and is what the RGB keeper used
    to use for its watchdog and debounce deadlines.
    """

    def __init__(self, seconds: float, clock=time, now: float | None = None):
        self.seconds = float(seconds)
        self.clock = clock
        now = clock.monotonic() if now is None else now
        self._next = now + self.seconds

    def remaining(self, now: float | None = None) -> float:
        now = self.clock.monotonic() if now is None else now
        return max(0.0, self._next - now)

    def due(self, now: float | None = None) -> bool:
        if self.seconds <= 0:
            return False
        now = self.clock.monotonic() if now is None else now
        if now < self._next:
            return False
        self._next = now + self.seconds
        return True

    def reset(self, now: float | None = None) -> None:
        now = self.clock.monotonic() if now is None else now
        self._next = now + self.seconds


def boottime_delta(clock=time) -> float:
    """Seconds the system has spent suspended since boot (0.0 if unavailable)."""
    try:
        return clock.clock_gettime(clock.CLOCK_BOOTTIME) - clock.monotonic()
    except (AttributeError, OSError):
        return 0.0


class ResumeDetector:
    """Report each suspend/resume once.

    CLOCK_BOOTTIME counts suspended time, CLOCK_MONOTONIC does not, so the
    difference only grows across a resume. ``poll()`` returns True on the
    first sample after a jump larger than ``threshold`` and False on every
    other sample; the baseline is taken by the first call.
    """

    def __init__(self, threshold: float = 1.5, clock=time):
        self.threshold = float(threshold)
        self.clock = clock
        self._last = None
        self.suspended_for = 0.0

    def delta(self) -> float:
        return boottime_delta(self.clock)

    def poll(self) -> bool:
        cur = self.delta()
        prev, self._last = self._last, cur
        if prev is None:
            return False
        jump = cur - prev
        if jump > self.threshold:
            self.suspended_for = jump
            return True
        self.suspended_for = 0.0
        return False


# ---------------------------------------------------------------------------
# identity-based discovery
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _hex_int(text: str | None, base: int = 16) -> int | None:
    if text is None:
        return None
    t = text.strip().lower()
    if t.startswith("0x"):
        t = t[2:]
    try:
        return int(t, base)
    except ValueError:
        return None


class Identity:
    """What actually identifies a device across a re-numbering.

    ``kind`` is ``"hidraw"`` or ``"input"``. ``vid``/``pid`` are mandatory;
    ``name`` and ``phys`` improve the match but are allowed to be empty.
    """

    __slots__ = ("kind", "vid", "pid", "name", "phys")

    def __init__(self, kind: str, vid: int, pid: int,
                 name: str = "", phys: str = ""):
        self.kind = kind
        self.vid = int(vid)
        self.pid = int(pid)
        self.name = name or ""
        self.phys = phys or ""

    # -- comparison ------------------------------------------------------
    def __eq__(self, other) -> bool:
        return (isinstance(other, Identity)
                and (self.kind, self.vid, self.pid, self.name, self.phys)
                == (other.kind, other.vid, other.pid, other.name, other.phys))

    def __hash__(self) -> int:
        return hash((self.kind, self.vid, self.pid, self.name, self.phys))

    def same_hardware(self, other) -> bool:
        return (isinstance(other, Identity)
                and self.vid == other.vid and self.pid == other.pid)

    def match_tier(self, other) -> int:
        """3 = same vid/pid/phys, 2 = same vid/pid/name, 0 = different hw.

        Used to rank candidates after a re-plug: a device that moved to
        another USB port keeps vid/pid/name but not ``phys``; a different
        device with the same vid/pid must never win over either.
        """
        if not self.same_hardware(other):
            return 0
        if self.phys and other.phys and self.phys == other.phys:
            return 3
        if self.name and other.name and self.name == other.name:
            return 2
        if not self.phys or not other.phys:
            return 2
        return 1

    def __str__(self) -> str:
        bits = [f"{self.vid:04x}:{self.pid:04x}"]
        if self.name:
            bits.append(f"name={self.name!r}")
        if self.phys:
            bits.append(f"phys={self.phys}")
        return " ".join(bits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Identity({self.kind}, {self})"


def _numeric_tail(name: str) -> int:
    i = len(name)
    while i > 0 and name[i - 1].isdigit():
        i -= 1
    try:
        return int(name[i:]) if i < len(name) else -1
    except ValueError:  # pragma: no cover - defensive
        return -1


def _nodes(root: str, prefix: str) -> list[str]:
    """hidrawN/eventN names under ``root``, sorted by number (not by string)."""
    try:
        names = [n for n in os.listdir(root) if n.startswith(prefix)]
    except OSError:
        return []
    return sorted(names, key=lambda n: (_numeric_tail(n), n))


# -- hidraw --------------------------------------------------------------

def parse_hidraw_uevent(text: str, kind: str = "hidraw") -> Identity | None:
    """Parse ``HID_ID=0003:00001A86:0000FE00`` plus HID_NAME/HID_PHYS."""
    vid = pid = None
    name = phys = ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key == "HID_ID":
            parts = value.split(":")
            if len(parts) == 3:
                vid = _hex_int(parts[1])
                pid = _hex_int(parts[2])
        elif key == "HID_NAME":
            name = value
        elif key == "HID_PHYS":
            phys = value
    if vid is None or pid is None:
        return None
    return Identity(kind, vid, pid, name, phys)


def hidraw_identity(node: str, sysfs_root: str = "/sys/class/hidraw"
                    ) -> Identity | None:
    """Identity of ``/dev/hidrawN`` from its sysfs ``uevent``, or None."""
    name = os.path.basename(node)
    text = _read_text(os.path.join(sysfs_root, name, "device", "uevent"))
    if text is None:
        return None
    return parse_hidraw_uevent(text)


def find_hidraw(identity: Identity, sysfs_root: str = "/sys/class/hidraw",
                skip_input_child: bool = True) -> str | None:
    """Find ``/dev/hidrawN`` with the same hardware identity.

    Never matches on the node number: the candidates are enumerated fresh and
    ranked by ``Identity.match_tier`` (phys, then name, then vid/pid). Only a
    tier with at least one hit is used, so a same-vid/pid node without the
    expected phys/name cannot mask a better match.
    """
    # A differently-named collection of the same vid/pid (another interface of
    # the same composite device) is not the node we lost: require the name to
    # match when the identity carries one. A re-plug into another USB port
    # changes phys but not the name, so that still matches.
    minimum = 2 if identity.name else 1
    best_tier = 0
    best: list[str] = []
    for name in _nodes(sysfs_root, "hidraw"):
        devdir = os.path.join(sysfs_root, name, "device")
        if skip_input_child and os.path.isdir(os.path.join(devdir, "input")):
            continue
        cand = hidraw_identity("/dev/" + name, sysfs_root)
        if cand is None:
            continue
        tier = identity.match_tier(cand)
        if tier < minimum:
            continue
        if tier > best_tier:
            best_tier, best = tier, ["/dev/" + name]
        elif tier == best_tier:
            best.append("/dev/" + name)
    return best[0] if best else None


# -- evdev input ---------------------------------------------------------

def input_identity(node: str, sysfs_root: str = "/sys/class/input"
                   ) -> Identity | None:
    """Identity of ``/dev/input/eventN`` from ``/sys/class/input/eventN``."""
    name = os.path.basename(node)
    base = os.path.join(sysfs_root, name, "device")
    dev_name = _read_text(os.path.join(base, "name")) or ""
    phys = _read_text(os.path.join(base, "phys")) or ""
    vid = _hex_int(_read_text(os.path.join(base, "id", "vendor")))
    pid = _hex_int(_read_text(os.path.join(base, "id", "product")))
    if vid is None or pid is None:
        return None
    return Identity("input", vid, pid, dev_name, phys)


def find_input_event(identity: Identity, sysfs_root: str = "/sys/class/input"
                     ) -> str | None:
    """Find the ``/dev/input/eventN`` with the same identity, by identity."""
    minimum = 2 if identity.name else 1
    best_tier = 0
    best: list[str] = []
    for name in _nodes(sysfs_root, "event"):
        cand = input_identity("/dev/input/" + name, sysfs_root)
        if cand is None:
            continue
        tier = identity.match_tier(cand)
        if tier < minimum:
            continue
        if tier > best_tier:
            best_tier, best = tier, ["/dev/input/" + name]
        elif tier == best_tier:
            best.append("/dev/input/" + name)
    return best[0] if best else None
