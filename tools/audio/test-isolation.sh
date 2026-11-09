#!/usr/bin/env bash
# test-isolation.sh - prove the Harman EQ filter chain in a real, isolated
# PipeWire graph without touching the live audio session.
#
# What it does
#   1. mktemp -d (prefix "audiotest.") with a private XDG_RUNTIME_DIR /
#      PIPEWIRE_RUNTIME_DIR, a private PipeWire config that loads only
#      module-null-sink (support.null-audio-sink), module-filter-chain and the
#      plumbing pw-* clients need.  No ALSA/SPA device probing: no api.alsa.*
#      object is ever created.
#   2. starts a private WirePlumber with every hardware monitor disabled
#      (profile override) and, when /dev/snd exists, inside bubblewrap with an
#      empty /dev/snd, so it physically cannot grab the real card.  It is only
#      there to link nodes by name, which is what a real session does.
#   3. loads the repository filter-chain config unchanged (only the
#      playback target is rewritten to the private null sink), asserts the
#      daemon parsed it and the nodes appeared, then runs
#      measure-in-isolation.py for the log-sweep transfer function.
#   4. restarts the private daemon with test-xover.conf and measures the
#      summed LR4 crossover.
#   5. tears everything down (kill daemon + WirePlumber, remove the runtime
#      dir) unless --keep-runtime is given.
#
# Usage
#   tools/audio/test-isolation.sh                  # full check, JSON on stdout
#   tools/audio/test-isolation.sh --quick          # short sweep, no hot tones
#   tools/audio/test-isolation.sh --keep-runtime   # leave the tree for poking
#   tools/audio/test-isolation.sh --json /tmp/x.json
#
# Exit: 0 pass, 1 fail, 2 usage, 77 skipped (PipeWire tooling missing).
# Requires: pipewire, pw-cli, pw-dump, pw-play, pw-record, pw-link, python3,
#           timeout, mktemp.  WirePlumber + bwrap are required on a host that
#           has /dev/snd (they are how nodes get linked without a live
#           session); in a container /dev/snd does not exist, so plain
#           WirePlumber is used.
set -u

