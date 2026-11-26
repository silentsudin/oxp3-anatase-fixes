#!/bin/bash
# SPDX-License-Identifier: MIT
# oxp3-install.sh - one entry point for a complete OneXPlayer 3 Anatase install.
#
#   ./oxp3-install.sh            # build, stage kernel modules/firmware, or finish after reboot
#   systemctl reboot             # only when it asks to
#   ./oxp3-install.sh            # second run: overlay stack + hardware fixes + verification
#
# It is idempotent and decides the phase from the live system:
#   - booted deployment lacks kmod-oxp3/oxp3-firmware -> build + first install, then asks for a reboot;
#   - booted deployment has them (after the reboot)   -> overlay Mesa/gamescope/XMX + hardware + verify.
# A later run that rebuilds the kernel modules installs the hardware fixes before asking for the
# reboot, so the reboot finishes it; it asks for one more run only when it also built new overlay
# RPMs (gamescope, MangoHud, Mesa), which can only go in once the new deployment is booted.
# Secure Boot: if the MOK key is not enrolled it queues it (once), asks for the firmware-screen
# enrolment + reboot, and the next run continues.
#
# Options: --dry-run  --status  --skip-build  --skip-hw  --skip-mok  --skip-verify
#          --uninstall  --force  --harman PATH|--harman-force|--no-harman
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
state_dir=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-fixes
state=$state_dir/bootstrapper
cache=${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}
out=$cache/out
mok_dir=${OXP3_MOK_DIR:-$HOME/.local/share/oxp3-fixes/mok}

DRY=0 SKIP_BUILD=0 SKIP_HW=0 SKIP_MOK=0 SKIP_VERIFY=0 FORCE=0 ACTION=install
HARMAN_PATH=${OXP3_HARMAN_PATH:-} NO_HARMAN=0 HARMAN_FORCE=0
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
run() {
    if (( DRY )); then printf '   [dry-run] %s\n' "$*"; else printf '   $ %s\n' "$*"; "$@"; fi
}

usage() {
    awk 'NR > 1 && /^set -uo pipefail/ { exit } NR > 1 && /^#/ { sub(/^# ?/, ""); print }' "$0"
    exit 2
}

while (( $# )); do
    case $1 in
        --dry-run) DRY=1 ;;
        --status) ACTION=status ;;
        --uninstall) ACTION=uninstall ;;
        --skip-build) SKIP_BUILD=1 ;;
        --skip-hw) SKIP_HW=1 ;;
        --skip-mok) SKIP_MOK=1 ;;
        --skip-verify) SKIP_VERIFY=1 ;;
        --force) FORCE=1 ;;
        --harman) [[ $# -ge 2 ]] || { echo "--harman needs a path" >&2; usage; }; HARMAN_PATH=$2; shift ;;
        --harman=*) HARMAN_PATH=${1#*=} ;;
        --no-harman) NO_HARMAN=1 ;;
        --harman-force) HARMAN_FORCE=1 ;;
        -h|--help) awk 'NR > 1 && /^set -uo pipefail/ { exit } NR > 1 && /^#/ { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
    shift
done

# Remember a path supplied on the first (pre-reboot) run; the second run then
# does not prompt again.
if [ -n "$HARMAN_PATH" ] && [ -e "$HARMAN_PATH" ]; then
    mkdir -p "$state_dir"; printf '%s\n' "$(readlink -f -- "$HARMAN_PATH")" >"$state_dir/harman-path" 2>/dev/null || true
fi

[[ $ACTION == install ]] && (( EUID != 0 )) || true

