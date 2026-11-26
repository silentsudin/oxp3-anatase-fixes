# SPDX-License-Identifier: MIT
"""Offline tests for files/oxp3-dock-watch, the read-only VMM6210 recorder.

A fake converter stands in for the DP AUX device: a dict of converter memory behind
the same RC register protocol. Covers strike reporting, the firmware log tail across
the ring wrap, the ring dump on attach, that a modeset is not reported as a dock loss,
and that nothing but the RC command/offset/length/PRIUS registers is ever written.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import pathlib
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

SCRIPT = support.REPO / "files" / "oxp3-dock-watch"
UNIT = support.REPO / "files" / "oxp3-dock-watch.service"
INSTALLER = support.REPO / "tools" / "oxp3-hw-install.sh"


def load():
    loader = importlib.machinery.SourceFileLoader("oxp3_dock_watch", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


dw = load()
RING = 0x90100000


class FakeConverter(dw.Aux):
    """Converter memory + RC protocol; records every DPCD write."""

    def __init__(self):
        self.rc = False
        self.mem_map = {}
        self.dpcd = {0x500: dw.SYNAPTICS_OUI, 0x507: dw.CHIP_VMM6210}
        self.writes = []
        self.offset = self.length = 0
        self.data = b""
        self.put(dw.LOG_RING_PTR, struct.pack("<II", RING, 0x90200000))
        self.put(dw.LOG_ENABLED, b"\x01")
        self.put(RING, b"\0" * dw.LOG_RING_SIZE)
        self.set_state(strikes=0, passes=0, log_w=0)

    def put(self, addr, b):
        for i, v in enumerate(b):
            self.mem_map[addr + i] = v

    def set_state(self, strikes, passes, log_w):
        blk = bytearray(32)
        blk[0x08] = 1
        struct.pack_into("<H", blk, 0x10, log_w)
        blk[0x17] = strikes
        struct.pack_into("<H", blk, 0x1A, passes)
        self.put(dw.STATE_BLOCK, bytes(blk))

    def log(self, w, text):
        """Append text to the ring at write index w (wrapping); returns the new index."""
        for ch in text.encode():
            self.mem_map[RING + w] = ch
            w = (w + 1) % dw.LOG_RING_SIZE
        return w

    def close(self):
        pass

    def rd(self, addr, n):
        if addr == dw.RC_CMD:
            return bytes([0, 0])
        if addr == dw.RC_DATA:
            return self.data[:n]
        base = max(k for k in self.dpcd if k <= addr)
        return self.dpcd[base][addr - base:addr - base + n]

    def wr(self, addr, data):
        data = bytes(data)
        self.writes.append(addr)
        if addr == dw.RC_OFFSET:
            self.offset = struct.unpack("<I", data)[0]
        elif addr == dw.RC_LEN:
            self.length = struct.unpack("<I", data)[0]
        elif addr == dw.RC_CMD and data[0] & 0x7F == dw.READ_FROM_MEMORY:
            self.data = bytes(self.mem_map.get(self.offset + i, 0) for i in range(self.length))


def fake_glob(pattern):
    return ["/sys/class/drm/card1-DP-1"] if pattern.startswith("/sys/class/drm/") else []


class DockWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = mock.patch.object(dw, "LOGDIR", self.tmp.name)
        self.logdir.start()
        self.addCleanup(self.logdir.stop)
        dw.dumped_w = None
        self.conv = FakeConverter()
        self.s = dw.Session(self.conv, "/dev/drm_dp_aux1", "card1-DP-1")

    def run_quiet(self, fn, *a):
        out = io.StringIO()
        with redirect_stdout(out):
            ret = fn(*a)
        return ret, out.getvalue()

    def test_attach_identifies_converter_and_dumps_ring(self):
        w = self.conv.log(0, "boot ok\nNo display detected, going back\n")
        self.conv.set_state(0, 0, w)
        ok, out = self.run_quiet(self.s.attach)
        self.assertTrue(ok)
        self.assertIn("VMM6210 attached", out)
        self.assertIn("fw (ring): No display detected, going back", out)
        files = list(pathlib.Path(self.tmp.name).glob("*-fwlog.txt"))
        self.assertEqual(len(files), 1)
        # unchanged ring: no second 32 KB dump on the next attach (e.g. after a modeset)
        _, out = self.run_quiet(self.s.attach)
        self.assertNotIn("fw (ring)", out)

    def test_other_branch_device_is_left_alone(self):
        self.conv.dpcd[0x500] = bytes.fromhex("001cf8")
        ok, out = self.run_quiet(self.s.attach)
        self.assertFalse(ok)
        self.assertNotIn(dw.RC_OFFSET, self.conv.writes)

    def test_strike_reports_the_compared_values(self):
        self.run_quiet(self.s.attach)
        self.conv.put(0x20330004, struct.pack("<I", 0x0000_0E00))     # tx0 out 3584
        self.conv.put(0x20330040, struct.pack("<I", 0x0010_0000))     # margin 16
        self.conv.put(0x2022084C, struct.pack("<I", 0x0000_0F00))     # rx0 pic 3840
        self.conv.set_state(strikes=1, passes=400, log_w=0)
        _, out = self.run_quiet(self.s.sample)
        self.assertIn("OUTPUT CHECK STRIKE 1/10: strikes 1 passes 400", out)
        self.assertIn("tx0 out 3584 margin 16", out)
        self.assertIn("rx0 pic 3840", out)
        self.assertTrue(list(pathlib.Path(self.tmp.name).glob("*-strike1.txt")))

    def test_firmware_log_tail_follows_the_wrap(self):
        start = dw.LOG_RING_SIZE - 10
        self.conv.set_state(0, 0, start)
        self.run_quiet(self.s.attach)
        w = self.conv.log(start, "Something is wrong. If it persists, will have to reset in 10\n")
        self.assertLess(w, start)
        self.conv.set_state(0, 0, w)
        _, out = self.run_quiet(self.s.sample)
        self.assertIn("fw: Something is wrong. If it persists, will have to reset in 10", out)

    def test_partial_log_line_waits_for_its_end(self):
        self.run_quiet(self.s.attach)
        w = self.conv.log(0, "half a li")
        self.conv.set_state(0, 0, w)
        _, out = self.run_quiet(self.s.sample)
        self.assertNotIn("fw:", out)
        w = self.conv.log(w, "ne\n")
        self.conv.set_state(0, 0, w)
        _, out = self.run_quiet(self.s.sample)
        self.assertIn("fw: half a line", out)

    def test_interruption_and_loss_both_keep_the_history(self):
        self.run_quiet(self.s.attach)
        with mock.patch.object(dw, "LOSS_WAIT", 0.5), \
                mock.patch.object(dw, "read_sysfs", return_value="connected"), \
                mock.patch.object(dw.glob, "glob", side_effect=fake_glob):
            _, out = self.run_quiet(self.s.lost, "connector switched off")
        self.assertIn("link interrupted", out)
        self.assertTrue(list(pathlib.Path(self.tmp.name).glob("*-interrupted.txt")))
        # the kernel reports the disconnect a moment after AUX fails (dock reset)
        states = iter(["connected", "connected", "disconnected"])

        def sysfs(path):
            return next(states) if path.endswith("card1-DP-1/status") else ""

        with mock.patch.object(dw, "read_sysfs", side_effect=sysfs), \
                mock.patch.object(dw.glob, "glob", side_effect=fake_glob):
            _, out = self.run_quiet(self.s.lost, "OSError: short AUX read")
        self.assertIn("DOCK LOST", out)
        saved = list(pathlib.Path(self.tmp.name).glob("*-lost.txt"))
        self.assertTrue(saved)
        self.assertIn("strikes", saved[0].read_text())

    def test_attach_logs_decoded_values(self):
        self.conv.put(0x2022084C, struct.pack("<I", 3840))
        _, out = self.run_quiet(self.s.attach)
        self.assertIn("rx0 pic 3840", out)
        self.assertIn("state block", out)

    def test_only_rc_registers_are_written(self):
        self.run_quiet(self.s.attach)
        for _ in range(3):
            self.run_quiet(self.s.sample)
        allowed = {dw.RC_CMD, dw.RC_OFFSET, dw.RC_LEN, dw.RC_DATA}
        self.assertLessEqual(set(self.conv.writes), allowed)
        # RC_DATA is written once per RC enable (the "PRIUS" key), never with sampled data
        self.assertEqual(self.conv.writes.count(dw.RC_DATA), 1)


class DockWatchInstallTest(unittest.TestCase):
    def test_unit_runs_the_installed_script(self):
        text = UNIT.read_text()
        self.assertIn("ExecStart=/usr/local/libexec/oxp3-dock-watch", text)
        self.assertIn("LogsDirectory=oxp3-dock-watch", text)
        self.assertIn("WantedBy=multi-user.target", text)

    def test_hdr_component_installs_and_removes_it(self):
        text = INSTALLER.read_text()
        self.assertIn('install_file 0755 "$repo/files/oxp3-dock-watch"', text)
        self.assertIn("run systemctl enable oxp3-dock-watch.service", text)
        self.assertRegex(text, r"comp_hdr_install\(\) \{[^}]*install_dock_watch")
        self.assertIn("comp_hdr_uninstall()", text)


if __name__ == "__main__":
    unittest.main()
