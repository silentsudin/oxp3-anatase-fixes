#!/usr/bin/env bash
# OXP3-ONEXPLAYER3-MIC
#
# internal-mic.sh - expose the OneXPlayer 3 internal microphone as an
# always-available PipeWire/UCM capture source, reversibly.
#
# What it writes (nothing else, ever):
#
#   <ucm2>/conf.d/sof-hda-dsp/<CardLongName>.conf   machine card profile: includes
#       the shipped sof-hda-dsp.conf, then our card-level override
#   <ucm2>/oxp3/oxp3-onEXPLAYER3-usecase.conf       card-level override: points the
#       HiFi use case at the wrapper below (merged after the shipped profile)
#   <ucm2>/oxp3/oxp3-onEXPLAYER3-HiFi.conf          use-case wrapper: includes the
#       shipped HiFi use case and then our microphone override
#   <ucm2>/oxp3/oxp3-onEXPLAYER3-mic.conf           microphone override: re-declares
#       only SectionDevice."Mic" with the codec's fixed 'Internal Mic Phantom Jack'
#       instead of the external 'Mic Jack'
#
# <ucm2> is /usr/share/alsa/ucm2 (override with --ucm2 for a scratch prefix).
# No shipped file is modified, so speaker/headphone/HDMI routing and the SOF
# EQ/DRC byte controls keep coming from the distribution's own profile.  The
# override files are separate includes on purpose: the UCM include pass merges
# with snd_config_merge(parent, included, 0), which substitutes duplicate scalar
# values, so only a value merged *after* the shipped one wins.
#
# Undo deletes exactly these files plus the manifest; files that do not carry our
# marker are never removed.
#
# Why this is not part of tools/oxp3-hw-install.sh --audio: it overrides the
# card's UCM profile, while the speaker EQ is a user-scope PipeWire drop-in.  One
# opt-in command keeps the two independent; see docs/hw/audio-microphone.md.
#
# Exit codes: 0 = done / already in that state, 1 = refused or failed,
# 2 = usage error.

set -u

PROG=internal-mic
VERSION=1.0.0
MARKER='OXP3-ONEXPLAYER3-MIC'

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
src_dir=$repo/files/ucm2

UCM2=/usr/share/alsa/ucm2
STATE=${OXP3_STATE_DIR:-/var/lib/oxp3}
MANIFEST=$STATE/audio-mic.manifest
DRY=0
ACTION=""
FORCE=0
ACTION_SET=0
SCRATCH=0
UCM2_SET=0
STATE_SET=0

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }
fail() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
is_root() { [ "$(id -u)" -eq 0 ]; }

usage() {
    cat <<'EOF'
internal-mic.sh - OneXPlayer 3 internal microphone exposure (opt-in, reversible)

  sudo tools/audio/internal-mic.sh install    add the four UCM2 override files
  sudo tools/audio/internal-mic.sh undo       remove exactly those files
       tools/audio/internal-mic.sh status     read-only state report
       tools/audio/internal-mic.sh --dry-run  print every write, change nothing
  tools/audio/internal-mic.sh --help

It installs a machine UCM2 card profile (conf.d/sof-hda-dsp/<CardLongName>.conf)
plus three files under /usr/share/alsa/ucm2/oxp3/ that include the shipped
sof-hda-dsp profile and the shipped HiFi use case, and then re-declare only
SectionDevice."Mic" with the codec's fixed 'Internal Mic Phantom Jack' instead of
the external 'Mic Jack'.  That makes the internal ALC245 digital microphone
(pin 0x12) an always-available capture source instead of one PipeWire marks
"not available" while nothing is plugged in.

The overrides are separate include files because the UCM include pass substitutes
duplicate scalar values from the later include; a value declared before the
include is lost.  No shipped file is modified, so speaker/headphone/HDMI routing
keeps coming from the distribution's profile.

Undo removes the four files (only when they carry our marker) and the manifest.
See docs/hw/audio-microphone.md for the evidence and for how to verify with
tools/oxp3-verify.sh (aud.mic) and tools/audio/mic_diag.py.

--ucm2 DIR   install into a scratch UCM tree instead of /usr/share/alsa/ucm2
--state DIR  directory for the undo manifest (default /var/lib/oxp3)
--force      override the card-identity checks (read the doc first)
             --ucm2 and --state together also allow a scratch-prefix install
             without root (used by tests)

Exit codes: 0 done/already, 1 refused or failed, 2 usage error.
EOF
}

