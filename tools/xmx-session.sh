#!/bin/bash
# SPDX-License-Identifier: MIT
# Make the XeSS XMX Vulkan driver the default for games in the desktop session.
#
# Without this, games use /usr/lib64/libvulkan_intel.so (the patched host Mesa) and
# never load the XMX/CM driver at ~/.cache/oxp3-fixes/xmx-driver, so the XeSS XMX
# path is not in use even when everything is installed. This writes a systemd
# user environment drop-in that exports the XMX ICD for the whole session.
#
# Usage: tools/xmx-session.sh install|status|remove   (no root)
set -uo pipefail
icd=${HOME:?}/.config/xess-xmx/icd-xmx.json
kernels=$HOME/.cache/xess-xmx/kernels
repo=$(cd "$(dirname "$0")/.." && pwd)
# Prefer the installed package's own helper: its shim and cm-compile.sh are one unit, and mixing a
# shim with our older nodgrf helper made the new shim compile with the wrong register-file flags
# (ghosting behind movement in games using XeSS, seen 2026-11-24). Only fall back to the repo's
# helper when no package is installed.
helper=$HOME/.local/share/xess-xmx/tools/cm-compile.sh
[ -x "$helper" ] || helper=$repo/tools/xmx-cm-compile-nodgrf.sh
conf=$HOME/.config/environment.d/90-oxp3-xmx.conf
log() { printf 'xmx-session: %s\n' "$*"; }
case ${1:-status} in
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac
case ${1:-status} in
    status)
        if [ -r "$conf" ]; then log "session environment: $conf"; sed 's/^/  /' "$conf"; else log "session environment not installed"; fi
        ;;
    install)
        # The host Mesa carries the CM injection when it was built with patches/xess-main;
        # then the session must NOT export a second ICD, or some processes end up on the
        # standalone XMX driver (a different Mesa version) and others on the host one.
        if grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so 2>/dev/null; then
            icd_lines="# the host Mesa has the CM injection: no separate ICD is selected"
        else
            [ -f "$icd" ] || { echo "missing ICD: $icd" >&2; exit 1; }
            icd_lines="VK_DRIVER_FILES=$icd
VK_ICD_FILENAMES=$icd"
        fi
        mkdir -p "$(dirname "$conf")"
        cat > "$conf" <<CONF
# XeSS XMX: CM kernel injection for games (written by tools/xmx-session.sh).
$icd_lines
ANV_CM_KERNEL_DIR=$kernels
ANV_CM_HELPER=$helper
VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer
force_vk_vendor=0
CONF
        log "wrote $conf"
        log "log out and back in (or restart the Gamemode session) for it to apply"
        ;;
    remove)
        rm -f "$conf" && log "removed $conf"
        ;;
    *) echo "usage: $0 install|status|remove" >&2; exit 2 ;;
esac
