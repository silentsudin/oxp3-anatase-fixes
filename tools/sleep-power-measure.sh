#!/bin/bash
# sleep-power-measure.sh -- measure what the OneXPlayer 3 actually burns in s2idle.
#
# The complaint this answers is "suspend is inefficient: the device stays warm and the
# battery drains". Warmth and drain are two different numbers, so this measures both, and
# it separates the two very different hours a handheld can spend "asleep":
#
#   awake window     : the machine is not suspended (idle desktop, screen off, or a suspend
#                      that never happened). A handheld that is warm on the desk is here.
#   suspended window : real s2idle/S0ix time, counted from the kernel's own
#                      /sys/power/suspend_stats/total_hw_sleep, so a window that silently
#                      aborted after 3 s can never be reported as 10 minutes of sleep.
#
# Everything except `suspend` runs as your normal user. `suspend` needs root: it suspends
# with rtcwake (both rtc_cmos and alarmtimer are enabled wake sources on this device) and
# measures the battery before and after the resume.
#
# Usage:
#   sleep-power-measure.sh snapshot                 # one line, for a loop you already have
#   sleep-power-measure.sh audit                    # s2idle config + every enabled wake source
#   sleep-power-measure.sh run  [-w SEC] [-l NAME] [-o FILE]    # measure the current state
#   sleep-power-measure.sh suspend [-w SEC] [-l NAME] [-o FILE] # needs root
#
# Health thresholds (override in the environment):
#   PASS_PCT_PER_H=1.0   % of the full battery per hour -- about 0.88 W on this 87.7 Wh pack
#   FAIL_PCT_PER_H=2.0   above this the device is still "draining like a laptop in a bag"
#
# Exit status: 0 pass, 1 fail or warn, 2 measurement invalid (on AC / suspend refused),
#              3 usage or setup error.
set -u

PASS_PCT_PER_H=${PASS_PCT_PER_H:-1.0}
FAIL_PCT_PER_H=${FAIL_PCT_PER_H:-2.0}
SAMPLES_S=${SAMPLES_S:-5}                 # sampling period inside a window
RESUME_SETTLE_S=${RESUME_SETTLE_S:-20}    # awake time after resume before the next reading

BAT=
for b in /sys/class/power_supply/BAT*; do
    [[ -r $b/energy_now || -r $b/charge_now ]] && { BAT=$b; break; }
done

RAPL_PSYS=
RAPL_PKG=
for z in /sys/class/powercap/intel-rapl:*/; do
    [[ -r $z/name ]] || continue
    read -r n < "$z/name"
    case $n in
        psys)      RAPL_PSYS=${z%/}/energy_uj ;;
        package-0) [[ -z $RAPL_PKG ]] && RAPL_PKG=${z%/}/energy_uj ;;
    esac
done

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit 3; }
die() { echo "sleep-power-measure: $*" >&2; exit 3; }

# ---------------------------------------------------------------- readers
battery_energy_wh() {
    [[ -n $BAT ]] || return 1
    if [[ -r $BAT/energy_now ]]; then
        awk -v e="$(< "$BAT/energy_now")" 'BEGIN{printf "%.3f", e/1e6}'
    elif [[ -r $BAT/charge_now && -r $BAT/voltage_now ]]; then
        awk -v c="$(< "$BAT/charge_now")" -v v="$(< "$BAT/voltage_now")" \
            'BEGIN{printf "%.3f", c*v/1e12}'
    else
        return 1
    fi
}
battery_full_wh() {
    if [[ -r $BAT/energy_full ]]; then
        awk -v e="$(< "$BAT/energy_full")" 'BEGIN{printf "%.3f", e/1e6}'
    elif [[ -r $BAT/charge_full && -r $BAT/voltage_now ]]; then
        awk -v c="$(< "$BAT/charge_full")" -v v="$(< "$BAT/voltage_now")" \
            'BEGIN{printf "%.3f", c*v/1e12}'
    else
        echo 0
    fi
}
battery_pct()    { [[ -r $BAT/capacity ]] && cat "$BAT/capacity" || echo "?"; }
battery_status() { [[ -r $BAT/status ]] && cat "$BAT/status" || echo "?"; }
on_ac() {
    local a
    for a in /sys/class/power_supply/A*/online; do
        [[ -r $a ]] || continue
        [[ $(< "$a") == 1 ]] && return 0
    done
    return 1
}
rapl_uj() { [[ -n $1 && -r $1 ]] || return 1; cat "$1"; }
suspend_ns() { [[ -r /sys/power/suspend_stats/total_hw_sleep ]] && cat /sys/power/suspend_stats/total_hw_sleep || echo 0; }
suspend_ok() { [[ -r /sys/power/suspend_stats/success ]] && cat /sys/power/suspend_stats/success || echo 0; }
pkg_temp() {
    local z
    for z in /sys/class/thermal/thermal_zone*; do
        [[ -r $z/type && -r $z/temp ]] || continue
        [[ $(< "$z/type") == x86_pkg_temp ]] && { awk -v t="$(< "$z/temp")" 'BEGIN{printf "%.1f", t/1000}'; return 0; }
    done
    echo "?"
}
temp_max() {  # temp_max A B -> larger of A, B (tolerates "?")
    if [[ $1 == "?" ]]; then echo "$2"; elif [[ $2 == "?" ]]; then echo "$1"
    else awk -v a="$1" -v b="$2" 'BEGIN{printf "%.1f", (a>b?a:b)}'; fi
}

