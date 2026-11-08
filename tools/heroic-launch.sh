#!/bin/sh
# Launch Heroic (Flatpak) from a Steam non-Steam shortcut, in Game Mode.
#
# Forwards SteamAppId/SteamGameId (which Steam's own hrun shim does) so UMU can
# tag the game window with STEAM_GAME / GAMESCOPECTRL_BASELAYER_APPID, which is
# what makes gamescope focus it. Keeps WAYLAND_DISPLAY so Heroic's Electron can
# start. Strips surrounding quotes so Steam's literal quoting of Launch Options
# does not turn the heroic:// URL into a non-URL argument.
#
# Special case: Cyberpunk 2077 (GOG appName=1423049311) is launched through Proton
# directly instead of inside Heroic. Heroic's flatpak + umu layer costs ~15 W of
# CPU under Gamemode, which starves the GPU on this 35 W package. Any extra launch
# options after the URL (e.g. -benchmark) are passed on to the game.
#
# Usage: Target = this script, Launch Options = heroic://launch?appName=...&runner=...

url="$1"
case "$url" in
    \"*\") url=${url#\"}; url=${url%\"} ;;
esac

case "$url" in
    *appName=1423049311*)
        shift 2>/dev/null || true
        exec flatpak-spawn --host \
            --env=SteamAppId="${SteamAppId:-}" \
            --env=SteamGameId="${SteamGameId:-}" \
            --env=STEAM_COMPAT_APP_ID="${SteamAppId:-}" \
            --env=STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam" \
            "$HOME/git/linux_scratch/oxp3-anatase-fixes/tools/cp2077-direct.sh" "$@"
        ;;
esac

exec flatpak-spawn --host \
    --env=SteamAppId="${SteamAppId:-}" \
    --env=SteamGameId="${SteamGameId:-}" \
    --env=STEAM_COMPAT_APP_ID="${SteamAppId:-}" \
    flatpak run com.heroicgameslauncher.hgl --no-gui --no-sandbox "$url"
