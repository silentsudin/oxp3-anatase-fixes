#!/bin/bash
# SPDX-License-Identifier: MIT
# dx11-wine-cpu.sh - the Wine-side CPU candidates, on top of the Proton Experimental baseline.
#
# DXVK sizes its worker pools from the processor topology that Wine reports, and on this machine
# that report is not the real one (2 P-cores at 4.7 GHz, 10 more at 3.6, 2 low-power at 3.1). A
# wrong topology means the wrong number of translation workers, which is a per-frame cost with
# idle-looking CPUs - the signature the fixed-cost batch is chasing.
#
#   baseline      Proton Experimental, no changes
#   topo-all      WINE_CPU_TOPOLOGY=14:0-13 (declare all 14 threads)
#   topo-smt      WINE_CPU_TOPOLOGY=7:0,1,2,3,4,5,6,7 (the launch-option form used on this box)
#   topo-pcore    WINE_CPU_TOPOLOGY=2:0,1 (two threads only: worst case for a worker pool)
#   esync-map     PROTON_NO_FSYNC=1 (already noise on CachyOS; re-checked on 3.1.1)
#
# Governor/EPP/scheduler variants are NOT here: they need root, and this machine currently runs
# sched_ext cosmos with the powersave governor and balance_power EPP - see
# tools/cpu-sched-test.sh epp-perf / gov-perf and docs/DX11-PERFORMANCE.md for the runbook.
#
# Everything is headless. Usage: tools/gpu-bench/dx11-wine-cpu.sh [-n repeats] [-w W] [-H H]
set -uo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
gm="$here/tools/gpu-bench/gravitymark.py"
repeats=1 width=1280 height=720
while getopts "n:w:H:h" opt; do
    case $opt in
        n) repeats=$OPTARG ;;
        w) width=$OPTARG ;;
        H) height=$OPTARG ;;
        h) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [-n repeats] [-w width] [-H height]" >&2; exit 2 ;;
    esac
done
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes/gpu-bench}
export OXP3_PROTON="$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/proton"
export OXP3_GM_PREFIX="$cache/gm-prefix-experimental"
out="$cache/results/dx11-wine-cpu-$(date +%Y%m%d-%H%M%S)"
echo "results: $out"
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
run topo-all d3d11 --env WINE_CPU_TOPOLOGY=14:0-13
run topo-smt d3d11 --env WINE_CPU_TOPOLOGY=7:0,1,2,3,4,5,6,7
run topo-pcore d3d11 --env WINE_CPU_TOPOLOGY=2:0,1
run esync-map d3d11 --env PROTON_NO_FSYNC=1
run baseline-end d3d11

echo
python3 "$gm" report "$out"
