#!/bin/bash
# oxp3-sagv.sh -- SAGV / QCLK-GV ("memory frequency point") status and measurement for the
# OneXPlayer 3 (Panther Lake, Arc B390, xe driver).
#
# Background and evidence: docs/hw/sagv.md. Short version: the Windows tool (PerfMode.exe +
# SAGVPlugin.dll) pins the SAGV/QCLK-GV point by writing an MMIO register pair in MCHBAR
# (PCI 0:0.0 config 0x48 + 0x5DA0 / 0x5DA4). Linux exposes NO interface for that: the only
# SAGV knob is the boot-time display parameter xe.enable_sagv (0400, read-only, "disable"),
# and the in-kernel QGV point selection is done by firmware via the PM DEMAND request.
#
# This tool is therefore read-only by design:
#   status              what the kernel/driver exposes right now (no root needed)
#   probe               exhaustive scan for any writable SAGV/QGV/QCLK interface
#   set <point|off|on>  never writes: prints the vendor register facts, the only real knob
#                       (boot parameter) and the exact privileged command, with --dry-run
#   measure             average memory (dram) / SoC (psys) / package power from /run/rapl-power
#   report              status + measure + verdict in one block
#
# Exit codes: 0 ok, 3 unavailable / refused (no interface), 4 usage.
set -u

PROG=${0##*/}
CARD_GLOB=/sys/class/drm/card[0-9]*
SAMPLE_S=30
INTERVAL=1
# where rapl-power publishes its 1 Hz averages (override for testing: SAGV_RAPL_DIR=/tmp/x)
RAPL_DIR=${SAGV_RAPL_DIR:-/run/rapl-power}

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 4; }
die()  { printf '%s: %s\n' "$PROG" "$*" >&2; exit 3; }
have() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ small readers
rd() { [ -r "$1" ] && cat "$1" 2>/dev/null; }
rd1() { rd "$1" | head -1; }

gpu_card() {
    local c
    for c in $CARD_GLOB; do
        [ -e "$c/device/driver" ] || continue
        local drv
        drv=$(basename "$(readlink -f "$c/device/driver")")
        case $drv in xe|i915) echo "$c"; return 0;; esac
    done
    return 1
}

gpu_driver() { basename "$(readlink -f "$1/device/driver")" 2>/dev/null; }

display_version() {
    # PTL: "Found pantherlake (device ID b080) integrated display version 30.00 stepping B0"
    have journalctl || return 0
    journalctl -k -b --no-pager 2>/dev/null \
        | sed -n 's/.*integrated display version \([0-9][0-9.]*\).*/\1/p' | head -1
}

