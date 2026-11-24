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
#   tools/xess-probe.sh --list         # every candidate game process, then pick one
#   tools/xess-probe.sh --pid 12345
set -uo pipefail
exe=""; pid=""; list=0
while (( $# )); do
    case $1 in
        --exe) exe=${2:-}; shift 2 ;;
        --pid) pid=${2:-}; shift 2 ;;
        --list) list=1; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [--exe SUBSTRING] [--pid PID]" >&2; exit 2 ;;
    esac
done

candidates() {
    # Under Wine the process name (comm) is truncated to 15 characters and usually keeps no ".exe",
    # so match the first token of the command line - the exe Wine was asked to run - and skip the
    # Wine/Steam infrastructure, which also ends in .exe.
    local d first comm
    for d in /proc/[0-9]*; do
        first=$(tr '\0' '\n' < "$d/cmdline" 2>/dev/null | head -1) || continue
        [[ -n $first ]] || continue
        first=${first##*\\}                      # c:\windows\system32\steam.exe -> steam.exe
        case ${first,,} in
            *.exe) ;;
            *) continue ;;
        esac
        case ${first,,} in
            steam.exe|services.exe|plugplay.exe|winemenubuilder.exe|winedevice.exe|explorer.exe|\
            rpcss.exe|svchost.exe|conhost.exe|tabtip.exe|start.exe|wineboot.exe|gameoverlayui.exe|\
            gravitymark.exe) : ;;   # gravitymark.exe is this repo's benchmark, not a game
            *) ;;
        esac
        case ${first,,} in
            steam.exe|services.exe|plugplay.exe|winemenubuilder.exe|winedevice.exe|explorer.exe|\
            rpcss.exe|svchost.exe|conhost.exe|tabtip.exe|start.exe|wineboot.exe|gameoverlayui.exe|\
            gravitymark.exe) continue ;;
        esac
        comm=$(cat "$d/comm" 2>/dev/null)
        printf '%s\t%s\t%s\n' "${d#/proc/}" "$comm" "${first:0:48}"
    done
}

if [[ $list == 1 ]]; then
    echo "candidate game processes:"
    c=$(candidates); [[ -n $c ]] || die "none found - is the game running?"
    printf '%s\n' "$c" | awk -F'\t' '{printf "  pid %-8s %-22s %s\n", $1, $2, $3}'
    exit 0
fi

if [[ -z $pid ]]; then
    while IFS=$'\t' read -r p comm first; do
        [[ -n $p ]] || continue
        if [[ -n $exe ]]; then
            [[ ${comm,,} == *"${exe,,}"* || ${first,,} == *"${exe,,}"* ]] || continue
        fi
        pid=$p; break
    done < <(candidates)
    if [[ -z $pid ]]; then
        echo "no game process found${exe:+ matching '$exe'} - candidates:" >&2
        candidates | awk -F'\t' '{printf "  pid %-8s %-22s %s\n", $1, $2, $3}' >&2
        echo "rerun with --pid N or drop --exe" >&2
        exit 1
    fi
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
echo "== GPU and power state right now (throttling versus a CPU/XeSS limit)"
for dev in /sys/class/drm/card*/device; do
    [ -d "$dev/gt" ] || [ -d "$dev/gt0" ] || continue
    for gt in "$dev"/gt*; do
        f=$gt/gt_cur_freq_mhz; [ -r "$f" ] && printf '  GPU clock (%s): %s MHz\n' "$(basename "$gt")" "$(cat "$f")"
    done
    for f in "$dev"/gt_max_freq_mhz "$dev"/gt_boost_freq_mhz; do
        [ -r "$f" ] && printf '  %-18s %s MHz\n' "$(basename "$f"):" "$(cat "$f")"
    done
done 2>/dev/null
for f in /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw; do
    [ -r "$f" ] && printf '  package PL1:       %s W\n' "$(( $(cat "$f") / 1000000 ))"
done
for z in /sys/class/thermal/thermal_zone*; do
    [ -r "$z/temp" ] || continue
    t=$(cat "$z/temp"); (( t > 30000 && t < 120000 )) || continue
    printf '  %-18s %s C\n' "$(cat "$z/type" 2>/dev/null):" "$(( t / 1000 ))"
done 2>/dev/null | head -4

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
