# SPDX-License-Identifier: MIT
"""record_rpm_state must not fail when a component's RPM glob matches nothing.

Regression: the kernel package is named by KMOD_NAME (kmod-oxp3-<kernelver>), not "kmod-oxp3-xe".
Globbing the wrong name matched nothing, the loop ended non-zero, and because the call site is a
plain command under `set -e`, install died silently right after "recorded Mesa 26.3.1" - before the
keep units, XeSS and the extras step. It must copy what exists and return 0.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "oxp3-fixes.sh"


def record_rpm_state_source() -> str:
    text = SCRIPT.read_text()
    m = re.search(r"^record_rpm_state\(\) \{\n.*?\n\}\n", text, re.S | re.M)
    assert m, "record_rpm_state not found in oxp3-fixes.sh"
    return m.group(0)


class RecordRpmStateTest(unittest.TestCase):
    def run_state(self, skip_kernel: int, rpms: dict[str, str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            state = Path(tmp) / "state"
            out.mkdir()
            (state / "rpms").mkdir(parents=True)
            for name in rpms:
                (out / name).write_text("")
            script = (
                "set -e\n"
                "sudo() { \"$@\"; }\n"
                f"SKIP_GAMESCOPE={rpms.get('__skip_gamescope__', 0)} SKIP_MANGOHUD=0 "
                f"SKIP_KERNEL={skip_kernel} KMOD_NAME=kmod-oxp3\n"
                f"OUT={out} MESA_STATE_DIR={state}\n"
                + record_rpm_state_source()
                + "\nrecord_rpm_state\necho COPIED=$(ls \"$MESA_STATE_DIR/rpms\" | wc -l)\n"
            )
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_missing_component_does_not_fail(self):
        # only gamescope exists: mangohud and the kernel module globs miss, and that used to exit 1
        res = self.run_state(0, {"gamescope-1.x86_64.rpm": ""})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("COPIED=1", res.stdout)

    def test_kernel_rpm_is_found_by_its_real_name(self):
        res = self.run_state(0, {"gamescope-1.x86_64.rpm": "",
                                 "mangohud-1.x86_64.rpm": "",
                                 "kmod-oxp3-7.3.4_an02.fc45.x86_64-202601010000.x86_64.rpm": ""})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("COPIED=3", res.stdout)

    def test_skip_kernel_leaves_the_module_out(self):
        res = self.run_state(1, {"mangohud-1.x86_64.rpm": "",
                                 "kmod-oxp3-7.3.4_an02.fc45.x86_64-202601010000.x86_64.rpm": ""})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("COPIED=1", res.stdout)


if __name__ == "__main__":
    unittest.main()