# RAPL zones: name<TAB>path (first zone wins per name, MSR domain before MMIO: the glob order
# is intel-rapl:, then intel-rapl-mmio:, which is what we want)
rapl_zones() {
    local z name
    for z in /sys/class/powercap/*; do
        [ -r "$z/name" ] || continue
        name=$(cat "$z/name" 2>/dev/null) || continue
        printf '%s\t%s\n' "$name" "$z"
    done
}

rapl_energy_ok() { [ -r "$1/energy_uj" ] && cat "$1/energy_uj" >/dev/null 2>&1; }

# ------------------------------------------------------------------ status
cmd_status() {
    local card drv dv
    echo "== OXP3 SAGV / QCLK-GV status =="
    if card=$(gpu_card); then
        drv=$(gpu_driver "$card")
        printf 'GPU                : %s (%s), driver=%s\n' \
            "$card" "$(sed -n 's/^PCI_ID=//p' "$card/device/uevent" 2>/dev/null | head -1)" "$drv"
    else
        printf 'GPU                : no xe/i915 DRM card found\n'
    fi
    dv=$(display_version)
    [ -n "$dv" ] && printf 'display version    : %s (HAS_SAGV yes, MCHBAR mirror %s)\n' \
        "$dv" "$(awk -v v="$dv" 'BEGIN{print (v+0 >= 14) ? "absent (>=14)" : "present (<14)"}')"

    echo
    echo "-- what the kernel exposes for SAGV"
    local p=/sys/module/${drv:-xe}/parameters/enable_sagv
    if [ -e "$p" ]; then
        local mode val
        mode=$(stat -c %A "$p" 2>/dev/null)
        if val=$(cat "$p" 2>/dev/null); then
            printf '  %-42s %s = %s\n' "$p" "$mode" "$val"
        else
            printf '  %-42s %s (root-only, boot-time parameter)\n' "$p" "$mode"
        fi
        printf '  %-42s absent\n' "sysfs SAGV point / debugfs SAGV node"
        printf '  %-42s %s\n' "writable at runtime" "NO (0400 => read-only, cmdline only)"
    else
        printf '  %s: absent (no SAGV parameter on this driver)\n' "$p"
    fi

    if have mountpoint && mountpoint -q /sys/kernel/debug 2>/dev/null; then
        if [ -r /sys/kernel/debug/dri ]; then
            printf '  %-42s %s\n' "debugfs /sys/kernel/debug/dri" "readable (root)"
            ls /sys/kernel/debug/dri 2>/dev/null | tr '\n' ' ' | sed 's/^/      minors: /'
            echo
        else
            printf '  %-42s %s\n' "debugfs /sys/kernel/debug/dri" \
                "present but root-only (0700 /sys/kernel/debug)"
        fi
    else
        printf '  %-42s %s\n' "debugfs" "not mounted"
    fi

    echo
    echo "-- MCHBAR (where Windows writes the SAGV point)"
    local hb=/sys/bus/pci/devices/0000:00:00.0
    if [ -r "$hb/config" ]; then
        local n
        n=$(wc -c < "$hb/config" 2>/dev/null || echo 0)
        printf '  host bridge config : readable by user, first read returns %s bytes\n' "$n"
        if [ "${n:-0}" -ge 80 ]; then
            printf '  MCHBAR (cfg 0x48)  : %s\n' "$(od -An -tx8 -j 0x48 -N 8 "$hb/config" 2>/dev/null | tr -d ' ')"
        else
            printf '  MCHBAR (cfg 0x48)  : not readable (config read capped below 0x48)\n'
        fi
    else
        printf '  host bridge config : not readable\n'
    fi
    printf '  resource mapping   : %s\n' \
        "$(cat "$hb/resource" 2>/dev/null | awk '$2!="0x0000000000000000"{n++} END{print n+0" BAR(s) with a non-zero base"}')"

    echo
    echo "-- RAPL domains usable for measuring the effect"
    local name path pub
    while IFS=$'\t' read -r name path; do
        if [ -r "$RAPL_DIR/$name" ]; then
            pub="W=$(cat "$RAPL_DIR/$name" 2>/dev/null)"
        else
            pub="not published"
        fi
        if rapl_energy_ok "$path"; then
            printf '  %-10s %-34s energy readable, %-10s %s\n' \
                "$name" "$path" "$name" "$pub"
        else
            printf '  %-10s %-34s energy root-only, %-10s %s\n' \
                "$name" "$path" "$name" "$pub"
        fi
    done < <(rapl_zones | awk -F'\t' '!seen[$1]++')

    echo
    echo "-- adjacent (NOT SAGV) knobs"
    printf '  %-30s %s\n' "platform_profile" "$(rd1 /sys/firmware/acpi/platform_profile)"
    if card=$(gpu_card); then
        local g="$card/device/tile0/gt0/freq0"
        [ -d "$g" ] && printf '  %-30s min=%s max=%s cur=%s MHz (gt, root can write min/max)\n' \
            "GT frequency" "$(rd1 "$g/min_freq")" "$(rd1 "$g/max_freq")" "$(rd1 "$g/cur_freq")"
    fi
    local u=/sys/devices/system/cpu/intel_uncore_frequency
    if [ -d "$u" ]; then
        local d
        d=$(find "$u" -maxdepth 1 -name 'package_*' | head -1)
        [ -n "$d" ] && printf '  %-30s min=%s max=%s kHz (uncore ratio, MSR 0x620)\n' \
            "uncore frequency" "$(rd1 "$d/min_freq_khz")" "$(rd1 "$d/max_freq_khz")"
    fi
    printf '  %-30s %s\n' "AC online" "$(rd1 /sys/class/power_supply/A*/online)"
    printf '  %-30s %s%%\n' "battery" "$(rd1 /sys/class/power_supply/BAT*/capacity)"

    echo
    echo "verdict: SAGV point selection is firmware-managed on this kernel; the only SAGV control"
    echo "         is the boot-time display parameter ${drv:-xe}.enable_sagv (and it can only"
    echo "         disable SAGV). See docs/hw/sagv.md, and '$PROG probe' for the full scan."
}

