#!/usr/bin/env bash
# tests/fingerprint-session/run.sh - offline end-to-end tests for the runbook.
#
# Everything runs against a fake /sys tree built by
#   tools/fingerprint/oxp3-fp-session.sh --fixture-init DIR
# so there is no root, no real /sys, no spidev and no sensor access. What is
# exercised is the *real* script: its argument handling, its preconditions, its
# state machine, its journal, its trap-driven rollback and its JSON summary.
#
#   tests/fingerprint-session/run.sh        run every scenario
#   tests/fingerprint-session/run.sh -v     print each session's console log
#
# Exit: 0 all passed, 1 at least one failure, 2 setup problem.
set -u

export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

self=$0
here=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 2
REPO=$(CDPATH= cd -- "$here/../.." && pwd) || exit 2
S=$REPO/tools/fingerprint/oxp3-fp-session.sh
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

[ -x "$S" ] || { printf 'tests/fingerprint-session: %s missing or not executable\n' "$S" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { printf 'tests/fingerprint-session: python3 is required\n' >&2; exit 2; }
command -v mkfifo >/dev/null 2>&1 || { printf 'tests/fingerprint-session: mkfifo is required\n' >&2; exit 2; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-fp-session-tests.XXXXXX") || exit 2
trap 'rm -rf -- "${TMP:-}"' EXIT
FAILED=0
COUNT=0

pass() { COUNT=$((COUNT + 1)); printf 'ok   %s\n' "$*"; }
fail() { COUNT=$((COUNT + 1)); FAILED=$((FAILED + 1)); printf 'FAIL %s\n' "$*"; }
check() { local d=$1; shift; if "$@" >/dev/null 2>&1; then pass "$d"; else fail "$d"; fi; }
contains() { grep -Fq -- "$2" "$1" 2>/dev/null; }
show_log() { [ "$VERBOSE" = 1 ] && sed -n '1,120p' "$1"; return 0; }

fixture() { # dir variant
    "$S" --fixture-init "$1" --variant "$2" >/dev/null 2>&1
}

unbound() { [ ! -e "$1/sys/bus/spi/devices/spi-BF63112:00/driver" ]; }

wait_for() { # file pattern timeout-seconds
    local file=$1 pattern=$2 limit=${3:-10} waited=0
    while [ "$waited" -lt "$((limit * 10))" ]; do
        grep -Fq -- "$pattern" "$file" 2>/dev/null && return 0
        sleep 0.1
        waited=$((waited + 1))
    done
    return 1
}

run_stdin() { # dir session-dir stdin-text -> writes $TMP/last.out, rc in $RUN_RC
    printf '%s' "$3" | "$S" --root "$1" --out "$2" --no-irq >"$TMP/last.out" 2>&1
    RUN_RC=$?
    show_log "$TMP/last.out"
}

printf 'oxp3-fp-session offline tests (fake /sys trees, no root, no sensor)\n'
printf -- '------------------------------------------------------------------------\n'

# ---------------------------------------------------------------- 1. CLI surface
"$S" --help >"$TMP/help.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/help.txt" 'interactive session'; then
    pass "--help documents the usage"
else
    fail "--help documents the usage"
fi
if contains "$TMP/help.txt" 'Exit codes: 0 ok, 1 failure, 2 usage/precondition, 3 restore not verified'; then
    pass "--help documents the exit codes"
else
    fail "--help documents the exit codes"
fi
if "$S" --version >"$TMP/version.txt" 2>&1 && contains "$TMP/version.txt" 'oxp3-fp-session '; then
    pass "--version prints the tool and version"
else
    fail "--version prints the tool and version"
fi
"$S" --definitely-not-an-option >"$TMP/badarg.txt" 2>&1
if [ $? -eq 2 ] && contains "$TMP/badarg.txt" 'unknown argument'; then
    pass "an unknown argument is a usage error (exit 2)"
else
    fail "an unknown argument is a usage error (exit 2)"
fi

# ---------------------------------------------------------------- 2. fixtures
fixture "$TMP/clean" clean || fail "fixture clean builds"
if [ -f "$TMP/clean/sys/firmware/acpi/tables/DSDT" ]; then
    pass "the fixture provides a synthetic DSDT"
else
    fail "the fixture provides a synthetic DSDT"
fi
if [ -f "$TMP/clean/sys/bus/spi/devices/spi-BF63112:00/driver_override" ]; then
    pass "the fixture provides driver_override like a real SPI device"
else
    fail "the fixture provides driver_override like a real SPI device"
fi

# ---------------------------------------------------------------- 3. plan / no mutation
before=$(find "$TMP/clean" -printf '%p %s %T@\n' 2>/dev/null | sort | sha256sum)
before_plan=$(ls -1d "$REPO"/docs/state/fp-session-* 2>/dev/null | sort)
if "$S" --dry-run --root "$TMP/clean" >"$TMP/plan.txt" 2>&1; then
    pass "--dry-run exits 0 on a healthy fixture"
else
    fail "--dry-run exits 0 on a healthy fixture"
    show_log "$TMP/plan.txt"
fi
contains "$TMP/plan.txt" 'Plan (every mutation is listed' && pass "the plan lists the mutations" \
    || fail "the plan lists the mutations"
contains "$TMP/plan.txt" 'driver_override' && pass "the plan shows the driver_override command" \
    || fail "the plan shows the driver_override command"
contains "$TMP/plan.txt" '--allow-writes' && pass "the plan shows the gated read command" \
    || fail "the plan shows the gated read command"
"$S" --dry-run --root "$TMP/clean" --irq-pin 18 --reset-pin 16 --gpio-chip gpiochip1 --allow-reset >"$TMP/plan-pins.txt" 2>&1
contains "$TMP/plan-pins.txt" '--gpio 18' && pass "--irq-pin reaches the probe command" \
    || fail "--irq-pin reaches the probe command"
contains "$TMP/plan-pins.txt" '--reset-gpio 16' && pass "--reset-pin reaches the probe command" \
    || fail "--reset-pin reaches the probe command"
contains "$TMP/plan-pins.txt" '--gpio-chip gpiochip1' && pass "--gpio-chip reaches the probe command" \
    || fail "--gpio-chip reaches the probe command"

contains "$TMP/plan.txt" 'Undo (also performed automatically' && pass "the plan prints the undo commands" \
    || fail "the plan prints the undo commands"
after=$(find "$TMP/clean" -printf '%p %s %T@\n' 2>/dev/null | sort | sha256sum)
if [ "$before" = "$after" ] && unbound "$TMP/clean"; then
    pass "--dry-run changed nothing in the fixture tree"
else
    fail "--dry-run changed nothing in the fixture tree"
fi
after_plan=$(ls -1d "$REPO"/docs/state/fp-session-* 2>/dev/null | sort)
if [ "$after_plan" = "$before_plan" ]; then
    pass "--dry-run wrote no transcript into docs/state"
else
    fail "--dry-run wrote a transcript into docs/state"
fi
"$S" --non-interactive --root "$TMP/clean" >"$TMP/plan2.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/plan2.txt" 'Copy/paste sequence' && contains "$TMP/plan2.txt" 'sudo tools/fingerprint/oxp3-fp-session.sh'; then
    pass "--non-interactive prints the copy/paste sequence"
else
    fail "--non-interactive prints the copy/paste sequence"
fi

# ---------------------------------------------------------------- 4. refusals
for variant in no-acpi open-by-pid bound-spidev bound-other; do
    dir=$TMP/refuse-$variant
    fixture "$dir" "$variant"
    "$S" --dry-run --root "$dir" >"$TMP/refuse-$variant.txt" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then pass "the $variant fixture is refused (exit $rc)"; else
        fail "the $variant fixture is refused"; fi
    case $variant in
        no-acpi) contains "$TMP/refuse-$variant.txt" 'ACPI device' \
                     && pass "the refusal names the missing ACPI device" \
                     || fail "the refusal names the missing ACPI device" ;;
        open-by-pid) contains "$TMP/refuse-$variant.txt" 'already open by another process' \
                     && pass "the refusal names the other process" \
                     || fail "the refusal names the other process" ;;
        bound-spidev) contains "$TMP/refuse-$variant.txt" 'ALREADY bound' \
                     && pass "an existing spidev bind needs --allow-existing-bind" \
                     || fail "an existing spidev bind needs --allow-existing-bind" ;;
        bound-other) contains "$TMP/refuse-$variant.txt" 'different driver' \
                     && pass "another driver needs --allow-takeover" \
                     || fail "another driver needs --allow-takeover" ;;
    esac
