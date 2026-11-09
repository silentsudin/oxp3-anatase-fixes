#!/usr/bin/env bash
# oxp3-wizard - guided, resumable hardware validation that needs eyes and hands.
#
#   tools/oxp3-wizard.sh                       run every phase interactively (resumable)
#   tools/oxp3-wizard.sh --phase volume        only one phase (repeatable/comma list)
#   tools/oxp3-wizard.sh --resume [FILE]       continue the newest (or a given) run
#   tools/oxp3-wizard.sh --non-interactive     print the plan only; no prompt, no write
#   tools/oxp3-wizard.sh --dry-run             plan + the exact commands; no write, no run
#   tools/oxp3-wizard.sh --selftest            offline self-test of the state machine
#   tools/oxp3-wizard.sh --version             version
#   tools/oxp3-wizard.sh --help                this header
#
# What it is
#   Some acceptance checks cannot be automated: does a volume hold ramp, which LED
#   zone lights up, does the home button do the configured action, is the device
#   still warm after a real suspend, how loud is the fan, which speaker EQ variant
#   sounds better, is XMX upscaling active and artefact-free.  This wizard walks the
#   owner through them in dependency order, explains why each one matters, shows the
#   exact command/action, lets every item be skipped, and writes the answers plus
#   verifier check ids to docs/state/validation-<UTC timestamp>.json and .md.
#
# Safety model
#   * No sudo, no reboot, no unit install, no persistent configuration write.
#   * Privileged steps (logind restart, battery limit/bypass, fan mode) are only
#     PRINTED; the owner runs them and reports what happened.
#   * The only live-state writes are body RGB (LED phase) and the speaker EQ
#     drop-in (audio phase, user scope).  Both need the phase to be started, the
#     phase confirmation word typed and a per-item [y] answer, and both are
#     restored at the end (or the exact restore command is printed).
#   * Suspend is the only machine-wide action: it needs the literal word "suspend"
#     at that item; an empty answer skips it.  It is never run in plan/dry-run/
#     selftest/simulate modes.
#   * Every executed command has a timeout ($OXP3_WIZARD_CMD_TIMEOUT, 20 s default).
#   * Ctrl-C runs the registered restores, then exits; the journal can be resumed.
#
# State
#   The JSON is derived from an append-only journal next to it; a run that is
#   interrupted or powered off during the suspend item can be resumed with
#   --resume and keeps every answer it already recorded.
#
# Exit codes: 0 completed/planned, 1 at least one item failed, 2 usage/state error,
#             130 interrupted.
#
# Docs: docs/WIZARD.md
set -u

PROG=oxp3-wizard
VERSION=1.0.0
SCHEMA=1
UNIQUE_NAME=${UNIQUE_NAME:-wizard}

export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
TOOLS_DIR=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 2
REPO=$(CDPATH= cd -- "$TOOLS_DIR/.." && pwd) || exit 2

# ------------------------------------------------------------------ defaults
STATE_DIR_DEFAULT=$REPO/docs/state
STATE_DIR=$STATE_DIR_DEFAULT
STATE_DIR_SET=0
CMD_TIMEOUT=${OXP3_WIZARD_CMD_TIMEOUT:-20}
VERIFY_TIMEOUT=${OXP3_WIZARD_VERIFY_TIMEOUT:-180}
SUSPEND_TIMEOUT=${OXP3_WIZARD_SUSPEND_TIMEOUT:-420}
MODE=interactive
PLAN=0
DRY=0
SELFTEST=0
SIMULATE=0
RESUME=0
RESUME_ARG=""
PHASES_ARG=""
RUN_TS=""
JOURNAL=""
OUT_JSON=""
OUT_MD=""
SELECTED=()
EOF_HIT=0
QUIT=0
CHECK_STATE_FILE=""
declare -A CHECK_STATE=()
declare -A DONE=()
declare -A RESTORE_DESC=()
declare -A RESTORE_CMD=()
declare -A RESTORE_KIND=()
RESTORE_KEYS=()
RESTORE_RESULTS=()
FAN_ENABLE=""
FAN_PWM=""
SUSPEND_ATTEMPTED=0
PF_INSTALL_RC=""
PF_VERIFY_RC=""
PF_COUNTS=""
PF_FAILING=""
EQ_WAS_ON=0
LED_DIRTY=0
AUDIO_DIRTY=0

US=$'\x1f'

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }
have() { command -v "$1" >/dev/null 2>&1; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
today_utc() { date -u +%Y-%m-%d; }

usage() { awk 'NR == 1 { next } /^set -u/ { exit } /^#/ { sub(/^# ?/, ""); print }' "$self"; }

# ------------------------------------------------------------------ arguments
while [ "$#" -gt 0 ]; do
    case $1 in
        --phase|--phases)
            [ "$#" -ge 2 ] || die "--phase needs a name"
            PHASES_ARG=${PHASES_ARG:+$PHASES_ARG,}$2; shift 2 ;;
        --phase=*|--phases=*) PHASES_ARG=${PHASES_ARG:+$PHASES_ARG,}${1#*=}; shift ;;
        --resume)
            RESUME=1
            if [ "$#" -ge 2 ] && [ "${2#--}" = "$2" ]; then RESUME_ARG=$2; shift 2; else shift; fi ;;
        --resume=*) RESUME=1; RESUME_ARG=${1#*=}; shift ;;
        --state-dir|--out)
            [ "$#" -ge 2 ] || die "$1 needs a directory"
            STATE_DIR=$2; STATE_DIR_SET=1; shift 2 ;;
        --state-dir=*|--out=*) STATE_DIR=${1#*=}; STATE_DIR_SET=1; shift ;;
        --non-interactive|--plan) PLAN=1; MODE=plan; shift ;;
        --dry-run) DRY=1; MODE=dry-run; shift ;;
        --selftest) SELFTEST=1; shift ;;
        --simulate) SIMULATE=1; shift ;;
        --timeout)
            [ "$#" -ge 2 ] || die "--timeout needs seconds"
            CMD_TIMEOUT=$2; shift 2 ;;
        --version) printf '%s %s\n' "$PROG" "$VERSION"; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$PHASES_ARG" ] || true
case $CMD_TIMEOUT in ''|*[!0-9]*) die "--timeout must be a number" ;; esac
[ "$CMD_TIMEOUT" -gt 0 ] || die "--timeout must be positive"

# ------------------------------------------------------------------ phase and item inventory
PHASES=()
ITEM_ORDER=()
declare -A PHASE_TITLE PHASE_WHY PHASE_COMPONENT PHASE_DOC PHASE_WORD PHASE_ACTION
declare -A ITEM_PHASE ITEM_KIND ITEM_EXEC ITEM_EXPECT ITEM_CHECKS ITEM_TITLE
declare -A ITEM_PROMPT ITEM_OPTIONS ITEM_CMD ITEM_DOC ITEM_NOTE

CUR_PHASE=""
def_phase() { # id title why component doc confirm-word action-text
    CUR_PHASE=$1
    PHASES+=("$1")
    PHASE_TITLE[$1]=$2
    PHASE_WHY[$1]=$3
    PHASE_COMPONENT[$1]=$4
    PHASE_DOC[$1]=$5
    PHASE_WORD[$1]=$6
    PHASE_ACTION[$1]=$7
}

d() { # id kind exec expect checks title prompt options cmd doc note
    local id=$1
    [ -z "${ITEM_PHASE[$id]:-}" ] || die "duplicate item id: $id"
    ITEM_ORDER+=("$id")
    ITEM_PHASE[$id]=$CUR_PHASE
    ITEM_KIND[$id]=$2
    ITEM_EXEC[$id]=$3
    ITEM_EXPECT[$id]=$4
    ITEM_CHECKS[$id]=$5
    ITEM_TITLE[$id]=$6
    ITEM_PROMPT[$id]=$7
    ITEM_OPTIONS[$id]=$8
    ITEM_CMD[$id]=$9
    ITEM_DOC[$id]=${10}
    ITEM_NOTE[$id]=${11:-0}
}

V=docs/VALIDATION.md
LH=docs/hw/home-button-leds.md
SP=docs/hw/sleep-power.md
BT=docs/hw/battery-tdp.md
PF=docs/hw/power-fan.md
AU=docs/hw/audio-harman-apo.md
XM=docs/xess-xmx.md
HO=docs/HANDOVER.md

def_phase preflight "Preflight" \
    "A hands-on answer is only meaningful against a known install and verifier state: which components are installed, which rows already fail, and whether this shell can see the user session." \
    "" "docs/INSTALL-HW.md §5; $V §1" "" \
    "read-only: tools/oxp3-preflight.sh, tools/oxp3-hw-install.sh --status and tools/oxp3-verify.sh --json (default mode, never --full)"
d preflight.install info read -- "" "Install state" "" "" "tools/oxp3-hw-install.sh --status" "docs/INSTALL-HW.md §5" 0
d preflight.verify info read -- "" "Verifier summary" "" "" "tools/oxp3-verify.sh --json" "$V §1" 0
d preflight.preflight info read -- "" "Preflight verdict" \
    "Would an install complete on this host, and which components would be skipped? Read-only; the installer refuses on FAIL unless --force." \
    "" "tools/oxp3-preflight.sh" "docs/PREFLIGHT.md" 0
d preflight.session choice none "" "" "Validation session" \
    "Where are you running this from? The home-button unit, the RGB keeper and the EQ need the user session (a root/SSH shell cannot see them)." \
    "desktop=Desktop session on the device|ssh=SSH or root shell|console=Plain console" "" "docs/WIZARD.md §2" 0

