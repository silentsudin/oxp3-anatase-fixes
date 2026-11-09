#!/bin/bash
# oxp3-verify - prove every OneXPlayer 3 hardware fix end-to-end, in one command.
#
# This is the post-reboot acceptance harness for the fixes in docs/INSTALL-HW.md:
# volume rocker, home button + LEDs, body RGB, suspend/resume policy, Bluetooth
# firmware, power envelope + fan + battery, speaker EQ, display HDR/ICC/vibrance,
# Mesa/XMX graphics and the Decky status plugin.
#
#   tools/oxp3-verify.sh                 read-only, no root: every check that can
#                                        be proven from sysfs, units, files,
#                                        journals and the shipped tools
#   tools/oxp3-verify.sh --full          also the functional tests that need root
#                                        or are invasive: volume tap/hold selftest,
#                                        one LED write frame, a real suspend and a
#                                        battery/power measurement
#   tools/oxp3-verify.sh --json          machine-readable report (same checks)
#   tools/oxp3-verify.sh --selftest      unit-test the parsing helpers against
#                                        synthetic fixtures (tests/verify/fixtures)
#   tools/oxp3-verify.sh -h              usage
#
# Default mode never writes: it only reads files, asks systemd/journalctl and
# runs the repo's own read-only tools. Scratch output goes to a private
# directory under $TMPDIR and is removed on exit.
#
# --full writes nothing persistent either, but it does change live state:
#   * one solid frame to the top RGB strip (zone 06), then re-applies
#     ~/.config/oxp3-rgb.toml when present (otherwise leaves that zone off)
#   * an awake battery/power measurement (60 s by default)
# Persistent configuration is changed only with --with-changes, which runs the
# two opt-in writes: activating the speaker EQ drop-in (tools/audio/eq-on.sh) and
# the saved fan-mode probe (fan.savemode), which snapshots and restores
# /etc/oxp3/power.json and pwm1_enable around the one write it needs.
#
# sleep.suspend is the ONLY check in this harness that suspends the machine. It
# needs three independent opt-ins, so a plain `--full` can never cost a session:
# --full, --with-changes and OXP3_ALLOW_SUSPEND=1. When they are met it runs one
# real s2idle cycle through tools/sleep-power-measure.sh suspend (rtcwake, default
# 60 s, override OXP3_VERIFY_SUSPEND_S) and requires suspend_stats/success to
# increment. Everything else, including sleep.trigger, is read-only.
#
# The deferred risks listed in docs/UNIT-TESTS.md section 5 are covered by
# read-only probes in the default mode (hid.node, led.sides, rgbd.timing,
# imu.iio, pwr.ac, pwr.order, fan.curvedup, bat.limit, inst.paths, inst.backups);
# fan.savemode
# is the only one that needs --full --with-changes.
#
# States and exit code:
#   PASS    the fix is present and the probe proved it
#   FAIL    the fix is installed but broken / the invariant does not hold
#   SKIP    not installed, not applicable here, no root, or nothing to observe
#           yet (e.g. no suspend attempt since boot); the hint says what to do
#   UNKNOWN readable, but this check cannot assert a value (e.g. only the
#           auto_null sink is visible, so "Dummy Output" cannot be told apart
#           from "no audio seat in this context")
# Exit: 0 no failures and at least one PASS, 1 any FAIL, 2 nothing passed
# (skips/unknown only), 3 usage or internal error (same convention as the other
# tools). PASS+SKIP is a normal 0: a laptop without the RGB daemon can still be
# healthy.
#
# Everything reusable is reused, never re-implemented:
#   tools/oxp3-status.sh --json        Mesa/XMX/sched/xe/RAPL indicators
#   tools/oxp3-powerctl.py status --json   powercap, fan, battery, profile
#   tools/sleep-power-measure.sh       audit, run, suspend
#   tools/oxp3-bt-firmware.sh --check  pinned-hash + this-boot firmware state
#   tools/oxp3-led.py                  status, selftest, one write frame
#   tools/oxp3-rgbd.py --check         keeper config + frame count
#   tools/oxp3-imu.py selftest         IMU / gyro to Steam
#   tools/display/display-mode.sh status   HDR, WCG, ICC, panel EDID
#   tools/display/vibrance.sh status   shader/vkBasalt state
#   tools/audio/eq_response.py         EQ model table
#   tools/check-working.sh             recorded working build (driver/PL1/xe)
# Every one of them is optional: a missing tool degrades its checks to SKIP with
# the exact install/repair command, it never aborts the run.
# Deliberately not parsed: tools/oxp3-hw-install.sh --status prints a human
# matrix, not a stable interface; the live files/units are inspected instead
# (see docs/VALIDATION.md, "Open points").
#
# Environment overrides:
#   OXP3_VERIFY_MEASURE_S   seconds for the --full awake measurement (60)
#   OXP3_VERIFY_SUSPEND_S   seconds for the one real suspend (60)
#   OXP3_ALLOW_SUSPEND      must be 1 to let sleep.suspend really suspend
#   OXP3_VERIFY_TIMEOUT     default per-tool timeout (20 s)
#
# See docs/VALIDATION.md for what each check proves, the expected healthy
# output and the fix->check mapping.
set -u
# Stable parsing and no __pycache__ anywhere: default mode is strictly read-only.
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

PROG=oxp3-verify
VERSION=1.0.0

self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
TOOLS_DIR=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 3
REPO=$(CDPATH= cd -- "$TOOLS_DIR/.." && pwd) || exit 3

FULL=0
WITH_CHANGES=0
JSON=0
SELFTEST=0
MODE_NAME=default
TOOL_TIMEOUT=${OXP3_VERIFY_TIMEOUT:-20}
case $TOOL_TIMEOUT in ''|*[!0-9]*) TOOL_TIMEOUT=20 ;; esac

WORK=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-verify.XXXXXX") || {
    echo "$PROG: cannot create a temporary directory" >&2
    exit 3
}
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT HUP INT TERM

# ------------------------------------------------------------------ small helpers
have() { command -v "$1" >/dev/null 2>&1; }
is_root() { [ "$(id -u 2>/dev/null || echo 1)" = 0 ]; }
read1() { # first line of a readable file, empty otherwise
    [ -r "$1" ] || return 1
    local v=""
    IFS= read -r v <"$1" || true
    printf '%s' "$v"
}
clean() { printf '%s' "$1" | tr '\r\n\t' '   '; }
num_eq() { awk -v a="${1:-x}" -v b="${2:-y}" 'BEGIN{exit !(a+0 == b+0)}'; }
num_gt() { awk -v a="${1:-0}" -v b="${2:-0}" 'BEGIN{exit !(a+0 > b+0)}'; }
last_line() { [ -f "$1" ] && tail -n 1 "$1" 2>/dev/null || true; }
tail_flat() { [ -f "$1" ] && tail -n "${2:-2}" "$1" 2>/dev/null | tr '\n' ' ' || true; }

strip_ansi() { sed 's/\x1b\[[0-9;]*[A-Za-z]//g'; }

run_tool() { # NAME TIMEOUT CMD...
    local name=$1 secs=$2
    shift 2
    RUN_OUT=$WORK/$name.out
    if have timeout; then
        timeout "$secs" "$@" >"$RUN_OUT" 2>&1
    else
        "$@" >"$RUN_OUT" 2>&1
    fi
    RUN_RC=$?
    return 0
}

unit_state() { # UNIT [--user] -> "LoadState|ActiveState|UnitFileState"
    local unit=$1 out load active file
    shift
    out=$(systemctl "$@" show -p LoadState -p ActiveState -p UnitFileState "$unit" 2>/dev/null) || out=""
    load=$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p')
    active=$(printf '%s\n' "$out" | sed -n 's/^ActiveState=//p')
    file=$(printf '%s\n' "$out" | sed -n 's/^UnitFileState=//p')
    printf '%s|%s|%s' "${load:-?}" "${active:-?}" "${file:--}"
}

journal_text() { # UNIT [journalctl flags]
    local unit=$1
    shift
    journalctl "$@" -u "$unit" -b --no-pager -o cat 2>/dev/null | head -n 400
}

json_ok() { # FILE
    have python3 || return 3
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" >/dev/null 2>&1
}

json_get() { # FILE dotted.path
    have python3 || return 3
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        cur = json.load(fh)
except Exception:
    sys.exit(3)
for part in sys.argv[2].split("."):
    if isinstance(cur, list):
        try:
            cur = cur[int(part)]
        except (ValueError, IndexError):
            sys.exit(4)
    elif isinstance(cur, dict):
        if part not in cur:
            sys.exit(4)
        cur = cur[part]
    else:
        sys.exit(4)
if cur is None:
    print("")
elif isinstance(cur, bool):
    print("true" if cur else "false")
elif isinstance(cur, (int, float)):
    print(cur)
else:
    print(str(cur).replace("\n", " ").replace("\t", " "))
PY
}

# ------------------------------------------------------------------ parsers
# These are the units under `--selftest`; each reads text on stdin (or one file
# argument) and prints KEY=VALUE lines so the checks stay plain shell.

parse_display_status() { # stdin: tools/display/display-mode.sh status
    strip_ansi | awk '
        /^[[:space:]]*Output:[[:space:]]/ {
            line = $0; sub(/^[[:space:]]*/, "", line); split(line, a, /[[:space:]]+/); out = a[3]
        }
        /^[[:space:]]*HDR:[[:space:]]/ {
            line = $0; sub(/^[[:space:]]*/, "", line); split(line, a, /[[:space:]]+/); hdr = a[2]
        }
        /^[[:space:]]*Wide Color Gamut:[[:space:]]/ {
            line = $0; sub(/^[[:space:]]*/, "", line); split(line, a, /[[:space:]]+/); wcg = a[4]
        }
        /^[[:space:]]*ICC profile:[[:space:]]/ {
            line = $0; sub(/^[[:space:]]*/, "", line); split(line, a, /[[:space:]]+/); icc = a[3]
        }
        /^[[:space:]]*Color resolution:[[:space:]]/ {
            line = $0; if (match(line, /\(([0-9]+)\)/)) depth = substr(line, RSTART + 1, RLENGTH - 2)
        }
        /^[[:space:]]*Vrr:[[:space:]]/ {
            line = $0; sub(/^[[:space:]]*/, "", line); split(line, a, /[[:space:]]+/); vrr = a[2]
        }
        END { printf "OUTPUT=%s\nHDR=%s\nWCG=%s\nICC=%s\nDEPTH=%s\nVRR=%s\n", out, hdr, wcg, icc, depth, vrr }
    '
}

aspm_selected() { # stdin: /sys/module/pcie_aspm/parameters/policy content
    # The selected value is bracketed ("default [powersave] powersupersave");
    # a kernel that prints one word selects that word.
    awk '{
        sel = ""
        for (i = 1; i <= NF; i++)
            if ($i ~ /^\[/) { gsub(/[\[\]]/, "", $i); sel = $i }
        if (sel == "") sel = $1
        print sel
        exit
    }'
}

parse_suspend_stats() { # DIR
    local d=$1
    printf 'SUCCESS=%s\nFAIL=%s\nLAST_HW_SLEEP=%s\nTOTAL_HW_SLEEP=%s\nMAX_HW_SLEEP=%s\n' \
        "$(read1 "$d/success" || true)" "$(read1 "$d/fail" || true)" \
        "$(read1 "$d/last_hw_sleep" || true)" "$(read1 "$d/total_hw_sleep" || true)" \
        "$(read1 "$d/max_hw_sleep" || true)"
}

# What the suspend_stats tuple can and cannot support. Pure function over the
# numbers so --selftest can pin every branch without a host
# (docs/hw/sleep-power.md 2.6):
#   SUCCESS FAIL LAST MAX -> keyword
# SUCCESS/FAIL empty = unreadable. LAST/MAX are empty when the kernel does not
# expose the hardware-sleep attributes at all (they exist only when the ACPI FADT
# advertises Low-Power S0). All three hw_* attributes are MICROSECONDS, not ns.
# `max_hw_sleep` is the platform PMC's reporting ceiling (set at probe), never a
# measurement - so success=0 with a non-zero max is coherent, not an anomaly to
# PASS or FAIL on.
suspend_stats_verdict() {
    local success=$1 fail=$2 last=$3 max=$4
    if [ -z "$success" ] || [ -z "$fail" ]; then
        printf 'unreadable'
    elif num_gt "$fail" 0; then
        printf 'fail'
    elif num_gt "$success" 0; then
        if [ -z "$last" ]; then
            printf 'pass-nohw'
        elif num_gt "$last" 0; then
            printf 'pass'
        else
            printf 'unknown-hwzero'
        fi
    elif [ -n "$last" ] && num_gt "$last" 0; then
        printf 'unknown-last'
    elif [ -n "$max" ] && num_gt "$max" 0; then
        printf 'unknown-ceiling'
    else
        printf 'skip'
    fi
}

parse_logind_handles() { # stdin: systemd-analyze cat-config systemd/logind.conf
    # cat-config prints the compiled defaults as "#HandlePowerKey=poweroff" and the
    # local drop-in values uncommented, so both layers come out of one stream. A key
    # that only appears as a default is still reported: the check needs to say what a
    # bare power key would do *without* our drop-in (it is poweroff, not suspend).
    awk '
        function slot(k) {
            if (k == "HandlePowerKey") return "POWER"
            if (k == "HandleSuspendKey") return "SUSPEND"
            if (k == "HandleLidSwitch") return "LID"
            if (k == "HandleLidSwitchExternalPower") return "LID_EXT"
            if (k == "HandleLidSwitchDocked") return "LID_DOCK"
            if (k == "HandleHibernateKey") return "HIBERNATE"
            if (k == "HandlePowerKeyLongPress") return "POWER_LONG"
            return ""
        }
        /^[[:space:]]*\[/ {
            s = $0; gsub(/[][[:space:]]/, "", s); sect = s; next
        }
        {
            line = $0; wasdef = 0
            if (line ~ /^[[:space:]]*#/) { wasdef = 1; sub(/^[[:space:]]*#[[:space:]]*/, "", line) }
            sub(/^[[:space:]]+/, "", line); sub(/[[:space:]]+$/, "", line)
            if (sect != "Login") next
            i = index(line, "=")
            if (!i) next
            k = substr(line, 1, i - 1); v = substr(line, i + 1)
            name = slot(k)
            if (name == "") next
            if (wasdef) { if (!(name in over)) dflt[name] = v; next }
            over[name] = 1; cur[name] = v
        }
        END {
            n = split("POWER SUSPEND LID LID_EXT LID_DOCK HIBERNATE POWER_LONG", names, " ")
            for (i = 1; i <= n; i++) {
                nm = names[i]
                v = (nm in cur) ? cur[nm] : ((nm in dflt) ? dflt[nm] : "?")
                printf "%s=%s\n", nm, v
            }
            printf "OVERRIDES=%d\n", length(over)
        }
    '
}

parse_inhibitors() { # stdin: systemd-inhibit --list
    # Columns are WHO UID USER PID COMM WHAT WHY MODE. WHAT never contains a space
    # ("sleep" or "handle-power-key:handle-lid-switch"), WHY does, so WHAT is field 6
    # and MODE is the last field. Only a "block" inhibitor on "sleep" can refuse a
    # suspend; handle-* blocks only stop logind's own event handling.
    awk '
        NR == 1 { next }
        NF < 7 { next }
        {
            mode = $NF
            if (mode != "block" && mode != "delay") next
            what = $6; who = $1
            total++
            if (what ~ /(^|:)sleep(:|$)/) {
                if (mode == "block") { bs++; bwho = bwho " " who }
                else ds++
            }
            if (mode == "block" && what ~ /handle-/) { bh++; hwho = hwho " " who }
        }
        END {
            printf "TOTAL=%d\nBLOCK_SLEEP=%d\nBLOCK_HANDLES=%d\nDELAY_SLEEP=%d\nSLEEP_WHO=%s\nHANDLE_WHO=%s\n",
                total + 0, bs + 0, bh + 0, ds + 0, bwho, hwho
        }
    '
}

parse_polkit_rule() { # stdin: a .rules file (JSON-ish polkit JS)
    # Comment lines are skipped: the shipped rule *names* suspend-ignore-inhibit in
    # its explanation of what it does not grant, and that must not count as a grant.
    awk '
        {
            line = $0; sub(/^[[:space:]]+/, "", line)
            if (line ~ /^\*/ || line ~ /^\/\*/ || line ~ /^\/\//) next
        }
        /polkit\.addRule/ { rule++ }
        /org\.freedesktop\.login1\.(suspend|hibernate)/ { ids++ }
        /suspend-ignore-inhibit/ { ignore++ }
        /subject\.local/ { localg++ }
        /subject\.active/ { activeg++ }
        /polkit\.Result\.YES/ { yes++ }
        END {
            printf "RULE=%d\nIDS=%d\nIGNORE_INHIBIT=%d\nLOCAL_GUARD=%d\nACTIVE_GUARD=%d\nYES=%d\n",
                rule + 0, ids + 0, ignore + 0, localg + 0, activeg + 0, yes + 0
        }
    '
}

pkcheck_rc_name() { # exit status of pkcheck --action-id ... --process PID
    case ${1:-} in
        0) printf yes ;;
        1) printf no ;;
        2) printf challenge ;;
        '') printf missing ;;
        *) printf error ;;
    esac
}

pkcheck_verdict() { # ACTION-ID -> yes|no|challenge|missing|error (never suspends)
    have pkcheck || { printf missing; return 0; }
    local rc=0
    pkcheck --action-id "$1" --process $$ >/dev/null 2>&1 || rc=$?
    pkcheck_rc_name "$rc"
}

parse_power_domains() { # FILE: oxp3-powerctl status --json -> "kind<TAB>pl1<TAB>pl2<TAB>pl4"
    have python3 || return 3
    python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(3)
for dom in data.get("domains") or []:
    c = dom.get("constraints") or {}
    def lim(k):
        v = (c.get(k) or {}).get("limit_uw")
        return "" if v is None else str(v)
    print("%s\t%s\t%s\t%s" % (dom.get("kind") or "?", lim("pl1"), lim("pl2"), lim("pl4")))
PY
}

parse_fan() { # FILE: oxp3-powerctl status --json
    local f=$1
    printf 'PWM=%s\nPWM_ENABLE=%s\nRPM=%s\nMODE=%s\n' \
        "$(json_get "$f" fan.pwm)" "$(json_get "$f" fan.pwm_enable)" \
        "$(json_get "$f" fan.rpm)" "$(json_get "$f" fan.mode)"
}

parse_bt_check() { # stdin: tools/oxp3-bt-firmware.sh --check
    awk '
        /^\[ok\] ibt-/ { ok++ }
        /^\[warn\] ibt-/ { bad++ }
        /Failed to load/ { journalfail = 1 }
        /no firmware-load failure in this boot/ { journalfail = 0 }
        /^\[ok\] radio looks healthy/ { verdict = "healthy" }
        /^\[warn\] action needed/ { verdict = "action" }
        END {
            printf "FILES_OK=%d\nFILES_BAD=%d\nJOURNAL_FAIL=%d\nVERDICT=%s\n",
                ok + 0, bad + 0, journalfail + 0, verdict
        }
    '
}

parse_led_status() { # stdin: tools/oxp3-led.py status
    awk '
        /^vendor interface[[:space:]]*:/ {
            line = $0; sub(/.*:[[:space:]]*/, "", line); node = line
        }
        /^permissions[[:space:]]*:/ {
            line = $0; sub(/.*:[[:space:]]*/, "", line); perm = line
        }
        /^zones \(side ids\)[[:space:]]*:/ { zones = 1 }
        /^presets[[:space:]]*:/ { presets = 1 }
        END { printf "NODE=%s\nPERM=%s\nZONES=%d\nPRESETS=%d\n", node, perm, zones + 0, presets + 0 }
    '
}

parse_rgbd_check() { # stdin: tools/oxp3-rgbd.py --check
    awk '
        /^device[[:space:]]*:/ {
            line = $0; sub(/^[^:]*:[[:space:]]*/, "", line); dev = line
        }
        /^journal[[:space:]]*:/ {
            line = $0; sub(/^[^:]*:[[:space:]]*/, "", line)
            journal = (line ~ /^readable/) ? "readable" : "fallback"
        }
        /^config[[:space:]]*:/ {
            line = $0; sub(/^[^:]*:[[:space:]]*/, "", line); cfg = line
        }
        /^frames[[:space:]]*:/ {
            line = $0; sub(/^[^:]*:[[:space:]]*/, "", line); split(line, a, /[[:space:]]+/); frames = a[1]
        }
        /^[[:space:]]*zone / { zones++ }
        END {
            printf "DEVICE=%s\nJOURNAL=%s\nCONFIG=%s\nFRAMES=%s\nZONES=%d\n",
                dev, journal, cfg, frames, zones + 0
        }
    '
}

parse_audio_sinks() { # stdin: pactl list short sinks
    awk '{
        n++
        name = $2
        if (name ~ /(auto_null|dummy|null)/) dummy = 1
        else real = 1
    }
    END { printf "SINKS=%d\nREAL=%d\nDUMMY=%d\n", n + 0, real + 0, dummy + 0 }'
}

parse_level_report() { # stdin: tools/audio/level-report.py --json
    local code
    code=$(cat <<'PY'
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(3)
controls = d.get("controls") or []
c = controls[0] if controls else {}
eq = d.get("eq") or {}
sink = d.get("sink") or {}

def flag(v):
    return "yes" if v else "no"

def num(v, fmt):
    return "" if v is None or isinstance(v, bool) else fmt % v

print("CONTROL=%s" % (d.get("control") or ""))
print("VALUE=%s" % ("" if c.get("value") is None else c["value"]))
print("MAX=%s" % ("" if c.get("max") is None else c["max"]))
print("PERCENT=%s" % ("" if c.get("percent") is None else c["percent"]))
print("ALSA_DB=%s" % num(d.get("alsa_db"), "%.2f"))
print("AT_MAX=%s" % flag(d.get("at_max")))
print("PROFILE=%s" % (eq.get("profile") or ""))
print("EQ_INSTALLED=%s" % flag(eq.get("installed")))
print("HEADROOM_DB=%s" % num(eq.get("headroom_db"), "%.1f"))
print("NET_DB=%s" % num(d.get("net_db"), "%.2f"))
print("NET_VS_OLD_DB=%s" % num(d.get("net_vs_old_db"), "%.2f"))
print("CLIP_MARGIN_DB=%s" % num(eq.get("clip_margin_db"), "%.2f"))
print("SINK_PCT=%s" % ("" if sink.get("percent") is None else sink["percent"]))
print("SINK_MUTED=%s" % flag(sink.get("muted")))
PY
)
    python3 -c "$code"
}

parse_preflight_summary() { # stdin: tools/oxp3-preflight.sh --json
    local code
    code=$(cat <<'PY'
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(3)
counts = data.get("counts") or {}
skipped = data.get("skipped_components") or []
safe = data.get("safe")
print("SAFE=%s" % ("true" if safe is True else "false"))
print("VERDICT=%s" % (data.get("verdict") or "?"))
print("FAIL=%s" % (counts.get("fail", "?")))
print("WARN=%s" % (counts.get("warn", "?")))
print("OK=%s" % (counts.get("ok", "?")))
print("TOTAL=%s" % (counts.get("total", "?")))
print("SKIPPED=%s" % (", ".join(str(x) for x in skipped) if skipped else "-"))
PY
)
    python3 -c "$code"
}

parse_vol_journal() { # stdin: journalctl -u oxp3-voltap
    grep 'grabbed /dev/input/event' 2>/dev/null \
        | grep -v 'for probing' \
        | sed -n 's/.*grabbed \(\/dev\/input\/event[0-9]*\).*/\1/p' \
        | tail -n 1
}

parse_voltopology() { # stdin: oxp3-voltap check --kv
    awk '
        {
            i = index($0, "=")
            if (i) {
                key = substr($0, 1, i - 1)
                if (key ~ /^[A-Z_]+$/) value[key] = substr($0, i + 1)
            }
        }
        END {
            printf "TOPO=%s\nSEL=%s\nSEL_ROLE=%s\nSEL_HELD=%s\n" \
                   "SEL_REASON=%s\nSEL_SOURCE=%s\nPREDICTED_EXIT=%s\n" \
                   "HHD_RUNNING=%s\nHHD_PROCESS=%s\nHHD_VIRTUAL_PRESENT=%s\n" \
                   "HHD_PID=%s\nHHD_VIRTUAL=%s\nATKBD=%s\nATKBD_HELD=%s\n" \
                   "HOLDERS_KNOWN=%s\nCANDIDATES=%s\n" \
                   "ALLOW_HHD_GRAB=%s\nHHD_PROTECTED=%s\nOBSERVE=%s\n", \
                value["TOPO"], value["SEL"], value["SEL_ROLE"], \
                value["SEL_HELD"], value["SEL_REASON"], value["SEL_SOURCE"], \
                value["PREDICTED_EXIT"], value["HHD_RUNNING"], \
                value["HHD_PROCESS"], value["HHD_VIRTUAL_PRESENT"], \
                value["HHD_PID"], value["HHD_VIRTUAL"], value["ATKBD"], \
                value["ATKBD_HELD"], value["HOLDERS_KNOWN"], \
                value["CANDIDATES"], value["ALLOW_HHD_GRAB"], \
                value["HHD_PROTECTED"], value["OBSERVE"]
        }
    '
}

parse_hhd_journal() { # stdin: journalctl -u hhd
    # The outage signature: hhd could not open/grab its own volume keyboard and
    # then assumed the controllers disconnected.  A later successful
    # initialization means the episode was recovered (the boot keeps history).
    awk '
        /Device or resource busy|Assuming controllers disconnected/ {
            busy++
            last = $0
            last_bad = NR
            next
        }
        /Initialization Complete!|Loaded the following plugins|Emulated controller launched/ {
            last_ok = NR
        }
        END {
            recovered = (busy > 0 && last_ok > last_bad) ? 1 : 0
            printf "BUSY=%d\nRECOVERED=%d\nLAST=%s\n", busy + 0, recovered, last
        }
    '
}

parse_homebtn_journal() { # stdin: journalctl --user -u oxp3-homebtn
    awk '
        /listening on \/dev\/hidraw/ { listen = 1 }
        /button 0x[0-9a-fA-F]+ pressed/ { press++ }
        END { printf "LISTEN=%d\nPRESS=%d\n", listen + 0, press + 0 }
    '
}

parse_lifecycle_journal() { # stdin: this boot's journal text of one daemon
    awk '
        # A heartbeat also contains "listening on"; it is neither a reopen nor
        # a recovery and must not pair with a preceding loss.
        /watchdog:/ { watchdog++; next }
        /listening on \/dev\/hidraw/ {
            open++
            if (pending) { recovered++; pending = 0 }
            next
        }
        /grabbed \/dev\/input\/event/ {
            open++
            if (pending) { recovered++; pending = 0 }
            next
        }
        /applied [0-9]+ frame\(s\) to \/dev\/hidraw/ {
            open++
            if (pending) { recovered++; pending = 0 }
            next
        }
        /device lost:/ { lost++; pending = 1; next }
        /reconnect #[0-9]+/ { recon++; next }
        /resume detected:/ { resume++ }
        /uinput: (recreated|unavailable)/ { uinput++ }
        END {
            printf "OPEN=%d\nLOST=%d\nRECONNECT=%d\nRECOVERED=%d\nRESUME=%d\nWATCHDOG=%d\nUINPUT=%d\n",
                open + 0, lost + 0, recon + 0, recovered + 0, resume + 0, watchdog + 0, uinput + 0
        }
    '
}

parse_fd_report() { # stdin: "unit<TAB>pid<TAB>state<TAB>denied<TAB>fds"
    # vol_observe=1 means the volume daemon logged the documented OBSERVE state,
    # so it legitimately holds no input fd (the supported hhd path).
    awk -F'\t' -v vol_observe="${VOLTAP_OBSERVE:-0}" '
        function needed(u) {
            if (u == "oxp3-voltap.service") return "input"
            if (u == "oxp3-homebtn.service") return "hidraw"
            return ""
        }
        {
            unit = $1; pid = $2; state = $3; denied = $4; fds = $5
            if (state != "active") next
            active++
            detail = detail sprintf("%s pid=%s fds=%s; ", unit, pid, (fds == "" ? "-" : fds))
            need = needed(unit)
            if (need == "" && unit == "oxp3-rgbd.service") { perapply++; next }
            # The volume daemon is allowed to hold no input fd when it is in
            # observable degraded/observe mode (hhd owns the volume path).
            if (unit == "oxp3-voltap.service" && vol_observe == 1) { volskip++; next }
            if (denied == "1") { denied_n++; next }
            checked++
            found = (need == "input") ? (fds ~ /\/dev\/input\/event/) : (fds ~ /\/dev\/hidraw/)
            if (found) ok++
            else missing = missing sprintf("%s(needs %s) ", unit, need)
        }
        END {
            if (active == 0) verdict = "skip"
            else if (checked == 0 && denied_n == 0) verdict = "skip"
            else if (checked > 0 && ok == checked) verdict = "pass"
            else verdict = "fail"
            printf "ACTIVE=%d\nCHECKED=%d\nDENIED=%d\nPERAPPLY=%d\nVOLSKIP=%d\nOK=%d\nVERDICT=%s\nWHY=%s\nMISSING=%s\n",
                active + 0, checked + 0, denied_n + 0, perapply + 0, volskip + 0, ok + 0, verdict, detail, missing
        }
    '
}

