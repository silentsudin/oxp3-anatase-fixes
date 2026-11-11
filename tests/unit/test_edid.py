# SPDX-License-Identifier: MIT
"""Unit tests for tools/display/oxp3-edid.py.

The EDID blob is built here from scratch (our own synthetic bytes, not a
vendor blob) so every parser path - chroma packing, detailed timing, CTA-861
extension blocks, HDR static metadata, checksums - has a deterministic input.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support

edid = support.load("oxp3_edid", "display/oxp3-edid.py")

SRGB = {"red": (0.640, 0.330), "green": (0.300, 0.600),
        "blue": (0.150, 0.060), "white": (0.3127, 0.3290)}
WIDE = {"red": (0.680, 0.320), "green": (0.265, 0.690),
        "blue": (0.150, 0.060), "white": (0.3127, 0.3290)}


def _code(v):
    return int(round(v * 1024))


def pack_chroma(prim):
    rx, ry = _code(prim["red"][0]), _code(prim["red"][1])
    gx, gy = _code(prim["green"][0]), _code(prim["green"][1])
    bx, by = _code(prim["blue"][0]), _code(prim["blue"][1])
    wx, wy = _code(prim["white"][0]), _code(prim["white"][1])
    rl = ((rx & 3) << 6) | ((ry & 3) << 4) | ((gx & 3) << 2) | (gy & 3)
    bl = ((bx & 3) << 6) | ((by & 3) << 4) | ((wx & 3) << 2) | (wy & 3)
    return bytes([rl, bl, rx >> 2, ry >> 2, gx >> 2, gy >> 2,
                  bx >> 2, by >> 2, wx >> 2, wy >> 2])


def descriptor(tag, payload):
    d = bytearray(18)
    d[0:3] = b"\x00\x00\x00"
    d[3] = tag
    d[4] = 0
    d[5:5 + len(payload)] = payload[:13]
    return bytes(d)


def base_block(prim=SRGB, **overrides):
    b = bytearray(128)
    b[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    b[8:10] = (0x0443).to_bytes(2, "big")        # manufacturer "ABC"
    b[10:12] = (0x1234).to_bytes(2, "little")    # product code
    b[16] = overrides.get("week", 5)
    b[17] = overrides.get("year", 35)            # 2025
    b[18], b[19] = 1, 4                          # EDID 1.4
    b[20] = overrides.get("video_input", 0xB5)   # digital, 10-bit, DisplayPort
    b[21], b[22] = 30, 20
    b[23] = 120                                  # gamma 2.20
    b[24] = 0x0A
    b[0x19:0x23] = pack_chroma(prim)
    # 1920x1200@~155 Hz detailed timing (400 MHz pixel clock).
    dt = bytearray(18)
    dt[0:2] = (40000).to_bytes(2, "little")
    ha, hb, va, vb = 1920, 160, 1200, 40
    dt[2], dt[3] = ha & 0xFF, hb & 0xFF
    dt[4] = ((ha >> 8) << 4) | (hb >> 8)
    dt[5], dt[6] = va & 0xFF, vb & 0xFF
    dt[7] = ((va >> 8) << 4) | (vb >> 8)
    b[0x36:0x48] = dt
    b[0x48:0x5A] = descriptor(0xFC, b"OXP3 Panel")
    b[0x5A:0x6C] = descriptor(0xFD, bytes([48, 165, 30, 200, 40]))
    b[0x6C:0x7E] = descriptor(0xFE, b"SN123")
    b[126] = overrides.get("extensions", 1)
    b[127] = (-sum(b[:127])) & 0xFF
    return bytes(b)


def hdr_static_block(eotf=0x04, meta=0x01, max_lum=0x82, max_fall=0x50, min_lum=0x00):
    data = bytes([0x06, eotf, meta, max_lum, max_fall, min_lum])
    return bytes([(7 << 5) | len(data)]) + data


def colorimetry_block(b1=0x90, b2=0x01):
    data = bytes([0x05, b1, b2])
    return bytes([(7 << 5) | len(data)]) + data


def cta_block(blocks=b"", revision=3):
    e = bytearray(128)
    e[0] = 0x02
    e[1] = revision
    data_end = 4 + len(blocks)
    e[2] = data_end
    e[3] = 0
    e[4:data_end] = blocks
    e[127] = (-sum(e[:127])) & 0xFF
    return bytes(e)


def edid_with_extension(ext, **kwargs):
    return base_block(**kwargs) + ext


class ChromaAndTimingTest(unittest.TestCase):
    def test_mfg_id(self):
        self.assertEqual(edid._mfg_id(0x0443), "ABC")
        self.assertEqual(edid._mfg_id((1 << 10) | (24 << 5) | 5), "AXE")

    def test_xy_packing_round_trip(self):
        # 0.640 is encoded as 655/1024: high byte 0xA3, low two bits in 0xC0.
        self.assertAlmostEqual(edid._xy(0xA3, 0xC0, 6), 0.6396, places=4)
        self.assertAlmostEqual(edid._xy(0x00, 0xC0, 6), 3 / 1024, places=6)

    def test_luminance_code(self):
        self.assertEqual(edid._lum(0), 50.0)
        self.assertEqual(edid._lum(32), 100.0)
        self.assertEqual(edid._lum(64), 200.0)
        self.assertGreater(edid._lum(200), edid._lum(100))

    def test_parse_chroma(self):
        chroma = edid.parse_chroma(pack_chroma(WIDE))
        for name in ("red", "green", "blue", "white"):
            for got, want in zip(chroma[name], WIDE[name]):
                self.assertAlmostEqual(got, want, delta=1 / 1024)

    def test_parse_detail_timing(self):
        dt = edid.parse_detail_timing(base_block()[0x36:0x48])
        self.assertEqual(dt["pixel_clock_khz"], 400_000)
        self.assertEqual((dt["width"], dt["height"]), (1920, 1200))
        self.assertEqual((dt["h_total"], dt["v_total"]), (2080, 1240))
        self.assertAlmostEqual(dt["refresh_hz"], 155.086, places=2)


class DecodeTest(unittest.TestCase):
    def test_full_decode(self):
        blob = edid_with_extension(cta_block(hdr_static_block() + colorimetry_block()))
        info = edid.decode(blob)
        self.assertTrue(info["base_checksum_ok"])
        self.assertEqual(info["mfg"], "ABC")
        self.assertEqual(info["product_code"], "0x1234")
        self.assertEqual(info["year"], 2025)
        self.assertEqual(info["edid_version"], "1.4")
        self.assertEqual(info["video_input"]["bit_depth"], 10)
        self.assertEqual(info["video_input"]["interface"], "DisplayPort")
        self.assertEqual(info["name"], "OXP3 Panel")
        self.assertEqual(info["serial"], "SN123")
        self.assertEqual(info["range_limits"]["max_vrate_hz"], 165)
        self.assertEqual(info["range_limits"]["max_pclk_mhz"], 400)
        cap = info["capability"]
        self.assertEqual(cap["native_mode"], "1920x1200")
        self.assertAlmostEqual(cap["max_refresh_hz"], 155.086, places=2)
        self.assertEqual(cap["bit_depth"], 10)
        self.assertTrue(cap["pq"])
        self.assertFalse(cap["hlg"])
        self.assertIn("BT2020RGB", cap["colorimetry"])
        self.assertIn("AdobeRGB", cap["colorimetry"])
        self.assertIn("DCI-P3", cap["colorimetry"])
        self.assertIn("10-bit capable", cap["verdict"])
        self.assertIn("PQ/HDR10", cap["verdict"])

    def test_hdr_static_numbers(self):
        info = edid.decode(edid_with_extension(
            cta_block(hdr_static_block(eotf=0x0C))))
        h = info["capability"]["hdr_static_metadata"]
        self.assertEqual(h["eotf"], ["SMPTE-ST-2084-PQ", "HLG"])
        self.assertEqual(h["supported_eotf_mask"], 0x0C)
        self.assertTrue(h["static_metadata_type1"])
        self.assertEqual(h["max_luminance_nits"], round(edid._lum(0x82), 1))
        self.assertEqual(h["max_fall_nits"], round(edid._lum(0x50), 1))

    def test_gamut_classification(self):
        self.assertEqual(edid.decode(base_block(extensions=0))["capability"]["gamut_class"],
                         "sRGB-class")
        wide = edid.decode(base_block(SRGB if False else WIDE, extensions=0))
        self.assertIn("wide gamut", wide["capability"]["gamut_class"])

    def test_sdr_only_panel(self):
        info = edid.decode(base_block(extensions=0))
        cap = info["capability"]
        self.assertFalse(cap["pq"])
        self.assertEqual(cap["hdr_claim"], "SDR only as described by EDID")
        # A 10-bit panel still advertises depth, so the verdict names that and
        # stops short of any HDR claim.
        self.assertIn("10-bit capable", cap["verdict"])
        self.assertNotIn("PQ", cap["verdict"])
        self.assertNotIn("BT.2020", cap["verdict"])

    def test_bad_checksum_is_reported_not_fatal(self):
        blob = bytearray(base_block(extensions=0))
        blob[127] ^= 0xFF
        info = edid.decode(bytes(blob))
        self.assertFalse(info["base_checksum_ok"])
        self.assertEqual(info["mfg"], "ABC")

    def test_not_an_edid(self):
        with self.assertRaises(ValueError):
            edid.decode(b"")
        with self.assertRaises(ValueError):
            edid.decode(b"\x00" * 128)

    def test_truncated_extension_is_ignored(self):
        blob = base_block(extensions=2) + cta_block()
        info = edid.decode(blob)  # second extension is missing
        self.assertEqual(len(info["cta"]), 1)
        self.assertEqual(info["extensions"], 2)

    def test_displayid_and_unknown_extensions(self):
        displayid = bytearray(128)
        displayid[0] = 0x70
        displayid[1] = 0x20
        displayid[127] = (-sum(displayid[:127])) & 0xFF
        unknown = bytearray(128)
        unknown[0] = 0x10
        unknown[127] = (-sum(unknown[:127])) & 0xFF
        info = edid.decode(base_block(extensions=2) + bytes(displayid) + bytes(unknown))
        self.assertEqual(info["displayid"][0]["version_byte"], 0x20)
        self.assertEqual(info["other_extensions"][0]["tag"], "0x10")

    def test_cta_malformed_block_stops_cleanly(self):
        ext = bytearray(cta_block())
        ext[2] = 0  # data_block_offset 0 -> treated as "to 127"
        ext[4] = 0xFF  # a block header claiming a huge length
        ext[127] = (-sum(ext[:127])) & 0xFF
        info = edid.decode(base_block() + bytes(ext))
        self.assertEqual(len(info["cta"]), 1)

    def test_render_contains_verdict(self):
        info = edid.decode(edid_with_extension(cta_block(hdr_static_block())))
        text = edid.render(info)
        self.assertIn("VERDICT", text)
        self.assertIn("OXP3 Panel", text)
        self.assertIn("1920x1200", text)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def test_json_output(self):
        path = self.tree.write_bytes("panel.bin",
                                     edid_with_extension(cta_block(hdr_static_block())))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = edid.main(["--json", str(path)])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data[str(path)]["mfg"], "ABC")

    def test_invalid_file_is_exit_1(self):
        path = self.tree.write_bytes("bad.bin", b"not an edid")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = edid.main([str(path)])
        self.assertEqual(rc, 1)
        self.assertIn("not an EDID base block", err.getvalue())

    def test_all_with_no_edids_is_exit_1(self):
        with mock.patch.object(edid, "find_edids", return_value=[]), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(edid.main(["--all"]), 1)

    def test_find_edids_skips_empty_attributes(self):
        good = self.tree.write_bytes("good/edid", base_block())
        empty = self.tree.write_bytes("empty/edid", b"")
        with mock.patch.object(edid.glob, "glob",
                               return_value=[str(empty), str(good)]):
            self.assertEqual(edid.find_edids(), [str(good)])



if __name__ == "__main__":
    unittest.main()
