"""Lifecycle tests for tools/oxp3-homebtn.py: fake device fds (pipes), fake
uinput keyboards and a fake clock. No /dev/hidraw, no /dev/uinput, no root.

The happy path is asserted first (one press -> one key pair); the rest covers
the failure modes the daemon must survive: EOF/ENODEV on the vendor node,
select() EBADF, a vanished uinput device, an absent device at start and a
recycled node number that no longer matches the expected identity.
"""

from __future__ import annotations

import errno
import os
import unittest
from unittest import mock

import support

life = support.load("oxp3_lifecycle", "oxp3_lifecycle.py")
homebtn = support.load("oxp3_homebtn", "oxp3-homebtn.py")

VID_PID = life.Identity("hidraw", 0x1A86, 0xFE00, "ONEXPLAYER 3",
                        "usb-0000:00:14.0-3/input2")


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStop:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.flag = False

    def is_set(self) -> bool:
        return self.flag

    def wait(self, seconds: float) -> bool:
        self.clock.advance(seconds)
        return self.flag


class FakeSelect:
    """Returns the fd as readable, or raises the queued OSError once."""

    def __init__(self, *, raise_once: OSError | None = None, readable: bool = True):
        self.error = raise_once
        self.readable = readable
        self.calls = 0

    def __call__(self, rlist, wlist, xlist, timeout=None):
        self.calls += 1
        if self.error is not None:
            err, self.error = self.error, None
            raise err
        return (list(rlist) if self.readable else [], [], [])


class FakeKeyboard:
    def __init__(self, *, fail_times: int = 0):
        self.emits = []
        self.name = "OXP3 Home Button"
        self.closed = False
        self._fail = fail_times

    def emit(self, value):
        if self._fail:
            self._fail -= 1
            raise OSError(errno.ENODEV, "uinput gone")
        self.emits.append(value)

    def close(self):
        self.closed = True