daemon_fd_lines() { # UNIT USER(0|1) -> fd report line
    local unit=$1 user=$2 st load active file pid=0 fds="" denied=0 l target
    if [ "$user" = 1 ]; then st=$(unit_state "$unit" --user); else st=$(unit_state "$unit"); fi
    IFS='|' read -r load active file <<<"$st"
    if [ "${active:-?}" != active ]; then
        printf '%s\t-\t%s\t0\t-\n' "$unit" "${active:-?}"
        return 0
    fi
    if [ "$user" = 1 ]; then
        pid=$(systemctl --user show -p MainPID --value "$unit" 2>/dev/null)
    else
        pid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null)
    fi
    case ${pid:-} in ''|*[!0-9]*) pid=0 ;; esac
    if [ "$pid" -eq 0 ] || [ ! -d "/proc/$pid/fd" ] || ! ls "/proc/$pid/fd" >/dev/null 2>&1; then
        denied=1
    else
        for l in "/proc/$pid/fd"/*; do
            target=$(readlink "$l" 2>/dev/null) || continue
            case $target in
                /dev/hidraw*|/dev/input/event*|/dev/uinput)
                    case ",$fds," in
                        *",$target,"*) ;;
                        *) fds="${fds:+$fds,}$target" ;;
                    esac
                    ;;
            esac
        done
    fi
    printf '%s\t%s\tactive\t%s\t%s\n' "$unit" "$pid" "$denied" "${fds:--}"
}

parse_sleep_audit() { # stdin: tools/sleep-power-measure.sh audit
    awk '
        /^mem_sleep[[:space:]]*:/ {
            line = $0; sub(/^[^:]*:[[:space:]]*/, "", line); mem = line
        }
        /kernel suspend record/ { record = 1 }
        /device wakeup \/ runtime PM/ { wake = 1 }
        END { printf "MEM=%s\nRECORD=%d\nWAKE_TABLE=%d\n", mem, record + 0, wake + 0 }
    '
}

# --- deferred-risk probes (docs/UNIT-TESTS.md section 5) --------------------
# Each of these is a pure function over a fixture so --selftest can pin every
# branch, including the SKIP paths; the checks below only gather the real input.

hid_vendor_node() { # DEVICE dir with a uevent file; true for 1a86:fe00
    awk -F'[=:]' '
        /^HID_ID=/ { v = $3; p = $4; sub(/^0+/, "", v); sub(/^0+/, "", p)
                     if (toupper(v) == "1A86" && toupper(p) == "FE00") found = 1
                     exit }
        END { exit !found }
    ' "$1/uevent" 2>/dev/null
}

hid_report() { # DIR (default /sys/class/hidraw) -> candidate map
    # Every 1a86:fe00 hidraw node without an input child, and whether its
    # report descriptor advertises usage page 0xFF00 (HID item 06 00 ff).
    local root=${1:-/sys/class/hidraw} d node hex total=0 vendor=0 nodes="" vnodes=""
    for d in "$root"/hidraw*; do
        [ -d "$d/device" ] || continue
        hid_vendor_node "$d/device" || continue
        [ -d "$d/device/input" ] && continue
        node=/dev/$(basename "$d")
        hex=$(od -An -v -tx1 "$d/device/report_descriptor" 2>/dev/null | tr -d ' \n')
        total=$((total + 1)); nodes="$nodes $node"
        case $hex in
            *0600ff*) vendor=$((vendor + 1)); vnodes="$vnodes $node" ;;
        esac
    done
    printf 'TOTAL=%d\nVENDOR=%d\nNODES=%s\nVENDOR_NODES=%s\n' \
        "$total" "$vendor" "${nodes# }" "${vnodes# }"
}

parse_hid_verdict() { # stdin: SELECTED, SEL_VENDOR + hid_report
    local selected="" sel_vendor=0 total=0 vendor=0 vnodes="" key val verdict why
    while IFS='=' read -r key val; do
        case $key in
            SELECTED) selected=$val ;;
            SEL_VENDOR) sel_vendor=$val ;;
            TOTAL) total=$val ;;
            VENDOR) vendor=$val ;;
            VENDOR_NODES) vnodes=$val ;;
        esac
    done
    case $total in ''|*[!0-9]*) total=0 ;; esac
    case $vendor in ''|*[!0-9]*) vendor=0 ;; esac
    if [ "$total" -eq 0 ]; then
        verdict=skip
        why="no 1a86:fe00 hidraw node without an input child (device absent or the rule/install is missing)"
    elif [ -z "$selected" ]; then
        verdict=fail
        why="oxp3_hid.find_vendor_hidraw() returned nothing although $total candidate node(s) exist"
    elif [ "$sel_vendor" = 1 ]; then
        verdict=pass
        if [ "$vendor" -gt 1 ]; then
            why="selected $selected advertises usage page 0xFF00; $vendor candidates do, so the first-match choice is ambiguous"
        else
            why="selected $selected is the only candidate and advertises usage page 0xFF00"
        fi
    elif [ "$vendor" -gt 0 ]; then
        verdict=fail
        why="selected $selected does not advertise usage page 0xFF00, but$vnodes does: the descriptor is ignored"
    else
        verdict=unknown
        why="no candidate descriptor advertises usage page 0xFF00; decide with od -An -tx1"
    fi
    printf 'VERDICT=%s\nWHY=%s\nTOTAL=%d\nVENDOR=%d\n' "$verdict" "$why" "$total" "$vendor"
}

parse_dry_frames() { # stdin: oxp3-led.py --dry-run output
    awk '
        /^dry-run / { dry++ }
        /^send / { send++ }
        END { printf "FRAMES=%d\nSENT=%d\n", dry + 0, send + 0 }
    '
}

parse_adaptive_decision() { # FILE: oxp3-adaptivetdp.py --dry-run --once --json
    have python3 || return 3
    python3 - "$1" <<'PY'
import json, sys
# run_tool merges stderr into the same file, and the daemon always prints its
# human-readable change line there: decode the first JSON document only.
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    data = json.JSONDecoder().raw_decode(text.lstrip())[0]
except Exception:
    sys.exit(3)
battery = data.get("battery") or {}
on_ac = data.get("on_ac")
capacity = battery.get("capacity_pct")
print("ON_AC=%s" % ("" if on_ac is None else ("true" if on_ac else "false")))
print("WANT=%s" % (data.get("want") or ""))
print("CAPACITY=%s" % ("" if capacity is None else capacity))
print("BAT_STATUS=%s" % (battery.get("status") or ""))
print("POLICY=%s" % (data.get("policy") or ""))
PY
}

parse_ac_verdict() { # stdin: ON_AC WANT CAPACITY BAT_STATUS FLOOR ALLOW
    local on_ac="" want="" capacity="" status="" floor=20 allow=false key val
    while IFS='=' read -r key val; do
        case $key in
            ON_AC) on_ac=$val ;;
            WANT) want=$val ;;
            CAPACITY) capacity=$val ;;
            BAT_STATUS) status=$val ;;
            FLOOR) floor=$val ;;
            ALLOW) allow=$val ;;
        esac
    done
    local numeric=1
    case $capacity in ''|*[!0-9]*) numeric=0 ;; esac
    local verdict why
    if [ "$on_ac" = true ] || [ "$on_ac" = false ]; then
        # The AC/Mains node exists, so read_ac_online() never returns None and
        # both battery clamps in Policy.target() are live.
        if [ "$on_ac" = false ] && [ "$want" = performance ] && [ "$allow" != true ]; then
            verdict=fail
            why="AC state is readable (on_ac=false) but performance was chosen on battery: the clamp is broken"
        else
            verdict=pass
            why="AC state detected (on_ac=$on_ac): the battery clamp in the policy is live"
        fi
    elif [ "$status" = Discharging ]; then
        # No AC node: on_ac is None and both clamps are skipped. Only a policy
        # that stays at balanced or below is safe here.
        if [ "$want" = performance ] && [ "$allow" != true ]; then
            verdict=fail
            why="no AC/Mains node (on_ac unknown) and the battery is discharging, but performance was allowed: both battery clamps are skipped"
        elif [ "$numeric" = 1 ] && [ "$capacity" -le "$floor" ] && [ "$want" != quiet ]; then
            verdict=fail
            why="no AC/Mains node and the battery is at ${capacity} % (floor ${floor} %), but the policy did not force quiet"
        else
            verdict=skip
            why="no AC/Mains node: on_ac is unknown, so both battery clamps are skipped (deferred risk); battery ${capacity:-?} % ${status:-?}"
        fi
    else
        verdict=skip
        why="no AC/Mains node: on_ac is unknown, so both battery clamps are skipped (deferred risk); battery status=${status:-?}"
    fi
    printf 'VERDICT=%s\nWHY=%s\n' "$verdict" "$why"
}

parse_limit_order() { # DRYRUN_JSON STATUS_JSON -> order invariant per domain
    have python3 || return 3
    python3 - "$1" "$2" <<'PY'
import json, sys
def load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        sys.exit(3)
dry, live = load(sys.argv[1]), load(sys.argv[2])
idx = {"pl1": 0, "pl2": 1, "pl4": 2}
status = {}
for dom in live.get("domains") or []:
    cons = dom.get("constraints") or {}
    status[dom.get("path")] = {k: (cons.get(k) or {}).get("limit_uw") for k in idx}

# Classify each planned write from the plan's own recorded before-state.  A
# plan written by apply_limits() carries before_uw/direction, so the check
# re-checks the decision against the exact limits the planner read.  A stale
# status sample (a live daemon may have rewritten the limits between `status`
# and this dry-run) can therefore never make a safe plan look unsafe.  Plans
# without that record (an older installed tool) fall back to the status file.
by_path, path_order, problems = {}, [], []
all_recorded = True
for rep in dry.get("domain_reports") or []:
    for w in rep.get("written") or []:
        level = w.get("level")
        if level not in idx:
            continue
        path = w.get("domain")
        want = w.get("value_uw")
        if want is None:
            kind = "unknown"
        elif w.get("direction") in ("raise", "lower", "same"):
            kind = w["direction"]
        elif "before_uw" in w:
            before = w["before_uw"]
            kind = ("unknown" if before is None else
                    "raise" if want > before else
                    "lower" if want < before else "same")
        else:
            all_recorded = False
            before = (status.get(path) or {}).get(level)
            kind = ("unknown" if before is None else
                    "raise" if want > before else
                    "lower" if want < before else "same")
        if path not in by_path:
            by_path[path] = []
            path_order.append(path)
        by_path[path].append((level, kind))

for path in path_order:
    seq = by_path[path]
    order = []
    for level, _kind in seq:
        if level not in order:
            order.append(level)
    raises = [(i, level) for i, (level, kind) in enumerate(seq) if kind == "raise"]
    lowers = [(i, level) for i, (level, kind) in enumerate(seq) if kind == "lower"]
    if raises and lowers:
        last_raise = max(i for i, _ in raises)
        first_lower = min(i for i, _ in lowers)
        if last_raise > first_lower:
            problems.append("%s: a lowering write precedes a raising one (%s)"
                            % (path, ",".join(order)))
    raise_nums = [idx[level] for _, level in raises]
    if raise_nums != sorted(raise_nums, reverse=True):
        problems.append("%s: raising writes are not ceiling-first (%s)"
                        % (path, ",".join(level for _, level in raises)))
    lower_nums = [idx[level] for _, level in lowers]
    if lower_nums != sorted(lower_nums):
        problems.append("%s: lowering writes are not floor-first (%s)"
                        % (path, ",".join(level for _, level in lowers)))
mixed = sum(1 for path in path_order
            if any(k == "raise" for _l, k in by_path[path])
            and any(k == "lower" for _l, k in by_path[path]))
print("DOMAINS=%d" % len(path_order))
print("MIXED=%d" % mixed)
print("VIOLATION=%d" % (1 if problems else 0))
print("RECORDED=%d" % (1 if all_recorded else 0))
print("WHY=%s" % ("; ".join(problems) or
                  "raises are ceiling-first and precede every lowering"))
PY
}

parse_curve_summary() { # FILE: oxp3-powerctl.py fan curve --dry-run --once --json
    have python3 || return 3
    python3 - "$1" <<'PY'
import json, os, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(3)
pwm = data.get("last_pwm")
print("OK=%s" % ("true" if data.get("ok") else "false"))
print("PWM=%s" % ("" if pwm is None else pwm))
print("STEPS=%s" % (data.get("steps", "")))
print("DRY=%s" % ("true" if data.get("dry_run") else "false"))
print("CURVE=%s" % os.path.basename(data.get("curve") or ""))
PY
}

parse_battery_limit_dry() { # FILE: oxp3-powerctl.py battery limit --dry-run --json
    have python3 || return 3
    python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(3)
print("REQUESTED=%s" % data.get("requested", ""))
print("APPLIED=%s" % data.get("applied", ""))
print("DRY=%s" % ("true" if data.get("dry_run") else "false"))
print("CAPABILITY=%s" % (data.get("capability") or ""))
PY
}

parse_inst_paths() { # stdin: SYS_FW, HOST_MESA, FLATPAK_MESA from install-common.sh
    awk '
        /^SYS_FW=/ { fw = substr($0, 8) }
        /^HOST_MESA=/ { mesa = substr($0, 11) }
        /^FLATPAK_MESA=/ { flat = substr($0, 14) }
        END {
            verdict = "pass"; why = "helpers resolve against the live host paths"
            if (mesa == "") {
                verdict = "fail"; why = "host_mesa found no /usr/lib64/libgallium-*.so"
            } else if (mesa !~ /^[0-9]/) {
                verdict = "fail"; why = "host_mesa returned an implausible version '" mesa "'"
            } else if (flat != "same as the host" && flat != "Anatase'\''s (not ours)") {
                verdict = "fail"; why = "flatpak_mesa returned an unknown verdict '" flat "'"
            } else if (fw != "" && fw !~ /^\/usr\/lib\/firmware\//) {
                verdict = "fail"; why = "sys_fw returned a path outside /usr/lib/firmware: " fw
            }
            printf "VERDICT=%s\nWHY=%s\nSYS_FW=%s\nHOST_MESA=%s\nFLATPAK_MESA=%s\n",
                verdict, why, fw, mesa, flat
        }
    '
}

parse_iio_devices() { # DIR (default /sys/bus/iio/devices) -> primary/fallback map
    local root=${1:-/sys/bus/iio/devices} d name label gyro
    local primary="" fallback="" pcount=0 fcount=0
    for d in "$root"/iio:device*; do
        [ -d "$d" ] || continue
        name=$(read1 "$d/name" || true)
        label=$(read1 "$d/label" || true)
        gyro=$(read1 "$d/in_anglvel_x_raw" || true)
        if printf '%s' "$name" | grep -q '10EC5280' || [ "$label" = accel-display ]; then
            pcount=$((pcount + 1))
            [ -z "$primary" ] && primary=$(basename "$d")
        elif printf '%s' "$name" | grep -q '^i2c-' && [ -n "$gyro" ]; then
            fcount=$((fcount + 1))
            [ -z "$fallback" ] && fallback=$(basename "$d")
        fi
    done
    printf 'PRIMARY=%s\nPRIMARY_COUNT=%d\nFALLBACK=%s\nFALLBACK_COUNT=%d\n' \
        "$primary" "$pcount" "$fallback" "$fcount"
}

status_indicator() { # FILE ID -> "state<TAB>value"
    have python3 || return 3
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(3)
for grp in data.get("groups") or []:
    for ind in grp.get("indicators") or []:
        if ind.get("id") == sys.argv[2]:
            value = (ind.get("value") or "").replace("\n", " ").replace("\t", " ")
            print("%s\t%s" % (ind.get("state", "unknown"), value))
            sys.exit(0)
sys.exit(4)
PY
}

exit_code() { # PASS FAIL SKIP UNKNOWN
    if [ "${2:-0}" -gt 0 ]; then
        printf '1'
    elif [ "${1:-0}" -eq 0 ] && [ $((${3:-0} + ${4:-0})) -gt 0 ]; then
        printf '2'
    else
        printf '0'
    fi
}

# ------------------------------------------------------------------ result store
IDS=(); GRP=(); TITLES=(); STATES=(); SUMMARIES=(); DETAILS=(); HINTS=()
PASS_N=0; FAIL_N=0; SKIP_N=0; UNKNOWN_N=0
RUN_OUT=""; RUN_RC=0
STATUS_JSON=""; STATUS_READY=0

record() { # ID GROUP "TITLE" STATE "SUMMARY" "DETAIL" "HINT"
    IDS+=("$1"); GRP+=("$2"); TITLES+=("$3"); STATES+=("$4")
    SUMMARIES+=("$(clean "$5")"); DETAILS+=("$(clean "${6:-}")"); HINTS+=("$(clean "${7:-}")")
    case $4 in
        PASS) PASS_N=$((PASS_N + 1)) ;;
        FAIL) FAIL_N=$((FAIL_N + 1)) ;;
        SKIP) SKIP_N=$((SKIP_N + 1)) ;;
        *) UNKNOWN_N=$((UNKNOWN_N + 1)) ;;
    esac
}

total_checks() { printf '%s' "${#IDS[@]}"; }

ensure_status_json() {
    if [ "$STATUS_READY" -eq 1 ]; then
        # A first attempt that cached "unavailable" must stay unavailable; the old
        # `[ -n "$STATUS_JSON" ]; return 0` returned success with an empty report.
        [ -n "$STATUS_JSON" ] || return 1
        return 0
    fi
    STATUS_READY=1
    local tool=$REPO/tools/oxp3-status.sh
    [ -x "$tool" ] || return 1
    have python3 || return 1
    run_tool status-json 90 "$tool" --json
    # Exit 1 is a verdict (an indicator is fail), not a backend failure: the JSON is
    # still complete and the dependent checks must report the fail, not degrade to
    # SKIP/UNKNOWN. 2 = --strict, 3 = backend could not start.
    case $RUN_RC in 0|1) ;; *) STATUS_JSON=""; return 1 ;; esac
    if json_ok "$RUN_OUT"; then
        STATUS_JSON=$RUN_OUT
        return 0
    fi
    STATUS_JSON=""
    return 1
}

status_field() { # dotted path -> scalar from the status JSON (file path or text)
    local src=${STATUS_JSON:-}
    [ -n "$src" ] && have python3 || return 1
    python3 - "$1" "$src" <<'PY' 2>/dev/null
import json, sys
path = sys.argv[1].split(".")
src = sys.argv[2]
try:
    cur = json.load(open(src, encoding="utf-8")) if src and not src.lstrip().startswith("{") else json.loads(src)
except Exception:
    raise SystemExit(1)
for part in path:
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        raise SystemExit(1)
print(json.dumps(cur) if isinstance(cur, (dict, list)) else cur)
PY
}


ind_state() { # status indicator state or empty
    [ -n "$STATUS_JSON" ] || return 1
    local line
    line=$(status_indicator "$STATUS_JSON" "$1" 2>/dev/null) || return 1
    printf '%s' "${line%%$'\t'*}"
}

ind_value() { # status indicator value or empty
    [ -n "$STATUS_JSON" ] || return 1
    local line
    line=$(status_indicator "$STATUS_JSON" "$1" 2>/dev/null) || return 1
    printf '%s' "${line#*$'\t'}"
}

simple_ind() { # CHECK_ID INDICATOR_ID GROUP "TITLE" PREFIX "HINT"
    local id=$1 ind=$2 grp=$3 title=$4 prefix=$5 hint=$6 st value
    st=$(ind_state "$ind") || st=""
    value=$(ind_value "$ind") || value=""
    if [ -z "$st" ]; then
        record "$id" "$grp" "$title" UNKNOWN "indicator missing from oxp3-status --json" "" "$REPO/tools/oxp3-status.sh --json does not list $ind; $hint"
        return 0
    fi
    case $st in
        ok)
            if [ -n "$prefix" ] && ! printf '%s' "$value" | grep -qF -- "$prefix"; then
                record "$id" "$grp" "$title" FAIL "value does not contain $prefix" "$value" "$hint"
            else
                record "$id" "$grp" "$title" PASS "$value" "" ""
            fi
            ;;
        warn) record "$id" "$grp" "$title" FAIL "warn: $value" "" "$hint" ;;
        fail) record "$id" "$grp" "$title" FAIL "fail: $value" "" "$hint" ;;
        *) record "$id" "$grp" "$title" UNKNOWN "state=$st" "$value" "$hint" ;;
    esac
}

tool_path() { # relative path if executable
    local p=$REPO/$1
    [ -x "$p" ] && printf '%s' "$p"
}

powerctl_cmd() {
    if [ -x /usr/local/libexec/oxp3-powerctl ]; then
        printf '%s' /usr/local/libexec/oxp3-powerctl
    else
        tool_path tools/oxp3-powerctl.py || true
    fi
}

# oxp3-powerctl's saved state (profile/limits/fan). --with-changes snapshots
# and restores this file; no default or --full run ever writes it.
POWER_STATE=/etc/oxp3/power.json

watts() { awk -v u="${1:-0}" 'BEGIN{printf "%.2f", u / 1000000}'; }

# ------------------------------------------------------------------ 0. preflight
check_preflight() {
    local tool out safe verdict fails warns skipped detail
    tool=$(tool_path tools/oxp3-preflight.sh)
    if [ -z "$tool" ]; then
        record preflight.verdict preflight "preflight verdict" SKIP \
            "tools/oxp3-preflight.sh not found" "" \
            "restore the checkout (tools/oxp3-preflight.sh + tools/oxp3_preflight.py)"
        return 0
    fi
    run_tool preflight 90 "$tool" --json
    case $RUN_RC in
        0|1) ;;
        2) record preflight.verdict preflight "preflight verdict" UNKNOWN \
               "preflight usage error" "$(tail_flat "$RUN_OUT" 2)" \
               "tools/oxp3-preflight.sh --help"
           return 0 ;;
        *) record preflight.verdict preflight "preflight verdict" UNKNOWN \
               "preflight could not start (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" \
               "python3 tools/oxp3_preflight.py --selftest"
           return 0 ;;
    esac
    if ! json_ok "$RUN_OUT"; then
        record preflight.verdict preflight "preflight verdict" UNKNOWN \
            "preflight did not return JSON (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" \
            "python3 tools/oxp3_preflight.py --selftest"
        return 0
    fi
    out=$(parse_preflight_summary <"$RUN_OUT")
    safe=$(kv "$out" SAFE)
    verdict=$(kv "$out" VERDICT)
    fails=$(kv "$out" FAIL)
    warns=$(kv "$out" WARN)
    skipped=$(kv "$out" SKIPPED)
    detail="warnings: ${warns:-?}; would skip: ${skipped:-none}"
    if [ "$safe" = true ]; then
        record preflight.verdict preflight "preflight verdict" PASS \
            "$verdict: ${fails:-0} blocker(s), ${warns:-0} warning(s)" "$detail" ""
    else
        record preflight.verdict preflight "preflight verdict" FAIL \
            "$verdict: ${fails:-?} blocker(s)" "$detail" \
            "tools/oxp3-preflight.sh ; fix the FAIL rows (docs/PREFLIGHT.md)"
    fi
}

# ------------------------------------------------------------------ 1. volume
check_volume() {
    local st load active file journal dev observe h hwdb
    st=$(unit_state oxp3-voltap.service)
    IFS='|' read -r load active file <<<"$st"

    if ! have systemctl; then
        record vol.unit volume "oxp3-voltap service" UNKNOWN "systemctl not available" "" "this host is not systemd-managed"
    elif [ "$load" = not-found ]; then
        record vol.unit volume "oxp3-voltap service" SKIP "not installed (the supported path is the hhd plugin)" "LoadState=not-found" "sudo tools/hhd-oxp3-voltap/install.sh install (plugin); legacy daemon: sudo tools/oxp3-voltap/install.sh install --hwdb"
    elif [ "$active" = active ]; then
        record vol.unit volume "oxp3-voltap service" PASS "active (running)" "unit oxp3-voltap.service; UnitFileState=$file" ""
    elif [ "$file" = disabled ]; then
        record vol.unit volume "oxp3-voltap service" SKIP \
            "installed but disabled on purpose (legacy daemon)" \
            "LoadState=$load ActiveState=$active UnitFileState=$file" \
            "supported path: the hhd plugin (sudo tools/hhd-oxp3-voltap/install.sh install ; sudo systemctl restart hhd)"
    else
        record vol.unit volume "oxp3-voltap service" FAIL "unit is $active" "LoadState=$load ActiveState=$active" "journalctl -u oxp3-voltap -b ; sudo systemctl restart oxp3-voltap"
    fi

    journal=$(journal_text oxp3-voltap)
    observe=0
    printf '%s\n' "$journal" | grep -q 'OBSERVE: hhd owns the volume path' && observe=1
    dev=$(printf '%s\n' "$journal" | parse_vol_journal)

    # Which device would be grabbed and why (read-only; root only completes the
    # holder view).  vol.topology is the decision, vol.holder is the EVIOCGRAB
    # owner of the physical AT keyboard that makes that decision necessary.
    local vbin topo t_topo t_sel t_role t_selheld t_reason t_source t_pred
    local t_hhdrunning t_hhdpresent t_hhdpids t_atkbd t_atheld t_known
    local t_allow t_protected t_observe t_hhdvirtual
    vbin=$REPO/tools/oxp3-voltap/oxp3-voltap
    if [ ! -x "$vbin" ] || ! have python3; then
        record vol.topology volume "volume device choice" SKIP "daemon or python3 missing" "$vbin" "install the repo tooling and python3"
        record vol.holder volume "physical volume device holder" SKIP "daemon or python3 missing" "$vbin" "install the repo tooling and python3"
    else
        run_tool voltap-topology 20 "$vbin" check --kv
        topo=$(parse_voltopology <"$RUN_OUT" 2>/dev/null)
        t_topo=$(kv "$topo" TOPO); t_sel=$(kv "$topo" SEL)
        t_role=$(kv "$topo" SEL_ROLE); t_selheld=$(kv "$topo" SEL_HELD)
        t_reason=$(kv "$topo" SEL_REASON); t_source=$(kv "$topo" SEL_SOURCE)
        t_pred=$(kv "$topo" PREDICTED_EXIT)
        t_hhdrunning=$(kv "$topo" HHD_RUNNING)
        t_hhdpresent=$(kv "$topo" HHD_VIRTUAL_PRESENT)
        t_hhdpids=$(kv "$topo" HHD_PID)
        t_atkbd=$(kv "$topo" ATKBD); t_atheld=$(kv "$topo" ATKBD_HELD)
        t_known=$(kv "$topo" HOLDERS_KNOWN)
        t_allow=$(kv "$topo" ALLOW_HHD_GRAB)
        t_protected=$(kv "$topo" HHD_PROTECTED)
        t_observe=$(kv "$topo" OBSERVE)
        t_hhdvirtual=$(kv "$topo" HHD_VIRTUAL)

        if [ -z "$t_topo" ]; then
            record vol.topology volume "volume device choice" SKIP "cannot read /proc/bus/input/devices" "$(last_line "$RUN_OUT")" "run $vbin check --kv"
            record vol.holder volume "physical volume device holder" SKIP "cannot read /proc" "" "run $vbin check --kv"
        elif [ "$t_pred" = 4 ] && [ "$t_protected" = 1 ]; then
            record vol.topology volume "volume device choice" PASS \
                "observe: hhd owns the volume path; nothing would be grabbed" \
                "$t_reason" \
                "supported path: install/keep the hhd plugin (tools/hhd-oxp3-voltap)"
            record vol.holder volume "physical volume device holder" PASS \
                "hhd holds the physical AT keyboard; we leave it alone" \
                "ATKBD=${t_atkbd:--} HHD_PID=${t_hhdpids:--}" ""
        elif [ "$t_pred" = 3 ]; then
            record vol.topology volume "volume device choice" FAIL "grab would be refused: $t_reason" "selected=${t_sel:--} held=${t_selheld:--}" "sudo $vbin explain ; sudo $vbin diagnose --duration 8"
            record vol.holder volume "physical volume device holder" FAIL "every volume candidate is held" "$t_reason" "sudo $vbin explain ; sudo $vbin diagnose --duration 8"
        elif [ -n "$t_sel" ]; then
            if [ "$t_source" = explicit ]; then
                record vol.topology volume "volume device choice" PASS "explicit override: would grab $t_sel" "$t_reason" ""
            elif [ "$t_role" = hhd-virtual ]; then
                record vol.topology volume "volume device choice" FAIL \
                    "would grab hhd's own volume keyboard ($t_sel)" \
                    "$t_reason; ALLOW_HHD_GRAB=${t_allow:-?}. hhd needs that device: a second EVIOCGRAB makes hhd drop the controller" \
                    "unset VOLTAP_ALLOW_HHD_GRAB, install the hhd plugin (tools/hhd-oxp3-voltap) and restart hhd; docs/DAEMONS.md"
            elif [ "$t_role" = at-kbd ]; then
                if [ "$t_hhdrunning" = 1 ] && [ "$t_known" != 1 ]; then
                    record vol.topology volume "volume device choice" UNKNOWN \
                        "physical AT keyboard selected; hhd is running and the holder scan needs root" \
                        "$t_reason; at that point the daemon would refuse the EBUSY grab and observe" \
                        "sudo $vbin check --kv ; the supported path is the hhd plugin"
                elif [ "$t_hhdrunning" = 1 ]; then
                    record vol.topology volume "volume device choice" PASS \
                        "would grab the physical AT keyboard; hhd does not hold it" \
                        "$t_reason" ""
                else
                    record vol.topology volume "volume device choice" PASS "would grab $t_sel (physical fallback)" "$t_reason; hhd not holding it" ""
                fi
            else
                record vol.topology volume "volume device choice" FAIL "unexpected selected role $t_role" "$t_reason" "sudo $vbin explain"
            fi
        elif [ "$t_hhdpresent" = 1 ]; then
            record vol.topology volume "volume device choice" FAIL "hhd volume keyboard present but nothing would be grabbed" "$t_reason" "sudo $vbin explain"
        else
            record vol.topology volume "volume device choice" SKIP "no volume event device on this host" "$t_reason" "sudo $vbin check"
        fi

        if [ -z "$t_topo" ] || [ "$t_pred" = 3 ] || [ "$t_pred" = 4 ]; then
            : # already recorded above
        elif [ "$t_known" != 1 ]; then
            record vol.holder volume "physical volume device holder" SKIP "holder scan needs root (/proc/*/fdinfo)" "ATKBD=${t_atkbd:--} HHD_PID=${t_hhdpids:--}" "sudo $vbin check --kv"
        elif [ -z "$t_atheld" ] || [ "$t_atheld" = "-" ]; then
            record vol.holder volume "physical volume device holder" PASS "nothing holds the physical AT keyboard" "ATKBD=${t_atkbd:--}" ""
        else
            local hit=0 p
            local -a held_pids
            IFS=, read -r -a held_pids <<<"$t_atheld"
            for p in "${held_pids[@]}"; do
                case ",$t_hhdpids," in
                    *",$p,"*) hit=1 ;;
                esac
            done
            if [ "$hit" = 1 ]; then
                record vol.holder volume "physical volume device holder" PASS "hhd holds the physical AT keyboard" "ATKBD=${t_atkbd:--} held by pid ${t_atheld}" ""
            else
                record vol.holder volume "physical volume device holder" UNKNOWN "AT keyboard held by a non-hhd pid" "ATKBD=${t_atkbd:--} held by ${t_atheld}" "sudo $vbin diagnose --duration 8 ; check for a second grabber"
            fi
        fi

        # Did this boot's daemon journal show a grab of hhd's own device?  That
        # is the exact regression that dropped the emulated controller.
        if [ -n "$dev" ] && [ -n "$t_hhdvirtual" ] && [ "$dev" = "$t_hhdvirtual" ]; then
            record vol.grab volume "volume event device grabbed" FAIL \
                "our daemon grabbed hhd's own device ${dev}" \
                "journalctl -u oxp3-voltap -b" \
                "sudo systemctl disable --now oxp3-voltap ; sudo systemctl restart hhd ; install the hhd plugin (tools/hhd-oxp3-voltap)"
        elif [ "$active" != active ]; then
            record vol.grab volume "volume event device grabbed" SKIP "daemon not running" "the supported path with hhd is the plugin" "sudo tools/hhd-oxp3-voltap/install.sh install ; sudo systemctl restart hhd"
        elif [ -n "$dev" ]; then
            record vol.grab volume "volume event device grabbed" PASS "grabbed ${dev#grabbed } (not an hhd device)" "journalctl -u oxp3-voltap -b" ""
        elif [ "$observe" = 1 ]; then
            record vol.grab volume "volume event device grabbed" PASS \
                "observe: hhd owns the volume path; nothing grabbed" \
                "journalctl -u oxp3-voltap -b" \
                "supported path: the hhd plugin (tools/hhd-oxp3-voltap)"
        else
            record vol.grab volume "volume event device grabbed" FAIL "no grab and no OBSERVE line in this boot's journal" "expected 'grabbed /dev/input/eventN (...)' or 'OBSERVE: hhd owns the volume path'" "journalctl -u oxp3-voltap -b ; cat /etc/oxp3-voltap.conf"
        fi
    fi

    hwdb=""
    for h in /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb /etc/udev/hwdb.d/84-oxp3-volume-keys.hwdb; do
        [ -f "$h" ] && { hwdb=$h; break; }
    done
    if [ -z "$hwdb" ]; then
        record vol.hwdb volume "force-release hwdb" SKIP "not installed" "no 84-oxp3-volume-keys*.hwdb in /etc/udev/hwdb.d" "sudo tools/oxp3-voltap/install.sh install --hwdb"
    elif ! grep -q 'KEYBOARD_KEY_[0-9a-fA-F]*=!' "$hwdb" 2>/dev/null; then
        record vol.hwdb volume "force-release hwdb" FAIL "installed file has no force-release ('!') mapping" "$hwdb" "reinstall it from tools/oxp3-voltap/ (install.sh install --hwdb)"
    elif [ -f /etc/udev/hwdb.bin ] && [ /etc/udev/hwdb.bin -nt "$hwdb" ]; then
        record vol.hwdb volume "force-release hwdb" PASS "installed and compiled" "$hwdb -> /etc/udev/hwdb.bin" ""
    else
        record vol.hwdb volume "force-release hwdb" FAIL "installed but not compiled into /etc/udev/hwdb.bin" "$hwdb" "sudo systemd-hwdb update && sudo udevadm trigger --subsystem-match=input --action=change"
    fi

    [ "$FULL" -eq 1 ] || return 0
    if ! is_root; then
        record vol.hold volume "tap/hold functional test" SKIP "needs root (/dev/uinput)" "" "sudo tools/oxp3-verify.sh --full   (or: sudo tools/oxp3-voltap/oxp3-voltap selftest)"
        return 0
    fi
    local bin=$REPO/tools/oxp3-voltap/oxp3-voltap
    if [ ! -x "$bin" ]; then
        record vol.hold volume "tap/hold functional test" SKIP "daemon source missing" "$bin" ""
        return 0
    fi
    run_tool voltap-selftest 60 "$bin" selftest
    if [ "$RUN_RC" -eq 0 ] && grep -q 'selftest done' "$RUN_OUT" 2>/dev/null; then
        record vol.hold volume "tap/hold functional test" PASS "selftest done (tap/hold state machine over uinput)" "$(last_line "$RUN_OUT")" ""
    else
        record vol.hold volume "tap/hold functional test" FAIL "selftest failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "sudo tools/oxp3-voltap/oxp3-voltap selftest ; journalctl -u oxp3-voltap -b"
    fi
}