# ---- live-state probes -------------------------------------------------------------
booted_local() { # names of requested local packages in the booted deployment
    rpm-ostree status --booted --json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
for dep in d.get("deployments",[]):
    if dep.get("booted"):
        print("\n".join(dep.get("requested-local-packages",[])))
' 2>/dev/null
}
has_kernel_stack() { local l; l=$(booted_local); [[ $l == *kmod-oxp3* ]]; }
has_firmware() { local l; l=$(booted_local); [[ $l == *oxp3-firmware* ]]; }
staged_deployment() {
    rpm-ostree status --json 2>/dev/null | python3 -c '
import json,sys
print("yes" if any(d.get("staged") for d in json.load(sys.stdin).get("deployments",[])) else "no")' 2>/dev/null
}
secure_boot() { local o; o=$(mokutil --sb-state 2>/dev/null || true); [[ $o == *"SecureBoot enabled"* ]]; }
mok_enrolled() {
    local key=$mok_dir/MOK.der o
    [[ -e $key ]] || return 1
    # Do not pipe mokutil into grep -q: with pipefail the early close turns a
    # successful match into a SIGPIPE failure (this made an enrolled key read "no").
    o=$(mokutil --test-key "$key" 2>/dev/null || true)
    [[ $o == *"already enrolled"* ]]
}
have_artifacts() {
    ls "$out"/gamescope-*.x86_64.rpm >/dev/null 2>&1 &&
    ls "$out"/mesa-vulkan-drivers-*.x86_64.rpm >/dev/null 2>&1 &&
    ls "$out"/mangohud-*.x86_64.rpm >/dev/null 2>&1
}
steam_present() {
    [ -d "$HOME/.local/share/Steam/steamapps" ] || \
        [ -d "$HOME/.var/app/org.anatase.Steam/.local/share/Steam/steamapps" ]
}

