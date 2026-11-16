#!/bin/sh
# SPDX-License-Identifier: MIT
# lpmd-game-status.sh - show game-like processes, their affinity and lpmd class.
#
#   tools/lpmd-game-status.sh          # as your user: PID/comm/affinity
#   sudo tools/lpmd-game-status.sh     # also asks lpmd for the classification
#
# Finds processes launched from game directories (Recomp Library, steamapps,
# Heroic) or Windows executables, skipping shells/editors.
set -eu
[ "$(id -u)" = 0 ] && root=1 || root=0
for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    [ -r "$d/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case $cmd in
        *"/Games/"*|*steamapps*|*Heroic*|*.exe*) ;;
        *) continue ;;
    esac
    case $cmd in
        *python*|*bash*|*zsh*|*grep*|*lpmd-game-status*) continue ;;
    esac
    comm=$(cat "$d/comm" 2>/dev/null) || continue
    aff=$(taskset -pc "$pid" 2>/dev/null | sed 's/.*list: //')
    printf '%s %-18s affinity=%s\n' "$pid" "$comm" "$aff"
    if [ "$root" = 1 ]; then
        intel_lpmd_control GET-PROC-CLASSIFICATION "$comm" 2>/dev/null || true
    fi
done
