"""Offline tests for the health front-end's aggregation and verdict logic.

``tools/oxp3-health.sh`` composes four existing reporters.  The interesting part
is not running them (that is the ``health`` repo-test suite) but what it does
with their JSON: the READY/INSTALLED/WORKING verdict, the non-PASS-only rows,
the next-command selection and the behaviour when a reporter is missing, crashes
or times out.

The five fixtures under ``tests/fixtures/health`` cover all-pass, blockers,
partial install, missing tools and a crashing tool.  They are the same files the
tool's ``--selftest`` reads.  The stage runner is also exercised against stub
shell scripts in a temporary directory, so no host state, no root and no real
hardware is touched.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest

import support

REPO = support.REPO
HEALTH = REPO / "tools" / "oxp3-health.sh"
BACKEND = REPO / "tools" / "oxp3_health.py"
FIXTURES = REPO / "tests" / "fixtures" / "health"

health = support.load("oxp3_health", "oxp3_health.py")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def report_for(name: str):
    fixture = load_fixture(name)
    return health.build_report(fixture["stages"]), fixture


class StubTools:
    """A temporary tools/ directory holding scripted reporter stubs."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="oxp3-health-test-")
        self.root = pathlib.Path(self._tmp.name)

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, body: str, mode: int = 0o755) -> pathlib.Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)
        return path


class AggregateFixtureTest(unittest.TestCase):
    def test_all_pass_is_ok(self):
        report, fixture = report_for("all-pass")
        expect = fixture["expect"]
        self.assertTrue(report["ready_to_install"])
        self.assertEqual(expect["verdict"], report["verdict"])
        self.assertEqual(expect["exit_code"], report["exit_code"])
        self.assertEqual(3, report["installed"]["done"])
        self.assertEqual([], report["installed"]["missing"])
        self.assertEqual(2, report["components"]["installed"])
        self.assertEqual(2, report["components"]["total"])
        self.assertEqual([], report["problems"])
        self.assertEqual(1, len(report["next_commands"]))
        self.assertIn("oxp3-firstrun.sh --collect", report["next_commands"][0])

    def test_blockers_mean_not_ready(self):
        report, fixture = report_for("blockers")
        self.assertFalse(report["ready_to_install"])
        self.assertEqual(["core.disk"], [b["id"] for b in report["blockers"]])
        self.assertEqual("attention", report["verdict"])
        self.assertEqual(1, report["exit_code"])
        text = health.render_text(report, fixture["stages"])
        self.assertIn("READY TO INSTALL: no", text)
        self.assertIn("BLOCK  core.disk", text)
        self.assertIn("fix: sudo rpm-ostree cleanup -p", text)

    def test_partial_install_is_not_a_failure(self):
        report, fixture = report_for("partial-install")
        self.assertTrue(report["ready_to_install"])
        self.assertEqual("ok", report["verdict"])
        self.assertEqual(0, report["exit_code"])
        self.assertEqual(2, report["installed"]["done"])
        self.assertEqual(5, report["installed"]["total"])
        self.assertEqual(
            ["hw-install", "udev-rules", "bluetooth"],
            [row["id"] for row in report["installed"]["missing"]],
        )
        self.assertEqual(1, report["components"]["installed"])
        self.assertEqual(3, report["components"]["total"])
        self.assertEqual("bluetooth", report["components"]["partial"][0]["id"])
        text = health.render_text(report, fixture["stages"])
        self.assertIn("INSTALLED: 2/5 steps", text)
        self.assertIn("partial: bluetooth 2/3", text)

    def test_missing_tools_are_reported_not_fatal(self):
        report, fixture = report_for("missing-tools")
        self.assertIsNone(report["ready_to_install"])
        self.assertEqual("attention", report["verdict"])
        self.assertEqual(1, report["exit_code"])
        self.assertEqual(
            ["preflight", "firstrun", "status"],
            [problem["stage"] for problem in report["problems"]],
        )
        self.assertEqual(1, report["verify"]["counts"]["working"])
        text = health.render_text(report, fixture["stages"])
        self.assertIn("TOOL PROBLEMS (3)", text)
        self.assertIn("READY TO INSTALL: unknown", text)
        self.assertIn("INSTALLED: unknown", text)
        self.assertIn("WORKING / FAILING: 1 working", text)

    def test_crashing_tool_keeps_the_other_stages(self):
        report, fixture = report_for("tool-crash")
        self.assertEqual(["verify"], [problem["stage"] for problem in report["problems"]])
        self.assertIn("backend could not start", report["problems"][0]["error"])
        # The stages that did answer are still aggregated.
        self.assertTrue(report["ready_to_install"])
        self.assertEqual(1, report["installed"]["done"])
        self.assertEqual(2, report["installed"]["total"])
        self.assertEqual(["sched.unit"], [row["id"] for row in report["status"]["non_ok"]])
        self.assertEqual("attention", report["verdict"])
        text = health.render_text(report, fixture["stages"])
        self.assertIn("! verify", text)
        self.assertIn("WARN     status/sched.unit", text)

    def test_no_usable_stage_is_unknown(self):
        stages = {
            name: {"status": "missing", "error": "not found", "rc": None, "duration_ms": 0}
            for name in health.STAGE_ORDER
        }
        report = health.build_report(stages)
        self.assertEqual("unknown", report["verdict"])
        self.assertEqual(3, report["exit_code"])
        self.assertEqual(4, len(report["problems"]))

    def test_all_stages_skipped_is_unknown_without_problems(self):
        stages = {
            name: {"status": "skipped", "error": "", "rc": None, "duration_ms": 0}
            for name in health.STAGE_ORDER
        }
        report = health.build_report(stages)
        self.assertEqual("unknown", report["verdict"])
        self.assertEqual(3, report["exit_code"])
        self.assertEqual([], report["problems"])