def_phase volume "Volume rocker: ramp vs step" \
    "The original bug: the kernel repeats the rocker key while held, so a hold ramps the volume. The fix normalises the key so one press is one step and a 3 s hold is still one step. Automated rows cannot hear the difference." \
    "volume" "$V §3 volume; docs/hw/volume-buttons.md" "" \
    "hold the rocker and count the on-screen steps; the daemon is read with tools/oxp3-voltap/oxp3-voltap check --kv"
d vol.hold_ramp yesno read no "vol.hwdb,vol.hold" "Hold Volume-Up: ramp or step?" \
    "Hold Volume-Up for 3 s. Did the level ramp continuously (more than one step)?" "" \
    "tools/oxp3-voltap/oxp3-voltap check --kv" "$V §3 volume" 1
d vol.taps choice none 3 "" "Three taps" \
    "Tap Volume-Up 3 times, 1 s apart. How many steps did the level move?" \
    "3=Exactly 3 steps|fewer=Fewer than 3|more=More than 3" "" "$V §3 volume" 1
d vol.down yesno none yes "vol.hold" "Hold Volume-Down" \
    "Hold Volume-Down for 3 s. Did the level move exactly one step (no ramp)?" "" "" "$V §3 volume" 0
d vol.daemon info read -- "vol.unit,vol.grab,vol.topology,vol.holder" "Volume daemon state" "" "" \
    "tools/oxp3-voltap/oxp3-voltap check --kv" "$V §3 volume" 0

def_phase leds "Body RGB: 5 zones, brightness, one effect" \
    "The body lighting is a vendor HID protocol, not /sys/class/leds: the only way to know the zone table is right is to light each zone in a distinct colour and look. This also proves the hidraw permission chain end to end." \
    "leds,rgb" "$V §3 home button / LEDs / RGB; $LH §6" LEDS \
    "writes live RGB frames: tools/oxp3-led.py solid <hex> --sides <N> (restored at the end)"
d led.zone1 choice action expected "led.status,led.zones,led.write" "Zone 1: left stick" \
    "Zone 1 was set to red. What did you see?" \
    "expected=Red, left stick only|other=Different colour|nothing=Nothing lit|wrong=Another zone lit" \
    "tools/oxp3-led.py solid ff0000 --sides 1" "$LH §6" 0
d led.zone2 choice action expected "led.status,led.zones,led.write" "Zone 2: right stick" \
    "Zone 2 was set to green. What did you see?" \
    "expected=Green, right stick only|other=Different colour|nothing=Nothing lit|wrong=Another zone lit" \
    "tools/oxp3-led.py solid 00ff00 --sides 2" "$LH §6" 0
d led.zone5 choice action expected "led.status,led.zones,led.write" "Zone 5: G-key" \
    "Zone 5 was set to blue. What did you see?" \
    "expected=Blue, G-key area only|other=Different colour|nothing=Nothing lit|wrong=Another zone lit" \
    "tools/oxp3-led.py solid 0000ff --sides 5" "$LH §6" 0
d led.zone6 choice action expected "led.status,led.zones,led.write" "Zone 6: top strip" \
    "Zone 6 was set to white. What did you see?" \
    "expected=White, top strip only|other=Different colour|nothing=Nothing lit|wrong=Another zone lit" \
    "tools/oxp3-led.py solid ffffff --sides 6" "$LH §6" 0
d led.zone7 choice action expected "led.status,led.zones,led.write" "Zone 7: controller connector" \
    "Zone 7 was set to magenta. What did you see?" \
    "expected=Magenta, connector only|other=Different colour|nothing=Nothing lit|wrong=Another zone lit" \
    "tools/oxp3-led.py solid ff00ff --sides 7" "$LH §6" 0
d led.brightness choice action expected "" "Brightness levels 1-4" \
    "Levels 1, 2, 3, 4 were written. Did brightness increase clearly from 1 (dimmest) to 4 (brightest)?" \
    "expected=Yes, four distinct levels|partial=Some levels looked the same|no=No visible change" \
    "for l in 1 2 3 4; do tools/oxp3-led.py brightness \"\$l\" --sides all; sleep 1; done" "$LH §6" 0
d led.effect yesno action yes "led.zones" "One effect" \
    "An aurora effect was started on all zones. Did every zone animate together?" "" \
    "tools/oxp3-led.py effect aurora --sides all" "$LH §6" 0
d led.rgbd info read -- "rgbd.unit,rgbd.check" "RGB keeper config" "" "" \
    "tools/oxp3-rgbd.py --check --config ${XDG_CONFIG_HOME:-$HOME/.config}/oxp3-rgb.toml" "$V §3 rgbd" 0

def_phase homebtn "Home button action" \
    "A press is decoded from the vendor HID node and emitted as KEY_HOMEPAGE; the journal proves the press, only the owner can say whether the configured action actually happened." \
    "leds" "$V §3 home button / LEDs / RGB; $LH §7" "" \
    "press the home button once, then read journalctl --user -u oxp3-homebtn -b"
d homebtn.unit info read -- "homebtn.unit,homebtn.mapping,hid.access" "Home-button daemon" "" "" \
    "systemctl --user is-active oxp3-homebtn" "$V §3 homebtn" 0
d homebtn.press choice none homepage "" "Press the home button once" \
    "Press the home button once (desktop or in-game). What happened?" \
    "homepage=The configured action happened (Homepage/Steam)|nothing=Nothing happened|other=A different action happened" "" "$LH §7" 1

def_phase sleep "Real suspend: policy, one cycle, warmth" \
    "Suspend works, but nothing used to ask for it: logind had no key policy, PowerDevil and hhd block the key, and a 3 s aborted cycle is indistinguishable from sleep in most tools. This phase proves one real s2idle cycle and records how warm the device felt." \
    "sleep" "$V §3 sleep; $SP" "" \
    "the policy step is sudo (restart systemd-logind, printed only); the suspend step runs as your user and only after you type suspend"
d sleep.policy choice manual installed "sleep.units,sleep.trigger" "logind sleep policy" \
    "Is the sleep trigger policy installed and parsed (verifier sleep.trigger)? If it is missing, install it first: sudo tools/oxp3-hw-install.sh --sleep" \
    "installed=Yes / sleep.trigger PASS|restart=Installed but logind needs a restart|missing=Not installed" \
    "sudo systemctl restart systemd-logind" "$SP" 1
d sleep.pre info read -- "sleep.stats" "Suspend baseline" "" "" \
    "sed -n 1p /sys/power/suspend_stats/success; sed -n 1p /sys/power/suspend_stats/last_hw_sleep" "$V §3 sleep" 0
d sleep.suspend confirm none confirmed "sleep.stats,sleep.suspend" "One real suspend" \
    "This really suspends the machine. Type 'suspend' to run systemctl suspend now; an empty answer skips it." "" \
    "systemctl suspend" "$V §3 sleep" 0
d sleep.warm yesno none no "sleep.measure" "Warmth after wake" \
    "After waking, did the device feel warm or hot to the touch?" "" "" "$SP" 0
d sleep.wake choice none "" "" "How did it wake?" \
    "How did the machine wake up?" \
    "button=Power button|keyboard=Keyboard or trackpad|other=Something else|no-wake=It did not wake by itself" "" "$SP" 0

def_phase battery "Battery: charge limit and bypass" \
    "The charge limit (ACPI _BTP) and charge-inhibit ('bypass') are root-only writes, so the wizard prints the commands and records what you observe. This is also the check that the EC stops charging while plugged in." \
    "battery" "$V §3 battery; $BT §5" "" \
    "printed only (sudo): tools/oxp3-powerctl.py battery limit 80 and battery bypass on; reset commands are printed at the end"
d bat.limit choice manual applied "bat.threshold,bat.limit" "Charge limit" \
    "Run the printed command (no --save), then read BAT0/charge_control_end_threshold. Did it apply?" \
    "applied=Threshold reads 80|not-applied=Still 100|other=A different value" \
    "sudo tools/oxp3-powerctl.py battery limit 80" "$BT §5" 1
d bat.bypass choice manual inhibited "bat.behaviour" "Charge bypass" \
    "Plug in the adapter, run the printed command, then read charge_behaviour. Did charging stop?" \
    "inhibited=charge_behaviour shows inhibit-charge|auto=Still auto|unknown=Cannot tell" \
    "sudo tools/oxp3-powerctl.py battery bypass on" "$BT §5" 1
d bat.restore info manual -- "bat.threshold,bat.behaviour" "Reset battery state" "" "" \
    "sudo tools/oxp3-powerctl.py battery bypass off; sudo tools/oxp3-powerctl.py battery limit off" "$BT §5" 0

def_phase fan "Fan: baseline, manual steps, audibility" \
    "The EC fan is readable (rpm/pwm) but only your ears can say whether manual pwm steps are clearly audible and whether the curve follows load. Writes need root, so the steps are printed; the restore command is derived from the baseline read here." \
    "fan,power" "$V §3 power / fan; $PF" "" \
    "read-only baseline: tools/oxp3-powerctl.py fan status --json; manual steps and the restore are printed (sudo)"
d fan.auto info read -- "pwr.fan" "Fan auto baseline" "" "" \
    "tools/oxp3-powerctl.py fan status --json" "$PF" 0
d fan.manual yesno manual yes "pwr.fan,fan.savemode" "Manual fan steps" \
    "Run the printed commands (pwm 90, then 160, then 255). Did the fan get clearly louder?" "" \
    "sudo tools/oxp3-powerctl.py fan manual 90; sudo tools/oxp3-powerctl.py fan manual 160; sudo tools/oxp3-powerctl.py fan manual 255" "$PF" 1
d fan.curve yesno none "" "" "Fan curve under load" \
    "After a few minutes in a game, does the fan rise with temperature and settle again instead of hunting?" "" "" "$PF" 1
