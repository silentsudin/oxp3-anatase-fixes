# Recomp Launcher games: desktop scaling and the gamescope wrapper

## Symptom

Games started from **RecompLauncher** use well under the Arc B390's capacity and do not reach the
expected frame rate, while the same class of game from Steam or Heroic does. Both native Linux
recomps and Windows/Proton recomps are affected.

## Root cause (measured on 2026-11-15)

The Plasma desktop runs the OXP3 panel at **1920x1200 with KWin scale 1.7**. That makes the
*logical* desktop **1129x706**:

```text
$ kscreen-doctor -o | grep -E 'Geometry|Scale'
    Geometry: 0,0 1130x706
    Scale: 1.7

$ DISPLAY=:0 xdpyinfo | grep dimensions
    dimensions:    1129x706 pixels

$ DISPLAY=:0 (RandR) eDP-1 mode
    1129x706 @ 143.83 Hz
```

RecompLauncher launches games directly into that desktop session. They therefore see a
**1129x706** display, not the panel's 1920x1200:

- XWayland games: `_NET_WM_STATE_FULLSCREEN` window is 1129x706; the game's own overlay reported
  `Window 1129x706`, `Game image 1920x1080` (MKW) / `window 1129x706` (PLA's Wayland log).
- Native Wayland games: `larecomp.log` reports `fullscreen 1: asked (wayland)` and then
  `[launcher] window 1129x706: drawn at 0.88x, fonts at 1.50x`.

The game renders a small logical surface and **KWin upscales it to 1920x1200**, so the GPU has
little work to do and frame delivery is tied to the desktop compositor. A game whose own config
caps at 60 fps (e.g. `mkw.ini` has `fps = 60`) still shows 60 fps; a game configured for 144
(`plarecomp`: `fps = 144`, `resolution = match`) starts at 1129x706 because it inherits the scaled
desktop.

Steam's Gamemode path already runs games under **gamescope at 1920x1200@144**, which is why those
games are not affected by the desktop scale.

## Fix: run RecompLauncher inside gamescope

`tools/recomp-gamescope.sh` starts RecompLauncher inside a gamescope session with the panel's native
mode. Games launched from that launcher — native and Proton — inherit
`GAMESCOPE_WAYLAND_DISPLAY`/`ENABLE_GAMESCOPE_WSI` and get the full 1920x1200 display:

```sh
tools/recomp-gamescope.sh
# or:
RECOMP_APPIMAGE="$HOME/Applications/RecompLauncher.AppImage" tools/recomp-gamescope.sh
# different mode:
tools/recomp-gamescope.sh --width 1920 --height 1200 --refresh 144 -- --desktop
```

Verified with the real launcher and Mario Kart World: the game window becomes **1920x1200** inside
gamescope instead of 1129x706, with the overlay showing `Window 1920x1200`. The game's own frame cap
still applies; MKW is a 60 fps title (`fps = 60` in `mkw.ini`), so set per-game `fps`/`fps_limit`
only for games that support higher rates.

To make this the normal launcher entry, point the desktop entry at the wrapper instead of the
AppImage, e.g.:

```ini
[Desktop Entry]
Name=RecompLauncher (gamescope)
Exec=/path/to/oxp3-anatase-fixes/tools/recomp-gamescope.sh -- --desktop
Type=Application
```

## Why not just change the desktop scale?

Changing KWin to 100% fixes the logical resolution but makes the whole desktop too small and does
not help native Wayland clients that deliberately use the logical size. `gamescope` gives the game a
compositor of its own at native resolution and refresh, matching Steam's Gamemode path.
