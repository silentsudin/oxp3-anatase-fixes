#!/bin/bash
# Steam launch-option wrapper for the Wukong benchmark tool:  /var/home/holzr/.cache/oxp3-winre/wk-wrap.sh %command%
# Runs the game inside a headless 1280x720 gamescope (measurements never touch your screen) with the
# extra environment from ~/.cache/oxp3-winre/wk-env (one K=V per line; written by the A/B scripts).
#
# Steam's launcher (steam-runtime-launch-client --alongside-steam) re-applies Steam's own environment,
# so a plain `gamescope -- %command%` still sent the game to the desktop (DISPLAY=:0). The inner stage
# runs inside gamescope, reads gamescope's DISPLAY/WAYLAND_DISPLAY and injects them right before the
# proton command, after every launcher layer.
if [[ ${1:-} == --inner ]]; then
    shift
    env_file=$HOME/.cache/oxp3-winre/wk-env
    inj=(env "DISPLAY=$DISPLAY" "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}" "GAMESCOPE_WAYLAND_DISPLAY=${GAMESCOPE_WAYLAND_DISPLAY:-}")
    if [[ -r $env_file ]]; then
        while IFS= read -r line; do
            [[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]] && inj+=("$line")
        done < "$env_file"
    fi
    args=(); done=0
    for a in "$@"; do
        if (( ! done )) && [[ $a == */proton ]]; then args+=("${inj[@]}"); done=1; fi
        args+=("$a")
    done
    (( done )) || { echo "wk-wrap: no proton argument found; refusing to run on the desktop" >&2; exit 1; }
    echo "wk-wrap: DISPLAY=$DISPLAY WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} extra=${#inj[@]}" >&2
    exec "${args[@]}"
fi
exec gamescope --backend headless -W 1280 -H 720 -- "$0" --inner "$@" > "$HOME/.cache/oxp3-winre/wk-last.log" 2>&1