# ------------------------------------------------------------------ 1b. hhd owns the volume path
# The outage of 2026-11-09: a second EVIOCGRAB on hhd's own virtual volume
# keyboard made hhd's grab() fail with EBUSY, hhd assumed the controller had
# disconnected and 28DE:12FF disappeared.  These two read-only invariants catch
# that class of regression:
#   * hhd is healthy in this boot (active, no EBUSY / "assuming controllers
#     disconnected" in its journal);
#   * nothing of ours holds an hhd-* event device (our processes' /proc/<pid>/fd,
#     EVIOCGRAB from fdinfo where readable, and this boot's oxp3-voltap journal).
check_hhd_volume() {
    local st load active file jtext busy=0 jreadable=0 paths="" d name path last_busy="" recovered=0
    local p pid cmd tgt l fd held="" grabbers="" denied=0 journal_grabbed="" jvol

    st=$(unit_state hhd.service)
    IFS='|' read -r load active file <<<"$st"
    if ! have systemctl; then
        record hhd.unit hhd "hhd service" UNKNOWN "systemctl not available" "" "this host is not systemd-managed"
        return 0
    elif [ "$load" = not-found ]; then
        record hhd.unit hhd "hhd service" SKIP "hhd is not installed" "LoadState=not-found" "the volume plugin and topology rules assume hhd; without it the daemon may take the free AT keyboard"
        record hhd.grab hhd "no hhd device held by us" PASS "hhd is not installed; no hhd event device exists" "" ""
        return 0
    elif [ "$active" = active ]; then
        record hhd.unit hhd "hhd service" PASS "active (running)" "UnitFileState=$file" ""
    else
        record hhd.unit hhd "hhd service" FAIL "hhd is $active" "LoadState=$load ActiveState=$active" "sudo systemctl restart hhd ; journalctl -u hhd -b --no-pager | tail -40"
    fi

    # hhd is chatty; health is about the most recent state, so use the tail
    # (journal_text truncates the head, which would hide a later recovery).
    jtext=$(journalctl -u hhd.service -b --no-pager -o cat 2>/dev/null | tail -n 600)
    local jkv
    if [ -n "$jtext" ]; then
        jreadable=1
        jkv=$(printf '%s\n' "$jtext" | parse_hhd_journal)
        busy=$(printf '%s\n' "$jkv" | sed -n 's/^BUSY=//p')
        last_busy=$(printf '%s\n' "$jkv" | sed -n 's/^LAST=//p')
        recovered=$(printf '%s\n' "$jkv" | sed -n 's/^RECOVERED=//p')
    fi
    if [ "$jreadable" -eq 0 ]; then
        record hhd.busy hhd "hhd volume device not held by others" SKIP \
            "hhd journal not readable" "" \
            "journalctl -u hhd -b --no-pager ; sudo tools/oxp3-verify.sh"
    elif [ "${busy:-0}" -gt 0 ] && [ "${recovered:-0}" = 1 ]; then
        record hhd.busy hhd "hhd volume device not held by others" PASS \
            "recovered: ${busy} earlier EBUSY/disconnect line(s); hhd initialized afterwards" \
            "${last_busy:--}" ""
    elif [ "${busy:-0}" -gt 0 ]; then
        record hhd.busy hhd "hhd volume device not held by others" FAIL \
            "this boot's hhd journal reports EBUSY/disconnect (${busy} line(s)) with no later successful init" \
            "${last_busy:--}" \
            "find the second grabber: sudo tools/oxp3-verify.sh ; then remove it (never grab what hhd owns, docs/DAEMONS.md)"
    else
        record hhd.busy hhd "hhd volume device not held by others" PASS \
            "no EBUSY/disconnect in this boot's hhd journal" \
            "journalctl -u hhd -b" ""
    fi

    # Which event devices are hhd's own virtual volume keyboards?
    for d in /sys/class/input/event*; do
        [ -r "$d/device/name" ] || continue
        name=$(cat "$d/device/name" 2>/dev/null || true)
        case $name in
            *"Handheld Daemon"*) paths="$paths /dev/input/${d##*/}" ;;
        esac
    done
    if [ -z "$paths" ]; then
        record hhd.grab hhd "no hhd device held by us" PASS \
            "hhd's virtual volume keyboard is not present" "" ""
        return 0
    fi

    # Anything of ours that is running the daemon must not have an fd or grab
    # on one of those devices.
    for p in /proc/[0-9]*; do
        pid=${p#/proc/}
        [ -r "$p/cmdline" ] || continue
        cmd=$(tr '\0' ' ' <"$p/cmdline" 2>/dev/null || true)
        case $cmd in
            *oxp3-voltap*run*) ;;
            *) continue ;;
        esac
        if [ ! -r "$p/fd" ]; then
            denied=$((denied + 1))
            continue
        fi
        for l in "$p/fd"/*; do
            tgt=$(readlink "$l" 2>/dev/null) || continue
            for d in $paths; do
                [ "$tgt" = "$d" ] || continue
                held="$held $pid:$tgt"
                fd=${l##*/}
                if [ -r "$p/fdinfo/$fd" ] && grep -q '^grab: 1$' "$p/fdinfo/$fd" 2>/dev/null; then
                    grabbers="$grabbers $pid:$tgt"
                fi
            done
        done
    done

    # Historical evidence: did this boot's daemon journal claim a grab on one?
    jvol=$(journal_text oxp3-voltap)
    if [ -n "$jvol" ]; then
        for d in $paths; do
            if printf '%s\n' "$jvol" | grep -q "grabbed $d "; then
                journal_grabbed="$journal_grabbed $d"
            fi
        done
    fi

    if [ -n "$grabbers" ]; then
        record hhd.grab hhd "no hhd device held by us" FAIL \
            "our process holds an EVIOCGRAB on hhd's device:$grabbers" \
            "that is the outage: hhd cannot open its own volume keyboard" \
            "sudo systemctl disable --now oxp3-voltap ; sudo systemctl restart hhd ; install the hhd plugin (tools/hhd-oxp3-voltap)"
    elif [ -n "$held" ]; then
        record hhd.grab hhd "no hhd device held by us" FAIL \
            "our process has an open fd on hhd's device:$held" "" \
            "stop holding hhd's devices (docs/DAEMONS.md); the supported path is the hhd plugin"
    elif [ -n "$journal_grabbed" ]; then
        record hhd.grab hhd "no hhd device held by us" FAIL \
            "this boot's daemon journal shows a grab of hhd's device:$journal_grabbed" \
            "journalctl -u oxp3-voltap -b" \
            "sudo systemctl disable --now oxp3-voltap ; sudo systemctl restart hhd ; install the hhd plugin (tools/hhd-oxp3-voltap)"
    elif [ "$denied" -gt 0 ]; then
        record hhd.grab hhd "no hhd device held by us" SKIP \
            "cannot inspect the root daemon's fds" \
            "denied /proc/<pid>/fd for ${denied} daemon process(es); devices:$paths" \
            "sudo tools/oxp3-verify.sh --full (read-only); the journal part above already passed"
    else
        record hhd.grab hhd "no hhd device held by us" PASS \
            "no process of ours holds an fd or grab on hhd's device(s)" \
            "checked:$paths" ""
    fi
}

# ------------------------------------------------------------------ 2. LEDs / home
check_leds() {
    local rule=/etc/udev/rules.d/90-oxp3-hid.rules
    local led node="" perm="" kv zones presets status_ok=0

    if [ -f "$rule" ]; then
        record hid.rule leds "hidraw udev rule" PASS "installed" "$rule" ""
    else
        record hid.rule leds "hidraw udev rule" SKIP "not installed" "no $rule" "sudo install -m 0644 tools/oxp3-hid.rules $rule && sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=change"
    fi

    led=$REPO/tools/oxp3-led.py
    if [ -x "$led" ] && have python3; then
        run_tool led-status 30 "$led" status
        if [ "$RUN_RC" -eq 0 ]; then
            status_ok=1
            kv=$(parse_led_status <"$RUN_OUT")
            node=$(printf '%s\n' "$kv" | sed -n 's/^NODE=//p')
            perm=$(printf '%s\n' "$kv" | sed -n 's/^PERM=//p')
        fi
    fi

    if [ "$status_ok" -eq 0 ]; then
        record led.status leds "vendor HID protocol visible" SKIP "oxp3-led.py status failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 1)" "python3 tools/oxp3-led.py status ; is the device connected?"
        record led.zones leds "addressable RGB zones" SKIP "no status" "" "tools/oxp3-led.py selftest"
    else
        record led.status leds "vendor HID protocol visible" PASS "protocol readable on ${node:-?}" "vendor node ${node:-?} (permissions ${perm:-?})" ""
        if printf '%s\n' "$kv" | grep -q '^ZONES=1' && printf '%s\n' "$kv" | grep -q '^PRESETS=1'; then
            record led.zones leds "addressable RGB zones" PASS "5 zones, 11 presets" "zone ids left_stick/right_stick/gkey/top/connector" ""
        else
            record led.zones leds "addressable RGB zones" FAIL "zone/preset table missing from status" "$(printf '%s' "$kv" | tr '\n' ' ')" "tools/oxp3-led.py selftest ; docs/hw/home-button-leds.md"
        fi
    fi

    if [ -z "$node" ]; then
        record hid.access leds "hidraw node writable by user" SKIP "vendor node not reported" "device may be absent or the probe failed" "tools/oxp3-led.py status"
    elif [ -w "$node" ]; then
        record hid.access leds "hidraw node writable by user" PASS "writable as $(id -un)" "$node (permissions ${perm:-?})" ""
    elif [ ! -f "$rule" ]; then
        record hid.access leds "hidraw node writable by user" SKIP "udev rule not installed" "$node is root-only (permissions ${perm:-?})" "sudo install -m 0644 tools/oxp3-hid.rules $rule && sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=change"
    else
        record hid.access leds "hidraw node writable by user" FAIL "rule installed but node is still not writable by $(id -un)" "$node (permissions ${perm:-?})" "sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=change ; then re-plug the device"
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): find_vendor_hidraw() ignores
    # the report descriptor and takes the first input-less 1a86:fe00 node. The
    # descriptor is the only thing that proves the node is the vendor collection
    # (usage page 0xFF00), so compare the tool's choice with every candidate.
    local hrep hsel hselv hvn hver hwhy n
    if [ ! -f "$REPO/tools/oxp3_hid.py" ] || ! have python3; then
        record hid.node leds "vendor hidraw node selection" SKIP "oxp3_hid.py or python3 missing" "$REPO/tools/oxp3_hid.py" ""
    else
        hrep=$(hid_report /sys/class/hidraw)
        hsel=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import oxp3_hid
print(oxp3_hid.find_vendor_hidraw() or "")' "$REPO/tools" 2>/dev/null) || hsel=""
        hselv=0
        hvn=$(printf '%s\n' "$hrep" | sed -n 's/^VENDOR_NODES=//p')
        for n in $hvn; do [ "$n" = "$hsel" ] && hselv=1; done
        hrep=$(printf 'SELECTED=%s\nSEL_VENDOR=%s\n%s\n' "$hsel" "$hselv" "$hrep" | parse_hid_verdict)
        hver=$(printf '%s\n' "$hrep" | sed -n 's/^VERDICT=//p')
        hwhy=$(printf '%s\n' "$hrep" | sed -n 's/^WHY=//p')
        case $hver in
            pass)
                record hid.node leds "vendor hidraw node selection" PASS "$hwhy" "candidates: $(printf '%s\n' "$hrep" | sed -n 's/^VENDOR=//p') of $(printf '%s\n' "$hrep" | sed -n 's/^TOTAL=//p') advertise usage page 0xFF00" ""
                ;;
            fail)
                record hid.node leds "vendor hidraw node selection" FAIL "$hwhy" "the firmware accepts frames on the node with usage page 0xFF00" "od -An -tx1 /sys/class/hidraw/hidraw*/device/report_descriptor | grep -n '06 00 ff' ; then make the descriptor authoritative in oxp3_hid.find_vendor_hidraw() and update docs/UNIT-TESTS.md"
                ;;
            skip)
                record hid.node leds "vendor hidraw node selection" SKIP "$hwhy" "hidraw nodes for 1a86:fe00: $(printf '%s\n' "$hrep" | sed -n 's/^TOTAL=//p')" "install the udev rule / connect the device, then re-run: tools/oxp3-led.py status"
                ;;
            *)
                record hid.node leds "vendor hidraw node selection" UNKNOWN "$hwhy" "$hsel" "od -An -tx1 /sys/class/hidraw/hidraw*/device/report_descriptor ; docs/hw/home-button-leds.md"
                ;;
        esac
    fi

    local hb=oxp3-homebtn.service
    st=$(unit_state "$hb" --user)
    local u_load u_active u_file
    IFS='|' read -r u_load u_active u_file <<<"$st"
    if [ "$u_load" = "?" ] || [ -z "$u_load" ]; then
        record homebtn.unit leds "home-button unit" UNKNOWN "no user systemd instance" "systemctl --user is unavailable in this context" "run inside the logged-in desktop session (systemctl --user status oxp3-homebtn)"
    elif [ "$u_load" = not-found ]; then
        record homebtn.unit leds "home-button unit" SKIP "user unit not installed" "LoadState=not-found" "install ~/.config/systemd/user/oxp3-homebtn.service (tools/oxp3-homebtn.service) and systemctl --user enable --now oxp3-homebtn"
    elif [ "$u_active" = active ]; then
        record homebtn.unit leds "home-button unit" PASS "active (running)" "unit $hb (user) UnitFileState=$u_file" ""
    else
        record homebtn.unit leds "home-button unit" FAIL "user unit is $u_active" "LoadState=$u_load ActiveState=$u_active" "journalctl --user -u oxp3-homebtn -b ; systemctl --user restart oxp3-homebtn"
    fi

    if [ "$u_load" = "?" ] || [ -z "$u_load" ]; then
        record homebtn.mapping leds "home button maps to KEY_HOMEPAGE" UNKNOWN "no user systemd instance" "" "systemctl --user cat oxp3-homebtn"
        record homebtn.press leds "home button press observed" UNKNOWN "no user systemd instance" "" "run inside the desktop session"
    elif [ "$u_load" = not-found ]; then
        record homebtn.mapping leds "home button maps to KEY_HOMEPAGE" SKIP "unit not installed" "" "unit ExecStart must use --key homepage"
        record homebtn.press leds "home button press observed" SKIP "unit not installed" "" "systemctl --user enable --now oxp3-homebtn"
    else
        local execs
        execs=$(systemctl --user show -p ExecStart --value "$hb" 2>/dev/null)
        if printf '%s' "$execs" | grep -q -- '--key homepage' && ! printf '%s' "$execs" | grep -q -- '--exec'; then
            record homebtn.mapping leds "home button maps to KEY_HOMEPAGE" PASS "ExecStart uses --key homepage" "$execs" ""
        else
            record homebtn.mapping leds "home button maps to KEY_HOMEPAGE" FAIL "unit does not run with --key homepage" "$execs" "edit ~/.config/systemd/user/oxp3-homebtn.service (ExecStart ... --key homepage), then systemctl --user daemon-reload && systemctl --user restart oxp3-homebtn"
        fi
        if [ "$u_active" != active ]; then
            record homebtn.press leds "home button press observed" SKIP "daemon not running" "" "systemctl --user start oxp3-homebtn"
        else
            local hbkv listen press
            hbkv=$(journal_text "$hb" --user | parse_homebtn_journal)
            listen=$(printf '%s\n' "$hbkv" | sed -n 's/^LISTEN=//p')
            press=$(printf '%s\n' "$hbkv" | sed -n 's/^PRESS=//p')
            if num_gt "$press" 0; then
                record homebtn.press leds "home button press observed" PASS "$press press(es) this boot" "the daemon reads the vendor frame and emits KEY_HOMEPAGE through uinput" ""
            elif [ "${listen:-0}" = 1 ]; then
                record homebtn.press leds "home button press observed" SKIP "daemon is listening, no press since boot" "journalctl --user -u oxp3-homebtn -b" "press the home button once, then re-run"
            else
                record homebtn.press leds "home button press observed" FAIL "daemon active but never opened the vendor hidraw node" "no 'listening on /dev/hidraw' line in this boot's user journal" "journalctl --user -u oxp3-homebtn -b ; check the udev rule and the device"
            fi
        fi
    fi

    local cfg=$HOME/.config/oxp3-rgb.toml
    st=$(unit_state oxp3-rgbd.service --user)
    IFS='|' read -r u_load u_active u_file <<<"$st"
    if [ ! -f "$cfg" ]; then
        record rgbd.unit leds "RGB keeper unit" SKIP "no user config" "$cfg missing" "install -m 0644 tools/oxp3-rgbd.toml.example ~/.config/oxp3-rgb.toml && systemctl --user enable --now oxp3-rgbd"
        record rgbd.check leds "RGB keeper config + frames" SKIP "no user config" "$cfg missing" "same: install the example config, then tools/oxp3-rgbd.py --check"
    else
        if [ "$u_load" = "?" ] || [ -z "$u_load" ]; then
            record rgbd.unit leds "RGB keeper unit" UNKNOWN "no user systemd instance" "systemctl --user is unavailable in this context" "run inside the logged-in desktop session"
        elif [ "$u_file" = disabled ] || [ "$u_active" = inactive ]; then
            record rgbd.unit leds "RGB keeper unit" SKIP "HHD owns the RGB zones (per-zone daemon stopped/disabled by the handback tool)" "UnitFileState=$u_file ActiveState=$u_active" "per-zone custom colours again: tools/oxp3-rgb-handback.sh --undo"
        elif [ "$u_load" = not-found ]; then
            record rgbd.unit leds "RGB keeper unit" SKIP "HHD owns the RGB zones (per-zone daemon not installed)" "config present at $cfg but the unit is not installed; HHD drives left/right stick, G-key, top and connector" "per-zone custom colours: re-run the installer with --rgb-daemon, or tools/oxp3-rgb-handback.sh --undo"
        elif [ "$u_active" = active ]; then
            record rgbd.unit leds "RGB keeper unit" PASS "active (running)" "unit oxp3-rgbd.service (user)" ""
        else
            record rgbd.unit leds "RGB keeper unit" FAIL "user unit is $u_active" "LoadState=$u_load ActiveState=$u_active" "journalctl --user -u oxp3-rgbd -b ; systemctl --user restart oxp3-rgbd"
        fi
        local rgbd=$REPO/tools/oxp3-rgbd.py
        if [ -x "$rgbd" ] && have python3; then
            run_tool rgbd-check 30 "$rgbd" --config "$cfg" --check
            kv=$(parse_rgbd_check <"$RUN_OUT")
            local frames rzones cfgline
            frames=$(printf '%s\n' "$kv" | sed -n 's/^FRAMES=//p')
            rzones=$(printf '%s\n' "$kv" | sed -n 's/^ZONES=//p')
            cfgline=$(printf '%s\n' "$kv" | sed -n 's/^CONFIG=//p')
            if [ "$RUN_RC" -eq 0 ] && [ -n "$frames" ]; then
                record rgbd.check leds "RGB keeper config + frames" PASS "config parses, $frames frame(s)" "$cfgline" ""
            else
                record rgbd.check leds "RGB keeper config + frames" FAIL "config/frame check failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 3)" "tools/oxp3-rgbd.py --config $cfg --check ; docs/hw/rgb-coverage.md"
            fi
        else
            record rgbd.check leds "RGB keeper config + frames" SKIP "tool or python3 missing" "" ""
        fi
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): parse_sides([""]) selects
    # nothing, so --sides "" emits zero frames and exits 0. Prove the empty
    # selection is a no-op that never widens to "all zones", and that a real
    # selection still encodes frames -- both with --dry-run, so nothing is sent.
    if [ ! -x "$led" ] || ! have python3; then
        record led.sides leds "empty --sides is a no-op" SKIP "oxp3-led.py or python3 missing" "$led" ""
    else
        local eframes oframes empty_rc one_rc
        run_tool led-sides-empty 30 "$led" --dry-run solid 0000ff --sides ''
        empty_rc=$RUN_RC
        eframes=$(parse_dry_frames <"$RUN_OUT" | sed -n 's/^FRAMES=//p')
        run_tool led-sides-one 30 "$led" --dry-run solid 0000ff --sides 6
        one_rc=$RUN_RC
        oframes=$(parse_dry_frames <"$RUN_OUT" | sed -n 's/^FRAMES=//p')
        if [ "$empty_rc" -eq 0 ] && [ "${eframes:-x}" = 0 ] && [ "$one_rc" -eq 0 ] && [ "${oframes:-0}" -gt 0 ]; then
            record led.sides leds "empty --sides is a no-op" PASS "empty selection: 0 frames; --sides 6: $oframes frame(s) (dry-run)" "a non-empty operand that selects nothing is also silent (deferred: raise instead)" ""
        elif [ "$empty_rc" -eq 0 ] && [ "${eframes:-x}" != 0 ]; then
            record led.sides leds "empty --sides is a no-op" FAIL "empty --sides emitted ${eframes:-?} frame(s)" "an empty operand must not select every zone" "python3 tools/oxp3-led.py --dry-run solid 0000ff --sides '' ; fix parse_sides() in tools/oxp3_hid.py"
        else
            record led.sides leds "empty --sides is a no-op" FAIL "dry-run failed (empty rc=$empty_rc, real rc=$one_rc)" "$(tail_flat "$RUN_OUT" 2)" "python3 tools/oxp3-led.py --dry-run solid 0000ff --sides 6"
        fi
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): the rgbd journal watcher,
    # its settle window and the debounce are event-driven. The one precondition
    # that is observable read-only is whether the hhd journal is readable at
    # all; without it the daemon silently falls back to hhdctl polling.
    local rgbdex=$REPO/tools/oxp3-rgbd.toml.example
    if [ ! -x "$REPO/tools/oxp3-rgbd.py" ] || ! have python3 || [ ! -f "$rgbdex" ]; then
        record rgbd.timing leds "rgbd journal/settle path" SKIP "oxp3-rgbd.py, python3 or the example config missing" "$rgbdex" ""
    else
        local tdev tjournal tframes
        run_tool rgbd-timing 30 "$REPO/tools/oxp3-rgbd.py" --config "$rgbdex" --check
        kv=$(parse_rgbd_check <"$RUN_OUT")
        tdev=$(printf '%s\n' "$kv" | sed -n 's/^DEVICE=//p')
        tjournal=$(printf '%s\n' "$kv" | sed -n 's/^JOURNAL=//p')
        tframes=$(printf '%s\n' "$kv" | sed -n 's/^FRAMES=//p')
        if [ -n "$tdev" ] && [ "$tjournal" = readable ] && [ "${tframes:-0}" -gt 0 ]; then
            record rgbd.timing leds "rgbd journal/settle path" PASS "hhd journal readable, vendor node $tdev, example config encodes ${tframes} frame(s)" "settle/debounce re-applies are logged by the daemon; check journalctl --user -u oxp3-rgbd after a resume" ""
        elif [ -z "$tdev" ]; then
            record rgbd.timing leds "rgbd journal/settle path" SKIP "vendor hidraw node absent" "$(tail_flat "$RUN_OUT" 1)" "install the udev rule / connect the device, then: tools/oxp3-rgbd.py --config $rgbdex --check"
        elif [ "$tjournal" != readable ]; then
            record rgbd.timing leds "rgbd journal/settle path" SKIP "hhd journal not readable: the daemon would poll hhdctl instead" "journal: ${tjournal:-?}" "sudo systemctl status hhd ; journalctl -u hhd -n 5 ; then re-run tools/oxp3-rgbd.py --config $rgbdex --check"
        else
            record rgbd.timing leds "rgbd journal/settle path" FAIL "check failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-rgbd.py --config $rgbdex --check"
        fi
    fi

    [ "$FULL" -eq 1 ] || return 0
    if [ -z "$node" ] || { [ ! -w "$node" ] && ! is_root; }; then
        record led.write leds "LED write test" SKIP "hidraw node not writable" "${node:-no vendor node}" "install the udev rule, or run: sudo tools/oxp3-verify.sh --full"
        return 0
    fi
    if [ ! -x "$led" ]; then
        record led.write leds "LED write test" SKIP "tool missing" "$led" ""
        return 0
    fi
    run_tool led-write 30 "$led" solid 0000ff --sides 6
    if [ "$RUN_RC" -eq 0 ] && grep -q '^send ' "$RUN_OUT" 2>/dev/null; then
        local restore=""
        if [ -f "$HOME/.config/oxp3-rgb.toml" ] && [ -x "$REPO/tools/oxp3-rgbd.py" ]; then
            run_tool led-restore 30 "$REPO/tools/oxp3-rgbd.py" --config "$HOME/.config/oxp3-rgb.toml" --once
            restore="restored ~/.config/oxp3-rgb.toml"
        else
            run_tool led-restore 30 "$led" off --sides 6
            restore="zone 06 left off (no rgbd config to restore)"
        fi
        record led.write leds "LED write test" PASS "one solid frame to zone 06 accepted" "$(grep -m 1 '^send ' "$RUN_OUT"); $restore" ""
    else
        record led.write leds "LED write test" FAIL "write rejected (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-led.py probe ; check the udev rule and /dev/hidraw*"
    fi
}

