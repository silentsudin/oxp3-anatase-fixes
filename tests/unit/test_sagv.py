"""Behavioural tests for tools/oxp3-sagv.sh.

The tool is deliberately read-only; these tests pin its documented exit codes
(0 ok, 3 unavailable/refused, 4 usage) and exercise the measurement path
against a synthetic /run/rapl-power directory. `sleep` is stubbed so the
5-second minimum sample window costs no wall clock.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import unittest

import support

SAGV = support.REPO / "tools" / "oxp3-sagv.sh"


class SagvTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def env(self, rapl_dir=None, extra_path=None):
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        path = []
        if extra_path:
            path.append(str(extra_path))
        path.append(env.get("PATH", "/usr/bin:/bin"))
        env["PATH"] = ":".join(path)
        if rapl_dir is not None:
            env["SAGV_RAPL_DIR"] = str(rapl_dir)
        env["TMPDIR"] = str(self.tree.root)
        return env

    def run_sagv(self, *args, rapl_dir=None, extra_path=None):
        return subprocess.run([str(SAGV), *args], capture_output=True, text=True,
                              timeout=60, env=self.env(rapl_dir, extra_path))

    def stub_sleep(self):
        bindir = self.tree.mkdir("bin")
        sleep = bindir / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        return bindir

    # -- usage and set -----------------------------------------------------
    def test_help_is_usage_exit_4(self):
        res = self.run_sagv("help")
        self.assertEqual(res.returncode, 4)
        self.assertIn("status", res.stdout)

    def test_unknown_command_is_usage_exit_4(self):
        self.assertEqual(self.run_sagv("frobnicate").returncode, 4)

    def test_set_without_argument_is_usage(self):
        self.assertEqual(self.run_sagv("set").returncode, 4)

    def test_set_off_dry_run_prints_the_command_and_writes_nothing(self):
        res = self.run_sagv("set", "off", "--dry-run")
        self.assertEqual(res.returncode, 0)
        self.assertIn("rpm-ostree kargs --append-if-missing=xe.enable_sagv=0",
                      res.stdout)
        self.assertIn("nothing was written", res.stdout)

    def test_set_on_without_dry_run_still_only_prints(self):
        res = self.run_sagv("set", "on")
        self.assertEqual(res.returncode, 0)
        self.assertIn("delete-if-present=xe.enable_sagv=0", res.stdout)

    def test_set_point_refuses_with_exit_3(self):
        res = self.run_sagv("set", "point5")
        self.assertEqual(res.returncode, 3)
        self.assertIn("NOT SETTABLE", res.stdout)
        self.assertIn("sagv_force_point", res.stdout)

    def test_set_point_dry_run_is_exit_0(self):
        res = self.run_sagv("set", "point=3", "--dry-run")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Would write", res.stdout)

    def test_set_bad_point_is_usage(self):
        self.assertEqual(self.run_sagv("set", "pointx").returncode, 4)

    # -- measure -----------------------------------------------------------
    def test_measure_needs_five_seconds(self):
        res = self.run_sagv("measure", "--seconds", "2")
        self.assertEqual(res.returncode, 3)
        self.assertIn("need at least 5 s", res.stderr)

    def test_measure_without_samples_is_exit_3(self):
        empty = self.tree.mkdir("rapl-empty")
        res = self.run_sagv("measure", "--seconds", "5", rapl_dir=empty)
        self.assertEqual(res.returncode, 3)
        self.assertIn("no RAPL samples", res.stderr)

    def test_measure_averages_fixture_samples(self):
        rapl = self.tree.mkdir("rapl")
        values = {"package-0": "20.0", "core": "5.5", "uncore": "1.0",
                  "dram": "1.5", "psys": "24.0"}
        for name, value in values.items():
            (rapl / name).write_text(value + "\n")
        res = self.run_sagv("measure", "--seconds", "5", "--interval", "5",
                            rapl_dir=rapl, extra_path=self.stub_sleep())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("package-0", res.stdout)
        self.assertRegex(res.stdout, r"package-0\s+20\.00\s+20\.00\s+20\.00\s+1")
        self.assertRegex(res.stdout, r"dram\s+1\.50")
        self.assertIn("sampling", res.stdout)

    def test_measure_interpolates_nothing_and_requires_integer_seconds(self):
        res = self.run_sagv("measure", "--seconds", "5.5")
        self.assertEqual(res.returncode, 4)

    # -- status / report ---------------------------------------------------
    def test_status_is_read_only_exit_0(self):
        res = self.run_sagv("status")
        self.assertEqual(res.returncode, 0)
        self.assertIn("SAGV / QCLK-GV status", res.stdout)
        self.assertIn("verdict:", res.stdout)

    def test_report_without_dram_or_psys_still_succeeds(self):
        empty = self.tree.mkdir("rapl-empty")
        res = self.run_sagv("report", rapl_dir=empty)
        self.assertEqual(res.returncode, 0)
        self.assertIn("lacks dram/psys", res.stdout)


if __name__ == "__main__":
    unittest.main()
