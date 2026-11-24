#!/bin/bash
# SPDX-License-Identifier: MIT
# xess-probe.sh - what a running game actually got, and what it loaded.
#
# Every XeSS question so far was answered by inference from files on disk. This reads the live
# process instead: its environment (the only thing that decides whether the patched driver compiles
# XMX kernels at run time), the SDK/shim it mapped (which release), and whether kernels are being
# compiled right now. No root needed; the game's uid is ours.
#
#   tools/xess-probe.sh                 # auto-detect a running Proton game
#   tools/xess-probe.sh --exe Trails    # by executable substring
#   tools/xess-probe.sh --pid 12345
set -uo pipefail
exe=""; pid=""
while (( $# )); do
    case $1 in
        --exe) exe=${2:-}; shift 2 ;;
        --pid) pid=${2:-}; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [--exe SUBSTRING] [--pid PID]" >&2; exit 2 ;;
    esac
done

if [[ -z $pid ]]; then
    # gamescope and proton carry the game's name in their own command lines, so match the process
    # name (comm) - the game's exe - and never a wrapper. Same rule as tools/gpu-bench/thread-cpu.py.
    for d in /proc/[0-9]*; do
        comm=$(cat "$d/comm" 2>/dev/null) || continue
        case $comm in *.exe) ;; *) continue ;; esac
        case $comm in steam.exe|services.exe|plugplay.exe|winemenubuilder.exe|winedevice.exe|explorer.exe|rpcss.exe|svchost.exe|conhost.exe) continue ;; esac
        if [[ -n $exe ]]; then [[ ${comm,,} == *"${exe,,}"* ]] || continue; fi
        pid=${d#/proc/}; break
    done
fi
[[ -n $pid && -r /proc/$pid/cmdline ]] || { echo "no running game process found${exe:+ matching '$exe'}" >&2; exit 1; }

cmd=$(tr '\0' ' ' < /proc/$pid/cmdline)
echo "game:   pid $pid"
echo "        ${cmd:0:120}"

echo
echo "== environment the game got (from /proc/$pid/environ)"
for k in ANV_CM_HELPER ANV_CM_KERNEL_DIR XMX_IGC_DIR INTC_ALT_DRIVER_EXTENSIONS_PATH \
         VK_DRIVER_FILES VK_ICD_FILENAMES MESA VK_LOADER_LAYERS_ENABLE VK_ADD_LAYER_PATH \
         CM_MAX_GRF VKD3D_DISABLE_EXTENSIONS WINE_CPU_TOPOLOGY; do
    v=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | sed -n "s/^$k=//p" | head -1)
    printf '  %-34s %s\n' "$k" "${v:-<unset>}"
done

echo
echo "== what it loaded"
maps=/proc/$pid/maps
for pat in igdext64.dll igdext64 libxess libxell libvulkan_intel libVkLayer; do
    m=$(grep -o "/[^ ]*$pat[^ ]*" "$maps" 2>/dev/null | sort -u | head -2)
    [[ -n $m ]] || continue
    while IFS= read -r f; do
        if [[ -r $f ]]; then printf '  %-58s %s\n' "${f#/var/home/holzr}" "$(sha256sum "$f" 2>/dev/null | cut -c1-16)"
        else printf '  %-58s (unreadable)\n' "${f#/var/home/holzr}"; fi
    done <<< "$m"
done
grep -q 'libvulkan_intel' "$maps" 2>/dev/null && echo "  vulkan driver: libvulkan_intel (host Mesa)" || echo "  vulkan driver: not mapped"

echo
echo "== runtime kernel compilation (only the XMX path does this)"
log=$HOME/.cache/xess-xmx/compile.log
if [[ -r $log ]]; then
    printf '  compile.log last write: %s\n' "$(stat -c %y "$log" | cut -d. -f1)"
    printf '  kernels compiled in the last 10 min: %s\n' "$(find "$HOME/.cache/xess-xmx" -maxdepth 1 -name '*.cmk' -newermt '-10 minutes' 2>/dev/null | wc -l)"
else
    echo "  no compile.log"
fi
kd=$HOME/.cache/xess-xmx/kernels
[[ -d $kd ]] && echo "  kernel cache: $(ls "$kd" | wc -l) kernels" || echo "  kernel cache: absent (run-time compile only)"

echo
echo "== verdict"
h=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | sed -n 's/^ANV_CM_HELPER=//p' | head -1)
k=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | sed -n 's/^ANV_CM_KERNEL_DIR=//p' | head -1)
if [[ -z $h ]]; then
    echo "  NO ANV_CM_HELPER in the game: the patched driver cannot compile XMX kernels - XeSS is on DP4a."
elif [[ ! -x $h ]]; then
    echo "  ANV_CM_HELPER points at $h, which is not executable: XMX compiles will fail, XeSS falls back."
else
    echo "  ANV_CM_HELPER=$h (executable)"
    [[ -n $k && ! -d $k ]] && echo "  ANV_CM_KERNEL_DIR=$k does not exist: every kernel compiles at run time"
    [[ -n $k && -d $k ]] && echo "  ANV_CM_KERNEL_DIR=$k ($(ls "$k" | wc -l) kernels)"
fi