show_status() {
    say "state"
    info "booted kernel: $(uname -r)"
    info "kmod-oxp3 in booted deployment: $(has_kernel_stack && echo yes || echo NO)"
    info "oxp3-firmware in booted deployment: $(has_firmware && echo yes || echo NO)"
    info "staged deployment: $(staged_deployment)"
    info "Secure Boot: $(secure_boot && echo enabled || echo disabled); MOK enrolled: $(mok_enrolled && echo yes || echo no)"
    info "build artifacts: $(have_artifacts && echo present || echo missing) ($out)"
    info "Steam library: $(steam_present && echo present || echo missing)"
    local fgl g
    if fgl=$(flatpak info --system --show-location runtime/org.freedesktop.Platform.GL.oxp3/x86_64/26.08 2>/dev/null); then
        g=$(ls "$fgl"/files/lib/libgallium-*.so 2>/dev/null | head -1); g=${g##*/libgallium-}
        info "Flatpak GL driver 26.08 (Flathub apps): Mesa ${g%.so}; this session FLATPAK_GL_DRIVERS=${FLATPAK_GL_DRIVERS:-<unset>}"
    else
        info "Flatpak GL driver 26.08 (Flathub apps): not installed"
    fi
    [[ -x $here/tools/check-working.sh ]] && { $here/tools/check-working.sh 2>/dev/null | tail -n +1 | head -12; }
}

# ---- Harman APO input (user-owned files; no vendor data in the repo) ---------------
harman_default() {
    local d
    for d in "$HOME/Downloads/Harmon APO" "$HOME/Downloads/harmon apo" \
             "$HOME/Downloads/oxp3/Harmon APO" "$HOME/Downloads/oxp3/harmon"; do
        [ -d "$d" ] && { printf '%s' "$d"; return 0; }
    done
    for d in "$HOME"/Downloads/*Harmon* "$HOME"/Downloads/*harman* "$HOME"/Downloads/*APO*.zip; do
        [ -e "$d" ] && { printf '%s' "$d"; return 0; }
    done
    return 1
}
ask_harman() {
    local default answer
    (( NO_HARMAN )) && return 0
    if [ -z "$HARMAN_PATH" ] && [ -f "$state_dir/harman-path" ]; then
        HARMAN_PATH=$(<"$state_dir/harman-path")
    fi
    if [ -n "$HARMAN_PATH" ] && [ -e "$HARMAN_PATH" ]; then return 0; fi
    if (( ! DRY )) && [ -t 0 ] && [ -t 1 ]; then
        default=$(harman_default || true)
        while :; do
            printf '\nPath to the Harman APO files (folder, .zip or GA18v3.reg)%s\n' \
                "${default:+ [$default]}"
            printf 'leave blank to skip the vendor EQ: '
            IFS= read -r answer || answer=""
            [ -z "$answer" ] && answer=$default
            if [ -z "$answer" ]; then NO_HARMAN=1; return 0; fi
            if [ -e "$answer" ]; then
                HARMAN_PATH=$(readlink -f -- "$answer")
                mkdir -p "$state_dir"; printf '%s\n' "$HARMAN_PATH" >"$state_dir/harman-path"
                return 0
            fi
            echo "not found: $answer (try again, or blank to skip)" >&2
        done
    fi
    NO_HARMAN=1
}

# ---- uninstall ---------------------------------------------------------------------
if [[ $ACTION == uninstall ]]; then
    say "uninstall"
    (( DRY )) || { read -r -p "This removes the overlay stack, kernel modules/firmware and hardware fixes. Continue? [y/N] " a; [[ $a == [yY]* ]] || exit 1; }
    run sudo "$here/oxp3-fixes.sh" uninstall
    run sudo "$here/tools/oxp3-hw-install.sh" --uninstall --all
    run "$here/tools/oxp3-rollback.sh" restore
    echo
    info "reboot to return to the stock deployment"
    exit 0
fi

[[ $ACTION == status ]] && { show_status; exit 0; }
if (( EUID == 0 )); then
    die "run this script as your normal user (not with sudo); it calls sudo where needed"
fi
command -v rpm-ostree >/dev/null || die "this is not an rpm-ostree (Anatase) system"

# ---- phase 1: secure boot / MOK ----------------------------------------------------
# Queue the key now and keep going with the build/staging, so the enrolment happens
# during the reboot this install already needs (one reboot, two runs in total).
MOK_NEEDED=0
if (( ! SKIP_MOK )) && secure_boot && ! mok_enrolled; then
    MOK_NEEDED=1
    say "queue the module-signing key (MOK)"
    run "$here/oxp3-fixes.sh" mok-setup || die "mok-setup failed (use --skip-mok to install without kernel modules)"
fi

# ---- phase 2: build + kernel/firmware staging --------------------------------------
if ! has_kernel_stack || ! has_firmware; then
    if [[ $(staged_deployment) == yes ]]; then
        say "a deployment is staged: reboot, then run ./oxp3-install.sh again"
        exit 0
    fi
    say "build (as your user; the Mesa RPMs take the longest)"
    if (( ! SKIP_BUILD )); then
        run "$here/oxp3-fixes.sh" build || die "build failed"
    elif ! have_artifacts; then
        die "no build artifacts in $out and --skip-build was given; run ./oxp3-fixes.sh build"
    fi
    say "first install: stage kernel modules + firmware"
    run sudo OXP3_USER="$(id -un)" "$here/oxp3-fixes.sh" install || die "install failed"
    say "reboot required to boot the patched kernel/firmware"
    if (( MOK_NEEDED )); then
        info "in the blue MOK manager choose 'Enroll MOK' -> the oxp3 key, then continue booting"
    fi
    info "systemctl reboot"
    info "then run ./oxp3-install.sh again (it will finish automatically)"
    exit 0
fi

# Hardware fixes: services and config in /etc and /usr/local. They do not depend on the kernel
# modules or the /usr overlay, so they also go in on a run that stages new modules - a reboot
# then finishes the install instead of needing another run (2026-11-25: the dock recorder
# waited for a second run the user had no reason to make).
hardware_step() {
    (( SKIP_HW )) && return 0
    local hw_args import_args
    say "hardware fixes (volume, LEDs/RGB, suspend, Bluetooth, power, IMU, audio, XMX, Decky)"
    ask_harman
    hw_args=(--all --audio-activate --restart-plugin-loader)
    if [ -n "$HARMAN_PATH" ] && [ -e "$HARMAN_PATH" ]; then
        if (( DRY )); then
            info "[dry-run] import Harman APO: $HARMAN_PATH"
        else
            import_args=(--path "$HARMAN_PATH" --profile loud)
            (( HARMAN_FORCE )) && import_args+=(--force)
            "$here/tools/audio/harman-import.sh" "${import_args[@]}" ||
                die "the Harman APO import failed (hash or format); fix the path, add --harman-force for another vendor revision, or use --no-harman"
        fi
        hw_args+=(--harman "$HARMAN_PATH")
        (( HARMAN_FORCE )) && hw_args+=(--harman-force)
    else
        hw_args+=(--no-harman)
    fi
    run sudo "$here/tools/oxp3-hw-install.sh" "${hw_args[@]}" \
        || warn "hardware installer reported a problem; see the lines above"
}

# Overlay RPMs (gamescope, MangoHud, Mesa) built by this run but not installed yet. They go into
# the /usr overlay, which oxp3-fixes.sh install leaves alone while a deployment is staged.
overlay_pending() {
    local f nevra
    for f in "$out"/gamescope-*.x86_64.rpm "$out"/mangohud-*.x86_64.rpm \
             "$out"/mesa-vulkan-drivers-*.x86_64.rpm; do
        [[ -e $f ]] || continue
        nevra=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}' "$f" 2>/dev/null) || continue
        rpm -q "$nevra" >/dev/null 2>&1 || { printf '%s\n' "${f##*/}"; return 0; }
    done
    return 1
}

