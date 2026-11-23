#!/bin/bash
# SPDX-License-Identifier: MIT
# dx11-ab.sh - A/B the DXVK candidates against the measured D3D11 baseline.
#
# The Cross-API matrix (tools/gpu-bench/gravitymark.py matrix) established where DX11 stands:
# D3D11 runs at ~85% of the native-Vulkan frame rate at both 1280x720 and 640x360, i.e. the
# deficit does not shrink when the pixel count drops 4x. A gap that is constant in relative
# terms is a per-frame CPU cost on the D3D11 path (DXVK command translation plus Wine), not
# worse GPU work - so the candidates below are all CPU-side or driver-side knobs.
#
#   baseline     no change (the number every candidate has to beat)
#   intel-jay    INTEL_JAY=all, the env the desktop's Steam launch options carry
#   no-gpl       dxvk.enableGraphicsPipelineLibrary = False
#   cached-dyn   d3d11.cachedDynamicResources = a  (cache dynamic buffers, D3D11's weak spot)
#   no-desc-heap dxvk.enableDescriptorHeap = False (DXVK 3.x path, enabled on this driver)
#   pcores       CPU 0-1 only - the 4.7 GHz cores here - for DXVK's submission thread
#   pcores-d3d12 same pinning on the D3D12 path, as a control: if it helps both, it is not
#                a DXVK effect
#
# Everything runs headless (gamescope --backend headless), so it never touches the visible
# desktop. Usage:
#   tools/gpu-bench/dx11-ab.sh                 # 2 repeats per variant at 1280x720
#   tools/gpu-bench/dx11-ab.sh -n 3 -w 640 -H 360
set -uo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
gm="$here/tools/gpu-bench/gravitymark.py"
repeats=2 width=1280 height=720
while getopts "n:w:H:h" opt; do
    case $opt in
        n) repeats=$OPTARG ;;
        w) width=$OPTARG ;;
        H) height=$OPTARG ;;
        h) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [-n repeats] [-w width] [-H height]" >&2; exit 2 ;;
    esac
done
out="$HOME/.cache/oxp3-fixes/gpu-bench/results/dx11-ab-$(date +%Y%m%d-%H%M%S)"
echo "results: $out"

run() { # <label> <api> [extra args...]
    local label=$1 api=$2; shift 2
    local i
    for ((i = 1; i <= repeats; i++)); do
        echo "== $label ($i/$repeats)"
        python3 "$gm" run --api "$api" --width "$width" --height "$height" \
            --label "$label-$i" --out "$out" "$@" 2>&1 | tail -1
    done
}

run baseline d3d11
run intel-jay d3d11 --env INTEL_JAY=all
run no-gpl d3d11 --env "DXVK_CONFIG=dxvk.enableGraphicsPipelineLibrary = False"
run cached-dyn d3d11 --env "DXVK_CONFIG=d3d11.cachedDynamicResources = a"
run no-desc-heap d3d11 --env "DXVK_CONFIG=dxvk.enableDescriptorHeap = False"
run pcores d3d11 --taskset 0-1
run pcores-d3d12 d3d12 --taskset 0-1

echo
python3 "$gm" report "$out"
