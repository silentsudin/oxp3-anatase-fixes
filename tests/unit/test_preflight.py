"""Offline tests for tools/oxp3_preflight.py (the preflight validator).

Every probe is a pure function over a ``FixtureHost``: no host sysfs, no
subprocess, no root, no hardware. The fixture files under
``tests/fixtures/preflight`` are the same ones ``--selftest`` runs, so a parser
regression and a verdict regression fail in both places.

Nothing here writes outside a temporary directory (and these tests do not need
one: the fixture host is in memory).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import support

PF = support.load("oxp3_preflight", "oxp3_preflight.py")
FIXTURES = support.REPO / "tests" / "fixtures" / "preflight"


def fixture(name: str):
    return PF.load_fixture(FIXTURES / (name + ".json"))


def context_for(name: str, selected=None):
    data = fixture(name)
    host = PF.FixtureHost(data)
    return PF.Context(
        host,
        str(data.get("user") or "tester"),
        str(data.get("home") or "/home/tester"),
        str(data.get("repo") or "/repo"),
        int(data.get("euid", 0)),
        tuple(selected or PF.COMPONENTS),
        False,
    )


class ParserTest(unittest.TestCase):
    def test_systemd_version(self):
        self.assertEqual(PF.parse_systemd_version("systemd 262 (262-3.fc45)"), 262)
        self.assertEqual(PF.parse_systemd_version("systemd 245\n+PAM +AUDIT"), 245)
        self.assertIsNone(PF.parse_systemd_version("not systemd"))
        self.assertIsNone(PF.parse_systemd_version(None))

    def test_passwd_lookup(self):
        text = "root:x:0:0::/root:/bin/bash\ntester:x:1000:1000::/home/tester:/bin/bash\n"
        self.assertEqual(PF.parse_passwd(text, "tester"), (1000, "/home/tester"))
        self.assertIsNone(PF.parse_passwd(text, "nobody"))
        self.assertIsNone(PF.parse_passwd(None, "tester"))

    def test_kernel_options(self):
        opts = PF.parse_kernel_options(
            "CONFIG_HIDRAW=y\n"
            "# CONFIG_INPUT_UINPUT is not set\n"
            "CONFIG_BMI260=m\n"
            'CONFIG_MODULE_SIG_HASH="sha512"\n'
        )
        self.assertEqual(opts["CONFIG_HIDRAW"], "y")
        self.assertEqual(opts["CONFIG_INPUT_UINPUT"], "n")
        self.assertEqual(opts["CONFIG_BMI260"], "m")
        self.assertEqual(opts["CONFIG_MODULE_SIG_HASH"], "sha512")

    def test_unit_restart_sections(self):
        unit = "[Unit]\nRestart=always\n[Service]\nRestart=on-failure\nRestartSec=2\n"
        self.assertEqual(PF.parse_unit_restart(unit),
                         [("Unit", "always"), ("Service", "on-failure")])
        self.assertEqual(PF.parse_unit_restart("[Service]\n#Restart=always\n"), [])

    def test_logind_defaults_and_overrides(self):
        effective = PF.parse_logind_options([
            "[Login]\n#HandlePowerKey=poweroff\n#HandleSuspendKey=suspend\n",
            "[Login]\nHandlePowerKey=suspend\n",
        ])
        self.assertEqual(effective["HandlePowerKey"], "suspend")
        self.assertEqual(effective["HandleSuspendKey"], "suspend")
        # A key only in a later file wins; a commented key is a default.
        effective = PF.parse_logind_options([
            "[Login]\nHandleLidSwitch=ignore\n",
            "[Login]\n#HandleLidSwitch=suspend\n",
        ])
        self.assertEqual(effective["HandleLidSwitch"], "ignore")

    def test_hhd_version(self):
        self.assertEqual(PF.parse_hhd_version("4.1.12+git.154.geca609b0f54a"),
                         (4, 1, 12))
        self.assertIsNone(PF.parse_hhd_version("no version here"))
        self.assertTrue(PF.version_at_least((4, 1, 12), PF.HHD_MIN))
        self.assertFalse(PF.version_at_least((4, 1, 11), PF.HHD_MIN))

    def test_stale_checkout_references(self):
        repo = "/var/home/tester/git/oxp3-anatase-fixes"
        text = (
            "ExecStart=/var/home/tester/git/oxp3-anatase-fixes/tools/x.py\n"
            "ANV=/var/home/tester/old/oxp3-anatase-fixes/tools/y.sh\n"
            "X=%h/git/oxp3-anatase-fixes/tools/z.sh\n"
        )
        self.assertEqual(
            PF.stale_checkout_refs(text, repo, "/var/home/tester"),
            ["/var/home/tester/old/oxp3-anatase-fixes"],
        )
        self.assertEqual(PF.stale_checkout_refs(
            "X=/var/home/tester/git/oxp3-anatase-fixes/tools/z.sh", repo,
            "/var/home/tester"), [])


class ProbeTest(unittest.TestCase):
    def test_vendor_hid_prefers_the_input_less_node(self):
        host = PF.FixtureHost({
            "dirs": ["/sys/class/hidraw/hidraw2/device/input",
                     "/sys/class/hidraw/hidraw4/device"],
            "files": {
                "/sys/class/hidraw/hidraw2/device/uevent":
                    "HID_ID=0003:00001A86:0000FE00\n",
                "/sys/class/hidraw/hidraw4/device/uevent":
                    "HID_ID=0003:00001A86:0000FE00\n",
                "/sys/class/hidraw/hidraw4/device/report_descriptor": "\x06\x00\xff",
            },
        })
        ctx = PF.Context(host, "tester", "/home/tester", "/repo", 0,
                         PF.COMPONENTS, False)
        self.assertEqual(PF.find_vendor_hidraw(ctx), "/dev/hidraw4")

    def test_component_state_counts(self):
        ctx = context_for("conflicting-daemon")
        present, total, missing = PF.component_state(ctx, "volume")
        self.assertEqual((present, total), (2, 4))
        self.assertEqual(len(missing), 2)

    def test_verdict_safe_degraded_and_unsafe(self):
        okc = [PF.ok("a", "x", "t")]
        warnc = [PF.ok("a", "x", "t"), PF.warn("b", "x", "t", "s", "f")]
        badc = [PF.fail("a", "x", "t", "s", "f", ("volume",))]
        self.assertEqual(PF.explain_verdict(okc, PF.COMPONENTS, False)[:2],
                         (True, 0))
        safe, code, _skip, warns = PF.explain_verdict(warnc, PF.COMPONENTS, False)
        self.assertTrue(safe)
        self.assertEqual(code, 0)
        self.assertEqual(warns, ["b"])
        self.assertEqual(PF.explain_verdict(warnc, PF.COMPONENTS, True)[1], 1)
        safe, code, skipped, _warns = PF.explain_verdict(badc, PF.COMPONENTS, False)
        self.assertFalse(safe)
        self.assertEqual(code, 1)
        self.assertEqual(skipped, ["volume"])


class FixtureTest(unittest.TestCase):
    def test_every_fixture_matches_its_expected_verdict(self):
        cases = sorted(p for p in FIXTURES.glob("*.json")
                       if not p.name.startswith("base"))
        self.assertGreaterEqual(len(cases), 7)
        for path in cases:
            with self.subTest(fixture=path.stem):
                passed, errors = PF.run_fixture(path)
                self.assertTrue(passed, "%s: %s" % (path.stem, errors))

    def test_sane_host_has_no_warnings(self):
        ctx = context_for("base")
        checks = PF.run_checks(ctx)
        self.assertEqual([c.id for c in checks if c.state == PF.FAIL], [])
        self.assertEqual([c.id for c in checks if c.state == PF.WARN], [])

    def test_missing_dir_blocks_only_the_unit_components(self):
        ctx = context_for("missing-dir")
        checks = PF.run_checks(ctx)
        comps = PF.component_report(checks, ctx.selected)
        safe, code, skipped, _warns = PF.explain_verdict(checks, ctx.selected, False)
        self.assertFalse(safe)
        self.assertEqual(code, 1)
        self.assertEqual(skipped, ["volume", "leds", "sleep", "power"])
        self.assertEqual(comps["volume"]["state"], "blocked")
        self.assertEqual(comps["audio"]["state"], "ready")

    def test_unprivileged_is_safe_but_degraded(self):
        ctx = context_for("unprivileged")
        checks = PF.run_checks(ctx)
        safe, code, _skipped, warns = PF.explain_verdict(checks, ctx.selected, False)
        self.assertTrue(safe)
        self.assertEqual(code, 0)
        self.assertIn("core.root", warns)
        self.assertEqual(PF.explain_verdict(checks, ctx.selected, True)[1], 1)

    def test_component_filter_selects_the_right_checks(self):
        ctx = context_for("base", selected=("volume",))
        checks = PF.run_checks(ctx)
        ids = {c.id for c in checks}
        self.assertIn("state.volume", ids)
        self.assertIn("tool.evdev", ids)
        self.assertNotIn("state.audio", ids)
        self.assertNotIn("tool.pactl", ids)

    def test_json_shape(self):
        ctx = context_for("base")
        checks = PF.run_checks(ctx)
        comps = PF.component_report(checks, ctx.selected)
        safe, code, skipped, warns = PF.explain_verdict(checks, ctx.selected, False)
        data = json.loads(PF.render_json(ctx, checks, comps, safe, code, skipped, warns))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["tool"], "oxp3-preflight")
        self.assertTrue(data["safe"])
        self.assertEqual(data["verdict"], "safe")
        self.assertEqual(data["skipped_components"], [])
        self.assertEqual(sum(data["counts"][k] for k in ("ok", "warn", "fail")),
                         data["counts"]["total"])
        self.assertEqual(len(data["checks"]), data["counts"]["total"])
        for check in data["checks"]:
            self.assertIn(check["state"], (PF.OK, PF.WARN, PF.FAIL))
            self.assertTrue(check["id"])

    def test_text_names_the_verdict_and_fixes(self):
        ctx = context_for("unprivileged")
        checks = PF.run_checks(ctx)
        comps = PF.component_report(checks, ctx.selected)
        safe, _code, skipped, warns = PF.explain_verdict(checks, ctx.selected, False)
        text = PF.render_text(ctx, checks, comps, safe, skipped, warns, False)
        self.assertIn("verdict: SAFE", text)
        self.assertIn("fix:", text)
        self.assertIn("core.root", text)


if __name__ == "__main__":
    unittest.main()
