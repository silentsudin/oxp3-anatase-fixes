# SPDX-License-Identifier: MIT
"""Tests for tools/gpu-bench/d3d-api.py - the D3D API comparison harness.

The parser is the part that decides whether a measurement means anything, and MangoHud's CSV
format is not a stable interface: 0.8.4 writes a metadata line, then a `fps,...` header, then
rows that end with `elapsed`. These tests pin the format that was captured from a real run
(vkcube, MangoHud 0.8.4, 2026-11-23) and the window slicing built on it.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import support

D3D = support.load("d3d_api", "gpu-bench/d3d-api.py")

# Exactly the shape MangoHud 0.8.4 wrote: two metadata lines, the metric header, then rows.
REAL_CSV = """os,cpu,gpu,ram,kernel,driver,cpuscheduler
Anatase,Intel Arc G3 Extreme,Intel(R) Arc(tm) B390 (PTL),32377624,7.3.4-an02.fc45.x86_64,,powersave
fps,frametime,cpu_load,cpu_power,gpu_load,cpu_temp,gpu_temp,gpu_core_clock,gpu_mem_clock,gpu_vram_used,gpu_power,ram_used,swap_used,process_rss,cpu_mhz,elapsed
60.02,16.66,12.5,9.1,88.0,55.0,53.0,2300,1600,812,14.2,9000,10,700,2400,0.1
60.00,16.67,13.0,9.2,90.0,55.0,53.0,2300,1600,812,14.2,9000,10,700,2400,0.2
50.00,20.00,14.0,9.3,95.0,55.0,53.0,2300,1600,812,14.2,9000,10,700,2400,0.3
40.00,25.00,15.0,9.4,99.0,55.0,53.0,2300,1600,812,14.2,9000,10,700,2400,0.4
"""


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def write(self, text: str) -> Path:
        p = self.tree.path("mangohud.csv")
        p.write_text(text)
        return p

    def test_metadata_lines_are_skipped(self):
        s = D3D.parse_mangohud(self.write(REAL_CSV), 4.0)
        self.assertEqual(s["samples"], 4)
        self.assertNearly(s["fps_avg"], 52.51, 0.1)

    def test_window_takes_the_tail_of_the_log(self):
        # the window is the end of the run (0.2s at log_interval=100 -> the last two rows);
        # MangoHud's `elapsed` is in nanoseconds and must not drive the slicing
        s = D3D.parse_mangohud(self.write(REAL_CSV), 0.2)
        self.assertEqual(s["samples"], 2)
        self.assertNearly(s["fps_avg"], 45.0, 0.1)

    def test_loads_are_averaged_from_whatever_columns_exist(self):
        s = D3D.parse_mangohud(self.write(REAL_CSV), 4.0)
        self.assertNearly(s["cpu_load_avg"], 13.6, 0.1)
        self.assertNearly(s["gpu_load_avg"], 93.0, 0.1)
        self.assertNearly(s["gpu_clock_avg"], 2300.0, 0.1)

    def test_one_percent_low_comes_from_the_worst_frametime(self):
        s = D3D.parse_mangohud(self.write(REAL_CSV), 4.0)
        self.assertNearly(s["frametime_ms_1pct_low"], 25.0, 0.1)
        self.assertNearly(s["fps_1pct_low"], 40.0, 0.1)

    def test_elapsed_in_nanoseconds_does_not_widen_the_window(self):
        csv = REAL_CSV.replace(",0.1", ",101261773").replace(",0.2", ",201517669")
        csv = csv.replace(",0.3", ",301596072").replace(",0.4", ",401596072")
        s = D3D.parse_mangohud(self.write(csv), 0.2)
        self.assertEqual(s["samples"], 2)

    def test_empty_log_reports_zero_samples(self):
        self.assertEqual(D3D.parse_mangohud(self.write(""), 1.0)["samples"], 0)

    def assertNearly(self, got, want, delta):
        self.assertIsNotNone(got)
        self.assertLessEqual(abs(got - want), delta, f"{got} != {want} (+-{delta})")


class ConfigTest(unittest.TestCase):
    def test_mangohud_config_targets_the_result_folder(self):
        cfg = D3D.mangohud_config(Path("/tmp/res"))
        self.assertIn("output_folder=/tmp/res", cfg)
        # 0.8.4 ignores output_file entirely and needs log_duration to start logging
        self.assertNotIn("output_file=", cfg)
        self.assertIn("autostart_log=1", cfg)
        self.assertIn("log_duration=0", cfg)

    def test_no_metric_names_are_forced(self):
        # naming a metric the driver does not expose makes MangoHud discard the whole config
        cfg = D3D.mangohud_config(Path("/tmp/res"))
        for name in ("gpu_load", "cpu_load", "fps,", "frametime,"):
            self.assertNotIn(name, cfg)


class TitlesTest(unittest.TestCase):
    def test_every_title_has_an_appid_exe_and_apis(self):
        for key, t in D3D.TITLES.items():
            self.assertRegex(t["appid"], r"^\d+$", key)
            self.assertTrue(t["exe"], key)
            self.assertTrue(t["apis"], key)
            for api, flags in t["apis"].items():
                self.assertIn(api, ("dx11", "dx12", "vk"), key)
                self.assertIsInstance(flags, list)

    def test_dx11_and_dx12_are_measured_in_the_same_title_when_possible(self):
        # the comparison is only meaningful inside one title; Palworld is the installed one
        self.assertEqual(sorted(D3D.TITLES["palworld"]["apis"]), ["dx11", "dx12"])


if __name__ == "__main__":
    unittest.main()
