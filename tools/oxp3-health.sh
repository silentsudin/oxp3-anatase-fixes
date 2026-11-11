#!/bin/sh
# SPDX-License-Identifier: MIT
# oxp3-health - one read-only command that answers "can I install / is it
# working / what is left", without the 70-row wall of PASS lines.
#
#   tools/oxp3-health.sh                  short sectioned summary (default, read-only)
#   tools/oxp3-health.sh --full           pass --full to the verify stage (invasive)
#   tools/oxp3-health.sh --json           machine-readable report (schema 1)
#   tools/oxp3-health.sh --quiet          only the one-line verdict
#   tools/oxp3-health.sh --verbose        also print the raw stage reports
#   tools/oxp3-health.sh --no-preflight   skip a stage; also --no-verify,
#                                         --no-firstrun, --no-status
#   tools/oxp3-health.sh --log FILE       write the full log to FILE
#   tools/oxp3-health.sh --selftest       offline aggregation/verdict self-test
#   tools/oxp3-health.sh -h|--help
#
# It composes the existing reporters and duplicates none of their logic:
#
#   oxp3-preflight.sh --json            safe-to-install verdict + FAIL/WARN rows
#   oxp3-verify.sh --json [--full]      the acceptance checks
#   oxp3-firstrun.sh --check-only --json  install step states
#   oxp3-status.sh --json               fix/XMX/scheduler indicators
#
# Sections: READY TO INSTALL (yes/no + blockers), INSTALLED (x/y + missing),
# WORKING/FAILING counts with only the non-PASS rows and their remediation,
# WARNINGS, TOOL PROBLEMS (a missing/crashed/timed-out stage is reported, never
# fatal) and the next 1-3 commands. Full detail goes to
# ${XDG_CACHE_HOME:-/tmp}/oxp3-health/<utc>-<pid>/health.log.
#
# Read-only by contract: the component tools run in their documented read-only
# modes and nothing is cached between runs. A stage that is missing or fails is
# reported and the other stages still run. Exit codes: 0 ok, 1 attention
# (blocker, FAIL row or tool problem), 2 usage, 3 no stage produced a report.
#
# Docs: docs/HEALTH.md
set -u

# Resolve the repository root even when this script is invoked through a symlink
# from ~/.local/bin or similar.
self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 3
backend=$script_dir/oxp3_health.py

if [ ! -f "$backend" ]; then
    echo "oxp3-health: backend not found: $backend" >&2
    exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "oxp3-health: python3 not found in PATH" >&2
    exit 3
fi

exec python3 "$backend" "$@"
