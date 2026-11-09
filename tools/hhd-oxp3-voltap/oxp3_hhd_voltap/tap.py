"""Tap normalisation for the ONEXPLAYER 3 volume rocker, as an HHD filter.

This is the same state machine that ``tools/oxp3-voltap`` uses in front of
``/dev/input/event*``, moved *into* HHD's producer -> multiplexer path so that
nothing has to open or grab an input device:

    EC -> atkbd -> hhd/device/oxp OxpAtKbd -> [this filter] -> Multiplexer
                                             -> UInputDevice("... Volume Keyboard")

The rules are the ones proven by ``tests/unit/test_voltap.py`` and the volume
integration harness:

* one clean down/up pair per physical press, never a key left down;
* a press is held for at least ``hold`` seconds (HHD's own tap used 60 ms; the
  client needs a non-zero duration and a later batch is safer than a same-batch
  press/release, which the input core may coalesce);
* duplicate presses for a key that is already down are dropped (EC duplicate);
* a press closer than ``gap`` to the previous tap is contact bounce;
* a release without a press still proves that one press happened (the EC drops
  breaks, and atkbd's force_release collapses press+release into one batch, so
  HHD can see only the release) - it becomes one tap unless it is the late tail
  of a tap we already synthesised;
* ``max_hold`` caps any synthetic hold, so a wrong configuration cannot leave
  the key down indefinitely.

Everything is clock-injected and pure, so it is unit-testable offline.
"""

from __future__ import annotations

# HHD names the translated volume buttons "key_volumeup"/"key_volumedown".
# tools/oxp3-voltap derives the same codes from the evdev KEY_* constants.
VOLUME_CODES = frozenset(("key_volumeup", "key_volumedown"))

DEFAULT_HOLD = 0.060
DEFAULT_GAP = 0.060
DEFAULT_MAX_HOLD = 0.500


class TapNormaliser:
    """One clean tap per physical press, never a held volume key."""

    __slots__ = ("hold", "gap", "max_hold", "orphan_tap", "down", "last_tap",
                 "synthetic_tail", "stats", "failed")

    def __init__(self, hold=DEFAULT_HOLD, gap=DEFAULT_GAP,
                 max_hold=DEFAULT_MAX_HOLD, orphan_tap=True):
        self.hold = max(0.0, float(hold))
        self.gap = max(0.0, float(gap))
        self.max_hold = max(0.0, float(max_hold))
        if self.max_hold and self.max_hold < self.hold:
            self.hold = self.max_hold
        self.orphan_tap = bool(orphan_tap)
        self.down = {}            # code -> time.perf_counter() of the press
        self.last_tap = {}        # code -> time the last release went out
        self.synthetic_tail = set()  # releases we produced ourselves
        self.failed = False
        self.stats = {
            "press": 0,
            "repeat": 0,
            "duplicate": 0,
            "bounce": 0,
            "orphan_release": 0,
            "orphan_tap": 0,
            "late_release": 0,
            "deferred_release": 0,
            "watchdog": 0,
        }

    # -- pure step ---------------------------------------------------------
    def handle(self, now: float, code: str, value) -> list[tuple[str, bool]]:
        """Consume one HHD button event for a volume key.

        ``value`` is HHD's bool (True/False); integers 0/1/2 are accepted
        defensively because a future HHD may forward raw evdev values again.
        """
        if code not in VOLUME_CODES:
            return []
        if self.failed:
            return []

        if isinstance(value, bool):
            raw = 1 if value else 0
        elif isinstance(value, int):
            raw = value
        else:
            return []

        if raw == 2:
            # The input core generated repeat; never let it reach the mixer.
            self.stats["repeat"] += 1
            return []
        if raw not in (0, 1):
            return []

        if raw == 1:
            if code in self.down:
                self.stats["duplicate"] += 1
                return []
            last = self.last_tap.get(code)
            if last is not None and now - last < self.gap:
                self.stats["bounce"] += 1
                return []
            self.down[code] = now
            self.synthetic_tail.discard(code)
            self.stats["press"] += 1
            return [(code, True)]

        # raw == 0: a real release.
        start = self.down.pop(code, None)
        if start is not None:
            self.synthetic_tail.discard(code)
            # A deferred release is due at press + hold; if the real release
            # arrives earlier, honour the minimum hold instead of a zero-length
            # tap (same batch as the preset means the client may miss it).
            if now - start < self.hold:
                self.stats["deferred_release"] += 1
                self.down[code] = start
                return []
            self.last_tap[code] = now
            return [(code, False)]

        self.stats["orphan_release"] += 1
        last = self.last_tap.get(code)
        if last is not None and now - last < self.gap:
            self.stats["bounce"] += 1
            return []
        if code in self.synthetic_tail:
            # The tail of a press we already discharged with a synthetic
            # release; dropping it keeps one physical press to one tap.
            self.stats["late_release"] += 1
            self.synthetic_tail.discard(code)
            return []
        if not self.orphan_tap:
            return []
        self.stats["orphan_tap"] += 1
        self.down[code] = now
        return [(code, True)]

    def poll(self, now: float) -> list[tuple[str, bool]]:
        """Emit releases that are due (minimum hold, or the max_hold cap)."""
        if self.failed:
            return []
        out: list[tuple[str, bool]] = []
        for code, start in list(self.down.items()):
            due = start + (self.hold if self.hold else 0.0)
            if now >= due:
                del self.down[code]
                self.last_tap[code] = now
                self.synthetic_tail.add(code)
                out.append((code, False))
        return out

    def release_all(self, now: float) -> list[tuple[str, bool]]:
        """Release everything (plugin close / HHD shutdown)."""
        out = []
        for code in list(self.down):
            del self.down[code]
            self.last_tap[code] = now
            self.synthetic_tail.add(code)
            out.append((code, False))
        return out

    def next_deadline(self):
        if not self.down:
            return None
        return min(start + self.hold for start in self.down.values())

    # -- defensive wrapper used by the HHD hook ----------------------------
    def guard(self, fn, *args, **kwargs):
        """Run one step; on any unexpected failure disable and re-raise.

        The caller (``hhd_hook``) catches the exception, logs once and returns
        HHD's own events unchanged.  Disabling here means a broken state machine
        can never keep perturbing the controller for the rest of the boot.
        """
        try:
            return fn(*args, **kwargs)
        except Exception:
            self.failed = True
            raise
