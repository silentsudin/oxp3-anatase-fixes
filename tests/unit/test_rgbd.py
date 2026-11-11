# SPDX-License-Identifier: MIT
"""Unit tests for tools/oxp3-rgbd.py: TOML config parsing, frame planning and
the --check/--once exit codes. No hidraw device and no HHD are required."""

from __future__ import annotations

import argparse
import io
import pathlib
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support

hid = support.load("oxp3_hid", "oxp3_hid.py")
rgbd = support.load("oxp3_rgbd", "oxp3-rgbd.py")

GOOD = """\
[global]
brightness = "medium"
level = "low"

[zones.left_stick]
effect = "solid"
color = "#ff6600"

[zones.gkey]
effect = "preset"
preset = "aurora"

[zones.top]
effect = "off"
"""


def args_for(config, **overrides) -> argparse.Namespace:
    base = dict(config=str(config), dry_run=True, watchdog=30.0,
                reapply_delay=0.5, settle=25.0, no_journal=False,
                hhd_unit="hhd")
    base.update(overrides)
    return argparse.Namespace(**base)


class DevicePinTest(unittest.TestCase):
    """--device is authoritative and never falls back to a discovery scan."""

    def test_pinned_device_skips_discovery(self):
        args = args_for("/nonexistent", device="/dev/hidraw9")
        with mock.patch.object(hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(rgbd.os.path, "exists", return_value=True):
            daemon = rgbd.RgbDaemon(args)
            self.assertEqual(daemon._discover(), "/dev/hidraw9")
        scan.assert_not_called()

    def test_missing_pinned_device_is_absent_not_discovered(self):
        args = args_for("/nonexistent", device="/dev/hidraw9")
        with mock.patch.object(hid, "find_vendor_hidraw") as scan, \
             mock.patch.object(rgbd.os.path, "exists", return_value=False):
            daemon = rgbd.RgbDaemon(args)
            self.assertIsNone(daemon._discover())
        scan.assert_not_called()

    def test_without_device_it_discovers(self):
        args = args_for("/nonexistent")
        # rgbd imports its own oxp3_hid instance; patching the test's copy would
        # leave the real discovery call in place (the vendor node is present here).
        with mock.patch.object(rgbd.hid, "find_vendor_hidraw",
                               return_value="/dev/hidraw4") as scan:
            daemon = rgbd.RgbDaemon(args)
            # The assertion belongs inside the patch: _discover() outside it performs
            # a real scan, so the test used to pass only while the vendor node happened
            # to be /dev/hidraw4 on this host.
            self.assertEqual(daemon._discover(), "/dev/hidraw4")
        self.assertGreaterEqual(scan.call_count, 1)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def cfg(self, text: str):
        return rgbd.load_config(str(self.tree.write("rgb.toml", text)))

    def test_good_config(self):
        cfg = self.cfg(GOOD)
        self.assertEqual(cfg["brightness"], "medium")
        self.assertEqual([z["side"] for z in cfg["zones"]], [0x01, 0x05, 0x06])
        self.assertEqual([z["effect"] for z in cfg["zones"]], ["solid", "preset", "off"])
        # global level applies to every zone that does not override it
        self.assertEqual([z["level"] for z in cfg["zones"]], ["low", "low", "low"])
        self.assertEqual(cfg["zones"][0]["color"], (0xFF, 0x66, 0x00))
        self.assertEqual(cfg["zones"][1]["preset_kind"], "mc")
        self.assertEqual(cfg["zones"][1]["preset_value"], 0x01)

    def test_defaults(self):
        cfg = self.cfg("[zones.top]\n")
        self.assertEqual(cfg["brightness"], "high")
        zone = cfg["zones"][0]
        self.assertEqual(zone["effect"], "solid")
        self.assertEqual(zone["color"], (255, 255, 255))
        self.assertEqual(zone["level"], "high")

    def test_keep_brightness_and_explicit_zone_level(self):
        cfg = self.cfg('[global]\nbrightness = "keep"\n'
                       '[zones.top]\nlevel = "high"\ncolor = "010203"\n')
        self.assertEqual(cfg["brightness"], "keep")
        self.assertEqual(cfg["zones"][0]["level"], "high")
        # "keep" only makes sense for the off frame, which needs a level.
        cfg2 = self.cfg('[global]\nbrightness = "keep"\n[zones.top]\n')
        self.assertEqual(cfg2["zones"][0]["level"], "high")

    def test_zero_padded_and_hex_zone_ids(self):
        # Regression: int("05", 0) raises ValueError in Python, so a TOML key
        # like [zones.05] used to be rejected even though the LED CLI accepts
        # zero-padded ids (fixed in the same round as the LED side parser).
        cfg = self.cfg("[zones.05]\n[zones.0x06]\n[zones.7]\n")
        self.assertEqual([z["side"] for z in cfg["zones"]], [0x05, 0x06, 0x07])

    def test_other_model_zone_is_accepted(self):
        cfg = self.cfg("[zones.0x03]\n")
        self.assertEqual(cfg["zones"][0]["side"], 0x03)

    def test_preset_id_nine_is_a_solid(self):
        cfg = self.cfg('[zones.top]\neffect = "preset"\npreset = "9"\n')
        self.assertEqual(cfg["zones"][0]["preset_kind"], "solid")
        self.assertEqual(cfg["zones"][0]["preset_value"], hid.PRESET_CLASSIC_RGB)

    def test_error_paths(self):
        cases = {
            "no zones": '[global]\nbrightness = "high"\n',
            "unknown effect": '[zones.top]\neffect = "disco"\n',
            "bad brightness": '[global]\nbrightness = "max"\n[zones.top]\n',
            "bad level": '[zones.top]\nlevel = "max"\n',
            "unknown zone": '[zones.nonsense]\n',
            "bad colour": '[zones.top]\ncolor = "#zzz"\n',
            "preset missing": '[zones.top]\neffect = "preset"\n',
            "preset invalid": '[zones.top]\neffect = "preset"\npreset = "12"\n',
            "id out of range": '[zones.8]\n',
            "hex out of range": '[zones.0x100]\n',
        }
        for label, text in cases.items():
            with self.assertRaises(ValueError, msg=label):
                self.cfg(text)

    def test_missing_file_is_not_a_config_error_type(self):
        with self.assertRaises(OSError):
            rgbd.load_config(str(self.tree.path("absent.toml")))


class FramePlanTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def cfg(self, text: str):
        return rgbd.load_config(str(self.tree.write("rgb.toml", text)))

    def test_brightness_is_emitted_before_the_colour(self):
        frames = rgbd.build_frames(self.cfg(GOOD))
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[0], hid.rgb_brightness(0x01, True, "low"))
        self.assertEqual(frames[1], hid.rgb_solid(0x01, (0xFF, 0x66, 0x00)))
        self.assertEqual(frames[2], hid.rgb_brightness(0x05, True, "low"))
        self.assertEqual(frames[3], hid.rgb_preset(0x05, 0x01))
        # "off" sends only the disable frame, even when a brightness is set.
        self.assertEqual(frames[4], hid.rgb_brightness(0x06, False, "low"))

    def test_keep_brightness_skips_the_enable_frame(self):
        cfg = self.cfg('[global]\nbrightness = "keep"\n[zones.top]\ncolor = "112233"\n')
        frames = rgbd.build_frames(cfg)
        self.assertEqual(frames, [hid.rgb_solid(0x06, (0x11, 0x22, 0x33))])

    def test_breath_effect(self):
        cfg = self.cfg('[zones.top]\neffect = "breath"\ncolor = "010203"\n')
        self.assertEqual(rgbd.build_frames(cfg),
                         [hid.rgb_brightness(0x06, True, "high"),
                          hid.rgb_breath(0x06, (1, 2, 3))])

    def test_only_side_filter(self):
        cfg = self.cfg(GOOD)
        frames = rgbd.build_frames(cfg, only_side=0x05)
        self.assertEqual(frames[1], hid.rgb_preset(0x05, 0x01))
        self.assertTrue(all(f[4] == 0x05 for f in frames))
        self.assertEqual(rgbd.build_frames(cfg, only_side=0x07), [])


class DaemonStatusTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.config = self.tree.write("rgb.toml", GOOD)

    def _daemon(self, **overrides):
        return rgbd.RgbDaemon(args_for(self.config, **overrides))

    def test_check_reports_and_fails_without_device(self):
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             mock.patch.object(rgbd, "journal_readable", return_value=False), \
             mock.patch.object(rgbd, "hhd_rgb_mode", return_value="disabled"), \
             redirect_stdout(buf):
            rc = self._daemon().check()
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("NOT FOUND", out)
        self.assertIn("zone left_stick", out)
        self.assertIn("frames        : 5", out)

    def test_check_succeeds_with_a_device(self):
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw",
                               return_value="/dev/hidraw7"), \
             mock.patch.object(rgbd.os, "stat",
                               return_value=mock.Mock(st_mode=0o660, st_uid=0, st_gid=18)), \
             mock.patch.object(rgbd, "journal_readable", return_value=True), \
             mock.patch.object(rgbd, "hhd_rgb_mode", return_value="disabled"), \
             redirect_stdout(buf):
            self.assertEqual(self._daemon().check(), 0)
        self.assertIn("readable", buf.getvalue())

    def test_check_with_bad_config_returns_1(self):
        self.config.write_text("[zones.nonsense]\n", encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             mock.patch.object(rgbd, "journal_readable", return_value=False), \
             mock.patch.object(rgbd, "hhd_rgb_mode", return_value=None), \
             redirect_stdout(buf):
            self.assertEqual(self._daemon().check(), 1)
        self.assertIn("config        : ERROR", buf.getvalue())

    def test_run_once_dry_run_with_device_returns_0(self):
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw", return_value="/dev/hidraw7"), \
             redirect_stdout(buf):
            self.assertEqual(self._daemon().run_once(), 0)
        self.assertEqual(buf.getvalue().count("\n"), 5)

    def test_run_once_without_device_is_not_a_silent_success(self):
        # Regression: apply() logs "node not found" and returned None, so
        # --once exited 0 even though nothing had been applied.
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             redirect_stdout(io.StringIO()), mock.patch.object(rgbd, "log"):
            self.assertEqual(self._daemon().run_once(), 1)

    def test_run_once_permission_denied_returns_1(self):
        daemon = self._daemon(dry_run=False)
        with mock.patch.object(hid, "find_vendor_hidraw", return_value="/dev/hidraw7"), \
             mock.patch.object(rgbd.os, "open", side_effect=PermissionError), \
             mock.patch.object(rgbd, "log"), redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.run_once(), 1)
        self.assertEqual(daemon.last_error, "permission")

    def test_run_once_bad_config_returns_1(self):
        self.config.write_text("[zones.nonsense]\n", encoding="utf-8")
        with mock.patch.object(rgbd, "log"), redirect_stdout(io.StringIO()):
            self.assertEqual(self._daemon().run_once(), 1)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def test_selftest_entrypoint(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(rgbd.main(["--selftest"]), 0)
        self.assertIn("config parses (3 zones, 5 frames)", buf.getvalue())

    def test_takeover_text_mentions_all_three_options(self):
        text = rgbd.takeover_text()
        self.assertIn("hhd.settings.rgb=false", text)
        self.assertIn("rgb.handheld.mode.mode=disabled", text)
        self.assertIn("plugins.yml", text)

    def test_check_cli_exit_code(self):
        cfg = self.tree.write("rgb.toml", GOOD)
        buf = io.StringIO()
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             mock.patch.object(rgbd, "journal_readable", return_value=False), \
             mock.patch.object(rgbd, "hhd_rgb_mode", return_value=None), \
             redirect_stdout(buf):
            self.assertEqual(rgbd.main(["--check", "--config", str(cfg)]), 1)
        buf = io.StringIO()
        bad = self.tree.write("bad.toml", "[zones.nonsense]\n")
        with mock.patch.object(hid, "find_vendor_hidraw", return_value=None), \
             mock.patch.object(rgbd, "journal_readable", return_value=False), \
             mock.patch.object(rgbd, "hhd_rgb_mode", return_value=None), \
             redirect_stdout(buf):
            self.assertEqual(rgbd.main(["--check", "--config", str(bad)]), 1)
        self.assertIn("ERROR", buf.getvalue())

    def test_missing_config_file(self):
        with mock.patch.object(rgbd, "log"), redirect_stdout(io.StringIO()):
            rc = rgbd.main(["--once", "--dry-run",
                            "--config", str(self.tree.path("absent.toml"))])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
