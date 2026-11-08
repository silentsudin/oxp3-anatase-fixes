#!/bin/bash
# Launch Cyberpunk 2077 through Proton directly, without Heroic/umu in between.
# Use as a Steam non-Steam shortcut so Gamemode launches it with the Steam environment
# but without Heroic's flatpak + umu layers on top (which cost ~15 W of CPU here).
#
# Extra arguments are passed to the game, e.g. add "-benchmark" as a launch option
# to get an unattended benchmark run whose summary lands in
#   .../drive_c/users/steamuser/Documents/CD Projekt Red/Cyberpunk 2077/benchmarkResults/
set -eu
PROTON="${PROTON:-$HOME/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton}"
GAME="${GAME:-$HOME/Games/Heroic/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe}"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export STEAM_COMPAT_DATA_PATH="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077"
exec "$PROTON" run "$GAME" "$@"
