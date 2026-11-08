#!/bin/bash
# cpu-sched-test.sh <tag>
# Apply one CPU-scheduler/EPP configuration, run GravityMark (native, CPU-heavy) and the Cyberpunk 2077
# benchmark (DX12, via Proton directly), record both results plus the state, then restore the previous setup.
#
# tags: baseline | lavd | lavd-autopower | bpfland | epp-perf | epp-perf-lavd
set -u
tag=${1:-}
case "$tag" in baseline|lavd|lavd-autopower|bpfland|epp-perf|epp-perf-lavd) ;; *) echo "usage: $0 <tag>"; exit 2;; esac
repo=$(cd "$(dirname "$0")/.." && pwd)
gm=$HOME/git/linux_scratch/gravitymark/bin
cpdir="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077/pfx/drive_c/users/steamuser/Documents/CD Projekt Red/Cyberpunk 2077/benchmarkResults"
results="$repo/docs/cpu-sched-results.csv"
[ -e "$results" ] || echo "tag,date,gravity_fps,cp_avg,cp_min,cp_max,scheduler,epp_pcores,epp_ecores,epp_lpecores" > "$results"

pol() { echo /sys/devices/system/cpu/cpufreq/policy$1; }
set_epp() { # $1 policy cpu, $2 value
    echo "$2" | sudo tee "$(pol "$1")/energy_performance_preference" >/dev/null 2>&1
}
current_epp() { cat "$(pol "$1")/energy_performance_preference" 2>/dev/null; }
sched_state() { cat /sys/kernel/sched_ext/state 2>/dev/null || echo unknown; }
stop_scx() { sudo pkill -x scx_lavd 2>/dev/null; sudo pkill -x scx_bpfland 2>/dev/null; sudo pkill -x scx_rusty 2>/dev/null; sleep 1; }
start_scx() { # $1 = scheduler name, rest = args
    local name=$1; shift
    sudo setsid nohup "$name" "$@" > "/tmp/scx-$tag.log" 2>&1 &
    for _ in $(seq 1 20); do [ "$(sched_state)" = enabled ] && return 0; sleep 0.5; done
    echo "  warning: $name did not report enabled (state=$(sched_state)), see /tmp/scx-$tag.log"; return 1
}

# --- remember previous setup
prev_epp0=$(current_epp 0); prev_epp2=$(current_epp 2); prev_epp10=$(current_epp 10)

# --- apply the requested configuration
stop_scx
case "$tag" in
    baseline) ;;
    lavd) start_scx scx_lavd --balanced ;;
    lavd-autopower) start_scx scx_lavd --autopower ;;
    bpfland) start_scx scx_bpfland ;;
    epp-perf|epp-perf-lavd)
        set_epp 0 performance; set_epp 1 performance
        for c in 2 3 4 5 6 7 8 9; do set_epp "$c" balance_performance; done
        for c in 10 11 12 13; do set_epp "$c" power; done
        [ "$tag" = epp-perf-lavd ] && start_scx scx_lavd --balanced ;;
esac
echo "== $tag: sched=$(sched_state) epp(p0)=$(current_epp 0) epp(p2)=$(current_epp 2) epp(p10)=$(current_epp 10)"

# --- GravityMark: CPU-heavy config, native Vulkan, closes itself
before=$(wc -l < "$HOME/.GravityMark/GravityMark.log" 2>/dev/null || echo 0)
( cd "$gm" && timeout 300 ./GravityMark.x64 -vk -meshlet 1 -temporal 1 -benchmark 1 -count 1 -close 1 \
    -asteroids 100000 -width 1280 -height 720 -fullscreen 0 -vsync 0 > "/tmp/gm-$tag.out" 2>&1 )
gfps=$(tail -n +$((before+1)) "$HOME/.GravityMark/GravityMark.log" 2>/dev/null | grep -oE "FPS: [0-9.]+" | tail -1 | awk '{print $2}')
echo "  GravityMark FPS: ${gfps:-none}"

# --- Cyberpunk 2077 (direct Proton, -benchmark auto-runs and closes)
mapfile -t oldd < <(ls -d "$cpdir"/*/ 2>/dev/null)
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export STEAM_COMPAT_DATA_PATH="$HOME/Games/Heroic/Prefixes/Cyberpunk 2077"
timeout 420 "$HOME/git/linux_scratch/oxp3-anatase-fixes/tools/cp2077-direct.sh" -benchmark > "/tmp/cp-$tag.out" 2>&1
newd=""
for d in $(ls -d "$cpdir"/*/ 2>/dev/null); do [[ " ${oldd[*]:-} " == *" $d "* ]] || newd=$d; done
if [ -n "$newd" ] && [ -e "$newd/summary.json" ]; then
    read -r avg mn mx <<<"$(python3 -c "import json;d=json.load(open('$newd/summary.json'))['Data'];print(round(d['averageFps'],1),round(d['minFps'],1),round(d['maxFps'],1))")"
else avg=NA; mn=NA; mx=NA; fi
echo "  Cyberpunk avg=${avg} min=${mn} max=${mx}"

# --- restore
stop_scx
set_epp 0 "$prev_epp0"; set_epp 1 "$prev_epp0"; set_epp 2 "$prev_epp2"; set_epp 10 "$prev_epp10"
for c in 3 4 5 6 7 8 9; do set_epp "$c" "$prev_epp2"; done
for c in 11 12 13; do set_epp "$c" "$prev_epp10"; done
echo "== restored: sched=$(sched_state) epp(p0)=$(current_epp 0) epp(p2)=$(current_epp 2) epp(p10)=$(current_epp 10)"

echo "$tag,$(date -Is),${gfps:-NA},$avg,$mn,$mx,$(sched_state),$prev_epp0,$prev_epp2,$prev_epp10" >> "$results"
echo "recorded in $results"
