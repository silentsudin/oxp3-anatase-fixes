#!/bin/bash
# SPDX-License-Identifier: MIT
# oxp3-power-install: install/remove oxp3-powerctl, oxp3-adaptivetdp and their
# systemd units.
#
#   tools/oxp3-power-install.sh install             tool + boot/resume units
#   tools/oxp3-power-install.sh install --fan-curve tool + boot/resume + the
#                                                   opt-in fan-curve service
#   tools/oxp3-power-install.sh install --adaptive-tdp
#                                                   also the *inactive* adaptive
#                                                   TDP daemon + unit
#   tools/oxp3-power-install.sh uninstall
#
# The boot units are harmless until a profile is saved:
#   sudo /usr/local/libexec/oxp3-powerctl profile balanced --save
# The adaptive daemon is installed but never enabled or started here: it is
# opt-in and you start it yourself once you trust the decision log:
#   sudo systemctl start oxp3-adaptivetdp
# See docs/hw/power-fan.md and docs/hw/battery-tdp.md. Nothing here touches the
# kernel or the EC directly.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)
bindir=/usr/local/libexec
unitdir=/etc/systemd/system

install_all() {
    local fan_curve=no adaptive=no
    for opt in "$@"; do
        case $opt in
            --fan-curve)    fan_curve=yes ;;
            --adaptive-tdp) adaptive=yes ;;
            *) echo "unknown option $opt (want --fan-curve and/or --adaptive-tdp)" >&2; exit 2 ;;
        esac
    done

    sudo install -D -m 0755 "$here/oxp3-powerctl.py" "$bindir/oxp3-powerctl"
    sudo install -m 0644 "$root/files/oxp3-power-profile.service" "$unitdir/oxp3-power-profile.service"
    sudo install -m 0644 "$root/files/oxp3-power-resume.service" "$unitdir/oxp3-power-resume.service"
    if [ "$fan_curve" = yes ]; then
        sudo install -m 0644 "$root/files/oxp3-fan-curve.service" "$unitdir/oxp3-fan-curve.service"
    fi
    sudo systemctl daemon-reload
    # Enabled now, active only once /etc/oxp3/power.json exists (ConditionPathExists).
    sudo systemctl enable oxp3-power-profile.service oxp3-power-resume.service >/dev/null
    if [ "$fan_curve" = yes ]; then
        sudo systemctl enable --now oxp3-fan-curve.service >/dev/null
    fi
    echo "installed: $bindir/oxp3-powerctl + oxp3-power-profile/resume.service"
    echo "pick a profile once (it is what the boot/resume units re-apply):"
    echo "  sudo $bindir/oxp3-powerctl profile balanced --save"
    echo "check what this kernel exposes, read-only:"
    echo "  $bindir/oxp3-powerctl status"
    echo "battery health, charge limit and charge-inhibit ('bypass'):"
    echo "  $bindir/oxp3-powerctl battery status"

    if [ "$adaptive" = yes ]; then
        # The daemon imports oxp3-powerctl.py by path, so both have to sit in
        # the same directory and keep these exact names.
        sudo install -D -m 0755 "$here/oxp3-adaptivetdp.py" "$bindir/oxp3-adaptivetdp"
        sudo install -m 0644 "$root/files/oxp3-adaptivetdp.service" "$unitdir/oxp3-adaptivetdp.service"
        sudo systemctl daemon-reload
        echo "adaptive TDP installed but NOT enabled (opt-in, default off)."
        echo "  watch it decide, writing nothing:"
        echo "    $bindir/oxp3-adaptivetdp --once --dry-run --json"
        echo "  validate with a stress run (docs/hw/battery-tdp.md section 5), then:"
        echo "    sudo systemctl start oxp3-adaptivetdp     # Ctrl-C/stop restores the preset"
        echo "    sudo systemctl enable oxp3-adaptivetdp    # only if you want it every boot"
        echo "  decision log: journalctl -u oxp3-adaptivetdp -b  and  /var/log/oxp3-adaptivetdp.jsonl"
    fi

    if [ "$fan_curve" != yes ]; then
        echo "the fan curve is opt-in: re-run with --fan-curve, or"
        echo "  sudo install -m 0644 files/oxp3-fan-curve.service $unitdir/ && sudo systemctl enable --now oxp3-fan-curve.service"
    fi
}

uninstall_all() {
    sudo systemctl disable --now oxp3-adaptivetdp.service 2>/dev/null || true
    sudo systemctl disable --now oxp3-fan-curve.service 2>/dev/null || true
    sudo systemctl disable --now oxp3-power-profile.service oxp3-power-resume.service 2>/dev/null || true
    sudo rm -f "$unitdir/oxp3-power-profile.service" "$unitdir/oxp3-power-resume.service" \
               "$unitdir/oxp3-fan-curve.service" "$unitdir/oxp3-adaptivetdp.service" \
               "$bindir/oxp3-powerctl" "$bindir/oxp3-adaptivetdp"
    sudo systemctl daemon-reload
    # Deliberately kept: /etc/oxp3/ (power.json, baseline, fan curve) and
    # /var/log/oxp3-adaptivetdp.jsonl - remove by hand.
    echo "removed the units, $bindir/oxp3-powerctl and $bindir/oxp3-adaptivetdp;"
    echo "/etc/oxp3/ and /var/log/oxp3-adaptivetdp.jsonl were left in place"
    echo "a live profile is undone by 'restore-baseline' *before* uninstalling,"
    echo "or simply by a reboot: the firmware reinitialises the RAPL limits."
}

case ${1:-install} in
    install)
        shift || true
        install_all "$@"
        ;;
    uninstall|remove)
        uninstall_all
        ;;
    *)
        echo "usage: $0 {install [--fan-curve] [--adaptive-tdp]|uninstall}" >&2
        exit 2
        ;;
esac