done

# The explicit opt-ins let the same fixtures proceed (still plan-only).
"$S" --dry-run --root "$TMP/refuse-bound-spidev" --allow-existing-bind >"$TMP/ext.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/ext.txt" 'no bind/unbind is performed'; then
    pass "--allow-existing-bind accepts the pre-existing bind and plans to leave it"
else
    fail "--allow-existing-bind accepts the pre-existing bind and plans to leave it"
fi
"$S" --dry-run --root "$TMP/refuse-bound-other" --allow-takeover >"$TMP/other.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/other.txt" 're-bound on exit'; then
    pass "--allow-takeover accepts the other driver and plans to restore it"
else
    fail "--allow-takeover accepts the other driver and plans to restore it"
fi

# ---------------------------------------------------------------- 5. full session, typed skip
mkdir -p "$TMP/session-skip"
run_stdin "$TMP/clean" "$TMP/session-skip" 'y
y
skip
'
if [ "$RUN_RC" -eq 0 ]; then pass "a typed skip finishes the session (exit 0)"; else
    fail "a typed skip finishes the session (exit $RUN_RC)"; fi
unbound "$TMP/clean" && pass "the previous binding is restored" || fail "the previous binding is restored"
[ "$(cat "$TMP/clean/sys/bus/spi/devices/spi-BF63112:00/driver_override" 2>/dev/null)" = "" ] \
    && pass "driver_override was cleared again" || fail "driver_override was cleared again"
