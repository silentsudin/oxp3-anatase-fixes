"""Unit tests for tools/oxp3-adaptivetdp.py.

The daemon is split into a pure policy, a pure hysteresis, samplers and the
AdaptiveTdp wrapper; everything below runs without root, powercap or a GPU.
The fake powerctl object exercises the decision/logging path of the daemon.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

import support

adapt = support.load("oxp3_adaptivetdp", "oxp3-adaptivetdp.py")
pc = support.load("oxp3_powerctl", "oxp3-powerctl.py")


def policy(**overrides):
    args = adapt.build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return adapt.Policy(args)


class PolicyTest(unittest.TestCase):
    def test_load_ladder_boundaries(self):
        p = policy()
        self.assertEqual(p.target(90, 60, True, 80)[0], adapt.LEVEL_OF["performance"])
        self.assertEqual(p.target(65, 60, True, 80)[0], adapt.LEVEL_OF["performance"])
        self.assertEqual(p.target(64.9, 60, True, 80)[0], adapt.LEVEL_OF["balanced"])
        self.assertEqual(p.target(30, 60, True, 80)[0], adapt.LEVEL_OF["balanced"])
        self.assertEqual(p.target(29.9, 60, True, 80)[0], adapt.LEVEL_OF["quiet"])
        self.assertEqual(p.target(None, 60, True, 80)[0], adapt.LEVEL_OF["quiet"])

    def test_reason_quotes_the_measured_value(self):
        _rung, why = policy().target(71, 60, True, 80)
        self.assertIn("load 71 %", why)
        self.assertIn(">= 65", why)

    def test_soft_temperature_clamps_to_balanced(self):
        p = policy()
        self.assertEqual(p.target(90, 84.9, True, 80)[0], adapt.LEVEL_OF["performance"])
        rung, why = p.target(90, 85, True, 80)
        self.assertEqual(rung, adapt.LEVEL_OF["balanced"])
        self.assertIn("soft-temp", why)

    def test_hard_temperature_forces_quiet_immediately(self):
        p = policy()
        rung, why = p.target(10, 92, True, 80)
        self.assertEqual(rung, adapt.LEVEL_OF["quiet"])
        self.assertIn("SAFETY", why)

    def test_battery_caps_at_balanced(self):
        p = policy()
        self.assertEqual(p.target(90, 60, False, 80)[0], adapt.LEVEL_OF["balanced"])
        rung, why = p.target(90, 60, False, 80)
        self.assertIn("on battery", why)

    def test_battery_performance_opt_in(self):
        p = policy(allow_battery_performance=True)
        self.assertEqual(p.target(90, 60, False, 80)[0], adapt.LEVEL_OF["performance"])

    def test_battery_floor_forces_quiet(self):
        p = policy()
        self.assertEqual(p.target(80, 60, False, 20)[0], adapt.LEVEL_OF["quiet"])
        self.assertEqual(p.target(80, 60, False, 21)[0], adapt.LEVEL_OF["balanced"])

    def test_ceiling(self):
        p = policy(ceiling="balanced")
        rung, why = p.target(95, 60, True, 80)
        self.assertEqual(rung, adapt.LEVEL_OF["balanced"])
        self.assertIn("ceiling", why)
        p = policy(ceiling="quiet")
        self.assertEqual(p.target(95, 60, True, 80)[0], adapt.LEVEL_OF["quiet"])

    def test_unknown_ac_state_is_treated_as_not_on_battery(self):
        # Pinned current behaviour: a missing AC node (None) skips both battery
        # clamps. See docs/UNIT-TESTS.md (deferred: safer default is battery).
        p = policy()
        self.assertEqual(p.target(90, 60, None, 5)[0], adapt.LEVEL_OF["performance"])


class HysteresisTest(unittest.TestCase):
    def test_first_sample_always_commits(self):
        h = adapt.Hysteresis(3, 0, now=0)
        self.assertEqual(h.update(adapt.LEVEL_OF["balanced"], 0), ("change", 1))
        self.assertEqual(h.current, 1)

    def test_same_want_holds_and_resets_pending(self):
        h = adapt.Hysteresis(2, 0, now=0)
        h.update(1, 0)
        h.update(2, 1)                 # pending
        self.assertEqual(h.update(1, 2), ("hold", 1))
        self.assertIsNone(h.pending)
        self.assertEqual(h.count, 0)

    def test_dwell_requires_n_consecutive_samples(self):
        h = adapt.Hysteresis(3, 0, now=0)
        h.update(adapt.LEVEL_OF["quiet"], 0)
        self.assertEqual(h.update(2, 1), ("hold", 0))
        self.assertEqual(h.update(2, 2), ("hold", 0))
        self.assertEqual(h.update(2, 3), ("change", 2))
        self.assertEqual(h.last_change, 3)

    def test_min_interval_blocks_a_dwelled_change(self):
        h = adapt.Hysteresis(1, 10, now=0)
        h.update(0, 0)
        self.assertEqual(h.update(2, 1), ("hold", 0))
        self.assertEqual(h.update(2, 9.9), ("hold", 0))
        self.assertEqual(h.update(2, 10), ("change", 2))

    def test_immediate_bypasses_dwell_and_interval(self):
        h = adapt.Hysteresis(5, 999, now=0)
        h.update(2, 0)
        self.assertEqual(h.update(0, 0.1, immediate=True), ("change", 0))

    def test_dwell_floor_is_one(self):
        h = adapt.Hysteresis(0, 0, now=0)
        h.update(0, 0)
        self.assertEqual(h.update(2, 1), ("change", 2))

    def test_a_revert_to_current_clears_the_candidate(self):
        h = adapt.Hysteresis(3, 0, now=0)
        h.update(1, 0)
        self.assertEqual(h.update(2, 1), ("hold", 1))
        self.assertEqual(h.count, 1)
        self.assertEqual(h.update(1, 2), ("hold", 1))  # back to the current rung
        self.assertIsNone(h.pending)
        self.assertEqual(h.count, 0)


class SamplerTest(unittest.TestCase):
    def test_cpu_sampler_computes_busy_percentage(self):
        lines = ["cpu  100 0 100 800 0 0 0 0 0 0\n",
                 "cpu  200 0 150 850 0 0 0 0 0 0\n"]
        with mock.patch.object(adapt, "read_first_line", side_effect=lines):
            sampler = adapt.CpuSampler()
            busy = sampler.sample()
        self.assertAlmostEqual(busy, 75.0, places=2)

    def test_cpu_sampler_counts_iowait_as_idle(self):
        lines = ["cpu  0 0 0 100 0 0 0 0 0 0\n",
                 "cpu  0 0 0 150 50 0 0 0 0 0\n"]
        with mock.patch.object(adapt, "read_first_line", side_effect=lines):
            sampler = adapt.CpuSampler()
            self.assertEqual(sampler.sample(), 0.0)

    def test_cpu_sampler_malformed_and_stalled(self):
        with mock.patch.object(adapt, "read_first_line",
                               return_value="not a cpu line"):
            self.assertIsNone(adapt.CpuSampler().sample())
        lines = ["cpu  1 2 3 4 5\n", "cpu  1 2 3 4 5\n"]
        with mock.patch.object(adapt, "read_first_line", side_effect=lines):
            sampler = adapt.CpuSampler()
            self.assertIsNone(sampler.sample())  # dt == 0

    def test_cpu_sampler_clamps(self):
        lines = ["cpu  10 0 10 80\n", "cpu  5 0 5 90\n"]  # counters went backwards
        with mock.patch.object(adapt, "read_first_line", side_effect=lines):
            sampler = adapt.CpuSampler()
            self.assertIsNone(sampler.sample())

    def test_gpu_busy_paths_and_value(self):
        real_isdir, real_exists = adapt.os.path.isdir, adapt.os.path.exists

        def isdir(path):
            return True if path == "/sys/class/drm" else real_isdir(path)

        def exists(path):
            return True if path.endswith("card0/device/gpu_busy_percent") else False

        with mock.patch.object(adapt.os.path, "isdir", side_effect=isdir), \
             mock.patch.object(adapt.os.path, "exists", side_effect=exists), \
             mock.patch.object(adapt.os, "listdir", return_value=["card1", "card0"]), \
             mock.patch.object(adapt, "read_int", return_value=42):
            paths = adapt.gpu_busy_paths()
            self.assertEqual(paths, ["/sys/class/drm/card0/device/gpu_busy_percent"])
            self.assertEqual(adapt.read_gpu_busy(), 42)

    def test_gpu_busy_absent(self):
        with mock.patch.object(adapt.os.path, "isdir", return_value=False):
            self.assertEqual(adapt.gpu_busy_paths(), [])
            self.assertIsNone(adapt.read_gpu_busy())

    def test_read_ac_online(self):
        values = {"type": "Mains", "online": "1"}

        def read(path):
            return values[path.rsplit("/", 1)[-1]]

        with mock.patch.object(adapt.os.path, "isdir", return_value=True), \
             mock.patch.object(adapt.os, "listdir", return_value=["BAT0", "ADP1"]), \
             mock.patch.object(adapt, "read_first_line", side_effect=read):
            self.assertTrue(adapt.read_ac_online())
        values["online"] = "0"
        with mock.patch.object(adapt.os.path, "isdir", return_value=True), \
             mock.patch.object(adapt.os, "listdir", return_value=["ADP1"]), \
             mock.patch.object(adapt, "read_first_line", side_effect=read):
            self.assertFalse(adapt.read_ac_online())

    def test_read_ac_online_absent(self):
        with mock.patch.object(adapt.os.path, "isdir", return_value=False):
            self.assertIsNone(adapt.read_ac_online())

    def test_read_battery_scaling(self):
        values = {"capacity": "77", "status": "Charging", "power_now": "12000000"}

        def read_int(path):
            return int(values[path.rsplit("/", 1)[-1]])

        def read_line(path):
            return values[path.rsplit("/", 1)[-1]]

        with mock.patch.object(adapt.os.path, "isdir", return_value=True), \
             mock.patch.object(adapt, "read_int", side_effect=read_int), \
             mock.patch.object(adapt, "read_first_line", side_effect=read_line):
            bat = adapt.read_battery()
        self.assertEqual(bat["capacity_pct"], 77)
        self.assertEqual(bat["status"], "Charging")
        self.assertAlmostEqual(bat["power_now_w"], 12.0)

    def test_read_battery_absent(self):
        with mock.patch.object(adapt.os.path, "isdir", return_value=False):
            self.assertEqual(adapt.read_battery(), {})

    def test_read_helpers(self):
        tree = support.TmpTree()
        self.addCleanup(tree.cleanup)
        good = tree.write("int", "42\n")
        bad = tree.write("bad", "not a number\n")
        self.assertEqual(adapt.read_first_line(str(good)), "42")
        self.assertIsNone(adapt.read_first_line(str(tree.path("none"))))
        self.assertEqual(adapt.read_first_line(str(tree.path("none")), "x"), "x")
        self.assertEqual(adapt.read_int(str(good)), 42)
        self.assertEqual(adapt.read_int(str(bad), 7), 7)
        self.assertEqual(adapt.read_int(str(tree.path("none")), 8), 8)


# --------------------------------------------------------------------------- #
# AdaptiveTdp decision/logging path with a fake powerctl
# --------------------------------------------------------------------------- #

class FakePowerctl:
    PROFILES = pc.PROFILES
    LEVELS = pc.LEVELS
    ControlError = pc.ControlError

    def __init__(self, initial="balanced"):
        self.initial = initial
        self.calls = []
        self.fail = None

    # -- used by AdaptiveTdp.__init__ --------------------------------------
    def require_domains(self, allow_partial=False):
        return ["fake-domain"], []

    def find_temp_sensor(self, explicit=None):
        return "/fake/temp"

    def collect_status(self):
        return {"domains": []}

    def current_profile(self, status):
        return {"name": self.initial} if self.initial else None

    def read_temp(self, path):
        return 60.0

    # -- write path --------------------------------------------------------
    def apply_limits(self, domains, values, *, dry_run=False, allow_partial=False):
        if self.fail:
            raise self.fail
        self.calls.append(("limits", values, dry_run))
        return {"errors": [], "written": [], "readback": []}

    def write_platform_profile(self, name, *, dry_run=False):
        self.calls.append(("platform", name, dry_run))
        return {"set": name, "dry_run": dry_run}


def make_args(tmpdir, **overrides):
    # main() turns --once into --steps 1; the test builds args directly.
    args = adapt.build_parser().parse_args(
        ["--dry-run", "--interval", "0", "--steps", "1"])
    args.log_file = str(tmpdir / "decisions.jsonl")
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class AdaptiveTdpTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def make(self, fake=None, **overrides):
        fake = fake or FakePowerctl()
        self.fake = fake
        args = make_args(self.tree.root, **overrides)
        daemon = adapt.AdaptiveTdp(args, fake)
        daemon.cpu.sample = lambda: None
        self.addCleanup(lambda: daemon.log_fh and daemon.log_fh.close())
        return daemon

    def records(self):
        path = self.tree.path("decisions.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_first_decision_applies_the_wanted_preset(self):
        daemon = self.make()
        daemon.sample = lambda: {"load_pct": 90.0, "load_cpu_pct": 90.0,
                                 "load_gpu_pct": None, "temp_c": 60.0,
                                 "on_ac": True, "battery": {"capacity_pct": 80}}
        self.assertEqual(daemon.step(), 0)
        change = [r for r in self.records() if r["event"] == "change"][0]
        self.assertEqual(change["from_profile"], "balanced")
        self.assertEqual(change["to_profile"], "performance")
        self.assertFalse(change["applied"])  # dry-run
        self.assertEqual(self.fake.calls[0][0], "limits")

    def test_noop_when_the_preset_is_already_applied(self):
        daemon = self.make()
        daemon.applied = "performance"
        daemon.sample = lambda: {"load_pct": 90.0, "load_cpu_pct": 90.0,
                                 "load_gpu_pct": None, "temp_c": 60.0,
                                 "on_ac": True, "battery": {"capacity_pct": 80}}
        self.assertEqual(daemon.step(), 0)
        change = [r for r in self.records() if r["event"] == "change"][0]
        self.assertTrue(change["noop"])
        self.assertEqual(self.fake.calls, [])

    def test_control_error_is_logged_and_exit_2(self):
        fake = FakePowerctl()
        fake.fail = pc.ControlError("denied", hint="sudo")
        daemon = self.make(fake)
        daemon.sample = lambda: {"load_pct": 90.0, "load_cpu_pct": 90.0,
                                 "load_gpu_pct": None, "temp_c": 60.0,
                                 "on_ac": True, "battery": {"capacity_pct": 80}}
        self.assertEqual(daemon.step(), 2)
        record = [r for r in self.records() if r["event"] == "error"][0]
        self.assertIn("denied", record["error"])
        self.assertEqual(record["hint"], "sudo")

    def test_platform_profile_is_written_on_change(self):
        daemon = self.make(platform_profile=True)
        daemon.sample = lambda: {"load_pct": 90.0, "load_cpu_pct": 90.0,
                                 "load_gpu_pct": None, "temp_c": 60.0,
                                 "on_ac": True, "battery": {"capacity_pct": 80}}
        daemon.step()
        self.assertIn(("platform", "performance", True), self.fake.calls)

    def test_sample_prefers_the_gpu_load(self):
        daemon = self.make()
        daemon.cpu.sample = lambda: 10.0
        with mock.patch.object(adapt, "read_gpu_busy", return_value=70), \
             mock.patch.object(adapt, "read_battery", return_value={}), \
             mock.patch.object(adapt, "read_ac_online", return_value=True), \
             mock.patch.object(daemon.pc, "read_temp", return_value=55.0):
            sample = daemon.sample()
        self.assertEqual(sample["load_pct"], 70.0)
        self.assertEqual(sample["load_cpu_pct"], 10.0)
        self.assertEqual(sample["temp_c"], 55.0)

    def test_run_steps_and_logs_start_stop(self):
        daemon = self.make()
        daemon.sample = lambda: {"load_pct": 10.0, "load_cpu_pct": 10.0,
                                 "load_gpu_pct": None, "temp_c": 50.0,
                                 "on_ac": True, "battery": {"capacity_pct": 80}}
        self.assertEqual(daemon.run(), 0)
        events = [r["event"] for r in self.records()]
        self.assertEqual(events[0], "start")
        # The one sample that decides the first change is logged as "change".
        self.assertIn("change", events)
        self.assertEqual(events[-1], "stop")

    def test_start_refuses_battery_performance_by_default(self):
        daemon = self.make()
        daemon.sample = lambda: {"load_pct": 95.0, "load_cpu_pct": 95.0,
                                 "load_gpu_pct": None, "temp_c": 50.0,
                                 "on_ac": False, "battery": {"capacity_pct": 70}}
        daemon.run()
        change = [r for r in self.records() if r["event"] == "change"][0]
        self.assertEqual(change["to_profile"], "balanced")

    def test_restore_returns_the_initial_preset(self):
        daemon = self.make(dry_run=False)
        daemon.applied = "performance"
        daemon.restore()
        self.assertEqual(self.fake.calls[-1], ("limits", {
            "pl1": 25.0, "pl2": 45.0, "pl4": 160.0}, False))
        self.assertEqual(self.records()[-1]["event"], "restore")

    def test_restore_is_skipped_for_dry_run_and_no_restore(self):
        daemon = self.make()
        daemon.applied = "performance"
        daemon.restore()
        self.assertEqual(self.fake.calls, [])
        daemon = self.make(no_restore=True, dry_run=False)
        daemon.applied = "performance"
        daemon.restore()
        self.assertEqual(self.fake.calls, [])

    def test_unwritable_log_file_falls_back_to_stderr(self):
        blocker = self.tree.write("blocker", "x")
        args = make_args(self.tree.root, log_file=str(blocker / "sub" / "log.jsonl"))
        err = io.StringIO()
        with redirect_stderr(err):
            daemon = adapt.AdaptiveTdp(args, FakePowerctl())
        self.assertIsNone(daemon.log_fh)
        self.assertIn("cannot open log", err.getvalue())
        daemon.log({"event": "probe"})  # must not raise

    def test_load_powerctl_refuses_a_missing_path(self):
        with self.assertRaises(SystemExit) as ctx:
            adapt.load_powerctl("/nonexistent/oxp3-powerctl.py")
        self.assertIn("cannot find", str(ctx.exception))

    def test_ladder_and_levels_are_consistent(self):
        self.assertEqual(adapt.LADDER, ("quiet", "balanced", "performance"))
        for name, idx in adapt.LEVEL_OF.items():
            self.assertEqual(adapt.LADDER[idx], name)


if __name__ == "__main__":
    unittest.main()