while [ "$#" -gt 0 ]; do
    case $1 in
        install|undo|status) ACTION=$1; ACTION_SET=1 ;;
        --dry-run|-n) DRY=1 ;;
        --force) FORCE=1 ;;
        --ucm2) shift; [ "$#" -gt 0 ] || die "--ucm2 needs a directory"; UCM2=$1; UCM2_SET=1 ;;
        --state) shift; [ "$#" -gt 0 ] || die "--state needs a directory"; STATE=$1; MANIFEST=$STATE/audio-mic.manifest; STATE_SET=1 ;;
        --help|-h) usage; exit 0 ;;
        --version) printf '%s %s\n' "$PROG" "$VERSION"; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done
[ "$ACTION_SET" -eq 1 ] || ACTION=status
# `--dry-run` on its own asks "what would install do?"; it never needs root.
[ "$DRY" -eq 1 ] && [ "$ACTION" = status ] && ACTION=install
[ "$UCM2_SET" -eq 1 ] && [ "$STATE_SET" -eq 1 ] && SCRATCH=1

# ---------------------------------------------------------------- card lookup
card_driver=""
card_longname=""
card_index=""

json_field() { # JSON-REPORT FIELD
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    c = json.load(sys.stdin).get("card") or {}
except Exception:
    sys.exit(1)
v = c.get(sys.argv[1])
print("" if v is None else v)' "$2" 2>/dev/null
}

lookup_card() {
    local json=""
    if [ -x "$repo/tools/audio/mic_diag.py" ] && command -v python3 >/dev/null 2>&1; then
        json=$("$repo/tools/audio/mic_diag.py" --json 2>/dev/null) || json=""
    fi
    if [ -n "$json" ]; then
        card_driver=$(json_field "$json" driver) || card_driver=""
        card_longname=$(json_field "$json" longname) || card_longname=""
        card_index=$(json_field "$json" index) || card_index=""
    fi
    if [ -z "$card_driver" ] && [ -r /proc/asound/cards ]; then
        local line next
        while IFS= read -r line; do
            IFS= read -r next || next=""
            case $line in
                *"]:"*)
                    card_driver=$(printf '%s' "$line" | sed -n 's/^ *[0-9][0-9]* *\[[^]]*\]: *\([^ ]*\) *-.*/\1/p')
                    card_index=$(printf '%s' "$line" | sed -n 's/^ *\([0-9][0-9]*\) *\[.*/\1/p')
                    card_longname=$(printf '%s' "$next" | sed 's/^ *//; s/ *$//')
                    break
                    ;;
            esac
        done < /proc/asound/cards
    fi
    [ -n "$card_driver" ] && [ -n "$card_longname" ]
}

target_card() { printf '%s/conf.d/%s/%s.conf' "$UCM2" "$card_driver" "$card_longname"; }
target_usecase() { printf '%s/oxp3/oxp3-onEXPLAYER3-usecase.conf' "$UCM2"; }
target_hifi() { printf '%s/oxp3/oxp3-onEXPLAYER3-HiFi.conf' "$UCM2"; }
target_mic() { printf '%s/oxp3/oxp3-onEXPLAYER3-mic.conf' "$UCM2"; }

is_ours() { [ -f "$1" ] && grep -q "$MARKER" "$1" 2>/dev/null; }

write_manifest() { # triples: PATH SHA EXISTED ...
    mkdir -p -- "$STATE" || return 1
    {
        printf '# %s %s schema 1\n' "$PROG" "$VERSION"
        printf '# path\tsha256\texisted_before\n'
        while [ "$#" -ge 3 ]; do
            printf '%s\t%s\t%s\n' "$1" "$2" "$3"
            shift 3
        done
    } > "$MANIFEST"
}

