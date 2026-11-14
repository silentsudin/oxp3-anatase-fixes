#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install the OneXPlayer 3 speaker chain in the *user* scope.  No root, no host
# changes: everything lands in ~/.config and ~/.local, and --undo puts back
# exactly what was there.
#
#   install-user.sh               default profile (shelf +2.84 dB, includes staging)
#   install-user.sh --safe        safe profile (shelf -8 dB, cannot clip, quieter)
#   install-user.sh --undo        restore the previous drop-in and remove our files
#   install-user.sh --print-profile   print "<profile> <headroom_db>" and exit
#   install-user.sh --harman PATH     use the user's own Harman APO folder/GA18v3.reg
#   install-user.sh                   (interactive) prompts for the Harman path
#   install-user.sh --no-harman       use the recorded reproduction without a prompt
#   install-user.sh --harman-force    accept an unvalidated vendor revision (hash check)
#   install-user.sh --dry-run         verify/extract and print what would be installed
#
# What it installs:
#   ~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf   the chain
#   ~/.local/libexec/oxp3-speaker-gain                            the gain applier
#   ~/.config/systemd/user/oxp3-speaker-gain.service              boot oneshot
# and it applies the ALSA speaker gain immediately (idempotent).
#
# Backup contract (what --undo reverses):
#   * a drop-in that already existed is copied to
#     ~/.local/state/oxp3/audio/51-oxp3-speaker-eq.conf.bak on the first install
#     and put back byte-identical on --undo; a drop-in we created is removed;
#   * the ALSA values are saved by gain-stage.sh apply (one row per control) and
#     restored by gain-stage.sh undo, which --undo also runs.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
confdir=${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d
unitdir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
libexec=$HOME/.local/libexec
statedir=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3/audio
meta=$statedir/install.meta
backup=$statedir/51-oxp3-speaker-eq.conf.bak

profile=loud
action=install
harman_path=${OXP3_HARMAN_PATH:-}
harman_force=0
no_harman=0
dry_run=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --loud) profile=loud ;;
        --safe) profile=safe ;;
        --profile=*) profile=${1#--profile=} ;;
        --profile)
            shift
            [ "$#" -gt 0 ] || { echo "install-user.sh: --profile needs a value" >&2; exit 2; }
            profile=$1 ;;
        --harman) shift; harman_path=${1:-} ;;
        --harman=*) harman_path=${1#--harman=} ;;
        --harman-force) harman_force=1 ;;
        --no-harman) no_harman=1 ;;
        --dry-run) dry_run=1 ;;
        --undo) action=undo ;;
        --print-profile) action=print-profile ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "install-user.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

case $profile in
    loud|safe) ;;
    *) echo "install-user.sh: unknown profile '$profile' (loud|safe)" >&2; exit 2 ;;
esac

headroom_of() {
    case $1 in
        loud) printf '%s' "2.84" ;;
        safe) printf '%s' "-8" ;;
        *) echo "install-user.sh: unknown profile '$1' (loud|safe)" >&2; exit 2 ;;
    esac
}

# The model is the single source of truth when python3 is available; the fixed
# fallback above is kept in sync with eq_response.PROFILES (test_level.py pins
# the model values).
model_headroom() {
    if command -v python3 >/dev/null 2>&1 && [ -x "$here/eq_response.py" ]; then
        python3 "$here/eq_response.py" --profile "$1" --print-headroom 2>/dev/null ||
            headroom_of "$1"
    else
        headroom_of "$1"
    fi
}

if [ "$action" = print-profile ]; then
    gain=$(model_headroom "$profile")
    printf '%s %s\n' "$profile" "$gain"
    exit 0
fi

template=$here/oxp3-speaker-eq.conf
if [ "$action" = install ] && (( ! no_harman )); then
    if [ -z "$harman_path" ] && [ -t 0 ]; then
        default_harman=""
        for d in "$HOME/Downloads/Harmon APO" "$HOME/Downloads/oxp3/Harmon APO" \
                 "$HOME/Downloads/oxp3/harmon" "$HOME/.local/share/oxp3-fixes/harman"; do
            [ -d "$d" ] && { default_harman=$d; break; }
        done
        printf 'Harman APO folder or GA18v3.reg%s: ' "${default_harman:+ [$default_harman]}"
        IFS= read -r answer || answer=""
        if [ -z "$answer" ]; then harman_path=$default_harman; else harman_path=$answer; fi
    fi
    if [ -n "$harman_path" ]; then
        import_args=(--path "$harman_path" --profile "$profile")
        (( harman_force )) && import_args+=(--force)
        if bash "$here/harman-import.sh" "${import_args[@]}"; then
            template=$statedir/harman/oxp3-speaker-eq.generated.conf
        else
            echo "install-user.sh: Harman import failed; refusing to install a different EQ" >&2
            exit 1
        fi
    else
        echo "warning: no Harman APO supplied; using the recorded reproduction in tools/audio/oxp3-speaker-eq.conf" >&2
        echo "  next time: tools/audio/install-user.sh --harman '/path/to/Harmon APO'" >&2
    fi
fi

if [ "$action" = install ] && (( dry_run )); then
    echo "dry-run: would install from $template"
    exit 0
fi

