#!/usr/bin/env bash
# oxp3-speaker-gain - put the OneXPlayer 3 speaker volume control at maximum and
# remember what it was, so the change is reversible.
#
# The ALC245 speaker amp is exposed as a playback volume control with a 0.75 dB
# step.  The shipped UCM leaves it at 82/87 (-3.75 dB), i.e. 3.75 dB of free
# level.  Raising it to 87/87 costs nothing and cannot clip: it only removes a
# static attenuation before the DAC.
#
#   gain-stage.sh apply      set every present speaker control to its maximum;
#                            the previous values are saved once (idempotent)
#   gain-stage.sh undo       restore the saved values and forget the backup
#   gain-stage.sh report     read-only loudness report (level-report.py)
#   gain-stage.sh status     alias of report
#   gain-stage.sh --help
#
# The control is "Front" on UCMs that name it that way, "Speaker" on this codec
# and "Mono" on UCMs with a joined mono control; only the ones that exist are
# touched, and a machine with none exits 0 with a warning (the boot service must
# never fail a session for an absent card).
#
# Environment:
#   OXP3_AUDIO_CARD        force an ALSA card number (default: detect the SOF card)
#   OXP3_SPEAKER_CONTROLS  override the candidate control list
#   OXP3_GAIN_STATE        override the state directory
#   OXP3_GAIN_WAIT         seconds to wait for a control (default 5; 0 = one try)
#   OXP3_AMIXER            amixer binary (default: amixer)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
amixer_bin=${OXP3_AMIXER:-amixer}
candidates=${OXP3_SPEAKER_CONTROLS:-"Front Speaker Mono"}
wait_s=${OXP3_GAIN_WAIT:-5}
case $wait_s in ''|*[!0-9]*) wait_s=5 ;; esac
state_dir=${OXP3_GAIN_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/oxp3/audio-gain}
state_file=$state_dir/controls.tsv

say() { printf 'oxp3-speaker-gain: %s\n' "$*"; }
warn() { printf 'oxp3-speaker-gain: warning: %s\n' "$*" >&2; }

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

detect_card() {
    if [ -n "${OXP3_AUDIO_CARD:-}" ]; then
        printf '%s' "$OXP3_AUDIO_CARD"
        return 0
    fi
    if [ -r /proc/asound/cards ]; then
        local n
        n=$(awk '/sof-hda-dsp|sofhdadsp/ { gsub(/^[[:space:]]+/, ""); print $1; exit }' \
                /proc/asound/cards)
        if [ -n "$n" ]; then
            printf '%s' "$n"
            return 0
        fi
    fi
    printf '0'
}

sget() { "$amixer_bin" -c "${2:-$card}" sget "$1" 2>/dev/null; }
control_max() { sed -n 's/.*Limits: Playback [0-9][0-9]* - \([0-9][0-9]*\).*/\1/p' | head -n 1; }
control_values() { sed -n 's/.*Playback \([0-9][0-9]*\) \[[0-9][0-9]*%\].*/\1/p' | paste -sd, -; }

