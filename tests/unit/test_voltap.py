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
            "orphan_tap", "late_release", "watchdog", "forwarded",
            "repeat_forwarded"})

    def test_non_volume_repeat_is_forwarded_only_when_proxying(self):
        t = self.make(forward_repeat=True)
        self.assertEqual(t.handle(0.0, A, 2, False), [(A, 2)])
        self.assertEqual(t.stats["repeat_forwarded"], 1)
        t2 = self.make()
        self.assertEqual(t2.handle(0.0, A, 2, False), [])
        self.assertEqual(t2.stats["repeat_forwarded"], 0)

    def test_volume_repeat_is_dropped_even_when_forwarding(self):
        t = self.make(forward_repeat=True)
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.handle(0.005, UP, 2, True), [])
        self.assertEqual(t.stats["repeat"], 1)

    def test_zero_and_negative_hold_are_clamped_by_dwell_like_semantics(self):
        # hold <= 0 makes every press release on the next poll; no crash.
        t = self.make(hold=0.0)
        t.handle(0.0, UP, 1, True)
        self.assertEqual(t.poll(0.0), [(UP, 0)])


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------
# discovery / selection / grab-failure state machine
# --------------------------------------------------------------------------
import os
import pathlib
from types import SimpleNamespace

import support

UP_BIT = 1 << 115
DOWN_BIT = 1 << 114
KEY_VOLUME = UP_BIT | DOWN_BIT


def make_cfg(**over):
    base = dict(
        devices=[], name="", phys="", prefer="auto", allow_physical=True,
        hhd_name="Handheld Daemon Volume Keyboard", hhd_phys="hhd-vbtn",
        atkbd_name="AT Translated Set 2 keyboard",
        atkbd_phys="isa0060/serio0",
    )
    base.update(over)
    return SimpleNamespace(**base)


def make_dev(event="event23", name="", phys="", bus=0, ev=0, key=0,
             rel=0, abs=0):
    raw = {"name": name, "phys": phys, "sysfs": "", "uniq": "",
           "bus": bus, "vendor": 1, "product": 1, "version": 1,
           "handlers": [event], "bits": {}}
    caps = {"EV": ev, "KEY": key, "REL": rel, "ABS": abs}
    return voltap.DeviceInfo(raw, event, caps)


def hhd_dev():
    return make_dev("event23", "Handheld Daemon Volume Keyboard",
                    "phys-hhd-vbtn", bus=3, ev=0x200003, key=KEY_VOLUME)


def at_dev():
    return make_dev("event4", "AT Translated Set 2 keyboard",
                    "isa0060/serio0/input0", bus=0x11, ev=0x120013,
                    key=KEY_VOLUME)


def shim_dev():
    return make_dev("event30", "OXP3 volume tap shim", "phys-oxp3-voltap",
                    bus=3, ev=3, key=KEY_VOLUME)


def volume_only_dev():
    return make_dev("event11", "HID 1a86:fe00 Consumer Control",
                    "usb-0000:00:14.0-5/input1", bus=3, ev=3, key=KEY_VOLUME)


def plan_for(devices, cfg=None, holders=None, known=True, hhd=None):
    return voltap.build_plan(devices, cfg or make_cfg(), holders or {},
                             known, hhd or {"running": False, "pids": []})


class BitmapTest(unittest.TestCase):
    def test_kernel_bitmap_words_are_high_word_first(self):
        value = voltap.parse_bitmap("c000000000000 0")
        self.assertTrue(value & UP_BIT)
        self.assertTrue(value & DOWN_BIT)
        self.assertFalse(value & (1 << 113))

    def test_empty_and_garbage_bitmap(self):
        self.assertEqual(voltap.parse_bitmap(""), 0)
        self.assertEqual(voltap.parse_bitmap(None), 0)
        self.assertEqual(voltap.parse_bitmap("zzzz 0"), 0)

    def test_ev_rep_bit(self):
        self.assertTrue(voltap.parse_bitmap("120013") &
                        (1 << voltap.ecodes.EV_REP))
        self.assertFalse(voltap.parse_bitmap("200003") &
                         (1 << voltap.ecodes.EV_REP))