# ---- phase 3: overlay stack + hardware + verification ------------------------------
if (( MOK_NEEDED )); then
    say "the MOK key is queued but not enrolled yet"
    info "reboot, enrol it in the blue MOK manager, then run ./oxp3-install.sh again"
    exit 0
fi
if [[ $(staged_deployment) == yes ]]; then
    say "a deployment is staged: reboot, then run ./oxp3-install.sh again"
    exit 0
fi
# Always run the build: every component is cached by a hash of its inputs, so an unchanged one
# takes seconds, while a changed patch, a Mesa pin or a new component (e.g. the Flatpak GL driver)
# gets built. Building only when artifacts were missing left such changes silently uninstalled.
if (( ! SKIP_BUILD )); then
    say "build (cached components are reused)"
    run "$here/oxp3-fixes.sh" build || die "build failed"
elif ! have_artifacts; then
    die "no build artifacts in $out and --skip-build was given; run ./oxp3-install.sh without --skip-build"
fi
say "second install: overlay Mesa/gamescope/MangoHud/XMX/extras"
run sudo OXP3_USER="$(id -un)" "$here/oxp3-fixes.sh" install || die "install failed"
# A rebuilt kernel module or firmware package is staged as a new deployment by that install,
# which then stops before the overlay step: finish after the reboot, like the first run.
if (( ! DRY )) && [[ $(staged_deployment) == yes ]]; then
    hardware_step
    say "updated kernel modules/firmware were staged: reboot required"
    info "systemctl reboot"
    if pending=$(overlay_pending); then
        info "after the reboot run ./oxp3-install.sh once more: $pending (and the rest of the"
        info "/usr overlay) can only go in once the new deployment is booted"
    else
        info "that finishes the install; the hardware fixes above are already in place"
    fi
    exit 0
fi

hardware_step

if (( ! SKIP_VERIFY )); then
    say "verification"
    run "$here/tools/check-working.sh" || warn "check-working reports a mismatch (see above)"
    if (( DRY )); then
        info "[dry-run] $here/tools/oxp3-verify.sh --json"
    else
        "$here/tools/oxp3-verify.sh" --json >/tmp/oxp3-verify-install.json 2>/dev/null || true
    fi
    if (( ! DRY )) && [[ -s /tmp/oxp3-verify-install.json ]]; then
        python3 - /tmp/oxp3-verify-install.json <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); c=d.get("counts",{})
print("   verify: %s pass, %s fail, %s skip, %s unknown" % (c.get("pass",0),c.get("fail",0),c.get("skip",0),c.get("unknown",0)))
for i in d.get("issues",[])[:6]: print("   -", i.get("state"), i.get("id"), str(i.get("message"))[:110])
PY
    fi
    if ! steam_present; then
        info "Steam is not set up yet: after its first start run tools/xmx-steam-enable.sh --all and restart Steam."
    fi
fi

say "done"
info "leave and re-enter Gamemode once; the first game launch rebuilds shader caches (CPU-bound, one time)."
[[ -d $state_dir ]] || mkdir -p "$state_dir"
printf '%s\n' "$(date -u +%FT%TZ)" > "$state"
