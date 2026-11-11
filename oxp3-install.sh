#!/bin/bash
# oxp3-install.sh - one entry point for a complete OneXPlayer 3 Anatase install.
#
#   ./oxp3-install.sh            # build, stage kernel modules/firmware, or finish after reboot
#   systemctl reboot             # only when it asks to
#   ./oxp3-install.sh            # second run: overlay stack + hardware fixes + verification
#
# It is idempotent and decides the phase from the live system:
#   - booted deployment lacks kmod-oxp3/oxp3-firmware -> build + first install, then asks for a reboot;
#   - booted deployment has them (after the reboot)   -> overlay Mesa/gamescope/XMX + hardware + verify.
# Secure Boot: if the MOK key is not enrolled it queues it (once), asks for the firmware-screen
# enrolment + reboot, and the next run continues.
#
# Options: --dry-run  --status  --skip-build  --skip-hw  --skip-mok  --skip-verify
#          --uninstall  --force
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
state_dir=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-fixes
state=$state_dir/bootstrapper
cache=${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}
out=$cache/out
mok_dir=${OXP3_MOK_DIR:-$HOME/.local/share/oxp3-fixes/mok}

DRY=0 SKIP_BUILD=0 SKIP_HW=0 SKIP_MOK=0 SKIP_VERIFY=0 FORCE=0 ACTION=install
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
run() {
    if (( DRY )); then printf '   [dry-run] %s\n' "$*"; else printf '   $ %s\n' "$*"; "$@"; fi
}

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

for arg in "$@"; do
    case $arg in
        --dry-run) DRY=1 ;;
        --status) ACTION=status ;;
        --uninstall) ACTION=uninstall ;;
        --skip-build) SKIP_BUILD=1 ;;
        --skip-hw) SKIP_HW=1 ;;
        --skip-mok) SKIP_MOK=1 ;;
        --skip-verify) SKIP_VERIFY=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage ;;
    esac
done

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
    [[ -x $here/tools/check-working.sh ]] && { $here/tools/check-working.sh 2>/dev/null | tail -n +1 | head -12; }
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
    run sudo "$here/oxp3-fixes.sh" install || die "install failed"
    say "reboot required to boot the patched kernel/firmware"
    if (( MOK_NEEDED )); then
        info "in the blue MOK manager choose 'Enroll MOK' -> the oxp3 key, then continue booting"
    fi
    info "systemctl reboot"
    info "then run ./oxp3-install.sh again (it will finish automatically)"
    exit 0
fi

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
if (( ! SKIP_BUILD )) && ! have_artifacts; then
    say "build (artifacts missing)"
    run "$here/oxp3-fixes.sh" build || die "build failed"
fi
say "second install: overlay Mesa/gamescope/MangoHud/XMX/extras"
run sudo "$here/oxp3-fixes.sh" install || die "install failed"

if (( ! SKIP_HW )); then
    say "hardware fixes (volume, LEDs/RGB, suspend, Bluetooth, power, IMU, audio, XMX, Decky)"
    run sudo "$here/tools/oxp3-hw-install.sh" --all --audio-activate --restart-plugin-loader \
        || warn "hardware installer reported a problem; see the lines above"
fi

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
