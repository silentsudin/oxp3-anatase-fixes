#!/usr/bin/env bash
# Install the OneXPlayer 3 speaker EQ in the *user* scope.  No root, no host changes:
# everything lands in ~/.config/pipewire and ~/.config/wireplumber.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
confdir=${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d
mkdir -p "$confdir"

sink=$(pactl list sinks short 2>/dev/null | awk '/HiFi__Speaker__sink/ {print $2; exit}')
if [ -z "${sink:-}" ]; then
  echo "warning: no HiFi__Speaker__sink found; keeping the name baked into the config" >&2
  sink="alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink"
fi
echo "hardware speaker sink: $sink"

dst=$confdir/51-oxp3-speaker-eq.conf
sed "s|^\( *node.target *= *\).*|\1\"$sink\"|" "$here/oxp3-speaker-eq.conf" > "$dst"
echo "installed $dst"

echo
echo "next:  systemctl --user restart pipewire wireplumber   (or run eq-on.sh, which does it)"