# ------------------------------------------------------------------ probe
cmd_probe() {
    local card
    echo "== SAGV interface probe (read-only) =="
    echo
    echo "[1] filesystem scan: any node whose name mentions SAGV/QGV/QCLK/Geyserville"
    local hits
    hits=$(timeout 60 find /sys -maxdepth 7 \
        \( -iname '*sagv*' -o -iname '*qgv*' -o -iname '*qclk*' -o -iname '*geyserville*' \) \
        -print 2>/dev/null)
    if [ -n "$hits" ]; then echo "$hits" | sed 's/^/    /'
    else echo "    (none)"; fi

    echo
    echo "[2] module parameters (readable names even when values are root-only)"
    local m
    for m in /sys/module/xe/parameters /sys/module/i915/parameters; do
        [ -d "$m" ] || continue
        echo "    $m:"
        ls "$m" 2>/dev/null | grep -iE 'sagv|qgv|qclk|gt|power|dc|psr|fbc' | sed 's/^/      /'
    done

    echo
    echo "[3] debugfs (SAGV state would live here if it existed)"
    if [ -d /sys/kernel/debug ]; then
        if [ -r /sys/kernel/debug/dri ]; then
            echo "    /sys/kernel/debug/dri/<minor>/ (readable):"
            ls /sys/kernel/debug/dri/*/ 2>/dev/null | grep -iE 'sagv|qgv|wm|display_params|freq' \
                | sort -u | sed 's/^/      /'
        else
            echo "    /sys/kernel/debug is root-only (0700); source review shows no SAGV file is"
            echo "    registered anyway (see docs/hw/sagv.md §3). Verify as root with:"
            echo "      sudo ls /sys/kernel/debug/dri/*/ | grep -iE 'sagv|qgv'"
        fi
    else
        echo "    debugfs not mounted"
    fi

    echo
    echo "[4] PCI config space of the host bridge (MCHBAR lives at 0x48)"
    local hb=/sys/bus/pci/devices/0000:00:00.0
    if [ -r "$hb/config" ]; then
        echo "    vendor/device: $(od -An -tx2 -j 0 -N 4 "$hb/config" 2>/dev/null | tr -s ' ')"
        echo "    readable size: $(wc -c < "$hb/config" 2>/dev/null) bytes"
        echo "    MCHBAR read as root: sudo setpci -s 00:00.0 48.l  (low dword; 64-bit register)"
    else
        echo "    not readable"
    fi

    echo
    echo "[5] powercap domains (the measurement surface)"
    rapl_zones | awk -F'\t' '!seen[$1]++{printf "    %-12s %s\n", $1, $2}'

    echo
    echo "result: no writable SAGV/QGV/QCLK interface exists on this system."
    return 3
}

