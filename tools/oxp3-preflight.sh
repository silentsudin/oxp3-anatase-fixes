#!/bin/bash
# SPDX-License-Identifier: MIT
# oxp3-preflight.sh - refuse a half-install: check every prerequisite the
# OneXPlayer 3 installers in this repository rely on, before anything is written.
#
#   tools/oxp3-preflight.sh                 human report, OK/WARN/FAIL per check
#   tools/oxp3-preflight.sh --json          machine-readable report (schema 1)
#   tools/oxp3-preflight.sh --quiet         only the problems + the verdict
#   tools/oxp3-preflight.sh --strict        exit 1 while any WARN remains
#   tools/oxp3-preflight.sh --component volume
#                                           one install path (repeatable; a
#                                           comma list also works). Valid:
#                                           volume leds rgb sleep bluetooth
#                                           power imu audio decky
#   tools/oxp3-preflight.sh --user NAME     user whose session/units are checked
#   tools/oxp3-preflight.sh --selftest      parser checks + fixture verdicts
#   tools/oxp3-preflight.sh --fixtures DIR  run a fixture directory
#   tools/oxp3-preflight.sh -h|--help
#
# Read-only by contract: it never writes a file, starts/stops a unit, reloads
# udev, touches sysfs or installs a package. Every privileged remediation is
# printed as a `fix:` command for the operator.
#
# Exit codes: 0 safe, 1 unsafe (or --strict with warnings), 2 usage, 3 engine
# could not start. The installer aborts on 1 unless --force; the verifier maps
# the verdict to its preflight.verdict check.
#
# See docs/PREFLIGHT.md for the check inventory, the verdict logic and the
# remediation table. The probe engine is tools/oxp3_preflight.py.
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
engine="$here/oxp3_preflight.py"

if ! command -v python3 >/dev/null 2>&1; then
    printf 'oxp3-preflight: python3 is required (install python3)\n' >&2
    exit 3
fi
if [ ! -r "$engine" ]; then
    printf 'oxp3-preflight: probe engine missing: %s\n' "$engine" >&2
    exit 3
fi

# Keep the repository free of __pycache__ even when the engine is imported by
# the unit tests rather than executed.
export PYTHONDONTWRITEBYTECODE=1
export LC_ALL=C

exec python3 "$engine" "$@"
