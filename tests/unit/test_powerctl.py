# SPDX-License-Identifier: MIT
"""Unit tests for tools/oxp3-powerctl.py.

Covers the pure logic (time-window parsing, fan curve, battery behaviour
classification, capability probing) and the write path against a synthetic
powercap/power_supply tree: write ordering, readback verification, and the
documented exit codes (2 = control missing/not permitted, 3 = readback
mismatch, 4 = status partial). No sysfs on the host is touched.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support

pc = support.load("oxp3_powerctl", "oxp3-powerctl.py")


def run_main(argv, **patches):
    """Call powerctl.main() with stdout/stderr captured and globals patched."""
    out, err = io.StringIO(), io.StringIO()
    stack = [mock.patch.object(pc, k, v) for k, v in patches.items()]
    for ctx in stack:
        ctx.start()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = pc.main(argv)
            except SystemExit as exc:  # argparse --version etc.
                rc = exc.code
    finally:
        for ctx in reversed(stack):
            ctx.stop()
    return rc, out.getvalue(), err.getvalue()


class PowercapCase(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.cap = str(self.tree.path("powercap"))
        self.tree.mkdir("powercap")
        self.powercap = mock.patch.object(pc, "POWERCAP", self.cap)
        self.powercap.start()
        self.addCleanup(self.powercap.stop)

    def zone(self, zone="intel-rapl:0", **kwargs):
        return support.make_powercap(self.tree, zone=zone, **kwargs)

    def limits(self, zone="intel-rapl:0"):
        return [int(self.tree.path(f"powercap/{zone}/constraint_{i}_power_limit_uw")
                    .read_text()) / 1e6 for i in range(3)]


class FormatTest(unittest.TestCase):
    def test_fmt_w(self):
        self.assertEqual(pc.fmt_w(25_000_000), "25.000 W")
        self.assertEqual(pc.fmt_w(125_000), "0.125 W")

    def test_fmt_tau_units(self):
        self.assertEqual(pc.fmt_tau(999), "999 us")
        self.assertEqual(pc.fmt_tau(1000), "1.000 ms")
        self.assertEqual(pc.fmt_tau(999_999), "999.999 ms")
        self.assertEqual(pc.fmt_tau(1_000_000), "1.000 s")


class ParseTauTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(pc.parse_tau("56ms"), 56_000)
        self.assertEqual(pc.parse_tau("5.6s"), 5_600_000)
        self.assertEqual(pc.parse_tau("28000us"), 28_000)
        self.assertEqual(pc.parse_tau("28000us"), 28_000)
        self.assertEqual(pc.parse_tau("56 µs"), 56)
        self.assertEqual(pc.parse_tau(" 56 MS "), 56_000)

    def test_bare_number_is_seconds(self):
        self.assertEqual(pc.parse_tau("28"), 28_000_000)
        self.assertEqual(pc.parse_tau("1.5"), 1_500_000)
        self.assertEqual(pc.parse_tau(2), 2_000_000)

    def test_bad_values_raise_control_error(self):
        for text in ("", "ms", "56xs", "56 ms extra", "1e6us", "nan"):
            with self.assertRaises(pc.ControlError, msg=text):
                pc.parse_tau(text)


class ChargeBehaviourParseTest(unittest.TestCase):
    def test_bracketed_selection(self):
        selected, modes = pc.parse_charge_behaviour(
            "[auto] inhibit-charge inhibit-charge-awake")
        self.assertEqual(selected, "auto")
        self.assertEqual(modes, ["auto", "inhibit-charge", "inhibit-charge-awake"])

    def test_single_bare_token_is_the_selection(self):
        self.assertEqual(pc.parse_charge_behaviour("inhibit-charge"),
                         ("inhibit-charge", ["inhibit-charge"]))

    def test_empty(self):
        self.assertEqual(pc.parse_charge_behaviour(""), (None, []))
        self.assertEqual(pc.parse_charge_behaviour(None), (None, []))

    def test_middle_bracket_and_partial_tokens(self):
        selected, modes = pc.parse_charge_behaviour("auto [inhibit-charge] x")
        self.assertEqual(selected, "inhibit-charge")
        self.assertEqual(modes, ["auto", "inhibit-charge", "x"])
        selected, modes = pc.parse_charge_behaviour("[auto]x inhibit-charge")
        self.assertEqual(selected, None)  # not a whole bracketed token
        self.assertEqual(modes, ["[auto]x", "inhibit-charge"])


class ProbeStateTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def test_absent(self):
        self.assertEqual(pc.probe_state(str(self.tree.path("nope"))), "absent")

    def test_read_only_mode(self):
        p = self.tree.write("ro", "1")
        p.chmod(0o444)
        self.assertEqual(pc.probe_state(str(p)), "read-only")

    def test_writable_file(self):
        p = self.tree.write("rw", "1")
        p.chmod(0o644)
        self.assertEqual(pc.probe_state(str(p)), "writable")

    def test_has_write_bits_but_no_access_is_root_only(self):
        p = self.tree.write("root", "1")
        p.chmod(0o644)
        with mock.patch.object(pc.os, "access", return_value=False):
            self.assertEqual(pc.probe_state(str(p)), "root-only")


class CurveTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)

    def test_curve_pwm_boundaries_and_interpolation(self):
        pts = list(pc.DEFAULT_CURVE)
        self.assertEqual(pc.curve_pwm(pts, 20), 60)       # below the first point
        self.assertEqual(pc.curve_pwm(pts, 95), 255)      # at the last point
        self.assertEqual(pc.curve_pwm(pts, 120), 255)     # above it
        self.assertEqual(pc.curve_pwm(pts, 40), 60)
        self.assertEqual(pc.curve_pwm(pts, 47.5), 80)     # midpoint 40..55
        self.assertEqual(pc.curve_pwm(pts, None), None)

    def test_curve_pwm_duplicate_temperature_keeps_the_last_rise(self):
        # At the exact duplicated temperature the first matching pair wins, so
        # the lower pwm is returned; above it the later duplicate defines the
        # slope. Pinned because the `t1 == t0` guard in curve_pwm() is
        # effectively unreachable for sorted input (noted in docs/UNIT-TESTS.md).
        self.assertEqual(pc.curve_pwm([(50, 10), (50, 20), (60, 30)], 50), 10)
        self.assertEqual(pc.curve_pwm([(50, 10), (50, 20), (60, 30)], 55), 25)

    def test_load_curve_accepts_comments_and_separators(self):
        path = self.tree.write("curve", """
            # temp pwm
            80 200
            40,60
            55:100
            70=160
        """)
        points, source = pc.load_curve(str(path))
        self.assertEqual(points, [(40.0, 60), (55.0, 100), (70.0, 160), (80.0, 200)])
        self.assertEqual(source, str(path))

    def test_load_curve_sorts(self):
        path = self.tree.write("curve", "70 160\n40 60\n")
        self.assertEqual(pc.load_curve(str(path))[0], [(40.0, 60), (70.0, 160)])

    def test_default_curve_when_the_default_file_is_absent(self):
        missing = str(self.tree.path("no-curve"))
        with mock.patch.object(pc, "CURVE_FILE", missing):
            points, source = pc.load_curve(missing)
        self.assertEqual(points, list(pc.DEFAULT_CURVE))
        self.assertEqual(source, "built-in default")

    def test_explicit_missing_file_raises(self):
        with self.assertRaises(pc.ControlError):
            pc.load_curve(str(self.tree.path("absent")))

    def test_bad_curve_lines_raise_control_error(self):
        for body in ("", "# only comments\n", "abc def\n", "40\n", "40 nope\n",
                     "40 300\n", "40 -1\n"):
            path = self.tree.write("curve", body)
            with self.assertRaises(pc.ControlError, msg=body):
                pc.load_curve(str(path))


class DomainTest(PowercapCase):
    def test_discovery_prefers_msr_and_skips_subzones(self):
        self.zone("intel-rapl:0")
        self.zone("intel-rapl-mmio:0")
        support.make_powercap(self.tree, zone="intel-rapl:0:0", name="core")
        domains, missing = pc.require_domains()
        self.assertEqual([d.kind for d in domains], ["msr", "mmio"])
        self.assertEqual(missing, [])

    def test_no_zone_raises(self):
        with self.assertRaises(pc.ControlError) as ctx:
            pc.require_domains()
        self.assertIn("no intel-rapl package-0 zone", str(ctx.exception))

    def test_missing_levels_and_allow_partial(self):
        self.zone(limits=(35.0, 37.0, 122.0))
        # Remove PL4: only long_term/short_term remain.
        self.tree.path("powercap/intel-rapl:0/constraint_2_name").unlink()
        with self.assertRaises(pc.ControlError) as ctx:
            pc.require_domains()
        self.assertIn("PL4", str(ctx.exception))
        domains, missing = pc.require_domains(allow_partial=True)
        self.assertEqual(missing, ["pl4"])
        self.assertEqual(len(domains), 1)

    def test_get_survives_malformed_sysfs_content(self):
        self.zone()
        self.tree.write("powercap/intel-rapl:0/constraint_0_power_limit_uw", "garbage")
        domain = pc.discover_domains()[0]
        # A malformed attribute is "unreadable", not a traceback.
        self.assertIsNone(domain.get("pl1"))

    def test_enabled_and_window(self):
        self.zone()
        domain = pc.discover_domains()[0]
        self.assertTrue(domain.enabled())
        self.assertEqual(domain.get_window("pl1"), 27_980_000)
        self.assertEqual(domain.label, "MSR")


class ApplyLimitsTest(PowercapCase):
    RANK = {"pl1": 0, "pl2": 1, "pl4": 2}

    def assert_dependency_safe(self, rep):
        """Raises precede every lowering, ceiling-first; lowers floor-first.

        Per zone (the unit the kernel/verify check judge): no "lower" write may
        appear before a "raise", the raising subsequence must descend by level
        (PL4 -> PL2 -> PL1) and the lowering one must ascend (PL1 -> PL2 -> PL4).
        """
        per_domain = {}
        for w in rep["written"]:
            per_domain.setdefault(w["domain"], []).append(w)
        for path, seq in per_domain.items():
            kinds = [w["direction"] for w in seq]
            raises = [i for i, kind in enumerate(kinds) if kind == "raise"]
            lowers = [i for i, kind in enumerate(kinds) if kind == "lower"]
            if raises and lowers:
                self.assertLess(max(raises), min(lowers),
                                f"{path}: a lowering write precedes a raise: {kinds}")
            ranks = [self.RANK[w["level"]] for w in seq if w["direction"] == "raise"]
            self.assertEqual(ranks, sorted(ranks, reverse=True),
                             f"{path}: raising writes are not ceiling-first: {kinds}")
            ranks = [self.RANK[w["level"]] for w in seq if w["direction"] == "lower"]
            self.assertEqual(ranks, sorted(ranks),
                             f"{path}: lowering writes are not floor-first: {kinds}")

    def test_all_raise_order_is_pl4_pl2_pl1(self):
        self.zone(limits=(10.0, 25.0, 90.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 35.0, "pl2": 80.0, "pl4": 239.0},
                              dry_run=True)
        self.assertEqual([w["level"] for w in rep["written"]], ["pl4", "pl2", "pl1"])
        self.assertEqual([w["direction"] for w in rep["written"]], ["raise"] * 3)
        self.assertEqual([w["value_uw"] for w in rep["written"]],
                         [239_000_000, 80_000_000, 35_000_000])
        self.assertEqual([w["before_uw"] for w in rep["written"]],
                         [90_000_000, 25_000_000, 10_000_000])
        self.assert_dependency_safe(rep)

    def test_all_lower_order_is_pl1_pl2_pl4(self):
        self.zone(limits=(35.0, 80.0, 239.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 10.0, "pl2": 25.0, "pl4": 90.0},
                              dry_run=True)
        self.assertEqual([w["level"] for w in rep["written"]], ["pl1", "pl2", "pl4"])
        self.assertEqual([w["direction"] for w in rep["written"]], ["lower"] * 3)
        self.assertEqual([w["value_uw"] for w in rep["written"]],
                         [10_000_000, 25_000_000, 90_000_000])
        self.assert_dependency_safe(rep)

    def test_mixed_pl1_below_pl2_still_raises_first(self):
        # New envelope PL1 25 W < PL2 45 W, and PL1 is the *lowering* level
        # while PL2/PL4 rise.  Sorting by level (or lowest-first) would emit
        # PL1 first, i.e. a lowering write before both raises.
        self.zone(limits=(35.0, 37.0, 61.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 25.0, "pl2": 45.0, "pl4": 160.0},
                              dry_run=True)
        self.assertEqual([w["level"] for w in rep["written"]], ["pl4", "pl2", "pl1"])
        self.assertEqual([w["direction"] for w in rep["written"]],
                         ["raise", "raise", "lower"])
        self.assertEqual([w["value_uw"] for w in rep["written"]],
                         [160_000_000, 45_000_000, 25_000_000])
        self.assert_dependency_safe(rep)

    def test_mixed_change_raises_first_then_lowers(self):
        self.zone(limits=(10.0, 80.0, 239.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 25.0, "pl2": 45.0, "pl4": 160.0},
                              dry_run=True)
        self.assertEqual([w["level"] for w in rep["written"]], ["pl1", "pl2", "pl4"])
        self.assertEqual([w["direction"] for w in rep["written"]],
                         ["raise", "lower", "lower"])
        self.assert_dependency_safe(rep)

    def test_single_zone_single_limit(self):
        self.zone(limits=(35.0, 37.0, 61.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl2": 45.0}, dry_run=True)
        self.assertEqual([w["level"] for w in rep["written"]], ["pl2"])
        self.assertEqual(rep["written"][0]["direction"], "raise")
        self.assertEqual(rep["written"][0]["before_uw"], 37_000_000)
        rep = pc.apply_limits(domains, {"pl2": 30.0}, dry_run=True)
        self.assertEqual(rep["written"][0]["direction"], "lower")
        self.assertEqual(rep["written"][0]["value_uw"], 30_000_000)

    def test_split_direction_across_zones_is_ordered_per_zone(self):
        # This host's MMIO PL2 is 52 W while the MSR mirror reads 37 W, so the
        # same level raises on one zone and lowers on the other.  Classifying
        # the level once for all domains let the MMIO lowering precede the MMIO
        # floor lowering; each (domain, level) must be classified on its own
        # read value.
        self.zone("intel-rapl:0", limits=(35.0, 37.0, 61.0))
        self.zone("intel-rapl-mmio:0", limits=(35.0, 52.0, 61.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 25.0, "pl2": 45.0, "pl4": 160.0},
                              dry_run=True)
        per_zone = {}
        for w in rep["written"]:
            per_zone.setdefault(os.path.basename(w["domain"]), []).append(w["level"])
        self.assertEqual(per_zone["intel-rapl:0"], ["pl4", "pl2", "pl1"])
        self.assertEqual(per_zone["intel-rapl-mmio:0"], ["pl4", "pl1", "pl2"])
        self.assertEqual(len(rep["written"]), 6)
        self.assert_dependency_safe(rep)

    def test_missing_limit_is_skipped_and_keeps_dependency_order(self):
        self.zone(limits=(35.0, 37.0, 61.0))
        # A kernel that exposes only long_term/short_term: no PL4 ceiling.
        self.tree.path("powercap/intel-rapl:0/constraint_2_name").unlink()
        domains, missing = pc.require_domains(allow_partial=True)
        self.assertEqual(missing, ["pl4"])
        rep = pc.apply_limits(domains, {"pl1": 25.0, "pl2": 45.0, "pl4": 160.0},
                              dry_run=True)
        self.assertEqual([w["level"] for w in rep["written"]], ["pl2", "pl1"])
        self.assertEqual([w["value_uw"] for w in rep["written"]],
                         [45_000_000, 25_000_000])
        self.assertEqual([s["level"] for s in rep["skipped"]], ["pl4"])
        self.assert_dependency_safe(rep)
        # Asking only for the absent level writes nothing instead of raising.
        rep = pc.apply_limits(domains, {"pl4": 160.0}, dry_run=True)
        self.assertEqual(rep["written"], [])
        self.assertEqual([s["level"] for s in rep["skipped"]], ["pl4"])

    def test_dry_run_reports_values_without_writing(self):
        self.zone(limits=(10.0, 25.0, 90.0))
        before = self.limits()
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 35.0}, dry_run=True)
        self.assertEqual(rep["written"][0]["value_uw"], 35_000_000)
        self.assertEqual(rep["readback"], [])
        self.assertEqual(self.limits(), before)

    def test_successful_write_reads_back(self):
        self.zone(limits=(10.0, 25.0, 90.0))
        domains, _ = pc.require_domains()
        rep = pc.apply_limits(domains, {"pl1": 35.0, "pl2": 80.0, "pl4": 239.0})
        self.assertEqual(rep["errors"], [])
        self.assertEqual(len(rep["readback"]), 3)
        self.assertTrue(all(rb["ok"] for rb in rep["readback"]))
        self.assertEqual(self.limits(), [35.0, 80.0, 239.0])

    def test_readback_tolerance_accepts_quantisation(self):
        self.zone()
        domains, _ = pc.require_domains()
        real = pc.Domain.set
        with mock.patch.object(pc.Domain, "set",
                               lambda self, level, uw: real(self, level, uw + 200_000)):
            rep = pc.apply_limits(domains, {"pl1": 10.0})
        self.assertEqual(rep["errors"], [])
        self.assertTrue(rep["readback"][0]["ok"])

        domains, _ = pc.require_domains()
        with mock.patch.object(pc.Domain, "set",
                               lambda self, level, uw: real(self, level, uw + 500_000)):
            rep = pc.apply_limits(domains, {"pl1": 10.0})
        self.assertTrue(rep["readback"][0]["ok"] is False)
        self.assertIn("readback mismatch", rep["errors"][0]["error"])

    def test_write_failure_is_not_reported_as_a_readback_mismatch(self):
        self.zone()
        domains, _ = pc.require_domains()

        def denied(path, value):
            # Simulated EACCES: chmod(0444) would not deny root, and the suite
            # must give the same verdict in a root container.
            raise pc.ControlError(f"cannot write {value!r} to {path}: Permission denied",
                                  path=path, errno=13)

        with mock.patch.object(pc, "write_text", denied):
            rep = pc.apply_limits(domains, {"pl1": 15.0})
        self.assertEqual(rep["written"], [])
        self.assertEqual(len(rep["errors"]), 1)
        self.assertIn("cannot write", rep["errors"][0]["error"])
        self.assertEqual(rep["readback"], [])


class ExitCodeTest(PowercapCase):
    """The documented exit codes, exercised through main()."""

    def test_readback_mismatch_is_exit_3(self):
        self.zone()
        real = pc.Domain.set
        with mock.patch.object(pc, "check_root", lambda *a, **k: None), \
             mock.patch.object(pc.Domain, "set",
                              lambda self, level, uw: real(self, level, uw // 2)):
            rc, out, _ = run_main(["setpl1", "15"])
        self.assertEqual(rc, 3, out)

    def test_write_failure_is_exit_2_not_3(self):
        self.zone()

        def denied(path, value):
            raise pc.ControlError(f"cannot write {value!r} to {path}: Permission denied",
                                  path=path, errno=13)

        with mock.patch.object(pc, "check_root", lambda *a, **k: None), \
             mock.patch.object(pc, "write_text", denied):
            rc, out, err = run_main(["setpl1", "15"])
        self.assertEqual(rc, 2, out + err)
        self.assertNotIn("readback", out + err)

    def test_zero_watts_is_exit_2(self):
        with mock.patch.object(pc, "check_root", lambda *a, **k: None):
            rc, _, err = run_main(["setpl1", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("> 0 W", err)

    def test_profile_dry_run_order_and_exit_0(self):
        self.zone(limits=(10.0, 25.0, 90.0))
        rc, out, _ = run_main(["profile", "performance", "--dry-run", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["profile"], "performance")
        self.assertEqual(data["values"], {"pl1": 35.0, "pl2": 36.0, "pl4": 160.0})
        written = [w["level"] for w in data["domain_reports"][0]["written"]]
        self.assertEqual(written, ["pl4", "pl2", "pl1"])

    def test_profile_unknown_name_is_exit_2(self):
        rc, _, err = run_main(["profile", "turbo"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown profile", err)

    def test_fan_argument_validation_happens_before_hardware_probe(self):
        rc, _, err = run_main(["fan", "speed"])
        self.assertEqual(rc, 2)
        self.assertIn("needs a pwm value", err)
        rc, _, err = run_main(["fan", "speed", "300"])
        self.assertEqual(rc, 2)
        self.assertIn("0-255", err)

    def test_status_partial_is_exit_4(self):
        status = {"version": pc.VERSION, "uid": 0, "root": True, "domains": [],
                 "fan": None, "thermal": {}, "warnings": [], "errors": ["boom"]}
        with mock.patch.object(pc, "collect_status", return_value=status):
            rc, out, _ = run_main(["status", "--json"])
        self.assertEqual(rc, 4)
        self.assertEqual(json.loads(out)["errors"], ["boom"])


class FanTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.hw = str(self.tree.path("hwmon"))
        self.tree.mkdir("hwmon")
        self.patch = mock.patch.object(pc, "HWMON", self.hw)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_find_oxp_ec_and_status(self):
        support.make_fan(self.tree, pwm=124, pwm_enable=2, rpm=2400)
        fan = pc.find_fan()
        self.assertIsNotNone(fan)
        self.assertEqual(fan.name, "oxp_ec")
        self.assertEqual(fan.ec_duty(124), 89)  # 124 * 184 / 255
        self.assertEqual(fan.ec_duty(), 89)
        self.assertEqual(fan.mode(), ("auto", "2"))
        self.assertEqual(pc.fan_status(fan)["rpm"], 2400)

    def test_find_fan_fallback_requires_pwm_and_rpm(self):
        self.tree.write("hwmon/hwmon0/name", "other")
        self.tree.write("hwmon/hwmon1/name", "other2")
        self.tree.write("hwmon/hwmon1/pwm1", 10)
        self.tree.write("hwmon/hwmon1/fan1_input", 1000)
        self.assertEqual(pc.find_fan().path, str(self.tree.path("hwmon/hwmon1")))

    def test_find_fan_none(self):
        self.assertIsNone(pc.find_fan())

    def test_set_duty_rejects_out_of_range(self):
        support.make_fan(self.tree)
        fan = pc.find_fan()
        with self.assertRaises(pc.ControlError):
            fan.set_duty(256)
        with self.assertRaises(pc.ControlError):
            fan.set_duty(-1)

    def test_apply_fan_sets_manual_before_pwm_and_verifies(self):
        support.make_fan(self.tree, pwm=10, pwm_enable=2)
        fan = pc.find_fan()
        rep = pc.apply_fan(fan, mode="manual", duty=180)
        self.assertEqual(rep["errors"], [])
        # apply_fan() only logs the pwm action for the caller-chosen mode.
        self.assertEqual([a["value"] for a in rep["actions"]], [180])
        self.assertEqual(rep["readback"]["pwm"], 180)

    def test_apply_fan_readback_mismatch_is_an_error(self):
        support.make_fan(self.tree, pwm=10, pwm_enable=2)
        fan = pc.find_fan()
        with mock.patch.object(pc.Fan, "set_duty", lambda self, pwm: None):
            rep = pc.apply_fan(fan, mode="manual", duty=180)
        self.assertEqual(rep["readback"]["pwm"], 10)
        self.assertIn("readback mismatch", rep["errors"][0]["error"])

    def test_apply_fan_dry_run_writes_nothing(self):
        support.make_fan(self.tree)
        fan = pc.find_fan()
        rep = pc.apply_fan(fan, mode="manual", duty=200, dry_run=True)
        self.assertEqual(rep["readback"], {})
        self.assertEqual(pc.find_fan().duty(), 124)

    def test_curve_dry_run_one_step(self):
        support.make_fan(self.tree)
        temp = self.tree.write("temp1_input", 61000)
        fan = pc.find_fan()
        buf = io.StringIO()
        with redirect_stdout(buf):
            summary = pc.run_curve(fan, list(pc.DEFAULT_CURVE), interval=0,
                                   steps=1, sensor=str(temp), dry_run=True,
                                   restore_auto=True, quiet=False)
        self.assertEqual(summary["steps"], 1)
        self.assertIsNotNone(summary["last_pwm"])
        self.assertIn("degC -> pwm", buf.getvalue())
        self.assertEqual(pc.find_fan().duty(), 124)  # not written

    def test_read_temp_heuristic(self):
        self.assertIsNone(pc.read_temp(None))
        self.assertEqual(pc.read_temp(str(self.tree.write("t1", 61500))), 61.5)
        self.assertEqual(pc.read_temp(str(self.tree.write("t2", 61))), 61.0)
        self.assertIsNone(pc.read_temp(str(self.tree.write("t3", "hot"))))


class TemperatureSensorTest(unittest.TestCase):
    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.patch = mock.patch.object(pc, "HWMON", str(self.tree.path("hwmon")))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_coretemp_package_id_zero(self):
        self.tree.write("hwmon/hwmon2/name", "coretemp")
        self.tree.write("hwmon/hwmon2/temp1_label", "Package id 0")
        self.tree.write("hwmon/hwmon2/temp1_input", 55000)
        self.tree.write("hwmon/hwmon2/temp2_label", "Core 0")
        self.assertEqual(pc.find_temp_sensor(),
                         str(self.tree.path("hwmon/hwmon2/temp1_input")))

    def test_explicit_sensor_is_used_as_is(self):
        self.assertEqual(pc.find_temp_sensor("/tmp/x"), "/tmp/x")

    def test_no_sensor(self):
        real_glob = pc.glob.glob
        with mock.patch.object(pc.glob, "glob",
                               side_effect=lambda pattern: real_glob(pattern)
                               if "hwmon" in pattern else []):
            self.assertIsNone(pc.find_temp_sensor())


class CurrentProfileTest(unittest.TestCase):
    @staticmethod
    def status(limits, kinds=("msr", "mmio")):
        domains = []
        for kind in kinds:
            domains.append({
                "path": f"/sys/class/powercap/intel-rapl{' -mmio' if kind == 'mmio' else ''}:0",
                "kind": kind, "zone": "package-0", "enabled": True,
                "constraints": {level: {"name": level,
                                        "limit_uw": int(round(uw * 1e6)),
                                        "time_window_us": None, "writable": False}
                                for (level, _), uw in zip(pc.LEVELS, limits)},
            })
        return {"domains": domains}

    def test_exact_and_near_match(self):
        self.assertEqual(pc.current_profile(self.status((35.0, 36.0, 160.0)))["name"],
                         "performance")
        self.assertEqual(pc.current_profile(self.status((15.1, 16.1, 160.1)))["name"],
                         "quiet")

    def test_out_of_tolerance_is_custom(self):
        self.assertEqual(pc.current_profile(self.status((35.5, 36.0, 160.0)))["name"],
                         "custom")

    def test_unreadable_limit_is_custom(self):
        st = self.status((35.0, 36.0, 160.0))
        st["domains"][0]["constraints"]["pl2"]["limit_uw"] = None
        self.assertEqual(pc.current_profile(st)["name"], "custom")

    def test_no_domains_returns_none(self):
        self.assertIsNone(pc.current_profile({"domains": []}))


class BatteryFixture(PowercapCase):
    def setUp(self):
        super().setUp()
        self.tree.write("power_supply/BAT0/type", "Battery")
        support.make_battery(self.tree, capacity=80,
                             behaviour="[auto] inhibit-charge inhibit-charge-awake",
                             end_threshold=80)
        support.make_mains(self.tree, online=1)
        self.bat = mock.patch.object(pc, "BATTERY_GLOB",
                                     str(self.tree.path("power_supply/BAT*")))
        self.bat.start()
        self.addCleanup(self.bat.stop)


class BatteryStatusTest(BatteryFixture):
    def test_classification_and_values(self):
        info = pc.battery_status()
        self.assertEqual(info["name"], "BAT0")
        self.assertTrue(info["present"])
        self.assertEqual(info["capacity_pct"], 80)
        self.assertEqual(info["health_pct"], 92.6)  # 50/54 Wh
        self.assertEqual(info["energy_full_wh"], 50.0)
        self.assertEqual(info["power_now_w"], 7.5)
        self.assertEqual(info["charge_behaviour"], "auto")
        self.assertTrue(info["bypass"] is False)
        self.assertEqual(info["capabilities"]["charge_control_end_threshold"],
                         "writable")
        self.assertIn("cycle_count", [w for w in info["warnings"]][0])

    def test_bypass_flag_and_vendor_equivalence(self):
        support.make_battery(self.tree, behaviour="[inhibit-charge] auto")
        info = pc.battery_status()
        self.assertTrue(info["bypass"])
        self.assertIn("vendor mode 2", info["bypass_vendor_equivalent"])
        self.assertIn("0xA4=0x03", info["bypass_vendor_equivalent"])

    def test_absent_charge_behaviour_warns(self):
        self.tree.path("power_supply/BAT0/charge_behaviour").unlink()
        info = pc.battery_status()
        self.assertIn("absent", " ".join(info["warnings"]).lower())

    def test_no_battery(self):
        with mock.patch.object(pc, "BATTERY_GLOB", str(self.tree.path("none/BAT*"))):
            info = pc.battery_status()
        self.assertFalse(info["present"])
        self.assertIsNone(info["path"])
        self.assertIn("no /sys/class/power_supply/BAT*", info["warnings"][0])


class ChargeLimitTest(BatteryFixture):
    def test_success_writes_and_reads_back(self):
        rep = pc.set_charge_threshold(70)
        self.assertEqual(rep["applied"], 70)
        self.assertEqual(pc.read_int(str(self.tree.path(
            "power_supply/BAT0/charge_control_end_threshold"))), 70)

    def test_dry_run_does_not_write(self):
        rep = pc.set_charge_threshold(60, dry_run=True)
        self.assertEqual(rep["applied"], 80)  # current value
        self.assertTrue(rep["dry_run"])

    def test_none_means_no_limit(self):
        rep = pc.set_charge_threshold(None)
        self.assertEqual(rep["requested"], 100)
        self.assertEqual(rep["applied"], 100)

    def test_out_of_range(self):
        with self.assertRaises(pc.ControlError):
            pc.set_charge_threshold(150)
        with self.assertRaises(pc.ControlError):
            pc.set_charge_threshold(-1)

    def test_warns_outside_usual_range(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rep = pc.set_charge_threshold(40)
        self.assertEqual(rep["applied"], 40)
        self.assertIn("outside the usual 50-100", err.getvalue())

    def test_absent_attribute(self):
        self.tree.path("power_supply/BAT0/charge_control_end_threshold").unlink()
        with self.assertRaises(pc.ControlError) as ctx:
            pc.set_charge_threshold(70)
        self.assertIn("does not exist", str(ctx.exception))

    def test_read_only_attribute(self):
        p = self.tree.path("power_supply/BAT0/charge_control_end_threshold")
        p.chmod(0o444)
        with self.assertRaises(pc.ControlError) as ctx:
            pc.set_charge_threshold(70)
        self.assertIn("read-only", str(ctx.exception))

    def test_readback_mismatch(self):
        with mock.patch.object(pc, "read_int", return_value=55):
            with self.assertRaises(pc.ControlError) as ctx:
                pc.set_charge_threshold(70)
        self.assertIn("readback mismatch", str(ctx.exception))

    def test_battery_status_cli_exit_codes(self):
        ns = argparse.Namespace(battery_action="status", json=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(pc.cmd_battery(ns), 0)
        self.assertIn("battery BAT0", buf.getvalue())
        with mock.patch.object(pc, "BATTERY_GLOB", str(self.tree.path("x/BAT*"))), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(pc.cmd_battery(ns), 2)


class ChargeBypassTest(BatteryFixture):
    def patch_target(self, path):
        return mock.patch.object(pc, "find_battery",
                                 return_value=str(self.tree.path("power_supply/BAT0")))

    def test_on_writes_inhibit_and_reads_back(self):
        rep = pc.set_charge_bypass(True)
        self.assertEqual(rep["requested"], "inhibit-charge")
        self.assertEqual(rep["applied"], "inhibit-charge")
        self.assertEqual(rep["previous"], "auto")

    def test_awake_variant(self):
        rep = pc.set_charge_bypass(True, mode="inhibit-charge-awake")
        self.assertEqual(rep["applied"], "inhibit-charge-awake")

    def test_off_writes_auto(self):
        support.make_battery(self.tree, behaviour="[inhibit-charge] auto")
        rep = pc.set_charge_bypass(False)
        self.assertEqual(rep["applied"], "auto")

    def test_dry_run_returns_current(self):
        rep = pc.set_charge_bypass(True, dry_run=True)
        self.assertEqual(rep["applied"], "auto")
        self.assertEqual(rep["requested"], "inhibit-charge")

    def test_bad_mode_rejected_before_write(self):
        with self.assertRaises(pc.ControlError):
            pc.set_charge_bypass(True, mode="not-a-mode")

    def test_mode_not_advertised(self):
        support.make_battery(self.tree, behaviour="[auto] inhibit-charge")
        with self.assertRaises(pc.ControlError) as ctx:
            pc.set_charge_bypass(True, mode="inhibit-charge-awake")
        self.assertIn("does not offer", str(ctx.exception))

    def test_single_token_file_allows_any_write(self):
        # A driver that renders only the current value must not be treated as
        # "this is the only supported mode".
        self.tree.write("power_supply/BAT0/charge_behaviour", "auto")
        rep = pc.set_charge_bypass(True, mode="inhibit-charge-awake")
        self.assertEqual(rep["applied"], "inhibit-charge-awake")

    def test_einval_is_translated(self):
        real = pc.write_text

        def fake(path, value):
            if path.endswith("charge_behaviour"):
                raise pc.ControlError("EINVAL", path=path, errno=22)
            return real(path, value)

        with mock.patch.object(pc, "write_text", fake):
            with self.assertRaises(pc.ControlError) as ctx:
                pc.set_charge_bypass(True)
        self.assertIn("does not support", str(ctx.exception))
        self.assertIn("available", ctx.exception.hint)

    def test_readback_mismatch(self):
        real = pc.write_text

        def fake(path, value):
            return real(path, "auto")

        with mock.patch.object(pc, "write_text", fake):
            with self.assertRaises(pc.ControlError) as ctx:
                pc.set_charge_bypass(True)
        self.assertIn("readback mismatch", str(ctx.exception))

    def test_absent_attribute(self):
        self.tree.path("power_supply/BAT0/charge_behaviour").unlink()
        with self.assertRaises(pc.ControlError) as ctx:
            pc.set_charge_bypass(True)
        self.assertIn("does not exist", str(ctx.exception))


class StateTest(PowercapCase):
    def test_load_state_missing_and_corrupt(self):
        with mock.patch.object(pc, "STATE_FILE", str(self.tree.path("none.json"))):
            self.assertEqual(pc.load_state(), {})
        bad = self.tree.write("bad.json", "{not json")
        with mock.patch.object(pc, "STATE_FILE", str(bad)):
            self.assertEqual(pc.load_state(), {})

    def test_save_state_round_trip(self):
        state_file = self.tree.path("state/power.json")
        with mock.patch.object(pc, "STATE_FILE", str(state_file)), \
             mock.patch.object(pc, "STATE_DIR", str(state_file.parent)):
            pc.save_state({"profile": "quiet", "limits": {"pl1": 10}})
            self.assertEqual(pc.load_state()["profile"], "quiet")

    def test_save_state_failure_is_a_control_error(self):
        blocker = self.tree.write("blocker", "x")
        with mock.patch.object(pc, "STATE_FILE", str(blocker / "power.json")), \
             mock.patch.object(pc, "STATE_DIR", str(blocker)):
            with self.assertRaises(pc.ControlError):
                pc.save_state({"a": 1})

    def test_platform_profile_dry_run_and_failure(self):
        self.assertEqual(pc.write_platform_profile("balanced", dry_run=True),
                         {"set": "balanced", "dry_run": True})
        with mock.patch.object(pc, "PLATFORM_PROFILE",
                               str(self.tree.path("absent/profile"))):
            result = pc.write_platform_profile("balanced")
        self.assertIn("error", result)

    def test_snapshot_save_and_restore_baseline(self):
        self.zone(limits=(35.0, 80.0, 239.0))
        baseline = self.tree.path("state/power-baseline.json")
        baseline.parent.mkdir(parents=True, exist_ok=True)
        ns = argparse.Namespace(save=True, json=False)
        with mock.patch.object(pc, "BASELINE_FILE", str(baseline)), \
             mock.patch.object(pc, "STATE_DIR", str(baseline.parent)), \
             mock.patch.object(pc, "collect_status",
                               return_value={"domains": [], "platform_profile": None}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(pc.cmd_snapshot(ns), 0)
        self.assertTrue(baseline.exists())
        data = json.loads(baseline.read_text())
        self.assertEqual(data["version"], pc.VERSION)

    def test_restore_baseline_without_file_is_exit_2(self):
        with mock.patch.object(pc, "BASELINE_FILE", str(self.tree.path("absent.json"))):
            rc, _, err = run_main(["restore-baseline"])
        self.assertEqual(rc, 2)
        self.assertIn("snapshot --save", err)

    def test_restore_without_state_is_exit_2(self):
        with mock.patch.object(pc, "STATE_FILE", str(self.tree.path("absent.json"))), \
             mock.patch.object(pc, "check_root", lambda *a, **k: None):
            rc, _, err = run_main(["restore"])
        self.assertEqual(rc, 2)
        self.assertIn("no saved state", err)

    def test_restore_dry_run_applies_profile(self):
        self.zone(limits=(10.0, 25.0, 90.0))
        state_file = self.tree.write("state/power.json", json.dumps(
            {"profile": "balanced", "limits": {"pl1": 25.0},
             "battery": {"end_threshold": 80}}))
        with mock.patch.object(pc, "STATE_FILE", str(state_file)), \
             mock.patch.object(pc, "check_root", lambda *a, **k: None):
            rc, out, _ = run_main(["restore", "--dry-run", "--json"])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertIn("profile balanced", data["restored"])
        self.assertTrue(any("limits PL1" in r for r in data["restored"]))


class EcMapTest(unittest.TestCase):
    """The recovered vendor register map is data, so it gets a data test.

    The confirmed offsets are cross-checked against upstream oxpec.c's own
    constants: if someone edits EC_MAP, one of these has to be re-derived from
    upstream/oxpec-ec-addresses.md, not tweaked by feel.
    """

    # drivers/platform/x86/oxpec.c (master, 2026-11-09)
    OXPEC_PWM_ENABLE = 0x4A      # OXP_SENSOR_PWM_ENABLE_REG
    OXPEC_PWM = 0x4B             # OXP_SENSOR_PWM_REG
    OXPEC_FAN = 0x58             # OXP_2_SENSOR_FAN_REG (oxp_g1_i)
    OXPEC_CHARGE_LIMIT = 0xA3    # OXP_X1_CHARGE_LIMIT_REG
    OXPEC_CHARGE_INHIBIT = 0xA4  # OXP_X1_CHARGE_INHIBIT_REG

    def row(self, what):
        for entry in pc.EC_MAP:
            if entry[0] == what:
                return entry
        self.fail(f"EC_MAP lost the {what!r} row")

    def test_confirmed_rows_match_upstream_oxpec(self):
        self.assertEqual(int(self.row("fan mode")[1], 16), self.OXPEC_PWM_ENABLE)
        self.assertEqual(int(self.row("fan duty")[1], 16), self.OXPEC_PWM)
        self.assertEqual(int(self.row("fan tach H")[1], 16), self.OXPEC_FAN)
        self.assertEqual(int(self.row("charge limit")[1], 16), self.OXPEC_CHARGE_LIMIT)
        self.assertEqual(int(self.row("bypass power")[1], 16), self.OXPEC_CHARGE_INHIBIT)

    def test_offsets_are_unique_and_in_the_0x400_space(self):
        offsets = [row[1] for row in pc.EC_MAP]
        self.assertEqual(len(offsets), len(set(offsets)), "duplicate EC offset in EC_MAP")
        for entry in pc.EC_MAP:
            vendor = int(entry[2], 16)
            self.assertEqual(vendor - 0x400, int(entry[1], 16),
                             f"{entry[0]}: vendor value is not 0x400 + offset")
            self.assertIn(entry[5], ("confirmed", "candidate"))

    def test_only_the_charge_and_fan_core_is_confirmed(self):
        confirmed = {row[0] for row in pc.EC_MAP if row[5] == "confirmed"}
        self.assertEqual(confirmed, {"fan mode", "fan duty", "fan tach H",
                                     "fan tach L", "charge limit", "bypass power"})

    def test_bypass_encoding_is_the_bit_mask(self):
        encoding = self.row("bypass power")[4]
        self.assertIn("0x00", encoding)
        self.assertIn("0x01", encoding)
        self.assertIn("0x03", encoding)

    def test_map_cli_json(self):
        rc, out, _ = run_main(["ec", "map", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        fields = {row["vendor_field"] for row in data["registers"]}
        self.assertIn("addrBypassPower", fields)
        self.assertIn("addrChargeLimit", fields)

    def test_map_cli_text_mentions_provenance(self):
        rc, out, _ = run_main(["ec", "map"])
        self.assertEqual(rc, 0)
        self.assertIn("0xA4", out)
        self.assertIn("upstream/oxpec-ec-addresses.md", out)


class EcProbeTest(unittest.TestCase):
    """`ec probe` must describe the interfaces and never claim a write path."""

    def setUp(self):
        self.tree = support.TmpTree()
        self.addCleanup(self.tree.cleanup)
        self.hwmon = str(self.tree.path("hwmon"))
        self.supply = str(self.tree.path("power_supply/BAT*"))
        for name, value in (("HWMON", self.hwmon), ("BATTERY_GLOB", self.supply)):
            patcher = mock.patch.object(pc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_probe_without_interfaces_is_partial(self):
        rc, out, err = run_main(["ec", "probe"])
        self.assertEqual(rc, 2)
        self.assertIn("absent", out)
        self.assertIn("no oxp_ec fan hwmon node", out)

    def test_probe_reports_states_from_a_simulated_sysfs(self):
        node = support.make_fan(self.tree, directory="hwmon/hwmon6")
        # A real fan1_input is 0444; the synthetic tree is owned by the test
        # user, so the mode is what has to carry the classification.
        os.chmod(node / "fan1_input", 0o444)
        support.make_battery(self.tree, end_threshold=80,
                             behaviour="[auto] inhibit-charge inhibit-charge-awake")
        rc, out, _ = run_main(["ec", "probe", "--json"])
        self.assertEqual(rc, 0, out)
        info = json.loads(out)
        self.assertEqual(info["fan"]["pwm1"], "writable")
        self.assertEqual(info["fan"]["fan1_input"], "read-only")
        self.assertEqual(info["battery"]["charge_control_end_threshold"], "writable")
        self.assertIsNone(info["raw_ec_window"])

    def test_probe_finds_a_simulated_raw_ec_window(self):
        support.make_fan(self.tree, directory="hwmon/hwmon6")
        window = self.tree.write("ec/ec0/io", "00" * 256)
        with mock.patch.object(pc, "EC_WINDOW_CANDIDATES", (str(window),)):
            rc, out, _ = run_main(["ec", "probe", "--json"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["raw_ec_window"]["path"], str(window))

    def test_write_is_refused_even_with_a_window(self):
        window = self.tree.write("ec/ec0/io", "00" * 256)
        with mock.patch.object(pc, "EC_WINDOW_CANDIDATES", (str(window),)), \
             mock.patch.object(pc, "check_root", lambda *a, **k: None):
            rc, out, err = run_main(["ec", "write"])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("refusing to write EC RAM directly", err)
        self.assertEqual(window.read_text(), "00" * 256)  # untouched


if __name__ == "__main__":
    unittest.main()