contains "$TMP/session-skip/journal.tsv" 'restore' && pass "the restore is journaled" \
    || fail "the restore is journaled"
contains "$TMP/session-skip/summary.json" '"verified": true' && pass "summary.json says the restore was verified" \
    || fail "summary.json says the restore was verified"
contains "$TMP/session-skip/summary.json" '"unknowns"' && pass "summary.json lists the remaining unknowns" \
    || fail "summary.json lists the remaining unknowns"
contains "$TMP/session-skip/session.log" 'Bind spidev' && pass "the console transcript exists" \
    || fail "the console transcript exists"
if python3 -m json.tool "$TMP/session-skip/summary.json" >/dev/null 2>&1; then
    pass "summary.json is valid JSON"
else
    fail "summary.json is valid JSON"
fi

# ---------------------------------------------------------------- 6. the probe runs and fails offline -> still restores
mkdir -p "$TMP/session-probe"
run_stdin "$TMP/clean" "$TMP/session-probe" 'y
y
probe
'
if [ "$RUN_RC" -eq 1 ]; then pass "a failing probe exits 1 (data, not a crash)"; else
    fail "a failing probe exits 1 (exit $RUN_RC)"; fi
unbound "$TMP/clean" && pass "the error path restored the binding" || fail "the error path restored the binding"
contains "$TMP/session-probe/summary.json" '"verified": true' \
    && pass "the error path summary records a verified restore" \
    || fail "the error path summary records a verified restore"
contains "$TMP/session-probe/commands.txt" '--allow-writes' \
    && pass "the exact read command was recorded for the owner" \
    || fail "the exact read command was recorded for the owner"
contains "$TMP/session-probe/commands.txt" 'fw scheme info' \
    && pass "the allowlisted reads were recorded" || fail "the allowlisted reads were recorded"
if [ -s "$TMP/session-probe/probe-readonly.txt" ]; then
    pass "the probe output was captured into the transcript"
else
    fail "the probe output was captured into the transcript"
fi

# ---------------------------------------------------------------- 7. wrong confirmation word
mkdir -p "$TMP/session-word"
run_stdin "$TMP/clean" "$TMP/session-word" 'y
y
PROBE
'
if [ "$RUN_RC" -eq 1 ] && contains "$TMP/last.out" 'expected exactly "probe"'; then
    pass "anything but the literal word refuses the read"
else
    fail "anything but the literal word refuses the read"
fi
[ ! -e "$TMP/session-word/probe-readonly.txt" ] \
    && pass "a refused read captured no device output" || fail "a refused read captured no device output"

# ---------------------------------------------------------------- 8. interrupt while bound -> trap restores
# A background child of a non-interactive shell inherits SIGINT as SIG_IGN,
# and bash cannot trap a signal that was ignored on entry. Reset the
# disposition with coreutils' --default-signal so this really exercises the
# same trap Ctrl-C uses; fall back to SIGTERM (same handler) if unavailable.
fixture "$TMP/sigint" clean
mkdir -p "$TMP/session-sigint"
mkfifo "$TMP/sigint.in" || exit 1
exec 8<>"$TMP/sigint.in"
SIG=INT
launcher=(env --default-signal=INT)
if ! env --default-signal=INT true >/dev/null 2>&1; then
    SIG=TERM
    launcher=()
    printf 'note  env --default-signal unavailable: exercising the shared handler with SIGTERM\n'
fi
"${launcher[@]}" "$S" --root "$TMP/sigint" --out "$TMP/session-sigint" --no-irq \
    <"$TMP/sigint.in" >"$TMP/sigint.out" 2>&1 &
sig_pid=$!
printf 'y\ny\n' >&8
if wait_for "$TMP/session-sigint/journal.tsv" 'probe' 10 || wait_for "$TMP/sigint.out" 'Type "probe"' 10; then
    pass "the session reached the read prompt"
else
    fail "the session reached the read prompt"
fi
kill -"$SIG" "$sig_pid" 2>/dev/null || true
for _ in $(seq 1 100); do
    kill -0 "$sig_pid" 2>/dev/null || break
    sleep 0.1
done
if kill -0 "$sig_pid" 2>/dev/null; then
    fail "the interrupted session terminated"
    kill -KILL "$sig_pid" 2>/dev/null || true