def vendor_report(code: int = 0x24, value: int = 1) -> bytes:
    buf = bytearray(64)
    buf[0] = 0xB2
    buf[1] = 0x3F
    buf[2] = 0x01
    buf[6] = code
    buf[12] = value
    buf[-2] = 0x3F
    buf[-1] = 0xB2
    return bytes(buf)


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.clock = FakeClock()
        self.stop = FakeStop(self.clock)
        self.said = []
        self.pipes = []

    def pipe(self, payload: bytes = b"", close_write: bool = True):
        r, w = os.pipe()
        # The real hidraw fd is opened O_NONBLOCK; a blocking pipe would make
        # read_reports() wait for the next report instead of returning on
        # EAGAIN.
        os.set_blocking(r, False)
        self.pipes.append((r, w))
        if payload:
            os.write(w, payload)
        if close_write:
            os.close(w)
        return r

    def tearDown(self):
        for r, w in self.pipes:
            for fd in (r, w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def daemon(self, opener, *, factory=None, select_fn=None, probe=None,
               hold=0.0, buttons=(0x24,)):
        return homebtn.HomeButtonDaemon(
            set(buttons),
            keycode=homebtn.KEYS["homepage"],
            hold=hold,
            open_device=opener,
            keyboard_factory=factory or FakeKeyboard,
            clock=self.clock,
            select_fn=select_fn or FakeSelect(),
            identity_probe=probe,
            say=self.said.append,
            stop=self.stop,
        )

    # -- happy path ------------------------------------------------------
    def test_press_and_release_emit_one_key_pair(self):
        fd = self.pipe(vendor_report(0x24, 1) + vendor_report(0x24, 0),
                       close_write=False)
        kb = FakeKeyboard()
        d = self.daemon(lambda: (fd, "/dev/hidraw7", VID_PID),
                        factory=lambda: kb)
        self.assertTrue(d.open())
        self.assertTrue(d.service_once())
        self.assertEqual(kb.emits, [1, 0])
        self.assertIn("listening on /dev/hidraw7 (identity 1a86:fe00", self.said[0])
        self.assertIn("button 0x24 pressed", self.said)
        self.assertIn("button 0x24 released", self.said)

    def test_irrelevant_reports_are_ignored(self):
        fd = self.pipe(vendor_report(0x22, 1), close_write=False)
        kb = FakeKeyboard()
        d = self.daemon(lambda: (fd, "/dev/hidraw7", VID_PID), factory=lambda: kb)
        d.open()
        d.service_once()
        self.assertEqual(kb.emits, [])

    def test_hold_safety_net_releases(self):
        fd = self.pipe(vendor_report(0x24, 1), close_write=False)
        kb = FakeKeyboard()
        d = self.daemon(lambda: (fd, "/dev/hidraw7", VID_PID),
                        factory=lambda: kb, hold=0.5)
        d.open()
        d.service_once()
        self.assertEqual(kb.emits, [1])
        d.auto_release()                    # still inside the hold
        self.assertEqual(kb.emits, [1])
        self.clock.advance(0.6)
        d.auto_release()
        self.assertEqual(kb.emits, [1, 0])
        d.auto_release()                    # never twice
        self.assertEqual(kb.emits, [1, 0])
        self.assertIn("button 0x24 auto-released", self.said)

    # -- device loss -----------------------------------------------------
    def test_eof_is_a_loss_and_reconnects_by_identity(self):
        fd1 = self.pipe()                  # write end closed -> read() == b""
        d = self.daemon(lambda: (fd1, "/dev/hidraw7", VID_PID))
        self.assertTrue(d.open())
        self.assertFalse(d.service_once())
        self.assertEqual(d.losses, 1)
        self.assertIsNone(d.fd)
        self.assertTrue(any("device lost: EOF" in m for m in self.said),
                        self.said)

        fd2 = self.pipe(vendor_report(), close_write=False)
        d.open_device = lambda: (fd2, "/dev/hidraw9", VID_PID)
        self.assertTrue(d.open())
        self.assertIn("reconnect #1 to /dev/hidraw9", self.said[-2])
        self.assertEqual(d.backoff.attempts, 0)

    def test_select_ebadf_is_a_loss(self):
        fd = self.pipe()
        d = self.daemon(lambda: (fd, "/dev/hidraw7", VID_PID),
                        select_fn=FakeSelect(raise_once=OSError(errno.EBADF, "bad fd")))
        d.open()
        self.assertFalse(d.service_once())
        self.assertEqual(d.losses, 1)
        self.assertTrue(any("select: EBADF" in m for m in self.said), self.said)

    def test_open_failures_back_off_and_reset_on_success(self):
        calls = {"n": 0}

        def opener():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise OSError(errno.ENODEV, "no device")
            return (self.pipe(vendor_report(), close_write=False),
                    "/dev/hidraw7", VID_PID)

        d = self.daemon(opener)
        self.assertFalse(d.open())
        self.assertEqual(self.clock.now - 100.0, 0.5)
        self.assertFalse(d.open())
        self.assertEqual(self.clock.now - 100.0, 1.5)
        self.assertFalse(d.open())
        self.assertEqual(self.clock.now - 100.0, 3.5)
        self.assertEqual(d.backoff.attempts, 3)
        # The reason did not change, so it is logged once, not three times.
        self.assertEqual(sum("device lost:" in m for m in self.said), 1)
        self.assertTrue(d.open())
        self.assertEqual(d.backoff.attempts, 0)
        self.assertIn("listening on /dev/hidraw7", self.said[-1])

    def test_permission_failure_is_explained_and_retried(self):
        d = self.daemon(lambda: (_ for _ in ()).throw(
            PermissionError(errno.EACCES, "denied")))
        self.assertFalse(d.open())
        self.assertTrue(any("oxp3-hid.rules" in m for m in self.said), self.said)

    def test_watchdog_detects_recycled_node_number(self):
        fd = self.pipe()
        node = str(self.tree.path("dev/hidraw7"))
        d = self.daemon(lambda: (fd, node, VID_PID),
                        probe=lambda _node: life.Identity(
                            "hidraw", 0x1A86, 0xFE00, "ONEXPLAYER 3", "usb-other"))
        d.open()
        self.assertFalse(d.watchdog())
        self.assertEqual(d.losses, 1)
        self.assertTrue(any("now has identity" in m for m in self.said), self.said)

    def test_watchdog_detects_a_node_that_vanished(self):
        fd = self.pipe()
        node = str(self.tree.path("dev/hidraw8"))
        d = self.daemon(lambda: (fd, node, VID_PID), probe=lambda _n: None)
        d.open()
        self.assertFalse(d.watchdog())
        self.assertTrue(any("disappeared" in m for m in self.said), self.said)

    # -- explicit --device (integration harness pin) ---------------------
    def _pinned(self, device="/dev/hidraw7"):
        return homebtn.HomeButtonDaemon(
            {0x24}, keycode=homebtn.KEYS["homepage"], device=device,
            clock=self.clock, select_fn=FakeSelect(),
            say=self.said.append, stop=self.stop)

    def test_pinned_device_is_opened_verbatim_without_a_scan(self):
        fd = self.pipe()
        d = self._pinned()
        with mock.patch.object(homebtn.life, "hidraw_identity",
                               return_value=VID_PID), \
             mock.patch.object(homebtn.hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(homebtn.os.path, "exists", return_value=True), \
             mock.patch.object(homebtn.os, "open", return_value=fd) as opener:
            got_fd, node, ident = d._open_vendor()
        self.assertEqual((got_fd, node), (fd, "/dev/hidraw7"))
        self.assertIs(ident, VID_PID)
        scan.assert_not_called()
        self.assertEqual(opener.call_args.args[0], "/dev/hidraw7")

    def test_pinned_device_that_is_gone_does_not_scan(self):
        d = self._pinned()
        with mock.patch.object(homebtn.hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(homebtn.os.path, "exists", return_value=False):
            with self.assertRaises(life.DeviceGone):
                d._open_vendor()
        scan.assert_not_called()

    def test_after_loss_only_the_recorded_identity_is_used(self):
        # Once an identity exists, the pinned path is not trusted blindly (the
        # node number may be recycled) and discovery is not used either.
        d = self._pinned()
        d.identity = VID_PID
        fd = self.pipe()
        with mock.patch.object(homebtn.life, "find_hidraw",
                               return_value="/dev/hidraw9") as finder, \
             mock.patch.object(homebtn.life, "hidraw_identity",
                               return_value=VID_PID), \
             mock.patch.object(homebtn.hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(homebtn.os.path, "exists", return_value=False), \
             mock.patch.object(homebtn.os, "open", return_value=fd):
            _fd, node, _ident = d._open_vendor()
        self.assertEqual(node, "/dev/hidraw9")
        finder.assert_called_once()
        scan.assert_not_called()

    def test_uinput_name_is_configurable(self):
        with mock.patch.object(homebtn, "UInputKeyboard") as factory:
            factory.return_value = FakeKeyboard()
            d = homebtn.HomeButtonDaemon({0x24}, keycode=homebtn.KEYS["homepage"],
                                         uinput_name="OXP3 Home Button (test)",
                                         clock=self.clock, say=self.said.append,
                                         stop=self.stop)
            self.assertTrue(d.ensure_keyboard())
        factory.assert_called_once_with("OXP3 Home Button (test)",
                                        homebtn.KEYS["homepage"])

    def test_identity_less_pinned_device_is_refused(self):
        # The only remaining identity would be the shared vid:pid, which a real
        # interface also has; the pinned path must not be used in that case.
        fd = self.pipe()
        d = self._pinned()
        with mock.patch.object(homebtn.life, "hidraw_identity", return_value=None), \
             mock.patch.object(homebtn.os.path, "exists", return_value=True), \
             mock.patch.object(homebtn.os, "open", return_value=fd):
            with self.assertRaises(life.DeviceGone) as ctx:
                d._open_vendor()
        self.assertIn("vid:pid", str(ctx.exception))

    def test_watchdog_is_quiet_when_identity_matches(self):
        fd = self.pipe()
        d = self.daemon(lambda: (fd, "/dev/hidraw7", VID_PID),
                        probe=lambda _n: VID_PID)
        d.open()
        self.assertTrue(d.watchdog())
        self.assertEqual(d.losses, 0)
        self.assertTrue(any(m.startswith("watchdog: listening on /dev/hidraw7")
                            for m in self.said), self.said)

    # -- uinput ----------------------------------------------------------
    def test_uinput_write_failure_recreates_the_keyboard(self):
        made = []

        def factory():
            kb = FakeKeyboard(fail_times=1 if not made else 0)
            made.append(kb)
            return kb

        d = self.daemon(lambda: (_ for _ in ()).throw(AssertionError("no open")),
                        factory=factory)
        d.handle_report(vendor_report(0x24, 1))    # first keyboard dies
        self.assertEqual(len(made), 2)
        self.assertTrue(any("uinput: write failed" in m for m in self.said), self.said)
        self.assertTrue(any("uinput: recreated" in m for m in self.said), self.said)
        self.assertTrue(made[0].closed)
        d.handle_report(vendor_report(0x24, 1))    # new device works
        d.handle_report(vendor_report(0x24, 0))
        self.assertEqual(made[1].emits, [1, 0])

    def test_uinput_unavailable_does_not_kill_the_daemon(self):
        d = self.daemon(lambda: (_ for _ in ()).throw(AssertionError("no open")),
                        factory=lambda: (_ for _ in ()).throw(
                            OSError(errno.EACCES, "no uinput")))
        d.handle_report(vendor_report(0x24, 1))   # must not raise
        self.assertTrue(any("uinput: unavailable" in m for m in self.said), self.said)

    # -- reader primitives ----------------------------------------------
    def test_read_reports_classifies_eof_enodev_eagain_and_eintr(self):
        self.addCleanup(setattr, homebtn.os, "read", homebtn.os.read)
        cases = [
            (b"", life.DeviceGone),
            (OSError(errno.ENODEV, "gone"), life.DeviceGone),
            (OSError(errno.EBADF, "closed"), life.DeviceGone),
            (OSError(errno.EPIPE, "pipe"), life.DeviceGone),
        ]
        for payload, exc_type in cases:
            kw = ({"return_value": payload} if isinstance(payload, bytes)
                  else {"side_effect": payload})
            with mock.patch.object(homebtn.os, "read", **kw):
                with self.assertRaises(exc_type):
                    list(homebtn.read_reports(5))

        with mock.patch.object(homebtn.os, "read",
                               side_effect=OSError(errno.EAGAIN, "again")):
            self.assertEqual(list(homebtn.read_reports(5)), [])
        with mock.patch.object(homebtn.os, "read",
                               side_effect=[OSError(errno.EINTR, "intr"),
                                            OSError(errno.EAGAIN, "again")]):
            self.assertEqual(list(homebtn.read_reports(5)), [])

        report = vendor_report()
        with mock.patch.object(homebtn.os, "read",
                               side_effect=[report, OSError(errno.EAGAIN, "again")]):
            self.assertEqual(list(homebtn.read_reports(5)), [report])
        with mock.patch.object(homebtn.os, "read",
                               side_effect=[OSError(errno.EINTR, "intr"), report,
                                            OSError(errno.EAGAIN, "again")]):
            self.assertEqual(list(homebtn.read_reports(5)), [report])

    def test_short_or_foreign_reports_are_dropped(self):
        short = b"\xb2\x3f\x01"
        foreign = bytes(64)
        with mock.patch.object(homebtn.os, "read",
                               side_effect=[short, foreign,
                                            OSError(errno.EAGAIN, "again")]):
            self.assertEqual(list(homebtn.read_reports(5)), [])


if __name__ == "__main__":
    unittest.main()