class RenderTest(unittest.TestCase):
    def test_text_lists_only_non_pass_rows(self):
        report, fixture = report_for("all-pass")
        text = health.render_text(report, fixture["stages"])
        # A PASS summary must not be echoed: only non-PASS rows are printed.
        self.assertIn("READY TO INSTALL: yes", text)
        self.assertIn("== NEXT", text)
        self.assertNotIn("ec418f04e589965a", text)
        self.assertNotIn("preflight.verdict", text)

    def test_quiet_is_exactly_one_verdict_line(self):
        report, _ = report_for("partial-install")
        line = health.quiet_line(report)
        self.assertEqual(1, len(line.splitlines()))
        self.assertTrue(line.startswith("health: READY yes"))
        self.assertIn("INSTALLED 2/5", line)
        self.assertIn("verdict ok", line)
        report, _ = report_for("blockers")
        self.assertIn("verdict attention", health.quiet_line(report))
        report, _ = report_for("missing-tools")
        self.assertIn("READY unknown", health.quiet_line(report))

    def test_json_is_schema_one_and_serialisable(self):
        report, _ = report_for("all-pass")
        encoded = health.render_json(report)
        parsed = json.loads(encoded)
        self.assertEqual(1, parsed["schema"])
        self.assertEqual("oxp3-health", parsed["tool"])
        self.assertEqual("1.0.0", parsed["version"])
        self.assertIsInstance(parsed["stages"], dict)
        self.assertEqual(set(health.STAGE_ORDER), set(parsed["stages"]))
        self.assertIsInstance(parsed["exit_code"], int)
        for key in ("ready_to_install", "blockers", "installed", "verify", "status",
                    "problems", "next_commands", "verdict"):
            self.assertIn(key, parsed)

    def test_next_commands_are_bounded_deduplicated_and_cleaned(self):
        stages = {
            "preflight": {
                "status": "ok",
                "rc": 1,
                "duration_ms": 1,
                "data": {
                    "safe": False,
                    "checks": [
                        {"id": "core.disk", "state": "FAIL", "summary": "low space",
                         "remediation": str(REPO / "tools" / "oxp3-rollback.sh") + " adopt   # then status"}
                    ],
                },
            },
            "verify": {
                "status": "ok",
                "rc": 1,
                "duration_ms": 1,
                "data": {
                    "counts": {"total": 3, "pass": 1, "fail": 2, "skip": 0, "unknown": 0},
                    "checks": [
                        {"id": "bt.journal", "group": "bluetooth", "state": "fail",
                         "summary": "firmware", "remediation": "sudo tools/oxp3-bt-firmware.sh"},
                        {"id": "bt.controller", "group": "bluetooth", "state": "fail",
                         "summary": "controller", "remediation": "sudo modprobe btintel_pcie"},
                        {"id": "inst.backups", "group": "install", "state": "fail",
                         "summary": "no backup",
                         "remediation": str(REPO / "tools" / "oxp3-rollback.sh") + " adopt   # then status"},
                    ],
                },
            },
            "firstrun": {
                "status": "ok",
                "rc": 0,
                "duration_ms": 1,
                "data": {
                    "steps": [
                        {"id": "hw-install", "state": "todo", "command": "sudo tools/oxp3-hw-install.sh --all"},
                        {"id": "udev-rules", "state": "todo", "command": "sudo tools/oxp3-hw-install.sh --leds"},
                    ]
                },
            },
            "status": {"status": "ok", "rc": 0, "duration_ms": 1, "data": {"overall": "warn", "groups": []}},
        }
        report = health.build_report(stages)
        commands = report["next_commands"]
        self.assertLessEqual(len(commands), 3)
        self.assertEqual(len(set(commands)), len(commands))
        # The first fail of each verify group, then the blocker, then install steps.
        self.assertEqual("tools/oxp3-rollback.sh adopt", commands[0])
        self.assertIn("sudo tools/oxp3-bt-firmware.sh", commands)
        # No repo prefix and no trailing comment.
        for command in commands:
            self.assertNotIn(str(REPO), command)
            self.assertNotIn("   #", command)


class StageRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tools = StubTools()
        self.addCleanup(self.tools.cleanup)

    def run_stage(self, name=None, timeout=5, full=False):
        name = name or "preflight"
        return health.run_stage(name, timeout, tool_dir=self.tools.root, full=full, cwd=str(REPO))

    def test_missing_tool_is_a_missing_stage(self):
        stage = self.run_stage()
        self.assertEqual("missing", stage["status"])
        self.assertIn("not found", stage["error"])
        self.assertIsNone(stage["rc"])

    def test_crashing_tool_is_an_error_stage(self):
        self.tools.write("oxp3-preflight.sh", "#!/bin/sh\nexit 9\n")
        stage = self.run_stage()
        self.assertEqual("error", stage["status"])
        self.assertEqual(9, stage["rc"])
        self.assertIn("no output", stage["error"])

    def test_tool_that_prints_garbage_is_an_error_stage(self):
        self.tools.write("oxp3-preflight.sh", "#!/bin/sh\nprintf 'not json\\n'\nexit 0\n")
        stage = self.run_stage()
        self.assertEqual("error", stage["status"])
        self.assertIn("not valid JSON", stage["error"])

    def test_a_verdict_exit_is_data(self):
        # preflight exits 1 when unsafe; that is a verdict, not a dead backend.
        self.tools.write(
            "oxp3-preflight.sh",
            '#!/bin/sh\nprintf \'%s\\n\' \'{"schema": 1, "safe": false}\'\nexit 1\n',
        )
        stage = self.run_stage()
        self.assertEqual("ok", stage["status"])
        self.assertEqual(1, stage["rc"])
        self.assertFalse(stage["data"]["safe"])

    def test_backend_start_failure_is_an_error(self):
        self.tools.write(
            "oxp3-preflight.sh",
            '#!/bin/sh\nprintf \'%s\\n\' \'{"schema": 1}\'\nexit 3\n',
        )
        stage = self.run_stage()
        self.assertEqual("error", stage["status"])
        self.assertIn("backend could not start", stage["error"])

    def test_timeout_is_a_timeout_stage(self):
        self.tools.write("oxp3-preflight.sh", "#!/bin/sh\nsleep 5\n")
        stage = self.run_stage(timeout=0.3)
        self.assertEqual("timeout", stage["status"])
        self.assertIn("timed out", stage["error"])

    def test_collect_runs_every_stage_and_keeps_going(self):
        fixture = load_fixture("partial-install")["stages"]
        self.tools.write(
            "oxp3-preflight.sh",
            "#!/bin/sh\ncat <<'EOF'\n"
            + json.dumps(fixture["preflight"]["data"])
            + "\nEOF\n",
        )
        self.tools.write("oxp3-verify.sh", "#!/bin/sh\nexit 3\n")
        stages = health.collect(tool_dir=self.tools.root, cwd=str(REPO))
        self.assertEqual("ok", stages["preflight"]["status"])
        self.assertEqual("error", stages["verify"]["status"])
        self.assertEqual("missing", stages["firstrun"]["status"])
        self.assertEqual("missing", stages["status"]["status"])
        report = health.build_report(stages)
        self.assertEqual(["verify", "firstrun", "status"], [p["stage"] for p in report["problems"]])
        self.assertTrue(report["ready_to_install"])
        self.assertEqual(1, report["exit_code"])

    def test_full_passes_through_to_verify_only(self):
        self.assertNotIn("--full", health.stage_argv("verify", self.tools.root, full=False))
        self.assertIn("--full", health.stage_argv("verify", self.tools.root, full=True))
        self.assertNotIn("--full", health.stage_argv("preflight", self.tools.root, full=True))


