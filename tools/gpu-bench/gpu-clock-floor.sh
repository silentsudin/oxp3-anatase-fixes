#!/bin/bash
# SPDX-License-Identifier: MIT
# gpu-clock-floor.sh - the one root session: does a GPU frequency floor help, and does Smart CPU
# boost steal the GPU's power budget?
#
# The handoff (docs/GPU-PERF-GAPS.md T5/T6) lists two things nobody could measure without root:
# GT0 idles at 450-650 MHz and every recorded run has 1% lows at about half the mean; and the CPU
# and GPU share one PL1/PL2 budget, so the Smart CPU boost may be taking power from the GPU.
#
# Root does only the sysfs writes; the benchmark runs as the invoking user, because the harness
# needs that user's Proton, prefix and caches (running it as root looks for /root/.local/share/Steam
# and fails, which is what happened on the first attempt).
#
# Usage:  sudo tools/gpu-bench/gpu-clock-floor.sh [--floors "450 1200 1800 2300"] [--api d3d11]
set -uo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
floors="450 1200 1800 2300"; api=d3d11
while (( $# )); do
    case $1 in
        --floors) floors=${2:-}; shift 2 ;;
        --api) api=${2:-}; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
(( EUID == 0 )) || { echo "this writes /sys; run it with sudo (one session for all floors)" >&2; exit 1; }

# the person who called sudo owns the Steam install, the prefix and the caches
user=${SUDO_USER:-}
[[ -n $user && $user != root ]] || {
    echo "run this with sudo from your desktop session (SUDO_USER is empty: the benchmark needs your Proton and prefix)" >&2
    exit 1
}
home=$(getent passwd "$user" | cut -d: -f6)
uid=$(id -u "$user")
xrd=${OXP3_XDG_RUNTIME_DIR:-/run/user/$uid}
[[ -d $home ]] || { echo "no home directory for $user" >&2; exit 1; }
cache=${OXP3_GPU_BENCH_CACHE:-$home/.cache/oxp3-fixes/gpu-bench}

dev=${OXP3_GT_DEV:-/sys/class/drm/card1/device/tile0/gt0/freq0}
[[ -d $dev ]] || { echo "no $dev - is this the xe driver?" >&2; exit 1; }

num() { tr -dc '0-9' < "$1" 2>/dev/null | head -c 6; }
rundir=$(mktemp -d /tmp/oxp3-clock.XXXXXX) || { echo "cannot create a temp dir" >&2; exit 1; }
bout=$rundir/out
mkdir -p "$bout"
chown -R "$user":"$(id -gn "$user" 2>/dev/null || echo "$user")" "$rundir" 2>/dev/null || true
rm -rf /tmp/gpu-clock-floor 2>/dev/null || true   # root-owned leftovers from an earlier attempt
echo "samples:         $rundir"
orig_min=$(num "$dev/min_freq"); orig_min=${orig_min:-450}
orig_profile=$(tr -d "[]" < "$dev/power_profile" 2>/dev/null | awk '{print $1}'); orig_profile=${orig_profile:-base}

# can root actually write these? an unwritable control means the whole session would measure nothing
probe=$(mktemp); if echo "$orig_min" > "$dev/min_freq" 2>"$probe"; then min_writable=yes; else min_writable=no; fi
if echo "$orig_profile" > "$dev/power_profile" 2>>"$probe"; then prof_writable=yes; else prof_writable=no; fi
write_err=$(cat "$probe" 2>/dev/null | head -2); rm -f "$probe"

proton=${OXP3_PROTON:-"$home/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton"}
[[ -x $proton ]] || proton=$(ls -d "$home"/.local/share/Steam/steamapps/common/Proton\ */proton 2>/dev/null | head -1)
echo "user:            $user ($home)"
echo "GPU controls:    min_freq writable=$min_writable, power_profile writable=$prof_writable (now min=$orig_min profile=$orig_profile)"
[[ -n $write_err ]] && echo "                 write error: $write_err"
echo "proton:          ${proton:-<none found - the benchmark will fail>}"
[[ $min_writable == yes ]] || echo "NOTE: min_freq refuses writes; those runs will only report the clock, not a floor"