# ---------------------------------------------------------------- snapshot / audit
cmd_snapshot() {
    printf 'battery=%s%% (%s Wh of %s Wh, %s) suspend_ok=%s hw_sleep=%.2fs pkg=%sC\n' \
        "$(battery_pct)" "$(battery_energy_wh)" "$(battery_full_wh)" "$(battery_status)" \
        "$(suspend_ok)" "$(awk -v n="$(suspend_ns)" 'BEGIN{print n/1e9}')" "$(pkg_temp)"
}

cmd_audit() {
    echo "== suspend capability"
    printf 'mem_sleep       : %s\n' "$(tr -d '\n' < /sys/power/mem_sleep)"
    printf 'state           : %s\n' "$(tr '\n' ' ' < /sys/power/state)"
    printf 'hibernate (disk): %s\n' "$(tr -d '\n' < /sys/power/disk)"
    printf 'lockdown        : %s\n' "$(tr -d '\n' < /sys/kernel/security/lockdown 2>/dev/null || echo '(unreadable)')"
    printf 'platform_profile: %s\n' "$(cat /sys/firmware/acpi/platform_profile 2>/dev/null || echo '?')"
    printf 'pcie_aspm.policy: %s\n' "$(grep -o '\[[a-z]*\]' /sys/module/pcie_aspm/parameters/policy 2>/dev/null | tr -d '[]' || echo '?')"
    printf 'nvme ps_latency : %s us (nvme_core.default_ps_max_latency_us)\n' \
        "$(cat /sys/module/nvme_core/parameters/default_ps_max_latency_us 2>/dev/null || echo '?')"

    echo
    echo "== kernel suspend record (this boot)"
    for f in success fail failed_suspend failed_resume last_failed_dev last_failed_step \
             last_hw_sleep max_hw_sleep total_hw_sleep; do
        [[ -r /sys/power/suspend_stats/$f ]] && \
            printf '  %-18s %s\n' "$f" "$(tr -d '\n' < /sys/power/suspend_stats/$f)"
    done
    if [[ $EUID -eq 0 ]]; then
        [[ -r /sys/kernel/debug/pmc_core/slp_s0_residency_usec ]] && \
            printf '  %-18s %s us\n' "S0ix residency" "$(< /sys/kernel/debug/pmc_core/slp_s0_residency_usec)"
        [[ -r /sys/kernel/debug/pmc_core/ltr_show ]] && \
            echo "  S0ix blockers   : /sys/kernel/debug/pmc_core/ltr_show, .../slp_s0_debug_status"
    else
        echo "  (root shows the S0ix residency and the pmc_core blocker lists)"
    fi

    echo
    echo "== ACPI wake sources enabled (/proc/acpi/wakeup)"
    awk '$3=="*enabled"{printf "  %-6s %-4s %s\n", $1, $2, $4}' /proc/acpi/wakeup 2>/dev/null ||
        echo "  (no /proc/acpi/wakeup)"

    echo
    echo "== device wakeup / runtime PM enabled right now"
    local d n
    for d in /sys/bus/pci/devices/*/power; do
        [[ -r $d/wakeup && $(< $d/wakeup) == enabled ]] || continue
        n=$(basename "$(dirname "$d")")
        printf '  pci  %-14s wakeup=enabled runtime=%s\n' "$n" "$(cat $d/runtime_status 2>/dev/null)"
    done
    for d in /sys/bus/usb/devices/*/power; do
        [[ -r $d/wakeup && $(< $d/wakeup) == enabled ]] || continue
        n=$(basename "$(dirname "$d")")
        printf '  usb  %-14s wakeup=enabled runtime=%-10s control=%s autosuspend=%s\n' \
            "$n" "$(cat $d/runtime_status 2>/dev/null)" "$(cat $d/control 2>/dev/null)" \
            "$(cat $d/autosuspend_delay_ms 2>/dev/null)"
    done

    echo
    echo "== suspend inhibitors"
    systemd-inhibit --list --no-pager 2>/dev/null | sed 's/^/  /' || echo "  (systemd-inhibit unavailable)"
}

