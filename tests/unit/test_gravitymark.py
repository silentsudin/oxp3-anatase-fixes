# SPDX-License-Identifier: MIT
"""Tests for tools/gpu-bench/gravitymark.py - the cross-API GravityMark runner.

The numbers this tool produces decide whether DX11 is really slower and by how much, so the
statistics and the frametime-report parser are pinned here: a unit slip (seconds vs ms) would
turn a 15% gap into a 1000x one and hide the answer.
"""
from __future__ import annotations

import unittest

import support

GM = support.load("gravitymark", "gpu-bench/gravitymark.py")


class TimesParseTest(unittest.TestCase):
    def test_parses_the_delta_column_ignoring_flags(self):
        # the real report shape: '<index>: <timestamp> <delta seconds> <flags>'
        text = "      0:     3.846979  0.000000  0\n      1:     3.848636  0.001657  0\n"
        self.assertEqual(GM.parse_times(self.tree_file(text)), [0.0, 0.001657])

    def test_skips_malformed_lines(self):
        text = "header\n0: 1.0 0.010 0\n\nbad line here\n1: 1.1 x 0\n"
        self.assertEqual(GM.parse_times(self.tree_file(text)), [0.01])

    def tree_file(self, text):
        import tempfile
        from pathlib import Path
        t = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        t.write(text)
        t.close()
        self.addCleanup(Path(t.name).unlink)
        return Path(t.name)


class SummaryTest(unittest.TestCase):
    def test_mean_and_low_are_in_consistent_units(self):
        # 10 ms per frame for 100 frames = 100 fps; worst frame 20 ms = 50 fps 1% low
        d = [0.010] * 99 + [0.020]
        s = GM.summarise(d)
        self.assertEqual(s["frames"], 100)
        # 100 frames in 99*10ms + 20ms = 1.01s -> 99.01 fps
        self.assertAlmostEqual(s["fps_mean"], 99.01, places=1)
        self.assertAlmostEqual(s["frametime_ms_mean"], 10.1, places=1)
        self.assertAlmostEqual(s["frametime_ms_p99"], 20.0, places=1)
        self.assertAlmostEqual(s["fps_1pct_low"], 50.0, places=1)

    def test_warmup_frames_are_dropped(self):
        d = [1.0, 1.0, 0.01, 0.01]
        s = GM.summarise(d, warmup=2)
        self.assertEqual(s["frames"], 2)
        self.assertAlmostEqual(s["fps_mean"], 100.0, places=1)

    def test_warmup_larger_than_the_log_keeps_everything(self):
        s = GM.summarise([0.01, 0.01], warmup=99)
        self.assertEqual(s["frames"], 2)

    def test_empty_report_has_no_frames(self):
        self.assertEqual(GM.summarise([]), {"frames": 0})


class ApiTableTest(unittest.TestCase):
    def test_every_api_maps_to_a_flag_the_engine_documents(self):
        # -direct3d11 / -direct3d12 / -vulkan: the manual's names, not -dx11/-dx12
        self.assertEqual(GM.APIS["d3d11"], ["-direct3d11"])
        self.assertEqual(GM.APIS["d3d12"], ["-direct3d12"])
        self.assertEqual(GM.APIS["vk-win"], ["-vulkan"])
        self.assertEqual(GM.APIS["vk-native"], ["-vulkan"])


if __name__ == "__main__":
    unittest.main()
