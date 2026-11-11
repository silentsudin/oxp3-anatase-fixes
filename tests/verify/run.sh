#!/bin/sh
# SPDX-License-Identifier: MIT
# Offline unit tests for tools/oxp3-verify.sh: every parser against our own
# synthetic fixtures in tests/verify/fixtures. No hardware, no root, no network.
#
#   tests/verify/run.sh          # or: tools/oxp3-verify.sh --selftest
#
# Exit 0 = all assertions passed, 1 = a fixture assertion failed, 3 = fixtures
# missing. The harness's own default mode is the integration test; this runner
# only proves the parsing/exit-code logic in isolation.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec "$repo/tools/oxp3-verify.sh" --selftest
