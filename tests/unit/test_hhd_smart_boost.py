# SPDX-License-Identifier: MIT
"""patches/hhd/0004: hhd's Smart CPU boost caps each core type and nothing else.

Applies the patch to a copy of the installed hhd adjustor package and drives the new helpers
against a fake /sys/devices/system/cpu shaped like the OneXPlayer 3's Panther Lake: P-cores cpu0-1
(base 1.9, max 4.7 GHz), E-cores cpu2-9 (1.6 / 3.6), LP E-cores cpu10-13 (1.5 / 3.1). Skipped where
hhd is not installed or the patch no longer fits it (the installer reports that case itself).
"""

import importlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support

PATCH = support.REPO / "patches" / "hhd" / "0004-adjustor-smart-cpu-boost.patch"
CORES = [(range(0, 2), 1900000, 4700000), (range(2, 10), 1600000, 3600000),
         (range(10, 14), 1500000, 3100000)]


def installed_site():
    for path in sys.path + ["/usr/lib/python3.15/site-packages"]:
        if path and (Path(path) / "adjustor" / "fuse" / "gpu.py").is_file():
            return Path(path)
    return None


def drop_adjustor_modules():
    for mod in [m for m in sys.modules if m == "adjustor" or m.startswith("adjustor.")]:
        del sys.modules[mod]


class HhdSmartBoostPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site = installed_site()
        if site is None:
            raise unittest.SkipTest("hhd (adjustor) not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="oxp3-hhd-smart-"))
        shutil.copytree(site / "adjustor", cls.tmp / "adjustor",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # an earlier version of this patch may be applied (as the installer does, take it out first)
        kept = Path("/usr/local/share/oxp3-fixes/hhd-patches") / PATCH.name
        if kept.is_file() and kept.read_bytes() != PATCH.read_bytes():
            subprocess.run(["git", "-C", str(cls.tmp), "apply", "-R", "-p1", str(kept)],
                           capture_output=True, text=True)
        res = subprocess.run(["git", "-C", str(cls.tmp), "apply", "-p1", str(PATCH)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            rev = subprocess.run(["git", "-C", str(cls.tmp), "apply", "--check", "-R", "-p1",
                                  str(PATCH)], capture_output=True, text=True)
            if rev.returncode != 0:
                shutil.rmtree(cls.tmp)
                raise unittest.SkipTest("patch 0004 does not fit the installed hhd")
        sys.path.insert(0, str(cls.tmp))
        drop_adjustor_modules()
        cls.gpu = importlib.import_module("adjustor.fuse.gpu")

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.tmp))
        drop_adjustor_modules()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oxp3-cpu-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for cpus, base, top in CORES:
            for n in cpus:
                d = self.root / f"cpu{n}" / "cpufreq"
                d.mkdir(parents=True)
                (d / "base_frequency").write_text(f"{base}\n")
                (d / "cpuinfo_max_freq").write_text(f"{top}\n")
                (d / "cpuinfo_min_freq").write_text("400000\n")
                (d / "scaling_max_freq").write_text(f"{top}\n")
                (d / "scaling_min_freq").write_text("400000\n")
        (self.root / "cpufreq").mkdir()            # not a cpuN directory
        (self.root / "intel_pstate").mkdir()
        (self.root / "intel_pstate" / "no_turbo").write_text("0\n")
        self.gpu.CPU_PATH = str(self.root) + "/"
        self.gpu.INTEL_BOOST_PATH = str(self.root / "intel_pstate" / "no_turbo")

    def maxes(self):
        return {n: int((self.root / f"cpu{n}" / "cpufreq" / "scaling_max_freq").read_text())
                for cpus, _, _ in CORES for n in cpus}

    def test_core_types_fastest_first(self):
        (self.root / "cpu14").mkdir()              # offline: no cpufreq, skipped
        self.assertEqual(self.gpu.get_cpu_classes(),
                         [list(range(0, 2)), list(range(2, 10)), list(range(10, 14))])
        self.assertTrue(self.gpu.supports_smart_boost())

    def test_caps_per_core_type(self):
        self.gpu.set_smart_boost_caps()
        m = self.maxes()
        self.assertEqual({m[n] for n in range(0, 2)}, {3000000})
        self.assertEqual({m[n] for n in range(2, 10)}, {2000000})
        self.assertEqual({m[n] for n in range(10, 14)}, {1500000})

    def test_frequency_scaling_lifts_the_caps(self):
        # Enabled / Disabled / auto all call set_frequency_scaling, which must undo Smart
        self.gpu.set_smart_boost_caps()
        self.gpu.set_frequency_scaling(nonlinear=False)
        m = self.maxes()
        # hhd writes cpu0's maximum everywhere; the real kernel clamps it per core
        self.assertEqual(set(m.values()), {4700000})

    def test_cap_never_exceeds_what_a_core_supports(self):
        for n in range(10, 14):
            (self.root / f"cpu{n}" / "cpufreq" / "cpuinfo_max_freq").write_text("1200000\n")
        self.gpu.set_smart_boost_caps()
        self.assertEqual({self.maxes()[n] for n in range(10, 14)}, {1200000})

    def test_not_offered_without_three_core_types(self):
        for n in range(10, 14):
            (self.root / f"cpu{n}" / "cpufreq" / "base_frequency").write_text("1600000\n")
        self.assertFalse(self.gpu.supports_smart_boost())

    def test_settings_offer_smart_between_disabled_and_enabled(self):
        import yaml
        sets = yaml.safe_load((self.tmp / "adjustor/drivers/gpu/settings.yml").read_text())

        def find(node):
            if isinstance(node, dict):
                if "cpu_boost" in node and isinstance(node["cpu_boost"], dict):
                    return node["cpu_boost"]
                for v in node.values():
                    r = find(v)
                    if r:
                        return r
            return None

        boost = find(sets)
        self.assertEqual(list(boost["options"]), ["disabled", "smart", "enabled"])
        self.assertEqual(boost["default"], "enabled")   # nothing changes unless Smart is picked

    def test_driver_reads_the_boost_value_as_a_string(self):
        src = (self.tmp / "adjustor/drivers/gpu/__init__.py").read_text()
        self.assertIn('conf["tdp.amd_energy.mode.manual.cpu_boost"].to(str)', src)
        self.assertIn('set_cpu_boost(new_boost in ("enabled", "smart"))', src)
        # the caps come after set_frequency_scaling, which would otherwise reset them
        self.assertLess(src.index("set_frequency_scaling(\n                            nonlinear=self.supports_nonlinear"),
                        src.index("self.smart.start(curr)"))
        self.assertIn("self.smart.tick(curr)", src)

    # --- the controller: levels follow the load -------------------------------------------------

    def classes_tops(self):
        classes = self.gpu.get_cpu_classes()
        tops = {n: top for cpus, _, top in CORES for n in cpus}
        return classes, tops

    def busy(self, p=10.0, e=10.0, lp=10.0, **one):
        b = {n: p for n in range(0, 2)}
        b.update({n: e for n in range(2, 10)})
        b.update({n: lp for n in range(10, 14)})
        b.update({int(k[3:]): v for k, v in one.items()})
        return b

    def test_saturated_core_raises_the_level_at_once(self):
        classes, tops = self.classes_tops()
        nxt = self.gpu.smart_next_level
        self.assertEqual(nxt(0, self.busy(cpu0=95), classes, tops, 0), (1, 0))   # a P-core
        self.assertEqual(nxt(0, self.busy(cpu5=90), classes, tops, 0), (1, 0))   # an E-core
        self.assertEqual(nxt(1, self.busy(cpu0=90), classes, tops, 0), (2, 0))
        self.assertEqual(nxt(2, self.busy(cpu0=100), classes, tops, 0), (2, 0))  # top level

    def test_lp_cores_do_not_lift_the_caps(self):
        # Steam's shader pre-compilation on the LP E-cores is background work
        classes, tops = self.classes_tops()
        self.assertEqual(self.gpu.smart_next_level(0, self.busy(lp=100), classes, tops, 0), (0, 0))

    def test_level_drops_only_after_three_quiet_checks(self):
        classes, tops = self.classes_tops()
        nxt, quiet = self.gpu.smart_next_level, self.busy(p=30, e=30)
        level, streak = 2, 0
        for expected in ((2, 1), (2, 2), (1, 0)):
            level, streak = nxt(level, quiet, classes, tops, streak)
            self.assertEqual((level, streak), expected)

    def test_no_step_down_that_would_bounce_back(self):
        # 70 % on a P-core at 4.7 GHz is 4.7/3.8 x 70 = 87 % at level 1: it would saturate again
        classes, tops = self.classes_tops()
        nxt = self.gpu.smart_next_level
        self.assertEqual(nxt(2, self.busy(cpu0=70), classes, tops, 2), (2, 0))
        # the GPU-bound case seen in Trails (busiest thread 38 % of a core) stays at level 0
        self.assertEqual(nxt(0, self.busy(cpu3=38 * 4.7 / 3.0), classes, tops, 0), (0, 0))

    def test_saturated_thread_raises_even_when_no_core_is_busy(self):
        # Elden Ring, 2026-11-24: main thread 95 % of a core, hopping cpu0/1/4, every core < 70 %
        classes, tops = self.classes_tops()
        nxt = self.gpu.smart_next_level
        spread = self.busy(p=65, e=15)
        self.assertEqual(nxt(1, spread, classes, tops, 0), (1, 0))          # cores alone: stuck at 1
        self.assertEqual(nxt(1, spread, classes, tops, 0, hot_thread=95), (2, 0))

    def test_busy_thread_blocks_a_step_down(self):
        # 75 % at level 1 (3.8 GHz) is 95 % at level 0 (3.0 GHz): stay
        classes, tops = self.classes_tops()
        self.assertEqual(self.gpu.smart_next_level(1, self.busy(p=40, e=15), classes, tops, 2,
                                                   hot_thread=75), (1, 0))

    def test_frame_capped_game_thread_holds_its_clock(self):
        # Elden Ring, 2026-11-27: main thread 40-49 % of a core at level 2 with every core quiet.
        # At 3.8 GHz that is 50-61 %: below the core bar, but its heavy frames would be late.
        classes, tops = self.classes_tops()
        nxt, quiet = self.gpu.smart_next_level, self.busy(p=30, e=20)
        for thread in (40, 46, 49):
            level, streak = 2, 0
            for _ in range(5):
                level, streak = nxt(level, quiet, classes, tops, streak, hot_thread=thread)
            self.assertEqual(level, 2, f"thread {thread} %")
        # at level 1 a 40 % thread is 51 % at 3.0 GHz: hold level 1 too
        self.assertEqual(nxt(1, quiet, classes, tops, 2, hot_thread=40), (1, 0))

    def test_light_game_thread_still_steps_down(self):
        # a GPU-bound title whose main thread uses a quarter of a core gives the power back
        classes, tops = self.classes_tops()
        nxt, quiet = self.gpu.smart_next_level, self.busy(p=30, e=20)
        level, streak = 2, 0
        for _ in range(3):
            level, streak = nxt(level, quiet, classes, tops, streak, hot_thread=25)
        self.assertEqual(level, 1)
        for _ in range(3):
            level, streak = nxt(level, quiet, classes, tops, streak, hot_thread=25)
        self.assertEqual(level, 0)

    def test_busiest_thread_reads_only_hot_processes(self):
        proc = self.root / "proc"

        def stat(path, ticks, name="eldenring.exe"):
            path.mkdir(parents=True, exist_ok=True)
            # utime is field 14, stime 15; the name may contain spaces and parentheses
            (path / "stat").write_text(f"1 ({name}) S " + "0 " * 10 + f"{ticks} 0 " + "0 " * 30 + "\n")

        def tick(game_main, game_other, idle_proc):
            stat(proc / "100", game_main + game_other)
            stat(proc / "100" / "task" / "100", game_main)
            stat(proc / "100" / "task" / "101", game_other, name="GX Worker (1)")
            stat(proc / "200", idle_proc, name="steam")
            stat(proc / "200" / "task" / "200", idle_proc, name="steam")

        real_p, real_t = self.gpu.read_process_ticks, self.gpu.read_thread_ticks
        self.gpu.read_process_ticks = lambda p="/proc": real_p(str(proc))
        self.gpu.read_thread_ticks = lambda pid, p="/proc": real_t(pid, str(proc))
        self.addCleanup(setattr, self.gpu, "read_process_ticks", real_p)
        self.addCleanup(setattr, self.gpu, "read_thread_ticks", real_t)
        sb = self.gpu.SmartBoost()
        sb.hz = 100
        tick(0, 0, 0)
        sb.proc_prev = self.gpu.read_process_ticks()
        tick(190, 70, 1)                 # 2 s: main 95 %, worker 35 %, steam 0.5 %
        self.assertEqual(sb.busiest_thread(2.0), 0.0)     # first time hot: no thread history yet
        self.assertNotIn(200, {pid for pid, _ in sb.thread_prev})   # idle process not read
        tick(380, 140, 2)
        self.assertAlmostEqual(sb.busiest_thread(2.0), 95.0)

    def test_top_level_removes_every_cap(self):
        self.gpu.set_smart_boost_caps(0)
        self.gpu.set_smart_boost_caps(2)
        self.assertEqual(self.maxes(), {n: top for cpus, _, top in CORES for n in cpus})

    def test_controller_ticks_from_proc_stat(self):
        stat = self.root / "stat"
        ticks = {"t": 0}

        def write(busy_cpu0):
            ticks["t"] += 1000
            lines = []
            for n in range(14):
                busy = busy_cpu0 if n == 0 else 10
                lines.append(f"cpu{n} {ticks['t'] * busy // 100} 0 0 {ticks['t'] * (100 - busy) // 100} 0 0 0 0 0 0")
            stat.write_text("cpu  0 0 0 0 0 0 0 0 0 0\n" + "\n".join(lines) + "\n")

        real = self.gpu.read_cpu_times
        self.gpu.read_cpu_times = lambda path="/proc/stat": real(str(stat))
        self.addCleanup(setattr, self.gpu, "read_cpu_times", real)
        sb = self.gpu.SmartBoost()
        write(10)
        sb.start(0.0)
        self.assertEqual(self.maxes()[0], 3000000)
        write(100)                       # cpu0 pinned at its cap
        sb.tick(1.0)                     # too soon: no decision
        self.assertEqual(sb.level, 0)
        sb.tick(2.5)
        self.assertEqual(sb.level, 1)
        self.assertEqual(self.maxes()[0], 3800000)


if __name__ == "__main__":
    unittest.main()
