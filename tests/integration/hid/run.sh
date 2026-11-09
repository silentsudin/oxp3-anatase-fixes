#!/bin/bash
# End-to-end integration test for the OXP3 home button and LED/RGB tools
# against a virtual vendor HID device created through /dev/uhid.
#
# Opt-in: it creates an ephemeral HID device, runs the real tools/daemons and
# needs root (or an ACL on /dev/uhid and /dev/uinput).  Missing access is
# reported as SKIP (exit 77) with the exact command.
#
#   sudo tests/integration/hid/run.sh
#   OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only hid-integration
#   tools/repo-test.sh --integration          # all opt-in suites
#   make test-integration
#
# The harness never runs sudo, never opens a real 1a86:fe00 hidraw node (the
# tools are pinned with --device), always destroys the virtual device and kills
# the daemons it started, and supports --keep for debugging.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$here/test_hid_integration.py" "$@"