# ---------------------------------------------------------------- report
# Globals set by cmd_run / cmd_suspend before calling emit_report.
LABEL=window
T0=0; T1=0; E0=0; E1=0; S0=0; S1=0; OK0=0; OK1=0; TZ0="?"; TZMAX="?"; NSAMPLES=0; BASIS=""

emit_report() {   # -> prints the block, appends to $OUT_FILE, returns 0 pass / 1 fail / 2 invalid
    local elapsed suspended awake de full hours basis rate_pct rate_w psys_w verdict rc=0
    elapsed=$(awk -v a="$T0" -v b="$T1" 'BEGIN{printf "%.1f", b-a}')
    suspended=$(awk -v a="$S0" -v b="$S1" 'BEGIN{printf "%.1f", (b-a)/1e9}')
    awake=$(awk -v a="$elapsed" -v b="$suspended" 'BEGIN{printf "%.1f", a-b}')
    de=$(awk -v a="$E0" -v b="$E1" 'BEGIN{printf "%.3f", a-b}')
    full=$(battery_full_wh)

    # Which hours belong to the rate: a real suspended interval if there was one (>= 5 s),
    # otherwise the awake time of the window.
    if [[ -n $BASIS ]]; then basis=$BASIS
    elif awk -v s="$suspended" 'BEGIN{exit !(s >= 5)}'; then basis=$suspended
    else basis=$awake; fi
    hours=$(awk -v s="$basis" 'BEGIN{printf "%.4f", s/3600}')
    rate_pct=$(awk -v de="$de" -v f="$full" -v h="$hours" \
        'BEGIN{ if (h > 0 && f > 0) printf "%.2f", (de/f)/h*100; else print "0"}')
    rate_w=$(awk -v de="$de" -v h="$hours" 'BEGIN{ if (h > 0) printf "%.2f", de/h; else print "0"}')
    if [[ -n $RAPL_PSYS && -r $RAPL_PSYS && -n ${P0:-} && -n ${P1:-} ]]; then
        psys_w=$(awk -v a="$P0" -v b="$P1" -v s="$elapsed" \
            'BEGIN{ if (s > 0) printf "%.2f", (b-a)/1e6/s; else print "n/a"}')
    else
        psys_w="n/a"
        [[ -r /run/rapl-power/package-0 ]] && psys_w="~$(< /run/rapl-power/package-0) (package-0, 1 s avg)"
    fi

    if on_ac; then
        verdict="INVALID: on AC, the battery is not discharging -- unplug and measure again"; rc=2
    elif awk -v r="$rate_pct" -v f="$FAIL_PCT_PER_H" 'BEGIN{exit !(r > f)}'; then
        verdict="FAIL: ${rate_pct} %/h is above the ${FAIL_PCT_PER_H} %/h limit"; rc=1
    elif awk -v r="$rate_pct" -v p="$PASS_PCT_PER_H" 'BEGIN{exit !(r > p)}'; then
        verdict="WARN: ${rate_pct} %/h (over the ${PASS_PCT_PER_H} %/h target, under the ${FAIL_PCT_PER_H} %/h limit)"; rc=1
    else
        verdict="PASS: ${rate_pct} %/h at or below the ${PASS_PCT_PER_H} %/h target"
    fi

    local text
    text=$(printf '%s label=%s elapsed=%ss suspended=%ss awake=%ss
  battery   %s -> %s Wh (%s Wh, -%.3f Wh) of %s Wh at %s%%   => %s %%/h, %s W
  counters  suspend success +%s, hw_sleep +%ss, RAPL soc %s W, %s temperature samples
  thermal   x86_pkg_temp %s -> %s C (max %s C)
  verdict   %s' \
        "$(date -Is)" "$LABEL" "$elapsed" "$suspended" "$awake" \
        "$E0" "$E1" "$de" "$de" "$full" "$(battery_pct)" "$rate_pct" "$rate_w" \
        "$(awk -v a="$OK0" -v b="$OK1" 'BEGIN{print b-a}')" "$suspended" "$psys_w" "$NSAMPLES" \
        "$TZ0" "$(pkg_temp)" "$TZMAX" "$verdict")

    echo "$text"
    [[ -n ${OUT_FILE:-} ]] && printf -- '--- %s\n' "$text" >> "$OUT_FILE"
    return $rc
}

# ---------------------------------------------------------------- windows
cmd_run() {
    local win=300
    while (( $# )); do
        case $1 in
            -w) win=$2; shift 2 ;;
            -l) LABEL=$2; shift 2 ;;
            -o) OUT_FILE=$2; shift 2 ;;
            *) usage ;;
        esac
    done
    [[ $LABEL == window ]] && LABEL="awake-${win}s"

    T0=$(date +%s); E0=$(battery_energy_wh) || die "no readable battery"
    S0=$(suspend_ns); OK0=$(suspend_ok); TZ0=$(pkg_temp); TZMAX=$TZ0
    while (( $(date +%s) - T0 < win )); do
        sleep "$SAMPLES_S"
        NSAMPLES=$((NSAMPLES + 1))
        TZMAX=$(temp_max "$TZMAX" "$(pkg_temp)")
    done
    T1=$(date +%s); E1=$(battery_energy_wh); S1=$(suspend_ns); OK1=$(suspend_ok)
    emit_report
}