fi
wait "$sig_pid"
sig_rc=$?
exec 8>&-
if [ "$sig_rc" -eq 130 ]; then pass "the interrupt exits 130"; else fail "the interrupt exits 130 (got $sig_rc)"; fi
unbound "$TMP/sigint" && pass "the interrupt restored the previous binding" || fail "the interrupt restored the previous binding"
contains "$TMP/session-sigint/journal.tsv" 'interrupted' && pass "the interruption is journaled" \
    || fail "the interruption is journaled"
contains "$TMP/session-sigint/summary.json" '"verified": true' \
    && pass "the interrupted session still wrote a verified-restore summary" \
    || fail "the interrupted session still wrote a verified-restore summary"

# ---------------------------------------------------------------- 9. SIGKILL while bound -> --resume finishes it
fixture "$TMP/resume" clean
mkdir -p "$TMP/session-resume"
mkfifo "$TMP/resume.in" || exit 1
exec 9<>"$TMP/resume.in"
"$S" --root "$TMP/resume" --out "$TMP/session-resume" --no-irq <"$TMP/resume.in" \
    >"$TMP/resume1.out" 2>&1 &
res_pid=$!
printf 'y\ny\n' >&9
if wait_for "$TMP/resume1.out" 'Type "probe"' 10; then
    pass "the session reached the read prompt before the kill"
else
    fail "the session reached the read prompt before the kill"
fi
kill -KILL "$res_pid" 2>/dev/null || true
wait "$res_pid" 2>/dev/null || true
exec 9>&-
if [ -e "$TMP/resume/sys/bus/spi/devices/spi-BF63112:00/driver" ]; then
    pass "SIGKILL left the device bound (the state file is why resume works)"
else
    fail "SIGKILL left the device bound"
fi
if grep -q 'BOUND_BY_US=1' "$TMP/session-resume/session.state" 2>/dev/null; then
    pass "the write-ahead state recorded our bind before the probes"
else
    fail "the write-ahead state recorded our bind before the probes"
fi
printf 'y\nskip\n' | "$S" --resume "$TMP/session-resume" >"$TMP/resume2.out" 2>&1
res_rc=$?
show_log "$TMP/resume2.out"
if [ "$res_rc" -eq 0 ]; then pass "--resume completes the interrupted session (exit 0)"; else
    fail "--resume completes the interrupted session (exit $res_rc)"; fi
contains "$TMP/resume2.out" 'resumed' && pass "--resume reports what it reused" || fail "--resume reports what it reused"
unbound "$TMP/resume" && pass "--resume restored the binding it owned" || fail "--resume restored the binding it owned"
contains "$TMP/session-resume/summary.json" '"bound_by_session": true' \
    && pass "--resume kept the ownership record" || fail "--resume kept the ownership record"
if grep -q 'resumed' "$TMP/session-resume/journal.tsv"; then
    pass "the resumed phases are journaled"
else
    fail "the resumed phases are journaled"
fi

# ---------------------------------------------------------------- 10. a foreign bind is never unbound
fixture "$TMP/foreign" bound-spidev
mkdir -p "$TMP/session-foreign"
printf 'y\nskip\n' | "$S" --root "$TMP/foreign" --out "$TMP/session-foreign" \
    --no-irq --allow-existing-bind >"$TMP/foreign.out" 2>&1
foreign_rc=$?
show_log "$TMP/foreign.out"
if [ "$foreign_rc" -eq 0 ]; then pass "a pre-existing spidev bind is usable (exit 0)"; else
    fail "a pre-existing spidev bind is usable (exit $foreign_rc)"; fi
if [ -e "$TMP/foreign/sys/bus/spi/devices/spi-BF63112:00/driver" ]; then
    pass "the pre-existing spidev bind was left alone"
else
    fail "the pre-existing spidev bind was left alone"
fi
contains "$TMP/session-foreign/summary.json" '"external_bind": true' \
    && pass "the summary marks the bind as external" || fail "the summary marks the bind as external"
contains "$TMP/session-foreign/journal.tsv" 'external' \
    && pass "the external binding is journaled" || fail "the external binding is journaled"

# ---------------------------------------------------------------- 11. takeover restores the other driver
fixture "$TMP/takeover" bound-other
mkdir -p "$TMP/session-takeover"
printf 'y\ny\ny\nskip\n' | "$S" --root "$TMP/takeover" --out "$TMP/session-takeover" \
    --no-irq --allow-takeover >"$TMP/takeover.out" 2>&1
takeover_rc=$?
show_log "$TMP/takeover.out"
if [ "$takeover_rc" -eq 0 ]; then pass "a takeover session succeeds (exit 0)"; else
    fail "a takeover session succeeds (exit $takeover_rc)"; fi
link=$(readlink "$TMP/takeover/sys/bus/spi/devices/spi-BF63112:00/driver" 2>/dev/null) || true
if [ "${link##*/}" = elanspi ]; then
    pass "the previous driver was re-bound on exit"
else
    fail "the previous driver was re-bound on exit (driver=${link##*/})"