d fan.restore info manual -- "" "Restore fan mode" "" "" "" "$PF" 0

def_phase audio "Speaker EQ A/B" \
    "The recovered Harman APO is a PipeWire filter-chain; only listening tells us whether it improves the speaker. The A/B toggles the drop-in (user scope) and the model table is recorded as the objective reference." \
    "audio" "$V §3 audio; $AU" AUDIO \
    "writes the user EQ drop-in via tools/audio/eq-on.sh / eq-off.sh; the state you had at the start is restored at the end"
d aud.eq_on yesno action yes "aud.scripts,aud.eq,aud.dummy" "EQ ON" \
    "Start a familiar track; the EQ was switched ON. Does the speaker sound clean (voices clear, no rattling or boom)?" "" \
    "tools/audio/eq-on.sh" "$AU" 1
d aud.eq_off yesno action yes "aud.scripts,aud.eq" "EQ OFF" \
    "The EQ was switched OFF. Does it now sound worse than with EQ on?" "" \
    "tools/audio/eq-off.sh" "$AU" 1
d aud.pref choice none "" "" "A/B preference" \
    "After hearing both, which do you prefer?" \
    "on=EQ on|off=EQ off|same=No difference" "" "$AU" 0
d aud.model info read -- "aud.model" "EQ model table (optional measurement)" "" "" \
    "tools/audio/eq_response.py" "$AU" 0

def_phase xmx "XMX / XeSS upscaling in-game" \
    "The driver stack can be verified statically, but whether upscaling is actually active in a game, and whether it is artefact-free, needs a launch and eyes on the frame. This phase prints the launch recipe and records the verdict." \
    "xess-xmx" "$V §3 graphics / XMX; $XM" "" \
    "launch through tools/xmx-run.sh -- <game> (Flatpak: docs/hw/flatpak-xmx.md); read-only prerequisite probe only"
d xmx.launch info read -- "gfx.xmx.icd,gfx.xmx.helper,gfx.xmx.igc" "XMX launch prerequisites" "" "" \
    "tools/xmx-run.sh -- true" "$XM" 0
d xmx.upscale yesno none yes "gfx.xmx.icd,gfx.xmx.trace" "Upscaling active" \
    "In-game, is XeSS upscaling active and visibly applied (option on, frame softer than native, Arc/XMX indicators agreeing)?" "" "" "$XM" 1
d xmx.artifacts yesno none no "gfx.xmx.driver,gfx.xmx.kernels" "Artefacts" \
    "Any white dots, flicker or edge artefacts with upscaling on?" "" "" "$XM" 1
d xmx.note text none "" "" "XMX notes" \
    "Resolution/fps or anything the next session should know (one line, empty = skip)" "" "" "$XM" 0

def_phase final "Anything else" \
    "The listed checks cover the fixes; the owner usually knows about the one thing that is still wrong. This free-text box and the overall verdict are the handover signal a later session reads first." \
    "" "$HO §8" "" "no command: free text"
d misc.other text none "" "" "Anything else broken?" \
    "Anything else broken, untested or annoying? (one line; empty = nothing)" "" "" "$HO §8" 0
d misc.overall choice none "" "" "Overall verdict" \
    "Overall, is this machine usable for gaming now?" \
    "usable=Usable|usable-caveats=Usable with caveats|broken=Broken" "" "$HO §8" 0

# ------------------------------------------------------------------ small helpers
sanitize() {
    local s=$1
    s=${s//$US/ }
    s=${s//$'\t'/ }
    s=${s//$'\r'/ }
    s=${s//$'\n'/; }
    printf '%s' "$s"
}

json_str() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/\\r}
    s=${s//$'\t'/\\t}
    s=$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037')
    printf '"%s"' "$s"
}

first_line() {
    local s=$1 l
    l=${s%%$'\n'*}
    printf '%s' "${l:0:200}"
}

redact() { local s=$1; s=${s//$HOME/\~}; printf '%s' "$s"; }

has_action() { # phase -> 0 if any item of the phase executes a live write
    local id
    for id in "${ITEM_ORDER[@]}"; do
        [ "${ITEM_PHASE[$id]}" = "$1" ] || continue
        [ "${ITEM_EXEC[$id]}" = action ] && return 0
    done
    return 1
}

item_ids_of_phase() { # phase -> one id per line
    local id
    for id in "${ITEM_ORDER[@]}"; do
        [ "${ITEM_PHASE[$id]}" = "$1" ] && printf '%s\n' "$id"
    done
}

# ------------------------------------------------------------------ journal
journal_append() { # phase id title status answer command checks evidence
    local phase=$1 id=$2 title=$3 status=$4 answer=$5 cmd=$6 checks=$7 ev=$8
    local ts line sp si st sa sanswer sc sk sev
    ts=$(now_utc)
    sp=$(sanitize "$phase"); si=$(sanitize "$id"); st=$(sanitize "$title")
    sa=$(sanitize "$status"); sanswer=$(sanitize "$answer")
    sc=$(sanitize "$cmd"); sk=$(sanitize "$checks")
    sev=$(sanitize "$ev")
    line=$sp$US$si$US$st$US$sa$US$sanswer$US$sc$US$sk$US$sev$US$ts
    printf '%s\n' "$line" >>"$JOURNAL"
    DONE["$phase/$id"]=1
}

journal_has() { [ -n "${DONE["$1/$2"]:-}" ]; }

load_journal() {
    [ -f "$JOURNAL" ] || return 0
    local p i rest
    while IFS=$US read -r p i rest; do
        [ -n "$p" ] && DONE["$p/$i"]=1
    done <"$JOURNAL"
}

# ------------------------------------------------------------------ command execution
run_capture() { # timeout cmd-string
    local t=$1 cmd=$2
    if [ "$SIMULATE" = 1 ]; then
        printf '  [sim] %s\n' "$cmd"
        RUN_OUT="simulated: $cmd"
        RUN_RC=0
        return 0
    fi
    RUN_OUT=$(cd "$REPO" && timeout --signal=TERM "$t" bash -c "$cmd" 2>&1)
    RUN_RC=$?
    RUN_OUT=$(redact "$RUN_OUT")
    return 0
}

read_suspend_stats() { # -> SS_SUCCESS SS_FAIL SS_LAST
    SS_SUCCESS=0 SS_FAIL=0 SS_LAST=0
    if [ "$SIMULATE" = 1 ]; then SS_SUCCESS=0; SS_FAIL=0; SS_LAST=0; return 0; fi
    [ -r /sys/power/suspend_stats/success ] && SS_SUCCESS=$(cat /sys/power/suspend_stats/success 2>/dev/null)
    [ -r /sys/power/suspend_stats/fail ] && SS_FAIL=$(cat /sys/power/suspend_stats/fail 2>/dev/null)
    [ -r /sys/power/suspend_stats/last_hw_sleep ] && SS_LAST=$(cat /sys/power/suspend_stats/last_hw_sleep 2>/dev/null)
    case $SS_SUCCESS in ''|*[!0-9]*) SS_SUCCESS=0 ;; esac
    case $SS_FAIL in ''|*[!0-9]*) SS_FAIL=0 ;; esac
    case $SS_LAST in ''|*[!0-9]*) SS_LAST=0 ;; esac
    return 0
}

do_suspend() { # -> SUSP_RC SUSP_DELTA SUSP_LAST SUSP_NOTE
    local before waited=0
    if [ "$SIMULATE" = 1 ]; then
        printf '  [sim] systemctl suspend\n'
        SUSP_RC=0 SUSP_DELTA=1 SUSP_LAST=8
        return 0
    fi
    read_suspend_stats
    before=$SS_SUCCESS
    run_capture "$SUSPEND_TIMEOUT" "systemctl suspend"
    SUSP_RC=$RUN_RC
    if [ "$SUSP_RC" -ne 0 ]; then
        SUSP_DELTA=0 SUSP_LAST=0
        return 0
    fi
    while [ "$waited" -lt "$SUSPEND_TIMEOUT" ]; do
        sleep 2
        waited=$((waited + 2))
        read_suspend_stats
        if [ "$SS_SUCCESS" -gt "$before" ]; then
            SUSP_DELTA=$((SS_SUCCESS - before))
            SUSP_LAST=$SS_LAST
            return 0
        fi
    done
    SUSP_DELTA=0 SUSP_LAST=0
    return 0
}

fan_baseline() {
    local out
    if [ "$SIMULATE" = 1 ]; then FAN_ENABLE=""; FAN_PWM=""; return 0; fi
    out=$(cd "$REPO" && timeout "$CMD_TIMEOUT" tools/oxp3-powerctl.py fan status --json 2>/dev/null) || true
    FAN_ENABLE=$(printf '%s' "$out" | sed -n 's/.*"pwm_enable": *"\([0-9]*\)".*/\1/p')
    FAN_PWM=$(printf '%s' "$out" | sed -n 's/.*"pwm": *\([0-9]*\).*/\1/p')
    return 0
}

audio_eq_on() {
    [ "$SIMULATE" = 1 ] && return 1
    pactl list short sinks 2>/dev/null | grep -q 'oxp3_speaker_eq\.sink'
}

fan_restore_cmd() {
    if [ "${FAN_ENABLE:-}" = 1 ] && [ -n "${FAN_PWM:-}" ]; then
        printf 'sudo tools/oxp3-powerctl.py fan manual %s' "$FAN_PWM"
    else
        printf 'sudo tools/oxp3-powerctl.py fan auto'
    fi
}

# ------------------------------------------------------------------ prompts
read_line() { # prompt -> REPLY; return 2 on EOF
    printf '%s' "$1"
    if ! IFS= read -r REPLY; then
        printf '\n'
        EOF_HIT=1
        return 2
    fi
    REPLY=${REPLY%$'\r'}
    return 0
}

ask_yesno() { # prompt -> ANSWER yes|no
    local a
    while :; do
        read_line "$1 (y/n, empty=skip) " || return 2
        a=$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')
        case $a in
            y|yes) ANSWER=yes; return 0 ;;
            n|no) ANSWER=no; return 0 ;;
            '') return 2 ;;
        esac
        printf '  answer y or n\n'
    done
}

