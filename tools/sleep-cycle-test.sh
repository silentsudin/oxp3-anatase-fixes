#!/bin/sh
# SPDX-License-Identifier: MIT
# sleep-cycle-test - drive N real s2idle cycles, guarantee a wake, attribute it,
# and turn sleep efficiency into a number.
#
# The question this answers is not "can this machine suspend" (it can) but
# "does it come back every time, who woke it, and what does it cost per hour".
# The earlier tools/sleep-power-measure.sh measures one window; this one runs a
# sequence and reports drain/hour plus the wake source with a verdict.
#
#   tools/sleep-cycle-test.sh --dry-run              plan and live preconditions,
#                                                    arm and write nothing
#   tools/sleep-cycle-test.sh --no-suspend           armed state only, no cycles,
#                                                    no drain claim
#   sudo tools/sleep-cycle-test.sh --cycles 3        the real measurement
#   sudo tools/sleep-cycle-test.sh --cycles 3 --sleep 300   better drain precision
#   tools/sleep-cycle-test.sh --analyse DIR          re-analyse a saved capture
#   tools/sleep-cycle-test.sh --selftest             offline analyser self-test
#
# What it does per cycle: baseline snapshot -> `rtcwake -m mem -s N` (so the
# machine always returns) -> on resume, CLOCK_BOOTTIME minus CLOCK_MONOTONIC
# for the real suspended time, /sys/power/suspend_stats deltas, the
# /sys/class/wakeup event-count deltas and the first kernel wake-source lines
# from this boot's journal -> a settle window that measures the awake cost.
# The report (JSON + text + the raw capture) lands in
# docs/state/sleep-cycles-<utc>/.
#
# Safety: real cycles need root. One typed confirmation covers the batch
# (`--yes` for scripted runs); every cycle prints the command it is about to
# run. Hard limits: 1..10 cycles, 10..3600 s per sleep, and a total runtime
# estimate that must fit --max-runtime. A trap disarms a pending RTC alarm on
# every exit path and says whether the disarm was verified. On AC the drain
# number is refused (or marked invalid with --allow-ac) and the reason is
# printed. --dry-run and --no-suspend are read-only.
#
# Exit codes: 0 GOOD, 1 INVESTIGATE/FAILING, 2 INVALID/NO-MEASUREMENT, 3 usage.
# Docs: docs/SLEEP-TEST.md
set -u

self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 3
backend=$script_dir/oxp3_sleep_cycles.py

if [ ! -f "$backend" ]; then
    echo "sleep-cycle-test: backend not found: $backend" >&2
    exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "sleep-cycle-test: python3 not found in PATH" >&2
    exit 3
fi

exec python3 "$backend" "$@"
