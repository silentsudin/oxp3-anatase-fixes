#!/bin/bash
# SPDX-License-Identifier: MIT
# dx11-fixed-cost.sh - hunt the fixed per-frame cost that remains with DXVK 3.1.x.
#
# Where this starts: with Proton Experimental (DXVK 3.1.1) D3D11 runs ~88 fps where native Vulkan
# runs ~95, and the deficit does not scale with resolution - but thread sampling shows the process
# using only ~0.2 cores, so it is not CPU throughput. A constant per-frame cost with idle threads
# is either serialisation (submit/present fences) or GPU work DXVK adds regardless of pixels, and
# the candidates below are aimed at exactly that, plus an asymptote run to measure the constant.
#
#   baseline      Proton Experimental, no changes
#   tearfree-off  dxvk.tearFree = False        (present without the tear-free resolve path)
#   no-flush      d3d11.ignoreExplicitFlush    (do not stall on every Flush)
#   relaxed       d3d11.relaxedGraphicsBarriers (+ relaxedBarriers)
#   submit-thread MESA_VK_ENABLE_SUBMIT_THREAD=1 (Mesa submits off the app thread)
#   framelat-1    dxgi.maxFrameLatency = 1     (shallower queue)
#   desc-buffer   dxvk.enableDescriptorBuffer = True
#   asymptote     320x180 for d3d11 and vk-native: where the frame rate flattens is the constant
#
# Everything is headless. Usage: tools/gpu-bench/dx11-fixed-cost.sh [-n repeats] [-w W] [-H H]
set -uo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
gm="$here/tools/gpu-bench/gravitymark.py"
repeats=1 width=1280 height=720
while getopts "n:w:H:h" opt; do
    case $opt in
        n) repeats=$OPTARG ;;
        w) width=$OPTARG ;;
        H) height=$OPTARG ;;
        h) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [-n repeats] [-w width] [-H height]" >&2; exit 2 ;;
    esac
done
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
export OXP3_PROTON="$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/proton"
export OXP3_GM_PREFIX="$cache/gm-prefix-experimental"
out="$cache/results/dx11-fixed-cost-$(date +%Y%m%d-%H%M%S)"
echo "results: $out"
# one clean prefix for the whole batch: a wedged one deadlocks every run at swapchain creation
for p in $(pgrep -x wineserver) $(pgrep -x gamescopereaper); do kill -9 "$p" 2>/dev/null; done

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
run tearfree-off d3d11 --env "DXVK_CONFIG=dxvk.tearFree = False"
run no-flush d3d11 --env "DXVK_CONFIG=d3d11.ignoreExplicitFlush = True"
run relaxed d3d11 --env "DXVK_CONFIG=d3d11.relaxedGraphicsBarriers = True; d3d11.relaxedBarriers = True"
run submit-thread d3d11 --env MESA_VK_ENABLE_SUBMIT_THREAD=1
run framelat-1 d3d11 --env "DXVK_CONFIG=dxgi.maxFrameLatency = 1"
run desc-buffer d3d11 --env "DXVK_CONFIG=dxvk.enableDescriptorBuffer = True"

# drift control: the same baseline again at the end. Session-to-session spread on this machine has
# been up to ~1.4% (thermal state), which is wider than the 0.25% within-session spread, so a
# candidate is only credible against a baseline measured in the same batch.
run baseline-end d3d11

echo
echo "== asymptote: 320x180"
for api in d3d11 vk-native; do
    python3 "$gm" run --api "$api" --width 320 --height 180 --label "asymptote-$api" \
        --out "$out" 2>&1 | tail -1
done

echo
python3 "$gm" report "$out"