preflight() {
    local out
    out=$(sudo -u "$user" env HOME="$home" XDG_RUNTIME_DIR="$xrd" \
            timeout 30 gamescope --backend headless -W 320 -H 240 -r 60 -- true 2>&1)
    if grep -qE 'Unable to open wayland socket|Failed to initialize wlserver|XDG_RUNTIME_DIR is invalid' <<<"$out"; then
        echo "preflight:    gamescope cannot start headless with XDG_RUNTIME_DIR=$xrd" >&2
        echo "              ${out##*$'\n'}" >&2
        exit 1
    fi
    echo "preflight:    gamescope headless starts (XDG_RUNTIME_DIR=$xrd)"
}
preflight
restore() {
    echo "$orig_min" > "$dev/min_freq" 2>/dev/null || true
    echo "$orig_profile" > "$dev/power_profile" 2>/dev/null || true
    echo "restored min_freq=$orig_min power_profile=$orig_profile"
}
trap restore EXIT INT TERM
sample_loop() { # <tag>: 10 Hz act_freq / throttle / RAPL until this tag's benchmark exits
    local tag=$1 out=$rundir/$tag.log waited=0
    rm -f "$out" 2>/dev/null || true
    : > "$out" || return 0
    while ! pgrep -f "gravitymark.py run .*--label $tag" >/dev/null 2>&1; do
        sleep 0.2; waited=$((waited + 1))
        (( waited > 200 )) && return   # the run never started; nothing to sample
    done
    while pgrep -f "gravitymark.py run .*--label $tag" >/dev/null 2>&1; do
        printf '%s %s %s %s %s %s %s\n' "$(date +%s.%N)" \
            "$(num "$dev/act_freq")" \
            "$(tr ' ' '_' < "$dev/throttle/reasons" 2>/dev/null)" \
            "$(cat /run/rapl-power/package-0 2>/dev/null)" "$(cat /run/rapl-power/core 2>/dev/null)" \
            "$(cat /run/rapl-power/uncore 2>/dev/null)" "$(cat /run/rapl-power/dram 2>/dev/null)" >> "$out"
        sleep 0.1
    done
}

run_one() { # <tag> <min_freq> <profile>
    local tag=$1 floor=$2 profile=$3 out
    [[ $min_writable == yes ]] && { echo "$floor" > "$dev/min_freq" 2>/dev/null; }
    [[ $prof_writable == yes ]] && { echo "$profile" > "$dev/power_profile" 2>/dev/null; }
    echo "== $tag (min_freq=$floor, power_profile=$profile)"
    sample_loop "$tag" & local sp=$!
    out=$(sudo -u "$user" env HOME="$home" XDG_RUNTIME_DIR="$xrd" OXP3_GPU_BENCH_CACHE="$cache" \
            python3 "$here/tools/gpu-bench/gravitymark.py" run --api "$api" \
            --width 1280 --height 720 --label "$tag" --out "$bout" 2>&1 | tail -1)
    wait "$sp" 2>/dev/null
    echo "   $out"
}

for f in $floors; do run_one "floor-$f" "$f" base; done
if [[ $prof_writable == yes ]]; then
    run_one "profile-power_saving" "$orig_min" power_saving
else
    echo "== power_profile: not settable on this platform (write returned EINVAL: SLPC refuses the parameter),"
    echo "   so there is no second profile to compare - the floors above are the whole measurement."
fi

python3 - "$rundir" <<'PY'
import glob, statistics, sys
rundir = sys.argv[1]
print(f"{'tag':24s} {'mean MHz':>9s} {'p10':>5s} {'p90':>5s} {'gated':>9s} {'thrtl':>9s} {'pl2':>6s} {'pkg W':>7s} {'core W':>7s}")
for path in sorted(glob.glob(rundir + "/*.log")):
    name = path.rsplit("/", 1)[-1][:-4]
    freqs, gated, throttled, pl2, pkg, core = [], 0, 0, 0, [], []
    for line in open(path):
        p = line.split()
        if len(p) < 3:
            continue
        try:
            f = int(p[1])
        except ValueError:
            continue
        # act_freq reads 0 when the GT parks between frames; that is gating, not a low clock, so it
        # must not drag the mean or p10 down (it did in the first version of this table)
        if f == 0:
            gated += 1
        else:
            freqs.append(f)
        if p[2] not in ("none", "-", "0"):
            throttled += 1
            if "pl2" in p[2]:
                pl2 += 1
        for vals, idx in ((pkg, 3), (core, 4)):
            try: vals.append(float(p[idx]))
            except (ValueError, IndexError): pass
    n = len(freqs) + gated
    if not n:
        continue
    ordered = sorted(freqs) if freqs else [0]
    m = len(ordered)
    print(f"{name:24s} {statistics.fmean(freqs) if freqs else 0:9.0f} {ordered[m//10]:5d} "
          f"{ordered[min(m-1, m*9//10)]:5d} {100*gated/n:8.0f}% {100*throttled/n:8.0f}% "
          f"{100*pl2/n:5.0f}% {statistics.fmean(pkg) if pkg else 0:7.1f} {statistics.fmean(core) if core else 0:7.1f}")
PY
