#!/bin/bash
# SPDX-License-Identifier: MIT
# perf-window.sh - one root session for GPU measurements: open the hardware counters for a limited
# time and let the (unprivileged) measurement scripts switch the package power limit between the
# stock 35 W and OneXPlayer's own 40 W "unlock", then restore everything.
#
#   sudo tools/gpu-bench/perf-window.sh [--minutes N]     (default 120, at most 240)
#
# While it runs:
#   - dev.xe.observation_paranoid=0   GPU OA counters (EU active/stall, sampler, memory bandwidth)
#   - kernel.perf_event_paranoid=-1   system-wide and kernel-side CPU profiling (ioctl cost)
#   - kernel.kptr_restrict=0          kernel symbols for that profile
#   - /run/oxp3-perf-window/cmd       the invoking user may write ONE of these words:
#         pl35   restore the power limits found at start
#         pl40   PL1 40 W / PL2 41 W on both RAPL zones (OneXConsole's own unlock preset)
#         stop   end the window now
#     Anything else is ignored and logged. The limit is re-asserted every 5 s (the EC rewrites PL1 on
#     power events), and a package temperature of 97 C for 3 s drops back to the start values and
#     refuses pl40 for the rest of the window.
#
# Everything is restored on timeout, `stop`, Ctrl-C, or any error (trap on EXIT).
set -u
minutes=120
while (( $# )); do
    case $1 in
        --minutes) minutes=${2:-}; shift 2 ;;
        -h|--help) sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ $minutes =~ ^[0-9]+$ ]] && (( minutes >= 1 && minutes <= 240 )) || { echo "--minutes must be 1-240" >&2; exit 2; }
(( EUID == 0 )) || { echo "run with sudo" >&2; exit 1; }
user=${SUDO_USER:-}
[[ -n $user && $user != root ]] || { echo "run with sudo from your own session (SUDO_USER is empty)" >&2; exit 1; }
group=$(id -gn "$user")

zones=(/sys/class/powercap/intel-rapl:0 /sys/class/powercap/intel-rapl-mmio:0)
temp_zone=$(for z in /sys/class/thermal/thermal_zone*; do [[ $(cat "$z/type" 2>/dev/null) == x86_pkg_temp ]] && echo "$z"; done | head -1)
dir=/run/oxp3-perf-window
log() { printf '[%s] %s\n' "$(date +%T)" "$*" | tee -a "$dir/log"; }

# --- save ---------------------------------------------------------------------------------
declare -A saved
save() { [[ -e $1 ]] && saved[$1]=$(cat "$1"); }
for f in /proc/sys/dev/xe/observation_paranoid /proc/sys/kernel/perf_event_paranoid /proc/sys/kernel/kptr_restrict; do save "$f"; done
for z in "${zones[@]}"; do save "$z/constraint_0_power_limit_uw"; save "$z/constraint_1_power_limit_uw"; done

restored=0
restore() {
    (( restored )) && return
    restored=1
    set_pl_saved
    for f in /proc/sys/dev/xe/observation_paranoid /proc/sys/kernel/perf_event_paranoid /proc/sys/kernel/kptr_restrict; do
        [[ -n ${saved[$f]:-} ]] && echo "${saved[$f]}" > "$f" 2>/dev/null
    done
    log "restored: $(summary)"
    echo "ended" > "$dir/status" 2>/dev/null
}
trap restore EXIT
trap 'exit 130' INT TERM

summary() {
    printf 'observation_paranoid=%s perf_event_paranoid=%s kptr_restrict=%s' \
        "$(cat /proc/sys/dev/xe/observation_paranoid 2>/dev/null)" \
        "$(cat /proc/sys/kernel/perf_event_paranoid)" "$(cat /proc/sys/kernel/kptr_restrict)"
    for z in "${zones[@]}"; do
        printf ' %s PL1=%sW PL2=%sW' "${z##*/}" \
            $(( $(cat "$z/constraint_0_power_limit_uw") / 1000000 )) $(( $(cat "$z/constraint_1_power_limit_uw") / 1000000 ))
    done
}