ask_choice() { # options prompt -> CHOICE_KEY CHOICE_LABEL
    local opts=$1 prompt=$2 o n=0 key label
    local -a parts=() keys=() labels=()
    local IFS='|'
    read -r -a parts <<<"$opts"
    IFS=$' \t\n'
    for o in "${parts[@]}"; do
        [ -n "$o" ] || continue
        key=${o%%=*}
        label=${o#*=}
        keys+=("$key")
        labels+=("$label")
        n=$((n + 1))
        printf '    %d) %s\n' "$n" "$label"
    done
    while :; do
        read_line "$prompt [1-$n or key, empty=skip] " || return 2
        if [ -z "$REPLY" ]; then return 2; fi
        if printf '%s' "$REPLY" | grep -qx '[0-9]\+'; then
            if [ "$REPLY" -ge 1 ] && [ "$REPLY" -le "$n" ]; then
                CHOICE_KEY=${keys[$((REPLY - 1))]}
                CHOICE_LABEL=${labels[$((REPLY - 1))]}
                return 0
            fi
        fi
        local i
        for i in "${!keys[@]}"; do
            if [ "$REPLY" = "${keys[$i]}" ] || [ "$REPLY" = "${labels[$i]}" ]; then
                CHOICE_KEY=${keys[$i]}
                CHOICE_LABEL=${labels[$i]}
                return 0
            fi
        done
        printf '  pick a number or one of: %s\n' "$(printf '%s ' "${keys[@]}")"
    done
}

ask_text() { # prompt -> ANSWER (may be empty)
    read_line "$1 " || return 2
    ANSWER=$REPLY
    return 0
}

ask_word() { # prompt word -> 0 ok, 2 EOF, 3 refused
    read_line "$1 " || return 2
    [ "$REPLY" = "$2" ] && return 0
    return 3
}

# ------------------------------------------------------------------ restore queue
restore_add() { # key description command kind(auto|manual)
    local key=$1
    [ -z "${RESTORE_DESC[$key]:-}" ] || return 0
    RESTORE_KEYS+=("$key")
    RESTORE_DESC[$key]=$2
    RESTORE_CMD[$key]=$3
    RESTORE_KIND[$key]=$4
}

restore_run_all() { # -> prints and executes automatic restores; records results
    local key rc status out
    RESTORE_RESULTS=()
    for key in "${RESTORE_KEYS[@]}"; do
        printf '  restore [%s] %s\n' "$key" "${RESTORE_DESC[$key]}"
        printf '    %s\n' "${RESTORE_CMD[$key]}"
        if [ "${RESTORE_KIND[$key]}" = auto ]; then
            if [ "$DRY" = 1 ] || [ "$PLAN" = 1 ]; then
                status=planned
            elif [ "$SIMULATE" = 1 ]; then
                status=simulated
            else
                run_capture "$CMD_TIMEOUT" "${RESTORE_CMD[$key]}"
                rc=$RUN_RC
                if [ "$rc" -eq 0 ]; then status=restored; else status=failed; fi
            fi
        else
            status=manual
        fi
        RESTORE_RESULTS+=("$key"$'\t'"$status"$'\t'"${RESTORE_CMD[$key]}")
    done
    return 0
}

# ------------------------------------------------------------------ preflight verifier
parse_verify_json() { # file -> sets PF_COUNTS PF_FAILING, fills CHECK_STATE
    local f=$1
    PF_COUNTS=""
    PF_FAILING=""
    CHECK_STATE=()
    if ! have python3; then return 1; fi
    local line
    while IFS=$'\t' read -r kind a b c d e; do
        case $kind in
            count)
                PF_COUNTS="total=$a pass=$b fail=$c skip=$d unknown=$e" ;;
            fail) PF_FAILING=${PF_FAILING:+$PF_FAILING,}$a ;;
            check) CHECK_STATE[$a]=$b ;;
        esac
    done <<EOF
$(python3 - "$f" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        d = json.load(fh)
except Exception:
    sys.exit(1)
c = d.get("counts") or {}
print("count\t%s\t%s\t%s\t%s\t%s" % (c.get("total", 0), c.get("pass", 0),
      c.get("fail", 0), c.get("skip", 0), c.get("unknown", 0)))
for chk in d.get("checks") or []:
    if chk.get("state") == "fail":
        print("fail\t%s" % chk.get("id", "?"))
for chk in d.get("checks") or []:
    print("check\t%s\t%s" % (chk.get("id", "?"), chk.get("state", "?")))
PY
)
EOF
    return 0
}

load_prior_verifier() { # rebuild the verifier map of a resumed run from its JSON
    [ -f "$OUT_JSON" ] || return 0
    have python3 || return 0
    local k a b
    while IFS=$'\t' read -r k a b; do
        case $k in
            exit) PF_VERIFY_RC=$a ;;
            count) PF_COUNTS=$a ;;
            fail) PF_FAILING=$b ;;
            check) [ -n "$a" ] && CHECK_STATE[$a]=$b ;;
        esac
    done <<EOF
$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    d = json.load(fh)
v = d.get("verifier") or {}
print("exit\t%s" % v.get("exit_code", "unknown"))
print("count\t%s" % (v.get("counts") or ""))
print("fail\t%s" % (v.get("failing") or ""))
for key, state in (v.get("checks") or {}).items():
    print("check\t%s\t%s" % (key, state))
' "$OUT_JSON" 2>/dev/null)
EOF
    return 0
}

check_state() { printf '%s' "${CHECK_STATE[$1]:-?}"; }

evidence_checks() { # checks-csv -> "verify:id=state;;..."
    local csv=$1 id out=""
    [ -n "$csv" ] || { printf ''; return 0; }
    local IFS=','
    for id in $csv; do
        [ -n "$id" ] || continue
        out=${out:+$out;;}verify:$id=$(check_state "$id")
    done
    printf '%s' "$out"
}

# ------------------------------------------------------------------ output artifacts
write_json() {
    local phase item title status answer cmd checks ev ts
    local -A cpass=() cfail=() cskip=() cblocked=() ctotal=()
    local n=0 pass=0 fail=0 skip=0 blocked=0
    if [ -f "$JOURNAL" ]; then
        while IFS=$US read -r phase item title status answer cmd checks ev ts; do
            [ -n "$item" ] || continue
            n=$((n + 1))
            ctotal[$phase]=$(( ${ctotal[$phase]:-0} + 1 ))
            case $status in
                pass) pass=$((pass + 1)); cpass[$phase]=$(( ${cpass[$phase]:-0} + 1 )) ;;
                fail) fail=$((fail + 1)); cfail[$phase]=$(( ${cfail[$phase]:-0} + 1 )) ;;
                skip) skip=$((skip + 1)); cskip[$phase]=$(( ${cskip[$phase]:-0} + 1 )) ;;
                *) blocked=$((blocked + 1)); cblocked[$phase]=$(( ${cblocked[$phase]:-0} + 1 )) ;;
            esac
        done <"$JOURNAL"
    fi

    {
        printf '{\n'
        printf '  "schema": %s,\n' "$SCHEMA"
        printf '  "tool": %s,\n' "$(json_str "$PROG")"
        printf '  "version": %s,\n' "$(json_str "$VERSION")"
        printf '  "unique_name": %s,\n' "$(json_str "$UNIQUE_NAME")"
        printf '  "generated_at": %s,\n' "$(json_str "$(now_utc)")"
        printf '  "started_at": %s,\n' "$(json_str "$STARTED_AT")"
        printf '  "mode": %s,\n' "$(json_str "$MODE")"
        printf '  "run_id": %s,\n' "$(json_str "$RUN_TS")"
        printf '  "resumed": %s,\n' "$([ "$RESUME" = 1 ] && printf true || printf false)"
        printf '  "host": {"kernel": %s, "root": %s, "repo": %s},\n' \
            "$(json_str "$(redact "$(uname -r 2>/dev/null || printf unknown)")")" \
            "$([ "$(id -u)" -eq 0 ] && printf true || printf false)" \
            "$(json_str "$(redact "$REPO")")"
        printf '  "phases": ['
        local first=1 p
        for p in "${SELECTED[@]}"; do
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '%s' "$(json_str "$p")"
        done
        printf '],\n'
        printf '  "verifier": {"exit_code": %s, "counts": %s, "failing": %s, "checks": {' \
            "$(json_str "${PF_VERIFY_RC:-unknown}")" "$(json_str "$PF_COUNTS")" \
            "$(json_str "$PF_FAILING")"
        first=1
        for p in $(printf '%s\n' "${!CHECK_STATE[@]}" | sort); do
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '%s: %s' "$(json_str "$p")" "$(json_str "${CHECK_STATE[$p]}")"
        done
        printf '}},\n'
        printf '  "items": [\n'
        first=1
        if [ -f "$JOURNAL" ]; then
            while IFS=$US read -r phase item title status answer cmd checks ev ts; do
                [ -n "$item" ] || continue
                [ "$first" = 1 ] || printf ',\n'
                first=0
                printf '    {"phase": %s, "id": %s, "title": %s, "status": %s, "answer": %s, "command": %s, "checks": [' \
                    "$(json_str "$phase")" "$(json_str "$item")" "$(json_str "$title")" \
                    "$(json_str "$status")" "$(json_str "$answer")" "$(json_str "$cmd")"
                local cf=1 c
                local IFS=','
                for c in $checks; do
                    [ -n "$c" ] || continue
                    [ "$cf" = 1 ] || printf ', '
                    cf=0
                    printf '%s' "$(json_str "$c")"
                done
                IFS=$' \t\n'
                printf '], "evidence": ['
                local ef=1 e rest=$ev
                while [ -n "$rest" ]; do
                    case $rest in
                        *';;'*) e=${rest%%';;'*}; rest=${rest#*';;'} ;;
                        *) e=$rest; rest='' ;;
                    esac
                    [ "$ef" = 1 ] || printf ', '
                    ef=0
                    printf '%s' "$(json_str "$e")"
                done
                printf '], "timestamp": %s}' "$(json_str "$ts")"
            done <"$JOURNAL"
        fi
        printf '\n  ],\n'
        printf '  "counts": {"total": %s, "pass": %s, "fail": %s, "skip": %s, "blocked": %s},\n' \
            "$n" "$pass" "$fail" "$skip" "$blocked"
        printf '  "phases_summary": {'
        first=1
        for p in "${SELECTED[@]}"; do
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '%s: {"total": %s, "pass": %s, "fail": %s, "skip": %s, "blocked": %s}' \
                "$(json_str "$p")" "${ctotal[$p]:-0}" "${cpass[$p]:-0}" \
                "${cfail[$p]:-0}" "${cskip[$p]:-0}" "${cblocked[$p]:-0}"
        done
        printf '},\n'
        printf '  "restore": ['
        first=1
        local key st rc
        for key in "${RESTORE_KEYS[@]}"; do
            st=manual
            for rc in "${RESTORE_RESULTS[@]:-}"; do
                case $rc in
                    "$key"$'\t'*) st=${rc#*$'\t'}; st=${st%%$'\t'*} ;;
                esac
            done
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '{"resource": %s, "status": %s, "command": %s}' \
                "$(json_str "$key")" "$(json_str "$st")" "$(json_str "${RESTORE_CMD[$key]}")"
        done
        printf '],\n'
        printf '  "last_validated_suggestions": ['
        first=1
        local comp ev line
        while IFS=$'\t' read -r comp ev line; do
            [ -n "$comp" ] || continue
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '{"component": %s, "date": %s, "evidence": %s, "one_line": %s}' \
                "$(json_str "$comp")" "$(json_str "$(today_utc)")" \
                "$(json_str "$ev")" "$(json_str "$line")"
        done <<EOF
$(suggestion_rows)
EOF
        printf '],\n'
        printf '  "open_points": ['
        first=1
        local op
        while IFS= read -r op; do
            [ -n "$op" ] || continue
            [ "$first" = 1 ] || printf ', '
            first=0
            printf '%s' "$(json_str "$op")"
        done <<EOF
$(open_points)
EOF
        printf ']\n'
        printf '}\n'
    } >"$OUT_JSON"
}

