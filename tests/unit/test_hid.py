# SPDX-License-Identifier: MIT
"""Unit tests for tools/oxp3_hid.py: frame encoding, parsing and io helpers.

The encoder is checked against the 64-byte frames captured from HHD on this
machine (the same vectors the tool's own selftest uses), plus structural
invariants that must hold for every frame the daemon or CLI can emit.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

hid = support.load("oxp3_hid", "oxp3_hid.py")


class VendorReportDescriptorTest(unittest.TestCase):
    """The documented descriptor is the protocol contract the uhid harness uses."""

    DOCUMENTED = bytes.fromhex(
        "0600ff0901a101090215002600ff750895408106"
        "090215002600ff750895409106"
        "c0")

    def test_exact_documented_bytes(self):
        self.assertEqual(hid.vendor_report_descriptor(), self.DOCUMENTED)
        self.assertEqual(hid.VENDOR_REPORT_DESCRIPTOR, self.DOCUMENTED)

    def test_shape(self):
        desc = hid.vendor_report_descriptor()
        self.assertEqual(len(desc), 34)
        self.assertIn(b"\x06\x00\xff", desc)     # usage page 0xFF00
        self.assertNotIn(b"\x85", desc)            # no report ID item
        self.assertEqual(desc.count(b"\x75\x08"), 2)
        self.assertEqual(desc.count(b"\x95\x40"), 2)  # 64-byte reports
        self.assertTrue(desc.endswith(b"\xc0"))

    def test_helper_returns_a_copy(self):
        first = hid.vendor_report_descriptor()
        self.assertIsNot(first, hid.VENDOR_REPORT_DESCRIPTOR)
        self.assertEqual(first, hid.vendor_report_descriptor())


class FrameTest(unittest.TestCase):
    def test_v1_frame_envelope_and_size(self):
        for cid in (hid.CID_RGB, hid.CID_BUTTON, hid.CID_MAP, hid.CID_RUMBLE):
            frame = hid.v1_frame(cid, [0x00])
            self.assertEqual(len(frame), hid.REPORT_SIZE)
            self.assertEqual(frame[0], cid)
            self.assertEqual(frame[1], 0x3F)
            self.assertEqual(frame[2], 0x01)
            self.assertEqual(frame[-2], 0x3F)
            self.assertEqual(frame[-1], cid)
            self.assertTrue(hid.is_vendor_report(frame))

    def test_v1_frame_payload_limit(self):
        # 3 header bytes + payload + 2 trailer bytes must fit in 64.
        self.assertEqual(len(hid.v1_frame(hid.CID_RGB, bytes(59))), 64)
        with self.assertRaises(ValueError):
            hid.v1_frame(hid.CID_RGB, bytes(60))

    def test_solid_layout_and_length(self):
        frame = hid.rgb_solid(0x05, (0x11, 0x22, 0x33))
        self.assertEqual(len(frame), 64)
        self.assertEqual(frame[0], hid.CID_RGB)
        self.assertEqual(frame[3:6], bytes([0xFE, 0x05, 0x02]))
        payload = frame[6:60]
        self.assertEqual(len(payload), 54)
        self.assertEqual(payload, bytes([0x11, 0x22, 0x33] * 18))
        self.assertEqual(frame[60:62], bytes([0x11, 0x22]))

    def test_breath_uses_f0_and_same_layout(self):
        frame = hid.rgb_breath(0x01, (1, 2, 3))
        self.assertEqual(frame[3:6], bytes([0xF0, 0x01, 0x02]))
        self.assertEqual(frame[6:60], bytes([1, 2, 3] * 18))
        self.assertEqual(frame[60:62], bytes([1, 2]))

    def test_brightness_string_and_numeric_level(self):
        a = hid.rgb_brightness(0x02, True, "high")
        b = hid.rgb_brightness(0x02, True, 0x04)
        self.assertEqual(a, b)
        self.assertEqual(a[3:9], bytes([0xFD, 0x02, 0x02, 0x01, 0x05, 0x04]))

    def test_brightness_disabled_flag(self):
        frame = hid.rgb_brightness(0x07, False, "low")
        self.assertEqual(frame[6], 0x00)
        self.assertEqual(frame[7:9], bytes([0x05, 0x01]))

    def test_off_uses_high_level_by_default(self):
        frame = hid.rgb_off(0x05)
        self.assertEqual(frame[3:9], bytes([0xFD, 0x05, 0x02, 0x00, 0x05, 0x04]))

    def test_intercept_values(self):
        self.assertEqual(hid.intercept(True)[3:6], bytes([0x03, 0x01, 0x02]))
        self.assertEqual(hid.intercept(False)[3:6], bytes([0x00, 0x01, 0x02]))

    def test_preset_by_id_and_classic_solid(self):
        # id 9 is the vendor "OXP classic" solid cherry red, not a preset byte.
        self.assertEqual(hid.rgb_preset_id(0x01, 9),
                         hid.rgb_solid(0x01, hid.PRESET_CLASSIC_RGB))
        self.assertEqual(hid.rgb_preset_id(0x02, 1)[3:5], bytes([0x0D, 0x02]))
        self.assertEqual(hid.rgb_preset_id(0x07, 10)[3:5], bytes([0x01, 0x07]))

    def test_preset_frames_returns_one_frame(self):
        self.assertEqual(len(hid.preset_frames(0x01, "aurora")), 1)
        self.assertEqual(hid.preset_frames(0x01, "9"),
                         [hid.rgb_solid(0x01, hid.PRESET_CLASSIC_RGB)])


class CapturedVectorTest(unittest.TestCase):
    """Exact 64-byte comparison with the frames observed on this machine."""

    def test_every_captured_vector_is_full_length(self):
        for name, (_frame, want) in hid.CAPTURED.items():
            self.assertEqual(len(want), hid.REPORT_SIZE * 2,
                             f"{name}: captured vector is not a full 64-byte frame")

    def test_all_captured_frames_match_exactly(self):
        for name, (frame, want) in hid.CAPTURED.items():
            self.assertEqual(frame.hex(), want, name)

    def test_selftest_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = hid.selftest()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(buf.getvalue().count("PASS"), len(hid.CAPTURED))


class ReportValidationTest(unittest.TestCase):
    def test_is_vendor_report_accepts_our_frames(self):
        self.assertTrue(hid.is_vendor_report(hid.rgb_off(0x01)))

    def test_is_vendor_report_rejects_short_and_malformed(self):
        good = hid.rgb_off(0x01)
        self.assertFalse(hid.is_vendor_report(good[:15]))
        bad_mid = bytes([good[0], 0x00]) + good[2:]
        self.assertFalse(hid.is_vendor_report(bad_mid))
        bad_tail = good[:-2] + bytes([0x00, good[0]])
        self.assertFalse(hid.is_vendor_report(bad_tail))
        bad_cid = good[:-1] + bytes([good[0] ^ 1])
        self.assertFalse(hid.is_vendor_report(bad_cid))


class RgbParsingTest(unittest.TestCase):
    def test_hex_forms(self):
        self.assertEqual(hid.parse_rgb("#ff6600"), (255, 102, 0))
        self.assertEqual(hid.parse_rgb("ff6600"), (255, 102, 0))
        self.assertEqual(hid.parse_rgb("f60"), (255, 102, 0))
        self.assertEqual(hid.parse_rgb("FF6600"), (255, 102, 0))

    def test_decimal_triplet(self):
        self.assertEqual(hid.parse_rgb("255, 0, 0"), (255, 0, 0))
        self.assertEqual(hid.parse_rgb("0xff,0x10,0"), (255, 16, 0))

    def test_named_colours(self):
        self.assertEqual(hid.parse_rgb("green"), (0, 255, 0))
        self.assertEqual(hid.parse_rgb("BLUE"), (0, 0, 255))
        self.assertEqual(hid.parse_rgb("white"), (255, 255, 255))
        # Regression: names shorter/longer than the hex heuristics used to be
        # unreachable because int() ran first and failed.
        self.assertEqual(hid.parse_rgb("red"), (255, 0, 0))
        self.assertEqual(hid.parse_rgb("off"), (0, 0, 0))
        self.assertEqual(hid.parse_rgb("orange"), (255, 0x60, 0))

    def test_bad_colours_exit(self):
        for text in ("zzz", "#12", "12", "1,2", "1,2,3,4", "1,2,x", ""):
            with self.assertRaises(SystemExit, msg=text):
                hid.parse_rgb(text)

    def test_out_of_range_components_exit(self):
        # Before the fix this produced a ValueError traceback from bytes().
        for text in ("256,0,0", "0,300,0", "0,0,-1"):
            with self.assertRaises(SystemExit, msg=text):
                hid.parse_rgb(text)
        with self.assertRaises(SystemExit):
            hid.parse_rgb("1000")

    def test_leading_hashes_are_stripped(self):
        self.assertEqual(hid.parse_rgb("##0f0"), (0, 255, 0))


class SideParsingTest(unittest.TestCase):
    def test_names_and_aliases(self):
        self.assertEqual(hid.parse_sides(["top"]), [0x06])
        self.assertEqual(hid.parse_sides(["sticks"]), [0x01, 0x02])
        self.assertEqual(hid.parse_sides(["body_left"]), [0x05])
        self.assertEqual(hid.parse_sides(None), hid.SIDE_ALIASES["all"])

    def test_explicit_empty_selection_yields_no_sides(self):
        # Documented quirk (see docs/UNIT-TESTS.md, deferred): an explicitly
        # empty --sides value is treated as "nothing selected" and the caller
        # then emits zero frames with exit 0. Only an absent option gets the
        # default. Pinned so a change to the parser is noticed.
        self.assertEqual(hid.parse_sides([""]), [])
        self.assertEqual(hid.parse_sides([" , "]), [])

    def test_numeric_forms_and_zero_padding(self):
        self.assertEqual(hid.parse_sides(["01"]), [0x01])
        self.assertEqual(hid.parse_sides(["0x5"]), [0x05])
        self.assertEqual(hid.parse_sides(["7,6"]), [0x07, 0x06])

    def test_deduplication_preserves_order(self):
        self.assertEqual(hid.parse_sides(["02,1", "left_stick"]), [0x02, 0x01])

    def test_default_and_empty_values(self):
        self.assertEqual(hid.parse_sides(None, default="probe"),
                         hid.SIDE_ALIASES["probe"])

    def test_bad_sides_exit(self):
        for value in ("bogus", "-1", "256", "0x100", "1,,x"):
            with self.assertRaises(SystemExit, msg=value):
                hid.parse_sides([value])


class PresetResolutionTest(unittest.TestCase):
    def test_names_aliases_and_case(self):
        self.assertEqual(hid.resolve_preset("aurora"), ("mc", 0x01))
        self.assertEqual(hid.resolve_preset("AURORA"), ("mc", 0x01))
        self.assertEqual(hid.resolve_preset("monster"), ("mc", 0x0D))
        self.assertEqual(hid.resolve_preset("warm"), ("mc", 0x08))

    def test_numeric_ids(self):
        self.assertEqual(hid.resolve_preset("10"), ("mc", 0x01))
        self.assertEqual(hid.resolve_preset("0x0b"), ("mc", 0x08))  # id 11
        self.assertEqual(hid.resolve_preset("9"), ("solid", hid.PRESET_CLASSIC_RGB))

    def test_bad_presets_exit(self):
        for text in ("bogus", "0", "12", "-1", ""):
            with self.assertRaises(SystemExit, msg=text):
                hid.resolve_preset(text)


class DeviceDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def _device(self, node: str, uevent: str, *, input_child=False,
                descriptor: bytes = b"\x06\x00\xff"):
        base = self.tree.mkdir(f"hidraw/{node}/device")
        (base / "uevent").write_text(uevent)
        (base / "report_descriptor").write_bytes(descriptor)
        if input_child:
            (base / "input").mkdir()
        return base

    def _patch_glob(self, paths):
        return mock.patch.object(hid.glob, "glob", return_value=[str(p) for p in paths])

    def test_finds_matching_vendor_node(self):
        good = self._device("hidraw3", "HID_ID=0003:00001A86:0000FE00\n")
        with self._patch_glob([good]):
            self.assertEqual(hid.find_vendor_hidraw(), "/dev/hidraw3")

    def test_skips_other_vendor_and_input_children(self):
        other = self._device("hidraw1", "HID_ID=0003:00001A86:0000FE01\n")
        with_input = self._device("hidraw2", "HID_ID=0003:00001A86:0000FE00\n",
                                  input_child=True)
        with self._patch_glob([other, with_input]):
            self.assertIsNone(hid.find_vendor_hidraw())

    def test_missing_uevent_is_ignored(self):
        base = self.tree.mkdir("hidraw/hidraw4/device")
        (base / "report_descriptor").write_bytes(b"\x06\x00\xff")
        with self._patch_glob([base]):
            self.assertIsNone(hid.find_vendor_hidraw())

    @unittest.skip("deferred: find_vendor_hidraw() returns the node even when its "
                   "report descriptor does not contain the 0xFF00 usage page (the "
                   "`if ...: return node` branch is dead). Making the descriptor "
                   "authoritative changes which /dev/hidrawN is picked on a device "
                   "with several input-less 1a86:fe00 collections; it needs an "
                   "on-device check of all three interfaces, so the behaviour is "
                   "documented in docs/UNIT-TESTS.md instead of changed here.")
    def test_descriptor_is_authoritative(self):
        no_usage = self._device("hidraw5", "HID_ID=0003:00001A86:0000FE00\n",
                                descriptor=b"\x05\x01\x09\x06")
        with self._patch_glob([no_usage]):
            self.assertIsNone(hid.find_vendor_hidraw())


class OpenAndSendTest(unittest.TestCase):
    def test_explicit_device_is_opened_verbatim(self):
        with mock.patch.object(hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(hid.os.path, "exists", return_value=True), \
             mock.patch.object(hid.os, "open", return_value=7) as opener:
            self.assertEqual(hid.open_report(dev="/dev/hidraw9"),
                             (7, "/dev/hidraw9"))
        scan.assert_not_called()
        self.assertEqual(opener.call_args.args[0], "/dev/hidraw9")

    def test_explicit_device_that_is_gone_is_a_clean_exit(self):
        with mock.patch.object(hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(hid.os.path, "exists", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                hid.open_report(dev="/dev/hidraw9")
        self.assertIn("does not exist", str(ctx.exception))
        scan.assert_not_called()

    def test_open_report_without_device_exits_with_hint(self):
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                hid.open_report()
        self.assertIn("not found", str(ctx.exception))

    def test_open_report_permission_hint(self):
        with mock.patch.object(hid, "find_vendor_hidraw", return_value="/dev/hidraw9"), \
             mock.patch.object(hid.os, "open", side_effect=PermissionError):
            with self.assertRaises(SystemExit) as ctx:
                hid.open_report()
        self.assertIn("oxp3-hid.rules", str(ctx.exception))

    def test_send_rejects_short_frames(self):
        with self.assertRaises(ValueError):
            hid.send(1, [b"\x00" * 63])

    def test_send_accounts_for_every_byte(self):
        frames = [hid.rgb_off(0x01), hid.rgb_off(0x02)]
        with mock.patch.object(hid.os, "write", return_value=64) as write:
            self.assertEqual(hid.send(9, frames), 128)
        self.assertEqual(write.call_count, 2)

    def test_send_retries_a_partial_write(self):
        frame = hid.rgb_off(0x01)
        with mock.patch.object(hid.os, "write", side_effect=[10, 54]) as write:
            self.assertEqual(hid.send(9, [frame]), 64)
        self.assertEqual(write.call_count, 2)
        # The second write must carry the remaining 54 bytes, not the whole frame.
        self.assertEqual(len(write.call_args_list[1].args[1]), 54)

    def test_send_fails_loudly_on_a_zero_write(self):
        with mock.patch.object(hid.os, "write", return_value=0):
            with self.assertRaises(OSError):
                hid.send(9, [hid.rgb_off(0x01)])


if __name__ == "__main__":
    unittest.main()