fi
contains "$TMP/session-takeover/summary.json" '"previous_driver": "elanspi"' \
    && pass "the summary names the replaced driver" || fail "the summary names the replaced driver"

# ---------------------------------------------------------------- 12. invalid reads never reach the device
"$S" --dry-run --root "$TMP/clean" --reads 'fw enrol' >"$TMP/badreads.txt" 2>&1
if [ $? -eq 2 ] && contains "$TMP/badreads.txt" 'allowed: '; then
    pass "an invalid --reads list is a usage error that names the allowlist"
else
    fail "an invalid --reads list is a usage error that names the allowlist"
fi
all_refused=1
for bad in enrol identify jtag update 0xB0; do
    if "$S" --dry-run --root "$TMP/clean" --reads "$bad" >/dev/null 2>&1; then
        all_refused=0
    fi
done
if [ "$all_refused" -eq 1 ]; then
    pass "every destructive name is refused by --reads"
else
    fail "every destructive name is refused by --reads"
fi

# ---------------------------------------------------------------- 13. --try-matrix
# The probe runs for real against the fixture's fake spidev node and a
# nonexistent GPIO chip path, so every cell fails closed; what is asserted is
# that the flag reaches the probe and that the matrix table becomes part of
# the transcript, without any real device or GPIO being opened.
fixture "$TMP/matrix" clean
mkdir -p "$TMP/session-matrix"
printf 'y\ny\nprobe\n' | "$S" --root "$TMP/matrix" --out "$TMP/session-matrix" \
    --try-matrix --irq-pin 18 --reset-pin 16 \
    --gpio-chip "$TMP/matrix/dev/gpiochip1" --allow-reset \
    >"$TMP/matrix.out" 2>&1
matrix_rc=$?
show_log "$TMP/matrix.out"
if [ "$matrix_rc" -eq 1 ]; then
    pass "--try-matrix runs to a matrix verdict (exit 1 = no valid answer)"
else
    fail "--try-matrix runs to a matrix verdict (exit $matrix_rc)"
fi
unbound "$TMP/matrix" && pass "--try-matrix restored the previous binding" \
    || fail "--try-matrix restored the previous binding"
contains "$TMP/session-matrix/commands.txt" '--try-matrix' \
    && pass "the session passes --try-matrix to the probe" \
    || fail "the session passes --try-matrix to the probe"
contains "$TMP/session-matrix/commands.txt" '--matrix-json' \
    && pass "the session passes an explicit --matrix-json path" \
    || fail "the session passes an explicit --matrix-json path"
if [ -s "$TMP/session-matrix/matrix.json" ]; then
    pass "the matrix table lands in the transcript (matrix.json)"
else
    fail "the matrix table lands in the transcript (matrix.json)"
fi
if python3 -m json.tool "$TMP/session-matrix/matrix.json" >/dev/null 2>&1; then
    pass "matrix.json is valid JSON"
else
    fail "matrix.json is valid JSON"
fi
if python3 - "$TMP/session-matrix/matrix.json" <<'PYMATRIX'
import json
import sys

data = json.load(open(sys.argv[1]))
assert data["planned"] == 18, data["planned"]
assert len(data["attempts"]) == 18, len(data["attempts"])
assert data["outcome"] == "no-valid-answer", data["outcome"]
assert data["attempts"][0]["polarity"] == "active-low"
assert data["attempts"][0]["settle_ms"] == 10
assert data["attempts"][0]["edge"] == "rising"
assert "no valid answer" in data["verdict"]
assert "tried polarity" in data["verdict"]
PYMATRIX
then
    pass "matrix.json has 18 attempts and the documented shape"
else
    fail "matrix.json has 18 attempts and the documented shape"
fi
contains "$TMP/session-matrix/probe-readonly.txt" 'polarity' \
    && pass "the compact matrix table is in the probe transcript" \
    || fail "the compact matrix table is in the probe transcript"
contains "$TMP/session-matrix/probe-readonly.txt" 'attempts failed' \
    && pass "the no-valid-answer verdict names the combinations tried" \
    || fail "the no-valid-answer verdict names the combinations tried"
contains "$TMP/session-matrix/summary.json" '"matrix"' \
    && pass "summary.json records the matrix table" \
    || fail "summary.json records the matrix table"
contains "$TMP/session-matrix/summary.json" '"outcome": "no-valid-answer"' \
    && pass "summary.json records the matrix outcome" \
    || fail "summary.json records the matrix outcome"
if python3 -m json.tool "$TMP/session-matrix/summary.json" >/dev/null 2>&1; then
    pass "summary.json stays valid JSON with the matrix embedded"
else
    fail "summary.json stays valid JSON with the matrix embedded"
fi

