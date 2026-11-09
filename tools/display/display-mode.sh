#!/bin/sh
# display-mode - HDR, wide-colour-gamut and display-profile control for the OXP3
# eDP panel under KDE Plasma 6 (KWin 6.7), entirely in userspace.
#
# KWin owns the connector, so HDR and colour management are compositor
# settings: this script only drives kscreen-doctor, and every change it makes
# is reversible (see 'reset').  It never talks to /dev/dri, never loads a
# gamma ramp and never needs root.  See docs/hw/hdr-vibrance.md.
#
#   tools/display/display-mode.sh status            panel + compositor state
#   tools/display/display-mode.sh hdr on|off|toggle
#   tools/display/display-mode.sh wcg on|off
#   tools/display/display-mode.sh sdr-nits 100..1000
#   tools/display/display-mode.sh icc panel|none    install/clear our ICC profile
#   tools/display/display-mode.sh profile sRGB|EDID|ICC
#   tools/display/display-mode.sh reset             back to Plasma defaults
#   tools/display/display-mode.sh gamemode          what Gamemode needs (print only)
#
# The state we changed is recorded in $XDG_RUNTIME_DIR/oxp3-display-mode.state
# so 'reset' can put the panel back even after a partial failure.
#
# Exit codes: 0 ok, 1 failure, 2 usage.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd) || exit 2
edid_tool="$repo/tools/display/oxp3-edid.py"
icc_tool="$repo/tools/display/oxp3-icc.py"
icc_dir="${XDG_DATA_HOME:-$HOME/.local/share}/color/icc"
icc_file="$icc_dir/oxp3-panel.icc"
state_file="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/oxp3-display-mode.state"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# The kscreen-doctor JSON is the authoritative view; a small python reader keeps
# this script free of jq (which is not guaranteed on an immutable host either).
read_state() {
    python3 - "$@" <<'PY'
import json, subprocess, sys
try:
    raw = subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True, check=True).stdout
except Exception as exc:  # pragma: no cover - diagnostic path
    print(f"error: kscreen-doctor -j failed: {exc}", file=sys.stderr)
    sys.exit(1)
data = json.loads(raw)
want = sys.argv[1] if len(sys.argv) > 1 else None
for out in data.get("outputs", []):
    if not out.get("enabled"):
        continue
    name = out.get("name") or ""
    if want and want.lower() not in name.lower():
        continue
    mode = out.get("currentModeId")
    print(json.dumps({
        "name": name,
        "hdr": out.get("hdr"),
        "wcg": out.get("wcg"),
        "sdr_nits": out.get("sdr-brightness"),
        "icc": out.get("iccProfilePath") or "",
        "profile_source": out.get("colorProfileSource"),
        "hdr_profile_source": out.get("hdrColorProfileSource"),
        "max_bpc": out.get("maxBpc"),
        "brightness": out.get("brightness"),
        "mode": mode,
        "modes": [m.get("name") for m in out.get("modes", [])][:1],
    }))
PY
}

output_name() {
    # eDP first, then whatever is enabled.
    n=$(read_state eDP 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null || true)
    if [ -z "${n:-}" ]; then
        n=$(read_state 2>/dev/null | head -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null || true)
    fi
    [ -n "${n:-}" ] || die "no enabled output found"
    printf '%s\n' "$n"
}

record_state() {
    mkdir -p "$(dirname "$state_file")"
    if [ ! -f "$state_file" ]; then
        read_state "$1" >"$state_file" 2>/dev/null || true
        say "saved pre-change state to $state_file"
    fi
}

kdo() { kscreen-doctor "$@"; }