# -------------------------------------------------------------------- install
do_install() {
    local src_card=$src_dir/oxp3-onEXPLAYER3.conf
    local src_case=$src_dir/oxp3-onEXPLAYER3-usecase.conf
    local src_hifi=$src_dir/oxp3-onEXPLAYER3-HiFi.conf
    local src_mic=$src_dir/oxp3-onEXPLAYER3-mic.conf
    local f
    for f in "$src_card" "$src_case" "$src_hifi" "$src_mic"; do
        [ -r "$f" ] || fail "missing $f (restore files/ucm2/ from git)"
    done
    lookup_card || fail "no ALSA card found in /proc/asound/cards (is the SOF driver loaded?)"

    local tcard tcase thifi tmic realucm
    tcard=$(target_card)
    tcase=$(target_usecase)
    thifi=$(target_hifi)
    tmic=$(target_mic)
    realucm=$(readlink -f -- "$UCM2" 2>/dev/null || printf '%s' "$UCM2")

    if [ "$FORCE" -eq 0 ]; then
        [ "$card_driver" = sof-hda-dsp ] \
            || fail "card driver '$card_driver' is not sof-hda-dsp; this profile only supports the SOF HDA card (read docs/hw/audio-microphone.md, then --force)"
        case $card_longname in
            *ONEXPLAYER3*|*ONE_NETBOOK*) ;;
            *) fail "card '$card_longname' is not an OneXPlayer 3; refusing (see docs/hw/audio-microphone.md)" ;;
        esac
        [ -r "$UCM2/conf.d/$card_driver/sof-hda-dsp.conf" ] \
            || fail "shipped $UCM2/conf.d/$card_driver/sof-hda-dsp.conf is missing; the machine profile would have nothing to include"
        [ -r "$UCM2/Intel/sof-hda-dsp/HiFi.conf" ] \
            || fail "shipped $UCM2/Intel/sof-hda-dsp/HiFi.conf is missing; the wrapper would have no playback devices"
    fi

    local -a srcs=("$src_card" "$src_case" "$src_hifi" "$src_mic")
    local -a dsts=("$tcard" "$tcase" "$thifi" "$tmic")
    local i
    for i in "${!dsts[@]}"; do
        if [ -f "${dsts[$i]}" ] && ! is_ours "${dsts[$i]}"; then
            fail "${dsts[$i]} exists and is not ours; refusing to overwrite (move it away first)"
        fi
    done

    if [ "$DRY" -eq 1 ]; then
        note "dry run: card ${card_index:-?} $card_driver '$card_longname'"
        for i in "${!srcs[@]}"; do
            note "would install ${dsts[$i]}   (from ${srcs[$i]#$repo/})"
        done
        note "would write   $MANIFEST"
        note "no file written (--dry-run)"
        return 0
    fi
    [ "$SCRATCH" -eq 1 ] || is_root || fail "install writes under $UCM2; run it with sudo"

    local -a existed=() backups=()
    mkdir -p -- "$(dirname -- "$tcard")" "$(dirname -- "$tcase")" "$STATE" \
        || fail "cannot create target directories"
    for i in "${!dsts[@]}"; do
        if [ -f "${dsts[$i]}" ]; then
            backups[$i]=${dsts[$i]}.oxp3-bak.$(date +%Y%m%d%H%M%S)
            cp -p -- "${dsts[$i]}" "${backups[$i]}" || fail "cannot back up ${dsts[$i]}"
            existed[$i]=yes
        else
            backups[$i]=""
            existed[$i]=no
        fi
    done
    for i in "${!srcs[@]}"; do
        install -m 0644 -- "${srcs[$i]}" "${dsts[$i]}" || fail "cannot write ${dsts[$i]}"
    done

    if command -v alsaucm >/dev/null 2>&1; then
        local rc=0
        ALSA_CONFIG_UCM2=$realucm alsaucm -c "hw:${card_index:-0}" list _verbs >/dev/null 2>&1 || rc=$?
        if [ "$rc" -ne 0 ]; then
            for i in "${!dsts[@]}"; do
                rm -f -- "${dsts[$i]}"
                [ -n "${backups[$i]}" ] && mv -f -- "${backups[$i]}" "${dsts[$i]}"
            done
            fail "the UCM parser rejected the new profile (alsaucm rc=$rc); files rolled back"
        fi
    else
        note "warning: alsaucm is not installed, so the UCM syntax was not validated."
        note "         Undo is one command: sudo $0 undo"
    fi

    local -a manifest_args=()
    for i in "${!dsts[@]}"; do
        manifest_args+=("${dsts[$i]}" "$(sha256sum "${dsts[$i]}" | cut -d' ' -f1)" "${existed[$i]}")
    done
    write_manifest "${manifest_args[@]}" || fail "cannot write $MANIFEST"
    note "installed:"
    for i in "${!dsts[@]}"; do note "  ${dsts[$i]}"; done
    note "  $MANIFEST"
    note ""
    note "next (as the desktop user, not root):"
    note "  systemctl --user restart pipewire wireplumber"
    note "  tools/oxp3-verify.sh            # aud.mic should be PASS"
    note "  tools/audio/mic_diag.py         # verdict: exposed"
    note "then record a few seconds and listen:"
    note "  pw-record --rate 48000 --channels 1 /tmp/oxp3-mic.wav"
    return 0
}

