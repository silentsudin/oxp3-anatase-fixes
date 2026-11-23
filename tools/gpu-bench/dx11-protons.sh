#!/bin/bash
# SPDX-License-Identifier: MIT
# dx11-protons.sh - D3D11 throughput per installed Proton, i.e. per DXVK version.
#
# The cross-API matrix showed D3D11 running at ~85% of native Vulkan with the overhead constant
# across resolutions, so it is DXVK's per-frame CPU cost. If that cost differs between DXVK
# versions, the actionable fix is "run DX11 titles on the Proton that translates best", which
# needs no patch at all - so measure it before concluding the deficit is inherent.
#
# Each Proton gets its own Wine prefix (they are not shareable), created on first use.
# Everything runs headless: no window ever appears on the desktop.
#
#   tools/gpu-bench/dx11-protons.sh [-n repeats] [-w width] [-H height]
set -uo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
gm="$here/tools/gpu-bench/gravitymark.py"
repeats=2 width=1280 height=720
while getopts "n:w:H:h" opt; do
    case $opt in
        n) repeats=$OPTARG ;;
        w) width=$OPTARG ;;
        H) height=$OPTARG ;;
        h) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [-n repeats] [-w width] [-H height]" >&2; exit 2 ;;
    esac
done
steam=$HOME/.local/share/Steam
out="$HOME/.cache/oxp3-fixes/gpu-bench/results/dx11-protons-$(date +%Y%m%d-%H%M%S)"
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
echo "results: $out"

# label|path to the proton script
protons=()
add() { [[ -x $2 ]] && protons+=("$1|$2"); }
add "cachyos" "$steam/compatibilitytools.d/Proton-CachyOS Latest/proton"
add "proton11" "$steam/steamapps/common/Proton 11.0/proton"
add "experimental" "$steam/steamapps/common/Proton - Experimental/proton"
add "hotfix" "$steam/steamapps/common/Proton Hotfix/proton"
add "ge-proton11" "$steam/compatibilitytools.d/GE-Proton11-12/proton"
(( ${#protons[@]} )) || { echo "no Proton found" >&2; exit 1; }

for entry in "${protons[@]}"; do
    label=${entry%%|*} proton=${entry#*|}
    export OXP3_PROTON=$proton
    export OXP3_GM_PREFIX="$cache/gm-prefix-$label"
    dxvk=$(cat "$(dirname "$proton")"/files/lib64/wine/dxvk/version 2>/dev/null | awk '{print $NF}')
    echo "== $label (DXVK $dxvk)"
    for ((i = 1; i <= repeats; i++)); do
        python3 "$gm" run --api d3d11 --width "$width" --height "$height" \
            --label "$label-$i" --note "DXVK $dxvk" --out "$out" 2>&1 | tail -1
    done
done

echo
python3 "$gm" report "$out"
