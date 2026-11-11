#!/bin/sh
# SPDX-License-Identifier: MIT
# oxp3-firstrun - one-shot first-run checklist and redacted evidence collector.
#
# Run this once after the post-install reboot. Without arguments it prints the
# ordered install checklist with the live state of every step; with --collect it
# also writes a redacted evidence bundle that can be handed back as-is.
#
#   tools/oxp3-firstrun.sh                      checklist only (read-only)
#   tools/oxp3-firstrun.sh --check-only         same, explicit
#   tools/oxp3-firstrun.sh --collect            checklist + bundle (see --out)
#   tools/oxp3-firstrun.sh --collect --dry-run  plan the bundle, write nothing
#   tools/oxp3-firstrun.sh --collect --json     machine summary on stdout
#   tools/oxp3-firstrun.sh --collect --no-journal
#   tools/oxp3-firstrun.sh --collect --out DIR
#   tools/oxp3-firstrun.sh --selftest           offline self-test (fixtures)
#
# The tool is read-only: it never writes outside its bundle directory, never
# starts/stops a unit, never suspends, and never asks for root. Where a probe
# needs root (or a journal the user cannot read) the step is reported as
# degraded instead of failing the run. Documented privileged commands are only
# printed, never executed. Exit codes: 0 ok, 1 collection failure, 2 usage,
# 3 the backend could not start.
#
# Docs: docs/FIRSTRUN.md
set -u

# Resolve the repository root even when this script is invoked through a symlink
# from ~/.local/bin or similar.
self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 3
backend=$script_dir/oxp3_firstrun.py

if [ ! -f "$backend" ]; then
    echo "oxp3-firstrun: backend not found: $backend" >&2
    exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "oxp3-firstrun: python3 not found in PATH" >&2
    exit 3
fi

exec python3 "$backend" "$@"
