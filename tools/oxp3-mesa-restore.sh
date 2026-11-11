#!/bin/bash
# SPDX-License-Identifier: MIT
# Restore the recorded known-good host Mesa (libvulkan_intel.so with the Anatase
# XMX patches) after an rpm-ostree update or reboot replaced it with the stock
# distribution build.
#
# The custom build lives on the rpm-ostree "usroverlay" hotfix layer, so a reboot
# clears it.  This restores it now (ephemeral).  For a persistent installation,
# build the patched package with ./oxp3-fixes-fast.sh and layer it, or keep using
# the runtime override for the Flatpak side (docs/WORKING-BUILD-2026-11-08.md).
#
# Usage: sudo tools/oxp3-mesa-restore.sh status|restore
set -uo pipefail
known=$HOME/.cache/oxp3-fixes/known-good/host-current
[ -e "$known" ] || known=$(ls -t "$HOME/.cache/oxp3-fixes/known-good"/host-*-libvulkan_intel.so 2>/dev/null | head -1)
[[ -r $known ]] || known=/var/home/holzr/.cache/oxp3-fixes/known-good/host-current
[[ -e $known ]] || known=$(ls -t /var/home/holzr/.cache/oxp3-fixes/known-good/host-*-libvulkan_intel.so 2>/dev/null | head -1)
target=/usr/lib64/libvulkan_intel.so
have() { sha256sum "$target" 2>/dev/null | cut -d' ' -f1; }
case ${1:-status} in
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac
[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo $0 status|restore" >&2; exit 2; }
case ${1:-status} in
    status)
        echo "host:   $(have)"
        echo "known:  $(sha256sum "$known" 2>/dev/null | cut -d' ' -f1)"
        echo "hotfix: $(rpm-ostree status --booted 2>/dev/null | awk -F': ' '/Unlocked/{print $2}')"
        ;;
    restore)
        [[ -r $known ]] || { echo "known-good copy missing: $known" >&2; exit 1; }
        cur=$(have)
        want=$(sha256sum "$known" | cut -d' ' -f1)
        if [[ $cur == "$want" ]]; then echo "already the recorded build ($want)"; exit 0; fi
        echo "current $cur -> restoring $want"
        unlock=$(rpm-ostree status --booted 2>/dev/null | awk -F': ' '/Unlocked/{print $2}')
        case ${unlock:-none} in
            none|"") rpm-ostree usroverlay || { echo "could not enter the usroverlay" >&2; exit 1; } ;;
            *) echo "deployment already unlocked ($unlock); using the writable /usr layer" ;;
        esac
        cp -f -- "$known" "$target" || exit 1
        got=$(have)
        [[ $got == "$want" ]] || { echo "restore verification failed: $got" >&2; exit 1; }
        echo "restored and verified: $got"
        echo "note: the usroverlay is ephemeral (cleared by reboot). For persistence, build and layer the patched package,"
        echo "      or rely on the Flatpak runtime override; see docs/WORKING-BUILD-2026-11-08.md."
        ;;
    *) echo "usage: sudo $0 status|restore" >&2; exit 2 ;;
esac
