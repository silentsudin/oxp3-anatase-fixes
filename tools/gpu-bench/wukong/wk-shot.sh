#!/bin/bash
# wk-shot.sh <png> : screenshot the headless gamescope started by wk-wrap.sh (Steam launch).
png=${1:?png}
p=$(pgrep -x gamescope-wl | head -1); [[ -n $p ]] || { echo "no gamescope" >&2; exit 1; }
d=$(grep -o "Starting Xwayland on :[0-9]*" "$HOME/.cache/oxp3-winre/wk-last.log" | tail -1 | grep -o "[0-9]*$")
rm -f /proc/$p/root/tmp/gamescope.png
podman exec oxp3-prof bash -c "DISPLAY=:$d xprop -root -f GAMESCOPECTRL_REQUEST_SCREENSHOT 32c -set GAMESCOPECTRL_REQUEST_SCREENSHOT 1" || exit 1
for _ in $(seq 1 20); do [[ -s /proc/$p/root/tmp/gamescope.png ]] && break; sleep 0.3; done
sleep 0.5; cp /proc/$p/root/tmp/gamescope.png "$png" && echo "$png"
