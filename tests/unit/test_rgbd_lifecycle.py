"""Lifecycle tests for tools/oxp3-rgbd.py.

Covers the three fixes that are specific to the RGB keeper:

  * a simulated suspend/resume: CLOCK_BOOTTIME jumps, CLOCK_MONOTONIC does
    not, and the detector reports the jump exactly once;
  * timers use CLOCK_MONOTONIC, so an hour of suspend does not make the
    watchdog fire in a catch-up burst (or early) on wake;
  * a vanished vendor node or a failed write schedules a bounded backoff
    retry, and the next success logs ``reconnect #`` and resets the ladder.

No hidraw device, HHD, journal or real clock is touched.
"""

from __future__ import annotations

import argparse
import io
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

life = support.load("oxp3_lifecycle", "oxp3_lifecycle.py")
hid = support.load("oxp3_hid", "oxp3_hid.py")
rgbd = support.load("oxp3_rgbd", "oxp3-rgbd.py")

CONFIG = '[zones.top]\neffect = "solid"\ncolor = "#010203"\n'


class FakeClock:
    CLOCK_BOOTTIME = 7

    def __init__(self, mono: float = 1000.0, boot: float = 1000.0):
        self.mono = mono
        self.boot = boot

    def monotonic(self) -> float:
        return self.mono

    def clock_gettime(self, clock_id: int) -> float:
        return self.boot

    def suspend(self, seconds: float) -> None:
        """Time passes while the system is suspended: only BOOTTIME moves."""
        self.boot += seconds

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.boot += seconds


class FakeStop:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.flag = False

    def is_set(self) -> bool:
        return self.flag

    def set(self) -> None:
        self.flag = True

    def wait(self, seconds: float) -> bool:
        self.clock.advance(seconds)
        return self.flag


class RgbdLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.config = self.tree.write("rgb.toml", CONFIG)
        self.clock = FakeClock()
        self.logs = []

    def daemon(self, **overrides):
        args = dict(config=str(self.config), dry_run=True, watchdog=30.0,
                    reapply_delay=0.5, settle=0.0, no_journal=True,
                    hhd_unit="hhd")
        args.update(overrides)
        d = rgbd.RgbDaemon(argparse.Namespace(**args), clock=self.clock)
        d.stop = FakeStop(self.clock)
        self.assertTrue(d.reload_config())
        return d

    def logger(self):
        return mock.patch.object(rgbd, "log", side_effect=self.logs.append)

    # -- resume detection -------------------------------------------------
    def test_simulated_suspend_resume_is_reported_once(self):
        d = self.daemon()
        with self.logger(), mock.patch.object(d, "schedule") as sched:
            self.assertFalse(d.resume_step())      # baseline sample
            self.clock.advance(5.0)                # ordinary awake time
            self.assertFalse(d.resume_step())
            self.clock.suspend(120.0)              # suspend: BOOTTIME only
            self.assertTrue(d.resume_step())
            self.assertFalse(d.resume_step())      # not reported twice
        self.assertEqual(sched.call_count, 1)
        self.assertEqual(sched.call_args.args[0], "resume detected")
        self.assertTrue(any("resume detected: 120.0s" in m for m in self.logs),
                        self.logs)

    def test_resume_baseline_is_taken_by_the_first_sample(self):
        d = self.daemon()
        with self.logger(), mock.patch.object(d, "schedule") as sched:
            self.clock.suspend(600.0)   # the daemon starts after a resume
            self.assertFalse(d.resume_step())
        self.assertEqual(sched.call_count, 0)

    def test_watchdog_timer_does_not_fire_after_a_long_suspend(self):
        d = self.daemon()
        iv = d.watchdog_interval
        now = self.clock.monotonic()
        self.assertFalse(iv.due(now))
        self.clock.suspend(3600.0)                 # an hour asleep
        self.assertFalse(iv.due(self.clock.monotonic()))
        self.clock.advance(30.0)                   # 30 s of awake time
        self.assertTrue(iv.due(self.clock.monotonic()))

    # -- device loss / backoff -------------------------------------------
    def test_missing_device_schedules_backoff_and_reconnect(self):
        d = self.daemon()
        with self.logger():
            with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
                 redirect_stdout(io.StringIO()):
                self.assertFalse(d.apply("startup"))
                delay1 = d.retry_due - self.clock.monotonic()
                self.assertAlmostEqual(delay1, 0.5)
                self.assertFalse(d.apply("tick"))
                delay2 = d.retry_due - self.clock.monotonic()
                self.assertAlmostEqual(delay2, 1.0)
            with mock.patch.object(hid, "find_vendor_hidraw",
                                   return_value="/dev/hidraw7"), \
                 redirect_stdout(io.StringIO()):
                self.assertTrue(d.apply("retry"))
        self.assertIsNone(d.retry_due)
        self.assertEqual(d.reconnects, 1)
        self.assertEqual(d.backoff.attempts, 0)
        self.assertTrue(any("device lost:" in m for m in self.logs), self.logs)
        self.assertTrue(any("reconnect #1 to /dev/hidraw7" in m for m in self.logs),
                        self.logs)
        self.assertGreater(d.last_apply, 0)

    def test_write_enodev_schedules_a_retry(self):
        d = self.daemon(dry_run=False)
        with self.logger():
            with mock.patch.object(hid, "find_vendor_hidraw",
                                   return_value="/dev/hidraw7"), \
                 mock.patch.object(rgbd.os, "open", return_value=9), \
                 mock.patch.object(rgbd.os, "close"), \
                 mock.patch.object(hid, "send",
                                   side_effect=OSError(19, "no such device")):
                self.assertFalse(d.apply("startup"))
            self.assertIsNotNone(d.retry_due)
            self.assertTrue(any("device lost: write /dev/hidraw7: ENODEV" in m
                                for m in self.logs), self.logs)
            with mock.patch.object(hid, "find_vendor_hidraw",
                                   return_value="/dev/hidraw8"), \
                 mock.patch.object(rgbd.os, "open", return_value=9), \
                 mock.patch.object(rgbd.os, "close"), \
                 mock.patch.object(hid, "send", return_value=64):
                self.assertTrue(d.apply("retry"))
        self.assertEqual(d.reconnects, 1)
        self.assertTrue(any("reconnect #1 to /dev/hidraw8" in m for m in self.logs),
                        self.logs)

    def test_open_error_backoff_and_permission_is_not_a_device_loss(self):
        d = self.daemon(dry_run=False)
        with self.logger():
            with mock.patch.object(hid, "find_vendor_hidraw",
                                   return_value="/dev/hidraw7"), \
                 mock.patch.object(rgbd.os, "open",
                                   side_effect=OSError(5, "i/o error")):
                self.assertFalse(d.apply("startup"))
            self.assertIsNotNone(d.retry_due)
            self.assertTrue(any("device lost: open /dev/hidraw7: EIO" in m
                                for m in self.logs), self.logs)

            d.retry_due = None
            with mock.patch.object(hid, "find_vendor_hidraw",
                                   return_value="/dev/hidraw7"), \
                 mock.patch.object(rgbd.os, "open",
                                   side_effect=PermissionError(13, "denied")):
                self.assertFalse(d.apply("tick"))
        self.assertIsNone(d.retry_due)
        self.assertEqual(d.last_error, "permission")
        self.assertEqual(sum("permission denied" in m for m in self.logs), 1)

    def test_retry_ladder_is_capped(self):
        d = self.daemon()
        with self.logger(), \
             mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             redirect_stdout(io.StringIO()):
            for _ in range(12):
                d.apply("tick")
        self.assertEqual(d.backoff.attempts, 12)
        self.assertLessEqual(d.retry_due - self.clock.monotonic(), 30.0)

    # -- supervision / health --------------------------------------------
    def test_watcher_crash_is_logged_and_restarted(self):
        d = self.daemon()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            d.stop.set()

        with self.logger():
            d._supervise("probe", flaky, retry_after=0.0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("watcher probe failed: RuntimeError: boom" in m
                            for m in self.logs), self.logs)

    def test_dead_watcher_thread_is_restarted(self):
        d = self.daemon()
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        d.threads["resume"] = t
        with self.logger(), mock.patch.object(d, "start_thread") as start:
            d._check_threads()
        self.assertTrue(any("watcher thread resume died" in m for m in self.logs),
                        self.logs)
        start.assert_called_once_with("resume", d.resume_watcher)

    def test_health_note_is_rate_limited(self):
        d = self.daemon()
        d.last_apply = self.clock.monotonic() - 500
        with self.logger():
            d._health_note(self.clock.monotonic())
            d._health_note(self.clock.monotonic() + 10)
        self.assertEqual(sum("no successful apply" in m for m in self.logs), 1)
        self.assertTrue(any("watchdog: no successful apply for 500s" in m
                            for m in self.logs), self.logs)

    # -- check output -----------------------------------------------------
    def test_check_reports_the_clock_contract(self):
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw",
                               return_value="/dev/hidraw7"), \
             mock.patch.object(rgbd.os, "stat",
                               return_value=mock.Mock(st_mode=0o660, st_uid=0,
                                                      st_gid=18)), \
             mock.patch.object(rgbd, "journal_readable", return_value=False), \
             mock.patch.object(rgbd, "hhd_rgb_mode", return_value=None), \
             redirect_stdout(buf):
            self.assertEqual(self.daemon().check(), 0)
        out = buf.getvalue()
        self.assertIn("CLOCK_BOOTTIME", out)
        self.assertIn("CLOCK_MONOTONIC", out)


if __name__ == "__main__":
    unittest.main()
