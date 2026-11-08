#!/bin/bash
# cpu-sched-test.sh <tag> - apply one existing scheduler configuration, measure gaming performance AND
# efficiency, then restore. Candidates are shipped sched_ext schedulers (scx 1.1.3) plus EPP/governor variants.
#
# tags: baseline cake cosmos-turbo cosmos-perf p2dq lavd-nc bpfland tickless epp-perf gov-perf
set -u
tag=${1:-}
case "$tag" in
  baseline|cake|cosmos-gaming|cosmos-lowlat|p2dq|lavd-nc|bpfland|tickless|epp-perf|gov-perf) ;;
  *) echo "usage: $0 <tag>   (baseline cake cosmos-gaming cosmos-lowlat p2dq lavd-nc bpfland tickless epp-perf gov-perf)"; exit 2 ;;
esac
repo=$(cd "$(dirname "$0")/.." && pwd)
gm=$HOME/git/linux_scratch/gravitymark/bin
cpdir="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077/pfx/drive_c/users/steamuser/Documents/CD Projekt Red/Cyberpunk 2077/benchmarkResults"
results="$repo/docs/cpu-sched-results.csv"
[ -e "$results" ] || echo "tag,date,gm_fps,gm_score,cp_avg,cp_min,cp_max,avg_pkg_w,fps_per_w,applied_sched,epp_p0,epp_p2,epp_lpe,valid" > "$results"

pol() { echo /sys/devices/system/cpu/cpufreq/policy$1; }
set_epp() { echo "$2" | sudo tee "$(pol "$1")/energy_performance_preference" >/dev/null 2>&1; }
current_epp() { cat "$(pol "$1")/energy_performance_preference" 2>/dev/null; }
sched_state() { cat /sys/kernel/sched_ext/state 2>/dev/null || echo unknown; }
stop_scx() { for s in scx_cake scx_cosmos scx_p2dq scx_lavd scx_bpfland scx_tickless; do sudo pkill -x "$s" 2>/dev/null; done; sleep 1; }
start_scx() { local name=$1; shift
  sudo setsid nohup "$name" "$@" > "/tmp/scx-$tag.log" 2>&1 &
  for _ in $(seq 1 30); do [ "$(sched_state)" = enabled ] && return 0; sleep 0.5; done
  echo "  WARNING: $name not enabled (state=$(sched_state)); see /tmp/scx-$tag.log"; return 1; }

prev0=$(current_epp 0); prev2=$(current_epp 2); prev10=$(current_epp 10)
pgrep -x steam >/dev/null || echo "warning: Steam is not running; the Cyberpunk benchmark may fail"

stop_scx
case "$tag" in
  baseline) ;;
  cake) start_scx scx_cake ;;
  cosmos-gaming) start_scx scx_cosmos -c 0 -p 0 ;;
  cosmos-lowlat) start_scx scx_cosmos -c 0 -p 0 -m performance ;;
  p2dq) start_scx scx_p2dq ;;
  lavd-nc) start_scx scx_lavd --balanced --no-core-compaction ;;
  bpfland) start_scx scx_bpfland ;;
  tickless) start_scx scx_tickless ;;
  epp-perf)
      for c in 0 1; do set_epp "$c" performance; done
      for c in 2 3 4 5 6 7 8 9; do set_epp "$c" balance_performance; done
      for c in 10 11 12 13; do set_epp "$c" power; done ;;
  gov-perf)
      for c in $(seq 0 13); do echo performance | sudo tee "$(pol "$c")/scaling_governor" >/dev/null 2>&1; done ;;
esac
applied_sched=$(sched_state); a0=$(current_epp 0); a2=$(current_epp 2); a10=$(current_epp 10)
valid=yes
case "$tag" in cake|cosmos-gaming|cosmos-lowlat|p2dq|lavd-nc|bpfland|tickless)
    [ "$applied_sched" = enabled ] || valid=no ;; esac
