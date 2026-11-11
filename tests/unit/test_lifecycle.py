# SPDX-License-Identifier: MIT
"""Unit tests for tools/oxp3_lifecycle.py.

The module is the shared suspend/hotplug core of oxp3-homebtn.py,
oxp3-rgbd.py and oxp3-voltap: backoff, errno classification, resume
detection on CLOCK_BOOTTIME, monotonic intervals and identity-based
device discovery. No real device, sysfs tree or clock is touched.
"""

from __future__ import annotations

import errno
import shutil
import unittest

import support

life = support.load("oxp3_lifecycle", "oxp3_lifecycle.py")


class FakeClock:
    """Deterministic replacement for the ``time`` module subset we use."""

    CLOCK_BOOTTIME = 7

    def __init__(self, mono: float = 1000.0, boot: float = 1000.0):
        self.mono = mono
        self.boot = boot

    def monotonic(self) -> float:
        return self.mono

    def clock_gettime(self, clock_id: int) -> float:
        if clock_id != self.CLOCK_BOOTTIME:  # pragma: no cover - defensive
            raise OSError(errno.EINVAL, "bad clock")
        return self.boot

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.boot += seconds


class FakeStop:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.flag = False

    def is_set(self) -> bool:
        return self.flag

    def wait(self, seconds: float) -> bool:
        self.clock.mono += seconds
        self.clock.boot += seconds
        return self.flag


class BackoffTest(unittest.TestCase):
    def test_ladder_and_cap(self):
        b = life.Backoff((0.5, 1.0, 2.0))
        self.assertEqual([b.next_delay() for _ in range(5)], [0.5, 1.0, 2.0, 2.0, 2.0])
        self.assertEqual(b.attempts, 5)
        b.reset()
        self.assertEqual(b.attempts, 0)
        self.assertEqual(b.next_delay(), 0.5)

    def test_peek_and_describe(self):
        b = life.Backoff((0.5, 1.0))
        self.assertEqual(b.peek_delay(), 0.5)
        self.assertIn("attempt 1", b.describe())
        b.next_delay()
        self.assertEqual(b.peek_delay(), 1.0)
        self.assertIn("retrying in 1.0s", b.describe("ENODEV (No such device)"))
        self.assertIn("ENODEV", b.describe("ENODEV (No such device)"))

    def test_empty_ladder_is_refused(self):
        with self.assertRaises(ValueError):
            life.Backoff(())


class ErrnoTest(unittest.TestCase):
    def test_device_errors(self):
        for err in (errno.ENODEV, errno.EPIPE, errno.EBADF, errno.EINVAL,
                    errno.EIO, errno.ENXIO, errno.ENOENT):
            self.assertTrue(life.is_device_error(OSError(err, "x")), err)
            self.assertFalse(life.is_permanent_error(OSError(err, "x")), err)

    def test_permanent_errors_are_not_device_errors(self):
        for err in (errno.EACCES, errno.EPERM, errno.EROFS):
            self.assertTrue(life.is_permanent_error(OSError(err, "x")), err)
            self.assertFalse(life.is_device_error(OSError(err, "x")), err)

    def test_error_reason_is_stable(self):
        self.assertEqual(life.error_reason(OSError(errno.ENODEV, "gone")),
                         "ENODEV (No such device)")
        self.assertEqual(life.error_reason(SystemExit("no device")), "no device")
        self.assertIn("ValueError", life.error_reason(ValueError("bad")))

    def test_non_oserror_is_neither(self):
        self.assertFalse(life.is_device_error(ValueError("x")))
        self.assertFalse(life.is_permanent_error(ValueError("x")))


class SleepTest(unittest.TestCase):
    def test_sleep_uses_the_clock_and_stays_interruptible(self):
        clock = FakeClock()
        stop = FakeStop(clock)
        self.assertFalse(life.sleep_interruptible(0.5, stop, clock))
        self.assertAlmostEqual(clock.mono - 1000.0, 0.5, places=6)

    def test_stop_before_sleep_returns_immediately(self):
        clock = FakeClock()
        stop = FakeStop(clock)
        stop.flag = True
        self.assertTrue(life.sleep_interruptible(30, stop, clock))
        self.assertEqual(clock.mono, 1000.0)