# ----------------------------------------------------------------------- undo
do_undo() {
    local tcard="" line path removed=0 kept=0
    local -a candidates=()
    lookup_card || true
    if [ -n "$card_driver" ] && [ -n "$card_longname" ]; then
        tcard=$(target_card)
        candidates+=("$tcard")
    fi
    candidates+=("$(target_usecase)" "$(target_hifi)" "$(target_mic)")
    if [ -f "$MANIFEST" ]; then
        while IFS=$'\t' read -r path _ _; do
            case $path in \#*|"") continue ;; esac
            candidates+=("$path")
        done < "$MANIFEST"
    fi

    if [ "$DRY" -eq 1 ]; then
        for path in "${candidates[@]}"; do
            note "would remove $path (only if it carries the marker)"
        done
        note "would remove $MANIFEST"
        note "dry run: nothing removed"
        return 0
    fi
    [ "$SCRATCH" -eq 1 ] || is_root || fail "undo removes files under $UCM2; run it with sudo"

    for path in "${candidates[@]}"; do
        [ -f "$path" ] || continue
        if is_ours "$path"; then
            rm -f -- "$path" && removed=$((removed + 1))
            note "removed $path"
        else
            kept=$((kept + 1))
            note "kept    $path (not ours: marker '$MARKER' missing)"
        fi
    done
    # the oxp3/ directory is ours alone; remove it only when empty
    rmdir -- "$UCM2/oxp3" 2>/dev/null || true
    rm -f -- "$MANIFEST" 2>/dev/null || true
    note "removed $removed file(s), kept $kept foreign file(s); manifest cleared"
    note "next: systemctl --user restart pipewire wireplumber"
    return 0
}

# --------------------------------------------------------------------- status
do_status() {
    lookup_card || { note "no ALSA card in /proc/asound/cards"; return 1; }
    local tcard tcase thifi tmic installed=1
    tcard=$(target_card)
    tcase=$(target_usecase)
    thifi=$(target_hifi)
    tmic=$(target_mic)
    note "card:         $card_index: $card_driver '$card_longname'"
    note "ucm2:         $UCM2"
    note "machine conf: $tcard $([ -f "$tcard" ] && echo '[present]' || echo '[absent]')"
    note "card override:$([ -f "$tcase" ] && echo ' [present]' || echo ' [absent]') $tcase"
    note "use case:     $thifi $([ -f "$thifi" ] && echo '[present]' || echo '[absent]')"
    note "mic override: $tmic $([ -f "$tmic" ] && echo '[present]' || echo '[absent]')"
    [ -f "$MANIFEST" ] && note "manifest:     $MANIFEST"
    local f
    for f in "$tcard" "$tcase" "$thifi" "$tmic"; do
        is_ours "$f" || installed=0
    done
    if [ "$installed" -eq 1 ]; then
        note "state:        installed (undo: sudo $0 undo)"
    else
        note "state:        not installed (install: sudo $0 install)"
    fi
    if [ -r "$UCM2/HDA/HiFi-mic.conf" ] && grep -q 'DeviceMicJack "${var:MicJackControl}"' "$UCM2/HDA/HiFi-mic.conf" 2>/dev/null; then
        note "shipped mic:  gated on the external 'Mic Jack' (this is what the override changes)"
    else
        note "shipped mic:  no longer looks jack-gated; re-check docs/hw/audio-microphone.md"
    fi
    if [ -x "$repo/tools/audio/mic_diag.py" ] && command -v python3 >/dev/null 2>&1; then
        note ""
        note "diagnosis:"
        "$repo/tools/audio/mic_diag.py" 2>/dev/null | sed 's/^/  /' || true
    fi
    return 0
}

case $ACTION in
    install) do_install ;;
    undo) do_undo ;;
    status) do_status ;;
    *) usage >&2; die "unknown action: $ACTION" ;;
esac
