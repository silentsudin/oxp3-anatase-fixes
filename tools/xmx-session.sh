#!/bin/bash
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
helper=$repo/tools/xmx-cm-compile-nodgrf.sh
[ -x "$helper" ] || helper=$HOME/.local/share/xess-xmx/tools/cm-compile.sh
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
        [ -f "$icd" ] || { echo "missing ICD: $icd" >&2; exit 1; }
        mkdir -p "$(dirname "$conf")"
        cat > "$conf" <<CONF
# XeSS XMX: use the XMX/CM Vulkan driver for games (written by tools/xmx-session.sh).
VK_DRIVER_FILES=$icd
VK_ICD_FILENAMES=$icd
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