at_max() { # VALUES MAX
    local v
    for v in ${1//,/ }; do
        [ "$v" = "$2" ] || return 1
    done
    return 0
}

# One scan: "control<TAB>max<TAB>value,value" for every present candidate.
scan() {
    local c out max vals
    for c in $candidates; do
        out=$(sget "$c") || continue
        max=$(printf '%s\n' "$out" | control_max)
        [ -n "$max" ] || continue
        vals=$(printf '%s\n' "$out" | control_values)
        [ -n "$vals" ] || continue
        printf '%s\t%s\t%s\n' "$c" "$max" "$vals"
    done
}

# Wait for the codec to expose a control (the boot unit may start early).
scan_wait() {
    local tries=$(( wait_s * 2 )) rows=""
    while :; do
        rows=$(scan)
        if [ -n "$rows" ] || [ "$tries" -le 0 ]; then
            printf '%s' "$rows"
            return 0
        fi
        tries=$((tries - 1))
        sleep 0.5
    done
}

saved() {
    [ -f "$state_file" ] &&
        awk -F'\t' -v c="$1" '$1 == c {found = 1} END {exit !found}' "$state_file"
}

apply() {
    local rows c max vals found=0 changed=0 failed=0
    rows=$(scan_wait)
    if [ -z "$rows" ]; then
        warn "no Front/Speaker/Mono playback control on ALSA card $card; nothing to do"
        warn "the speaker path may use a different card; set OXP3_AUDIO_CARD"
        return 0
    fi
    mkdir -p "$state_dir"
    # The codec's Capture switch is off after UCM configures the card; PipeWire then
    # presents an unmuted source that records pure zeros ("microphone disabled").
    # Turning the hardware switch on is harmless for playback and never touches the
    # user's PipeWire-level mute.
    local m
    for m in Capture Mic Mic1; do
        if "$amixer_bin" -c "$card" sget "$m" 2>/dev/null | grep -q '\[off\]'; then
            "$amixer_bin" -c "$card" sset "$m" cap >/dev/null 2>&1 \
                && say "$m capture switch unmuted on card $card" \
                || warn "could not unmute $m on card $card"
        fi
    done
    while IFS=$'\t' read -r c max vals; do
        [ -n "$c" ] || continue
        found=$((found + 1))
        if ! saved "$c"; then
            printf '%s\t%s\t%s\n' "$c" "$card" "$vals" >>"$state_file"
        fi
        if at_max "$vals" "$max"; then
            say "$c already at maximum ($max/$max) on card $card"
            continue
        fi
        # amixer accepts "87" (all channels); some controls need "87,87"
        per=$(printf '%s' "$vals" | tr ',' '\n' | sed "s/.*/$max/" | paste -sd, -)
        if ! "$amixer_bin" -c "$card" sset "$c" "$max" >/dev/null 2>&1 &&
           ! "$amixer_bin" -c "$card" sset "$c" "$per" >/dev/null 2>&1; then
            warn "could not set $c to $max on card $card"
            failed=$((failed + 1))
            continue
        fi
        changed=$((changed + 1))
        say "$c: $vals -> $max/$max on card $card"
    done <<<"$rows"
    if [ "$changed" -eq 0 ] && [ "$failed" -eq 0 ]; then
        say "speaker gain already at maximum ($found control(s))"
    elif [ "$failed" -gt 0 ]; then
        warn "raised $changed of $found speaker control(s); $failed could not be set"
    else
        say "raised $changed of $found speaker control(s); backup in $state_file"
    fi
    say "revert with: $0 undo"
}

undo() {
    local c card_saved vals n=0
    if [ ! -f "$state_file" ]; then
        say "nothing to undo (no saved speaker gain state)"
        return 0
    fi
    while IFS=$'\t' read -r c card_saved vals; do
        [ -n "$c" ] || continue
        if ! sget "$c" "$card_saved" >/dev/null 2>&1; then
            warn "$c is gone from card $card_saved; leaving it alone"
            continue
        fi
        if "$amixer_bin" -c "$card_saved" sset "$c" "$vals" >/dev/null 2>&1; then
            say "$c restored to $vals on card $card_saved"
        elif "$amixer_bin" -c "$card_saved" sset "$c" "${vals%%,*}" >/dev/null 2>&1; then
            say "$c restored to ${vals%%,*} on card $card_saved"
        else
            warn "could not restore $c on card $card_saved"
            continue
        fi
        n=$((n + 1))
    done <"$state_file"
    rm -f "$state_file"
    rmdir "$state_dir" 2>/dev/null || true
    say "restored $n control(s); speaker gain backup removed"
}

report() {
    if [ -x "$here/level-report.py" ] && command -v python3 >/dev/null 2>&1; then
        exec python3 "$here/level-report.py" "$@"
    fi
    warn "python3/level-report.py not available; raw controls:"
    scan
}

action=${1:-}
case $action in
    -h|--help|help) usage; exit 0 ;;
    apply|undo|sink|report|status) ;;
    "") usage; exit 2 ;;
    *) warn "unknown action '$1' (apply|undo|sink|report|status|--help)"; exit 2 ;;
esac
shift

if ! command -v "$amixer_bin" >/dev/null 2>&1; then
    warn "$amixer_bin not found; install alsa-utils or set OXP3_AMIXER"
    case $action in report|status) report "$@" ;; *) exit 0 ;; esac
fi
card=$(detect_card)
case $action in
    apply) apply ;;
    sink)  # one-shot loudness staging; NEVER called by the periodic service, so a
           # volume change in Gamemode stays where the user put it
           if command -v pactl >/dev/null 2>&1; then
               pactl set-sink-volume oxp3_speaker_eq.sink 130% >/dev/null 2>&1 \
                   && echo "oxp3-speaker-gain: sink volume set to 130% (+6.8 dB)" || true
           fi ;;
    undo) undo ;;
    report|status) report "$@" ;;
esac