# ------------------------------------------------------------------ set
cmd_set() {
    local want=${1:-} dry=0
    [ $# -ge 1 ] || usage
    shift
    while [ $# -gt 0 ]; do
        case $1 in
            --dry-run|-n) dry=1;;
            *) usage;;
        esac
        shift
    done
    [ -n "$want" ] || usage

    local drv=xe card
    card=$(gpu_card) && drv=$(gpu_driver "$card")

    case $want in
        off|on)
            echo "SAGV '$want' is the ONE thing that is actually controllable -- but only at boot,"
            echo "by a driver parameter that is not writable at runtime (mode 0400)."
            echo
            echo "  effect      : off = firmware never drops the QCLK-GV point; PM DEMAND is sent"
            echo "                with the maximum bandwidth request (U16_MAX), so memory/SA stay at"
            echo "                the highest frequency. This RAISES idle power; it is useful as the"
            echo "                A/B reference when measuring what SAGV is worth, not as a tuning"
            echo "                knob for battery life."
            if [ "$want" = off ]; then
                echo "  command     : sudo rpm-ostree kargs --append-if-missing=xe.enable_sagv=0"
            else
                echo "  command     : sudo rpm-ostree kargs --delete-if-present=xe.enable_sagv=0"
            fi
            echo "  then        : reboot (kernel parameter), verify with: $PROG status"
            if [ "$dry" = 1 ]; then
                echo
                echo "  (dry run: nothing was written, nothing executed)"
                return 0
            fi
            echo
            echo "  exited without running it: this tool never changes the host."
            return 0
            ;;
    esac

    local point=$want
    case $want in point*) point=${want#point}; point=${point#=};; esac
    case $point in ''|*[!0-9]*) usage;; esac

    cat <<EOF
SAGV point $point: NOT SETTABLE on this system. Details:

 1. No kernel interface exists. On Linux the QCLK-GV (SAGV) point is chosen by firmware:
    the driver only sends a bandwidth requirement in the PM DEMAND request
    (XELPDP_INITIATE_PMDEMAND_REQUEST, 0x45230+4*dword, qclk_gv_bw = bits 31:16,
    qclk_gv_index = bits 11:8). In this kernel intel_pmdemand_atomic_check() hardcodes
    "firmware will calculate the qclk_gv_index" and intel_pcode_restrict_qgv_points()
    returns early for display version >= 14, so nothing pins a point.
    There is no sysfs/debugfs/module parameter for it (only xe.enable_sagv, boolean, 0400).

 2. What the Windows tool writes (recovered from SAGVPlugin.dll, see docs/hw/sagv.md):
      MCHBAR = PCI 0:0.0 config space 0x48 (64-bit, bit0 = enable)
      write MCHBAR+0x5DA0 = point (32-bit)          <- the "sagvPoint" of perf_conf.ini
      write MCHBAR+0x5DA4 = 0x80000122 (apply)      poll bit31 (busy) to clear, <=10 ms
      read  MCHBAR+0x5DA4 = 0x80000022 (latch) then read MCHBAR+0x5DA0 for the current point
      read  MCHBAR+0x5918 = SA_PERF_STATUS (QCLK ratio) for the memory frequency readout
    The driver does not map MCHBAR on display version >= 14 (the read-only MCHBAR mirror is
    gone), and /dev/mem is behind CONFIG_STRICT_DEVMEM / CONFIG_IO_STRICT_DEVMEM.

 3. A sysfs/debugfs write would only be possible after a kernel patch (documented, not
    shipped). The path this tool would use, if it existed:
      /sys/kernel/debug/dri/<minor>/sagv_force_point
EOF
    if [ "$dry" = 1 ]; then
        echo
        echo "Would write: 'echo $point > /sys/kernel/debug/dri/<minor>/sagv_force_point'"
        echo "(interface absent: dry run only, nothing written)"
        return 0
    fi
    echo
    echo "Refusing to write anything. Use '$PROG measure' to quantify SAGV instead, or see"
    echo "docs/hw/sagv.md §6 for what a kernel-side override would have to do."
    return 3
}