# ------------------------------------------------------------------ IMU (bonus)
check_imu() {
    local tool=$REPO/tools/oxp3-imu.py
    if [ ! -x "$tool" ] || ! have python3; then
        record imu.selftest imu "IMU usable, gyro to Steam" SKIP "tool or python3 missing" "$tool" ""
        return 0
    fi
    run_tool imu-selftest 90 "$tool" selftest
    local res
    res=$(grep -m 1 '^RESULT:' "$RUN_OUT" 2>/dev/null)
    if [ "$RUN_RC" -eq 0 ] && printf '%s' "$res" | grep -q 'PASS'; then
        record imu.selftest imu "IMU usable, gyro to Steam" PASS "${res:-RESULT: PASS}" "" ""
    elif [ "$RUN_RC" -eq 2 ]; then
        record imu.selftest imu "IMU usable, gyro to Steam" SKIP "IMU not found" "$(tail_flat "$RUN_OUT" 2)" "docs/hw/camera-imu.md"
    else
        record imu.selftest imu "IMU usable, gyro to Steam" FAIL "selftest failed (rc=$RUN_RC)" "${res:-$(tail_flat "$RUN_OUT" 2)}" "tools/oxp3-imu.py info ; docs/hw/camera-imu.md"
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): find_iio()'s fallback takes
    # the first i2c-* node with any non-empty in_anglvel_x_raw, so a different
    # IIO device could win. On hardware, prove the primary key (ACPI id
    # 10EC5280 / label accel-display) is what the tool actually selected.
    local imap iprim ipc ifbc isel
    imap=$(parse_iio_devices /sys/bus/iio/devices)
    iprim=$(printf '%s\n' "$imap" | sed -n 's/^PRIMARY=//p')
    ipc=$(printf '%s\n' "$imap" | sed -n 's/^PRIMARY_COUNT=//p')
    ifbc=$(printf '%s\n' "$imap" | sed -n 's/^FALLBACK_COUNT=//p')
    if [ "${ipc:-0}" -eq 0 ]; then
        if [ "${ifbc:-0}" -gt 0 ]; then
            record imu.iio imu "IIO primary-key selection" SKIP "no ACPI-id/label match; the i2c fallback would be used" "fallback candidates: ${ifbc}" "docs/hw/camera-imu.md ; check /sys/bus/iio/devices/iio:device*/name and label"
        else
            record imu.iio imu "IIO primary-key selection" SKIP "no IIO device" "" "docs/hw/camera-imu.md"
        fi
    else
        run_tool imu-info 30 "$tool" -j info
        isel=$(json_get "$RUN_OUT" sysfs 2>/dev/null) || isel=""
        if [ -n "$isel" ] && [ "$(basename "$isel")" = "$iprim" ]; then
            record imu.iio imu "IIO primary-key selection" PASS "selected $iprim by ACPI id/label, not the i2c fallback" "fallback candidates present: ${ifbc}" ""
        elif [ -n "$isel" ]; then
            record imu.iio imu "IIO primary-key selection" FAIL "selected $(basename "$isel") while $iprim matches the primary key" "the fallback accepted a different device (a non-numeric in_anglvel_x_raw counts)" "tools/oxp3-imu.py -j info ; check name/label of /sys/bus/iio/devices/iio:device*/ ; see docs/UNIT-TESTS.md section 5"
        else
            record imu.iio imu "IIO primary-key selection" UNKNOWN "imu info gave no sysfs path (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-imu.py -j info"
        fi
    fi
}

# ------------------------------------------------------------------ 2b. daemons
# Lifecycle of the three long-running daemons (docs/DAEMONS.md): does each one
# currently hold a live device fd, and did this boot's journal show a device
# loss that recovered? Read-only; the root daemon's fds degrade to SKIP.
check_lifecycle() {
    local lines fd_kv verdict why missing active checked denied perapply
    local spec unit user jtext jkv l r o lost=0 recovered=0 open_lines=0
    local saw_journal=0 pending="" details=""

    lines="$(daemon_fd_lines oxp3-voltap.service 0)
$(daemon_fd_lines oxp3-homebtn.service 1)
$(daemon_fd_lines oxp3-rgbd.service 1)"
    if [ "$FULL" -ne 1 ] && journal_text oxp3-voltap             | grep -q 'OBSERVE: hhd owns the volume path'; then
        VOLTAP_OBSERVE=1
    fi
    fd_kv=$(printf '%s\n' "$lines" | parse_fd_report)
    verdict=$(printf '%s\n' "$fd_kv" | sed -n 's/^VERDICT=//p')
    why=$(printf '%s\n' "$fd_kv" | sed -n 's/^WHY=//p')
    missing=$(printf '%s\n' "$fd_kv" | sed -n 's/^MISSING=//p')
    active=$(printf '%s\n' "$fd_kv" | sed -n 's/^ACTIVE=//p')
    checked=$(printf '%s\n' "$fd_kv" | sed -n 's/^CHECKED=//p')
    denied=$(printf '%s\n' "$fd_kv" | sed -n 's/^DENIED=//p')
    perapply=$(printf '%s\n' "$fd_kv" | sed -n 's/^PERAPPLY=//p')
    volskip=$(printf '%s\n' "$fd_kv" | sed -n 's/^VOLSKIP=//p')

    case ${verdict:-skip} in
        pass)
            record life.fd lifecycle "live device fd per daemon" PASS \
                "every inspectable active daemon holds its device" "$why" "" ;;
        fail)
            record life.fd lifecycle "live device fd per daemon" FAIL \
                "missing device fd: ${missing:-unknown}" "$why" \
                "journalctl --user -u oxp3-homebtn -u oxp3-rgbd -b ; journalctl -u oxp3-voltap -b ; then restart the unit (docs/DAEMONS.md 5)" ;;
        *)
            if [ "${denied:-0}" -gt 0 ] && [ "${checked:-0}" -eq 0 ]; then
                record life.fd lifecycle "live device fd per daemon" SKIP \
                    "oxp3-voltap runs as root; /proc/<pid>/fd needs root" "$why" \
                    "sudo tools/oxp3-verify.sh --full (the check itself is read-only)"
            elif [ "${active:-0}" -eq 0 ]; then
                record life.fd lifecycle "live device fd per daemon" SKIP \
                    "no lifecycle daemon active" "$why" \
                    "install/enable oxp3-homebtn, oxp3-rgbd or oxp3-voltap (docs/DAEMONS.md)"
            elif [ "${volskip:-0}" -gt 0 ] && [ "${perapply:-0}" -eq 0 ]; then
                record life.fd lifecycle "live device fd per daemon" SKIP \
                    "volume daemon is in observe mode (hhd owns the volume path)" \
                    "$why" \
                    "supported path: install the hhd plugin (tools/hhd-oxp3-voltap); docs/DAEMONS.md"
            else
                record life.fd lifecycle "live device fd per daemon" SKIP \
                    "only the RGB keeper is active; it opens the node per apply" \
                    "active=${active:-0} checked=${checked:-0} per-apply=${perapply:-0}" \
                    "install/enable oxp3-homebtn or oxp3-voltap for a persistent fd"
            fi
            ;;
    esac

    local flags
    for spec in "oxp3-voltap.service:0" "oxp3-homebtn.service:1" "oxp3-rgbd.service:1"; do
        unit=${spec%:*}; user=${spec##*:}
        if [ "$user" = 1 ]; then flags="--user"; else flags=""; fi
        jtext=$(journal_text "$unit" $flags)
        [ -n "$jtext" ] || continue
        saw_journal=1
        jkv=$(printf '%s\n' "$jtext" | parse_lifecycle_journal)
        l=$(printf '%s\n' "$jkv" | sed -n 's/^LOST=//p')
        r=$(printf '%s\n' "$jkv" | sed -n 's/^RECOVERED=//p')
        o=$(printf '%s\n' "$jkv" | sed -n 's/^OPEN=//p')
        lost=$((lost + ${l:-0}))
        recovered=$((recovered + ${r:-0}))
        open_lines=$((open_lines + ${o:-0}))
        if [ "${l:-0}" -gt 0 ] && [ "${r:-0}" -eq 0 ]; then
            pending="$pending $unit"
        fi
        details="$details $unit(open=${o:-0},lost=${l:-0})"
    done

    if [ "$saw_journal" -eq 0 ]; then
        record life.reconnect lifecycle "device loss recovered" SKIP \
            "no lifecycle journal in this boot" "" \
            "start one of the daemons, then: journalctl --user -u oxp3-homebtn -b"
    elif [ -n "$pending" ]; then
        record life.reconnect lifecycle "device loss recovered" FAIL \
            "device lost and never reopened:$pending" \
            "lost=$lost recovered=$recovered open=$open_lines" \
            "journalctl --user -u oxp3-homebtn -u oxp3-rgbd -b ; journalctl -u oxp3-voltap -b ; docs/DAEMONS.md 2"
    elif [ "$lost" -gt 0 ]; then
        record life.reconnect lifecycle "device loss recovered" PASS \
            "$lost loss(es) recovered since boot" "open=$open_lines$details" ""
    else
        record life.reconnect lifecycle "device loss recovered" PASS \
            "no device loss this boot" "open=$open_lines$details" ""
    fi

    if [ "$FULL" -eq 0 ]; then
        record life.sim lifecycle "reconnect/suspend simulation" SKIP \
            "gated: only with --full" "" \
            "sudo tools/oxp3-verify.sh --full   (or: python3 -m unittest discover -s tests/unit -p 'test_*lifecycle.py' -q)"
        return 0
    fi
    if [ ! -d "$REPO/tests/unit" ] || ! have python3; then
        record life.sim lifecycle "reconnect/suspend simulation" SKIP \
            "tests/unit or python3 missing" "$REPO/tests/unit" ""
        return 0
    fi
    run_tool life-sim 180 python3 -m unittest discover -s "$REPO/tests/unit" \
        -p 'test_*lifecycle.py' -q
    if [ "$RUN_RC" -eq 0 ]; then
        record life.sim lifecycle "reconnect/suspend simulation" PASS \
            "offline reconnect/backoff/suspend suite passed" \
            "$(grep -E '^Ran ' "$RUN_OUT" 2>/dev/null | tail -n 1)" ""
    else
        record life.sim lifecycle "reconnect/suspend simulation" FAIL \
            "lifecycle unit suite failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 3)" \
            "python3 -m unittest discover -s tests/unit -p 'test_*lifecycle.py' -v"
    fi
}

# ------------------------------------------------------------------ 3. sleep
# Decide whether a suspend *request* has a usable path. Pure function over the
# already-parsed facts, so --selftest can pin every branch without a host:
#   DROP_OK POL_OK HPOWER HSUSPEND BSLEEP PKCHECK -> verdict keyword
# BSLEEP is the number of "block" inhibitors of type sleep (the only kind that can
# refuse a suspend). PKCHECK is the pkcheck verdict for org.freedesktop.login1.suspend.
trigger_verdict() {
    local drop_ok=$1 pol_ok=$2 hpower=$3 hsusp=$4 bsleep=$5 pk=$6
    if [ "$drop_ok" -eq 0 ] && [ "$pol_ok" -eq 0 ]; then
        printf 'skip-policy'
    elif [ "${bsleep:-0}" != 0 ]; then
        printf 'fail-blocked'
    elif [ "$drop_ok" -eq 0 ] || [ "$pol_ok" -eq 0 ]; then
        printf 'fail-incomplete'
    elif [ -z "$hpower" ]; then
        printf 'skip-unreadable'
    elif [ "$hpower" != suspend ] || [ "$hsusp" != suspend ]; then
        printf 'fail-keys'
    elif [ "$pk" != yes ]; then
        printf 'fail-auth'
    else
        printf 'pass'
    fi
}

check_sleep() {
    local mem
    mem=$(read1 /sys/power/mem_sleep)
    if [ -z "$mem" ]; then
        record sleep.mem sleep "s2idle selected" SKIP "/sys/power/mem_sleep unreadable" "" "docs/hw/sleep-power.md"
    elif printf '%s' "$mem" | grep -q '\[s2idle\]'; then
        record sleep.mem sleep "s2idle selected" PASS "mem_sleep: $mem" "" ""
    else
        record sleep.mem sleep "s2idle selected" FAIL "s2idle is not the selected sleep state" "mem_sleep: $mem" "docs/hw/sleep-power.md (no cmdline change is normally needed)"
    fi

    local kv success fail last_ total_ max_ tuple verdict secs
    kv=$(parse_suspend_stats /sys/power/suspend_stats)
    success=$(printf '%s\n' "$kv" | sed -n 's/^SUCCESS=//p')
    fail=$(printf '%s\n' "$kv" | sed -n 's/^FAIL=//p')
    last_=$(printf '%s\n' "$kv" | sed -n 's/^LAST_HW_SLEEP=//p')
    total_=$(printf '%s\n' "$kv" | sed -n 's/^TOTAL_HW_SLEEP=//p')
    max_=$(printf '%s\n' "$kv" | sed -n 's/^MAX_HW_SLEEP=//p')
    tuple="success=${success:-?} fail=${fail:-?} last_hw_sleep=${last_:-n/a}us total_hw_sleep=${total_:-n/a}us max_hw_sleep=${max_:-n/a}us"
    verdict=$(suspend_stats_verdict "$success" "$fail" "$last_" "$max_")
    case $verdict in
        unreadable)
            record sleep.stats sleep "real suspend recorded" SKIP "suspend_stats unreadable" "$tuple" "is CONFIG_PM_SLEEP_STATS enabled? docs/hw/sleep-power.md"
            ;;
        pass)
            secs=$(awk -v u="$last_" 'BEGIN{printf "%.1f", u / 1000000}')
            record sleep.stats sleep "real suspend recorded" PASS "last s2idle sleep ${secs}s (success=$success, fail=$fail)" "$tuple" ""
            ;;
        pass-nohw)
            record sleep.stats sleep "real suspend recorded" PASS "$success completed suspend(s) counted" "$tuple: this kernel does not expose the hardware-sleep attributes (the ACPI FADT has no Low-Power S0 flag), so the duration is not measurable here" ""
            ;;
        fail)
            record sleep.stats sleep "real suspend recorded" FAIL "$fail failed suspend attempt(s)" "$tuple last_failed_dev=$(read1 /sys/power/suspend_stats/last_failed_dev) last_failed_step=$(read1 /sys/power/suspend_stats/last_failed_step)" "dmesg | grep -i suspend ; journalctl -b | grep -i 'PM: suspend'"
            ;;
        unknown-hwzero)
            record sleep.stats sleep "real suspend recorded" UNKNOWN \
                "successful suspend cycle, but the platform PMC reports last_hw_sleep=0" \
                "$tuple: on s2idle this counter can stay 0 even when the cycle completed (it is written from the platform low-power hook, not the generic path); compare the kernel log entry/exit times and CLOCK_BOOTTIME - CLOCK_MONOTONIC before judging it" \
                "journalctl -b -k | grep 'PM: suspend' | tail -4 ; python3 -c 'import time; print(time.clock_gettime(time.CLOCK_BOOTTIME)-time.clock_gettime(time.CLOCK_MONOTONIC))' ; sudo tools/sleep-power-measure.sh suspend -w 60"
            ;;
        unknown-last)
            record sleep.stats sleep "real suspend recorded" UNKNOWN "last_hw_sleep>0 with success=0: not a completed suspend" "$tuple: the hardware-sleep time comes from the platform PMC's resume hook, not from the generic suspend path, so an aborted cycle can leave it non-zero without counting a success; it is never persisted across boots. Compare this boot's kernel log and CLOCK_BOOTTIME-MONOTONIC before believing either number" "journalctl -b -k | grep -c 'PM: suspend entry' ; python3 -c 'import time; print(time.clock_gettime(time.CLOCK_BOOTTIME)-time.clock_gettime(time.CLOCK_MONOTONIC))' ; docs/hw/sleep-power.md 2.6"
            ;;
        unknown-ceiling)
            record sleep.stats sleep "real suspend recorded" UNKNOWN "no completed suspend; max_hw_sleep is a ceiling, not a sleep" "$tuple: max_hw_sleep is the platform PMC's reporting ceiling ((2^32-1) x 122us on this platform, about 6.1 days), set at probe - not persisted and not a measured duration; success=0 does NOT mean the platform cannot suspend" "journalctl -b -k | grep -c 'PM: suspend entry' ; python3 -c 'import time; print(time.clock_gettime(time.CLOCK_BOOTTIME)-time.clock_gettime(time.CLOCK_MONOTONIC))' ; sudo tools/sleep-cycle-test.sh --cycles 1"
            ;;
        *)
            record sleep.stats sleep "real suspend recorded" SKIP "no suspend attempt since boot" "$tuple" "sudo OXP3_ALLOW_SUSPEND=1 tools/oxp3-verify.sh --full --with-changes; or: sudo tools/sleep-power-measure.sh suspend -w 60"
            ;;
    esac

    local st load active file pre log pre_s log_s
    st=$(unit_state oxp3-sleep-wakeup.service)
    IFS='|' read -r load active file <<<"$st"
    if [ "$load" = "?" ] || [ -z "$load" ]; then
        load=not-found
        record sleep.units sleep "suspend policy units" UNKNOWN "systemctl not available" "" "this host is not systemd-managed"
    fi
    pre_s=$(unit_state oxp3-sleep-pre.service)
    log_s=$(unit_state oxp3-sleep-log.service)
    pre=$(printf '%s' "$pre_s" | cut -d'|' -f2)
    log=$(printf '%s' "$log_s" | cut -d'|' -f2)
    if [ "$load" = not-found ]; then
        record sleep.units sleep "suspend policy units" SKIP "not installed" "LoadState=not-found" "sudo tools/oxp3-hw-install.sh --sleep"
    elif [ "$active" = active ]; then
        record sleep.units sleep "suspend policy units" PASS "wakeup policy applied" "wakeup=active pre=$pre log=$log lowpower=$(unit_state oxp3-lowpower.service | cut -d'|' -f2)" ""
    else
        record sleep.units sleep "suspend policy units" FAIL "oxp3-sleep-wakeup is $active" "LoadState=$load pre=$pre log=$log" "journalctl -u oxp3-sleep-wakeup -b ; sudo systemctl restart oxp3-sleep-wakeup"
    fi

    local rule=/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules installed=0
    [ "$load" != not-found ] && installed=1
    [ -f "$rule" ] && installed=1
    if [ "$installed" -eq 0 ]; then
        record sleep.wakeup sleep "wake sources disarmed" SKIP "sleep policy not installed" "no unit and no $rule" "sudo tools/oxp3-hw-install.sh --sleep"
    else
        local enabled="" addr pat d checked=0
        for pat in "1a86:fe00" "1997:2433" "045e:028e"; do
            for d in /sys/bus/usb/devices/*; do
                [ -r "$d/idVendor" ] && [ -r "$d/idProduct" ] || continue
                [ "$(read1 "$d/idVendor"):$(read1 "$d/idProduct")" = "$pat" ] || continue
                checked=$((checked + 1))
                [ "$(read1 "$d/power/wakeup")" = enabled ] && enabled="$enabled usb:$pat"
            done
        done
        for addr in 0000:00:06.0 0000:01:00.0 0000:00:1c.0 0000:56:00.0 0000:00:07.0 0000:00:07.1 0000:00:0d.0 0000:00:0d.2 0000:00:14.0; do
            [ -r "/sys/bus/pci/devices/$addr/power/wakeup" ] || continue
            checked=$((checked + 1))
            [ "$(read1 "/sys/bus/pci/devices/$addr/power/wakeup")" = enabled ] && enabled="$enabled pci:$addr"
        done
        if [ -z "$enabled" ]; then
            record sleep.wakeup sleep "wake sources disarmed" PASS "$checked wake-capable device(s) disabled" "the udev rule re-applies this after every resume/hotplug" ""
        else
            record sleep.wakeup sleep "wake sources disarmed" FAIL "wake sources still enabled:$enabled" "" "sudo systemctl restart oxp3-sleep-wakeup && sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=pci --subsystem-match=usb ; docs/hw/sleep-power.md"
        fi
    fi

    # ---- deep low-power policy (R6): the mmc wake source is not a PCI/USB
    # device, and the measured mmc0 is a *virtual* wakeup source (no device
    # link, so no power/wakeup bit to write); it is handled by parking the SD
    # host around suspend.  ASPM and the NVMe PS state say whether the storage
    # links are allowed to idle.
    local lp=/usr/local/libexec/oxp3-lowpower sd_unit=/etc/systemd/system/oxp3-sleep-lowpower-pre.service
    local mmc_seen=0 mmc_armed=0 mmc_virtual=0 mmc_names="" d name dev wake
    for d in /sys/class/wakeup/wakeup*; do
        [ -r "$d/name" ] || continue
        name=$(read1 "$d/name")
        case $name in mmc*) ;; *) continue ;; esac
        mmc_seen=$((mmc_seen + 1))
        mmc_names="$mmc_names $name"
        wake=""
        if [ -e "$d/device" ]; then
            dev=$(readlink -f "$d/device" 2>/dev/null || true)
            [ -n "$dev" ] && [ -e "$dev/power/wakeup" ] && wake="$dev/power/wakeup"
        fi
        if [ -n "$wake" ]; then
            [ "$(read1 "$wake")" = enabled ] && mmc_armed=$((mmc_armed + 1))
        else
            mmc_virtual=$((mmc_virtual + 1))
        fi
    done
    if [ "$mmc_seen" -eq 0 ]; then
        record sleep.mmcwake sleep "mmc wake source disarmed or parked" SKIP "no mmc wakeup source on this host" "no /sys/class/wakeup/*/name matches mmc*" ""
    elif [ "$mmc_armed" -gt 0 ]; then
        record sleep.mmcwake sleep "mmc wake source disarmed or parked" FAIL "$mmc_armed of $mmc_seen mmc wakeup source(s) still armed" "name(s):$mmc_names - a storage host that can wake the machine keeps the reader link out of its deepest state" "sudo systemctl restart oxp3-lowpower ; oxp3-lowpower status ; docs/hw/sleep-power.md R6"
    elif [ "$mmc_virtual" -gt 0 ] && [ ! -e "$sd_unit" ] && [ ! -e "$lp" ]; then
        record sleep.mmcwake sleep "mmc wake source disarmed or parked" SKIP "virtual mmc wakeup source, park policy not installed" "name(s):$mmc_names have no device link, so no power/wakeup exists to disable; the SD-host park removes the stall" "sudo tools/oxp3-hw-install.sh --sleep"
    else
        record sleep.mmcwake sleep "mmc wake source disarmed or parked" PASS "no armed mmc wakeup bit ($mmc_virtual virtual source(s) parked)" "name(s):$mmc_names; a virtual source shows wakeup_count=0 - it did not abort a suspend" ""
    fi

    # sleep.sdcard: the SD host is bound while awake (a leftover park means the
    # post-resume unit did not rebind, which would leave the reader dead).
    local sd_drv=/sys/bus/platform/drivers/rtsx_pci_sdmmc sd_state=/var/lib/oxp3-lowpower/state.json
    if [ ! -e "$sd_drv" ]; then
        record sleep.sdcard sleep "SD host parked only across suspend" SKIP "no rtsx_pci_sdmmc driver on this host" "" ""
    elif [ ! -e "$sd_drv/rtsx_pci_sdmmc.0" ]; then
        record sleep.sdcard sleep "SD host parked only across suspend" FAIL "the SD host is unbound while the system is awake" "a parked host must be rebound after every resume; the card reader is dead until it is" "sudo systemctl restart oxp3-lowpower ; oxp3-lowpower status ; docs/hw/sleep-power.md R6"
    elif have python3 && python3 - "$sd_state" <<'SDSTATE' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if (d.get("sd_host") or {}).get("unbound") is True else 1)
