"""Unit tests for tools/oxp3-rgb-sync.py: HHD state -> left-stick RGB."""

from __future__ import annotations

import unittest

import support

sync = support.load("oxp3_rgb_sync", "oxp3-rgb-sync.py")


class SolidColorTest(unittest.TestCase):
    def state(self, hue=0, sat=100, bright=10):
        return {
            "rgb.handheld.mode.solid.hue": str(hue),
            "rgb.handheld.mode.solid.saturation": str(sat),
            "rgb.handheld.mode.solid.brightness": str(bright),
        }

    def test_red_at_ten_percent_matches_hhd(self):
        # HHD logs "color: 25, 0, 0" for hue 0 / 100% / 10%; rounding borders at 25.5.
        self.assertEqual(sync.solid_rgb(self.state()), (26, 0, 0))

    def test_green_hue(self):
        self.assertEqual(sync.solid_rgb(self.state(hue=120, bright=100)), (0, 255, 0))

    def test_blue_hue_and_saturation(self):
        self.assertEqual(sync.solid_rgb(self.state(hue=240, sat=100, bright=100)), (0, 0, 255))
        self.assertEqual(sync.solid_rgb(self.state(hue=240, sat=0, bright=100)), (255, 255, 255))

    def test_hue_wraps(self):
        self.assertEqual(sync.solid_rgb(self.state(hue=360)), sync.solid_rgb(self.state(hue=0)))

    def test_missing_keys_is_none(self):
        self.assertIsNone(sync.solid_rgb({}))
        self.assertIsNone(sync.solid_rgb({"rgb.handheld.mode.solid.hue": "x"}))


if __name__ == "__main__":
    unittest.main()