cmd_status() {
    need kscreen-doctor
    say "== connected outputs"
    kscreen-doctor -o 2>/dev/null | sed -n '1,40p'
    say ""
    say "== panel capability (EDID)"
    if [ -x "$edid_tool" ]; then
        "$edid_tool" 2>/dev/null | sed -n '2,40p'
    else
        say "  ($edid_tool not executable)"
    fi
    say ""
    say "== HDR environment"
    if pgrep -x gamescope >/dev/null 2>&1; then
        say "  gamescope session: running (Gamemode)"
    else
        say "  gamescope session: not running (Plasma owns the connector)"
    fi
    if command -v gamescope >/dev/null 2>&1; then
        say "  gamescope: $(gamescope --version 2>&1 | tail -1 | sed 's/.*gamescope version/gamescope version/')"
    fi
    say "  WSI layer: $([ -f /usr/share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json ] && echo present || echo missing)"
    if [ -f "$state_file" ]; then say "  saved state: $state_file"; fi
}

cmd_hdr() {
    [ $# -eq 1 ] || die "usage: $0 hdr on|off|toggle"
    need kscreen-doctor
    out=$(output_name)
    record_state "$out"
    case $1 in
        on)     kdo "output.$out.hdr.enable" ;;
        off)    kdo "output.$out.hdr.disable" ;;
        toggle) kdo "output.$out.hdrAndWcg.toggle" ;;
        *)      die "usage: $0 hdr on|off|toggle" ;;
    esac
    say "HDR $1 requested on $out"
    read_state "$out"
}

cmd_wcg() {
    [ $# -eq 1 ] || die "usage: $0 wcg on|off"
    need kscreen-doctor
    out=$(output_name)
    record_state "$out"
    case $1 in
        on)  kdo "output.$out.wcg.enable" ;;
        off) kdo "output.$out.wcg.disable" ;;
        *)   die "usage: $0 wcg on|off" ;;
    esac
    say "wide colour gamut $1 requested on $out"
    read_state "$out"
}

cmd_sdr_nits() {
    [ $# -eq 1 ] || die "usage: $0 sdr-nits <100..1000>"
    nits=$1
    case $nits in ''|*[!0-9]*) die "sdr-nits must be an integer 100..1000" ;; esac
    [ "$nits" -ge 100 ] && [ "$nits" -le 1000 ] || die "sdr-nits must be 100..1000"
    need kscreen-doctor
    out=$(output_name)
    record_state "$out"
    kdo "output.$out.sdr-brightness.$nits"
    say "SDR content luminance set to $nits cd/m2 on $out (menu/desktop brightness in HDR)"
}

cmd_icc() {
    [ $# -eq 1 ] || die "usage: $0 icc panel|none"
    need kscreen-doctor
    out=$(output_name)
    case $1 in
        panel)
            mkdir -p "$icc_dir"
            [ -x "$icc_tool" ] || die "missing $icc_tool"
            "$icc_tool" --out "$icc_file" || die "profile generation failed"
            "$icc_tool" --check "$icc_file" >/dev/null || die "generated profile failed validation"
            record_state "$out"
            kdo "output.$out.iccprofile.$icc_file"
            kdo "output.$out.colorProfileSource.ICC"
            say "applied $icc_file to $out (colorProfileSource=ICC)"
            ;;
        none)
            record_state "$out"
            kdo "output.$out.colorProfileSource.sRGB"
            # An empty path clears it; kscreen-doctor has no explicit "unset".
            kdo "output.$out.iccprofile."
            say "cleared the ICC path on $out (KWin is back to sRGB source)"
            ;;
        *) die "usage: $0 icc panel|none" ;;
    esac
    read_state "$out"
}