class ResumeDetectorTest(unittest.TestCase):
    def test_long_suspend_is_reported_exactly_once(self):
        clock = FakeClock()
        det = life.ResumeDetector(1.5, clock)
        self.assertFalse(det.poll())          # baseline
        clock.advance(1.0)
        self.assertFalse(det.poll())          # ordinary awake time
        clock.boot += 300.0                   # 5 min suspended, mono unchanged
        self.assertTrue(det.poll())
        self.assertAlmostEqual(det.suspended_for, 300.0)
        self.assertFalse(det.poll())          # the same jump is not reported twice
        clock.advance(2.0)
        self.assertFalse(det.poll())

    def test_small_jump_is_not_a_resume(self):
        clock = FakeClock()
        det = life.ResumeDetector(1.5, clock)
        det.poll()
        clock.boot += 1.0
        self.assertFalse(det.poll())

    def test_missing_boottime_clock_degrades_to_zero(self):
        class NoBoot:
            def monotonic(self):
                return 1.0

        self.assertEqual(life.boottime_delta(NoBoot()), 0.0)
        det = life.ResumeDetector(1.5, NoBoot())
        self.assertFalse(det.poll())
        self.assertFalse(det.poll())


class IntervalTest(unittest.TestCase):
    def test_periodic_semantics(self):
        clock = FakeClock()
        iv = life.Interval(30.0, clock)
        self.assertFalse(iv.due())
        clock.advance(29.0)
        self.assertFalse(iv.due())
        clock.advance(1.0)
        self.assertTrue(iv.due())
        self.assertFalse(iv.due())
        self.assertAlmostEqual(iv.remaining(), 30.0)

    def test_a_long_suspend_does_not_fire_the_timer_in_a_burst(self):
        # CLOCK_MONOTONIC stops during suspend, so the deadline is still in
        # the future after wake-up - exactly one run, not N catch-up runs.
        clock = FakeClock()
        iv = life.Interval(30.0, clock)
        clock.boot += 3600.0                  # an hour suspended, mono unchanged
        self.assertFalse(iv.due())
        clock.advance(30.0)                   # 30 s of awake time
        self.assertTrue(iv.due())

    def test_zero_disables(self):
        clock = FakeClock()
        iv = life.Interval(0, clock)
        clock.advance(1000)
        self.assertFalse(iv.due())


class IdentityTest(unittest.TestCase):
    def test_uevent_parsing(self):
        ident = life.parse_hidraw_uevent(
            "HID_ID=0003:00001A86:0000FE00\n"
            "HID_NAME=ONEXPLAYER 3\n"
            "HID_PHYS=usb-0000:00:14.0-3/input2\n")
        self.assertIsNotNone(ident)
        self.assertEqual((ident.kind, ident.vid, ident.pid), ("hidraw", 0x1A86, 0xFE00))
        self.assertEqual(ident.name, "ONEXPLAYER 3")
        self.assertEqual(ident.phys, "usb-0000:00:14.0-3/input2")
        self.assertIn("1a86:fe00", str(ident))

    def test_bad_uevent(self):
        self.assertIsNone(life.parse_hidraw_uevent("HID_ID=0003:zz\n"))
        self.assertIsNone(life.parse_hidraw_uevent(""))

    def test_match_tiers(self):
        a = life.Identity("hidraw", 0x1A86, 0xFE00, "dev", "usb-1")
        self.assertEqual(a.match_tier(life.Identity("hidraw", 0x1A86, 0xFE00, "dev", "usb-1")), 3)
        self.assertEqual(a.match_tier(life.Identity("hidraw", 0x1A86, 0xFE00, "dev", "usb-2")), 2)
        self.assertEqual(a.match_tier(life.Identity("hidraw", 0x1A86, 0xFE00, "other", "usb-2")), 1)
        self.assertEqual(a.match_tier(life.Identity("hidraw", 0x1A86, 0xFE01, "dev", "usb-1")), 0)
        self.assertFalse(a.same_hardware(life.Identity("hidraw", 1, 2)))
        self.assertTrue(a.same_hardware(life.Identity("hidraw", 0x1A86, 0xFE00)))


class HidrawDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def node(self, name, uevent, *, input_child=False):
        base = self.tree.mkdir(f"{name}/device")
        self.tree.write(f"{name}/device/uevent", uevent)
        if input_child:
            self.tree.mkdir(f"{name}/device/input")
        return base

    def test_identity_and_find_after_renumbering(self):
        uevent = ("HID_ID=0003:00001A86:0000FE00\n"
                  "HID_NAME=ONEXPLAYER 3\n"
                  "HID_PHYS=usb-0000:00:14.0-3/input2\n")
        self.node("hidraw4", uevent)
        ident = life.hidraw_identity("/dev/hidraw4", str(self.tree.root))
        self.assertIsNotNone(ident)
        self.assertEqual(life.find_hidraw(ident, str(self.tree.root)), "/dev/hidraw4")

        # Suspend/resume renumbers the node: same identity, new number, and a
        # different device ahead of it in the listing. The old node is gone.
        shutil.rmtree(self.tree.root / "hidraw4")
        self.tree.write("hidraw1/device/uevent",
                        "HID_ID=0003:0000045E:0000028E\nHID_NAME=Gamepad\n")
        self.tree.write("hidraw9/device/uevent", uevent)
        self.assertEqual(life.find_hidraw(ident, str(self.tree.root)), "/dev/hidraw9")

    def test_other_hardware_with_the_same_name_is_never_picked(self):
        target = ("HID_ID=0003:00001A86:0000FE00\n"
                  "HID_NAME=ONEXPLAYER 3\nHID_PHYS=usb-1/input2\n")
        self.node("hidraw4", target)
        ident = life.hidraw_identity("/dev/hidraw4", str(self.tree.root))
        shutil.rmtree(self.tree.root / "hidraw4")
        self.node("hidraw4", "HID_ID=0003:00001A86:0000FE00\nHID_NAME=Other\n"
                             "HID_PHYS=usb-2/input2\n")
        self.assertIsNone(life.find_hidraw(ident, str(self.tree.root)))

    def test_input_child_nodes_are_skipped(self):
        uevent = "HID_ID=0003:00001A86:0000FE00\nHID_NAME=ONEXPLAYER 3\n"
        self.node("hidraw4", uevent)
        ident = life.hidraw_identity("/dev/hidraw4", str(self.tree.root))
        self.tree.mkdir("hidraw4/device/input")
        self.assertIsNone(life.find_hidraw(ident, str(self.tree.root)))
        self.assertEqual(life.find_hidraw(ident, str(self.tree.root),
                                          skip_input_child=False), "/dev/hidraw4")

    def test_nodes_are_sorted_numerically(self):
        for n in ("hidraw10", "hidraw2", "hidraw1"):
            self.tree.mkdir(f"{n}/device")
        self.assertEqual([n for n in life._nodes(str(self.tree.root), "hidraw")],
                         ["hidraw1", "hidraw2", "hidraw10"])

    def test_missing_uevent_is_ignored(self):
        self.tree.mkdir("hidraw3/device")
        self.assertIsNone(life.hidraw_identity("/dev/hidraw3", str(self.tree.root)))


class InputDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def event(self, name, *, device_name, phys, vid="0001", pid="0001"):
        base = f"input/{name}/device"
        self.tree.write(f"{base}/name", device_name)
        self.tree.write(f"{base}/phys", phys)
        self.tree.write(f"{base}/id/vendor", vid)
        self.tree.write(f"{base}/id/product", pid)
        return self.tree.path(base)

    def test_identity_and_rediscovery_after_renumbering(self):
        self.event("event23", device_name="Handheld Daemon Volume Keyboard",
                   phys="phys-hhd-vbtn")
        root = str(self.tree.root / "input")
        ident = life.input_identity("/dev/input/event23", root)
        self.assertIsNotNone(ident)
        self.assertEqual((ident.kind, ident.vid, ident.pid), ("input", 1, 1))
        self.assertEqual(life.find_input_event(ident, root), "/dev/input/event23")

        shutil.rmtree(self.tree.root / "input" / "event23")
        self.tree.mkdir("input/event1/device")
        self.tree.write("input/event1/device/name", "AT Translated Set 2 keyboard")
        self.tree.write("input/event1/device/phys", "isa0060/serio0")
        self.tree.write("input/event1/device/id/vendor", "0011")
        self.tree.write("input/event1/device/id/product", "0001")
        self.event("event25", device_name="Handheld Daemon Volume Keyboard",
                   phys="phys-hhd-vbtn")
        self.assertEqual(life.find_input_event(ident, root), "/dev/input/event25")

    def test_same_name_other_hardware_does_not_match(self):
        self.event("event23", device_name="Shim", phys="phys-a")
        root = str(self.tree.root / "input")
        ident = life.input_identity("/dev/input/event23", root)
        shutil.rmtree(self.tree.root / "input" / "event23")
        self.event("event24", device_name="Shim", phys="phys-b", vid="0002")
        self.assertIsNone(life.find_input_event(ident, root))

    def test_missing_id_files(self):
        self.tree.write("input/event7/device/name", "x")
        self.assertIsNone(life.input_identity("/dev/input/event7",
                                              str(self.tree.root / "input")))


class DeviceGoneTest(unittest.TestCase):
    def test_carries_reason(self):
        err = OSError(errno.ENODEV, "gone")
        exc = life.DeviceGone(life.error_reason(err), err)
        self.assertIn("ENODEV", str(exc))
        self.assertIs(exc.err, err)


if __name__ == "__main__":
    unittest.main()
