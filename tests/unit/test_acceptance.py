"""Offline tests for the goal-level acceptance evaluator.

``tools/oxp3-acceptance.sh`` has two halves: a live collector (verify, health and
the relevant repo-test suites) and a pure aggregation over the resulting evidence
bundle.  Only the aggregation is decided here, through the four synthetic bundles
in ``tests/fixtures/acceptance``:

  * ``all-green``         every item is implemented and every gate is recorded
  * ``awaiting-install``  the install steps are open, so nothing may be "proven"
  * ``blocked``           SAGV is registry-blocked and Bluetooth checks FAIL
  * ``missing-evidence``  no verifier/health/repo-test data at all

What is asserted, in order of importance:

  * a verdict is never stronger than the evidence: no item is complete without
    its device checks or its recorded acceptance artefact/sign-off;
  * "implemented-and-proven-offline" is an offline claim, and the report never
    calls it proven on the device (the words are asserted absent);
  * a missing source degrades to a named evidence gap, never to a silent pass;
  * the JSON contract (schema, seven items, closed verdict vocabulary) and the
    text report shape are stable.

No host state, root, network or hardware is touched; the live CLI is exercised
only through ``--evidence`` and ``--selftest``.
"""

from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import unittest

import support

REPO = support.REPO
TOOL = REPO / "tools" / "oxp3-acceptance.sh"
BACKEND = REPO / "tools" / "oxp3_acceptance.py"
FIXTURES = REPO / "tests" / "fixtures" / "acceptance"

acceptance = support.load("oxp3_acceptance", "oxp3_acceptance.py")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def report_for(name: str) -> dict:
    return acceptance.evaluate(fixture(name))


def item_by_id(report: dict, item_id: str) -> dict:
    return next(item for item in report["items"] if item["id"] == item_id)


class FixtureSetTest(unittest.TestCase):
    def test_all_four_fixtures_exist(self):
        for name in ("all-green", "awaiting-install", "blocked", "missing-evidence"):
            with self.subTest(name=name):
                self.assertTrue((FIXTURES / (name + ".json")).is_file())

    def test_fixture_bundles_are_synthetic_and_complete(self):
        for name in ("all-green", "awaiting-install", "blocked", "missing-evidence"):
            with self.subTest(name=name):
                bundle = fixture(name)
                self.assertEqual(bundle["schema"], 1)
                self.assertTrue(bundle["components"])
                self.assertIn("expect", bundle)
                self.assertIn("sources", bundle)


class AggregationTest(unittest.TestCase):
    def test_all_green_is_complete(self):
        report = report_for("all-green")
        self.assertEqual(report["counts"]["complete"], 7)
        self.assertEqual(report["counts"]["automated_complete"], 7)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["missing_evidence"], [])
        for item in report["items"]:
            with self.subTest(item=item["id"]):
                self.assertEqual(item["verdict"],
                                 "implemented-and-proven-offline")
                self.assertTrue(item["complete"])
                self.assertFalse(item["missing_evidence"])

    def test_awaiting_install_never_claims_proof(self):
        report = report_for("awaiting-install")
        for item_id in ("volume", "leds", "sleep", "decky"):
            with self.subTest(item=item_id):
                item = item_by_id(report, item_id)
                self.assertEqual(item["verdict"], "awaiting-install")
                self.assertFalse(item["complete"])
                self.assertIn("install step(s) still open", item["reason"])
        # The audio implementation is installed in user scope, so it reaches the
        # offline rung - but its listening sign-off is still pending.
        audio = item_by_id(report, "audio")
        self.assertEqual(audio["verdict"], "implemented-and-proven-offline")
        self.assertFalse(audio["complete"])
        self.assertIn("audio.listen", [entry["id"] for entry in report["remaining"]])
        self.assertEqual(report["counts"]["complete"], 0)

    def test_blocked_reports_the_registry_and_live_failure(self):
        report = report_for("blocked")
        audit = item_by_id(report, "audit")
        self.assertEqual(audit["verdict"], "blocked")
        self.assertIn("sagv", audit["reason"])
        self.assertIn("bt.journal", audit["reason"])
        self.assertEqual(report["exit_code"], 1)
        # Nothing blocked may be complete, and nothing may be device-proven.
        self.assertFalse(audit["complete"])

    def test_missing_evidence_is_named_never_invented(self):
        report = report_for("missing-evidence")
        self.assertEqual(report["counts"]["complete"], 0)
        self.assertEqual(report["counts"]["automated_complete"], 0)
        for item in report["items"]:
            with self.subTest(item=item["id"]):
                self.assertNotEqual(item["verdict"],
                                    "implemented-and-proven-offline")
                self.assertFalse(item["complete"])
                if item["verdict"] != "blocked":
                    self.assertEqual(item["verdict"], "awaiting-hardware")
        self.assertTrue(any("evidence source unavailable: verify" in gap
                            for gap in report["missing_evidence"]))
        self.assertEqual(report["exit_code"], 1)

    def test_offline_suite_failure_blocks_that_item(self):
        bundle = fixture("awaiting-install")
        for suite in bundle["sources"]["repo_test"]["data"]["suites"]:
            if suite["name"] == "fp-python":
                suite["state"] = "fail"
                suite["reason"] = "synthetic failure"
        report = acceptance.evaluate(bundle)
        finger = item_by_id(report, "fingerprint")
        self.assertEqual(finger["verdict"], "blocked")
        self.assertIn("fp-python", finger["reason"])

    def test_device_check_failure_blocks(self):
        bundle = fixture("all-green")
        for check in bundle["sources"]["verify"]["data"]["checks"]:
            if check["id"] == "aud.eq":
                check["state"] = "fail"
                check["summary"] = "synthetic sink missing"
        report = acceptance.evaluate(bundle)
        audio = item_by_id(report, "audio")
        self.assertEqual(audio["verdict"], "blocked")
        self.assertIn("aud.eq", audio["reason"])
        self.assertFalse(audio["complete"])

    def test_no_device_check_means_no_offline_proof(self):
        bundle = fixture("all-green")
        # Offline evidence stays green, but every device check is unobservable.
        for check in bundle["sources"]["verify"]["data"]["checks"]:
            check["state"] = "skip"
            check["summary"] = "not observable"
        report = acceptance.evaluate(bundle)
        for item in report["items"]:
            with self.subTest(item=item["id"]):
                # The fingerprint item has no check ids at all; its evidence is
                # the transcript artefact, so it is judged separately below.
                if item["id"] != "fingerprint":
                    self.assertNotEqual(item["verdict"],
                                        "implemented-and-proven-offline")
                    self.assertFalse(item["complete"])
        self.assertEqual(item_by_id(report, "volume")["verdict"], "awaiting-hardware")

    def test_signoffs_and_artifacts_are_the_only_manual_gate(self):
        bundle = fixture("all-green")
        bundle["artifacts"]["signoffs"] = {}
        report = acceptance.evaluate(bundle)
        self.assertEqual(item_by_id(report, "audio")["verdict"],
                         "implemented-and-proven-offline")
        self.assertFalse(item_by_id(report, "audio")["complete"])
        self.assertFalse(item_by_id(report, "decky")["complete"])
        self.assertEqual(report["counts"]["complete"], 5)
        # The GOOD-cycle report is the sleep acceptance gate, not its only
        # device proof: with sleep.stats passing the verdict stays at the
        # offline rung but the item is no longer complete.
        bundle["artifacts"]["sleep_cycles_good"] = {
            "found": False, "path": "", "detail": "none"}
        report = acceptance.evaluate(bundle)
        self.assertFalse(item_by_id(report, "sleep")["complete"])
        # And with the real-suspend check also unobservable it does move.
        for check in bundle["sources"]["verify"]["data"]["checks"]:
            if check["id"] == "sleep.stats":
                check["state"] = "skip"
        report = acceptance.evaluate(bundle)
        self.assertEqual(item_by_id(report, "sleep")["verdict"], "awaiting-hardware")

    def test_missing_components_yaml_is_a_gap_not_a_crash(self):
        bundle = fixture("all-green")
        bundle["components"] = []
        bundle["components_error"] = "docs/hw/components.yaml is missing"
        report = acceptance.evaluate(bundle)
        self.assertIn("docs/hw/components.yaml is missing", report["missing_evidence"])
        self.assertFalse(any(item["complete"] for item in report["items"]))