cmd_profile() {
    [ $# -eq 1 ] || die "usage: $0 profile sRGB|EDID|ICC"
    need kscreen-doctor
    out=$(output_name)
    record_state "$out"
    kdo "output.$out.colorProfileSource.$1"
    say "colorProfileSource=$1 on $out"
    read_state "$out"
}

cmd_reset() {
    need kscreen-doctor
    out=$(output_name)
    say "resetting $out to Plasma defaults (HDR off, WCG off, sRGB source, no ICC)"
    # sdr-brightness only lands while the output is in HDR mode, so restore it
    # before HDR is switched off.
    if [ -f "$state_file" ]; then
        prev_nits=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sdr_nits") or "")' "$state_file" 2>/dev/null || true)
        if [ -n "${prev_nits:-}" ]; then
            kdo "output.$out.sdr-brightness.$prev_nits" >/dev/null 2>&1 || true
            say "restored SDR brightness to $prev_nits cd/m2"
        fi
    fi
    kdo "output.$out.hdr.disable" >/dev/null 2>&1 || true
    kdo "output.$out.wcg.disable" >/dev/null 2>&1 || true
    kdo "output.$out.colorProfileSource.sRGB" >/dev/null 2>&1 || true
    kdo "output.$out.iccprofile." >/dev/null 2>&1 || true
    if [ -f "$state_file" ]; then
        # Restore the software-brightness (backlight) value we recorded.
        prev=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("brightness") or "")' "$state_file" 2>/dev/null || true)
        if [ -n "${prev:-}" ]; then
            pct=$(python3 -c 'import sys; print(int(round(float(sys.argv[1])*100)))' "$prev" 2>/dev/null || true)
            if [ -n "${pct:-}" ] && [ "$pct" -ge 0 ] && [ "$pct" -le 100 ]; then
                kdo "output.$out.brightness.$pct" >/dev/null 2>&1 || true
                say "restored brightness to $pct%"
            fi
        fi
        rm -f "$state_file"
    fi
    say "done - HDR off is the safe state (SDR, sRGB source, no ICC)"
    read_state "$out"
}

cmd_gamemode() {
    cat <<'EOF'
Gamemode (the "Gamemode" Wayland session) runs its own gamescope as DRM master,
so KWin is not involved and kscreen-doctor does not apply there.  On this image
the launcher is /usr/libexec/anatase-gamemode and it starts gamescope WITHOUT
--hdr-enabled, so Gamemode output is SDR today.

Verified facts this is based on (see docs/hw/hdr-vibrance.md):
  * gamescope 3.16.23.3-oxp3 supports --hdr-enabled, --hdr-pq-internal-enable,
    --hdr-sdr-content-nits, --sdr-gamut-wideness, --hdr-itm-enabled.
  * the panel EDID advertises PQ (HDR10) with static metadata type 1,
    ~1107 cd/m2 peak / ~476 cd/m2 maxFALL, BT.2020 RGB colourimetry.
  * the session already exports STEAM_GAMESCOPE_HDR_SUPPORTED=1 and
    ENABLE_GAMESCOPE_WSI=1, and sets GAMESCOPE_PATCHED_EDID_FILE.

To enable HDR in Gamemode (requires root, one-line change, reversible):

  sudoedit /usr/libexec/anatase-gamemode
  # in the gamescope_args=( ... ) array, add:
  #     --hdr-enabled
  #     --hdr-pq-internal-enable
  #     --hdr-sdr-content-nits 200        (tune: matches sdrBrightness in Plasma)

  # rollback:
  sudo cp /usr/libexec/anatase-gamemode /usr/libexec/anatase-gamemode.bak   # before editing
  sudo cp /usr/libexec/anatase-gamemode.bak /usr/libexec/anatase-gamemode   # after

Because /usr/libexec is part of the ostree image, an update overwrites the edit;
keep it as a small rpm-ostree/unit drop-in candidate (see the doc, "open points").

Verify inside the session:
  journalctl --user -b -u anatase-gamemode | grep -iE 'hdr|pq|colorspace'
  gamescopectl                     # prints display flags/features of the live instance

Vibrance in Gamemode: gamescope's --reshade-effect needs a flag in the same
launcher, so the no-root route is vkBasalt via Steam launch options
(tools/display/vibrance.sh launch-env).
EOF
}

[ $# -ge 1 ] || { sed -n '3,20p' "$0"; exit 2; }
sub=$1
shift
case $sub in
    status)   cmd_status ;;
    hdr)      cmd_hdr "$@" ;;
    wcg)      cmd_wcg "$@" ;;
    sdr-nits) cmd_sdr_nits "$@" ;;
    icc)      cmd_icc "$@" ;;
    profile)  cmd_profile "$@" ;;
    reset)    cmd_reset ;;
    gamemode) cmd_gamemode ;;
    *)        die "unknown subcommand: $sub" ;;
esac
