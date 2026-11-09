#!/bin/bash
# End-to-end integration test for tools/oxp3-voltap against virtual uinput
# devices.  Opt-in: it creates ephemeral devices and runs the real daemon.
#
#   tests/integration/volume/run.sh
#   OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only voltap-integration
#   make test-integration
#
# Requirements: python3-evdev, write access to /dev/uinput and read access to
# the event nodes of the devices it creates (root, or input group / logind
# uaccess ACL).  Missing access is reported as SKIP (exit 77) with the exact
# command; the test never runs sudo, never touches a real input device and
# leaves no virtual device or daemon behind.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$here/test_voltap_integration.py" "$@"
