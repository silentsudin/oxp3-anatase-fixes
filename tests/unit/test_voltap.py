"""Unit tests for the pure tap state machine in tools/oxp3-voltap/oxp3-voltap.

The daemon wrapper needs /dev/input and /dev/uinput; the Tapper class does not,
and it is the part that decides whether the mixer sees one step or a runaway
key. Synthetic Linux input sequences cover the documented rules.
"""

from __future__ import annotations

import unittest

import support

try:
    voltap = support.load("oxp3_voltap", "oxp3-voltap/oxp3-voltap")
    IMPORT_ERROR = None
except SystemExit as exc:  # python3-evdev missing
    voltap = None
    IMPORT_ERROR = str(exc)

if voltap is None:
    raise unittest.SkipTest(f"oxp3-voltap cannot be imported: {IMPORT_ERROR}")

UP = voltap.ecodes.KEY_VOLUMEUP
DOWN = voltap.ecodes.KEY_VOLUMEDOWN
A = voltap.ecodes.KEY_A


def taps(result):
    return [v for _code, v in result]


class TapperTest(unittest.TestCase):
    def make(self, **kwargs):
        return voltap.Tapper(**kwargs)

    # -- ordinary presses --------------------------------------------------
    def test_one_clean_pair_per_press(self):
        t = self.make(hold=0.030, gap=0.060)
        self.assertEqual(t.handle(0.0, UP, 1, True), [(UP, 1)])
        self.assertEqual(t.handle(0.010, UP, 0, True), [(UP, 0)])
        self.assertEqual(t.down, {})
        self.assertEqual(t.last_tap[UP], 0.010)
        self.assertEqual(t.stats["press"], 1)

    def test_repeat_is_never_forwarded(self):
        t = self.make()
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.handle(0.005, UP, 2, True), [])
        self.assertEqual(t.stats["repeat"], 1)

    def test_duplicate_press_is_dropped(self):
        t = self.make()
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.handle(0.001, UP, 1, True), [])
        self.assertEqual(t.stats["duplicate"], 1)

    def test_bounce_window_after_the_release(self):
        t = self.make(gap=0.060)
        t.handle(0.0, UP, 1, True)
        t.handle(0.010, UP, 0, True)
        self.assertEqual(t.handle(0.069, UP, 1, True), [])   # within gap
        self.assertEqual(t.stats["bounce"], 1)
        self.assertEqual(t.handle(0.070, UP, 1, True), [(UP, 1)])

    def test_per_key_state(self):
        t = self.make()
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.handle(0.001, DOWN, 1, True), [(DOWN, 1)])
        self.assertEqual(t.handle(0.002, UP, 0, True), [(UP, 0)])
        self.assertEqual(t.handle(0.003, DOWN, 0, True), [(DOWN, 0)])

    # -- synthetic release / watchdog -------------------------------------
    def test_watchdog_releases_a_key_that_never_ends(self):
        t = self.make(hold=0.5, max_hold=0.5)
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.poll(0.4), [])
        self.assertEqual(t.poll(0.5), [(UP, 0)])
        self.assertEqual(t.down, {})
        self.assertEqual(t.last_tap[UP], 0.5)

    def test_next_deadline_is_the_earliest_press(self):
        t = self.make(hold=0.030)
        self.assertIsNone(t.next_deadline())
        t.handle(1.0, UP, 1, True)
        t.handle(1.1, DOWN, 1, True)
        self.assertAlmostEqual(t.next_deadline(), 1.03)

    def test_release_all_releases_everything(self):
        t = self.make()
        t.handle(0.0, UP, 1, True)
        t.handle(0.0, DOWN, 1, True)
        self.assertEqual(sorted(t.release_all(1.0)), [(DOWN, 0), (UP, 0)])
        self.assertEqual(t.down, {})

    def test_late_release_after_a_synthetic_release_is_dropped(self):
        t = self.make(hold=0.030)
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.poll(0.030), [(UP, 0)])
        self.assertEqual(t.handle(1.0, UP, 0, True), [])
        self.assertEqual(t.stats["late_release"], 1)
        self.assertEqual(t.stats["orphan_tap"], 0)

    # -- orphan releases (the hhd case) -----------------------------------
    def test_orphan_release_becomes_one_tap(self):
        t = self.make(hold=0.030)
        self.assertEqual(t.handle(1.0, UP, 0, True), [(UP, 1)])
        self.assertEqual(t.stats["orphan_tap"], 1)
        self.assertEqual(t.stats["orphan_release"], 1)
        self.assertEqual(t.poll(1.03), [(UP, 0)])

    def test_orphan_release_after_a_normal_tap_still_taps(self):
        # Regression: origin[] kept "press" forever after a real release, so
        # the next orphan release (a press the kernel collapsed) was counted
        # as a "late release" and silently dropped - exactly the hhd sequence
        # this feature exists for.
        t = self.make(gap=0.060, hold=1.0)
        t.handle(0.0, UP, 1, True)
        t.handle(0.010, UP, 0, True)      # complete tap
        self.assertEqual(t.handle(0.500, UP, 0, True), [(UP, 1)])
        self.assertEqual(t.stats["orphan_tap"], 1)
        self.assertEqual(t.stats["late_release"], 0)

    def test_new_press_after_a_watchdog_release_resets_the_tail(self):
        t = self.make(hold=0.030, gap=0.060)
        t.handle(0.0, UP, 1, True)
        t.poll(0.030)                       # synthetic release
        t.handle(0.200, UP, 1, True)        # new accepted press
        self.assertEqual(t.handle(0.210, UP, 0, True), [(UP, 0)])
        self.assertEqual(t.stats["late_release"], 0)

    def test_orphan_tap_can_be_disabled(self):
        t = self.make(orphan_tap=False)
        self.assertEqual(t.handle(1.0, UP, 0, True), [])
        self.assertEqual(t.stats["orphan_tap"], 0)
        self.assertEqual(t.stats["orphan_release"], 1)

    def test_orphan_release_also_respects_the_gap(self):
        t = self.make(gap=0.060)
        t.handle(1.0, UP, 1, True)        # press
        t.handle(1.0, UP, 0, True)        # real release at the same instant
        self.assertEqual(t.handle(1.01, UP, 0, True), [])  # within gap
        self.assertEqual(t.stats["bounce"], 1)
        self.assertEqual(t.handle(1.2, UP, 0, True), [(UP, 1)])

    # -- forwarding of non-volume keys ------------------------------------
    def test_non_volume_keys_are_forwarded_without_state(self):
        t = self.make()
        self.assertEqual(t.handle(0.0, A, 1, False), [(A, 1)])
        self.assertEqual(t.handle(0.1, A, 2, False), [])
        self.assertEqual(t.handle(0.2, A, 0, False), [(A, 0)])
        self.assertEqual(t.stats["forwarded"], 2)
        self.assertEqual(t.down, {})

    # -- stats/documentation invariants -----------------------------------
    def test_stats_dictionary_is_stable(self):
        t = self.make()
        self.assertEqual(set(t.stats), {
            "press", "repeat", "duplicate", "bounce", "orphan_release",
            "orphan_tap", "late_release", "watchdog", "forwarded"})

    def test_zero_and_negative_hold_are_clamped_by_dwell_like_semantics(self):
        # hold <= 0 makes every press release on the next poll; no crash.
        t = self.make(hold=0.0)
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.poll(0.0), [(UP, 0)])


if __name__ == "__main__":
    unittest.main()