class ProcParserTest(unittest.TestCase):
    FIXTURE = (
        "I: Bus=0011 Vendor=0001 Product=0001 Version=ab83\n"
        'N: Name="AT Translated Set 2 keyboard"\n'
        "P: Phys=isa0060/serio0/input0\n"
        "S: Sysfs=/devices/platform/i8042/serio0/input/input4\n"
        "U: Uniq=unit-test\n"
        "H: Handlers=sysrq kbd leds event4 \n"
        "B: PROP=0\n"
        "B: EV=120013\n"
        "B: KEY=c000000000000 0\n"
        "\n"
        "I: Bus=0003 Vendor=0001 Product=0001 Version=0001\n"
        'N: Name="Handheld Daemon Volume Keyboard"\n'
        "P: Phys=phys-hhd-vbtn\n"
        "H: Handlers=kbd event23 \n"
        "B: EV=200003\n"
        "B: KEY=c000000000000 0\n"
    )

    def test_sections_and_fields(self):
        parsed = voltap.parse_input_devices(self.FIXTURE)
        self.assertEqual(len(parsed), 2)
        at = parsed[0]
        self.assertEqual(at["name"], "AT Translated Set 2 keyboard")
        self.assertEqual(at["phys"], "isa0060/serio0/input0")
        self.assertEqual(at["bus"], 0x11)
        self.assertEqual(at["handlers"], ["sysrq", "kbd", "leds", "event4"])
        self.assertEqual(at["uniq"], "unit-test")
        hhd = parsed[1]
        self.assertEqual(hhd["handlers"], ["kbd", "event23"])
        self.assertEqual(hhd["vendor"], 1)

    def test_discover_uses_proc_bitmaps_and_event_handlers(self):
        tmp = support.TmpTree()
        try:
            proc = tmp.write("proc-input-devices", self.FIXTURE)
            devices = voltap.discover(str(proc), str(tmp.path("sys")))
        finally:
            tmp.cleanup()
        self.assertEqual([d.path for d in devices],
                         ["/dev/input/event4", "/dev/input/event23"])
        self.assertTrue(devices[0].has_volume)
        self.assertTrue(devices[0].has_rep)
        self.assertFalse(devices[1].has_rep)

    def test_sysfs_capabilities_override_proc(self):
        tmp = support.TmpTree()
        try:
            proc = tmp.write("proc-input-devices", self.FIXTURE)
            base = "sys/class/input/event23/device/capabilities"
            tmp.write(base + "/ev", "0")
            tmp.write(base + "/key", "0")
            devices = voltap.discover(str(proc), str(tmp.path("sys")))
        finally:
            tmp.cleanup()
        event23 = [d for d in devices if d.event == "event23"][0]
        self.assertFalse(event23.has_volume)
        self.assertFalse(event23.has_rep)

    def test_sysfs_only_inventory_is_a_fallback(self):
        tmp = support.TmpTree()
        try:
            base = "sys/class/input/event7/device"
            tmp.write(base + "/name", "Fallback Keyboard\n")
            tmp.write(base + "/phys", "fallback/phys\n")
            tmp.write(base + "/capabilities/ev", "3")
            tmp.write(base + "/capabilities/key", "c000000000000 0")
            devices = voltap.discover(str(tmp.path("missing-proc")),
                                      str(tmp.path("sys")))
        finally:
            tmp.cleanup()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "Fallback Keyboard")
        self.assertTrue(devices[0].has_volume)


class HolderTest(unittest.TestCase):
    def test_grab_is_found_in_fdinfo_and_fd_symlink(self):
        tmp = support.TmpTree()
        try:
            tmp.write("proc/6752/fdinfo/7", "pos: 0\nflags: 02\ngrab: 1\n")
            tmp.write("proc/6752/fdinfo/8", "pos: 0\nflags: 02\ngrab: 0\n")
            os.makedirs(tmp.path("proc/6752/fd"))
            os.symlink("/dev/input/event4", tmp.path("proc/6752/fd/7"))
            os.symlink("/dev/input/event23", tmp.path("proc/6752/fd/8"))
            holders, stats = voltap.find_holders(
                ["/dev/input/event4", "/dev/input/event23"],
                str(tmp.path("proc")))
        finally:
            tmp.cleanup()
        self.assertEqual(holders["/dev/input/event4"], ["6752"])
        self.assertEqual(holders["/dev/input/event23"], [])
        self.assertTrue(stats["known"])

    def test_missing_proc_marks_holders_unknown(self):
        tmp = support.TmpTree()
        try:
            holders, stats = voltap.find_holders(
                ["/dev/input/event4"], str(tmp.path("no-proc")))
        finally:
            tmp.cleanup()
        self.assertEqual(holders["/dev/input/event4"], [])
        self.assertFalse(stats["known"])

    def test_detect_hhd_from_cmdline(self):
        tmp = support.TmpTree()
        try:
            tmp.write("proc/6752/cmdline", "/usr/bin/python3\0/usr/bin/hhd\0")
            tmp.write("proc/7000/cmdline", "/usr/bin/bash\0")
            info = voltap.detect_hhd(str(tmp.path("proc")))
        finally:
            tmp.cleanup()
        self.assertTrue(info["running"])
        self.assertEqual(info["pids"], [6752])


