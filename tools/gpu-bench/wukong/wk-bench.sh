#!/bin/bash
# wk-bench.sh <png> [start] : one Wukong benchmark pass; MangoHud logs 120 s of frame times
S=$(cd "$(dirname "$0")" && pwd)
# (started 10 s after Confirm, i.e. after scene load) into ~/.cache/oxp3-winre/mh; results screen saved.
png=${1:?png}; W=$HOME/.cache/oxp3-winre
if [[ ${2:-} == start ]]; then
    $S/wk-key.sh key Return; sleep 4
    $S/wk-key.sh mousemove 100 324 click 1; sleep 3
else
    $S/wk-key.sh key r; sleep 3
fi
$S/wk-key.sh mousemove 503 423 click 1
sleep 10
before=$(ls -t $W/mh/*.csv 2>/dev/null | grep -v _summary | head -1)
$S/wk-key.sh keydown Shift_L keydown F2 sleep 0.3 keyup F2 keyup Shift_L
sleep 135
for _ in $(seq 1 30); do
    $S/wk-shot.sh "$png" >/dev/null || exit 1
    t=$(podman exec oxp3-prof tesseract "$png" - 2>/dev/null)
    if grep -q "Benchmark Results" <<< "$t" && ! grep -q "Are you sure" <<< "$t"; then break; fi
    sleep 8
done
csv=$(ls -t $W/mh/*.csv 2>/dev/null | grep -v _summary | head -1)
[[ -n $csv && $csv != "$before" ]] || { echo "no new MangoHud log" >&2; exit 1; }
cp "$csv" "${png%.png}.csv"
python3 -I $S/mh-stats.py "${png%.png}.csv"
