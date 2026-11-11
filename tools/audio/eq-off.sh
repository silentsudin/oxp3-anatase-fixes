#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Turn the OneXPlayer 3 speaker EQ off: drop the filter-chain config, restart the
# user audio services and put the hardware Speaker sink back as default.
# No root.  This is also the full rollback (nothing outside ~/.config is touched).
set -euo pipefail
confdir=${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d
rm -f "$confdir/51-oxp3-speaker-eq.conf"
# The ALSA gain staging is a separate, independently reversible change; it is
# deliberately not touched here.  To put the mixer back as well:
#   tools/audio/gain-stage.sh undo
# To remove the boot unit and the applier too:
#   tools/audio/install-user.sh --undo
systemctl --user restart pipewire wireplumber

for _ in $(seq 1 40); do
  if pactl list sinks short 2>/dev/null | grep -q 'HiFi__Speaker__sink'; then break; fi
  sleep 0.25
done
id=$(pactl list sinks short 2>/dev/null | awk '/HiFi__Speaker__sink/ {print $1; exit}' || true)
if [ -n "${id:-}" ]; then
  pactl set-default-sink "$id"
  echo "speaker EQ off; default sink = hardware speaker ($id)"
else
  echo "speaker EQ off; no hardware speaker sink found (headphones/HDMI profile?)" >&2
fi