suggestion_rows() { # component<TAB>evidence-value<TAB>one-line edit
    local phase comp npass ntotal one ev
    local -A seen=()
    for phase in "${SELECTED[@]}"; do
        comp=${PHASE_COMPONENT[$phase]}
        [ -n "$comp" ] || continue
        case $comp in *,*) ;; esac
        local c
        local IFS=','
        for c in $comp; do
            [ -n "${seen[$c]:-}" ] && continue
            seen[$c]=1
            npass=0; ntotal=0
            local p2 i st
            for p2 in "${SELECTED[@]}"; do
                [ "${PHASE_COMPONENT[$p2]}" = "$c" ] || case ",${PHASE_COMPONENT[$p2]}," in *",$c,"*) ;; *) continue ;; esac
                while IFS= read -r i; do
                    st=$(journal_status "$p2" "$i")
                    [ -n "$st" ] || continue
                    ntotal=$((ntotal + 1))
                    [ "$st" = pass ] && npass=$((npass + 1))
                done <<EOF2
$(item_ids_of_phase "$p2")
EOF2
            done
            [ "$ntotal" -gt 0 ] || continue
            ev="docs/state/$(basename "$OUT_MD"): $c $npass/$ntotal pass (v$VERSION, $(today_utc))"
            one="components.yaml: $c.last_validated.evidence = \"$ev\" (date: $(today_utc))"
            printf '%s\t%s\t%s\n' "$c" "$ev" "$one"
        done
        IFS=$' \t\n'
    done
}

journal_status() { # phase id -> status or empty
    local p i rest
    [ -f "$JOURNAL" ] || return 0
    while IFS=$US read -r p i rest; do
        if [ "$p" = "$1" ] && [ "$i" = "$2" ]; then
            rest=${rest#*$US}
            printf '%s' "${rest%%$US*}"
            return 0
        fi
    done <"$JOURNAL"
}

open_points() {
    local phase id st
    for phase in "${SELECTED[@]}"; do
        while IFS= read -r id; do
            st=$(journal_status "$phase" "$id")
            [ "$st" = blocked ] || continue
            printf '%s/%s is blocked; see the recorded evidence.\n' "$phase" "$id"
        done <<EOF
$(item_ids_of_phase "$phase")
EOF
    done
    printf '%s\n' "No sudo was used: every privileged step (logind restart, battery limit/bypass, fan mode) is a printed command the owner runs."
    printf '%s\n' "The verifier ran in default mode only; --full (root, invasive) and --with-changes (persistent) are outside this wizard by design."
    printf '%s\n' "Answers are the owner's observations, not measurements; they are recorded as evidence, not as automated PASS rows."
}

write_md() {
    local phase item title status answer cmd checks ev ts
    local generated counts npass=0 nfail=0 nskip=0 nblocked=0
    generated=$(now_utc)
    if [ -f "$JOURNAL" ]; then
        while IFS=$US read -r phase item title status answer cmd checks ev ts; do
            [ -n "$item" ] || continue
            case $status in
                pass) npass=$((npass + 1)) ;;
                fail) nfail=$((nfail + 1)) ;;
                skip) nskip=$((nskip + 1)) ;;
                *) nblocked=$((nblocked + 1)) ;;
            esac
        done <"$JOURNAL"
    fi
    {
        printf '# Hardware validation record — %s\n\n' "$RUN_TS"
        printf 'Run: `%s` v%s, mode `%s`%s. Kernel `%s`. JSON: `%s`.\n\n' \
            "$PROG" "$VERSION" "$MODE" "$([ "$RESUME" = 1 ] && printf ', resumed' || true)" \
            "$(redact "$(uname -r 2>/dev/null || printf unknown)")" "$(basename "$OUT_JSON")"
        printf 'Items: **%d pass, %d fail, %d skip, %d blocked**. Verifier: exit %s, %s.\n\n' \
            "$npass" "$nfail" "$nskip" "$nblocked" "${PF_VERIFY_RC:-unknown}" "${PF_COUNTS:-not read}"
        if [ -n "$PF_FAILING" ]; then
            printf 'Verifier rows already failing before the walkthrough: `%s`.\n\n' "$PF_FAILING"
        fi
        printf 'This file is evidence, not a generated component table: do not edit generated docs from\n'
        printf 'it. The source of truth is `docs/hw/components.yaml`; the suggested `last_validated`\n'
        printf 'edits are printed at the end of this file and in the console (the wizard never edits it).\n\n'

        for phase in "${SELECTED[@]}"; do
            local any=0
            while IFS= read -r item; do
                journal_has "$phase" "$item" && any=1
            done <<EOF
$(item_ids_of_phase "$phase")
EOF
            [ "$any" = 1 ] || continue
            printf '## %s — %s\n\n' "$phase" "${PHASE_TITLE[$phase]}"
            printf '_Why:_ %s\n\n' "${PHASE_WHY[$phase]}"
            printf '| item | status | answer | command / action | verify checks | time |\n'
            printf '|---|---|---|---|---|---|\n'
            while IFS=$US read -r p item title status answer cmd checks ev t; do
                [ "$p" = "$phase" ] || continue
                printf '| `%s` | **%s** | %s | %s | %s | %s |\n' \
                    "$item" "$status" "$(md_cell "$answer")" "$(md_cell "$cmd")" \
                    "$(md_cell "$checks")" "${t#*T}"
            done <"$JOURNAL"
            printf '\n'
        done

        if [ "${#RESTORE_KEYS[@]}" -gt 0 ]; then
            printf '## Restore\n\n'
            local key st rc
            for key in "${RESTORE_KEYS[@]}"; do
                st=manual
                for rc in "${RESTORE_RESULTS[@]:-}"; do
                    case $rc in "$key"$'\t'*) st=${rc#*$'\t'}; st=${st%%$'\t'*} ;; esac
                done
                printf -- '- **%s** (%s): `%s`\n' "$key" "$st" "${RESTORE_CMD[$key]}"
            done
            printf '\n'
        fi

        printf '## Suggested `last_validated` edits (do not auto-apply)\n\n'
        printf '`docs/hw/components.yaml` is the source of truth; after editing it run\n'
        printf '`python3 tools/gen-docs.py --write` in a later session. One line per component:\n\n'
        while IFS=$'\t' read -r comp ev line; do
            [ -n "$comp" ] || continue
            printf '```yaml\n'
            printf '  - id: %s\n' "$comp"
            printf '    last_validated:\n'
            printf '      date: %s\n' "$(today_utc)"
            printf '      evidence: "%s"\n' "$ev"
            printf '```\n\n'
        done <<EOF
$(suggestion_rows)
EOF

        printf '## Anything else\n\n'
        local other overall
        other=$(journal_answer final misc.other)
        overall=$(journal_answer final misc.overall)
        printf -- '- Overall verdict: %s\n' "${overall:-not recorded}"
        printf -- '- Other issues: %s\n\n' "${other:-none recorded}"
        printf '## Open points\n\n'
        while IFS= read -r op; do printf -- '- %s\n' "$op"; done <<EOF
$(open_points)
EOF
    } >"$OUT_MD"
}