# ---------------------------------------------------------------- 14. --try-write-shape
# Same shape as the matrix scenario: the probe runs against the fixture's fake
# spidev node and a nonexistent GPIO chip, so every cell fails closed; what is
# asserted is that the pinned electrical flags reach the probe and that the
# write-shape table becomes part of the transcript.
fixture "$TMP/shape" clean
mkdir -p "$TMP/session-shape"
printf 'y\ny\nprobe\n' | "$S" --root "$TMP/shape" --out "$TMP/session-shape" \
    --try-write-shape --irq-pin 18 --reset-pin 16 \
    --gpio-chip "$TMP/shape/dev/gpiochip1" --allow-reset \
    --irq-edge rising --reset-polarity active-high --settle-ms 10 \
    >"$TMP/shape.out" 2>&1
shape_rc=$?
show_log "$TMP/shape.out"
if [ "$shape_rc" -eq 1 ]; then
    pass "--try-write-shape runs to a verdict (exit 1 = no valid shape)"
else
    fail "--try-write-shape runs to a verdict (exit $shape_rc)"
fi
unbound "$TMP/shape" && pass "--try-write-shape restored the previous binding" \
    || fail "--try-write-shape restored the previous binding"
contains "$TMP/session-shape/commands.txt" '--try-write-shape' \
    && pass "the session passes --try-write-shape to the probe" \
    || fail "the session passes --try-write-shape to the probe"
contains "$TMP/session-shape/commands.txt" '--write-shape-json' \
    && pass "the session passes an explicit --write-shape-json path" \
    || fail "the session passes an explicit --write-shape-json path"
contains "$TMP/session-shape/commands.txt" '--reset-polarity active-high' \
    && pass "the settled reset polarity reaches the probe" \
    || fail "the settled reset polarity reaches the probe"
contains "$TMP/session-shape/commands.txt" '--settle-ms 10' \
    && pass "the settled delay reaches the probe" \
    || fail "the settled delay reaches the probe"
contains "$TMP/session-shape/commands.txt" '--irq-edge rising' \
    && pass "the IRQ edge reaches the probe" \
    || fail "the IRQ edge reaches the probe"
if [ -s "$TMP/session-shape/write-shape.json" ]; then
    pass "the write-shape table lands in the transcript (write-shape.json)"
else
    fail "the write-shape table lands in the transcript (write-shape.json)"
fi
if python3 - "$TMP/session-shape/write-shape.json" <<'PYSHAPE'
import json
import sys

data = json.load(open(sys.argv[1]))
assert data["planned"] == 60, data["planned"]
assert len(data["cells"]) == 60, len(data["cells"])
assert data["outcome"] == "no-valid-answer", data["outcome"]
first = data["cells"][0]
assert first["command"] == "fw", first["command"]
assert first["selector"] == "0x40", first["selector"]
assert first["padding"] == "0"
assert first["read_len"] == 7, first["read_len"]
assert "write-shape cells failed" in data["verdict"], data["verdict"]
assert data["shift_summary"], "shift_summary missing"
PYSHAPE
then
    pass "write-shape.json has 60 cells and the documented shape"
else
    fail "write-shape.json has 60 cells and the documented shape"
fi
contains "$TMP/session-shape/probe-readonly.txt" 'write-shape: 60 cells' \
    && pass "the compact write-shape table is in the probe transcript" \
    || fail "the compact write-shape table is in the probe transcript"
contains "$TMP/session-shape/probe-readonly.txt" 'cand' \
    && pass "the compact table reports the payload candidate offset" \
    || fail "the compact table reports the payload candidate offset"
contains "$TMP/session-shape/summary.json" '"write_shape"' \
    && pass "summary.json records the write-shape table" \
    || fail "summary.json records the write-shape table"
contains "$TMP/session-shape/summary.json" '"outcome": "no-valid-answer"' \
    && pass "summary.json records the write-shape outcome" \
    || fail "summary.json records the write-shape outcome"
contains "$TMP/session-shape/summary.json" '"write-shape:0xA0/0xA1/0xC1"' \
    && pass "summary.json names the write-shape read set" \
    || fail "summary.json names the write-shape read set"
if python3 -m json.tool "$TMP/session-shape/summary.json" >/dev/null 2>&1; then
    pass "summary.json stays valid JSON with write_shape embedded"
else
    fail "summary.json stays valid JSON with write_shape embedded"
fi
# A dry run with different pinned settings must show them without touching the
# device or writing a table.
"$S" --dry-run --root "$TMP/shape" --try-write-shape --irq-pin 18 \
    --reset-pin 16 --gpio-chip gpiochip1 --allow-reset \
    --irq-edge falling --reset-polarity active-low --settle-ms 25 \
    >"$TMP/shape-plan.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/shape-plan.txt" '--reset-polarity active-low --settle-ms 25' \
   && contains "$TMP/shape-plan.txt" '--try-write-shape' \
   && contains "$TMP/shape-plan.txt" '60 cells (selector bit0'; then
    pass "a write-shape dry run shows the pinned settings and all cells"
else
    fail "a write-shape dry run shows the pinned settings and all cells"
