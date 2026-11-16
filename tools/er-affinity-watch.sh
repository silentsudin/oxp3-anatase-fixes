#!/bin/sh
# SPDX-License-Identifier: MIT
# er-affinity-watch.sh - keep every Elden Ring thread on the chosen CPUs.
#
# Elden Ring pins an engine worker (EWP_LOW_Single_Clus1) to a small CPU set,
# which causes stalls on hybrid CPUs.  The Windows community fixes it with an
# affinity mod / Process Lasso; this is the Linux equivalent: reapply the mask to
# all threads of eldenring.exe while it runs.
#
#   tools/er-affinity-watch.sh            # all CPUs 0-13, every second
#   tools/er-affinity-watch.sh 0-9        # P+E only
#   tools/er-affinity-watch.sh --once 0-13
#
# Run as your user while the game is running; no root needed.
set -eu
once=0
if [ "${1:-}" = "--once" ]; then once=1; shift; fi
mask=${1:-0-13}
printf 'er-affinity-watch: mask %s every 1s (Ctrl-C to stop)\n' "$mask" >&2
while :; do
    found=0
    for p in $(pgrep -x eldenring.exe 2>/dev/null); do
        found=1
        if taskset -a -p "$mask" "$p" >/dev/null 2>&1; then
            printf '%s pid %s -> %s\n' "$(date +%H:%M:%S)" "$p" "$mask" >&2
        fi
    done
    [ "$found" = 1 ] || printf '%s waiting for eldenring.exe...\n' "$(date +%H:%M:%S)" >&2
    [ "$once" = 1 ] && exit 0
    sleep 1
done
