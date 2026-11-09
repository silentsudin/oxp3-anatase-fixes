#!/bin/bash
# s2idle resume fix for the Predator GM7 / Biwin 1dee:1602 NVMe.
#
# That drive does not come back from s2idle through the ACPI simple-suspend path
# on Panther Lake: the machine suspends and never resumes (hard power-off needed).
# The known fix, used by the ONEXPLAYER 3 SteamOS fix pack on the same kernel
# family, is the kernel parameter nvme.noacpi=1, which makes the NVMe driver skip
# its own ACPI power management and let the platform handle the device.
#
# Usage: sudo tools/oxp3-nvme-suspend.sh status|apply|remove
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo $0 status|apply|remove" >&2; exit 2; }
KARG=nvme.noacpi=1
have_karg() { grep -qw -- "$KARG" /proc/cmdline; }
device() {
    local pci model
    pci=$(lspci -nn 2>/dev/null | grep -i '1dee:1602')
    model=$(cat /sys/class/nvme/nvme0/model 2>/dev/null | sed 's/ *$//')
    if [[ -n $pci ]]; then printf '%s\n' "1dee:1602 ${model}"; return 0; fi
    return 1
}
status() {
    local dev
    if dev=$(device); then
        echo "affected NVMe: $dev"
    else
        echo "affected NVMe: not present (this fix targets 1dee:1602 / Predator GM7)"
    fi
    if have_karg; then echo "kernel parameter: $KARG (active this boot)"; else echo "kernel parameter: $KARG (not active)"; fi
    echo "live setting: $(cat /sys/module/nvme_core/parameters/noacpi 2>/dev/null || echo '?') (N when the fix is not active)"
}
apply() {
    local dev
    if ! dev=$(device) && [[ ${1:-} != --force ]]; then
        echo "refusing: no 1dee:1602 NVMe on this machine (use --force to add the parameter anyway)" >&2
        return 1
    fi
    [[ -n $dev ]] && echo "affected NVMe: $dev"
    if have_karg; then echo "$KARG is already active"; return 0; fi
    echo "adding kernel argument $KARG with rpm-ostree (persistent, next boot)"
    rpm-ostree kargs --append="$KARG" || return 1
    echo
    echo "reboot to activate it, then test one suspend:"
    echo "  reboot ; cat /proc/cmdline | grep -o nvme.noacpi=1 ; sudo systemctl suspend"
    echo "undo: sudo $0 remove"
}
remove() {
    if ! have_karg && ! rpm-ostree kargs 2>/dev/null | grep -qw -- "$KARG"; then
        echo "$KARG is not set"; return 0
    fi
    echo "removing kernel argument $KARG"
    rpm-ostree kargs --delete="$KARG" || return 1
    echo "reboot to apply"
}
case ${1:-status} in
    status) status ;;
    apply) shift || true; apply "${1:-}" ;;
    remove) remove ;;
    *) echo "usage: sudo $0 status|apply|remove" >&2; exit 2 ;;
esac