class SelectionTest(unittest.TestCase):
    def test_classify_roles(self):
        cfg = make_cfg()
        self.assertEqual(voltap.classify(hhd_dev(), cfg), voltap.ROLE_HHD)
        self.assertEqual(voltap.classify(at_dev(), cfg), voltap.ROLE_AT)
        self.assertEqual(voltap.classify(shim_dev(), cfg), voltap.ROLE_NEVER)
        self.assertEqual(voltap.classify(volume_only_dev(), cfg),
                         voltap.ROLE_VOLUME)
        self.assertEqual(voltap.classify(make_dev("event5", "Mouse", "", key=0),
                                         cfg), voltap.ROLE_OTHER)

    def test_hhd_virtual_is_preferred_and_physical_is_refused_when_held(self):
        holders = {"/dev/input/event4": [6752]}
        plan = plan_for([at_dev(), hhd_dev()], holders=holders)
        self.assertEqual(plan["topology"], "hhd-virtual")
        self.assertEqual(plan["selected"].path, "/dev/input/event23")
        at = [c for c in plan["candidates"] if c.role == voltap.ROLE_AT][0]
        self.assertEqual(at.decision, "refused-held")
        self.assertIn(6752, at.held_by)
        self.assertEqual(plan["predicted_exit"], voltap.EXIT_OK)

    def test_physical_fallback_only_when_nothing_holds_it(self):
        plan = plan_for([at_dev()])
        self.assertEqual(plan["topology"], "physical")
        self.assertEqual(plan["selected"].path, "/dev/input/event4")
        self.assertEqual(plan["selected"].decision, "fallback")

    def test_physical_held_without_hhd_degrades(self):
        plan = plan_for([at_dev()], holders={"/dev/input/event4": [1234]})
        self.assertEqual(plan["topology"], "none")
        self.assertIsNone(plan["selected"])
        self.assertEqual(plan["predicted_exit"], voltap.EXIT_GRAB_REFUSED)

    def test_hhd_held_falls_back_to_free_physical(self):
        plan = plan_for([at_dev(), hhd_dev()],
                        holders={"/dev/input/event23": [999]})
        self.assertEqual(plan["selected"].path, "/dev/input/event4")
        self.assertEqual(plan["topology"], "physical")

    def test_unknown_holders_do_not_hide_the_physical_fallback(self):
        plan = plan_for([at_dev()], known=False)
        self.assertEqual(plan["selected"].path, "/dev/input/event4")
        self.assertFalse(plan["holders_known"])

    def test_physical_fallback_can_be_disabled(self):
        plan = plan_for([at_dev()], cfg=make_cfg(allow_physical=False))
        self.assertIsNone(plan["selected"])
        self.assertEqual(plan["predicted_exit"], voltap.EXIT_GRAB_REFUSED)

    def test_explicit_override_wins(self):
        plan = plan_for([at_dev(), hhd_dev()],
                        cfg=make_cfg(devices=["/dev/input/event4"]))
        self.assertEqual(plan["topology"], "explicit")
        self.assertEqual(plan["selected"].path, "/dev/input/event4")

    def test_explicit_missing_device_is_reported(self):
        plan = plan_for([hhd_dev()],
                        cfg=make_cfg(devices=["/dev/input/event99"]))
        self.assertIsNone(plan["selected"])
        self.assertEqual(plan["missing"], ["/dev/input/event99"])
        self.assertEqual(plan["predicted_exit"], voltap.EXIT_NO_DEVICE)

    def test_explicit_shim_is_never_grabbed(self):
        plan = plan_for([shim_dev()],
                        cfg=make_cfg(devices=["/dev/input/event30"]))
        self.assertIsNone(plan["selected"])
        self.assertEqual(plan["predicted_exit"], voltap.EXIT_NO_DEVICE)

    def test_configured_volume_only_device_is_used_when_nothing_better(self):
        plan = plan_for([volume_only_dev()],
                        cfg=make_cfg(name="Consumer Control"))
        self.assertEqual(plan["selected"].path, "/dev/input/event11")
        self.assertEqual(plan["topology"], "configured")

    def test_kv_output_has_stable_keys(self):
        plan = plan_for([at_dev(), hhd_dev()],
                        holders={"/dev/input/event4": [6752]})
        out = voltap.format_kv(plan, True)
        fields = dict(line.split("=", 1) for line in out.splitlines())
        self.assertEqual(fields["TOPO"], "hhd-virtual")
        self.assertEqual(fields["SEL"], "/dev/input/event23")
        self.assertEqual(fields["SEL_ROLE"], "hhd-virtual")
        self.assertEqual(fields["ATKBD"], "/dev/input/event4")
        self.assertEqual(fields["ATKBD_HELD"], "6752")
        self.assertEqual(fields["PREDICTED_EXIT"], "0")
        self.assertEqual(fields["HOLDERS_KNOWN"], "1")


