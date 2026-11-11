#!/bin/bash
# SPDX-License-Identifier: MIT
# Launch Cyberpunk 2077 through Proton directly, without Heroic/umu in between.
# Use as a Steam non-Steam shortcut so Gamemode launches it with the Steam environment
# but without Heroic's flatpak + umu layers on top (which cost ~15 W of CPU here).
#
# Feature composition (XeSS XMX env, gamescope/HDR, vibrance, MangoHud) lives in
# tools/oxp3-gamewrap.sh and its per-game config; this script only knows the
# Proton and game paths. See docs/LAUNCH.md.
#
# Extra arguments are passed to the game, e.g. add "-benchmark" as a launch option
# to get an unattended benchmark run whose summary lands in
#   .../drive_c/users/steamuser/Documents/CD Projekt Red/Cyberpunk 2077/benchmarkResults/
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
gamewrap="$here/oxp3-gamewrap.sh"

PROTON="${PROTON:-$HOME/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton}"
GAME="${GAME:-$HOME/Games/Heroic/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe}"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export STEAM_COMPAT_DATA_PATH="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077"

if [ -x "$gamewrap" ]; then
    # The wrapper decides everything; OXP3_XMX is still honoured as an explicit
    # override (config xmx="off" remains a hard veto).
    exec "$gamewrap" --app cyberpunk2077 -- "$PROTON" run "$GAME" "$@"
fi

# Backwards compatibility: an older checkout without the wrapper behaves exactly
# as before (OXP3_XMX=1 sources the XMX environment written by xmx-enable.sh).
if [ "${OXP3_XMX:-0}" = 1 ] && [ -f "$HOME/.config/xess-xmx/oxp3-env.sh" ]; then
    # shellcheck disable=SC1090
    . "$HOME/.config/xess-xmx/oxp3-env.sh"
fi
exec "$PROTON" run "$GAME" "$@"
