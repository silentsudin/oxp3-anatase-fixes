# SPDX-License-Identifier: MIT
"""patches/hhd/0003: hhd must apply OneXConsole's OneXPlayer 3 power model (with its 40 W unlock).

Applies the patch to a copy of the installed hhd adjustor package and drives
get_rapl/set_rapl against a fake powercap tree shaped like the OneXPlayer 3
(MSR + MMIO package zones). Skipped where hhd is not installed or the patch no
longer fits it (the installer reports that case itself).
"""

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support

PATCH = support.REPO / "patches" / "hhd" / "0003-adjustor-oxp3-vendor-power-limits.patch"


def installed_site():
    for path in sys.path + ["/usr/lib/python3.15/site-packages"]:
        if path and (Path(path) / "adjustor" / "core" / "rapl.py").is_file():
            return Path(path)
    return None


class HhdOxp3PowerPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site = installed_site()
        if site is None:
            raise unittest.SkipTest("hhd (adjustor) not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="oxp3-hhd-"))
        shutil.copytree(site / "adjustor", cls.tmp / "adjustor")
        res = subprocess.run(["git", "-C", str(cls.tmp), "apply", "-p1", str(PATCH)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            # already patched upstream copies reverse-apply; anything else does not fit
            rev = subprocess.run(["git", "-C", str(cls.tmp), "apply", "--check", "-R", "-p1",
                                  str(PATCH)], capture_output=True, text=True)
            if rev.returncode != 0:
                shutil.rmtree(cls.tmp)
                raise unittest.SkipTest("patch 0003 does not fit the installed hhd")
        sys.path.insert(0, str(cls.tmp))
        for mod in [m for m in sys.modules if m == "adjustor" or m.startswith("adjustor.")]:
            del sys.modules[mod]
        cls.rapl = importlib.import_module("adjustor.core.rapl")

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.tmp))
        for mod in [m for m in sys.modules if m == "adjustor" or m.startswith("adjustor.")]:
            del sys.modules[mod]
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oxp3-powercap-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        # startup values as found on the device: hhd's previous 40/42 W and our old 160 W PL4
        for zone in ("intel-rapl:0", "intel-rapl-mmio:0"):
            d = self.root / zone
            d.mkdir()
            (d / "name").write_text("package-0\n")
            (d / "enabled").write_text("1\n")
            for i, (kind, watts) in enumerate((("long_term", 40), ("short_term", 42),
                                               ("peak_power", 160))):
                (d / f"constraint_{i}_name").write_text(kind + "\n")
                (d / f"constraint_{i}_power_limit_uw").write_text(f"{watts * 1_000_000}\n")
            (d / "constraint_0_max_power_uw").write_text("25000000\n")
        self.rapl.POWERCAP = self.root

    def limits(self, zone):
        return tuple(int((self.root / zone / f"constraint_{i}_power_limit_uw").read_text())
                     // 1_000_000 for i in range(3))

    def test_oxp3_range_and_default(self):
        data = self.rapl.get_rapl("ONEXPLAYER 3")
        self.assertEqual((data.min_tdp, data.default_tdp, data.max_tdp), (3, 25, 40))

    def test_oxp3_limits_follow_onexconsole(self):
        data = self.rapl.get_rapl("ONEXPLAYER 3")
        for tdp, expect in ((40, (40, 41, 160)), (35, (35, 36, 160)), (25, (25, 26, 160)),
                            (10, (10, 11, 160))):
            self.assertTrue(self.rapl.set_rapl(data, tdp, True))
            for zone in ("intel-rapl:0", "intel-rapl-mmio:0"):
                self.assertEqual(self.limits(zone), expect, f"TDP {tdp} on {zone}")

    def test_oxp3_allows_the_vendor_unlock_but_not_above(self):
        data = self.rapl.get_rapl("ONEXPLAYER 3")
        self.assertTrue(self.rapl.set_rapl(data, 40, True))
        with self.assertRaises(ValueError):
            self.rapl.set_rapl(data, 41, True)

    def test_boost_off_is_unchanged(self):
        data = self.rapl.get_rapl("ONEXPLAYER 3")
        self.rapl.set_rapl(data, 25, False)
        self.assertEqual(self.limits("intel-rapl:0"), (25, 25, 25))

    def test_other_boards_keep_hhds_model(self):
        data = self.rapl.get_rapl("ONEXPLAYER X1 i")
        self.assertEqual((data.pl2_offset, data.pl4_fixed), (2, 0))
        self.rapl.set_rapl(data, 25, True)
        # PL2 = TDP + 2, PL4 scaled from the startup ceiling (32 W PL2 preset vs 160 W found)
        self.assertEqual(self.limits("intel-rapl:0")[:2], (25, 27))


if __name__ == "__main__":
    unittest.main()
