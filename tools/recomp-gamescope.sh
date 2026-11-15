#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Run RecompLauncher inside a gamescope session at the panel's native resolution.
#
# Why: the desktop is scaled (KWin scale 1.7 on the OXP3), so a game launched
# directly from RecompLauncher sees a 1129x706 logical display (XWayland and
# Wayland alike).  It renders at that smaller size and KWin upscales the result,
# leaving the GPU well below its capability.  Steam/Gamemode games already run
# inside gamescope at 1920x1200; this wrapper gives RecompLauncher the same
# environment.  Games launched from the wrapper inherit GAMESCOPE_WAYLAND_DISPLAY
# and ENABLE_GAMESCOPE_WSI, including Proton games.
#
#   tools/recomp-gamescope.sh                 # launcher at 1920x1200@144
#   RECOMP_APPIMAGE=/path/RecompLauncher.AppImage tools/recomp-gamescope.sh
#   tools/recomp-gamescope.sh --width 1920 --height 1200 --refresh 144 -- --desktop
#   tools/recomp-gamescope.sh --no-vrr
#
# The per-game fps cap still applies: e.g. mkw.ini has fps = 60, so 60 fps is
# correct there.  Set the game's own fps/fps_limit for titles that support more.
set -euo pipefail

app=${RECOMP_APPIMAGE:-$HOME/Applications/RecompLauncher.AppImage}
w=${RECOMP_WIDTH:-1920}
h=${RECOMP_HEIGHT:-1200}
r=${RECOMP_REFRESH:-144}
vrr=1
launcher_args=()

usage() {
    awk 'NR > 1 && /^set -euo pipefail/ { exit } NR > 1 && /^#/ { sub(/^# ?/, ""); print }' "$0"
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --appimage) app=${2:?--appimage needs a path}; shift ;;
        --appimage=*) app=${1#*=} ;;
        --width) w=${2:?--width needs a number}; shift ;;
        --height) h=${2:?--height needs a number}; shift ;;
        --refresh) r=${2:?--refresh needs a number}; shift ;;
        --no-vrr) vrr=0 ;;
        -h|--help) usage 0 ;;
        --) shift; launcher_args=("$@"); break ;;
        *) launcher_args=("$@"); break ;;
    esac
    shift
done

[ -x "$app" ] || { echo "recomp-gamescope: not executable: $app" >&2; echo "  set RECOMP_APPIMAGE=/path/to/RecompLauncher.AppImage" >&2; exit 2; }
command -v gamescope >/dev/null 2>&1 || { echo "recomp-gamescope: gamescope is not installed" >&2; exit 2; }
(( w > 0 && h > 0 && r > 0 )) || { echo "recomp-gamescope: width/height/refresh must be positive" >&2; exit 2; }

gargs=(-W "$w" -H "$h" -r "$r" -f)
(( vrr )) && gargs+=(--adaptive-sync)
echo "recomp-gamescope: ${w}x${h}@${r}${vrr:+ VRR} -> $app" >&2
exec gamescope "${gargs[@]}" -- "$app" "${launcher_args[@]}"