SDSTATE
    then
        record sleep.sdcard sleep "SD host parked only across suspend" FAIL "state says 'unbound' but the host is bound: a previous park was not recorded as restored" "$sd_state" "sudo systemctl restart oxp3-lowpower ; journalctl -u oxp3-sleep-lowpower-post -b"
    elif [ -e "$sd_unit" ] || [ -e "$lp" ]; then
        record sleep.sdcard sleep "SD host parked only across suspend" PASS "bound now; parked only across suspend by the pre/post units" "$(read1 "$sd_drv/rtsx_pci_sdmmc.0/driver" 2>/dev/null || true)" ""
    else
        record sleep.sdcard sleep "SD host parked only across suspend" SKIP "park policy not installed" "" "sudo tools/oxp3-hw-install.sh --sleep"
    fi

    local aspm_file=/sys/module/pcie_aspm/parameters/policy aspm_sel
    if [ ! -e "$aspm_file" ]; then
        record sleep.aspm sleep "PCIe ASPM powersave" SKIP "no pcie_aspm policy parameter" "the module is not built or not loaded" ""
    else
        aspm_sel=$(aspm_selected <"$aspm_file")
        if [ ! -e "$lp" ] && [ ! -e /etc/systemd/system/oxp3-lowpower.service ]; then
            record sleep.aspm sleep "PCIe ASPM policy" SKIP "low-power policy not installed" "pcie_aspm policy: $aspm_sel" "sudo tools/oxp3-hw-install.sh --sleep"
        elif [ "$aspm_sel" = powersave ]; then
            record sleep.aspm sleep "PCIe ASPM policy" PASS "pcie_aspm.policy: powersave (opt-in, owner asked for it)" "" ""
        elif [ -z "$aspm_sel" ] || [ "$aspm_sel" = default ]; then
            # ASPM is opt-in: powersave measured 0% S0ix and a worse sleep-only rate
            # on this platform, so leaving the firmware/ACPI default is the healthy state.
            record sleep.aspm sleep "PCIe ASPM policy" PASS "pcie_aspm.policy: ${aspm_sel:-default} (firmware default; ASPM opt-in not enabled - powersave measured 0% S0ix here)" "" ""
        else
            record sleep.aspm sleep "PCIe ASPM policy" FAIL "pcie_aspm.policy is $aspm_sel" "$(read1 "$aspm_file")" "set it back to default: echo default | sudo tee /sys/module/pcie_aspm/parameters/policy ; docs/hw/sleep-power.md R6"
        fi
    fi

    local gs_sha=/var/lib/oxp3-fixes/gamescope.sha256 gs_want gs_cur gs_rel
    gs_rel=$(rpm -q --qf '%{RELEASE}\n' gamescope 2>/dev/null | head -1)
    gs_want=$(cat "$gs_sha" 2>/dev/null | head -1)
    gs_cur=$(sha256sum /usr/bin/gamescope 2>/dev/null | cut -d' ' -f1)
    if [ -n "$gs_want" ] && [ "$gs_cur" = "$gs_want" ]; then
        record gfx.gamescope graphics "gamescope PQ HDR build" PASS "patched build installed (recorded digest)" "$gs_want" ""
    elif [ "${gs_rel}" != "${gs_rel#*oxp3}" ]; then
        record gfx.gamescope graphics "gamescope PQ HDR build" PASS "patched build installed (release $gs_rel)" "" ""
    else
        record gfx.gamescope graphics "gamescope PQ HDR build" FAIL "stock gamescope (${gs_rel:-absent}) has no PQ HDR; Gamemode HDR is black or washed out" "expected the oxp3 build from patches/gamescope" "sudo tools/oxp3-hw-install.sh --gamescope ; docs/hw/hdr-vibrance.md"
    fi

    local nv_n=/sys/class/nvme/nvme0 nv_lat nv_force
    if [ ! -d "$nv_n" ]; then
        record sleep.nvme sleep "NVMe power states allowed" SKIP "no NVMe controller present" "" ""
    else
        nv_lat=$(read1 /sys/module/nvme_core/parameters/default_ps_max_latency_us)
        nv_force=$(read1 /sys/module/nvme_core/parameters/force_apst)
        if [ -z "$nv_lat" ]; then
            record sleep.nvme sleep "NVMe power states allowed" SKIP "nvme_core parameters unreadable" "controller $nv_n is present" ""
        elif [ "$nv_lat" -gt 0 ] && [ "$nv_force" != Y ]; then
            record sleep.nvme sleep "NVMe power states allowed" PASS "ps_max_latency=${nv_lat}us force_apst=${nv_force:-N}" "$(read1 "$nv_n/model" 2>/dev/null || true)" ""
        else
            record sleep.nvme sleep "NVMe power states allowed" FAIL "ps_max_latency=${nv_lat}us force_apst=${nv_force:-N}: APST is disabled" "that makes the SSD hotter and cannot lower suspend drain" "do not set default_ps_max_latency_us=0 (docs/hw/sleep-power.md R4)"

    # The Predator GM7 / Biwin 1dee:1602 does not resume from s2idle through the
    # NVMe driver's ACPI simple-suspend path on Panther Lake; nvme.noacpi=1 is the
    # known fix (same drive and kernel family in the ONEXPLAYER 3 SteamOS fix pack).
    if lspci -nn 2>/dev/null | grep -qi '1dee:1602'; then
        if grep -qw 'nvme.noacpi=1' /proc/cmdline 2>/dev/null; then
            record sleep.nvmeacpi sleep "NVMe ACPI suspend quirk" PASS \
                "nvme.noacpi=1 active (the 1dee:1602 drive needs it to resume)" \
                "$(lspci -nn 2>/dev/null | grep -i '1dee:1602' | cut -c1-40)" ""
        else
            record sleep.nvmeacpi sleep "NVMe ACPI suspend quirk" SKIP \
                "1dee:1602 NVMe present without nvme.noacpi=1: this drive has failed to resume from s2idle" \
                "apply it with sudo tools/oxp3-nvme-suspend.sh apply (persistent, reversible)" \
                "sudo tools/oxp3-nvme-suspend.sh apply ; reboot"
        fi
    else
        record sleep.nvmeacpi sleep "NVMe ACPI suspend quirk" SKIP \
            "no 1dee:1602 NVMe (the quirk targets that drive only)" "" ""
    fi
        fi
    fi

    # ---- trigger: is there a path from "sleep" to /sys/power/state that does not
    # depend on a desktop session answering a key? Strictly read-only: the drop-in
    # file, its *parsed* form via systemd-analyze cat-config, the polkit rule, the
    # pkcheck verdict for login1.suspend (a permission query, it never suspends) and
    # the inhibitor list.
    local drop=/etc/systemd/logind.conf.d/60-oxp3-sleep.conf
    local pol=/etc/polkit-1/rules.d/49-oxp3-suspend.rules
    local drop_ok=0 pol_ok=0
    [ -f "$drop" ] && drop_ok=1
    [ -f "$pol" ] && pol_ok=1
    local lh="" hpower="" hsusp="" hlid="" rulekv="" inh=""
    local pk="" bsleep="" bhandle="" bswho="" bhwho="" sess=""
    if have systemd-analyze; then
        lh=$(systemd-analyze cat-config systemd/logind.conf 2>/dev/null | parse_logind_handles)
        hpower=$(printf '%s\n' "$lh" | sed -n 's/^POWER=//p')
        hsusp=$(printf '%s\n' "$lh" | sed -n 's/^SUSPEND=//p')
        hlid=$(printf '%s\n' "$lh" | sed -n 's/^LID=//p')
    fi
    [ "$pol_ok" -eq 1 ] && rulekv=$(parse_polkit_rule <"$pol")
    pk=$(pkcheck_verdict org.freedesktop.login1.suspend)
    # /etc/polkit-1/rules.d is 0750 root:polkitd, so an unprivileged run cannot
    # stat the rule file even when it is installed. The authoritative answer is
    # pkcheck, which asks polkit itself whether this user may suspend.
    if [ "$pol_ok" -eq 0 ] && [ ! -r "$(dirname "$pol")" ]; then
        case ${pk:-} in
            yes|YES|allowed|ALLOWED|auth_admin_keep|auth_admin|no-challenge*) pol_ok=1 ;;
        esac
    fi
    if have systemd-inhibit; then
        inh=$(systemd-inhibit --list --no-pager 2>/dev/null | parse_inhibitors)
        bsleep=$(printf '%s\n' "$inh" | sed -n 's/^BLOCK_SLEEP=//p')
        bhandle=$(printf '%s\n' "$inh" | sed -n 's/^BLOCK_HANDLES=//p')
        bswho=$(printf '%s\n' "$inh" | sed -n 's/^SLEEP_WHO=//p')
        bhwho=$(printf '%s\n' "$inh" | sed -n 's/^HANDLE_WHO=//p')
    fi
    case ${DESKTOP_SESSION:-}:${XDG_CURRENT_DESKTOP:-} in
        *gamescope*|*gamemode*) sess="gamemode: no session polkit agent, needs allow_active=yes or the rule" ;;
        *) sess="desktop: polkit-kde-authentication-agent-1 can prompt" ;;
    esac

    case $(trigger_verdict "$drop_ok" "$pol_ok" "$hpower" "$hsusp" "${bsleep:-0}" "$pk") in
    skip-policy)
        record sleep.trigger sleep "suspend request has a path" SKIP \
            "trigger policy not installed" \
            "logind power=${hpower:-?} suspend=${hsusp:-?} (compiled defaults); pkcheck=${pk:-?}; block-sleep=${bsleep:-?}" \
            "sudo tools/oxp3-hw-install.sh --sleep   (then: sudo systemctl restart systemd-logind; docs/hw/sleep-power.md 2.5)"
        ;;
    fail-blocked)
        record sleep.trigger sleep "suspend request has a path" FAIL \
            "a sleep block inhibitor refuses every suspend" \
            "held by:${bswho:- ?} (systemd-inhibit --list)" \
            "stop or reconfigure the service holding it; docs/hw/sleep-power.md 2.5"
        ;;
    fail-incomplete)
        if [ "${EUID:-$(id -u)}" -ne 0 ] && [ ! -r "$(dirname "$pol")" ]; then
            record sleep.trigger sleep "suspend request has a path" UNKNOWN \
                "logind drop-in present; the polkit rule is not readable without root and pkcheck has no session agent here" \
                "drop-in=$drop rule=$pol pkcheck=${pk:-none}" \
                "run tools/oxp3-verify.sh from a terminal in the desktop session, or as root, to confirm the polkit rule"
            :
        else
        record sleep.trigger sleep "suspend request has a path" FAIL \
            "trigger policy is incomplete (logind drop-in=$drop_ok polkit rule=$pol_ok)" \
            "missing: $([ "$drop_ok" -eq 0 ] && printf '%s ' "$drop")$([ "$pol_ok" -eq 0 ] && printf '%s' "$pol")" \
            "sudo tools/oxp3-hw-install.sh --sleep"
        fi
        ;;
    skip-unreadable)
        record sleep.trigger sleep "suspend request has a path" SKIP \
            "logind configuration not readable (systemd-analyze missing)" \
            "drop-in present at $drop" "systemd-analyze cat-config systemd/logind.conf"
        ;;
    fail-keys)
        record sleep.trigger sleep "suspend request has a path" FAIL \
            "logind does not sleep on the power/suspend key (power=$hpower suspend=$hsusp)" \
            "parsed from systemd-analyze cat-config (poweroff here means a power key would shut down); drop-in $drop" \
            "sudo tools/oxp3-hw-install.sh --sleep ; systemd-analyze cat-config systemd/logind.conf | grep -A6 '^\[Login\]'"
        ;;
    fail-auth)
        record sleep.trigger sleep "suspend request has a path" FAIL \
            "login1.suspend needs interactive auth (pkcheck=$pk)" \
            "rule=$pol yes=$(printf '%s\n' "$rulekv" | sed -n 's/^YES=//p'); $sess" \
            "pkcheck --action-id org.freedesktop.login1.suspend --process \$\$ ; journalctl -u polkit -b"
        ;;
    *)
        record sleep.trigger sleep "suspend request has a path" PASS \
            "logind keys=suspend, login1.suspend permitted, no sleep blocker" \
            "power=$hpower suspend=$hsusp lid=$hlid pkcheck=$pk block-sleep=${bsleep:-0} handle-block=${bhwho:-none} (${bhandle:-0} held); $sess" \
            ""
        ;;
    esac

    local audit=$REPO/tools/sleep-power-measure.sh
    if [ ! -x "$audit" ]; then
        record sleep.audit sleep "suspend audit" SKIP "tool missing" "$audit" ""
    else
        run_tool sleep-audit 30 "$audit" audit
        kv=$(parse_sleep_audit <"$RUN_OUT")
        local amem arec
        amem=$(printf '%s\n' "$kv" | sed -n 's/^MEM=//p')
        arec=$(printf '%s\n' "$kv" | sed -n 's/^RECORD=//p')
        if [ "${arec:-0}" = 1 ]; then
            record sleep.audit sleep "suspend audit" PASS "capability and suspend record readable" "mem_sleep: ${amem:-?}" ""
        else
            record sleep.audit sleep "suspend audit" FAIL "audit produced no suspend record block (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/sleep-power-measure.sh audit"
        fi
    fi

    [ "$FULL" -eq 1 ] || return 0
    local meas=${OXP3_VERIFY_MEASURE_S:-60}
    case $meas in ''|*[!0-9]*) meas=60 ;; esac
    if [ -x "$audit" ]; then
        run_tool sleep-measure $((meas + 90)) "$audit" run -w "$meas" -l verify
        local verdict
        verdict=$(grep -E '^(PASS|WARN|FAIL):' "$RUN_OUT" 2>/dev/null | tail -n 1)
        if [ "$RUN_RC" -eq 0 ]; then
            record sleep.measure sleep "battery drain measurement" PASS "${verdict:-measurement valid}" "" ""
        elif [ "$RUN_RC" -eq 2 ]; then
            record sleep.measure sleep "battery drain measurement" SKIP "measurement invalid" "${verdict:-on AC or no readable battery}" "run on battery: tools/sleep-power-measure.sh run -w ${meas}"
        else
            record sleep.measure sleep "battery drain measurement" FAIL "${verdict:-measurement failed}" "" "tools/sleep-power-measure.sh run -w ${meas} ; docs/hw/sleep-power.md"
        fi
    else
        record sleep.measure sleep "battery drain measurement" SKIP "tool missing" "$audit" ""
    fi

    # The only check in this harness that really suspends the machine. Three
    # independent opt-ins, because a double-click on `--full` must never cost a
    # session: --full (functional mode) + --with-changes (this run may change live
    # state) + OXP3_ALLOW_SUSPEND=1 in the environment. The measurement itself is
    # tools/sleep-power-measure.sh's RTC-wake cycle, so the same numbers land in the
    # same log as a manual `sleep-power-measure.sh suspend`.
    local sus=${OXP3_VERIFY_SUSPEND_S:-60}
    case $sus in ''|*[!0-9]*) sus=60 ;; esac
    if [ "$FULL" -ne 1 ] || [ "$WITH_CHANGES" -ne 1 ] || [ "${OXP3_ALLOW_SUSPEND:-0}" != 1 ]; then
        record sleep.suspend sleep "real suspend proof" SKIP \
            "gated: this is the only check that suspends the machine" \
            "full=$FULL with_changes=$WITH_CHANGES OXP3_ALLOW_SUSPEND=${OXP3_ALLOW_SUSPEND:-unset}" \
            "sudo OXP3_ALLOW_SUSPEND=1 OXP3_VERIFY_SUSPEND_S=${sus} tools/oxp3-verify.sh --full --with-changes   (one real s2idle cycle, ${sus}s)"
        return 0
    fi
    if ! is_root; then
        record sleep.suspend sleep "real suspend proof" SKIP "needs root (rtcwake)" "" "sudo OXP3_ALLOW_SUSPEND=1 tools/oxp3-verify.sh --full --with-changes   (suspends this machine for ${sus}s)"
        return 0
    fi
    if [ ! -x "$audit" ]; then
        record sleep.suspend sleep "real suspend proof" SKIP "tool missing" "$audit" ""
        return 0
    fi
    local before after
    before=$(read1 /sys/power/suspend_stats/success)
    run_tool sleep-suspend $((sus + 240)) "$audit" suspend -w "$sus" -l verify
    after=$(read1 /sys/power/suspend_stats/success)
    local sverdict counter
    sverdict=$(grep -E '^(PASS|WARN|FAIL):' "$RUN_OUT" 2>/dev/null | tail -n 1)
    counter="suspend_stats/success ${before:-?} -> ${after:-?}"
    if [ "$RUN_RC" -eq 0 ] && num_gt "${after:-0}" "${before:-0}"; then
        record sleep.suspend sleep "real suspend proof" PASS "${sverdict:-suspend and resume completed}" "$counter" ""
    elif [ "$RUN_RC" -eq 0 ]; then
        record sleep.suspend sleep "real suspend proof" FAIL "resume completed but the kernel counted no suspend" "$counter" "journalctl -k -b | grep -E 'PM: suspend entry|PM: suspend exit' ; docs/hw/sleep-power.md 5"
    elif [ "$RUN_RC" -eq 2 ]; then
        record sleep.suspend sleep "real suspend proof" FAIL "machine refused to suspend" "$(tail_flat "$RUN_OUT" 3) ($counter)" "journalctl -b | grep -i suspend ; docs/hw/sleep-power.md"
    else
        record sleep.suspend sleep "real suspend proof" FAIL "${sverdict:-suspend measurement failed}" "$counter" "docs/hw/sleep-power.md"
    fi
}

# ------------------------------------------------------------------ 4. Bluetooth
check_bluetooth() {
    local fw=/usr/lib/firmware/updates/intel n=0 missing="" f
    for f in ibt-00a0-0291-iml.sfi ibt-00a0-0291-pci.sfi; do
        if [ -s "$fw/$f" ]; then n=$((n + 1)); else missing="$missing $f"; fi
    done
    if [ "$n" -eq 2 ]; then
        record bt.files bluetooth "firmware in updates/intel" PASS "2/2 pinned files present" "$fw" ""
    else
        record bt.files bluetooth "firmware in updates/intel" FAIL "$n/2 files present" "missing:$missing" "sudo tools/oxp3-bt-firmware.sh   (downloads and verifies the pinned linux-firmware files)"
    fi

    local dc=/etc/dracut.conf.d/60-oxp3-bt-firmware.conf
    if [ -f "$dc" ]; then
        record bt.dracut bluetooth "initramfs drop-in" PASS "dracut install_items drop-in present" "$dc" ""
    else
        record bt.dracut bluetooth "initramfs drop-in" SKIP "no dracut drop-in" "the standalone script writes it; a layered oxp3-firmware rpm can cover the same files" "sudo tools/oxp3-bt-firmware.sh (persist=items) ; or layering oxp3-firmware"
    fi

    local line reload_ts failure_ts
    line=$(journalctl -k -b --no-pager 2>/dev/null | grep -m 1 'Failed to load Intel firmware')
    if [ -z "$line" ]; then
        record bt.journal bluetooth "no firmware-load failure this boot" PASS "kernel journal is clean" "" ""
    else
        # The probe before switch_root cannot see /usr/lib/firmware/updates, so a
        # failure there is expected; what matters is whether the reload service
        # fixed it afterwards. Compare the failure time with the last successful
        # oxp3-bt-reload line (and require the controller to be usable now).
        failure_ts=$(journalctl -k -b --no-pager -o short-unix 2>/dev/null \
            | awk '/Failed to load Intel firmware/ {print $1; exit}')
        reload_ts=$(journalctl -b --no-pager -o short-unix -u oxp3-bt-reload.service 2>/dev/null \
            | awk '/controller present/ {print $1}' | tail -n1)
        if [ -n "${reload_ts:-}" ] && [ -n "${failure_ts:-}" ] && \
           [ "$(printf '%s\n%s\n' "$failure_ts" "$reload_ts" | sort -n | tail -n1)" = "$reload_ts" ] && \
           btmgmt info >/dev/null 2>&1; then
            record bt.journal bluetooth "no firmware-load failure this boot" PASS \
                "the initramfs probe failed as expected, the boot-time reload recovered it and the controller works" \
                "$line" ""
        else
        record bt.journal bluetooth "no firmware-load failure this boot" FAIL "kernel logged a firmware load failure" "$line" "sudo modprobe -r btintel_pcie btintel && sudo modprobe btintel_pcie ; if it recurs, run sudo tools/oxp3-bt-firmware.sh (initramfs) and reboot"
        fi
    fi

    if [ ! -e /sys/class/bluetooth/hci0 ]; then
        record bt.controller bluetooth "controller visible" FAIL "no hci0" "the CNVi radio did not come up" "docs/hw/bluetooth.md ; sudo modprobe btintel_pcie ; journalctl -k -b | grep -i bluetooth"
    elif have btmgmt; then
        run_tool btmgmt 20 btmgmt info
        if grep -qE '^[[:space:]]*addr ' "$RUN_OUT" 2>/dev/null; then
            record bt.controller bluetooth "controller visible" PASS "btmgmt sees the controller" "$(grep -m 1 -E '^[[:space:]]*addr ' "$RUN_OUT")" ""
        elif grep -q 'Index list with 0 items' "$RUN_OUT" 2>/dev/null; then
            record bt.controller bluetooth "controller visible" FAIL "hci0 exists but btmgmt lists 0 controllers (no firmware)" "" "reload btintel_pcie now, or install the initramfs fix and reboot: sudo tools/oxp3-bt-firmware.sh"
        else
            record bt.controller bluetooth "controller visible" FAIL "btmgmt returned no controller" "$(tail_flat "$RUN_OUT" 2)" "journalctl -k -b | grep -i 'Bluetooth: hci0'"
        fi
    elif have bluetoothctl; then
        run_tool btmgmt 20 bluetoothctl show
        if grep -q '^Controller ' "$RUN_OUT" 2>/dev/null; then
            record bt.controller bluetooth "controller visible" PASS "bluetoothctl sees the controller" "$(grep -m 1 '^Controller ' "$RUN_OUT")" ""
        elif grep -q 'No default controller' "$RUN_OUT" 2>/dev/null; then
            record bt.controller bluetooth "controller visible" FAIL "bluetoothctl: no default controller" "" "reload btintel_pcie / install the initramfs fix"
        else
            record bt.controller bluetooth "controller visible" UNKNOWN "bluetoothctl gave no verdict" "$(tail_flat "$RUN_OUT" 2)" ""
        fi
    else
        record bt.controller bluetooth "controller visible" UNKNOWN "hci0 exists; no btmgmt/bluetoothctl to query it" "" "install bluez-btmgmt or bluez"
    fi

    local tool=$REPO/tools/oxp3-bt-firmware.sh
    if [ ! -x "$tool" ]; then
        record bt.diag bluetooth "firmware diagnosis" SKIP "tool missing" "$tool" ""
        return 0
    fi
    run_tool bt-check 60 "$tool" --check
    local kv verdict jfail
    kv=$(parse_bt_check <"$RUN_OUT")
    verdict=$(printf '%s\n' "$kv" | sed -n 's/^VERDICT=//p')
    jfail=$(printf '%s\n' "$kv" | sed -n 's/^JOURNAL_FAIL=//p')
    if [ "$RUN_RC" -eq 0 ]; then
        record bt.diag bluetooth "firmware diagnosis" PASS "radio looks healthy (pinned hashes verified)" "files ok=${n}/2, journal failure=${jfail:-0}" ""
    elif [ "$RUN_RC" -eq 1 ]; then
        record bt.diag bluetooth "firmware diagnosis" FAIL "action needed (${verdict:-pinned files or no firmware this boot})" "files ok=${n}/2, journal failure=${jfail:-0}" "read the verdict above; sudo tools/oxp3-bt-firmware.sh ; docs/hw/bluetooth.md"
    else
        record bt.diag bluetooth "firmware diagnosis" UNKNOWN "diagnosis did not complete (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-bt-firmware.sh --check"
    fi
}

# pwr.rapl is independent of oxp3-powerctl: it comes from oxp3-status --json.
check_pwr_rapl() {
    if ensure_status_json; then
        local rs
        rs=$(ind_state rapl.service) || rs=""
        case $rs in
            ok)
                record pwr.rapl power "rapl-power readings" PASS "$(ind_value rapl.service)" "" ""
                ;;
            warn|fail)
                record pwr.rapl power "rapl-power readings" FAIL "$(ind_value rapl.service)" "" "sudo systemctl restart rapl-power ; docs/hw/sagv.md"
                ;;
            "")
                record pwr.rapl power "rapl-power readings" UNKNOWN "rapl.service indicator missing" "" "tools/oxp3-status.sh --json"
                ;;
            *)
                record pwr.rapl power "rapl-power readings" UNKNOWN "state=$rs" "$(ind_value rapl.service)" ""
                ;;
        esac
    else
        record pwr.rapl power "rapl-power readings" SKIP "status backend unavailable" "" "tools/oxp3-status.sh --json"
    fi
}

# ------------------------------------------------------------------ 5. power / fan
# --with-changes only: this writes pwm1_enable and /etc/oxp3/power.json, so it
# runs only when the caller explicitly accepted persistent changes. Everything
# it touches is snapshotted first and restored (and verified) before the row is
# recorded, so a failed check never leaves the fan in manual mode.
check_fan_savemode() { # PC
    local pc=$1 fan="" h
    for h in /sys/class/hwmon/hwmon*; do
        [ "$(read1 "$h/name")" = oxp_ec ] && { fan=$h; break; }
    done
    if [ -z "$fan" ]; then
        record fan.savemode power "saved fan mode after --save" SKIP "no oxp_ec hwmon fan node" "" "sudo modprobe oxpec ; tools/oxp3-powerctl.py fan status"
        return 0
    fi
    if ! is_root; then
        record fan.savemode power "saved fan mode after --save" SKIP "needs root (writes pwm1_enable and $POWER_STATE)" "" "sudo tools/oxp3-verify.sh --full --with-changes"
        return 0
    fi
    local prev prev_pwm had_state=1 mode save_rc=0 restored=1 now
    prev=$(read1 "$fan/pwm1_enable" || true)
    prev_pwm=$(read1 "$fan/pwm1" || true)
    if [ -f "$POWER_STATE" ]; then
        cp -p -- "$POWER_STATE" "$WORK/power.json.snapshot" 2>/dev/null || had_state=-1
    else
        had_state=0
    fi
    if [ "$had_state" -eq -1 ]; then
        record fan.savemode power "saved fan mode after --save" SKIP "cannot snapshot $POWER_STATE; refusing to write" "" "check permissions on /etc/oxp3, then re-run"
        return 0
    fi

    run_tool fan-manual-save 40 "$pc" fan manual --save
    save_rc=$RUN_RC
    mode=$(json_get "$POWER_STATE" fan.mode 2>/dev/null) || mode=""

    # Restore first, judge afterwards: never leave the fan forced.
    if [ -n "$prev" ]; then
        printf '%s\n' "$prev" >"$fan/pwm1_enable" 2>/dev/null || restored=0
    fi
    if [ "$prev" = 1 ] && [ -n "$prev_pwm" ]; then
        printf '%s\n' "$prev_pwm" >"$fan/pwm1" 2>/dev/null || restored=0
    fi
    if [ "$had_state" -eq 1 ]; then
        cp -p -- "$WORK/power.json.snapshot" "$POWER_STATE" 2>/dev/null || restored=0
    else
        rm -f -- "$POWER_STATE" 2>/dev/null || restored=0
    fi
    now=$(read1 "$fan/pwm1_enable" || true)
    if [ -n "$prev" ] && [ "$now" != "$prev" ]; then restored=0; fi
    if [ "$restored" -ne 1 ]; then
        record fan.savemode power "saved fan mode after --save" FAIL "could not restore the live fan mode (was $prev, now ${now:-?})" "state file restored: $([ "$had_state" -eq 1 ] && echo yes || echo removed)" "echo ${prev:-2} | sudo tee $fan/pwm1_enable ; restore $POWER_STATE from your backup"
    elif [ "$save_rc" -ne 0 ]; then
        record fan.savemode power "saved fan mode after --save" FAIL "fan manual --save failed (rc=$save_rc)" "$(tail_flat "$RUN_OUT" 2)" "sudo tools/oxp3-powerctl.py fan manual --save"
    elif [ "$mode" = manual ]; then
        record fan.savemode power "saved fan mode after --save" PASS "saved state records mode=manual after pwm1_enable was written" "restored pwm1_enable=$prev and the previous state file" ""
    else
        record fan.savemode power "saved fan mode after --save" FAIL "saved state records mode='${mode:-missing}' although manual mode was written" "a later 'restore' would therefore select auto (deferred risk, docs/UNIT-TESTS.md section 5)" "store mode=manual in cmd_fan when pwm1_enable was written, then update docs/UNIT-TESTS.md"
    fi
}