# raise: PL2 before PL1; lower: PL1 before PL2 (never PL1 > PL2 in between)
set_pl() { # <pl1_uw> <pl2_uw> per zone
    local pl1=$1 pl2=$2 z
    for z in "${zones[@]}"; do
        if (( pl1 >= $(cat "$z/constraint_0_power_limit_uw") )); then
            echo "$pl2" > "$z/constraint_1_power_limit_uw"; echo "$pl1" > "$z/constraint_0_power_limit_uw"
        else
            echo "$pl1" > "$z/constraint_0_power_limit_uw"; echo "$pl2" > "$z/constraint_1_power_limit_uw"
        fi
    done
}
set_pl_saved() {
    local z
    for z in "${zones[@]}"; do
        local f0=$z/constraint_0_power_limit_uw f1=$z/constraint_1_power_limit_uw
        [[ -n ${saved[$f0]:-} ]] && echo "${saved[$f0]}" > "$f0"
        [[ -n ${saved[$f1]:-} ]] && echo "${saved[$f1]}" > "$f1"
    done
}

# --- open ---------------------------------------------------------------------------------
mkdir -p "$dir"; : > "$dir/cmd"; : > "$dir/log"
chown root:"$group" "$dir" "$dir/cmd"; chmod 0750 "$dir"; chmod 0660 "$dir/cmd"
chmod 0644 "$dir/log"
echo 0  > /proc/sys/dev/xe/observation_paranoid || { echo "cannot open the OA counters" >&2; exit 1; }
echo -1 > /proc/sys/kernel/perf_event_paranoid
echo 0  > /proc/sys/kernel/kptr_restrict
mode=stock; thermal_lock=0
echo "open stock $(date +%s) $minutes" > "$dir/status"; chmod 0644 "$dir/status"
log "window open for $minutes min: $(summary)"
log "the measurement scripts write pl35 / pl40 / stop to $dir/cmd; Ctrl-C here ends it early"

end=$(( $(date +%s) + minutes * 60 )); hot=0; tick=0
while (( $(date +%s) < end )); do
    sleep 1; tick=$((tick + 1))
    cmd=$(head -c 16 "$dir/cmd" 2>/dev/null | tr -dc 'a-z0-9'); [[ -n $cmd ]] && : > "$dir/cmd"
    case $cmd in
        "") ;;
        pl40)
            if (( thermal_lock )); then log "pl40 refused: thermal lock"
            else set_pl 40000000 41000000; mode=unlock; log "pl40: $(summary)"; fi ;;
        pl35) set_pl_saved; mode=stock; log "pl35: $(summary)" ;;
        stop) log "stop requested"; break ;;
        *) log "ignored command '$cmd'" ;;
    esac
    if [[ -n $temp_zone ]]; then
        t=$(cat "$temp_zone/temp" 2>/dev/null || echo 0)
        if (( t >= 97000 )); then hot=$((hot + 1)); else hot=0; fi
        if (( hot >= 3 )) && [[ $mode == unlock ]]; then
            set_pl_saved; mode=stock; thermal_lock=1; log "package ${t} mC for 3 s: back to the start limits, pl40 locked"
        fi
    fi
    if (( tick % 5 == 0 )) && [[ $mode == unlock ]]; then
        for z in "${zones[@]}"; do
            if (( $(cat "$z/constraint_0_power_limit_uw") != 40000000 )); then
                log "${z##*/} PL1 was rewritten to $(( $(cat "$z/constraint_0_power_limit_uw") / 1000000 )) W; re-asserting 40 W"
                set_pl 40000000 41000000
            fi
        done
    fi
    echo "open $mode $(( (end - $(date +%s)) / 60 ))min-left thermal_lock=$thermal_lock" > "$dir/status"
done
log "window closing"