md_cell() {
    local s=$1
    s=${s//|/\\|}
    printf '%s' "${s:0:300}"
}

journal_answer() {
    local p i rest
    [ -f "$JOURNAL" ] || return 0
    while IFS=$US read -r p i rest; do
        if [ "$p" = "$1" ] && [ "$i" = "$2" ]; then
            rest=${rest#*$US}; rest=${rest#*$US}
            printf '%s' "${rest%%$US*}"
            return 0
        fi
    done <"$JOURNAL"
}

# ------------------------------------------------------------------ plan / dry-run
print_plan() {
    local phase id n=0 total=${#SELECTED[@]}
    printf '%s %s  mode=%s  run=%s\n' "$PROG" "$VERSION" "$MODE" "${RUN_TS:-plan}"
    if [ "$PLAN" = 1 ]; then
        printf 'plan only: no prompts, no command is run and nothing is written\n'
    else
        printf 'dry run: no command is run and nothing is written; the exact commands are shown\n'
    fi
    printf 'state: %s\n' "$(redact "$STATE_DIR")"
    printf 'repo:  %s\n\n' "$(redact "$REPO")"
    for phase in "${SELECTED[@]}"; do
        n=$((n + 1))
        printf '[phase %d/%d] %s — %s\n' "$n" "$total" "$phase" "${PHASE_TITLE[$phase]}"
        printf '  why: %s\n' "${PHASE_WHY[$phase]}"
        printf '  action: %s\n' "${PHASE_ACTION[$phase]}"
        printf '  docs: %s\n' "${PHASE_DOC[$phase]}"
        [ -n "${PHASE_WORD[$phase]}" ] && printf '  typed confirmation word: %s\n' "${PHASE_WORD[$phase]}"
        while IFS= read -r id; do
            printf '  - %-16s %-7s ' "$id" "${ITEM_KIND[$id]}"
            case ${ITEM_EXEC[$id]} in
                read) printf 'read-only: %s' "${ITEM_CMD[$id]}" ;;
                action) printf 'WIZARD RUNS: %s' "${ITEM_CMD[$id]}" ;;
                manual) printf 'YOU RUN: %s' "${ITEM_CMD[$id]}" ;;
                *)
                    if [ "${ITEM_KIND[$id]}" = confirm ]; then
                        printf 'typed confirmation (%s): %s' "${PHASE_WORD[$phase]:-suspend}" "${ITEM_CMD[$id]}"
                    else
                        printf 'no command'
                    fi ;;
            esac
            printf '\n'
            [ -n "${ITEM_CHECKS[$id]}" ] && printf '      checks: %s\n' "${ITEM_CHECKS[$id]}"
            printf '      doc: %s\n' "${ITEM_DOC[$id]}"
        done <<EOF
$(item_ids_of_phase "$phase")
EOF
        printf '\n'
    done
    printf 'artifacts on completion: %s and %s\n' \
        "$(basename "$OUT_JSON")" "$(basename "$OUT_MD")"
    printf 'no file was created and no command was executed.\n'
}

# ------------------------------------------------------------------ item driver
item_answer_is_pass() { # kind expect answer key
    local kind=$1 expect=$2 answer=$3 key=$4
    case $kind in
        yesno)
            case $expect in
                yes) [ "$answer" = yes ] ;;
                no) [ "$answer" = no ] ;;
                *) return 0 ;;
            esac ;;
        choice)
            [ -z "$expect" ] && return 0
            case ",$expect," in *",$key,"*) return 0 ;; esac
            return 1 ;;
        *) return 0 ;;
    esac
}

ask_item() { # phase id
    local phase=$1 id=$2
    journal_has "$phase" "$id" && return 0
    local kind=${ITEM_KIND[$id]} exec=${ITEM_EXEC[$id]} expect=${ITEM_EXPECT[$id]}
    local checks=${ITEM_CHECKS[$id]} cmd=${ITEM_CMD[$id]} ek=""
    local status answer ev="" key label rc=0

    printf '\n  -- %s  [%s]\n' "${ITEM_TITLE[$id]}" "$id"

    # Resolve a dynamic command.
    if [ "$id" = fan.restore ]; then cmd=$(fan_restore_cmd); fi

    case $exec in
        read)
            printf '     read-only: %s\n' "$cmd"
            run_capture "$CMD_TIMEOUT" "$cmd"
            rc=$RUN_RC
            ev="cmd:$cmd"
            [ "$rc" -ne 0 ] && ev="$ev;;cmd-exit:$rc"
            printf '     -> exit %s: %s\n' "$rc" "$(first_line "$RUN_OUT")"
            ;;
        manual)
            printf '     you run (needs root), then answer:\n       %s\n' "$cmd"
            ;;
        action)
            printf '     wizard runs (live state): %s\n' "$cmd"
            if ! ask_yesno "     run it now?"; then
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip "not confirmed" "$cmd" "$checks" "not run"
                return 0
            fi
            printf '     running...\n'
            run_capture "$CMD_TIMEOUT" "$cmd"
            rc=$RUN_RC
            printf '     -> exit %s: %s\n' "$rc" "$(first_line "$RUN_OUT")"
            ev="cmd:$cmd;;cmd-exit:$rc"
            if [ "$phase" = leds ]; then
                LED_DIRTY=1
                if [ "$SIMULATE" = 1 ] || [ ! -f "$HOME/.config/oxp3-rgb.toml" ]; then
                    restore_add led "body RGB restored" "tools/oxp3-led.py off" auto
                else
                    restore_add led "body RGB restored (keeper re-applies the saved state)" \
                        "systemctl --user restart oxp3-rgbd" auto
                fi
            fi
            if [ "$phase" = audio ]; then
                if [ "$EQ_WAS_ON" = 1 ]; then
                    restore_add audio "speaker EQ restored to ON (state at start)" "tools/audio/eq-on.sh" auto
                else
                    restore_add audio "speaker EQ restored to OFF (state at start)" "tools/audio/eq-off.sh" auto
                fi
            fi
            if [ "$rc" -ne 0 ]; then
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" blocked \
                    "command failed (exit $rc)" "$cmd" "$checks" "$ev"
                return 0
            fi
            ;;
    esac

    # Conditional items that only make sense after a suspend was attempted.
    if [ "$id" = sleep.warm ] || [ "$id" = sleep.wake ]; then
        if [ "$SUSPEND_ATTEMPTED" -ne 1 ]; then
            journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip \
                "no suspend was run in this phase" "$cmd" "$checks" "not-applicable"
            return 0
        fi
    fi

    # Ask.
    case $kind in
        info)
            if [ "$exec" = manual ]; then
                status=pass
                answer="printed only (needs root)"
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" "$status" "$answer" "$cmd" "$checks" "$ev"
                return 0
            fi
            case $id in
                preflight.install)
                    PF_INSTALL_RC=$rc
                    answer="exit=$rc"
                    [ "$rc" -eq 0 ] && status=pass || status=blocked ;;
                preflight.verify)
                    PF_VERIFY_RC=$rc
                    if [ "$rc" -le 1 ]; then status=pass; else status=blocked; fi
                    if [ "$SIMULATE" = 1 ]; then
                        PF_COUNTS="total=0 pass=0 fail=0 skip=0 unknown=0"
                        PF_FAILING=""
                    else
                        tf=$(mktemp "${TMPDIR:-/tmp}/oxp3-wizard-verify.XXXXXX") || tf=""
                        if [ -n "$tf" ]; then
                            printf '%s\n' "$RUN_OUT" >"$tf"
                            parse_verify_json "$tf" || true
                            rm -f -- "$tf"
                        fi
                    fi
                    answer="exit=$rc ${PF_COUNTS}"
                    [ -n "$PF_FAILING" ] && answer="$answer failing=$PF_FAILING" ;;
                *)
                    answer="exit=$rc $(first_line "$RUN_OUT")"
                    [ "$rc" -eq 0 ] && status=pass || status=blocked ;;
            esac
            ev="$ev;;$(evidence_checks "$checks")"
            journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" "$status" "$answer" "$cmd" "$checks" "$ev"
            return 0 ;;
        yesno)
            if ! ask_yesno "$(printf '     %s' "${ITEM_PROMPT[$id]}")"; then
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip "skipped" "$cmd" "$checks" "$ev"
                return 0
            fi
            answer=$ANSWER
            item_answer_is_pass yesno "$expect" "$answer" "" && status=pass || status=fail
            if [ "$status" = fail ] && [ "${ITEM_NOTE[$id]}" = 1 ]; then
                if read_line "     what did you observe? " && [ -n "$REPLY" ]; then
                    answer="$answer ($REPLY)"
                fi
            fi ;;
        choice)
            if ! ask_choice "${ITEM_OPTIONS[$id]}" "$(printf '     %s' "${ITEM_PROMPT[$id]}")"; then
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip "skipped" "$cmd" "$checks" "$ev"
                return 0
            fi
            answer="$CHOICE_LABEL"
            item_answer_is_pass choice "$expect" "$answer" "$CHOICE_KEY" && status=pass || status=fail
            if [ "$status" = fail ] && [ "${ITEM_NOTE[$id]}" = 1 ]; then
                if read_line "     what did you observe? " && [ -n "$REPLY" ]; then
                    answer="$answer ($REPLY)"
                fi
            fi ;;
        text)
            if ! ask_text "$(printf '     %s' "${ITEM_PROMPT[$id]}")"; then
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip "skipped" "$cmd" "$checks" "$ev"
                return 0
            fi
            answer=$REPLY
            status=pass ;;
        confirm)
            if [ "$id" = sleep.suspend ]; then
                ask_word "$(printf '     %s' "${ITEM_PROMPT[$id]}")" suspend
                rc=$?
                if [ "$rc" -ne 0 ]; then
                    if [ "$rc" -eq 3 ]; then
                        journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip \
                            "not confirmed (the word suspend was not typed)" "$cmd" "$checks" \
                            "suspend:not-confirmed"
                    else
                        journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip \
                            "skipped" "$cmd" "$checks" "suspend:skipped"
                    fi
                    return 0
                fi
                SUSPEND_ATTEMPTED=1
                printf '     suspending now; wake the machine when you are ready...\n'
                do_suspend
                if [ "$SUSP_RC" -ne 0 ]; then
                    status=blocked
                    answer="suspend command refused (exit $SUSP_RC)"
                    ev="cmd:systemctl suspend;;cmd-exit:$SUSP_RC"
                elif [ "${SUSP_DELTA:-0}" -ge 1 ] && [ "${SUSP_LAST:-0}" -gt 0 ]; then
                    status=pass
                    answer="one real s2idle cycle: success +${SUSP_DELTA}, last_hw_sleep ${SUSP_LAST}s"
                    ev="cmd:systemctl suspend;;suspend_stats:success+${SUSP_DELTA};;suspend_stats:last_hw_sleep=${SUSP_LAST}s"
                else
                    status=fail
                    answer="no real sleep observed (success did not increment or last_hw_sleep=0)"
                    ev="cmd:systemctl suspend;;suspend_stats:no-delta"
                fi
                ev="$ev;;$(evidence_checks "$checks")"
                journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" "$status" "$answer" "$cmd" "$checks" "$ev"
                return 0
            fi
            journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" skip "no confirmation word defined" "$cmd" "$checks" ""
            return 0 ;;
    esac

    ev="$ev;;$(evidence_checks "$checks")"
    journal_append "$phase" "$id" "${ITEM_TITLE[$id]}" "$status" "$answer" "$cmd" "$checks" "$ev"
}