if [ "$action" = undo ]; then
    # 1. the boot unit and the applier
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now oxp3-speaker-gain.service >/dev/null 2>&1 || true
        rm -f "$unitdir/oxp3-speaker-gain.service"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    else
        rm -f "$unitdir/oxp3-speaker-gain.service"
    fi
    rm -f "$libexec/oxp3-speaker-gain"
    # 2. the drop-in: put back the pre-install file, or remove what we created
    dst=$confdir/51-oxp3-speaker-eq.conf
    restored=none
    if [ -f "$meta" ]; then
        if grep -qx 'conf=backup' "$meta" && [ -f "$backup" ]; then
            cp -p "$backup" "$dst"
            restored=previous
        else
            rm -f "$dst"
            restored=removed
        fi
    elif [ -f "$dst" ] && grep -q 'oxp3_speaker_eq' "$dst"; then
        rm -f "$dst"
        restored=removed
    fi
    # 3. the ALSA values, then the state directory
    if [ -x "$here/gain-stage.sh" ]; then
        bash "$here/gain-stage.sh" undo >/dev/null 2>&1 || true
    fi
    rm -f "$backup" "$meta"
    rmdir "$statedir" 2>/dev/null || true
    echo "uninstalled: drop-in $restored, unit and gain applier removed"
    echo "restart PipeWire/WirePlumber and set the hardware speaker sink as default if needed"
    exit 0
fi

gain=$(model_headroom "$profile")
mkdir -p "$confdir" "$unitdir" "$libexec" "$statedir"

sink=$(pactl list sinks short 2>/dev/null | awk '/HiFi__Speaker__sink/ {print $2; exit}' || true)
if [ -z "${sink:-}" ]; then
    echo "warning: no HiFi__Speaker__sink found; keeping the name baked into the config" >&2
    sink="alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink"
fi
echo "hardware speaker sink: $sink"

dst=$confdir/51-oxp3-speaker-eq.conf

# One-time backup of a drop-in that was already there (idempotent re-runs keep it).
if [ ! -f "$meta" ]; then
    if [ -f "$dst" ]; then
        cp -p "$dst" "$backup"
        printf 'conf=backup\nprofile=%s\n' "$profile" >"$meta"
        echo "backed up the existing drop-in to $backup"
    else
        printf 'conf=created\nprofile=%s\n' "$profile" >"$meta"
    fi
else
    sed -i "s/^profile=.*/profile=$profile/" "$meta"
fi

# The template is the loud config; --safe replaces only the headroom Gain.  The
# profile marker keeps the report readable without re-deriving it from the Gain.
tmp=$dst.tmp.$$
{
    printf '# oxp3-profile: %s (headroom %s dB)\n' "$profile" "$gain"
    sed -e "s|^\( *node.target *= *\).*|\1\"$sink\"|" \
        -e "s|\(\"Freq\" = 20000 \"Q\" = 0.7 \"Gain\" = \)-\?[0-9.]*|\1$gain|" \
        "$template"
} >"$tmp"
if ! grep -q "\"Gain\" = $gain" "$tmp"; then
    rm -f "$tmp"
    echo "error: could not set headroom $gain dB in the generated chain" >&2
    exit 1
fi
mv -f "$tmp" "$dst"
echo "installed $dst ($profile profile, headroom $gain dB)"

# Gain applier + boot unit (user scope; harmless when systemd --user is absent).
install -m 0755 "$here/gain-stage.sh" "$libexec/oxp3-speaker-gain"
install -m 0644 "$here/oxp3-speaker-gain.service" "$unitdir/oxp3-speaker-gain.service"
install -m 0755 "$here/oxp3-audio-defaults" "$libexec/oxp3-audio-defaults"
cat > "$unitdir/oxp3-audio-defaults.service" <<'UNIT'
[Unit]
Description=OneXPlayer 3: keep the internal microphone as the default source
After=pipewire.service wireplumber.service

[Service]
# The EQ sink (and therefore its monitor source) can appear after login, and
# WirePlumber then re-elects it as the default source. Poll cheaply: the script
# exits immediately unless the default source is a monitor or empty.
Type=simple
Restart=always
RestartSec=5
ExecStart=/bin/sh -c 'while :; do %h/.local/libexec/oxp3-audio-defaults; sleep 10; done' 

[Install]
WantedBy=default.target
UNIT
echo "installed $libexec/oxp3-speaker-gain and $unitdir/oxp3-speaker-gain.service"
if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user daemon-reload >/dev/null 2>&1 &&
       systemctl --user enable oxp3-speaker-gain.service
    systemctl --user enable oxp3-speaker-gain.service oxp3-audio-defaults.service >/dev/null 2>&1; then
        # A configured source that names the sink itself (from an earlier broken
        # election) pins the monitor forever; drop just that line so WirePlumber
        # recomputes from priority (mic 2000 > monitor 1500).
        wpstate=${XDG_STATE_HOME:-$HOME/.local/state}/wireplumber/default-nodes
        [ -f "$wpstate" ] && sed -i '/^default\.configured\.audio\.source=oxp3_speaker_eq\.sink$/d' "$wpstate" 2>/dev/null || true
        echo "enabled oxp3-speaker-gain.service (runs at login; the resume hook repeats it)"
    else
        echo "warning: could not enable oxp3-speaker-gain.service (no user bus?);" >&2
        echo "  it will be enabled on the next install inside a session" >&2
    fi
fi

# Apply now, not only at the next login.  Idempotent; guarded when the card is absent.
if [ -x "$here/gain-stage.sh" ]; then
    bash "$here/gain-stage.sh" apply || true
fi

echo
echo "next:  systemctl --user restart pipewire wireplumber   (or run eq-on.sh, which does it)"
if [ "$profile" = loud ]; then
    echo "note:  the loud profile has no limiter (none exists on this PipeWire build)."
    echo "       A 0 dBFS 142 Hz tone clips by ~3.7 dB; tools/audio/eq-on.sh --safe"
    echo "       keeps the -8 dB no-clip headroom if that matters."
fi
