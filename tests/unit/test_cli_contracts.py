# SPDX-License-Identifier: MIT
"""Offline CLI contracts for entry points whose failure was invisible in the
normal suites.

Three behaviours are pinned here:

* ``tools/decky-oxp3-status/build.sh --help`` must print its header and exit 0.
  The script changes into its own directory before reading ``$0``; a relative
  invocation used to make that read fail while the pipeline still exited 0
  (empty help + a ``sed: can't read`` line on stderr).
* ``tools/oxp3-hw-install.sh`` documents ``2`` for usage errors, so an unknown
  option must exit 2 rather than the generic failure ``1``.
* ``tools/oxp3-verify.sh`` consumes ``oxp3-status.sh --json``.  Exit 1 there is
  a *verdict* (an indicator is fail), not a dead backend, and a failed first
  attempt must not be cached as success.  Both bugs made the dependent checks
  report SKIP/UNKNOWN instead of the real FAIL/PASS.

Everything runs against the shipped files or a private temporary directory: no
root, no hardware, no system state.
"""

from __future__ import annotations

import pathlib
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest

import support

REPO = support.REPO
VERIFY = REPO / "tools" / "oxp3-verify.sh"
BUILD = REPO / "tools" / "decky-oxp3-status" / "build.sh"
INSTALLER = REPO / "tools" / "oxp3-hw-install.sh"


def _function_body(path: pathlib.Path, name: str) -> str:
    """Return the source of a top-level shell function, closing brace included."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^%s\(\) \{.*?^\}$" % re.escape(name), text, flags=re.M | re.S)
    if not match:
        raise AssertionError("function %s not found in %s" % (name, path))
    return match.group(0)


class DeckyBuildHelpTest(unittest.TestCase):
    def test_help_is_complete_and_exits_zero(self):
        res = subprocess.run(
            ["sh", str(BUILD), "--help"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        self.assertIn("Build, verify and package the OXP3 Status Decky plugin.", res.stdout)
        self.assertIn("./build.sh --offline", res.stdout)
        # The header selector must not spill into the first code line.
        self.assertNotIn("set -eu", res.stdout)


class InstallerUsageTest(unittest.TestCase):
    def test_unknown_option_is_a_usage_error(self):
        res = subprocess.run(
            ["bash", str(INSTALLER), "--not-an-option"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("unknown option", res.stderr)
        self.assertNotIn("Traceback", res.stderr)


class VerifyStatusConsumerTest(unittest.TestCase):
    """Run the real ensure_status_json() body against stubbed outputs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oxp3-cli-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        tool_dir = self.root / "tools"
        tool_dir.mkdir()
        status = tool_dir / "oxp3-status.sh"
        status.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        status.chmod(0o755)
        self.body = _function_body(VERIFY, "ensure_status_json")

    def run_scenario(self, rc: int, out: str) -> subprocess.CompletedProcess:
        script = textwrap.dedent(
            """\
            set -u
            REPO={repo}
            STATUS_JSON=""
            STATUS_READY=0
            STUB_RC={rc}
            STUB_OUT={out}
            have() {{ return 0; }}
            run_tool() {{ RUN_RC=$STUB_RC; RUN_OUT=$STUB_OUT; }}
            json_ok() {{
                [ -n "$1" ] || return 1
                printf '%s' "$1" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null
            }}
            {body}
            if ensure_status_json; then first=0; else first=1; fi
            if ensure_status_json; then second=0; else second=1; fi
            printf 'first=%s second=%s json=%s\\n' "$first" "$second" "${{STATUS_JSON:-}}"
            """
        ).format(
            repo=str(self.root),
            rc=rc,
            out=shlex.quote(out),
            body=self.body,
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_exit_one_with_json_is_a_verdict_not_a_dead_backend(self):
        res = self.run_scenario(1, '{"overall": "fail", "counts": {"fail": 1}}')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("first=0 second=0", res.stdout)
        self.assertIn('"overall": "fail"', res.stdout)

    def test_exit_zero_with_json_is_available(self):
        res = self.run_scenario(0, '{"overall": "warn"}')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("first=0 second=0", res.stdout)

    def test_failed_first_attempt_is_not_cached_as_success(self):
        res = self.run_scenario(3, "")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("first=1 second=1", res.stdout)

    def test_exit_one_without_json_stays_unavailable(self):
        res = self.run_scenario(1, "not json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("first=1 second=1", res.stdout)

    def test_no_cache_guard_returns_success_on_an_empty_report(self):
        # Static companion to the behavioural test above: the old one-liner
        # `[ -n "$STATUS_JSON" ]; return 0` is what made the second call lie.
        code = re.sub(r"^\s*#.*$", "", self.body, flags=re.M)
        self.assertNotIn('[ -n "$STATUS_JSON" ]; return 0', code)
        self.assertIn('[ -n "$STATUS_JSON" ] || return 1', code)
        self.assertRegex(code, r"case \$RUN_RC in\s+0\|1\)")


if __name__ == "__main__":
    unittest.main()