fi
if [ -e "$TMP/shape/write-shape.json" ]; then
    fail "a write-shape dry run wrote no table"
else
    pass "a write-shape dry run wrote no table"
fi

# ---------------------------------------------------------------- 15. --try-link
# The link sweep against the fixture's fake spidev node and a nonexistent GPIO
# chip: every cell fails closed, but the flag must reach the probe and the
# 16-cell table must land in link.json and in summary.json.
fixture "$TMP/link" clean
mkdir -p "$TMP/session-link"
printf 'y\ny\nprobe\n' | "$S" --root "$TMP/link" --out "$TMP/session-link" \
    --try-link --irq-pin 18 --reset-pin 16 \
    --gpio-chip "$TMP/link/dev/gpiochip1" --allow-reset \
    --link-settles 10,150 \
    >"$TMP/link.out" 2>&1
link_rc=$?
show_log "$TMP/link.out"
if [ "$link_rc" -eq 1 ]; then
    pass "--try-link runs to a link verdict (exit 1 = no plausible answer)"
else
    fail "--try-link runs to a link verdict (exit $link_rc)"
fi
unbound "$TMP/link" && pass "--try-link restored the previous binding" \
    || fail "--try-link restored the previous binding"
contains "$TMP/session-link/commands.txt" '--try-link' \
    && pass "the session passes --try-link to the probe" \
    || fail "the session passes --try-link to the probe"
contains "$TMP/session-link/commands.txt" '--link-json' \
    && pass "the session passes an explicit --link-json path" \
    || fail "the session passes an explicit --link-json path"
contains "$TMP/session-link/commands.txt" '--link-settles 10,150' \
    && pass "the settle list reaches the probe" \
    || fail "the settle list reaches the probe"
if [ -s "$TMP/session-link/link.json" ]; then
    pass "the link table lands in the transcript (link.json)"
else
    fail "the link table lands in the transcript (link.json)"
fi
if python3 - "$TMP/session-link/link.json" <<'PYLINK'
import json
import sys

data = json.load(open(sys.argv[1]))
assert data["planned"] == 16, data["planned"]
assert len(data["cells"]) == 16, len(data["cells"])
assert data["outcome"] == "no-plausible-answer", data["outcome"]
first = data["cells"][0]
assert first["mode"] == 0, first["mode"]
assert first["speed_hz"] == 1000000, first["speed_hz"]
assert first["settle_ms"] == 10, first["settle_ms"]
assert first["verdict"] == "error", first["verdict"]
assert data["cells"][3]["mode"] == 3, data["cells"][3]["mode"]
assert data["cells"][4]["speed_hz"] == 4000000, data["cells"][4]["speed_hz"]
assert data["cells"][8]["settle_ms"] == 150, data["cells"][8]["settle_ms"]
assert "no protocol-plausible answer" in data["verdict"], data["verdict"]
PYLINK
then
    pass "link.json has 16 cells and the documented shape"
else
    fail "link.json has 16 cells and the documented shape"
fi
contains "$TMP/session-link/probe-readonly.txt" 'mode speed' \
    && pass "the compact link table is in the probe transcript" \
    || fail "the compact link table is in the probe transcript"
contains "$TMP/session-link/probe-readonly.txt" '0xA1 (first 16B)' \
    && pass "the compact table shows the 0xA1 window" \
    || fail "the compact table shows the 0xA1 window"
contains "$TMP/session-link/summary.json" '"link"' \
    && pass "summary.json records the link table" \
    || fail "summary.json records the link table"
contains "$TMP/session-link/summary.json" '"link:0xA0/0xC1/0xA1"' \
    && pass "summary.json names the link read set" \
    || fail "summary.json names the link read set"
if python3 -m json.tool "$TMP/session-link/summary.json" >/dev/null 2>&1; then
    pass "summary.json stays valid JSON with link embedded"
else
    fail "summary.json stays valid JSON with link embedded"
fi
# A dry run must print the whole grid without opening a device or writing a table.
"$S" --dry-run --root "$TMP/link" --try-link --irq-pin 18 --reset-pin 16 \
    --gpio-chip gpiochip1 --allow-reset --link-settles 10,150,300 \
    >"$TMP/link-plan.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/link-plan.txt" '24 cells' \
   && contains "$TMP/link-plan.txt" 'settle 10,150,300 ms' \
   && contains "$TMP/link-plan.txt" '--try-link'; then
    pass "a link dry run shows the 24-cell grid with the optional 300 ms settle"
else
    fail "a link dry run shows the 24-cell grid with the optional 300 ms settle"
fi
if [ -e "$TMP/link/link.json" ]; then
    fail "a link dry run wrote no table"
else
    pass "a link dry run wrote no table"
fi