# ------------------------------------------------------------------ phase driver
skip_phase_remaining() { # phase reason
    local id
    while IFS= read -r id; do
        journal_has "$1" "$id" && continue
        journal_append "$1" "$id" "${ITEM_TITLE[$id]}" skip "$2" "${ITEM_CMD[$id]}" "${ITEM_CHECKS[$id]}" "phase-skipped"
    done <<EOF
$(item_ids_of_phase "$1")
EOF
}

run_phase() { # phase
    local phase=$1 id any=0 remaining=0 total=0
    while IFS= read -r id; do
        any=1
        total=$((total + 1))
        journal_has "$phase" "$id" || remaining=$((remaining + 1))
    done <<EOF
$(item_ids_of_phase "$phase")
EOF
    if [ "$any" = 0 ]; then return 0; fi
    if [ "$remaining" -eq 0 ]; then
        printf '\n[%s] complete (resumed): every item already has a recorded answer\n' "$phase"
        return 0
    fi

    printf '\n[%s] %s\n' "$phase" "${PHASE_TITLE[$phase]}"
    printf '  why: %s\n' "${PHASE_WHY[$phase]}"
    printf '  action: %s\n' "${PHASE_ACTION[$phase]}"
    printf '  docs: %s\n' "${PHASE_DOC[$phase]}"
    [ "$remaining" -lt "$total" ] && \
        printf '  resumed: %d item(s) already answered\n' "$((total - remaining))"

    local ans=y
    if ! read_line "  start this phase? [Y]es/[s]kip/[q]uit "; then
        ans=skip
    else
        ans=${REPLY:-y}
    fi
    case $ans in
        ''|y|Y|yes|Yes) ;;
        s|S|skip|Skip) skip_phase_remaining "$phase" "phase skipped"; return 0 ;;
        q|Q|quit|Quit) QUIT=1; return 0 ;;
        *) : ;;
    esac

    if [ -n "${PHASE_WORD[$phase]}" ] && has_action "$phase"; then
        printf '  this phase writes live state (not persistent config).\n'
        read_line "  type ${PHASE_WORD[$phase]} to continue (empty=skip phase): " || {
            skip_phase_remaining "$phase" "confirmation not given"
            return 0
        }
        if [ "$REPLY" != "${PHASE_WORD[$phase]}" ]; then
            skip_phase_remaining "$phase" "confirmation word not typed"
            return 0
        fi
    fi
    if [ "$phase" = leds ]; then LED_DIRTY=1; fi
    if [ "$phase" = audio ]; then
        if audio_eq_on; then EQ_WAS_ON=1; else EQ_WAS_ON=0; fi
        AUDIO_DIRTY=1
    fi
    if [ "$phase" = battery ]; then
        restore_add battery "battery limit/bypass reset (manual, needs root)" \
            "sudo tools/oxp3-powerctl.py battery bypass off; sudo tools/oxp3-powerctl.py battery limit off" manual
    fi
    if [ "$phase" = fan ]; then
        fan_baseline
        restore_add fan "fan mode restored (manual, needs root)" "$(fan_restore_cmd)" manual
    fi
    local -a ids=()
    while IFS= read -r id; do ids+=("$id"); done <<EOF
$(item_ids_of_phase "$phase")
EOF
    for id in "${ids[@]}"; do
        journal_has "$phase" "$id" && continue
        ask_item "$phase" "$id"
        if [ "$EOF_HIT" = 1 ]; then
            skip_phase_remaining "$phase" "stdin closed"
            QUIT=1
            return 0
        fi
        if [ "$QUIT" = 1 ]; then
            skip_phase_remaining "$phase" "quit requested"
            return 0
        fi
    done
}

select_phases() {
    SELECTED=()
    if [ -z "$PHASES_ARG" ]; then
        SELECTED=("${PHASES[@]}")
        return 0
    fi
    local p found
    local IFS=','
    for p in $PHASES_ARG; do
        found=0
        local q
        for q in "${PHASES[@]}"; do
            [ "$q" = "$p" ] && found=1
        done
        [ "$found" = 1 ] || die "unknown phase: $p (valid: ${PHASES[*]})"
        SELECTED+=("$p")
    done
    IFS=$' \t\n'
    return 0
}

# ------------------------------------------------------------------ artifacts / finish
finish() {
    local rc=0
    printf '\n== restore ==\n'
    if [ "${#RESTORE_KEYS[@]}" -eq 0 ]; then
        printf '  nothing was changed by this wizard run; nothing to restore.\n'
    else
        restore_run_all
    fi
    if [ "$PLAN" = 0 ] && [ "$DRY" = 0 ]; then
        mkdir -p -- "$STATE_DIR" || die "cannot create $STATE_DIR"
        write_json
        write_md
        printf '\n== record ==\n'
        printf '  JSON: %s\n' "$(redact "$OUT_JSON")"
        printf '  MD:   %s\n' "$(redact "$OUT_MD")"
        printf '  journal (resume with --resume): %s\n' "$(redact "$JOURNAL")"
        printf '\n== suggested last_validated edits (do not auto-apply) ==\n'
        local comp ev line
        while IFS=$'\t' read -r comp ev line; do
            [ -n "$comp" ] || continue
            printf '  %s\n' "$line"
        done <<EOF
$(suggestion_rows)
EOF
        printf '\n  After editing docs/hw/components.yaml run: python3 tools/gen-docs.py --write\n'
    fi
    local phase id st
    printf '\n== summary ==\n'
    for phase in "${SELECTED[@]}"; do
        foreach_phase_summary "$phase"
    done
    local nfail rfailed r
    nfail=$(journal_count_status fail)
    [ "${nfail:-0}" -gt 0 ] && rc=1
    rfailed=0
    for r in "${RESTORE_RESULTS[@]:-}"; do
        case $r in *$'\t'failed$'\t'*) rfailed=1 ;; esac
    done
    [ "$rfailed" -eq 1 ] && rc=1
    printf '\nopen points:\n'
    while IFS= read -r line; do printf '  - %s\n' "$line"; done <<EOF
$(open_points)
EOF
    printf '\n'
    return "$rc"
}

foreach_phase_summary() {
    local phase=$1 total=0 pass=0 fail=0 skip=0 blocked=0 id st
    while IFS= read -r id; do
        st=$(journal_status "$phase" "$id")
        [ -n "$st" ] || continue
        total=$((total + 1))
        case $st in
            pass) pass=$((pass + 1)) ;;
            fail) fail=$((fail + 1)) ;;
            skip) skip=$((skip + 1)) ;;
            *) blocked=$((blocked + 1)) ;;
        esac
    done <<EOF
$(item_ids_of_phase "$phase")
EOF
    printf '  %-10s %d/%d pass, %d fail, %d skip, %d blocked\n' \
        "$phase" "$pass" "$total" "$fail" "$skip" "$blocked"
}

journal_count_status() { # status -> count
    local want=$1 p i title st answer cmd checks ev ts n=0
    [ -f "$JOURNAL" ] || { printf 0; return 0; }
    while IFS=$US read -r p i title st answer cmd checks ev ts; do
        [ "$st" = "$want" ] && n=$((n + 1))
    done <"$JOURNAL"
    printf '%s' "$n"
}

