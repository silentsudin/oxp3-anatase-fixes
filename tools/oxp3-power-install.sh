#!/bin/bash
# oxp3-power-install: install/remove oxp3-powerctl and its systemd units.
#
#   tools/oxp3-power-install.sh install             tool + boot/resume units
#   tools/oxp3-power-install.sh install --fan-curve tool + boot/resume + the
#                                                   opt-in fan-curve service
#   tools/oxp3-power-install.sh uninstall
#
# The boot units are harmless until a profile is saved:
#   sudo /usr/local/libexec/oxp3-powerctl profile balanced --save
# See docs/hw/power-fan.md. Nothing here touches the kernel or the EC directly.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)
bindir=/usr/local/libexec
unitdir=/etc/systemd/system

install_all() {
    local fan_curve=${1:-no}
    sudo install -D -m 0755 "$here/oxp3-powerctl.py" "$bindir/oxp3-powerctl"
    sudo install -m 0644 "$root/files/oxp3-power-profile.service" "$unitdir/oxp3-power-profile.service"
    sudo install -m 0644 "$root/files/oxp3-power-resume.service" "$unitdir/oxp3-power-resume.service"
    if [ "$fan_curve" = fan-curve ]; then
        sudo install -m 0644 "$root/files/oxp3-fan-curve.service" "$unitdir/oxp3-fan-curve.service"
    fi
    sudo systemctl daemon-reload
    # Enabled now, active only once /etc/oxp3/power.json exists (ConditionPathExists).
    sudo systemctl enable oxp3-power-profile.service oxp3-power-resume.service >/dev/null
    if [ "$fan_curve" = fan-curve ]; then
        sudo systemctl enable --now oxp3-fan-curve.service >/dev/null
    fi
    echo "installed: $bindir/oxp3-powerctl + oxp3-power-profile/resume.service"
    echo "pick a profile once (it is what the boot/resume units re-apply):"
    echo "  sudo $bindir/oxp3-powerctl profile balanced --save"
    echo "check what this kernel exposes, read-only:"
    echo "  $bindir/oxp3-powerctl status"
    if [ "$fan_curve" != fan-curve ]; then
        echo "the fan curve is opt-in: re-run with --fan-curve, or"
        echo "  sudo install -m 0644 files/oxp3-fan-curve.service $unitdir/ && sudo systemctl enable --now oxp3-fan-curve.service"
    fi
}

uninstall_all() {
    sudo systemctl disable --now oxp3-fan-curve.service 2>/dev/null || true
    sudo systemctl disable --now oxp3-power-profile.service oxp3-power-resume.service 2>/dev/null || true
    sudo rm -f "$unitdir/oxp3-power-profile.service" "$unitdir/oxp3-power-resume.service" \
               "$unitdir/oxp3-fan-curve.service" "$bindir/oxp3-powerctl"
    sudo systemctl daemon-reload
    # Deliberately kept: /etc/oxp3/ (power.json, baseline, fan curve) - remove by hand.
    echo "removed the units and $bindir/oxp3-powerctl; /etc/oxp3/ was left in place"
    echo "a live profile is undone by 'restore-baseline' *before* uninstalling,"
    echo "or simply by a reboot: the firmware reinitialises the RAPL limits."
}

case ${1:-install} in
    install)
        shift || true
        case ${1:-} in
            --fan-curve) install_all fan-curve ;;
            "")          install_all ;;
            *)           echo "unknown option $1 (only --fan-curve)" >&2; exit 2 ;;
        esac
        ;;
    uninstall|remove)
        uninstall_all
        ;;
    *)
        echo "usage: $0 {install [--fan-curve]|uninstall}" >&2
        exit 2
        ;;
esac