class TextReportTest(unittest.TestCase):
    def test_text_lists_every_item_and_the_summary(self):
        report = report_for("awaiting-install")
        text = acceptance.render_text(report)
        for spec in acceptance.ITEM_SPECS:
            with self.subTest(item=spec["id"]):
                self.assertIn(spec["title"], text)
        self.assertIn("== summary: 7 items", text)
        self.assertIn("== what remains", text)
        self.assertIn("sudo tools/sleep-cycle-test.sh --cycles 3", text)
        self.assertIn("sudo tools/fingerprint/oxp3-fp-session.sh", text)

    def test_text_never_claims_device_proof_for_offline_evidence(self):
        text = acceptance.render_text(report_for("missing-evidence"))
        self.assertNotIn("proven on device", text)
        self.assertNotIn("device-proven", text.replace(
            "no item is called device-proven", ""))
        self.assertIn("offline claim only", text)

    def test_verbose_adds_mapped_checks(self):
        report = report_for("all-green")
        plain = acceptance.render_text(report)
        verbose = acceptance.render_text(report, verbose=True)
        self.assertGreater(len(verbose), len(plain))
        self.assertIn("vol.unit", verbose)

    def test_json_is_serializable_and_closed(self):
        report = report_for("all-green")
        json.dumps(report)
        self.assertEqual(report["tool"], "oxp3-acceptance")
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["verdict_vocabulary"], list(acceptance.VERDICTS))
        for item in report["items"]:
            self.assertIn(item["verdict"], acceptance.VERDICTS)
            self.assertIsInstance(item["complete"], bool)


class CliContractTest(unittest.TestCase):
    def run_tool(self, *args, timeout=120):
        return subprocess.run(
            [str(TOOL), *args],
            cwd=str(REPO), text=True, capture_output=True, timeout=timeout,
        )

    def test_help_exits_zero_with_usage(self):
        proc = self.run_tool("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage:", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_selftest_is_offline_and_green(self):
        proc = self.run_tool("--selftest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("selftest:", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_evidence_bundle_json_round_trip(self):
        bundle_path = FIXTURES / "awaiting-install.json"
        proc = self.run_tool("--evidence", str(bundle_path), "--json")
        # Exit 1 is data: the fixture contains a registry-blocked audit item.
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["schema"], 1)
        self.assertEqual(len(data["items"]), 7)
        self.assertIn("awaiting-install",
                      [item["verdict"] for item in data["items"]])

    def test_evidence_bundle_text_round_trip(self):
        proc = self.run_tool("--evidence", str(FIXTURES / "all-green.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("== summary: 7 items | 7 complete", proc.stdout)


if __name__ == "__main__":
    unittest.main()
