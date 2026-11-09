#!/bin/bash
# Hand the OXP3 RGB zones back to HHD (the default owner) and stop our per-zone
# daemon from re-applying its config over HHD's colours.
#
# HHD addresses all five zones itself (left stick 0x01, right stick 0x02, G-key
# 0x05, top 0x06, connector 0x07).  oxp3-rgbd is only for per-zone custom frames
# on top of that; when it is installed it re-applies its own zones after HHD, so
# a zone listed in ~/.config/oxp3-rgb.toml stops following HHD.
#
# Usage: tools/oxp3-rgb-handback.sh [--undo] [--keep-config]
set -uo pipefail
cfg=${XDG_CONFIG_HOME:-$HOME/.config}/oxp3-rgb.toml
unit=oxp3-rgbd.service
backup_dir=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-fixes/rgb-handback

undo=0 keep=0
for arg in "$@"; do
    case $arg in
        --undo) undo=1 ;;
        --keep-config) keep=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [--undo] [--keep-config]" >&2; exit 2 ;;
    esac
done

if [[ $undo == 1 ]]; then
    if [[ -f $backup_dir/oxp3-rgb.toml ]]; then
        cp -f -- "$backup_dir/oxp3-rgb.toml" "$cfg"
        echo "restored $cfg"
    fi
    systemctl --user enable --now "$unit" 2>/dev/null && echo "re-enabled $unit"
    exit 0
fi

mkdir -p "$backup_dir"
if [[ -f $cfg && $keep == 0 ]]; then
    cp -f -- "$cfg" "$backup_dir/oxp3-rgb.toml"
    echo "backed up $cfg -> $backup_dir/oxp3-rgb.toml"
fi
if systemctl --user is-active --quiet "$unit"; then
    systemctl --user disable --now "$unit"
    echo "stopped and disabled $unit"
else
    systemctl --user disable "$unit" >/dev/null 2>&1 || true
    echo "$unit is not running"
fi
echo
echo "HHD now owns the RGB zones (it addresses left/right stick, G-key, top and connector)."
echo "Configure the effect/colour in HHD's RGB settings, or for example:"
echo "  sudo hhdctl set hhd.settings.rgb=true"
echo "To bring our per-zone daemon back: tools/oxp3-rgb-handback.sh --undo"
