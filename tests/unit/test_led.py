"""Unit tests for tools/oxp3-led.py: CLI frame assembly, dry-run output and
the small validation helpers. Nothing is written to a device."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

hid = support.load("oxp3_hid", "oxp3_hid.py")
led = support.load("oxp3_led", "oxp3-led.py")


def ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class LevelValueTest(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(led.level_value("low"), hid.LEVELS["low"])
        self.assertEqual(led.level_value("medium"), hid.LEVELS["medium"])
        self.assertEqual(led.level_value("HIGH"), hid.LEVELS["high"])
        for n in (1, 2, 3, 4):
            self.assertEqual(led.level_value(str(n)), n)
        self.assertEqual(led.level_value("0x03"), 3)

    def test_out_of_range_and_garbage_exit(self):
        for text in ("0", "5", "-1", "off", "bogus", ""):
            with self.assertRaises(SystemExit, msg=text):
                led.level_value(text)


class DryRunCliTest(unittest.TestCase):
    """--dry-run must print exactly the frames it would send and return 0."""

    def _frames(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = led.main(["--dry-run", *argv])
        self.assertEqual(rc, 0)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("dry-run ")]
        return [bytes.fromhex(ln.split(" ", 1)[1]) for ln in lines], buf.getvalue()

    def test_solid_emits_brightness_then_colour_per_side(self):
        frames, out = self._frames(["solid", "ff6600", "--sides", "top"])
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], hid.rgb_brightness(0x06, True, "high"))
        self.assertEqual(frames[1], hid.rgb_solid(0x06, (255, 102, 0)))
        # --dry-run stops after echoing the frames; the human summary is only
        # printed on a real send (covered by StatusAndEmitTest).
        self.assertEqual(out.count("dry-run "), 2)

    def test_solid_breath_uses_f0(self):
        frames, _ = self._frames(["solid", "0033ff", "--sides", "gkey", "--breath"])
        self.assertEqual(frames[1], hid.rgb_breath(0x05, (0, 51, 255)))

    def test_breath_alias_matches_solid_breath(self):
        frames, _ = self._frames(["breath", "0033ff", "--sides", "gkey"])
        self.assertEqual(frames[1], hid.rgb_breath(0x05, (0, 51, 255)))

    def test_brightness_off_emits_only_the_off_frame(self):
        frames, _ = self._frames(["brightness", "off", "--sides", "01,02"])
        self.assertEqual(frames, [hid.rgb_off(0x01, hid.LEVELS["high"]),
                                  hid.rgb_off(0x02, hid.LEVELS["high"])])

    def test_brightness_emits_one_enable_frame_per_side(self):
        frames, _ = self._frames(["brightness", "medium"])
        self.assertEqual(len(frames), len(hid.SIDE_NAMES))
        self.assertEqual(frames[0], hid.rgb_brightness(0x01, True, "medium"))

    def test_off_covers_every_oxp3_zone(self):
        frames, _ = self._frames(["off"])
        self.assertEqual([f[4] for f in frames], hid.SIDE_ALIASES["all"])

    def test_effect_by_id_and_classic_solid(self):
        frames, _ = self._frames(["effect", "aurora", "--sides", "7"])
        self.assertEqual(frames[1], hid.rgb_preset(0x07, 0x01))
        frames, _ = self._frames(["effect", "9", "--sides", "1"])
        self.assertEqual(frames[1], hid.rgb_solid(0x01, hid.PRESET_CLASSIC_RGB))

    def test_effect_warns_about_preset_on_secondary_zones(self):
        _frames, out = self._frames(["effect", "aurora", "--sides", "gkey"])
        self.assertIn("no preset support", out)

    def test_probe_dry_run_uses_one_colour_per_side(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(led.main(["--dry-run", "probe"]), 0)
        lines = buf.getvalue().splitlines()
        sides = [ln for ln in lines if ln.startswith("dry-run side ")]
        self.assertEqual(len(sides), len(hid.SIDE_ALIASES["probe"]))
        self.assertEqual(len(set(sides)), len(sides), "each probe side needs its own line")
        first = bytes.fromhex(sides[0].split(": ", 1)[1])
        self.assertEqual(first[4], 0x00)              # probe starts at side id 0
        self.assertEqual(first[6:9], bytes([255, 0, 0]))  # palette colour 1

    def test_selftest_entrypoint(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(led.main(["selftest"]), 0)
        self.assertIn("PASS", buf.getvalue())

    def test_bad_level_is_a_clean_exit(self):
        for argv in (["solid", "ff0000", "--level", "9"],
                     ["brightness", "nope"],
                     ["effect", "nope"],
                     ["solid", "nope"]):
            with self.assertRaises(SystemExit, msg=str(argv)):
                led.main(["--dry-run", *argv])


class StatusAndEmitTest(unittest.TestCase):
    def test_status_without_device(self):
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             mock.patch.object(led, "hhd_rgb_mode", return_value=None), \
             redirect_stdout(buf):
            self.assertIsNone(led.cmd_status(None, False))
        out = buf.getvalue()
        self.assertIn("NOT FOUND", out)
        self.assertIn("zones (side ids)", out)

    def test_emit_sends_frames_and_reports(self):
        sent = []
        buf = io.StringIO()
        with mock.patch.object(led, "warn_hhd") as warn, \
             mock.patch.object(hid, "open_report", return_value=(42, "/dev/hidraw7")), \
             mock.patch.object(hid, "send", side_effect=lambda fd, fr: sent.extend(fr)), \
             mock.patch.object(led.os, "close") as close, \
             redirect_stdout(buf):
            rc = led.cmd_solid(ns(colour="red", sides=["top"], level="high",
                                  breath=False), False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 2)
        warn.assert_called_once()
        close.assert_called_once_with(42)
        self.assertIn("set red on sides [6]", buf.getvalue())

    def test_emit_dry_run_never_opens_the_device(self):
        with mock.patch.object(hid, "open_report") as opener, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(led.cmd_off(ns(sides=["top"]), True), 0)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
