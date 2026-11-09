#!/usr/bin/env bash
# tests/wizard/run.sh - offline end-to-end tests for tools/oxp3-wizard.sh.
#
# Everything runs in --simulate mode with a private state dir: no device access,
# no root, no write inside the checkout. Scripted stdin drives the real prompt
# and state machine; the produced JSON/MD/journal are asserted with python3.
#
#   tests/wizard/run.sh            run every test
#   tests/wizard/run.sh -v         show each wizard invocation
#
# Exit: 0 all passed, 1 at least one failure, 2 setup problem.
set -u

export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

self=$0
here=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 2
REPO=$(CDPATH= cd -- "$here/../.." && pwd) || exit 2
W=$REPO/tools/oxp3-wizard.sh
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

[ -x "$W" ] || { printf 'tests/wizard: %s is missing or not executable\n' "$W" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { printf 'tests/wizard: python3 is required\n' >&2; exit 2; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-wizard-tests.XXXXXX") || exit 2
trap 'rm -rf -- "$TMP"' EXIT
FAILED=0
COUNT=0

pass() { COUNT=$((COUNT + 1)); printf 'ok   %s\n' "$*"; }
fail() { COUNT=$((COUNT + 1)); FAILED=$((FAILED + 1)); printf 'FAIL %s\n' "$*"; }
check() { # description command...
    local desc=$1; shift
    if "$@"; then pass "$desc"; else fail "$desc"; fi
}
contains() { # file needle
    grep -Fq -- "$2" "$1"
}
not_contains() { ! grep -Fq -- "$2" "$1"; }

run_wizard() { # outfile -- args... (stdin comes from the caller)
    local out=$1; shift
    "$W" "$@" >"$out" 2>&1
    return $?
}

json_check() { # file python-expression (asserts)
    local f=$1 expr=$2
    python3 - "$f" "$expr" <<'PY'
import json
import re
import sys

path, expr = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    d = json.load(fh)
assert d.get("schema") == 1, "schema"
assert d.get("tool") == "oxp3-wizard", "tool"
assert d.get("unique_name"), "unique_name"
assert isinstance(d.get("mode"), str), "mode"
assert isinstance(d.get("host"), dict), "host"
assert isinstance(d.get("verifier"), dict), "verifier"
states = {"pass", "fail", "skip", "blocked"}
ids = set()
ts = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
for it in d["items"]:
    for key in ("phase", "id", "title", "status", "answer", "command", "checks", "evidence", "timestamp"):
        assert key in it, (it.get("id"), key)
    assert it["status"] in states, it
    assert isinstance(it["checks"], list), it["id"]
    assert isinstance(it["evidence"], list), it["id"]
    assert ts.match(it["timestamp"]), it["timestamp"]
    assert it["id"] not in ids, "duplicate item id"
    ids.add(it["id"])
    if it["status"] in ("pass", "fail"):
        assert it["answer"] != "", it["id"]
assert sum(d["counts"][k] for k in ("pass", "fail", "skip", "blocked")) == d["counts"]["total"]
assert len(d["items"]) == d["counts"]["total"]
assert isinstance(d["restore"], list) and isinstance(d["open_points"], list)
assert isinstance(d["last_validated_suggestions"], list)
exec(expr, {"json": json, "d": d, "states": states})
PY
}

printf 'wizard tests: %s\n' "$W"
BEFORE=$(git -C "$REPO" status --porcelain -- docs/state 2>/dev/null | sort)

# 1. selftest -------------------------------------------------------------
if run_wizard "$TMP/selftest.out" --selftest; then
    pass "selftest exits 0"
else
    fail "selftest exits 0"
fi
contains "$TMP/selftest.out" "0 failed" && pass "selftest reports no failures" \
    || fail "selftest reports no failures"

# 2. plan only ------------------------------------------------------------
mkdir -p "$TMP/plan"
if run_wizard "$TMP/plan.out" --non-interactive --state-dir "$TMP/plan"; then
    pass "plan mode exits 0"
else
    fail "plan mode exits 0"
fi
[ -z "$(ls -A "$TMP/plan")" ] && pass "plan mode writes nothing" || fail "plan mode writes nothing"
contains "$TMP/plan.out" "[phase 10/10] final" && pass "plan lists all 10 phases" \
    || fail "plan lists all 10 phases"
[ "$(grep -c '^  - ' "$TMP/plan.out")" -eq 40 ] && pass "plan lists all 40 items" \
    || fail "plan lists all 40 items"
not_contains "$TMP/plan.out" "[sim]" && pass "plan mode runs nothing" \
    || fail "plan mode runs nothing"

# 3. dry run --------------------------------------------------------------
mkdir -p "$TMP/dry"
if run_wizard "$TMP/dry.out" --dry-run --state-dir "$TMP/dry"; then
    pass "dry-run exits 0"
else
    fail "dry-run exits 0"
fi
[ -z "$(ls -A "$TMP/dry")" ] && pass "dry-run writes nothing" || fail "dry-run writes nothing"
contains "$TMP/dry.out" "WIZARD RUNS: tools/oxp3-led.py solid ff0000" \
    && pass "dry-run shows the exact invasive command" \
    || fail "dry-run shows the exact invasive command"
contains "$TMP/dry.out" "systemctl suspend" && pass "dry-run shows the suspend command" \
    || fail "dry-run shows the suspend command"

# 4. scripted run: preflight + volume -------------------------------------
mkdir -p "$TMP/run1"
printf '\n1\n\nn\n1\ny\n' | run_wizard "$TMP/run1.out" --phase preflight,volume \
    --simulate --state-dir "$TMP/run1"
RC=$?
[ "$RC" -eq 0 ] && pass "scripted run exits 0" || fail "scripted run exits 0 (got $RC)"
J1=$(ls "$TMP/run1"/validation-*.json 2>/dev/null | head -n 1)
[ -n "$J1" ] && pass "JSON record written" || fail "JSON record written"
[ -f "${J1%.json}.md" ] && pass "Markdown summary written" || fail "Markdown summary written"
[ -f "${J1%.json}.journal" ] && pass "journal written" || fail "journal written"
if [ -n "$J1" ]; then
    json_check "$J1" 'assert d["counts"] == {"total": 8, "pass": 8, "fail": 0, "skip": 0, "blocked": 0}; assert d["resumed"] is False' \
        && pass "JSON schema, item fields and counts" || fail "JSON schema, item fields and counts"
    json_check "$J1" 'assert [i["status"] for i in d["items"]] == ["pass"] * 8' \
        && pass "every scripted answer is recorded as pass" || fail "every scripted answer is recorded as pass"
    json_check "$J1" 'x=[i for i in d["items"] if i["id"]=="preflight.session"][0]; assert x["answer"].startswith("Desktop session"); assert "vol.hold" in [c for i in d["items"] for c in i["checks"]]' \
        && pass "answers and verifier check mapping recorded" || fail "answers and verifier check mapping recorded"
    contains "$TMP/run1.out" "components.yaml: volume.last_validated.evidence" \
        && pass "suggests the exact components.yaml edit" || fail "suggests the exact components.yaml edit"
fi

# 5. resume ---------------------------------------------------------------
mkdir -p "$TMP/run1"
printf '' | run_wizard "$TMP/resume.out" --resume "$J1" --phase preflight,volume \
    --simulate --state-dir "$TMP/run1"
RC=$?
[ "$RC" -eq 0 ] && pass "resume exits 0 on a complete run" || fail "resume exits 0 on a complete run (got $RC)"
contains "$TMP/resume.out" "complete (resumed)" && pass "resume skips answered phases" \
    || fail "resume skips answered phases"
json_check "$J1" 'assert d["resumed"] is True; assert d["counts"]["total"] == 8' \
    && pass "resume keeps every recorded item" || fail "resume keeps every recorded item"

# 6. suspend refusal ------------------------------------------------------
mkdir -p "$TMP/sleep-no"
printf '\n1\n\n' | run_wizard "$TMP/sleep-no.out" --phase sleep --simulate \
    --state-dir "$TMP/sleep-no"
J2=$(ls "$TMP/sleep-no"/validation-*.json 2>/dev/null | head -n 1)
[ -n "$J2" ] && pass "sleep refusal writes a record" || fail "sleep refusal writes a record"
not_contains "$TMP/sleep-no.out" "[sim] systemctl suspend" \
    && pass "suspend is not executed without the typed word" \
    || fail "suspend is not executed without the typed word"
json_check "$J2" 'x=[i for i in d["items"] if i["id"]=="sleep.suspend"][0]; assert x["status"]=="skip" and "not confirmed" in x["answer"]' \
    && pass "unconfirmed suspend is recorded as skip" || fail "unconfirmed suspend is recorded as skip"
json_check "$J2" 'x=[i for i in d["items"] if i["id"]=="sleep.warm"][0]; assert x["status"]=="skip"' \
    && pass "warmth item is skipped when no suspend ran" || fail "warmth item is skipped when no suspend ran"

# 7. suspend acceptance (simulated cycle) ---------------------------------
mkdir -p "$TMP/sleep-yes"
printf '\n1\nsuspend\nn\n1\n' | run_wizard "$TMP/sleep-yes.out" --phase sleep --simulate \
    --state-dir "$TMP/sleep-yes"
J3=$(ls "$TMP/sleep-yes"/validation-*.json 2>/dev/null | head -n 1)
json_check "$J3" 'x=[i for i in d["items"] if i["id"]=="sleep.suspend"][0]; assert x["status"]=="pass"; assert "last_hw_sleep" in x["answer"]' \
    && pass "typed suspend records one real cycle" || fail "typed suspend records one real cycle"
contains "$TMP/sleep-yes.out" "[sim] systemctl suspend" \
    && pass "typed suspend runs the simulate stub" || fail "typed suspend runs the simulate stub"

# 8. LED phase refuses a wrong confirmation word --------------------------
mkdir -p "$TMP/leds-no"
printf '\nNOPE\n' | run_wizard "$TMP/leds-no.out" --phase leds --simulate \
    --state-dir "$TMP/leds-no"
J4=$(ls "$TMP/leds-no"/validation-*.json 2>/dev/null | head -n 1)
not_contains "$TMP/leds-no.out" "[sim] tools/oxp3-led.py" \
    && pass "wrong phase word runs no LED write" || fail "wrong phase word runs no LED write"
json_check "$J4" 'assert all(i["status"]=="skip" for i in d["items"]); assert d["restore"]==[]' \
    && pass "wrong phase word skips the phase and restores nothing" \
    || fail "wrong phase word skips the phase and restores nothing"

# 9. LED phase with confirmation, then restore ----------------------------
mkdir -p "$TMP/leds-yes"
{
    printf '\nLEDS\n'
    for _ in 1 2 3 4 5; do printf 'y\n1\n'; done
    printf 'y\n1\ny\ny\n'
} | run_wizard "$TMP/leds-yes.out" --phase leds --simulate --state-dir "$TMP/leds-yes"
J5=$(ls "$TMP/leds-yes"/validation-*.json 2>/dev/null | head -n 1)
json_check "$J5" 'assert d["counts"]["pass"]==8; r=d["restore"]; assert len(r)==1 and r[0]["resource"]=="led" and r[0]["command"]' \
    && pass "LED phase passes and registers a restore" || fail "LED phase passes and registers a restore"
json_check "$J5" 'assert [i["status"] for i in d["items"]].count("pass")==8' \
    && pass "all eight LED items recorded" || fail "all eight LED items recorded"
contains "$TMP/leds-yes.out" "== restore ==" && pass "restore section printed" \
    || fail "restore section printed"
contains "$(ls "$TMP/leds-yes"/validation-*.md | head -n 1)" "**led** (simulated)" \
    && pass "restore recorded in the Markdown summary" || fail "restore recorded in the Markdown summary"

# 10. usage and state errors ---------------------------------------------
"$W" --phase bogus >"$TMP/badphase.out" 2>&1
[ $? -eq 2 ] && pass "unknown phase exits 2" || fail "unknown phase exits 2"
"$W" --resume "$TMP/does-not-exist.json" >"$TMP/badresume.out" 2>&1
[ $? -eq 2 ] && pass "missing resume target exits 2" || fail "missing resume target exits 2"
"$W" --bogus >"$TMP/badarg.out" 2>&1
[ $? -eq 2 ] && pass "unknown option exits 2" || fail "unknown option exits 2"
"$W" --version | grep -qx 'oxp3-wizard 1\.0\.0' && pass "--version prints the version" \
    || fail "--version prints the version"

# 11. nothing written into the checkout ----------------------------------
AFTER=$(git -C "$REPO" status --porcelain -- docs/state 2>/dev/null | sort)
[ "$BEFORE" = "$AFTER" ] && pass "tests wrote nothing under docs/state" \
    || fail "tests wrote nothing under docs/state"

printf '\n%d checks, %d failed\n' "$COUNT" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
