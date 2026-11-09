#!/usr/bin/env python3
"""oxp3-homebtn - turn the OneXPlayer 3 home button into KEY_HOMEPAGE.

The OXP3 home button is *not* part of any evdev keyboard on Linux: the
MCU delivers it as a vendor HID report on the 1a86:fe00 interface with
usage page 0xFF00 (same collection that carries the RGB commands).
A press looks like

    B2 3F ... <button id> ...  1        (byte 6 = id, byte 12 = 1)
    B2 3F ... <button id> ...  0        (release)

with 0x24 being the physical home button (0x21 is "guide", 0x22/0x23 are
the extra shoulder buttons).

This daemon reads those reports and either
  * creates a virtual keyboard and emits KEY_HOMEPAGE (default), or
  * runs a command on press (--exec), e.g. to launch Steam Big Picture.

Lifecycle (docs/DAEMONS.md)
---------------------------
The vendor hidraw node is re-numbered by every USB reset and suspend with
re-enumeration, and its fd turns stale (EOF/ENODEV/EBADF) rather than
reported by a friendly message. This daemon therefore
  * rediscovers the node by vendor/product/physical path, never by number,
  * treats EOF/ENODEV/EBADF/EPIPE as "device lost", closes and reopens with
    a bounded backoff (0.5 s .. 30 s), reset on success,
  * recreates the uinput keyboard if it vanished (uinput reload, resume),
  * runs an idle-health watchdog that verifies the open fd still belongs to
    the expected device (same node number can be reused after a reset),
  * logs every transition as ``device lost:`` / ``retrying in`` /
    ``reconnect #`` / ``watchdog:`` so the journal proves health.

Permission: the vendor hidraw node and /dev/uinput are root-only by
default.  Install tools/oxp3-hid.rules once (sudo); the logged-in user
then gets access through the uaccess ACL.  See
docs/hw/home-button-leds.md.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oxp3_hid as hid  # noqa: E402
import oxp3_lifecycle as life  # noqa: E402

# uinput ioctl numbers (linux/uinput.h)
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
EV_SYN = 0x00
EV_KEY = 0x01

KEYS = {
    "homepage": 172,   # KEY_HOMEPAGE
    "back": 158,       # KEY_BACK
    "search": 217,     # KEY_SEARCH
    "f20": 193,        # KEY_F20
    "playpause": 164,  # KEY_PLAYPAUSE
}

# How often the idle-health watchdog re-checks a quiet device (seconds).
HEALTH_INTERVAL = 5.0


class UInputKeyboard:
    def __init__(self, name: str, keycode: int):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, keycode)
            # struct uinput_setup { input_id id; char name[80]; __u32 ff_effects_max; }
            ident = struct.pack("HHHH", 0x0003, 0x0001, 0x0001, 0x0001)  # BUS_USB
            setup = ident + name.encode()[:79].ljust(80, b"\x00") + struct.pack("I", 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self.keycode = keycode
        self.name = name

    def emit(self, value: int):
        now = time.time()
        ev = struct.pack("qqHHi", int(now), int((now % 1) * 1_000_000),
                         EV_KEY, self.keycode, value)
        os.write(self.fd, ev)
        os.write(self.fd, struct.pack("qqHHi", int(now), int((now % 1) * 1_000_000),
                                      EV_SYN, 0, 0))

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def read_reports(fd: int):
    """Yield validated vendor reports from the hidraw node.

    Returns on EAGAIN (nothing buffered right now) and raises
    ``lifecycle.DeviceGone`` on EOF/ENODEV/EBADF/EPIPE so the caller reopens
    instead of spinning on a dead descriptor.
    """
    while True:
        try:
            buf = os.read(fd, hid.REPORT_SIZE)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if e.errno == errno.EINTR:
                continue
            raise life.DeviceGone(life.error_reason(e), e) from e
        if not buf:
            # hidraw returns EOF (0 bytes) on a device that went away without
            # a pending read error; the old loop treated it as "no data" and
            # spun at 100 % CPU forever.
            raise life.DeviceGone("EOF (device closed)", None)
        if len(buf) >= 16 and hid.is_vendor_report(buf):
            yield buf


class HomeButtonDaemon:
    """hidraw reader -> uinput emitter, with reconnect and health checks."""

    def __init__(self, buttons, *, keycode=None, exec_cmd=None, hold=0.0,
                 intercept=False, debug=False, open_device=None,
                 keyboard_factory=None, clock=time, select_fn=select.select,
                 health_interval: float = HEALTH_INTERVAL,
                 identity_probe=None, say=None, stop=None):
        self.buttons = set(buttons)
        self.keycode = keycode
        self.exec_cmd = exec_cmd
        self.hold = float(hold)
        self.intercept = intercept
        self.debug = debug
        self.clock = clock
        self.select_fn = select_fn
        self.health_interval = float(health_interval)
        self.identity_probe = identity_probe or life.hidraw_identity
        self.say = say or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.open_device = open_device or self._open_vendor
        self.keyboard_factory = keyboard_factory or (
            lambda: UInputKeyboard("OXP3 Home Button", self.keycode))

        self.stop = stop if stop is not None else threading.Event()
        self.fd = None
        self.node = None
        self.identity = None
        self.kbd = None
        self.pressed: dict[int, bool] = {}
        self.press_deadline: dict[int, float] = {}
        self.backoff = life.Backoff()
        self.reconnects = 0
        self.losses = 0
        self.opened_once = False
        self.last_reason = None
        self._last_health = 0.0
        self._last_health_log = None

    # -- device discovery and open --------------------------------------
    def _open_vendor(self):
        node = None
        # Prefer the identity captured on the first successful open: after a
        # reset the same physical interface comes back as a different
        # hidrawN, and a re-plug may move it, but vid/pid/phys survive.
        if self.identity is not None:
            node = life.find_hidraw(self.identity)
        if node is None:
            node = hid.find_vendor_hidraw()
        if not node:
            raise life.DeviceGone(
                "1a86:fe00 vendor HID interface (usage page 0xFF00) not present")
        flags = (os.O_WRONLY if self.intercept else os.O_RDONLY) | os.O_NONBLOCK
        fd = os.open(node, flags)
        ident = life.hidraw_identity(node)
        if ident is None:
            ident = life.Identity("hidraw", hid.VID, hid.PID)
        return fd, node, ident

    def open(self) -> bool:
        """Try once to open the device; sleeps the backoff on failure."""
        if self.stop.is_set():
            return False
        try:
            fd, node, ident = self.open_device()
        except (life.DeviceGone, OSError, SystemExit) as e:
            reason = life.error_reason(e)
            if life.is_permanent_error(e):
                # Missing udev rule / root-only node: retrying cannot fix it,
                # but the daemon must stay up and say why.
                if reason != self.last_reason:
                    self.say(f"device lost: {reason}; install tools/oxp3-hid.rules "
                             "(see docs/hw/home-button-leds.md); still retrying")
                    self.last_reason = reason
                delay = self.backoff.next_delay()
                life.sleep_interruptible(delay, self.stop, self.clock)
                return False
            delay = self.backoff.next_delay()
            if reason != self.last_reason or self.debug:
                self.say(f"device lost: {reason}; retrying in "
                         f"{delay:.1f}s (attempt {self.backoff.attempts})")
                self.last_reason = reason
            life.sleep_interruptible(delay, self.stop, self.clock)
            return False

        self.fd, self.node, self.identity = fd, node, ident
        recovered = self.losses > 0 or (self.opened_once and self.backoff.attempts > 0)
        self.opened_once = True
        self.backoff.reset()
        self.last_reason = None
        if recovered:
            self.reconnects += 1
            self.say(f"reconnect #{self.reconnects} to {node} (identity {ident})")
        self.say(f"listening on {node} (identity {ident})")
        if self.intercept:
            try:
                hid.send(fd, [hid.intercept(True)])
            except OSError as e:
                self._lost(f"intercept write failed: {life.error_reason(e)}")
        return True

    # -- loss / reopen ---------------------------------------------------
    def _lost(self, reason: str) -> None:
        if self.fd is None:
            return
        self.losses += 1
        self.say(f"device lost: {reason}; reopening by identity")
        self._close_fd()

    def _close_fd(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        # Clear pressed state: the old device is gone, so nothing is held
        # down on the new one; emitting a release later would be a phantom.
        self.pressed.clear()
        self.press_deadline.clear()

    # -- uinput ----------------------------------------------------------
    def ensure_keyboard(self) -> bool:
        if self.kbd is not None:
            return True
        if self.exec_cmd:
            return True
        try:
            self.kbd = self.keyboard_factory()
            self.say(f"uinput: created '{getattr(self.kbd, 'name', 'keyboard')}'")
            return True
        except OSError as e:
            self.say(f"uinput: unavailable ({life.error_reason(e)}); will retry")
            return False

    def _emit(self, value: int) -> None:
        if self.exec_cmd:
            return
        if not self.ensure_keyboard() or self.kbd is None:
            return
        try:
            self.kbd.emit(value)
        except OSError as e:
            reason = life.error_reason(e)
            self.say(f"uinput: write failed ({reason}); recreating the keyboard")
            self._recreate_keyboard(reason)

    def _recreate_keyboard(self, reason: str) -> None:
        if self.kbd is not None:
            try:
                self.kbd.close()
            except OSError:
                pass
            self.kbd = None
        self.pressed.clear()   # the old device died with any key held
        self.press_deadline.clear()
        if self.ensure_keyboard():
            self.say(f"uinput: recreated after {reason}")

    # -- report handling -------------------------------------------------
    def handle_report(self, buf: bytes) -> None:
        code = buf[6]
        value = buf[12]
        if code not in self.buttons:
            return
        down = value == 1
        old = self.pressed.get(code, False)
        if down and not old:
            self.pressed[code] = True
            self.press_deadline[code] = self.clock.monotonic() + self.hold
            self.say(f"button 0x{code:02x} pressed")
            if self.exec_cmd:
                subprocess.Popen(self.exec_cmd, shell=True)
            else:
                self._emit(1)
        elif not down and old:
            self.pressed[code] = False
            self.press_deadline.pop(code, None)
            self.say(f"button 0x{code:02x} released")
            if not self.exec_cmd:
                self._emit(0)

    def auto_release(self) -> None:
        """Optional max-hold safety net for buttons that never send a release."""
        if self.hold <= 0:
            return
        now = self.clock.monotonic()
        for code, is_down in list(self.pressed.items()):
            if is_down and now >= self.press_deadline.get(code, 0):
                self.pressed[code] = False
                self.press_deadline.pop(code, None)
                if not self.exec_cmd:
                    self._emit(0)
                self.say(f"button 0x{code:02x} auto-released")

    def service_once(self, timeout: float = 0.25) -> bool:
        """One select+read cycle. False when the device was lost."""
        if self.fd is None:
            return False
        try:
            ready, _, _ = self.select_fn([self.fd], [], [], timeout)
        except InterruptedError:
            return True
        except OSError as e:
            self._lost(f"select: {life.error_reason(e)}")
            return False
        if not ready:
            return True
        try:
            for buf in read_reports(self.fd):
                self.handle_report(buf)
        except life.DeviceGone as e:
            self._lost(e.reason)
            return False
        except OSError as e:
            self._lost(f"read: {life.error_reason(e)}")
            return False
        return True

    # -- idle-health watchdog -------------------------------------------
    def watchdog(self) -> bool:
        """Verify the open fd still belongs to the expected device.

        A suspend with re-enumeration normally produces ENODEV on the next
        read, but a device that is reset while no report is pending can leave
        a valid-looking fd whose node number has been recycled. The sysfs
        identity behind the recorded node path is the cheap, root-free proof;
        it is only consulted, never written.
        """
        if self.fd is None:
            return False
        try:
            os.fstat(self.fd)
        except OSError as e:
            self._lost(f"fd check: {life.error_reason(e)}")
            return False
        probe = self.identity_probe
        cur = probe(self.node) if probe is not None else None
        if cur is None:
            if not os.path.exists(self.node):
                self._lost(f"{self.node} disappeared")
                return False
        elif self.identity is not None and not self._identity_ok(cur):
            self._lost(f"{self.node} now has identity {cur}, expected {self.identity}")
            return False
        now = self.clock.monotonic()
        if self.debug or self._last_health_log is None or \
                now - self._last_health_log > 300:
            self._last_health_log = now
            self.say(f"watchdog: listening on {self.node} "
                     f"(reconnects={self.reconnects}, losses={self.losses})")
        return True

    def _identity_ok(self, cur) -> bool:
        if not cur.same_hardware(self.identity):
            return False
        if cur.phys and self.identity.phys:
            return cur.phys == self.identity.phys
        return True

    # -- main loop -------------------------------------------------------
    def run(self) -> int:
        self.ensure_keyboard()
        self._last_health = self.clock.monotonic()
        try:
            while not self.stop.is_set():
                if self.fd is None:
                    if not self.open():
                        continue
                self.service_once(0.25)
                self.auto_release()
                now = self.clock.monotonic()
                if now - self._last_health >= self.health_interval:
                    self._last_health = now
                    self.watchdog()
        finally:
            self._close_fd()
            if self.kbd is not None:
                try:
                    self.kbd.close()
                except OSError:
                    pass
        return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="OXP3 home button -> key/command")
    p.add_argument("--button", type=lambda s: int(s, 0), default=0x24,
                   help="button id to react to (default 0x24 = home)")
    p.add_argument("--also-guide", action="store_true",
                   help="also react to 0x21 (guide)")
    p.add_argument("--key", choices=sorted(KEYS), default="homepage")
    p.add_argument("--exec", dest="exec_cmd", default=None,
                   help="run this command on press instead of emitting a key")
    p.add_argument("--hold", type=float, default=0.0,
                   help="max press length in s; 0 = wait for the device release")
    p.add_argument("--intercept", action="store_true",
                   help="ask the MCU to intercept extra buttons (experimental)")
    p.add_argument("--health", type=float, default=HEALTH_INTERVAL,
                   help="seconds between idle health checks (0 = off)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    buttons = {args.button}
    if args.also_guide:
        buttons.add(0x21)

    daemon = HomeButtonDaemon(
        buttons,
        keycode=KEYS[args.key],
        exec_cmd=args.exec_cmd,
        hold=args.hold,
        intercept=args.intercept,
        debug=args.debug,
        health_interval=args.health,
    )

    def stop(*_):
        daemon.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