# ------------------------------------------------------- 16. --compare-read-shapes
# The pinned split-vs-cs-held comparison against the fixture's fake spidev node
# and a nonexistent GPIO chip: every row fails closed, but the flag must reach
# the probe, the 5-row table must land in read-shapes.json and summary.json,
# and the compact table must be in the probe transcript.
fixture "$TMP/compare" clean
mkdir -p "$TMP/session-compare"
printf 'y\ny\nprobe\n' | "$S" --root "$TMP/compare" --out "$TMP/session-compare" \
    --compare-read-shapes --irq-pin 18 --reset-pin 16 \
    --gpio-chip "$TMP/compare/dev/gpiochip1" --allow-reset \
    >"$TMP/compare.out" 2>&1
compare_rc=$?
show_log "$TMP/compare.out"
if [ "$compare_rc" -eq 1 ]; then
    pass "--compare-read-shapes runs to a verdict (exit 1 = no plausible answer)"
else
    fail "--compare-read-shapes runs to a verdict (exit $compare_rc)"
fi
unbound "$TMP/compare" && pass "--compare-read-shapes restored the previous binding" \
    || fail "--compare-read-shapes restored the previous binding"
contains "$TMP/session-compare/commands.txt" '--compare-read-shapes' \
    && pass "the session passes --compare-read-shapes to the probe" \
    || fail "the session passes --compare-read-shapes to the probe"
contains "$TMP/session-compare/commands.txt" '--read-shapes-json' \
    && pass "the session passes an explicit --read-shapes-json path" \
    || fail "the session passes an explicit --read-shapes-json path"
if [ -s "$TMP/session-compare/read-shapes.json" ]; then
    pass "the read-shape table lands in the transcript (read-shapes.json)"
else
    fail "the read-shape table lands in the transcript (read-shapes.json)"
fi
if python3 - "$TMP/session-compare/read-shapes.json" <<'PYSHAPES'
import json
import sys

data = json.load(open(sys.argv[1]))
assert data["planned"] == 4, data["planned"]
assert data["attempted"] == 5, data["attempted"]
assert data["outcome"] == "no-answer", data["outcome"]
rows = data["rows"]
assert [row["shape"] for row in rows] == [
    "split", "cs-held", "cs-held", "cs-held", "cs-held"], rows
assert [row["mode"] for row in rows[:2]] == [0, 0], rows
assert [row["mode"] for row in rows[2:]] == [1, 2, 3], rows
assert len(data["comparisons"]) == 3, data["comparisons"]
assert data["comparisons"][0]["agree"] is None, data["comparisons"][0]
PYSHAPES
then
    pass "read-shapes.json has 4 cells / 5 rows and the documented shape"
else
    fail "read-shapes.json has 4 cells / 5 rows and the documented shape"
fi
contains "$TMP/session-compare/probe-readonly.txt" 'compare-read-shapes:' \
    && pass "the compact read-shape table is in the probe transcript" \
    || fail "the compact read-shape table is in the probe transcript"
contains "$TMP/session-compare/probe-readonly.txt" 'verdict:' \
    && pass "the read-shape table carries a verdict line" \
    || fail "the read-shape table carries a verdict line"
contains "$TMP/session-compare/summary.json" '"read_shapes"' \
    && pass "summary.json records the read-shape table" \
    || fail "summary.json records the read-shape table"
contains "$TMP/session-compare/summary.json" '"read-shapes:0xA0/0xC1/0xA1"' \
    && pass "summary.json names the read-shape query set" \
    || fail "summary.json names the read-shape query set"
if python3 -m json.tool "$TMP/session-compare/summary.json" >/dev/null 2>&1; then
    pass "summary.json stays valid JSON with read_shapes embedded"
else
    fail "summary.json stays valid JSON with read_shapes embedded"
fi
# A dry run must print the plan without opening a device or writing a table.
"$S" --dry-run --root "$TMP/compare" --compare-read-shapes \
    --irq-pin 18 --reset-pin 16 --gpio-chip gpiochip1 --allow-reset \
    >"$TMP/compare-plan.txt" 2>&1
if [ $? -eq 0 ] && contains "$TMP/compare-plan.txt" '--compare-read-shapes' \
   && contains "$TMP/compare-plan.txt" 'mode 0 split+cs-held' \
   && contains "$TMP/compare-plan.txt" 'read-shapes.json'; then
    pass "a compare-read-shapes dry run shows the pinned plan"
else
    fail "a compare-read-shapes dry run shows the pinned plan"
fi
if [ -e "$TMP/compare/read-shapes.json" ]; then
    fail "a compare-read-shapes dry run wrote no table"
else
    pass "a compare-read-shapes dry run wrote no table"
fi

printf -- '------------------------------------------------------------------------\n'
if [ "$FAILED" -eq 0 ]; then
    printf 'fingerprint-session: %d checks passed\n' "$COUNT"
    exit 0
fi
printf 'fingerprint-session: %d of %d checks FAILED\n' "$FAILED" "$COUNT"
exit 1
