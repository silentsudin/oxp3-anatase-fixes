# SPDX-License-Identifier: MIT
"""Offline unit tests for the uhid integration harness helpers.

Everything asserted here is hardware-free: the uhid wire format is packed and
unpacked from bytes, the report builders are compared with the documented
protocol, and sysfs/log parsing runs against synthetic trees and strings.  The
opt-in end-to-end harness itself lives in tests/integration/hid/ and is only
run as root (see docs/VALIDATION.md).
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from unittest import mock

import support

INTEGRATION = support.REPO / "tests" / "integration" / "hid"
if str(INTEGRATION) not in sys.path:
    sys.path.insert(0, str(INTEGRATION))

import uhid_harness as uh  # noqa: E402

hid = support.load("oxp3_hid", "oxp3_hid.py")

# The descriptor documented in docs/hw/home-button-leds.md, written out here
# independently so the builder and the documentation cannot drift silently.
DOCUMENTED_DESCRIPTOR = bytes.fromhex(
    "0600ff0901a101090215002600ff750895408106"
    "090215002600ff750895409106"
    "c0")


class IdentityTest(unittest.TestCase):
    def test_test_device_identity_is_unmistakable(self):
        self.assertIn("integration test", uh.TEST_NAME)
        self.assertIn("integration-test", uh.TEST_PHYS)
        self.assertEqual((uh.VID, uh.PID), (hid.VID, hid.PID))
        self.assertLess(len(uh.TEST_NAME), 128)
        self.assertLess(len(uh.TEST_PHYS), 64)

    def test_descriptor_comes_from_the_protocol_module(self):
        self.assertEqual(uh.build_create2_event()[uh._RD_DATA_OFFSET:
                                                   uh._RD_DATA_OFFSET + 34],
                         DOCUMENTED_DESCRIPTOR)
        self.assertEqual(hid.vendor_report_descriptor(), DOCUMENTED_DESCRIPTOR)

    def test_descriptor_shape(self):
        desc = DOCUMENTED_DESCRIPTOR
        self.assertIn(b"\x06\x00\xff", desc)       # Usage Page (0xFF00)
        self.assertNotIn(b"\x85", desc)            # no Report ID item
        self.assertEqual(desc.count(b"\x75\x08"), 2)   # report size 8
        self.assertEqual(desc.count(b"\x95\x40"), 2)   # report count 64
        self.assertTrue(desc.endswith(b"\xc0"))


class Create2Test(unittest.TestCase):
    def test_event_layout(self):
        event = uh.build_create2_event()
        self.assertEqual(len(event), uh.UHID_EVENT_SIZE)
        self.assertEqual(uh.UHID_EVENT_SIZE, 4376)
        self.assertEqual(struct.unpack_from("<I", event, 0)[0], uh.UHID_CREATE2)
        self.assertEqual(event[4:4 + len(uh.TEST_NAME)],
                         uh.TEST_NAME.encode())
        self.assertEqual(event[132:132 + len(uh.TEST_PHYS)],
                         uh.TEST_PHYS.encode())
        rd_size, bus, vendor, product, version, country = struct.unpack_from(
            "<HHIIII", event, 260)
        self.assertEqual(rd_size, len(DOCUMENTED_DESCRIPTOR))
        self.assertEqual((bus, vendor, product, version, country),
                         (uh.BUS_USB, uh.VID, uh.PID, uh.VERSION, uh.COUNTRY))
        self.assertEqual(event[280:280 + rd_size], DOCUMENTED_DESCRIPTOR)

    def test_short_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            uh.build_create2_event(name="x" * 128)
        with self.assertRaises(ValueError):
            uh.build_create2_event(phys="y" * 64)

    def test_destroy_event_is_just_the_type(self):
        self.assertEqual(uh.build_destroy_event(), b"\x01\x00\x00\x00")


class Input2Test(unittest.TestCase):
    def test_event_layout(self):
        report = hid.rgb_off(0x06)
        event = uh.build_input2_event(report)
        self.assertEqual(struct.unpack_from("<I", event, 0)[0], uh.UHID_INPUT2)
        self.assertEqual(struct.unpack_from("<H", event, 4)[0], len(report))
        self.assertEqual(event[6:], report)
        # Short writes are legal for the kernel; this is the minimal encoding.
        self.assertEqual(len(event), 6 + len(report))


class OutputEventTest(unittest.TestCase):
    def _event(self, frame, rtype=uh.UHID_REPORT_OUTPUT):
        ev = bytearray(uh.UHID_EVENT_SIZE)
        struct.pack_into("<I", ev, 0, uh.UHID_OUTPUT)
        ev[4:4 + len(frame)] = frame
        struct.pack_into("<H", ev, 4100, len(frame))
        ev[4102] = rtype
        return bytes(ev)

    def test_parses_the_exact_wire_bytes(self):
        frame = hid.rgb_solid(0x06, (0xFF, 0x66, 0x00))
        data, rtype = uh.parse_output_event(self._event(frame))
        self.assertEqual(data, frame)
        self.assertEqual(rtype, uh.UHID_REPORT_OUTPUT)

    def test_ignores_non_output_events(self):
        ev = bytearray(uh.UHID_EVENT_SIZE)
        struct.pack_into("<I", ev, 0, uh.UHID_CREATE2)
        self.assertIsNone(uh.parse_output_event(bytes(ev)))
        self.assertIsNone(uh.parse_output_event(b""))


class ButtonReportTest(unittest.TestCase):
    def test_press_matches_the_documented_offsets(self):
        report = uh.button_report(0x24, True)
        self.assertEqual(len(report), hid.REPORT_SIZE)
        self.assertEqual(report[0], hid.CID_BUTTON)
        self.assertEqual(report[1], 0x3F)
        self.assertEqual(report[-2:], bytes([0x3F, hid.CID_BUTTON]))
        self.assertEqual(report[6], 0x24)
        self.assertEqual(report[12], 1)
        self.assertTrue(hid.is_vendor_report(report))

    def test_release_sets_byte_12_to_zero(self):
        self.assertEqual(uh.button_report(0x24, False)[12], 0)

    def test_other_ids_can_be_injected(self):
        self.assertEqual(uh.button_report(0x21, True)[6], 0x21)


class DeviceTransportTest(unittest.TestCase):
    """UhidDevice's fd protocol, exercised over a pipe instead of /dev/uhid."""

    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        base = self.tree.mkdir("hidraw7/device")
        (base / "uevent").write_text(
            f"HID_ID={uh.expected_hid_id()}\n"
            f"HID_NAME={uh.TEST_NAME}\nHID_PHYS={uh.TEST_PHYS}\n")

    def _device(self):
        return uh.UhidDevice(sysfs_root=str(self.tree.root))

    def test_create_writes_the_create2_event(self):
        dev = self._device()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        try:
            with mock.patch.object(uh.os, "open", return_value=write_fd), \
                 mock.patch.object(uh.os.path, "exists", return_value=True):
                node = dev.create()
            self.assertEqual(node, "/dev/hidraw7")
            self.assertEqual(os.read(read_fd, uh.UHID_EVENT_SIZE),
                             uh.build_create2_event())
            self.assertEqual(dev.fd, write_fd)
        finally:
            dev.fd = None
            os.close(write_fd)

    def test_inject_writes_an_input2_event(self):
        dev = self._device()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        try:
            dev.fd = write_fd
            report = uh.button_report(0x24, True)
            dev.inject(report)
            self.assertEqual(os.read(read_fd, 1024), uh.build_input2_event(report))
        finally:
            dev.fd = None
            os.close(write_fd)

    def test_destroy_sends_the_destroy_event(self):
        dev = self._device()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        dev.fd = write_fd
        dev.node = "/dev/hidraw7"
        with mock.patch.object(uh, "wait_hidraw_gone") as gone:
            dev.destroy()
        self.assertEqual(os.read(read_fd, 16), uh.build_destroy_event())
        self.assertIsNone(dev.fd)
        gone.assert_called_once()

    def test_collect_outputs_reads_a_queued_report(self):
        dev = self._device()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        frame = hid.rgb_solid(0x06, (0x11, 0x22, 0x33))
        event = bytearray(uh.UHID_EVENT_SIZE)
        struct.pack_into("<I", event, 0, uh.UHID_OUTPUT)
        event[4:4 + len(frame)] = frame
        struct.pack_into("<H", event, 4100, len(frame))
        event[4102] = uh.UHID_REPORT_OUTPUT
        os.write(write_fd, event)
        os.close(write_fd)
        os.set_blocking(read_fd, False)
        try:
            dev.fd = read_fd
            self.assertEqual(dev.collect_outputs(1, 2.0),
                             [(frame, uh.UHID_REPORT_OUTPUT)])
            self.assertEqual(dev.drain_outputs(), [])
        finally:
            dev.fd = None


class UeventTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def _device(self, node, uevent):
        base = self.tree.mkdir(f"{node}/device")
        (base / "uevent").write_text(uevent)
        return base

    def _uevent(self, vid=uh.VID, pid=uh.PID, name=uh.TEST_NAME,
                phys=uh.TEST_PHYS):
        return (f"HID_ID={uh.expected_hid_id(vid, pid)}\n"
                f"HID_NAME={name}\nHID_PHYS={phys}\n")

    def test_finds_only_the_test_identity(self):
        self._device("hidraw3", self._uevent())
        self._device("hidraw4", self._uevent(vid=0x1A86, pid=0xFE01))
        self._device("hidraw5", self._uevent(name="ONEXPLAYER 3"))
        self._device("hidraw6", self._uevent(phys="usb-0000:00:14.0-3/input2"))
        found = uh.find_hidraw_nodes(sysfs_root=str(self.tree.root))
        self.assertEqual(found, ["/dev/hidraw3"])

    def test_no_match_returns_empty(self):
        self._device("hidraw2", self._uevent(pid=0x0001))
        self.assertEqual(uh.find_hidraw_nodes(sysfs_root=str(self.tree.root)), [])


class LogParsingTest(unittest.TestCase):
    def test_node_paths_in_log_order(self):
        text = (
            "listening on /dev/hidraw4 (identity 1a86:fe00 name='ONEXPLAYER 3')\n"
            "device lost: EIO (I/O error); retrying in 0.5s (attempt 1)\n"
            "reconnect #1 to /dev/hidraw9 (identity 1a86:fe00 name='ONEXPLAYER 3')\n"
            "applied 2 frame(s) to /dev/hidraw9 (one-shot)\n")
        self.assertEqual(uh.node_paths_in_log(text),
                         ["/dev/hidraw4", "/dev/hidraw9", "/dev/hidraw9"])

    def test_watchdog_heartbeat_is_included(self):
        text = ("watchdog: listening on /dev/hidraw4 (reconnects=0)\n"
                "uinput: created 'OXP3 Home Button'\n")
        # The heartbeat names the same node; counting it is harmless and keeps
        # the safety assertion strict about every node the daemon claims.
        self.assertEqual(uh.node_paths_in_log(text), ["/dev/hidraw4"])

    def test_unrelated_lines_are_ignored(self):
        text = ("uinput: created 'OXP3 Home Button'\n"
                "button 0x24 pressed\n")
        self.assertEqual(uh.node_paths_in_log(text), [])

    def test_parse_report_lines(self):
        text = ("note: HHD rgb plugin is active\n"
                "send " + "aa" * 64 + "\n"
                "dry-run " + "bb" * 64 + "\n"
                "set red on sides [6]\n")
        frames = uh.parse_report_lines(text)
        self.assertEqual(frames, [bytes([0xAA]) * 64, bytes([0xBB]) * 64])

    def test_parse_input_events(self):
        first = uh.INPUT_EVENT.pack(1, 2, uh.EV_KEY, uh.KEY_HOMEPAGE, 1)
        second = uh.INPUT_EVENT.pack(3, 4, uh.EV_SYN, 0, 0)
        self.assertEqual(uh.parse_input_events(first + second),
                         [(uh.EV_KEY, uh.KEY_HOMEPAGE, 1), (uh.EV_SYN, 0, 0)])
        # A trailing partial record is ignored, never crash the reader.
        self.assertEqual(uh.parse_input_events(first + b"\x00"), 
                         [(uh.EV_KEY, uh.KEY_HOMEPAGE, 1)])


if __name__ == "__main__":
    unittest.main()