# ------------------------------------------------------------------ measure
cmd_measure() {
    local log=
    while [ $# -gt 0 ]; do
        case $1 in
            --seconds|-s) SAMPLE_S=${2:?seconds}; shift;;
            --interval|-i) INTERVAL=${2:?seconds}; shift;;
            *) usage;;
        esac
        shift
    done
    case $SAMPLE_S in ''|*[!0-9]*) usage;; esac
    [ "$SAMPLE_S" -ge 5 ] || die "need at least 5 s (got $SAMPLE_S)"

    local zones="package-0 core uncore dram psys"
    local z avail="" missing=""
    for z in $zones; do
        if [ -r "$RAPL_DIR/$z" ]; then avail="$avail $z"; else missing="$missing $z"; fi
    done
    if [ -z "$avail" ]; then
        echo "no RAPL samples found in $RAPL_DIR." >&2
        echo "rapl-power publishes package-0/core/uncore by default; dram and psys are needed" >&2
        echo "for memory/SoC power. Install/refresh it with:" >&2
        echo "  sudo install -m 0755 files/rapl-power /usr/local/libexec/rapl-power" >&2
        echo "  sudo systemctl restart rapl-power" >&2
        echo "or read the counters directly as root:" >&2
        echo "  sudo sh -c 'z=\$(cat /sys/class/powercap/intel-rapl:0/name); echo \$z'" >&2
        return 3
    fi
    [ -n "$missing" ] && echo "note: not published:$missing (see 'sudo install files/rapl-power')"

    echo "== SAGV effect measurement: ${SAMPLE_S}s on $(hostname), $(date -Is) =="
    echo "   scenario: $(rd1 /sys/class/power_supply/A*/online | sed 's/^1$/AC/;s/^0$/battery/') power, \
profile=$(rd1 /sys/firmware/acpi/platform_profile), \
battery=$(rd1 /sys/class/power_supply/BAT*/capacity)%"
    echo "   sampling $RAPL_DIR every ${INTERVAL}s; keep the machine in the same visual state."
    echo

    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-sagv.XXXXXX") || die "mktemp failed"

    local i
    local n=$(( SAMPLE_S / INTERVAL ))
    [ "$n" -ge 1 ] || n=1
    for i in $(seq 1 "$n"); do
        for z in $avail; do
            v=$(cat "$RAPL_DIR/$z" 2>/dev/null) || v=""
            [ -n "$v" ] && printf '%s\n' "$v" >> "$tmp/$z"
        done
        sleep "$INTERVAL"
    done

    printf '%-11s %8s %8s %8s %6s\n' domain avg_W min_W max_W n
    for z in $avail; do
        [ -s "$tmp/$z" ] || { printf '%-11s %8s\n' "$z" "-"; continue; }
        awk -v name="$z" '
            { s += $1; if (NR == 1 || $1 < mn) mn = $1; if (NR == 1 || $1 > mx) mx = $1 }
            END { printf "%-11s %8.2f %8.2f %8.2f %6d\n", name, s/NR, mn, mx, NR }
        ' "$tmp/$z"
    done

    echo
    echo "reading: dram ~= memory rail (what SAGV actually modulates), uncore ~= SoC fabric,"
    echo "psys ~= whole platform. Compare two runs in the SAME scenario:"
    echo "  A) SAGV on  (default)   B) SAGV off (xe.enable_sagv=0, reboot)"
    echo "B minus A at idle is the power SAGV is saving on this unit; it should come from dram"
    echo "and, to a smaller degree, uncore/psys. See docs/hw/sagv.md §5 for the full protocol."
    rm -rf "$tmp"
}

# ------------------------------------------------------------------ report
cmd_report() {
    cmd_status
    echo
    echo "== measurement =="
    if [ -r "$RAPL_DIR/dram" ] || [ -r "$RAPL_DIR/psys" ]; then
        cmd_measure --seconds 10
    else
        echo "$RAPL_DIR lacks dram/psys; run '$PROG measure' for the install hint."
    fi
}

case ${1:-status} in
    status)  cmd_status;;
    probe)   cmd_probe;;
    set)     shift; cmd_set "$@";;
    measure) shift; cmd_measure "$@";;
    report)  cmd_report;;
    -h|--help|help) usage;;
    *) usage;;
esac