check_power() {
    local pc st_json="" have_json=0
    pc=$(powerctl_cmd)
    if [ -n "$pc" ] && have python3; then
        run_tool powerctl-status 60 "$pc" status --json
        if [ "$RUN_RC" -eq 0 ] && json_ok "$RUN_OUT"; then
            have_json=1
            st_json=$RUN_OUT
        fi
    fi

    if [ "$have_json" -eq 0 ]; then
        record pwr.zones power "powercap zones readable" SKIP "oxp3-powerctl not available" "$( [ -n "$pc" ] && echo "$pc status --json failed (rc=$RUN_RC)" || echo 'not installed and not in the repo')" "sudo tools/oxp3-power-install.sh install ; tools/oxp3-powerctl.py status --json"
        record pwr.pl1 power "PL1 sync invariant" SKIP "no powerctl status" "" "same"
        record pwr.profile power "saved profile applied" SKIP "no powerctl status" "" "same"
        record pwr.fan power "fan sane" SKIP "no powerctl status" "" "same"
        record pwr.adaptive power "adaptive TDP opt-in" SKIP "no powerctl status" "" "same"
        record pwr.ac power "adaptive AC/battery clamp" SKIP "no powerctl status" "" "same"
        record pwr.order power "limit write ordering" SKIP "no powerctl status" "" "same"
        record fan.curvedup power "fan curve duplicate points" SKIP "no powerctl status" "" "same"
        if [ "$FULL" -eq 1 ] && [ "$WITH_CHANGES" -eq 1 ]; then
            record fan.savemode power "saved fan mode after --save" SKIP "no powerctl status" "" "same"
        fi
        check_pwr_rapl
        return 0
    fi

    local doms kinds="" pl1msr="" pl1mmio="" pl2msr="" pl2mmio="" pl4msr="" pl4mmio=""
    doms=$(parse_power_domains "$st_json")
    while IFS=$'\t' read -r kind pl1 pl2 pl4; do
        [ -n "$kind" ] || continue
        kinds="$kinds $kind"
        case $kind in
            msr) pl1msr=$pl1; pl2msr=$pl2; pl4msr=$pl4 ;;
            mmio) pl1mmio=$pl1; pl2mmio=$pl2; pl4mmio=$pl4 ;;
        esac
    done <<<"$doms"

    if printf '%s' "$kinds" | grep -q 'msr' && printf '%s' "$kinds" | grep -q 'mmio' && [ -n "$pl1msr" ] && [ -n "$pl1mmio" ]; then
        record pwr.zones power "powercap zones readable" PASS "both zones readable" "PL1 $(watts "$pl1msr")/$(watts "$pl1mmio") W, PL2 $(watts "$pl2msr")/$(watts "$pl2mmio") W, PL4 $(watts "$pl4msr")/$(watts "$pl4mmio") W (MSR/MMIO)" ""
    else
        record pwr.zones power "powercap zones readable" FAIL "powercap zones incomplete" "domains:${kinds:- none} PL1 msr=${pl1msr:-?} mmio=${pl1mmio:-?}" "sudo modprobe intel_rapl_msr intel_rapl_common ; sudo systemctl restart oxp3-rapl-pp1 ; docs/hw/power-fan.md"
    fi

    if [ -z "$pl1msr" ] || [ -z "$pl1mmio" ]; then
        record pwr.pl1 power "PL1 sync invariant" FAIL "PL1 unreadable on one zone" "msr=${pl1msr:-?} mmio=${pl1mmio:-?}" "docs/hw/power-fan.md"
    elif num_eq "$pl1msr" "$pl1mmio"; then
            if num_eq "$pl1msr" 35000000; then
                record pwr.pl1 power "PL1 sync invariant" PASS "PL1 35 W on both zones (tdp-sync invariant holds)" "MSR=MMIO=$(watts "$pl1msr") W" ""
            else
                record pwr.pl1 power "PL1 sync invariant" PASS "PL1 $(watts "$pl1msr") W on both zones (sync ok)" "recorded baseline is 35 W; hhd set $(watts "$pl1msr") W" ""
            fi
    else
        record pwr.pl1 power "PL1 sync invariant" FAIL "MSR PL1 $(watts "$pl1msr") W != MMIO PL1 $(watts "$pl1mmio") W" "the lower limit wins, so the MSR value caps the package" "systemctl status oxp3-tdp-sync ; sudo systemctl restart oxp3-tdp-sync ; docs/hw/power-fan.md"
    fi

    local saved live expect
    saved=$(json_get "$st_json" saved_state.profile 2>/dev/null) || saved=""
    live=$(json_get "$st_json" platform_profile 2>/dev/null) || live=""
    if [ -z "$saved" ]; then
        record pwr.profile power "saved profile applied" SKIP "no saved profile" "/etc/oxp3/power.json absent" "sudo /usr/local/libexec/oxp3-powerctl profile balanced --save"
    else
        case $saved in
            quiet) expect=low-power ;;
            balanced) expect=balanced ;;
            performance) expect=performance ;;
            *) expect="" ;;
        esac
        if [ -z "$expect" ]; then
            record pwr.profile power "saved profile applied" UNKNOWN "unknown saved profile '$saved'" "" "known: quiet, balanced, performance"
        elif [ "$live" = "$expect" ]; then
            record pwr.profile power "saved profile applied" PASS "profile $saved (platform_profile=$live)" "PL1 $(watts "$pl1msr") W, PL2 $(watts "$pl2msr") W" ""
        else
            record pwr.profile power "saved profile applied" FAIL "saved profile $saved expects platform_profile=$expect, live=$live" "" "sudo systemctl restart oxp3-power-profile ; /usr/local/libexec/oxp3-powerctl profile $saved --save"
        fi
    fi

    local fkv pe pwm rpm mode
    fkv=$(parse_fan "$st_json")
    pe=$(printf '%s\n' "$fkv" | sed -n 's/^PWM_ENABLE=//p')
    pwm=$(printf '%s\n' "$fkv" | sed -n 's/^PWM=//p')
    rpm=$(printf '%s\n' "$fkv" | sed -n 's/^RPM=//p')
    mode=$(printf '%s\n' "$fkv" | sed -n 's/^MODE=//p')
    if [ -z "$rpm" ] || [ -z "$pe" ]; then
        record pwr.fan power "fan sane" FAIL "fan node unreadable" "pwm_enable='${pe}' rpm='${rpm}'" "journalctl -k -b | grep -i oxp_ec ; docs/hw/power-fan.md"
    elif num_gt 0 "$rpm" && num_gt 12000 "$rpm"; then
        record pwr.fan power "fan sane" FAIL "fan1_input out of range" "${rpm} rpm" "docs/hw/power-fan.md"
    elif ! printf '%s' "$pe" | grep -qE '^(1|2)$'; then
        record pwr.fan power "fan sane" FAIL "pwm1_enable=$pe (expected 2 auto, 1 manual)" "" "echo 2 | sudo tee /sys/class/hwmon/hwmon*/pwm1_enable ; docs/hw/power-fan.md"
    else
        record pwr.fan power "fan sane" PASS "${mode:-?} mode, pwm ${pwm:-?}/255 (enable=$pe), ${rpm} rpm" "acpi_fan fan1_input returns EIO: known cosmetic bug, not the fan (use oxp_ec)" ""
    fi

    local t_load t_active t_file
    st=$(unit_state oxp3-adaptivetdp.service)
    IFS='|' read -r t_load t_active t_file <<<"$st"
    if [ "$t_load" = not-found ]; then
        record pwr.adaptive power "adaptive TDP opt-in" SKIP "not installed (opt-in not used)" "LoadState=not-found" "sudo tools/oxp3-power-install.sh install --adaptive-tdp (installs it inactive)"
    elif [ "$t_active" = active ] && [ "$t_file" = enabled ]; then
        record pwr.adaptive power "adaptive TDP opt-in" PASS "running (enabled at boot)" "unit oxp3-adaptivetdp.service" ""
    elif [ "$t_active" = active ]; then
        record pwr.adaptive power "adaptive TDP opt-in" PASS "running (started manually; not enabled at boot)" "documented opt-in path" ""
    elif [ "$t_file" = enabled ]; then
        record pwr.adaptive power "adaptive TDP opt-in" FAIL "enabled but not running" "ActiveState=$t_active UnitFileState=$t_file" "journalctl -u oxp3-adaptivetdp -b ; sudo systemctl restart oxp3-adaptivetdp"
    else
        record pwr.adaptive power "adaptive TDP opt-in" PASS "inactive as installed (opt-in)" "UnitFileState=$t_file" ""
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): read_ac_online() returns None
    # when there is no AC/Mains node, and Policy.target() then skips BOTH battery
    # clamps. Decide from one real unprivileged --dry-run sample, plus the
    # battery's own status, whether that could allow performance on battery.
    local atd=$REPO/tools/oxp3-adaptivetdp.py
    if [ ! -x "$atd" ] || ! have python3; then
        record pwr.ac power "adaptive AC/battery clamp" SKIP "oxp3-adaptivetdp.py or python3 missing" "$atd" ""
    else
        run_tool adaptive-decide 40 "$atd" --dry-run --once --json --log-file ''
        if [ "$RUN_RC" -ne 0 ]; then
            record pwr.ac power "adaptive AC/battery clamp" SKIP "dry-run decision unavailable (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "python3 tools/oxp3-adaptivetdp.py --dry-run --once --json --log-file '' ; docs/hw/battery-tdp.md"
        else
            local adec acv averdict awhy
            adec=$(parse_adaptive_decision "$RUN_OUT")
            acv=$(printf '%s\nFLOOR=20\nALLOW=false\n' "$adec" | parse_ac_verdict)
            averdict=$(printf '%s\n' "$acv" | sed -n 's/^VERDICT=//p')
            awhy=$(printf '%s\n' "$acv" | sed -n 's/^WHY=//p')
            case $averdict in
                pass) record pwr.ac power "adaptive AC/battery clamp" PASS "$awhy" "$(printf '%s\n' "$adec" | sed -n 's/^POLICY=//p')" "" ;;
                fail) record pwr.ac power "adaptive AC/battery clamp" FAIL "$awhy" "$(printf '%s\n' "$adec" | sed -n 's/^POLICY=//p')" "no ADP1/Mains node: either set on_ac=False when unknown or refuse to raise above balanced; then update docs/UNIT-TESTS.md section 5" ;;
                *) record pwr.ac power "adaptive AC/battery clamp" SKIP "$awhy" "$(printf '%s\n' "$adec" | sed -n 's/^POLICY=//p')" "on this unit there is no AC/Mains node; validate the clamp fix on a unit that has/lacks ADP1 (docs/hw/battery-tdp.md)" ;;
            esac
        fi
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): a mixed raise/lower switch is
    # ordered "all raises, then all lowers" rather than per-level. That path is
    # reachable whenever the live limits sit between two presets (this host:
    # MMIO PL2 52 W > balanced's 45 W), so a read-only dry-run shows the order.
    if ! have python3; then
        record pwr.order power "limit write ordering" SKIP "python3 missing" "" ""
    else
        run_tool profile-dry 40 "$pc" profile balanced --dry-run --json
        if [ "$RUN_RC" -ne 0 ] || ! json_ok "$RUN_OUT"; then
            record pwr.order power "limit write ordering" SKIP "profile --dry-run unavailable (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-powerctl.py profile balanced --dry-run --json"
        else
            local okv oviol omixed owhy
            okv=$(parse_limit_order "$RUN_OUT" "$st_json")
            oviol=$(printf '%s\n' "$okv" | sed -n 's/^VIOLATION=//p')
            omixed=$(printf '%s\n' "$okv" | sed -n 's/^MIXED=//p')
            orec=$(printf '%s\n' "$okv" | sed -n 's/^RECORDED=//p')
            owhy=$(printf '%s\n' "$okv" | sed -n 's/^WHY=//p')
            if [ "${oviol:-1}" = 0 ] && [ "${omixed:-0}" -gt 0 ]; then
                record pwr.order power "limit write ordering" PASS "mixed raise/lower on $omixed domain(s): raises ceiling-first, then lowers floor-first" "$owhy" ""
            elif [ "${oviol:-1}" = 0 ]; then
                record pwr.order power "limit write ordering" PASS "no mixed switch with the live limits; raises are ceiling-first" "$owhy" ""
            elif [ "${orec:-0}" = 0 ]; then
                # The installed tool predates the recorded pre-write state: the
                # mismatch is between the plan and a separately sampled status,
                # so a live writer may be the cause.  Not provable either way.
                record pwr.order power "limit write ordering" SKIP "plan does not record its pre-write limits, so a stale status sample cannot be ruled out" "$owhy" "install the current tools/oxp3-powerctl (sudo tools/oxp3-power-install.sh install) and re-run; no profile needs re-applying"
            else
                record pwr.order power "limit write ordering" FAIL "write order is not dependency-safe" "$owhy" "order by dependency in apply_limits() (open the ceiling before raising, close the floor before lowering); docs/UNIT-TESTS.md section 5"
            fi
        fi
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): curve_pwm()'s t1 == t0 branch
    # is unreachable for sorted points (the first matching pair wins). Prove on
    # hardware that a curve file WITH duplicate points is accepted and still
    # yields a defined pwm -- dry-run, one step, no write.
    if [ ! -x "$pc" ] || ! have python3; then
        record fan.curvedup power "fan curve duplicate points" SKIP "oxp3-powerctl missing" "$pc" ""
    else
        printf '40 60\n60 100\n60 140\n90 255\n' >"$WORK/dup.curve"
        run_tool curve-dup 40 "$pc" fan curve --file "$WORK/dup.curve" --dry-run --once --json
        local ckv cpwm cok
        ckv=$(parse_curve_summary "$RUN_OUT")
        cpwm=$(printf '%s\n' "$ckv" | sed -n 's/^PWM=//p')
        cok=$(printf '%s\n' "$ckv" | sed -n 's/^OK=//p')
        if [ "$RUN_RC" -eq 2 ]; then
            record fan.curvedup power "fan curve duplicate points" SKIP "no fan node or temperature sensor" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-powerctl.py fan status ; docs/hw/power-fan.md"
        elif [ "$RUN_RC" -eq 0 ] && [ "$cok" = true ] && printf '%s' "$cpwm" | grep -qE '^[0-9]+$' && [ "$cpwm" -ge 0 ] && [ "$cpwm" -le 255 ]; then
            record fan.curvedup power "fan curve duplicate points" PASS "curve with a duplicate 60 C point accepted (pwm $cpwm, 1 dry-run step)" "first matching pair wins; the t1 == t0 branch stays unreachable for sorted points" ""
        else
            record fan.curvedup power "fan curve duplicate points" FAIL "duplicate-point curve was not accepted (rc=$RUN_RC, pwm '${cpwm:-?}')" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-powerctl.py fan curve --file $WORK/dup.curve --dry-run --once ; docs/UNIT-TESTS.md section 5"
        fi
    fi

    check_pwr_rapl

    if [ "$FULL" -eq 1 ] && [ "$WITH_CHANGES" -eq 1 ]; then
        check_fan_savemode "$pc"
    fi
}

# ------------------------------------------------------------------ 6. battery
check_battery() {
    local f v t="" bf="" bv=""
    for f in /sys/class/power_supply/BAT*/charge_control_end_threshold; do
        [ -r "$f" ] && { t=$f; v=$(read1 "$f"); break; }
    done
    if [ -n "$t" ]; then
        record bat.threshold battery "charge end threshold reachable" PASS "charge_control_end_threshold=$v" "$t (writes need root)" ""
    else
        record bat.threshold battery "charge end threshold reachable" FAIL "charge_control_end_threshold not exposed" "no readable BAT*/charge_control_end_threshold" "docs/hw/battery-tdp.md (EC/kernel capability, not something userspace can add)"
    fi

    for f in /sys/class/power_supply/BAT*/charge_behaviour; do
        [ -r "$f" ] && { bf=$f; bv=$(read1 "$f"); break; }
    done
    if [ -n "$bf" ]; then
        record bat.behaviour battery "charge behaviour reachable" PASS "charge_behaviour: $bv" "$bf" ""
    else
        record bat.behaviour battery "charge behaviour reachable" FAIL "charge_behaviour not exposed" "" "docs/hw/battery-tdp.md"
    fi

    # Deferred risk (docs/UNIT-TESTS.md section 5): 'battery limit' with no
    # percentage resolves to 100 ("off"), so a script that forgot the argument
    # would clear a saved limit. Prove the contract read-only (--dry-run) and
    # prove the real no-argument form is refused without root.
    local pc
    pc=$(powerctl_cmd)
    if [ -z "$t" ] || [ -z "$pc" ] || ! have python3; then
        record bat.limit battery "battery limit default argument" SKIP "no readable charge limit or no oxp3-powerctl" "$pc" "docs/hw/battery-tdp.md"
        return 0
    fi
    local before after lkv lreq lapp ldry drc=0 rrc=0 refuse="" live_ok=1
    before=$(read1 "$t" || true)
    run_tool battery-limit-dry 30 "$pc" battery limit --dry-run --json
    drc=$RUN_RC
    lkv=$(parse_battery_limit_dry "$RUN_OUT") || lkv=""
    lreq=$(printf '%s\n' "$lkv" | sed -n 's/^REQUESTED=//p')
    lapp=$(printf '%s\n' "$lkv" | sed -n 's/^APPLIED=//p')
    ldry=$(printf '%s\n' "$lkv" | sed -n 's/^DRY=//p')
    if ! is_root; then
        run_tool battery-limit-refused 30 "$pc" battery limit
        rrc=$RUN_RC
        refuse="unprivileged no-argument call exits $rrc"
        [ "$(read1 "$t" || true)" = "$before" ] || { refuse="$refuse, THRESHOLD CHANGED"; live_ok=0; }
    fi
    after=$(read1 "$t" || true)
    if [ "$drc" -ne 0 ]; then
        record bat.limit battery "battery limit default argument" FAIL "dry-run failed (rc=$drc)" "$(tail_flat "$RUN_OUT" 2)" "tools/oxp3-powerctl.py battery limit --dry-run --json"
    elif [ "$ldry" != true ] || [ "$lapp" != "$before" ] || [ "$after" != "$before" ]; then
        record bat.limit battery "battery limit default argument" FAIL "dry-run wrote or reported a different live limit" "requested=${lreq:-?} applied=${lapp:-?} dry=${ldry:-?} live=${before:-?}->$after" "tools/oxp3-powerctl.py battery limit --dry-run --json ; docs/hw/battery-tdp.md"
    elif [ "$live_ok" -ne 1 ] || { [ -n "$refuse" ] && [ "$rrc" -ne 2 ]; }; then
        record bat.limit battery "battery limit default argument" FAIL "no-argument form is not refused without root" "$refuse" "sudo tools/oxp3-powerctl.py battery limit   (this clears the limit to 100 %)"
    elif [ "$lreq" = 100 ]; then
        record bat.limit battery "battery limit default argument" PASS "no argument resolves to 100 % ('off') in a dry run; nothing written${refuse:+; $refuse}" "deferred contract: an explicit 50-100 or 'off' is required to be safe" ""
    else
        record bat.limit battery "battery limit default argument" PASS "no argument reports requested=${lreq:-?} (prints instead of writing)" "${refuse:-not executed: running as root}" ""
    fi
}

# ------------------------------------------------------------------ 7. audio
check_audio() {
    local kv sinks real dummy def dropin
    local on=$REPO/tools/audio/eq-on.sh off=$REPO/tools/audio/eq-off.sh

    if [ -x "$on" ] && [ -x "$off" ]; then
        record aud.scripts audio "EQ on/off scripts" PASS "eq-on.sh and eq-off.sh executable" "" ""
    else
        record aud.scripts audio "EQ on/off scripts" FAIL "EQ scripts missing or not executable" "$on $off" "restore tools/audio/ from git and chmod +x"
    fi

    # aud.mic: is there a capture source with a real signal path?  The ALC245
    # internal microphone is a fixed codec pin (0x12), not a PCH DMIC, so the
    # question is exposure, not NHLT.  mic_diag.py only reads proc/sysfs/UCM.
    local micdiag=$REPO/tools/audio/mic_diag.py
    if [ -x "$micdiag" ] && have python3; then
        run_tool mic-diag 30 "$micdiag" --json
        if json_ok "$RUN_OUT"; then
            local mv ms mh mo
            mv=$(json_get "$RUN_OUT" verdict)
            ms=$(json_get "$RUN_OUT" summary)
            mh=$(json_get "$RUN_OUT" hint)
            mo=$(json_get "$RUN_OUT" ucm.override_installed)
            case ${mv:-} in
                exposed)
                    record aud.mic audio "internal microphone exposed" PASS "${ms:-capture source exposed}" "" "" ;;
                gated)
                    if [ "${mo:-false}" = true ]; then
                        record aud.mic audio "internal microphone exposed" FAIL "UCM override installed but the internal mic is still not exposed" "${ms:-?}" "${mh:-sudo tools/audio/internal-mic.sh undo ; docs/hw/audio-microphone.md}"
                    else
                        record aud.mic audio "internal microphone exposed" SKIP "internal mic pin present; no always-available capture source (opt-in fix not installed)" "${ms:-?}" "${mh:-sudo tools/audio/internal-mic.sh install ; docs/hw/audio-microphone.md}"
                    fi ;;
                pch-dmic)
                    record aud.mic audio "internal microphone exposed" SKIP "this machine uses a PCH DMIC capture PCM, not the codec pin" "${ms:-?}" "docs/hw/audio-microphone.md" ;;
                absent)
                    record aud.mic audio "internal microphone exposed" SKIP "no fixed internal mic pin in the codec pin configuration" "${ms:-?}" "docs/hw/audio-microphone.md" ;;
                *)
                    record aud.mic audio "internal microphone exposed" SKIP "no SOF HDA card / not applicable in this context" "${ms:-?}" "docs/hw/audio-microphone.md" ;;
            esac
        else
            record aud.mic audio "internal microphone exposed" UNKNOWN "mic_diag.py produced no JSON (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/audio/mic_diag.py ; docs/hw/audio-microphone.md"
        fi
    else
        record aud.mic audio "internal microphone exposed" SKIP "mic_diag.py or python3 missing" "$micdiag" ""
    fi

    # aud.level: read-only gain staging + headroom report.  It needs amixer, not
    # a PipeWire session, so it is checked before the pactl early return.
    local lvl=$REPO/tools/audio/level-report.py
    if [ -x "$lvl" ] && have python3; then
        run_tool level-report 30 "$lvl" --json
        if json_ok "$RUN_OUT"; then
            local lv lcontrol lat lprof lnet lvsold lclip lhead lsink
            lv=$(parse_level_report <"$RUN_OUT")
            lcontrol=$(kv "$lv" CONTROL)
            lat=$(kv "$lv" AT_MAX)
            lprof=$(kv "$lv" PROFILE)
            lnet=$(kv "$lv" NET_DB)
            lvsold=$(kv "$lv" NET_VS_OLD_DB)
            lclip=$(kv "$lv" CLIP_MARGIN_DB)
            lhead=$(kv "$lv" HEADROOM_DB)
            lsink=$(kv "$lv" SINK_PCT)
            local lvsold_signed=$lvsold
            case $lvsold in ''|-*) ;; *) lvsold_signed="+$lvsold" ;; esac
            if [ -z "$lcontrol" ]; then
                record aud.level audio "ALSA speaker gain at maximum" SKIP \
                    "no Front/Speaker/Mono playback control on the SOF card" \
                    "amixer -c0 scontrols" \
                    "tools/audio/gain-stage.sh report ; docs/hw/audio-harman-apo.md"
            elif [ "$lat" = yes ]; then
                record aud.level audio "ALSA speaker gain at maximum" PASS \
                    "$lcontrol at max; profile ${lprof:-?}, net ${lnet:-?} dB vs flat (${lvsold_signed:-?} dB vs old); clip margin ${lclip:-?} dB" \
                    "headroom=${lhead:-?} dB, sink=${lsink:-?}%" \
                    "$(case ${lclip:-0} in -*) echo 'loud profile clips above full scale; tools/audio/eq-on.sh --safe keeps -8 dB' ;; *) echo '' ;; esac)"
            else
                record aud.level audio "ALSA speaker gain at maximum" FAIL \
                    "$lcontrol is $(kv "$lv" VALUE)/$(kv "$lv" MAX) ($(kv "$lv" ALSA_DB) dB): 3.75 dB left below maximum" \
                    "net ${lnet:-?} dB vs flat (${lvsold_signed:-?} dB vs old)" \
                    "tools/audio/gain-stage.sh apply ; docs/hw/audio-harman-apo.md"
            fi
        else
            record aud.level audio "ALSA speaker gain at maximum" UNKNOWN \
                "level-report.py produced no JSON (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" \
                "tools/audio/level-report.py --json"
        fi
    else
        record aud.level audio "ALSA speaker gain at maximum" SKIP \
            "level-report.py or python3 missing" "$lvl" "tools/audio/gain-stage.sh report"
    fi

    if ! have pactl; then
        record aud.server audio "PipeWire session" SKIP "pactl missing" "" "install pipewire-utils; docs/hw/audio-harman-apo.md"
        record aud.eq audio "speaker EQ loaded" SKIP "pactl missing" "" "tools/audio/eq-on.sh"
        record aud.dummy audio "no Dummy Output fallback" SKIP "pactl missing" "" "docs/hw/README.md trap"
        record aud.model audio "EQ model table" SKIP "pactl missing" "" "tools/audio/eq_response.py"
        return 0
    fi

    run_tool pactl-info 15 pactl info
    if [ "$RUN_RC" -ne 0 ]; then
        record aud.server audio "PipeWire session" SKIP "no PipeWire/PulseAudio connection" "$(tail_flat "$RUN_OUT" 2)" "run this inside the desktop session; systemctl --user status pipewire wireplumber"
        record aud.eq audio "speaker EQ loaded" SKIP "no audio session" "" "tools/audio/eq-on.sh (inside the session)"
        record aud.dummy audio "no Dummy Output fallback" SKIP "no audio session" "" "docs/hw/README.md trap"
    else
        record aud.server audio "PipeWire session" PASS "audio server reachable" "$(grep -m 1 '^Server Name' "$RUN_OUT")" ""
        run_tool pactl-sinks 15 pactl list short sinks
        kv=$(parse_audio_sinks <"$RUN_OUT")
        sinks=$(printf '%s\n' "$kv" | sed -n 's/^SINKS=//p')
        real=$(printf '%s\n' "$kv" | sed -n 's/^REAL=//p')
        dummy=$(printf '%s\n' "$kv" | sed -n 's/^DUMMY=//p')
        def=$(sed -n 's/^Default Sink: //p' "$WORK/pactl-info.out" | head -n 1)

        dropin=$HOME/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf
        if [ ! -f "$dropin" ]; then
            record aud.eq audio "speaker EQ loaded" SKIP "EQ not installed" "$dropin missing" "tools/audio/eq-on.sh (no root; makes the filter sink the default)"
        elif grep -q 'oxp3_speaker_eq' "$RUN_OUT" 2>/dev/null; then
            record aud.eq audio "speaker EQ loaded" PASS "filter sink oxp3_speaker_eq.sink loaded" "$dropin" ""
        else
            record aud.eq audio "speaker EQ loaded" FAIL "drop-in present but the filter sink is not in PipeWire" "$(pactl list short sinks 2>/dev/null | tr '\n' ' ')" "systemctl --user restart pipewire wireplumber ; journalctl --user -u pipewire -n 50"
        fi

        if [ "${real:-0}" -eq 1 ]; then
            if printf '%s' "$def" | grep -qE 'auto_null|dummy'; then
                record aud.dummy audio "no Dummy Output fallback" FAIL "default sink is $def while a real sink exists" "" "pactl set-default-sink \$(pactl list short sinks | awk '/HiFi__Speaker__sink|oxp3_speaker_eq/ {print \$1; exit}')"
            else
                record aud.dummy audio "no Dummy Output fallback" PASS "default sink: ${def:-?}" "sinks=${sinks:-?}" ""
            fi
        elif [ "${dummy:-0}" -eq 1 ]; then
            record aud.dummy audio "no Dummy Output fallback" UNKNOWN "only auto_null (Dummy Output) visible" "no hardware sink in this context: no audio seat here, or the SOF card is wedged" "pactl list sinks ; journalctl --user -u pipewire ; docs/hw/README.md trap"
        else
            record aud.dummy audio "no Dummy Output fallback" UNKNOWN "no sinks reported" "sinks=${sinks:-0}" "pactl list short sinks"
        fi
    fi

    local model=$REPO/tools/audio/eq_response.py
    if [ -x "$model" ] && have python3; then
        run_tool eq-model 30 "$model"
        if [ "$RUN_RC" -eq 0 ] && grep -q 'max' "$RUN_OUT" 2>/dev/null; then
            record aud.model audio "EQ model table" PASS "8-band model reproduces (default loud profile, pre-gain -4 dB)" "$(grep -m 1 -i 'max' "$RUN_OUT")" ""
        else
            record aud.model audio "EQ model table" FAIL "eq_response.py failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/audio/eq_response.py ; docs/hw/audio-harman-apo.md"
        fi
    else
        record aud.model audio "EQ model table" SKIP "tool missing" "$model" ""
    fi

    if [ "$WITH_CHANGES" -eq 1 ]; then
        if [ ! -x "$on" ]; then
            record aud.activate audio "speaker EQ activation" SKIP "eq-on.sh missing" "" ""
        else
            run_tool eq-on 120 "$on"
            if [ "$RUN_RC" -eq 0 ] && grep -q 'speaker EQ on' "$RUN_OUT" 2>/dev/null; then
                record aud.activate audio "speaker EQ activation" PASS "eq-on.sh activated the filter sink" "$(grep -m 1 'speaker EQ on' "$RUN_OUT")" "persistent change: $HOME/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf (remove with eq-off.sh)"
            else
                record aud.activate audio "speaker EQ activation" FAIL "eq-on.sh failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 3)" "tools/audio/eq-on.sh ; journalctl --user -u pipewire -n 50"
            fi
        fi
    fi
}

# ------------------------------------------------------------------ 8. display
check_display() {
    local gm=/usr/local/libexec/anatase-gamemode-hdr dropin=/etc/systemd/user/anatase-gamemode.service.d/90-oxp3-hdr.conf
    if [ ! -r /usr/libexec/anatase-gamemode ]; then
        record dsp.gamemodehdr display "Gamemode HDR launcher" SKIP "session launcher not found" "/usr/libexec/anatase-gamemode missing" ""
    elif [ -r "$gm" ] && grep -q -- '--hdr-enabled' "$gm" 2>/dev/null && grep -q "^ExecStart=$gm$" "$dropin" 2>/dev/null; then
        record dsp.gamemodehdr display "Gamemode HDR launcher" PASS "HDR/PQ flags active through the session override" "$gm + $dropin" "persistent: /usr/local copy + /etc drop-in, no overlay"
    else
        record dsp.gamemodehdr display "Gamemode HDR launcher" FAIL "Gamemode launcher has no HDR/PQ flags (session output is SDR)" "patched copy: $([ -r "$gm" ] && echo yes || echo no); override: $([ -r "$dropin" ] && echo yes || echo no)" "sudo tools/display/gamemode-hdr.sh install ; docs/hw/hdr-vibrance.md section 5.2"
    fi
    local tool=$REPO/tools/display/display-mode.sh kv out hdr wcg icc depth vrr
    case ${DESKTOP_SESSION:-}:${XDG_CURRENT_DESKTOP:-} in
        *gamescope*|*gamemode*)
            record dsp.hdr display "HDR / WCG state" SKIP \
                "inside Gamemode: desktop HDR is a Plasma/kscreen feature, gamescope HDR is covered by dsp.gamemodehdr" \
                "session=${DESKTOP_SESSION:-?}/${XDG_CURRENT_DESKTOP:-?}" \
                "check HDR in the desktop session, or dsp.gamemodehdr for Gamemode"
            ;;
        *)
    if [ ! -x "$tool" ]; then
        record dsp.hdr display "HDR / WCG state" SKIP "display-mode.sh missing" "$tool" ""
        record dsp.icc display "ICC profile" SKIP "display-mode.sh missing" "$tool" ""
        record dsp.panel display "panel HDR capability" SKIP "display-mode.sh missing" "$tool" ""
    elif ! have kscreen-doctor; then
        record dsp.hdr display "HDR / WCG state" SKIP "kscreen-doctor not available" "no Plasma session in this context" "run inside the Plasma session: tools/oxp3-verify.sh"
        record dsp.icc display "ICC profile" SKIP "kscreen-doctor not available" "" ""
        record dsp.panel display "panel HDR capability" SKIP "kscreen-doctor not available" "" ""
    else
        run_tool display-status 30 "$tool" status
        if [ "$RUN_RC" -ne 0 ]; then
            if grep -q 'missing required tool\|no enabled output found' "$RUN_OUT" 2>/dev/null; then
                record dsp.hdr display "HDR / WCG state" SKIP "no enabled output in this context" "$(tail_flat "$RUN_OUT" 1)" "run inside the Plasma session with the panel connected"
                record dsp.icc display "ICC profile" SKIP "no enabled output" "" ""
                record dsp.panel display "panel HDR capability" SKIP "no enabled output" "" ""
            else
                record dsp.hdr display "HDR / WCG state" FAIL "display-mode.sh status failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/display/display-mode.sh status"
                record dsp.icc display "ICC profile" SKIP "display status failed" "" ""
                record dsp.panel display "panel HDR capability" SKIP "display status failed" "" ""
            fi
        else
            kv=$(parse_display_status <"$RUN_OUT")
            out=$(printf '%s\n' "$kv" | sed -n 's/^OUTPUT=//p')
            hdr=$(printf '%s\n' "$kv" | sed -n 's/^HDR=//p')
            wcg=$(printf '%s\n' "$kv" | sed -n 's/^WCG=//p')
            icc=$(printf '%s\n' "$kv" | sed -n 's/^ICC=//p')
            depth=$(printf '%s\n' "$kv" | sed -n 's/^DEPTH=//p')
            vrr=$(printf '%s\n' "$kv" | sed -n 's/^VRR=//p')
            if [ -z "$out" ] || [ -z "$hdr" ]; then
                # Gamescope's argv can be gamescope-wl / gamescope --...; match the
                # command line, not just the exact comm name.
                # These checks read the Plasma/KWin output state through
                # kscreen-doctor. Without a KWin compositor (Gamemode, tty, a
                # remote context) kscreen-doctor exists but prints nothing, so the
                # honest answer is "not applicable here", not UNKNOWN.
                if ! pgrep -f '[k]win_wayland' >/dev/null 2>&1; then
                    record dsp.hdr display "HDR / WCG state" SKIP \
                        "no Plasma compositor in this context (Gamemode/tty): desktop HDR is a KWin feature; see dsp.gamemodehdr" \
                        "" "check HDR in the desktop session, or dsp.gamemodehdr for Gamemode"
                    record dsp.icc display "ICC profile" SKIP \
                        "no Plasma compositor in this context (Gamemode/tty): desktop ICC is a KWin feature" \
                        "" "check the ICC profile in the desktop session"
                else
                record dsp.hdr display "HDR / WCG state" UNKNOWN "could not parse kscreen-doctor output" "$(tail_flat "$RUN_OUT" 1)" "kscreen-doctor -o"
                record dsp.icc display "ICC profile" UNKNOWN "could not parse kscreen-doctor output" "" "kscreen-doctor -o"
                fi
            else
                record dsp.hdr display "HDR / WCG state" PASS "HDR=$hdr, WCG=$wcg" "output=$out, ${depth}-bit, vrr=$vrr" ""
                if [ -z "$icc" ] || [ "$icc" = none ]; then
                    record dsp.icc display "ICC profile" PASS "no ICC profile applied (panel profile: none)" "tools/display/display-mode.sh icc panel installs ours" ""
                else
                    record dsp.icc display "ICC profile" PASS "ICC profile applied: $icc" "" ""
                fi
            fi
            if grep -q 'PQ/HDR10 claim present' "$RUN_OUT" 2>/dev/null; then
                record dsp.panel display "panel HDR capability" PASS "EDID reports 10-bit PQ/HDR10 wide gamut" "$(grep -m 1 '^VERDICT' "$RUN_OUT" | cut -c1-160)" ""
            else
                record dsp.panel display "panel HDR capability" UNKNOWN "EDID verdict not found" "$(grep -m 1 '^VERDICT' "$RUN_OUT" 2>/dev/null)" "tools/display/oxp3-edid.py"
            fi
        fi
    fi

    local vib=$REPO/tools/display/vibrance.sh
    if [ ! -x "$vib" ]; then
        record dsp.vibrance display "vibrance tooling" SKIP "vibrance.sh missing" "$vib" ""
        return 0
    fi
    run_tool vibrance-status 30 "$vib" status
    if [ "$RUN_RC" -eq 0 ]; then
        local shader gs
        shader=$(sed -n 's/^shader source *: *//p' "$RUN_OUT" | head -n 1)
        gs=$(sed -n 's/^gamescope *: *//p' "$RUN_OUT" | head -n 1)
        record dsp.vibrance display "vibrance tooling" PASS "shader present, gamescope: ${gs:-not installed}" "$shader" ""
    else
        record dsp.vibrance display "vibrance tooling" FAIL "vibrance.sh status failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "tools/display/vibrance.sh status"
    fi

    esac
}

