#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Turn the OneXPlayer 3 speaker EQ on: install the filter-chain drop-in, restart the
# user PipeWire/WirePlumber services once, wait for the sink and make it the default.
# No root.  Reversible with eq-off.sh.
#
#   eq-on.sh            default profile (-4 dB chain headroom) plus the loudness gain
#                       staging: ALSA at maximum (gain-stage.sh) and 130% sink volume
#                       risk at full scale because no limiter exists)
#   eq-on.sh --safe     -8 dB headroom: cannot clip at 0 dBFS
#   eq-on.sh --loud     the default, for symmetry
#   eq-on.sh --help
#
# Both profiles use the same installer and the same revert path (eq-off.sh, plus
# install-user.sh --undo / gain-stage.sh undo for the gain staging).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

profile=loud
while [ "$#" -gt 0 ]; do
    case $1 in
        --safe) profile=safe ;;
        --loud) profile=loud ;;
        -h|--help) usage; exit 0 ;;
        *) echo "eq-on.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

"$here/install-user.sh" "--$profile"
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
# Gain staging for loudness: ALSA is put at maximum by gain-stage.sh (login and
# resume), and the sink runs above 100% so the vendor EQ's -4 dB headroom does not
# leave the speakers quiet.  PipeWire allows up to 150%; 130% is a 6.8 dB lift.
pactl set-sink-volume "$id" 130% 2>/dev/null || true
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
  echo "speaker EQ on (profile: $profile, default sink = oxp3_speaker_eq.sink)"
fi
if [ "$profile" = loud ]; then
  echo "note: loud profile active; no limiter is installed on this PipeWire build." >&2
  echo "      Full-scale content near 142 Hz can clip - use --safe for the no-clip chain." >&2
fi
pactl list sinks short | grep -E 'oxp3_speaker_eq|HiFi__Speaker__sink' || true

# UCM re-runs when PipeWire opens the card, which resets the mixer to its default
# 82/87.  Re-apply the maximum now and once more shortly after, so the loudness
# survives the restart that eq-on itself triggers.
"$(dirname "$0")/gain-stage.sh" apply >/dev/null 2>&1 || true
( sleep 3; "$(dirname "$0")/gain-stage.sh" apply >/dev/null 2>&1 || true ) &