class GrabFSMTest(unittest.TestCase):
    class Cand:
        def __init__(self, path):
            self.path = path

    def scan(self, fsm, candidates, kinds):
        def attempt(cand):
            return kinds[cand.path], f"{kinds[cand.path]} detail"
        return fsm.scan([self.Cand(p) for p in candidates], attempt)

    def test_first_candidate_ok(self):
        fsm = voltap.GrabFSM()
        result = self.scan(fsm, ["a"], {"a": "ok"})
        self.assertEqual(result.state, "grabbed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.selected.path, "a")
        self.assertTrue(result.changed)

    def test_busy_first_then_ok_second(self):
        fsm = voltap.GrabFSM()
        result = self.scan(fsm, ["a", "b"], {"a": "busy", "b": "ok"})
        self.assertEqual(result.state, "grabbed")
        self.assertEqual(result.selected.path, "b")
        self.assertEqual(result.refused[0]["path"], "a")
        self.assertEqual(result.refused[0]["kind"], "busy")

    def test_all_busy_is_degraded_busy_exit_3(self):
        fsm = voltap.GrabFSM()
        result = self.scan(fsm, ["a", "b"], {"a": "busy", "b": "busy"})
        self.assertEqual(result.state, "degraded-busy")
        self.assertEqual(result.exit_code, voltap.EXIT_GRAB_REFUSED)
        self.assertIsNone(result.selected)
        self.assertEqual(len(result.refused), 2)
        self.assertIn("EBUSY", fsm.describe())

    def test_no_candidates_is_degraded_empty_exit_2(self):
        fsm = voltap.GrabFSM()
        result = self.scan(fsm, [], {})
        self.assertEqual(result.state, "degraded-empty")
        self.assertEqual(result.exit_code, voltap.EXIT_NO_DEVICE)

    def test_all_errors_is_degraded_error_exit_3(self):
        fsm = voltap.GrabFSM()
        result = self.scan(fsm, ["a"], {"a": "error"})
        self.assertEqual(result.state, "degraded-error")
        self.assertEqual(result.exit_code, voltap.EXIT_GRAB_REFUSED)

    def test_identical_retry_is_not_a_transition(self):
        fsm = voltap.GrabFSM()
        first = self.scan(fsm, ["a"], {"a": "busy"})
        second = self.scan(fsm, ["a"], {"a": "busy"})
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)

    def test_recovery_after_degraded_is_a_transition(self):
        fsm = voltap.GrabFSM()
        self.scan(fsm, ["a"], {"a": "busy"})
        result = self.scan(fsm, ["a"], {"a": "ok"})
        self.assertEqual(result.state, "grabbed")
        self.assertTrue(result.changed)

    def test_refused_set_change_is_a_transition(self):
        fsm = voltap.GrabFSM()
        self.scan(fsm, ["a"], {"a": "busy"})
        result = self.scan(fsm, ["a", "b"], {"a": "busy", "b": "busy"})
        self.assertTrue(result.changed)
