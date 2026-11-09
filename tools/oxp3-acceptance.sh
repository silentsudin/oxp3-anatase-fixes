#!/bin/sh
# oxp3-acceptance - judge the seven goal items from evidence, not memory.
#
#   tools/oxp3-acceptance.sh                 evaluate now, human report (read-only)
#   tools/oxp3-acceptance.sh --json          machine report (schema 1)
#   tools/oxp3-acceptance.sh --verbose       every mapped check and manual step
#   tools/oxp3-acceptance.sh --full          also the full-only suites + gen-docs
#   tools/oxp3-acceptance.sh --save FILE     save the raw evidence bundle
#   tools/oxp3-acceptance.sh --evidence FILE evaluate a saved bundle (no live run)
#   tools/oxp3-acceptance.sh --selftest      offline aggregation tests (fixtures)
#   tools/oxp3-acceptance.sh -h|--help
#
# It reads the component states, implementation paths, docs and verify ids from
# docs/hw/components.yaml (never a copy) and runs the cheapest evidence once:
# oxp3-verify.sh --json, oxp3-health.sh --json --no-verify and the relevant
# repo-test --fast suites.  The verdict vocabulary is closed:
#
#   implemented-and-proven-offline | awaiting-install | awaiting-hardware |
#   blocked | not-a-gap
#
# "implemented-and-proven-offline" is an offline claim only.  The report prints
# the device-check count and a per-item "complete" flag separately; nothing is
# called proven on the device without device evidence.
#
# Read-only by contract: every component tool runs in its documented read-only
# mode.  The only optional write is --save FILE.
#
# Exit codes: 0 all automated evidence green (device/manual work may remain),
# 1 a required check/suite failed or an evidence source is missing, 2 usage,
# 3 no evidence source could run.
#
# Docs: docs/ACCEPTANCE.md
set -u

self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 3
backend=$script_dir/oxp3_acceptance.py

if [ ! -f "$backend" ]; then
    echo "oxp3-acceptance: backend not found: $backend" >&2
    exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "oxp3-acceptance: python3 not found in PATH" >&2
    exit 3
fi

exec python3 "$backend" "$@"
