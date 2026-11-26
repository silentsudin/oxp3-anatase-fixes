#!/bin/bash
# wk-variant.sh <name> [K=V ...] : relaunch Wukong (via Steam) with this environment, warm run + measured run.
S=$(cd "$(dirname "$0")" && pwd)
name=$1; shift; W=$HOME/.cache/oxp3-winre; out=$HOME/.cache/oxp3-winre/wk; mkdir -p $out
p=$(pgrep -x gamescope-wl | head -1); [[ -n $p ]] && kill $p
for _ in $(seq 1 60); do
    ps -eo args | grep -q '^[A-Z]:.*b1' || break; sleep 2
done
sleep 15
{ printf '%s\n' "$@"; echo MANGOHUD=1; echo "MANGOHUD_CONFIG=no_display,log_duration=120,output_folder=$W/mh,toggle_logging=Shift_L+F2"; } > $W/wk-env
steam -applaunch 3132990 >/dev/null 2>&1 &
for _ in $(seq 1 40); do sleep 3; grep -q "Starting Xwayland" $W/wk-last.log 2>/dev/null && break; done
sleep 45
$S/wk-bench.sh $out/$name-warm.png start || exit 1
$S/wk-bench.sh $out/$name.png || exit 1
echo "$out/$name.png"