class LogTest(unittest.TestCase):
    def test_log_has_the_summary_and_every_raw_stage(self):
        report, _ = report_for("partial-install")
        stages = load_fixture("partial-install")["stages"]
        with tempfile.TemporaryDirectory(prefix="oxp3-health-log-") as tmp:
            path = pathlib.Path(tmp) / "nested" / "health.log"
            written = health.write_log(str(path), report, stages)
            self.assertEqual(str(path), written)
            text = path.read_text(encoding="utf-8")
            self.assertIn("READY TO INSTALL", text)
            self.assertIn("raw stage output", text)
            for name in health.STAGE_ORDER:
                self.assertIn("## %s:" % name, text)


class CliContractTest(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run(
            [str(BACKEND), *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_help_and_bad_option(self):
        res = self.run_tool("--help")
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn("--no-preflight", res.stdout)
        res = self.run_tool("--bogus")
        self.assertEqual(2, res.returncode, res.stdout + res.stderr)

    def test_skipping_every_stage_is_a_usage_error(self):
        res = self.run_tool("--no-preflight", "--no-verify", "--no-firstrun", "--no-status")
        self.assertEqual(2, res.returncode)
        self.assertIn("nothing to report", res.stderr)

    def test_selftest_passes(self):
        res = self.run_tool("--selftest")
        self.assertEqual(0, res.returncode, res.stdout + res.stderr)
        self.assertIn("selftest:", res.stdout)
        self.assertIn("fixtures", res.stdout)
        self.assertNotIn("FAIL ", res.stdout)

    def test_quiet_still_writes_its_log(self):
        with tempfile.TemporaryDirectory(prefix="oxp3-health-quiet-") as tmp:
            log = pathlib.Path(tmp) / "health.log"
            res = subprocess.run(
                [str(HEALTH), "--quiet", "--no-verify", "--log", str(log)],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                timeout=120,
            )
            # A live report: 0 or 1 is the verdict, 3 means nothing ran.
            self.assertIn(res.returncode, (0, 1), res.stdout + res.stderr)
            self.assertEqual(1, len(res.stdout.splitlines()))
            self.assertTrue(log.is_file())
            self.assertIn("READY TO INSTALL", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