on_abort() {
    trap - INT TERM
    printf '\n\ninterrupted: running the registered restores\n'
    restore_run_all
    if [ "$DRY" = 0 ] && [ "$PLAN" = 0 ] && [ -f "$JOURNAL" ]; then
        printf 'partial answers are in %s; continue with: %s --resume %s\n' \
            "$(redact "$JOURNAL")" "$self" "$(redact "$JOURNAL")"
    fi
    exit 130
}

# ------------------------------------------------------------------ selftest
selftest() {
    local tmp checks=0 failed=0
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-wizard-selftest.XXXXXX") || {
        printf 'selftest: cannot create a temp dir\n' >&2
        return 1
    }
    printf 'oxp3-wizard selftest %s (offline; no host access)\n' "$VERSION"
    st_ok() { checks=$((checks + 1)); }
    st_fail() { checks=$((checks + 1)); failed=$((failed + 1)); printf 'FAIL: %s\n' "$*"; }
    st_eq() { # label got want
        if [ "$2" = "$3" ]; then st_ok; else st_fail "$1: got '$2' want '$3'"; fi
    }

    bash -n -- "$self" && st_ok || st_fail "bash -n $self"

    st_eq "sanitize unit separator" "$(sanitize "$(printf 'a%sb\nc\td' "$US")")" "a b; c d"
    st_eq "json escape" "$(json_str 'a"b\c
d')" '"a\"b\\c\nd"'
    if have python3; then
        json_str "$(printf 'x\ty')" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' \
            && st_ok || st_fail "json_str output is not valid JSON"
    else
        st_fail "python3 is required for the selftest"
    fi

    # journal round-trip with delimiter injection attempts
    JOURNAL=$tmp/journal
    : >"$JOURNAL"
    journal_append phase1 item1 "title with $US and $(printf '\t') tab" pass "answer with ;; and , commas" \
        "cmd x" "chk.a,chk.b" "ev1;;ev2"
    journal_append phase1 item2 "second" fail "no" "" "" ""
    load_journal
    journal_has phase1 item1 && st_ok || st_fail "journal_has exact match"
    journal_has phase1 item && st_fail "journal_has must not match a prefix" || st_ok
    st_eq "journal status" "$(journal_status phase1 item2)" "fail"
    st_eq "journal answer" "$(journal_answer phase1 item1)" "answer with ;; and , commas"
    local nlines
    nlines=$(wc -l <"$JOURNAL")
    st_eq "journal lines" "$nlines" "2"

    # status derivation
    item_answer_is_pass yesno yes yes "" && st_ok || st_fail "yesno yes/yes"
    item_answer_is_pass yesno no no "" && st_ok || st_fail "yesno no/no"
    item_answer_is_pass yesno no yes "" && st_fail "yesno mismatch must fail" || st_ok
    item_answer_is_pass choice "a,b" "x" b && st_ok || st_fail "choice pass key"
    item_answer_is_pass choice "" "x" z && st_ok || st_fail "choice without expect always passes"
    item_answer_is_pass choice "a" "x" z && st_fail "choice non-pass key must fail" || st_ok

    # restore dedupe
    RESTORE_KEYS=(); RESTORE_DESC=(); RESTORE_CMD=(); RESTORE_KIND=()
    restore_add k "first" "cmd1" auto
    restore_add k "second" "cmd2" manual
    st_eq "restore dedupe count" "${#RESTORE_KEYS[@]}" "1"
    st_eq "restore first wins" "${RESTORE_CMD[k]}" "cmd1"

    # inventory
    st_eq "phase count" "${#PHASES[@]}" "10"
    st_eq "item count" "${#ITEM_ORDER[@]}" "40"
    local id bad=0
    local chk
    for id in "${ITEM_ORDER[@]}"; do
        [ -n "${ITEM_DOC[$id]:-}" ] || { bad=$((bad + 1)); printf 'FAIL: no doc for %s\n' "$id"; }
        [ -n "${ITEM_TITLE[$id]:-}" ] || { bad=$((bad + 1)); printf 'FAIL: no title for %s\n' "$id"; }
        chk=${ITEM_CHECKS[$id]}
        if [ -n "$chk" ] && ! printf '%s' "$chk" | grep -Eq '^[a-z0-9]+(\.[a-z0-9_]+)+(,[a-z0-9]+(\.[a-z0-9_]+)+)*$'; then
            bad=$((bad + 1)); printf 'FAIL: item %s has a malformed check list: %s\n' "$id" "$chk"
        fi
    done
    [ "$bad" -eq 0 ] && st_ok || st_fail "item metadata completeness"
    if [ -f "$REPO/tools/oxp3-verify.sh" ]; then
        for id in $(printf '%s\n' "${ITEM_CHECKS[@]}" | tr ',' '\n' | grep -v '^$' | sort -u); do
            grep -Fq -- "$id " "$REPO/tools/oxp3-verify.sh" \
                && st_ok || st_fail "verifier check id not found in oxp3-verify.sh: $id"
        done
    else
        st_fail "tools/oxp3-verify.sh not found"
    fi
    local pw
    for pw in leds audio; do
        [ -n "${PHASE_WORD[$pw]}" ] && st_ok || st_fail "phase $pw needs a typed confirmation word"
    done
    [ -n "${PHASE_COMPONENT[leds]}" ] && st_ok || st_fail "leds phase needs a components.yaml target"

    # plan for a single phase must be cheap and side-effect free
    local before after
    before=$(ls -A "$tmp" | sort | tr '\n' ' ')
    OUT_JSON=$tmp/out.json OUT_MD=$tmp/out.md JOURNAL=$tmp/journal
    write_json
    after=$(ls -A "$tmp" | sort | tr '\n' ' ')
    [ -f "$OUT_JSON" ] && st_ok || st_fail "write_json produced no file"
    python3 - "$OUT_JSON" <<'PY' && st_ok || st_fail "generated JSON schema/sanity"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["schema"] == 1
assert d["tool"] == "oxp3-wizard"
assert isinstance(d["items"], list) and d["items"]
states = {"pass", "fail", "skip", "blocked"}
for it in d["items"]:
    assert it["status"] in states, it
    assert set(("phase", "id", "status", "answer", "command", "checks", "timestamp")) <= set(it)
assert sum(d["counts"][k] for k in ("pass", "fail", "skip", "blocked")) == d["counts"]["total"]
PY
    rm -f -- "$OUT_JSON" "$OUT_MD"
    rm -rf -- "$tmp"

    if [ "$failed" -eq 0 ]; then
        printf 'selftest: %d checks passed, 0 failed\n' "$checks"
        return 0
    fi
    printf 'selftest: %d checks, %d failed\n' "$checks" "$failed"
    return 1
}

# ------------------------------------------------------------------ main
STARTED_AT=$(now_utc)

if [ "$SELFTEST" = 1 ]; then
    selftest
    exit $?
fi

STATE_DIR=${STATE_DIR%/}
case $STATE_DIR in
    ''|/) die "invalid state dir" ;;
esac

if [ "$RESUME" = 1 ]; then
    target=$RESUME_ARG
    if [ -z "$target" ]; then
        target=$(ls -1t "$STATE_DIR"/validation-*.journal 2>/dev/null | head -n 1)
        [ -n "$target" ] || die "no validation journal found under $STATE_DIR"
    fi
    [ -f "$target" ] || die "resume target not found: $target"
    case $target in
        *.json) JOURNAL=${target%.json}.journal ;;
        *.journal) JOURNAL=$target ;;
        *) die "resume target must be a .json or .journal file: $target" ;;
    esac
    [ -f "$JOURNAL" ] || die "journal not found next to $target"
    if [ "$STATE_DIR_SET" = 0 ]; then
        STATE_DIR=$(CDPATH= cd -- "$(dirname -- "$JOURNAL")" && pwd) || \
            die "cannot resolve the journal directory"
    fi
    RUN_TS=$(basename "$JOURNAL" .journal)
    RUN_TS=${RUN_TS#validation-}
    OUT_JSON=$STATE_DIR/validation-$RUN_TS.json
    OUT_MD=$STATE_DIR/validation-$RUN_TS.md
    # Keep the record path of the resumed run.
    base=$(basename "$JOURNAL" .journal)
    OUT_JSON=$STATE_DIR/$base.json
    OUT_MD=$STATE_DIR/$base.md
    load_prior_verifier
else
    RUN_TS=$(date -u +%Y%m%dT%H%M%SZ)
    JOURNAL=$STATE_DIR/validation-$RUN_TS.journal
    OUT_JSON=$STATE_DIR/validation-$RUN_TS.json
    OUT_MD=$STATE_DIR/validation-$RUN_TS.md
fi

select_phases

if [ "$PLAN" = 1 ] || [ "$DRY" = 1 ]; then
    print_plan
    exit 0
fi

mkdir -p -- "$STATE_DIR" || die "cannot create $STATE_DIR"
load_journal
trap on_abort INT TERM

printf '%s %s  mode=%s  unique=%s\n' "$PROG" "$VERSION" "$MODE" "$UNIQUE_NAME"
printf 'repo: %s\n' "$(redact "$REPO")"
printf 'record: %s\n' "$(redact "$OUT_JSON")"
printf 'phases: %s\n' "${SELECTED[*]}"
printf 'Every item can be skipped; nothing persistent is changed. Answers are written as soon as\n'
printf 'they are given, so an interrupted run resumes with --resume.\n'
if [ "$RESUME" = 1 ]; then printf 'resuming %s\n' "$(redact "$JOURNAL")"; fi

for phase in "${SELECTED[@]}"; do
    run_phase "$phase"
    [ "$QUIT" = 1 ] && break
done

if [ "$EOF_HIT" = 1 ]; then
    printf '\nstdin closed: remaining items were recorded as skip.\n'
fi

finish
exit $?