# ------------------------------------------------------------------ 9. graphics
check_graphics() {
    if ! ensure_status_json; then
        local id
        for id in gfx.mesa gfx.xmx.driver gfx.xmx.helper gfx.xmx.igc gfx.xmx.kernels gfx.xmx.icd gfx.xmx.trace gfx.xe gfx.working sched.state sched.scx sched.unit; do
            record "$id" graphics "graphics status unavailable" SKIP "oxp3-status.sh --json unavailable" "" "tools/oxp3-status.sh --json (needs python3)"
        done
        return 0
    fi

    local xess_game=0 game_exe="" game_dir=""
    # XeSS capability is a property of the install, not of the live process: games
    # keep libxess in plugin/ThirdParty directories and only load it when the
    # upscaler is enabled in-game. Check the running game's directory recursively,
    # fall back to the live maps, and treat igdext in the prefix as capability too.
    game_exe=$(status_field game.exe 2>/dev/null || true)
    if [ -n "$game_exe" ] && [ -d "$(dirname "$game_exe")" ]; then
        game_dir=$(dirname "$game_exe")
        find "$game_dir" -maxdepth 12 -iname 'libxess*.dll' -print -quit 2>/dev/null | grep -q . && xess_game=1
    fi
    [ "$xess_game" -eq 0 ] && grep -lsi 'libxess' /proc/[0-9]*/maps 2>/dev/null | head -1 | grep -q . && xess_game=1
    if [ "$xess_game" -eq 0 ]; then
        prefix=$(status_field game.prefix 2>/dev/null || true)
        [ -n "$prefix" ] && [ -d "$prefix/drive_c" ] && \
            find "$prefix/drive_c" -maxdepth 6 -iname 'igdext*.dll' -print -quit 2>/dev/null | grep -q . && xess_game=1
    fi

    simple_ind gfx.mesa mesa.host graphics "host Mesa driver" "ec418f04e589965a" "restore the recorded build: sudo tools/oxp3-mesa-restore.sh restore ; or rebuild: ./oxp3-fixes-fast.sh build install ; docs/WORKING-BUILD-2026-11-08.md"
    simple_ind gfx.xmx.driver xmx.driver graphics "XMX driver" "4b1d55ea" "rebuild the XMX driver: tools/build-xmx-driver.sh ; docs/xess-xmx.md"
    if [ "$xess_game" -eq 0 ]; then
        record gfx.xmx.helper graphics "nodgrf helper" SKIP "the running game has no XeSS files (no libxess in its directory, maps or prefix)" "game: ${game_exe:-not detected}" "XMX only applies when a game loads libxess; docs/xess-xmx.md"
        record gfx.xmx.igc graphics "IGC ocloc present" SKIP "the running game has no XeSS files (no libxess in its directory, maps or prefix)" "game: ${game_exe:-not detected}" "docs/xess-xmx.md"
        record gfx.xmx.icd graphics "per-game XMX env" SKIP "the running game has no XeSS files (no libxess in its directory, maps or prefix)" "game: ${game_exe:-not detected}" "docs/xess-xmx.md"
    else
        simple_ind gfx.xmx.helper xmx.helper graphics "nodgrf helper" "" "launch the game through tools/xmx-run.sh (or the Heroic XMX env) so ANV_CM_HELPER points at tools/xmx-cm-compile-nodgrf.sh ; docs/xess-xmx.md"
        simple_ind gfx.xmx.igc xmx.igc graphics "IGC ocloc present" "" "launch with the XMX stack: tools/xmx-run.sh sets XMX_IGC_DIR ; docs/xess-xmx.md"
        simple_ind gfx.xmx.icd xmx.icd graphics "per-game XMX env" "" "launch with tools/xmx-run.sh or the xmx-enable.sh launch option ; docs/xess-xmx.md"
    fi
    local xstate="" xev=""
    xstate=$(status_field xess.state)
    xev=$(status_field xess.evidence)
    case ${xstate:-} in
        xess-on-xmx)
            record gfx.xmx.runtime graphics "XeSS runtime path" PASS \
                "XMX active: the game mapped libxess and the XMX driver/shim is in the launch" \
                "${xev:-libxess mapped + xmx driver}" "" ;;
        xess-on-standard)
            record gfx.xmx.runtime graphics "XeSS runtime path" FAIL \
                "the game loads XeSS, but the XMX driver/shim is not part of this launch" \
                "${xev:-libxess mapped without the XMX driver}" \
                "launch through tools/xmx-run.sh, or enable the Steam launch option: tools/xmx-steam-enable.sh --appid <id>" ;;
        xess-off)
            record gfx.xmx.runtime graphics "XeSS runtime path" SKIP \
                "the running game does not load XeSS (upscaler off or not supported)" "${xev:-}" "" ;;
        no-game)
            record gfx.xmx.runtime graphics "XeSS runtime path" SKIP "no game running" "${xev:-}" "" ;;
        *)
            record gfx.xmx.runtime graphics "XeSS runtime path" UNKNOWN \
                "xess state missing from oxp3-status --json" "${xstate:-}" "tools/oxp3-status.sh --json" ;;
    esac
    simple_ind gfx.xmx.kernels xmx.kernels graphics "kernel cache populated" "" "compile a kernel: tools/xmx-cm-compile-nodgrf.sh ; docs/xess-xmx.md"

    local ts tv
    ts=$(ind_state xmx.trace) || ts=""
    tv=$(ind_value xmx.trace) || tv=""
    case $ts in
        ok)
            record gfx.xmx.trace graphics "igdext trace" PASS "$tv" "" ""
            ;;
        warn|na)
            record gfx.xmx.trace graphics "igdext trace" SKIP "trace not present (expected unless tracing is enabled)" "$tv" "enable the shim tracing for a traced launch if you need it; docs/xess-xmx.md"
            ;;
        fail)
            record gfx.xmx.trace graphics "igdext trace" FAIL "$tv" "" "docs/xess-xmx.md"
            ;;
        *)
            record gfx.xmx.trace graphics "igdext trace" UNKNOWN "state=${ts:-missing}" "$tv" ""
            ;;
    esac

    simple_ind gfx.xe xe.module graphics "patched xe.ko" "" "rebuild the kmod: ./oxp3-fixes.sh build install (twice after a kernel change)"

    local cw=$REPO/tools/check-working.sh
    if [ -x "$cw" ]; then
        run_tool check-working 120 "$cw"
        if [ "$RUN_RC" -eq 0 ]; then
            record gfx.working graphics "recorded working build" PASS "$(last_line "$RUN_OUT")" "$(grep -c '^OK' "$RUN_OUT" 2>/dev/null) OK line(s)" ""
        else
            record gfx.working graphics "recorded working build" FAIL "state diverges from the recorded working build" "$(grep -m 3 -E '^(MISMATCH|FAIL|BAD)' "$RUN_OUT" 2>/dev/null | tr '\n' ' ')" "tools/check-working.sh ; sudo tools/oxp3-mesa-restore.sh restore ; docs/WORKING-BUILD-2026-11-08.md"
        fi
    else
        record gfx.working graphics "recorded working build" SKIP "tool missing" "$cw" ""
    fi

    local unit_installed=1 us
    us=$(ind_value sched.unit) || us=""
    case $us in
        *not-installed*|*not\ installed*|"") unit_installed=0 ;;
    esac
    if [ "$unit_installed" -eq 0 ]; then
        record sched.state graphics "sched_ext state" SKIP "gaming scheduler not installed (opt-in)" "$us" "sudo systemctl enable --now oxp3-gaming-sched ; docs/cpu-sched.md"
        record sched.scx graphics "scx_cosmos running" SKIP "gaming scheduler not installed (opt-in)" "" "same"
        record sched.unit graphics "gaming scheduler unit" SKIP "not installed (opt-in)" "$us" "install oxp3-gaming-sched.service and enable it"
    else
        local ss sv
        ss=$(ind_state sched.state) || ss=""
        sv=$(ind_value sched.state) || sv=""
        if [ "$ss" = ok ]; then
            record sched.state graphics "sched_ext state" PASS "sched_ext $sv" "" ""
        else
            record sched.state graphics "sched_ext state" FAIL "sched_ext $sv" "the unit is installed but no scheduler is loaded" "systemctl status oxp3-gaming-sched ; journalctl -u oxp3-gaming-sched -b"
        fi
        local cs cv
        cs=$(ind_state sched.scx_cosmos) || cs=""
        cv=$(ind_value sched.scx_cosmos) || cv=""
        if [ "$cs" = ok ]; then
            record sched.scx graphics "scx_cosmos running" PASS "$cv" "" ""
        else
            record sched.scx graphics "scx_cosmos running" FAIL "$cv" "" "systemctl status oxp3-gaming-sched ; docs/cpu-sched.md"
        fi
        if [ "$ss" = ok ] || [ "$cs" = ok ]; then
            record sched.unit graphics "gaming scheduler unit" PASS "$us" "" ""
        else
            record sched.unit graphics "gaming scheduler unit" FAIL "unit installed but inactive" "$us" "systemctl start oxp3-gaming-sched"
        fi
    fi
}

# ------------------------------------------------------------------ 10b. install helpers
# Deferred risk (docs/UNIT-TESTS.md section 5): sys_fw/host_mesa/flatpak_mesa
# hard-code /usr paths, so the unit tests cannot exercise them. Source the
# library the way its callers do and resolve them against the live host.
check_install() {
    local lib=$REPO/tools/lib/install-common.sh kv verdict why fw hmesa fmesa rt
    if [ ! -f "$lib" ] || ! have bash; then
        record inst.paths install "install-common host helpers" SKIP "tools/lib/install-common.sh or bash missing" "$lib" ""
        return 0
    fi
    rt=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files
    [ -d "$rt" ] || rt=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files
    run_tool install-helpers 20 bash -c '
        source "$1" || exit 2
        FLATPAK_RT_FILES=$2
        echo "SYS_FW=$(sys_fw i915/tgl_dmc_ver2_12.bin)"
        echo "HOST_MESA=$(host_mesa)"
        echo "FLATPAK_MESA=$(flatpak_mesa)"
    ' bash "$lib" "$rt"
    if [ "$RUN_RC" -ne 0 ]; then
        record inst.paths install "install-common host helpers" FAIL "sourcing install-common.sh failed (rc=$RUN_RC)" "$(tail_flat "$RUN_OUT" 2)" "bash -c 'source tools/lib/install-common.sh; host_mesa; flatpak_mesa' ; docs/MAINTAINABILITY.md"
        return 0
    fi
    kv=$(parse_inst_paths <"$RUN_OUT")
    verdict=$(printf '%s\n' "$kv" | sed -n 's/^VERDICT=//p')
    why=$(printf '%s\n' "$kv" | sed -n 's/^WHY=//p')
    fw=$(printf '%s\n' "$kv" | sed -n 's/^SYS_FW=//p')
    hmesa=$(printf '%s\n' "$kv" | sed -n 's/^HOST_MESA=//p')
    fmesa=$(printf '%s\n' "$kv" | sed -n 's/^FLATPAK_MESA=//p')
    if [ -n "$fw" ] && [ ! -e "$fw" ]; then
        record inst.paths install "install-common host helpers" FAIL "sys_fw returned a path that does not exist" "$fw" "check /usr/lib/firmware/$fw ; docs/hw/bluetooth.md firmware section"
    elif [ "$verdict" != pass ]; then
        record inst.paths install "install-common host helpers" FAIL "$why" "fw=${fw:-none} host_mesa=${hmesa:-?} flatpak=${fmesa:-?}" "bash -c 'source tools/lib/install-common.sh; host_mesa; flatpak_mesa' ; docs/MAINTAINABILITY.md"
    else
        record inst.paths install "install-common host helpers" PASS "sys_fw/host_mesa/flatpak_mesa resolve against the live host" "fw=${fw:-none} host_mesa=$hmesa flatpak=$fmesa" ""
    fi

    # Undo state (docs/ROLLBACK.md, read-only): every file we modify in place must have a
    # timestamped backup image from before the edit. A known edit with no record means an
    # uninstall cannot put the previous content back; adopt records it retroactively.
    local rbt=$REPO/tools/oxp3-rollback.sh bad hint
    hint="$rbt adopt   # then: $rbt status   (docs/ROLLBACK.md)"
    if [ ! -x "$rbt" ]; then
        record inst.backups install "undo backups for in-place edits" SKIP "tools/oxp3-rollback.sh missing" "$rbt" ""
    else
        run_tool rollback-verify 30 "$rbt" verify
        bad=$(grep -E '^(UNBACKED|MISSING-IMAGE)' "$RUN_OUT" 2>/dev/null | head -4 | tr '\n' ' ' || true)
        if [ "$RUN_RC" -eq 0 ]; then
            record inst.backups install "undo backups for in-place edits" PASS \
                "every known edit has a backup image" "$(tail_flat "$RUN_OUT" 2)" ""
        else
            record inst.backups install "undo backups for in-place edits" FAIL \
                "in-place edit(s) without a backup" "${bad:-$rbt verify exited $RUN_RC}" "$hint"
        fi
    fi
}

# ------------------------------------------------------------------ 11. decky
decky_candidates() {
    if [ -n "${OXP3_DECKY_PLUGINS_DIR:-}" ]; then
        case $OXP3_DECKY_PLUGINS_DIR in
            "~") printf '%s\n' "$HOME" ;;
            "~/"*) printf '%s/%s\n' "$HOME" "${OXP3_DECKY_PLUGINS_DIR#\~/}" ;;
            *) printf '%s\n' "$OXP3_DECKY_PLUGINS_DIR" ;;
        esac
    fi
    printf '%s\n' "$HOME/homebrew/plugins/oxp3-status" "$HOME/homebrew/plugins/OXP3 Status"
}

decky_manifest_fields() { # FILE -> name, author, api_version, version (tab separated)
    python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(1)
if not isinstance(data, dict):
    sys.exit(1)
print("%s\t%s\t%s\t%s" % (data.get("name", ""), data.get("author", ""),
                          data.get("api_version", ""), data.get("version", "")))
PY
}

check_decky() {
    local dir="" d fields name author api version reason="" version_shown="-"
    while IFS= read -r d; do
        [ -f "$d/plugin.json" ] && { dir=$d; break; }
    done < <(decky_candidates)

    if [ -z "$dir" ]; then
        record decky.dir decky "plugin installed" SKIP "not installed" \
            "looked in ~/homebrew/plugins/oxp3-status and 'OXP3 Status' (or OXP3_DECKY_PLUGINS_DIR)" \
            "sudo tools/oxp3-hw-install.sh --decky   # then: sudo systemctl restart plugin_loader.service"
        record decky.backend decky "backend importable" SKIP "not installed" "" \
            "install the plugin, then: python3 -c 'import main' in the plugin dir"
    else
        if have python3; then
            if ! fields=$(decky_manifest_fields "$dir/plugin.json"); then
                reason="plugin.json is not valid JSON"
            else
                IFS=$'\t' read -r name author api version <<<"$fields"
                if [ "$name" != "OXP3 Status" ]; then
                    reason="plugin.json name is '$name' (expected 'OXP3 Status')"
                elif [ "$author" != "silentsudin" ]; then
                    reason="plugin.json author is '$author' (expected 'silentsudin')"
                elif [ "$api" != "1" ]; then
                    reason="plugin.json api_version is '$api' (expected 1)"
                elif [ -z "$version" ]; then
                    reason="plugin.json has no version"
                fi
            fi
        else
            # No python3: validate the stable fields with grep. The version is shown only when a
            # simple x.y.z string is present; decky.backend reports the missing interpreter.
            if ! grep -q '"name"[[:space:]]*:[[:space:]]*"OXP3 Status"' "$dir/plugin.json"; then
                reason="plugin.json does not name 'OXP3 Status' (python3 missing for full validation)"
            elif ! grep -q '"author"[[:space:]]*:[[:space:]]*"silentsudin"' "$dir/plugin.json"; then
                reason="plugin.json author is not 'silentsudin'"
            elif ! grep -q '"api_version"[[:space:]]*:[[:space:]]*1' "$dir/plugin.json"; then
                reason="plugin.json api_version is not 1"
            fi
            version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
                "$dir/plugin.json" | head -n 1)
            [ -n "$version" ] || reason=${reason:-"plugin.json has no version"}
        fi
        if [ -z "$reason" ] && [ ! -f "$dir/main.py" ]; then
            reason="main.py is missing"
        fi
        if [ -z "$reason" ] && [ ! -f "$dir/dist/index.js" ]; then
            reason="dist/index.js is missing"
        fi
        if [ -z "$reason" ]; then
            version_shown=${version:--}
            record decky.dir decky "plugin installed" PASS \
                "installed: version $version_shown" "$dir" ""
        else
            record decky.dir decky "plugin installed" FAIL "invalid install: $reason" \
                "$dir" "sudo tools/oxp3-hw-install.sh --decky --restart-plugin-loader"
        fi

        if ! have python3; then
            record decky.backend decky "backend importable" SKIP "python3 missing" "" ""
        else
            local out=$WORK/decky-import.out rc
            ( cd "$dir" && timeout "$TOOL_TIMEOUT" python3 -c 'import main' ) >"$out" 2>&1
            rc=$?
            if [ "$rc" -eq 0 ]; then
                record decky.backend decky "backend importable" PASS "main.py imports" "$dir/main.py" ""
            else
                record decky.backend decky "backend importable" FAIL "import failed (rc=$rc)" \
                    "$(tail_flat "$out" 2)" "cd '$dir' && python3 -c 'import main'"
            fi
        fi
    fi

    # Did plugin_loader log the plugin in this boot? Root owns the loader, so an unreadable
    # journal is UNKNOWN (with the exact command), never a claim that it did not load.
    if [ -z "$dir" ]; then
        record decky.loaded decky "loader logged the plugin" SKIP "not installed" "" \
            "sudo tools/oxp3-hw-install.sh --decky --restart-plugin-loader"
    elif ! have journalctl; then
        record decky.loaded decky "loader logged the plugin" UNKNOWN "journalctl missing" "" \
            "install systemd; then: journalctl -u plugin_loader.service -b | grep -i oxp3"
    else
        run_tool decky-journal "$TOOL_TIMEOUT" journalctl -u plugin_loader.service -b \
            --no-pager -n 4000
        if [ "$RUN_RC" -ne 0 ]; then
            record decky.loaded decky "loader logged the plugin" UNKNOWN \
                "journal not readable (or plugin_loader.service absent)" \
                "$(tail_flat "$RUN_OUT" 2)" \
                "sudo journalctl -u plugin_loader.service -b --no-pager | grep -i oxp3"
        elif grep -qiE 'oxp3[-_ ]?status' "$RUN_OUT"; then
            record decky.loaded decky "loader logged the plugin" PASS \
                "plugin_loader logged oxp3-status this boot" "$dir" ""
        else
            record decky.loaded decky "loader logged the plugin" SKIP \
                "no load line for oxp3-status in this boot's journal" "" \
                "sudo systemctl restart plugin_loader.service   # then re-run the verifier"
        fi
    fi

    if ensure_status_json; then
        local overall ok warn fail funknown
        overall=$(json_get "$STATUS_JSON" overall 2>/dev/null) || overall=""
        ok=$(json_get "$STATUS_JSON" counts.ok 2>/dev/null) || ok=0
        warn=$(json_get "$STATUS_JSON" counts.warn 2>/dev/null) || warn=0
        fail=$(json_get "$STATUS_JSON" counts.fail 2>/dev/null) || fail=0
        funknown=$(json_get "$STATUS_JSON" counts.unknown 2>/dev/null) || funknown=0
        if [ "$overall" = fail ]; then
            record decky.status decky "status overview" FAIL "overall=fail (ok=$ok warn=$warn fail=$fail)" "" "tools/oxp3-status.sh   # full indicator table"
        elif [ -n "$overall" ]; then
            record decky.status decky "status overview" PASS "overall=$overall (ok=$ok warn=$warn fail=$fail unknown=$funknown)" "warn rows are reported per-indicator by oxp3-status.sh, not asserted here" ""
        else
            record decky.status decky "status overview" UNKNOWN "no overall field" "" "tools/oxp3-status.sh --json"
        fi
    else
        record decky.status decky "status overview" SKIP "status backend unavailable" "" "tools/oxp3-status.sh --json"
    fi
}

# ------------------------------------------------------------------ selftest
ST_PASS=0
ST_FAIL=0
t_eq() { # LABEL EXPECTED ACTUAL
    if [ "$2" = "$3" ]; then
        ST_PASS=$((ST_PASS + 1))
    else
        ST_FAIL=$((ST_FAIL + 1))
        printf 'FAIL %s: expected %q, got %q\n' "$1" "$2" "$3"
    fi
}
t_contains() { # LABEL NEEDLE HAYSTACK
    case $3 in
        *"$2"*) ST_PASS=$((ST_PASS + 1)) ;;
        *)
            ST_FAIL=$((ST_FAIL + 1))
            printf 'FAIL %s: %q not found in %q\n' "$1" "$2" "$3"
            ;;
    esac
}
kv() { printf '%s\n' "$1" | sed -n "s/^$2=//p"; }

declared_ids() {
    {
        sed -n 's/^[[:space:]]*record[[:space:]][[:space:]]*\([a-z][a-z0-9._]*\)[[:space:]].*/\1/p' "$self"
        sed -n 's/^[[:space:]]*simple_ind[[:space:]][[:space:]]*\([a-z][a-z0-9._]*\)[[:space:]].*/\1/p' "$self"
    } | sort
}

