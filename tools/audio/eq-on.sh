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
if ! pactl set-default-sink "$id" 2>/dev/null; then
  # PipeWire can answer "Not supported" for a filter-chain sink; set the default
  # through wireplumber's metadata instead.
  pw-metadata -n default 0 "default.audio.sink" "{\"name\":\"oxp3_speaker_eq.sink\"}" >/dev/null 2>&1 || true
fi
cur=$(pactl get-default-sink 2>/dev/null || true)
if [ "$cur" != "oxp3_speaker_eq.sink" ]; then
  echo "warning: default sink is '${cur:-?}', not the EQ sink" >&2
  echo "  set it manually: pw-metadata -n default 0 default.audio.sink '{\"name\":\"oxp3_speaker_eq.sink\"}'" >&2
else
  echo "speaker EQ on (default sink = oxp3_speaker_eq.sink)"
fi
pactl list sinks short | grep -E 'oxp3_speaker_eq|HiFi__Speaker__sink' || true
