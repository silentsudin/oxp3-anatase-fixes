#!/usr/bin/env bash
# Turn the OneXPlayer 3 speaker EQ on: install the filter-chain drop-in, restart the
# user PipeWire/WirePlumber services once, wait for the sink and make it the default.
# No root.  Reversible with eq-off.sh.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
"$here/install-user.sh"
systemctl --user restart pipewire wireplumber

for _ in $(seq 1 40); do
  if pactl list sinks short 2>/dev/null | grep -q 'oxp3_speaker_eq.sink'; then break; fi
  sleep 0.25
done

id=$(pactl list sinks short 2>/dev/null | awk '/oxp3_speaker_eq\.sink/ {print $1; exit}')
if [ -z "${id:-}" ]; then
  echo "EQ sink did not appear - check: journalctl --user -u pipewire -n 50" >&2
  exit 1
fi
pactl set-default-sink "$id"
echo "speaker EQ on (sink $id = oxp3_speaker_eq.sink, default now)"
pactl list sinks short | grep -E 'oxp3_speaker_eq|HiFi__Speaker__sink' || true
