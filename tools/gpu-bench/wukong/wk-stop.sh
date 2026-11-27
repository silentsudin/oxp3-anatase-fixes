#!/bin/bash
# wk-stop.sh : close the off-screen Wukong instance (its results screen keeps rendering otherwise)
# and wait until Steam's game process is gone.
p=$(pgrep -x gamescope-wl | head -1)
[[ -n $p ]] && kill "$p"
for _ in $(seq 1 45); do ps -eo args | grep -q '^[A-Z]:.*b1' || exit 0; sleep 2; done
echo "Wukong still running" >&2; exit 1