run_selftest() {
    local fx=$REPO/tests/verify/fixtures
    if [ ! -d "$fx" ]; then
        echo "$PROG: fixtures not found: $fx" >&2
        return 3
    fi
    local out rc d

    # display-mode.sh output (with ANSI colour codes, as the real tool prints them)
    out=$(parse_display_status <"$fx/display-status.txt")
    t_eq "display/output" eDP-1 "$(kv "$out" OUTPUT)"
    t_eq "display/hdr" disabled "$(kv "$out" HDR)"
    t_eq "display/wcg" disabled "$(kv "$out" WCG)"
    t_eq "display/icc" none "$(kv "$out" ICC)"
    t_eq "display/depth" 10 "$(kv "$out" DEPTH)"
    t_eq "display/vrr" Never "$(kv "$out" VRR)"

    # powerctl status --json
    out=$(parse_power_domains "$fx/powerctl-status.json")
    t_eq "power/domain-count" 2 "$(printf '%s\n' "$out" | grep -c .)"
    t_eq "power/domain-msr" "msr	35000000	37000000	122000000" "$(printf '%s\n' "$out" | sed -n '1p')"
    t_eq "power/domain-mmio-pl1" 35000000 "$(printf '%s\n' "$out" | sed -n '2p' | cut -f2)"
    out=$(parse_fan "$fx/powerctl-status.json")
    t_eq "fan/pwm_enable" 2 "$(kv "$out" PWM_ENABLE)"
    t_eq "fan/rpm" 1396 "$(kv "$out" RPM)"
    t_eq "fan/mode" auto "$(kv "$out" MODE)"
    t_eq "power/platform_profile" balanced "$(json_get "$fx/powerctl-status.json" platform_profile)"
    t_eq "power/saved_profile" balanced "$(json_get "$fx/powerctl-status.json" saved_state.profile)"
    t_eq "power/battery_limit" 80 "$(json_get "$fx/powerctl-status.json" battery.charge_control_end_threshold)"

    # oxp3-status --json indicators
    local ind
    ind=$(status_indicator "$fx/oxp3-status.json" mesa.host)
    t_eq "status/mesa-state" ok "$(printf '%s' "$ind" | cut -f1)"
    t_contains "status/mesa-value" ec418f04e589965a "$(printf '%s' "$ind" | cut -f2)"
    t_eq "status/overall" warn "$(json_get "$fx/oxp3-status.json" overall)"
    t_eq "status/counts-ok" 23 "$(json_get "$fx/oxp3-status.json" counts.ok)"
    ind=$(status_indicator "$fx/oxp3-status.json" sched.unit)
    t_contains "status/sched-unit" "not installed" "$(printf '%s' "$ind" | cut -f2)"

    # suspend_stats: the tuple and what it can support (docs/hw/sleep-power.md 2.6).
    # max_hw_sleep is a probe-time ceiling, not a duration, so "success=0 with a
    # non-zero max" must map to UNKNOWN, never PASS or FAIL.
    out=$(parse_suspend_stats "$fx/suspend-stats/healthy")
    t_eq "suspend/success" 2 "$(kv "$out" SUCCESS)"
    t_eq "suspend/last_hw_sleep" 12345678 "$(kv "$out" LAST_HW_SLEEP)"
    t_eq "suspend/max_hw_sleep" 523986009990 "$(kv "$out" MAX_HW_SLEEP)"
    out=$(parse_suspend_stats "$fx/suspend-stats/broken")
    t_eq "suspend/broken-last_hw_sleep" 0 "$(kv "$out" LAST_HW_SLEEP)"
    t_eq "suspend/broken-fail" 1 "$(kv "$out" FAIL)"
    out=$(parse_suspend_stats "$fx/suspend-stats/ceiling-max")
    t_eq "suspend/ceiling-max-success" 0 "$(kv "$out" SUCCESS)"
    t_eq "suspend/ceiling-max-max" 523986009990 "$(kv "$out" MAX_HW_SLEEP)"
    out=$(parse_suspend_stats "$fx/suspend-stats/last-without-success")
    t_eq "suspend/last-without-success-last" 120000000 "$(kv "$out" LAST_HW_SLEEP)"
    t_eq "suspend/last-without-success-total" 120000000 "$(kv "$out" TOTAL_HW_SLEEP)"
    out=$(parse_suspend_stats "$fx/suspend-stats/no-hw")
    t_eq "suspend/no-hw-success" 1 "$(kv "$out" SUCCESS)"
    t_eq "suspend/no-hw-last-empty" "" "$(kv "$out" LAST_HW_SLEEP)"
    t_eq "suspend/verdict-pass" pass "$(suspend_stats_verdict 2 0 12345678 523986009990)"
    t_eq "suspend/verdict-pass-nohw" pass-nohw "$(suspend_stats_verdict 1 0 "" "")"
    t_eq "suspend/verdict-fail" fail "$(suspend_stats_verdict 1 1 0 523986009990)"
    t_eq "suspend/verdict-hwzero" unknown-hwzero "$(suspend_stats_verdict 1 0 0 523986009990)"
    t_eq "suspend/verdict-last-without-success" unknown-last \
        "$(suspend_stats_verdict 0 0 120000000 523986009990)"
    t_eq "suspend/verdict-ceiling-max" unknown-ceiling \
        "$(suspend_stats_verdict 0 0 0 523986009990)"
    t_eq "suspend/verdict-skip" skip "$(suspend_stats_verdict 0 0 0 0)"
    t_eq "suspend/verdict-unreadable" unreadable "$(suspend_stats_verdict "" "" "" "")"

    # sleep.trigger parsing: logind cat-config (compiled defaults vs our drop-in),
    # the polkit rule (shipped one must keep its guards and must not grant the
    # inhibit-bypass action), pkcheck's exit-status vocabulary and the inhibitor list
    out=$(parse_logind_handles <"$fx/logind-cat-config-default.txt")
    t_eq "logind/default-powerkey" poweroff "$(kv "$out" POWER)"
    t_eq "logind/default-suspendkey" suspend "$(kv "$out" SUSPEND)"
    t_eq "logind/default-overrides" 0 "$(kv "$out" OVERRIDES)"
    out=$(parse_logind_handles <"$fx/logind-cat-config-oxp3.txt")
    t_eq "logind/oxp3-powerkey" suspend "$(kv "$out" POWER)"
    t_eq "logind/oxp3-suspendkey" suspend "$(kv "$out" SUSPEND)"
    t_eq "logind/oxp3-lid" suspend "$(kv "$out" LID)"
    t_eq "logind/oxp3-power-long" poweroff "$(kv "$out" POWER_LONG)"
    t_eq "logind/oxp3-overrides" 6 "$(kv "$out" OVERRIDES)"
    out=$(parse_inhibitors <"$fx/inhibitors-plasma.txt")
    t_eq "inhibit/plasma-total" 9 "$(kv "$out" TOTAL)"
    t_eq "inhibit/plasma-block-sleep" 0 "$(kv "$out" BLOCK_SLEEP)"
    t_eq "inhibit/plasma-block-handles" 1 "$(kv "$out" BLOCK_HANDLES)"
    t_eq "inhibit/plasma-delay-sleep" 6 "$(kv "$out" DELAY_SLEEP)"
    t_eq "inhibit/plasma-handle-who" " PowerDevil" "$(kv "$out" HANDLE_WHO)"
    out=$(parse_inhibitors <"$fx/inhibitors-sleep-block.txt")
    t_eq "inhibit/sleep-block" 1 "$(kv "$out" BLOCK_SLEEP)"
    t_eq "inhibit/sleep-block-who" " faked-update" "$(kv "$out" SLEEP_WHO)"
    out=$(parse_polkit_rule <"$REPO/files/49-oxp3-suspend.rules")
    t_eq "polkit/rule" 1 "$(kv "$out" RULE)"
    t_eq "polkit/actions" 4 "$(kv "$out" IDS)"
    t_eq "polkit/no-inhibit-bypass" 0 "$(kv "$out" IGNORE_INHIBIT)"
    t_eq "polkit/local-guard" 1 "$(kv "$out" LOCAL_GUARD)"
    t_eq "polkit/active-guard" 1 "$(kv "$out" ACTIVE_GUARD)"
    out=$(parse_polkit_rule <"$fx/polkit-rule-bad.rules")
    t_eq "polkit/bad-inhibit-bypass" 1 "$(kv "$out" IGNORE_INHIBIT)"
    t_eq "polkit/bad-no-local-guard" 0 "$(kv "$out" LOCAL_GUARD)"
    t_eq "pkcheck/rc-0" yes "$(pkcheck_rc_name 0)"
    t_eq "pkcheck/rc-1" no "$(pkcheck_rc_name 1)"
    t_eq "pkcheck/rc-2" challenge "$(pkcheck_rc_name 2)"
    t_eq "pkcheck/rc-other" error "$(pkcheck_rc_name 7)"

    # sleep.trigger decision matrix (pure function, no host state involved)
    t_eq "trigger/none" skip-policy "$(trigger_verdict 0 0 poweroff suspend 0 yes)"
    t_eq "trigger/ok" pass "$(trigger_verdict 1 1 suspend suspend 0 yes)"
    t_eq "trigger/lid-only-differs" pass "$(trigger_verdict 1 1 suspend suspend 0 yes)"
    t_eq "trigger/powerkey-not-suspend" fail-keys "$(trigger_verdict 1 1 poweroff suspend 0 yes)"
    t_eq "trigger/suspendkey-not-suspend" fail-keys "$(trigger_verdict 1 1 suspend hibernate 0 yes)"
    t_eq "trigger/no-polkit-rule" fail-incomplete "$(trigger_verdict 1 0 suspend suspend 0 yes)"
    t_eq "trigger/no-logind-dropin" fail-incomplete "$(trigger_verdict 0 1 suspend suspend 0 yes)"
    t_eq "trigger/sleep-blocked" fail-blocked "$(trigger_verdict 1 1 suspend suspend 1 yes)"
    t_eq "trigger/needs-auth" fail-auth "$(trigger_verdict 1 1 suspend suspend 0 challenge)"
    t_eq "trigger/unreadable" skip-unreadable "$(trigger_verdict 1 1 "" "" 0 yes)"

    # oxp3-bt-firmware.sh --check output
    out=$(parse_bt_check <"$fx/bt-check.txt")
    t_eq "bt/healthy-files" 2 "$(kv "$out" FILES_OK)"
    t_eq "bt/healthy-journal" 0 "$(kv "$out" JOURNAL_FAIL)"
    t_eq "bt/healthy-verdict" healthy "$(kv "$out" VERDICT)"
    out=$(parse_bt_check <"$fx/bt-check-bad.txt")
    t_eq "bt/bad-files" 0 "$(kv "$out" FILES_OK)"
    t_eq "bt/bad-journal" 1 "$(kv "$out" JOURNAL_FAIL)"
    t_eq "bt/bad-verdict" action "$(kv "$out" VERDICT)"

    # oxp3-led.py status
    out=$(parse_led_status <"$fx/led-status.txt")
    t_eq "led/node" /dev/hidraw4 "$(kv "$out" NODE)"
    t_eq "led/perm" "0o600 uid=0 gid=0" "$(kv "$out" PERM)"
    t_eq "led/zones" 1 "$(kv "$out" ZONES)"
    t_eq "led/presets" 1 "$(kv "$out" PRESETS)"

    # oxp3-rgbd.py --check
    out=$(parse_rgbd_check <"$fx/rgbd-check.txt")
    t_eq "rgbd/frames" 10 "$(kv "$out" FRAMES)"
    t_eq "rgbd/zones" 5 "$(kv "$out" ZONES)"

    # pactl sinks
    out=$(parse_audio_sinks <"$fx/audio-sinks-real.txt")
    t_eq "audio/real-sinks" 3 "$(kv "$out" SINKS)"
    t_eq "audio/real-flag" 1 "$(kv "$out" REAL)"
    t_eq "audio/real-dummy" 0 "$(kv "$out" DUMMY)"
    out=$(parse_audio_sinks <"$fx/audio-sinks-dummy.txt")
    t_eq "audio/dummy-flag" 1 "$(kv "$out" DUMMY)"
    t_eq "audio/dummy-real" 0 "$(kv "$out" REAL)"

    # journals
    out=$(journal_fixture "$fx/vol-journal.txt")
    t_eq "vol/grab" /dev/input/event12 "$out"
    out=$(parse_voltopology <"$fx/vol-topology.txt")
    t_eq "voltopo/protected-topo" none "$(kv "$out" TOPO)"
    t_eq "voltopo/protected-sel" "" "$(kv "$out" SEL)"
    t_eq "voltopo/protected-at-held" 6752 "$(kv "$out" ATKBD_HELD)"
    t_eq "voltopo/protected-running" 1 "$(kv "$out" HHD_RUNNING)"
    t_eq "voltopo/protected-holders" 1 "$(kv "$out" HOLDERS_KNOWN)"
    t_eq "voltopo/protected-exit" 4 "$(kv "$out" PREDICTED_EXIT)"
    t_eq "voltopo/protected-protected" 1 "$(kv "$out" HHD_PROTECTED)"
    t_eq "voltopo/protected-observe" 1 "$(kv "$out" OBSERVE)"
    t_eq "voltopo/protected-allow" 0 "$(kv "$out" ALLOW_HHD_GRAB)"
    out=$(parse_voltopology <"$fx/vol-topology-hhd-allowed.txt")
    t_eq "voltopo/allowed-topo" hhd-virtual "$(kv "$out" TOPO)"
    t_eq "voltopo/allowed-role" hhd-virtual "$(kv "$out" SEL_ROLE)"
    t_eq "voltopo/allowed-allow" 1 "$(kv "$out" ALLOW_HHD_GRAB)"
    t_eq "voltopo/allowed-protected" 0 "$(kv "$out" HHD_PROTECTED)"
    t_eq "voltopo/allowed-exit" 0 "$(kv "$out" PREDICTED_EXIT)"
    out=$(parse_voltopology <"$fx/vol-topology-physical.txt")
    t_eq "voltopo/phys-topo" physical "$(kv "$out" TOPO)"
    t_eq "voltopo/phys-role" at-kbd "$(kv "$out" SEL_ROLE)"
    t_eq "voltopo/phys-hhd" 0 "$(kv "$out" HHD_RUNNING)"
    t_eq "voltopo/phys-at-free" "" "$(kv "$out" ATKBD_HELD)"
    t_eq "voltopo/phys-protected" 1 "$(kv "$out" HHD_PROTECTED)"
    # hhd journal: the outage signature must be detected (and not invented).
    out=$(parse_hhd_journal <"$fx/hhd-journal-busy.txt")
    t_eq "hhd/busy-count" 2 "$(kv "$out" BUSY)"
    t_contains "hhd/busy-last" "Assuming controllers disconnected" "$(kv "$out" LAST)"
    out=$(parse_hhd_journal <"$fx/hhd-journal-ok.txt")
    t_eq "hhd/ok-count" 0 "$(kv "$out" BUSY)"
    t_eq "hhd/ok-recovered" 0 "$(kv "$out" RECOVERED)"
    out=$(parse_hhd_journal <"$fx/hhd-journal-busy.txt")
    t_eq "hhd/busy-recovered" 0 "$(kv "$out" RECOVERED)"
    out=$(parse_hhd_journal <"$fx/hhd-journal-recovered.txt")
    t_eq "hhd/recovered-count" 2 "$(kv "$out" BUSY)"
    t_eq "hhd/recovered-flag" 1 "$(kv "$out" RECOVERED)"
    out=$(parse_homebtn_journal <"$fx/homebtn-journal.txt")
    t_eq "homebtn/listen" 1 "$(kv "$out" LISTEN)"
    t_eq "homebtn/press" 2 "$(kv "$out" PRESS)"

    # daemon lifecycle (docs/DAEMONS.md): journal recovery + live-fd report
    out=$(parse_lifecycle_journal <"$fx/lifecycle-journal-recovered.txt")
    t_eq "life/recovered-open" 2 "$(kv "$out" OPEN)"
    t_eq "life/recovered-lost" 1 "$(kv "$out" LOST)"
    t_eq "life/recovered-count" 1 "$(kv "$out" RECOVERED)"
    out=$(parse_lifecycle_journal <"$fx/lifecycle-journal-stuck.txt")
    t_eq "life/stuck-lost" 2 "$(kv "$out" LOST)"
    t_eq "life/stuck-recovered" 0 "$(kv "$out" RECOVERED)"
    out=$(parse_fd_report <"$fx/fd-report-good.txt")
    t_eq "life/fd-good" pass "$(kv "$out" VERDICT)"
    t_eq "life/fd-good-checked" 2 "$(kv "$out" CHECKED)"
    out=$(parse_fd_report <"$fx/fd-report-bad.txt")
    t_eq "life/fd-bad" fail "$(kv "$out" VERDICT)"
    t_contains "life/fd-bad-missing" "oxp3-homebtn.service" "$(kv "$out" MISSING)"

    # sleep audit
    out=$(parse_sleep_audit <"$fx/sleep-audit.txt")
    t_contains "sleep/audit-mem" "[s2idle]" "$(kv "$out" MEM)"
    t_eq "sleep/audit-record" 1 "$(kv "$out" RECORD)"

    # deferred-risk probes (docs/UNIT-TESTS.md section 5)
    out=$(hid_report "$fx/hidraw")
    t_eq "hid/candidates-total" 1 "$(kv "$out" TOTAL)"
    t_eq "hid/candidates-vendor" 1 "$(kv "$out" VENDOR)"
    t_eq "hid/candidates-vendor-node" /dev/hidraw0 "$(kv "$out" VENDOR_NODES)"
    out=$({ printf 'SELECTED=/dev/hidraw0\nSEL_VENDOR=1\n'; hid_report "$fx/hidraw"; } | parse_hid_verdict)
    t_eq "hid/composed-pass" pass "$(kv "$out" VERDICT)"
    out=$(parse_hid_verdict <"$fx/hid-verdict-pass.txt")
    t_eq "hid/verdict-pass" pass "$(kv "$out" VERDICT)"
    out=$(parse_hid_verdict <"$fx/hid-verdict-wrong.txt")
    t_eq "hid/verdict-wrong" fail "$(kv "$out" VERDICT)"
    t_contains "hid/wrong-why" "does not advertise usage page 0xFF00" "$(kv "$out" WHY)"
    out=$(parse_hid_verdict <"$fx/hid-verdict-ambiguous.txt")
    t_eq "hid/verdict-ambiguous" pass "$(kv "$out" VERDICT)"
    t_contains "hid/ambiguous-why" "ambiguous" "$(kv "$out" WHY)"
    out=$(parse_hid_verdict <"$fx/hid-verdict-none.txt")
    t_eq "hid/verdict-none" skip "$(kv "$out" VERDICT)"
    out=$(parse_hid_verdict <"$fx/hid-verdict-noselect.txt")
    t_eq "hid/verdict-noselect" fail "$(kv "$out" VERDICT)"

    out=$(parse_dry_frames <"$fx/led-dry-frames.txt")
    t_eq "led/sides-frames" 2 "$(kv "$out" FRAMES)"
    out=$(parse_dry_frames <"$fx/led-dry-empty.txt")
    t_eq "led/sides-empty" 0 "$(kv "$out" FRAMES)"

    out=$(parse_adaptive_decision "$fx/adaptive-ac.json")
    t_eq "ac/on-ac" true "$(kv "$out" ON_AC)"
    t_eq "ac/want" quiet "$(kv "$out" WANT)"
    out=$(parse_adaptive_decision "$fx/adaptive-noac-perf.json")
    t_eq "ac/on-ac-unknown" "" "$(kv "$out" ON_AC)"
    t_eq "ac/noac-status" Discharging "$(kv "$out" BAT_STATUS)"
    av=$(parse_adaptive_decision "$fx/adaptive-ac.json")
    out=$(printf '%s\nFLOOR=20\nALLOW=false\n' "$av" | parse_ac_verdict)
    t_eq "ac/ac-pass" pass "$(kv "$out" VERDICT)"
    av=$(parse_adaptive_decision "$fx/adaptive-ac-perf.json")
    out=$(printf '%s\nFLOOR=20\nALLOW=false\n' "$av" | parse_ac_verdict)
    t_eq "ac/ac-perf-fail" fail "$(kv "$out" VERDICT)"
    av=$(parse_adaptive_decision "$fx/adaptive-noac-perf.json")
    out=$(printf '%s\nFLOOR=20\nALLOW=false\n' "$av" | parse_ac_verdict)
    t_eq "ac/noac-perf-fail" fail "$(kv "$out" VERDICT)"
    av=$(parse_adaptive_decision "$fx/adaptive-noac-quiet.json")
    out=$(printf '%s\nFLOOR=20\nALLOW=false\n' "$av" | parse_ac_verdict)
    t_eq "ac/noac-quiet-skip" skip "$(kv "$out" VERDICT)"
    av=$(parse_adaptive_decision "$fx/adaptive-noac-floor.json")
    out=$(printf '%s\nFLOOR=20\nALLOW=false\n' "$av" | parse_ac_verdict)
    t_eq "ac/noac-floor-fail" fail "$(kv "$out" VERDICT)"

    out=$(parse_limit_order "$fx/powerctl-profile-dryrun.json" "$fx/powerctl-status.json")
    t_eq "order/domains" 2 "$(kv "$out" DOMAINS)"
    t_eq "order/mixed" 2 "$(kv "$out" MIXED)"
    t_eq "order/violation" 0 "$(kv "$out" VIOLATION)"
    t_eq "order/recorded" 1 "$(kv "$out" RECORDED)"
    out=$(parse_limit_order "$fx/powerctl-profile-bad-order.json" "$fx/powerctl-status.json")
    t_eq "order/bad-violation" 1 "$(kv "$out" VIOLATION)"
    t_eq "order/bad-recorded" 1 "$(kv "$out" RECORDED)"
    t_contains "order/bad-why" "precedes" "$(kv "$out" WHY)"
    # A live daemon rewrote MMIO PL2 after `status` but before the dry-run: the
    # plan's recorded before_uw keeps the real decision (lower), so a stale
    # snapshot must not turn a safe plan into a failure.
    out=$(parse_limit_order "$fx/powerctl-profile-dryrun.json" "$fx/powerctl-status-stale.json")
    t_eq "order/recorded-state" 0 "$(kv "$out" VIOLATION)"
    t_eq "order/recorded-state-recorded" 1 "$(kv "$out" RECORDED)"
    out=$(parse_limit_order "$fx/powerctl-profile-legacy.json" "$fx/powerctl-status-stale.json")
    t_eq "order/legacy-stale" 1 "$(kv "$out" VIOLATION)"
    t_eq "order/legacy-stale-recorded" 0 "$(kv "$out" RECORDED)"
    t_contains "order/legacy-stale-why" "precedes" "$(kv "$out" WHY)"

    out=$(parse_curve_summary "$fx/curve-dup.json")
    t_eq "curve/pwm" 178 "$(kv "$out" PWM)"
    t_eq "curve/ok" true "$(kv "$out" OK)"
    t_eq "curve/dry" true "$(kv "$out" DRY)"

    out=$(parse_battery_limit_dry "$fx/battery-limit-dry.json")
    t_eq "batlimit/requested" 100 "$(kv "$out" REQUESTED)"
    t_eq "batlimit/applied" 80 "$(kv "$out" APPLIED)"
    t_eq "batlimit/dry" true "$(kv "$out" DRY)"

    out=$(parse_inst_paths <"$fx/inst-paths.txt")
    t_eq "inst/verdict" pass "$(kv "$out" VERDICT)"
    t_eq "inst/host-mesa" 26.2.4 "$(kv "$out" HOST_MESA)"
    out=$(parse_inst_paths <"$fx/inst-paths-bad.txt")
    t_eq "inst/bad-verdict" fail "$(kv "$out" VERDICT)"

    out=$(parse_iio_devices "$fx/iio")
    t_eq "iio/primary" iio:device0 "$(kv "$out" PRIMARY)"
    t_eq "iio/primary-count" 1 "$(kv "$out" PRIMARY_COUNT)"
    t_eq "iio/fallback-count" 1 "$(kv "$out" FALLBACK_COUNT)"

    out=$(parse_rgbd_check <"$fx/rgbd-check.txt")
    t_eq "rgbd/timing-device" /dev/hidraw4 "$(kv "$out" DEVICE)"
    t_eq "rgbd/timing-journal" readable "$(kv "$out" JOURNAL)"
    out=$(parse_rgbd_check <"$fx/rgbd-check-nojournal.txt")
    t_eq "rgbd/timing-nojournal" fallback "$(kv "$out" JOURNAL)"
    t_eq "rgbd/timing-nojournal-device" /dev/hidraw4 "$(kv "$out" DEVICE)"

    # preflight verdict parser
    out=$(parse_preflight_summary <"$fx/preflight-ok.json")
    t_eq "preflight/safe" true "$(kv "$out" SAFE)"
    t_eq "preflight/verdict" safe "$(kv "$out" VERDICT)"
    t_eq "preflight/fail" 0 "$(kv "$out" FAIL)"
    t_eq "preflight/skipped" - "$(kv "$out" SKIPPED)"
    out=$(parse_preflight_summary <"$fx/preflight-unsafe.json")
    t_eq "preflight/unsafe" false "$(kv "$out" SAFE)"
    t_eq "preflight/blockers" 2 "$(kv "$out" FAIL)"
    t_eq "preflight/would-skip" "volume, sleep" "$(kv "$out" SKIPPED)"

    # level report (aud.level): gain staging, headroom and the net-vs-old figure
    out=$(parse_level_report <"$fx/level-report.json")
    t_eq "level/control" Speaker "$(kv "$out" CONTROL)"
    t_eq "level/value" 87 "$(kv "$out" VALUE)"
    t_eq "level/max" 87 "$(kv "$out" MAX)"
    t_eq "level/alsa-db" 0.00 "$(kv "$out" ALSA_DB)"
    t_eq "level/at-max" yes "$(kv "$out" AT_MAX)"
    t_eq "level/profile" loud "$(kv "$out" PROFILE)"
    t_eq "level/headroom" -4.0 "$(kv "$out" HEADROOM_DB)"
    t_eq "level/net" -4.00 "$(kv "$out" NET_DB)"
    t_eq "level/net-vs-old" 7.75 "$(kv "$out" NET_VS_OLD_DB)"
    t_eq "level/clip-margin" -3.74 "$(kv "$out" CLIP_MARGIN_DB)"
    t_eq "level/sink" 100 "$(kv "$out" SINK_PCT)"
    t_eq "level/muted" no "$(kv "$out" SINK_MUTED)"
    out=$(parse_level_report <"$fx/level-report-low.json")
    t_eq "level/low-control" Front "$(kv "$out" CONTROL)"
    t_eq "level/low-value" 82 "$(kv "$out" VALUE)"
    t_eq "level/low-alsa-db" -3.75 "$(kv "$out" ALSA_DB)"
    t_eq "level/low-at-max" no "$(kv "$out" AT_MAX)"
    t_eq "level/low-profile" safe "$(kv "$out" PROFILE)"
    t_eq "level/low-headroom" -8.0 "$(kv "$out" HEADROOM_DB)"
    t_eq "level/low-net" -7.75 "$(kv "$out" NET_DB)"
    t_eq "level/low-net-vs-old" 4.00 "$(kv "$out" NET_VS_OLD_DB)"
    t_eq "level/low-clip-margin" 0.26 "$(kv "$out" CLIP_MARGIN_DB)"

    # exit-code mapping
    t_eq "exit/fail" 1 "$(exit_code 10 1 2 0)"
    t_eq "exit/skips-only" 2 "$(exit_code 0 0 3 1)"
    t_eq "exit/pass-mixed" 0 "$(exit_code 4 0 2 1)"
    t_eq "exit/all-pass" 0 "$(exit_code 6 0 0 0)"

    # sleep.aspm: the selected ASPM policy, from the kernel's bracketed format
    t_eq "aspm/selected" "powersave" \
        "$(printf 'default performance [powersave] powersupersave\n' | aspm_selected)"
    t_eq "aspm/sole" "default" "$(printf '[default]\n' | aspm_selected)"
    t_eq "aspm/unbracketed" "powersave" "$(printf 'powersave\n' | aspm_selected)"
    t_eq "aspm/empty" "" "$(printf '' | aspm_selected)"

    # the declared check inventory must stay exactly what docs/VALIDATION.md documents
    local ids count
    ids=$(declared_ids | uniq)
    count=$(printf '%s\n' "$ids" | grep -c .)
    if [ "$count" -ge 65 ]; then ST_PASS=$((ST_PASS + 1)); else ST_FAIL=$((ST_FAIL + 1)); printf 'FAIL inventory: only %s distinct declared checks\n' "$count"; fi
    local expected
    expected=$(printf '%s\n' \
        aud.activate aud.dummy aud.eq aud.level aud.mic aud.model aud.scripts aud.server \
        bat.behaviour bat.limit bat.threshold \
        bt.controller bt.diag bt.dracut bt.files bt.journal \
        decky.backend decky.dir decky.loaded decky.status \
        dsp.hdr dsp.gamemodehdr dsp.icc dsp.panel dsp.vibrance \
        fan.curvedup fan.savemode \
        gfx.mesa gfx.gamescope gfx.working gfx.xe gfx.xmx.driver gfx.xmx.helper gfx.xmx.igc \
        gfx.xmx.icd gfx.xmx.kernels gfx.xmx.runtime gfx.xmx.trace \
        hhd.busy hhd.grab hhd.unit \
        hid.access hid.node hid.rule \
        homebtn.mapping homebtn.press homebtn.unit \
        imu.iio imu.selftest \
        inst.backups inst.paths \
        led.sides led.status led.write led.zones \
        life.fd life.reconnect life.sim \
        preflight.verdict \
        pwr.ac pwr.adaptive pwr.fan pwr.order pwr.pl1 pwr.profile pwr.rapl pwr.zones \
        rgbd.check rgbd.timing rgbd.unit \
        sched.scx sched.state sched.unit \
        sleep.aspm sleep.audit sleep.measure sleep.mem sleep.mmcwake sleep.nvme sleep.nvmeacpi sleep.sdcard sleep.stats sleep.suspend sleep.trigger sleep.units sleep.wakeup \
        vol.grab vol.hold vol.hwdb vol.holder vol.topology vol.unit \
        | sort | uniq)
    t_eq "inventory/declared" "$expected" "$ids"

    echo "selftest: $ST_PASS checks passed, $ST_FAIL failed"
    [ "$ST_FAIL" -eq 0 ] || return 1
    return 0
}

journal_fixture() { parse_vol_journal <"$1"; }

# ------------------------------------------------------------------ rendering
render_text() {
    local i st
    printf '%s %s  mode=%s  kernel=%s  root=%s\n' \
        "$PROG" "$VERSION" "$MODE_NAME" "$(uname -r 2>/dev/null || echo '?')" \
        "$(is_root && echo yes || echo no)"
    printf '\n'
    for i in "${!IDS[@]}"; do
        st=${STATES[$i]}
        printf '[%-5s] %-10s %-22s %s\n' "$st" "${GRP[$i]}" "${IDS[$i]}" "${SUMMARIES[$i]}"
        if [ "$st" != PASS ]; then
            [ -n "${DETAILS[$i]}" ] && printf '        %s\n' "${DETAILS[$i]}"
            [ -n "${HINTS[$i]}" ] && printf '        hint: %s\n' "${HINTS[$i]}"
        fi
    done
    printf '\n== summary: %d checks: %d pass, %d fail, %d skip, %d unknown -> exit %s\n' \
        "$(total_checks)" "$PASS_N" "$FAIL_N" "$SKIP_N" "$UNKNOWN_N" "$CODE"
    if [ "$FAIL_N" -gt 0 ]; then
        printf '   failing: %s\n' "$(failing_ids)"
    fi
}

failing_ids() {
    local i out=""
    for i in "${!IDS[@]}"; do
        [ "${STATES[$i]}" = FAIL ] && out="$out ${IDS[$i]}"
    done
    printf '%s' "${out# }"
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

render_json() {
    local i st sep="" rootj withj
    if is_root; then rootj=true; else rootj=false; fi
    if [ "$WITH_CHANGES" -eq 1 ]; then withj=true; else withj=false; fi
    printf '{\n'
    printf '  "schema": 1,\n'
    printf '  "tool": "%s",\n' "$PROG"
    printf '  "version": "%s",\n' "$VERSION"
    printf '  "generated_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')"
    printf '  "mode": "%s",\n' "$MODE_NAME"
    printf '  "root": %s,\n' "$rootj"
    printf '  "with_changes": %s,\n' "$withj"
    printf '  "kernel": "%s",\n' "$(json_escape "$(uname -r 2>/dev/null || echo '?')")"
    printf '  "counts": {"total": %d, "pass": %d, "fail": %d, "skip": %d, "unknown": %d},\n' \
        "$(total_checks)" "$PASS_N" "$FAIL_N" "$SKIP_N" "$UNKNOWN_N"
    printf '  "exit_code": %s,\n' "$CODE"
    printf '  "checks": [\n'
    for i in "${!IDS[@]}"; do
        st=${STATES[$i],,}
        printf '%s    {"id": "%s", "group": "%s", "title": "%s", "state": "%s", "summary": "%s", "detail": "%s", "remediation": "%s"}' \
            "$sep" "$(json_escape "${IDS[$i]}")" "$(json_escape "${GRP[$i]}")" \
            "$(json_escape "${TITLES[$i]}")" "$st" "$(json_escape "${SUMMARIES[$i]}")" \
            "$(json_escape "${DETAILS[$i]}")" "$(json_escape "${HINTS[$i]}")"
        sep=$',\n'
    done
    printf '\n  ]\n}\n'
}

usage() {
    awk 'NR == 1 { next } /^set -u/ { exit } /^#/ { sub(/^# ?/, ""); print }' "$self"
}

# ------------------------------------------------------------------ main
for arg in "$@"; do
    case $arg in
        --full) FULL=1 ;;
        --with-changes) WITH_CHANGES=1 ;;
        --json) JSON=1 ;;
        --selftest) SELFTEST=1 ;;
        -h|--help|help) usage; exit 0 ;;
        *)
            echo "$PROG: unknown option: $arg (try --help)" >&2
            exit 3
            ;;
    esac
done
if [ "$WITH_CHANGES" -eq 1 ] && [ "$FULL" -ne 1 ]; then
    echo "$PROG: --with-changes implies --full (persistent changes are only made by the functional tests)" >&2
    exit 3
fi

if [ "$SELFTEST" -eq 1 ]; then
    run_selftest
    exit $?
fi

if [ "$FULL" -eq 1 ]; then
    if [ "$WITH_CHANGES" -eq 1 ]; then MODE_NAME="full+changes"; else MODE_NAME=full; fi
fi

check_preflight
check_volume
check_hhd_volume
check_leds
check_lifecycle
check_imu
check_sleep
check_bluetooth
check_power
check_battery
check_audio
check_display
check_graphics
check_install
check_decky

CODE=$(exit_code "$PASS_N" "$FAIL_N" "$SKIP_N" "$UNKNOWN_N")
if [ "$JSON" -eq 1 ]; then
    render_json
else
    render_text
fi
exit "$CODE"