PROG=oxp3-audio-isolation
UNIQUE=audiotest
QUICK=0
KEEP=0
JSON=""

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case $1 in
        --quick) QUICK=1 ;;
        --keep-runtime) KEEP=1 ;;
        --json) shift; [ "$#" -gt 0 ] || { echo "$PROG: --json needs a path" >&2; exit 2; }; JSON=$1 ;;
        --json=*) JSON=${1#*=} ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
skip() { printf 'SKIP: %s\n' "$*"; exit 77; }

here=$(cd "$(dirname "$0")" && pwd) || exit 2
repo=$(cd "$here/../.." && pwd) || exit 2

# ------------------------------------------------------------------ tool gate
missing=""
for t in pipewire pw-cli pw-dump pw-play pw-record pw-link python3 timeout mktemp; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
[ -z "$missing" ] || skip "missing PipeWire tooling:$missing"
python3 -c 'import wave, struct, math' 2>/dev/null || skip "python3 stdlib incomplete"

HAVE_BWRAP=0
command -v bwrap >/dev/null 2>&1 && HAVE_BWRAP=1
command -v wireplumber >/dev/null 2>&1 || skip "wireplumber is not installed (needed to link nodes without a session manager)"
if [ -d /dev/snd ] && [ "$HAVE_BWRAP" -eq 0 ]; then
    skip "host has /dev/snd but no bubblewrap; refusing to start a second session manager without a hardware sandbox"
fi

# ------------------------------------------------------------------ private tree
base=$(mktemp -d "${TMPDIR:-/tmp}/${UNIQUE}.XXXXXX") || exit 1
chmod 700 "$base"
rt=$base/runtime
confA=$base/config-eq
confB=$base/config-xover
wpconf=$base/wp
data=$base/data
state=$base/state
work=$base/work
logs=$base/logs
mkdir -p "$rt" "$confA/pipewire.conf.d" "$confB/pipewire.conf.d" \
         "$wpconf/wireplumber.conf.d" "$data" "$state" "$work" "$logs"
chmod 700 "$rt" "$base"

pids=()
cleanup() {
    local i
    for ((i=${#pids[@]}-1; i>=0; i--)); do
        kill "${pids[$i]}" 2>/dev/null || true
    done
    sleep 0.3
    for ((i=${#pids[@]}-1; i>=0; i--)); do
        kill -9 "${pids[$i]}" 2>/dev/null || true
    done
    if [ "$KEEP" -eq 1 ]; then
        say "kept private tree: $base"
    else
        rm -rf -- "$base"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ------------------------------------------------------------------ configs
# Minimal daemon config: no context.exec, no ALSA, no devices.  module-access
# is required or native clients stall in the initial registry handshake;
# module-link-factory is what pw-link uses.
write_pipewire_conf() {
    cat > "$1/pipewire.conf" <<'EOF'
context.properties = {
    core.daemon = true
    core.name = pipewire-0
    default.clock.rate = 48000
    default.clock.quantum = 1024
    default.clock.min-quantum = 32
    default.clock.max-quantum = 8192
}
context.spa-libs = {
    support.* = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-spa-node-factory }
    { name = libpipewire-module-link-factory }
    { name = libpipewire-module-metadata }
    { name = libpipewire-module-access }
    { name = libpipewire-module-rt flags = [ ifexists nofail ] }
]
context.objects = [
    # A virtual clock.  Nothing here opens an ALSA device.
    { factory = spa-node-factory
      args = { factory.name = support.node.driver node.name = Dummy-Driver
               priority.driver = 20000 } }
    # The only sink in the graph: a SPA null sink (module-null-sink is not
    # shipped on this PipeWire build; support.null-audio-sink is equivalent).
    { factory = adapter
      args = { factory.name = support.null-audio-sink node.name = iso_null
               object.linger = true media.class = Audio/Sink
               audio.channels = 2 audio.position = [ FL FR ] } }
]
EOF
}

# WirePlumber: keep only what links nodes; every hardware monitor is off.
write_wp_conf() {
    cp /usr/share/wireplumber/wireplumber.conf "$wpconf/wireplumber.conf" \
        || fail "cannot read /usr/share/wireplumber/wireplumber.conf"
    cat > "$wpconf/wireplumber.conf.d/90-audiotest-isolation.conf" <<'EOF'
# Private test instance: no hardware is allowed to be probed.  This is the
# first line of defence; on a host with /dev/snd the instance also runs under
# bubblewrap with an empty /dev/snd.
wireplumber.profiles = {
  main = {
    hardware.audio = disabled
    hardware.bluetooth = disabled
    hardware.video-capture = disabled
  }
}
wireplumber.settings = {
  linking.allow-moving-streams = false
}
EOF
}

# Copy the shipped chain, but point its playback stream at the private null
# sink instead of the ALSA speaker sink (install-user.sh does the same thing
# for the real sink).  This is the only edit the test makes to the config.
write_eq_conf() {
    sed -e 's|^\( *node.target *= *\).*|\1"iso_null"|' \
        "$here/oxp3-speaker-eq.conf" > "$confA/pipewire.conf.d/51-oxp3-speaker-eq.conf" \
        || fail "cannot stage oxp3-speaker-eq.conf"
}

# ------------------------------------------------------------------ lifecycle
client_env() {
    env -u PIPEWIRE_REMOTE -u PIPEWIRE_CONFIG_DIR \
        XDG_RUNTIME_DIR="$rt" PIPEWIRE_RUNTIME_DIR="$rt" \
        OXP3_AUDIO_TEST_RUNTIME="$rt" OXP3_AUDIO_TEST_LOGS="$logs" "$@"
}

daemon_log=
wp_log=
phase=
start_stack() {
    phase=$1
    local cf=$2
    daemon_log=$logs/$phase-daemon.log
    wp_log=$logs/$phase-wireplumber.log
    # A previous phase may have left the socket behind.
    rm -f "$rt"/pipewire-0 "$rt"/pipewire-0-manager "$rt"/pipewire-0.lock \
          "$rt"/pipewire-0-manager.lock 2>/dev/null

    # Log level 3 (info) is required for the module/config lines we assert on
    # and for the filter-graph "using port" lines quoted in the docs.
    PIPEWIRE_DEBUG=3 PIPEWIRE_CONFIG_DIR="$cf" XDG_RUNTIME_DIR="$rt" \
        PIPEWIRE_RUNTIME_DIR="$rt" \
        pipewire -c pipewire.conf > "$daemon_log" 2>&1 &
    local pw_pid=$!
    pids+=("$pw_pid")
    for _ in $(seq 1 100); do
        [ -S "$rt/pipewire-0" ] && break
        kill -0 "$pw_pid" 2>/dev/null || fail "private pipewire exited during startup (see $daemon_log)"
        sleep 0.05
    done
    [ -S "$rt/pipewire-0" ] || fail "private pipewire did not create its socket"
    kill -0 "$pw_pid" 2>/dev/null || fail "private pipewire died (see $daemon_log)"

    # WirePlumber, hardware-free.
    local wp_cmd=(wireplumber)
    if [ -d /dev/snd ] && [ "$HAVE_BWRAP" -eq 1 ]; then
        wp_cmd=(bwrap --dev-bind / / --tmpfs /dev/snd wireplumber)
    fi
    WIREPLUMBER_CONFIG_DIR="$wpconf" XDG_RUNTIME_DIR="$rt" \
        PIPEWIRE_RUNTIME_DIR="$rt" XDG_DATA_HOME="$data" XDG_STATE_HOME="$state" \
        "${wp_cmd[@]}" > "$wp_log" 2>&1 &
    local wp_pid=$!
    pids+=("$wp_pid")
    sleep 0.6
    kill -0 "$wp_pid" 2>/dev/null || fail "private wireplumber exited (see $wp_log)"
    # Guarantee no ALSA node was opened by the private session manager.
    if ls -l "/proc/$wp_pid/fd" 2>/dev/null | grep -q '/dev/snd'; then
        fail "private wireplumber has /dev/snd open - refusing to continue"
    fi
    echo "$wp_pid" > "$base/$phase-wp.pid"
    echo "$pw_pid" > "$base/$phase-pw.pid"
}

stop_stack() {
    local wp_pid pw_pid
    wp_pid=$(cat "$base/$phase-wp.pid" 2>/dev/null || true)
    pw_pid=$(cat "$base/$phase-pw.pid" 2>/dev/null || true)
    for p in "${wp_pid:-}" "${pw_pid:-}"; do
        [ -n "$p" ] && { kill "$p" 2>/dev/null || true; }
    done
    for _ in $(seq 1 40); do
        if [ -n "${wp_pid:-}" ] && kill -0 "$wp_pid" 2>/dev/null; then sleep 0.05; else break; fi
    done
    for p in "${wp_pid:-}" "${pw_pid:-}"; do
        [ -n "$p" ] && { kill -9 "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; }
    done
    # Drop the dead pids from the global cleanup list.
    local keep=()
    for p in "${pids[@]}"; do
        [ "$p" = "${wp_pid:-x}" ] && continue
        [ "$p" = "${pw_pid:-x}" ] && continue
        keep+=("$p")
    done
    pids=("${keep[@]}")
    sleep 0.2
}

# Node presence / isolation assertions on pw-dump.
assert_graph() {
    local expected_in=$1 expected_out=$2 dump=$logs/$phase-pw-dump.json
    client_env timeout 10 pw-dump > "$dump" 2> "$logs/$phase-pw-dump.err" \
        || fail "pw-dump failed against the private daemon (see $dump.err)"
    python3 - "$dump" "$expected_in" "$expected_out" <<'PY' || exit 1
import json, sys
dump, want_in, want_out = sys.argv[1], sys.argv[2], sys.argv[3]
objects = json.load(open(dump))
nodes, bad = {}, []
for obj in objects:
    props = obj.get("info", {}).get("props", {}) or {}
    name = props.get("node.name")
    if obj["type"] == "PipeWire:Interface:Node" and name:
        nodes[name] = props
    text = json.dumps(props).lower()
    if "alsa" in text:
        bad.append("%s: %s" % (obj["type"], name or props.get("device.name")))
if want_in not in nodes:
    print("FAIL: filter-chain capture node %r missing (nodes: %s)"
          % (want_in, ", ".join(sorted(nodes))), file=sys.stderr)
    sys.exit(1)
if want_out not in nodes:
    print("FAIL: filter-chain playback node %r missing (nodes: %s)"
          % (want_out, ", ".join(sorted(nodes))), file=sys.stderr)
    sys.exit(1)
if bad:
    print("FAIL: hardware/ALSA object leaked into the isolated graph: %s"
          % "; ".join(bad), file=sys.stderr)
    sys.exit(1)
print("graph: %d nodes, filter-chain %s + %s present, no ALSA objects"
      % (len(nodes), want_in, want_out))
PY
}

assert_parsed() {
    # Config-parse proof: the daemon log must show the module loaded and no
    # config/module error.  (The filter-chain's own graph descriptors are
    # logged by spa.filter-graph at level 3; PIPEWIRE_LOG_LEVEL is not set, so
    # keep this to the module/parse lines that are always logged.)
    grep -q 'loaded module libpipewire-module-filter-chain' "$daemon_log" \
        || fail "filter-chain module was not loaded (see $daemon_log)"
    # Deliberately narrow: a missing session D-Bus produces "No such file or
    # directory" from module-rt on a bare host/container and is not a parse
    # error.  Only config/module/graph failures count.
    local bad
    bad=$(grep -iE 'error loading config|cannot create label|can.t create label|failed to load module|could not load module|failed to create (node|graph|context)|invalid ports' "$daemon_log" | head -n 3)
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" >&2
        fail "daemon reported a config/parse error (see $daemon_log)"
    fi
    say "config: libpipewire-module-filter-chain loaded, no parse errors"
}

run_phase_eq() {
    say "== phase eq: shipped filter chain against a null sink"
    start_stack eq "$confA"
    assert_parsed
    assert_graph oxp3_speaker_eq.sink oxp3_speaker_eq.out
    local q=()
    [ "$QUICK" -eq 1 ] && q+=(--quick)
    client_env python3 "$here/measure-in-isolation.py" --mode eq \
        --node oxp3_speaker_eq.sink --work "$work/eq" \
        --json "$logs/eq-result.json" "${q[@]}" \
        > "$logs/eq-stdout.json" 2> "$logs/eq-stderr.log" \
        || { cat "$logs/eq-stderr.log" >&2; cat "$logs/eq-stdout.json" >&2; fail "EQ measurement failed"; }
    cp "$logs/eq-result.json" "$logs/eq-result.kept.json" 2>/dev/null || true
    stop_stack
}

run_phase_xover() {
    say "== phase crossover: LR4 10.12 kHz, low+high summed"
    start_stack xover "$confB"
    assert_parsed
    assert_graph audiotest_xover.in audiotest_xover.out
    local q=()
    [ "$QUICK" -eq 1 ] && q+=(--quick)
    client_env python3 "$here/measure-in-isolation.py" --mode crossover \
        --node audiotest_xover.in --work "$work/xover" \
        --json "$logs/xover-result.json" "${q[@]}" \
        > "$logs/xover-stdout.json" 2> "$logs/xover-stderr.log" \
        || { cat "$logs/xover-stderr.log" >&2; cat "$logs/xover-stdout.json" >&2; fail "crossover measurement failed"; }
    stop_stack
}

# ------------------------------------------------------------------ report
report() {
    python3 - "$logs" "$JSON" <<'PY'
import json, os, sys
logs, out_path = sys.argv[1], sys.argv[2]
eq = json.load(open(os.path.join(logs, "eq-result.json")))
xo = json.load(open(os.path.join(logs, "xover-result.json")))
print()
print("== measured EQ vs model (1/3 octave, real isolated graph)")
print("%10s %10s %10s %8s" % ("Hz", "measured", "model", "diff"))
for row in eq["bands"]:
    print("%10.0f %+9.2f %+9.2f %+8.2f" % (row["freq_hz"], row["measured_db"],
                                           row["model_db"], row["diff_db"]))
s = eq["summary"]
print("bands %(bands)d   mean|err| %(mean_abs_db).3f dB   max|err| %(max_abs_db).3f dB @ %(max_at_hz).0f Hz" % s)
if "limiter" in eq:
    print()
    print("== hot 0 dBFS tones through the shipped chain (static headroom, no dynamics)")
    for key in ("1khz", "142hz"):
        h = eq["limiter"][key]
        print("%-6s 0 dBFS in -> RMS %+.2f dBFS, peak %+.2f dBFS "
              "(model %+.2f, peak diff %+.2f), clipped %d, tail %+.1f dBFS"
              % (key, h["steady_out_dbfs"], h["peak_dbfs"], h["model_dbfs"],
                 h["peak_minus_model_db"], h["clipped_samples"], h["tail_dbfs"]))
    v = eq["limiter"]["vendor_limiter_model"]
    print("vendor limiter model (offline): %.2f dB attenuation, residual gain reduction %.2f dB after release"
          % (v["attenuation_db"], v["residual_gain_reduction_db"]))
print()
print("== crossover (LR4 10.12 kHz): each band -6 dB at the corner, sum flat")
print("%10s %10s %10s %10s" % ("Hz", "low dB", "high dB", "sum dB"))
for label in ("100hz", "1khz", "corner", "16khz"):
    b = xo["branches"][label]
    print("%10.0f %+9.2f %+9.2f %+9.2f"
          % (b["freq_hz"], b["low_db"], b["high_db"], b["sum_db"]))
print("sum flatness over 100 Hz..16 kHz: max deviation %+.2f dB" % xo["max_deviation_db"])
print("checks: %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(xo["checks"].items())))
passed = bool(eq.get("pass")) and bool(xo.get("pass"))
print()
print("RESULT: %s" % ("PASS" if passed else "FAIL"))
if out_path:
    json.dump({"eq": eq, "crossover": xo, "pass": passed},
              open(out_path, "w"), indent=2, sort_keys=True)
    open(out_path, "a").write("\n")
sys.exit(0 if passed else 1)
PY
}

# ------------------------------------------------------------------ main
write_pipewire_conf "$confA"
write_pipewire_conf "$confB"
write_wp_conf
write_eq_conf
cp "$here/test-xover.conf" "$confB/pipewire.conf.d/52-audiotest-xover.conf" \
    || fail "cannot stage test-xover.conf"

say "isolation: runtime=$rt  config=$confA,$confB  wireplumber=$wpconf  (unique=$UNIQUE)"
[ "$HAVE_BWRAP" -eq 1 ] && say "isolation: wireplumber runs under bubblewrap with an empty /dev/snd"

run_phase_eq
run_phase_xover

say "logs: $logs"
report
