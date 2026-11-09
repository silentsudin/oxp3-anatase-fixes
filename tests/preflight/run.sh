#!/usr/bin/env bash
# tests/preflight/run.sh - the fixture runner for tools/oxp3-preflight.sh.
#
#   tests/preflight/run.sh            parser selftest + every fixture verdict
#   tools/repo-test.sh --only preflight
#
# It runs no host probe: --selftest uses FixtureHost (an in-memory system) and
# the fixture directory. Nothing is written outside the repository.
set -u
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
tool="$repo/tools/oxp3-preflight.sh"
fixtures="$repo/tests/fixtures/preflight"

[ -x "$tool" ] || { echo "missing $tool" >&2; exit 1; }
[ -d "$fixtures" ] || { echo "missing $fixtures" >&2; exit 1; }

export PYTHONDONTWRITEBYTECODE=1
rc=0
"$tool" --selftest || rc=1
"$tool" --fixtures "$fixtures" || rc=1
exit "$rc"
