#!/bin/bash
# wk-key.sh <xdotool args...> : run xdotool against the Wukong window in the headless gamescope.
d=$(grep -o "Starting Xwayland on :[0-9]*" "$HOME/.cache/oxp3-winre/wk-last.log" | tail -1 | grep -o "[0-9]*$")
podman exec oxp3-prof bash -c "export DISPLAY=:$d; w=\$(xdotool search --class steam_app_3132990 | head -1); xdotool windowfocus --sync \$w 2>/dev/null; xdotool $*"