cmd_suspend() {
    local win=300
    while (( $# )); do
        case $1 in
            -w) win=$2; shift 2 ;;
            -l) LABEL=$2; shift 2 ;;
            -o) OUT_FILE=$2; shift 2 ;;
            *) usage ;;
        esac
    done
    LABEL=${LABEL/window/s2idle}
    [[ $EUID -eq 0 ]] || die "suspend mode needs root: sudo $0 suspend -w $win"
    command -v rtcwake >/dev/null || die "rtcwake not found (util-linux)"
    # Measure whatever mode the kernel has active, and label the result: s2idle is
    # what we ship, but a machine that fell back to deep must still be measurable.
    mem_state=$(tr -d '\n' < /sys/power/mem_sleep)
    mem_mode=$(printf '%s\n' "$mem_state" | awk '{for(i=1;i<=NF;i++) if ($i ~ /^\[/) {gsub(/[][]/,"",$i); print $i}}')
    [ -n "$mem_mode" ] || die "/sys/power/mem_sleep is unreadable: $mem_state"
    if [ "$mem_mode" != s2idle ]; then
        echo "sleep-power-measure: active sleep mode is '$mem_mode' (mem_sleep: $mem_state), not s2idle." >&2
        echo "  measuring it anyway and labelling the result; make it s2idle with:" >&2
        echo "  echo s2idle | sudo tee /sys/power/mem_sleep   (or: sudo tools/oxp3-hw-install.sh --sleep)" >&2
    fi

    echo "== suspending for ${win}s via 'rtcwake -m mem' (s2idle, RTC alarm wakes it)" >&2
    T0=$(date +%s); E0=$(battery_energy_wh) || die "no readable battery"
    S0=$(suspend_ns); OK0=$(suspend_ok); TZ0=$(pkg_temp); TZMAX=$TZ0
    P0=$(rapl_uj "$RAPL_PSYS" || true); P1=""

    rtcwake -m mem -s "$win" -v 2>&1 | sed 's/^/   rtcwake: /' >&2
    local rtc=${PIPESTATUS[0]}
    (( rtc == 0 )) || { echo "rtcwake failed ($rtc): the machine refused to suspend" >&2; return 2; }

    # read immediately on resume: the point of this measurement is the sleeping hours
    T1=$(date +%s); E1=$(battery_energy_wh); S1=$(suspend_ns); OK1=$(suspend_ok)
    P1=$(rapl_uj "$RAPL_PSYS" || true); TZMAX=$(temp_max "$TZMAX" "$(pkg_temp)")
    emit_report
    local rc=$?

    echo "== settling ${RESUME_SETTLE_S}s, then measuring the awake cost of the same machine" >&2
    sleep "$RESUME_SETTLE_S"
    SAMPLES_S=5 cmd_run -w 30 -l "post-resume-awake" ${OUT_FILE:+-o "$OUT_FILE"} || true
    return $rc
}

# ---------------------------------------------------------------- main
cmd=${1:-}; [[ -n $cmd ]] || usage; shift
case $cmd in
    snapshot)       cmd_snapshot ;;
    audit)          cmd_audit ;;
    run)            cmd_run "$@" ;;
    suspend)        cmd_suspend "$@" ;;
    -h|--help|help) usage ;;
    *) usage ;;
esac