echo "== $tag sched=$applied_sched epp(p0)=$a0 epp(p2)=$a2 epp(lpe)=$a10 valid=$valid"

# package-power sampler for the whole measurement window
( while :; do cat /run/rapl-power/package-0 2>/dev/null; sleep 1; done ) > "/tmp/pwr-$tag.txt" &
pwrpid=$!

# GravityMark: CPU/geometry heavy, 100k asteroids, 640x360 (resolution-independent => CPU metric)
before=$(wc -l < "$HOME/.GravityMark/GravityMark.log" 2>/dev/null || echo 0)
( cd "$gm" && timeout 300 ./GravityMark.x64 -vk -meshlet 1 -temporal 1 -benchmark 1 -count 1 -close 1 \
    -asteroids 100000 -width 640 -height 360 -fullscreen 0 -vsync 0 > "/tmp/gm-$tag.out" 2>&1 )
newlog=$(tail -n +$((before+1)) "$HOME/.GravityMark/GravityMark.log" 2>/dev/null)
gfps=$(echo "$newlog" | grep -oE "FPS: [0-9.]+" | tail -1 | awk '{print $2}')
gscore=$(echo "$newlog" | grep -oE "Score: [0-9]+" | tail -1 | awk '{print $2}')
[ "$applied_sched" = enabled ] && [ "$(sched_state)" != enabled ] && valid=no
echo "  GravityMark ${gfps:-none} FPS score=${gscore:-none} (sched now $(sched_state))"

# Cyberpunk 2077: direct Proton, all CPUs (no affinity restriction), -benchmark auto-runs and closes
mapfile -t oldd < <(find "$cpdir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null)
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export STEAM_COMPAT_DATA_PATH="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077"
timeout 420 "$repo/tools/cp2077-direct.sh" -benchmark > "/tmp/cp-$tag.out" 2>&1
newd=""
while IFS= read -r d; do b=$(basename "$d"); case " ${oldd[*]:-} " in *" $b "*) ;; *) newd=$d ;; esac; done < <(find "$cpdir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
if [ -n "$newd" ] && [ -e "$newd/summary.json" ]; then
  read -r avg mn mx <<<"$(python3 -c "import json;d=json.load(open('$newd/summary.json'))['Data'];print(round(d['averageFps'],1),round(d['minFps'],1),round(d['maxFps'],1))")"
else avg=NA; mn=NA; mx=NA; fi
[ "$applied_sched" = enabled ] && [ "$(sched_state)" != enabled ] && valid=no
echo "  Cyberpunk avg=${avg} min=${mn} max=${mx} (sched now $(sched_state))"

kill "$pwrpid" 2>/dev/null; wait "$pwrpid" 2>/dev/null
pw=$(python3 - <<PY
vals=[]
for l in open("/tmp/pwr-$tag.txt",errors="replace"):
    try: vals.append(float(l.strip()))
    except ValueError: pass
print(round(sum(vals)/len(vals),1) if vals else "NA")
PY
)
fpw=$(python3 -c "print(round(${avg}/${pw},3)) if '${avg}'!='NA' and '${pw}'!='NA' and float('${pw}')>0 else print('NA')" 2>/dev/null || echo NA)
echo "  average package power ${pw} W, Cyberpunk FPS/W ${fpw}"

stop_scx
set_epp 0 "$prev0"; set_epp 1 "$prev0"; set_epp 2 "$prev2"; set_epp 10 "$prev10"
for c in 3 4 5 6 7 8 9; do set_epp "$c" "$prev2"; done
for c in 11 12 13; do set_epp "$c" "$prev10"; done
echo "== restored sched=$(sched_state) epp(p0)=$(current_epp 0) epp(p2)=$(current_epp 2) epp(lpe)=$(current_epp 10)"
echo "$tag,$(date -Is),${gfps:-NA},${gscore:-NA},$avg,$mn,$mx,$pw,$fpw,$applied_sched,$a0,$a2,$a10,$valid" >> "$results"
echo "recorded in $results"
