#!/bin/sh
# SPDX-License-Identifier: MIT
# oxp3-status - headless status report for the OneXPlayer 3 "Anatase" fixes.
#
# The exact same checks as the Decky Quick Access Menu plugin
# (tools/decky-oxp3-status), so a report taken over SSH, in a tty or from a
# support script matches what the panel shows. Read-only: nothing here writes,
# stops, starts or reconfigures anything.
#
#   tools/oxp3-status.sh                 human-readable report
#   tools/oxp3-status.sh --json          machine-readable report
#   tools/oxp3-status.sh --strict        exit 2 when anything is warn/unknown
#   tools/oxp3-status.sh --watch 5       refresh every 5 s (Ctrl-C to stop)
#   tools/oxp3-status.sh --home /var/home/holzr   inspect another user's files
#   tools/oxp3-status.sh --selftest      run every check against synthetic
#                                        /sys, /proc and HOME fixtures
#
# Exit codes: 0 = no failures, 1 = at least one fail, 2 = --strict and something
# is warn/unknown, 3 = the backend could not be started (or --selftest found a
# broken check).
set -u

# Resolve the repository root even when this script is invoked through a
# symlink from ~/.local/bin or similar.
self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 3
backend=$script_dir/decky-oxp3-status/main.py

if [ ! -f "$backend" ]; then
    echo "oxp3-status: backend not found: $backend" >&2
    exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "oxp3-status: python3 not found in PATH" >&2
    exit 3
fi

exec python3 "$backend" "$@"
