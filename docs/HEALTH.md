# Health — one command for "can I install / is it working / what is left"

Date: **2026-11-09**. Scope: `tools/oxp3-health.sh`, `tools/oxp3_health.py`,
`tests/unit/test_health.py`, `tests/fixtures/health/`, the `health` repo-test suite.

`tools/oxp3-health.sh` is the recommended first command on this repository. The three
questions used to need three tools — install readiness, live acceptance and the remaining
checklist — and each printed 70+ rows, so the four non-`PASS` rows were buried. This front-end
runs the same four reporters and prints a short sectioned summary instead:

| stage | command it runs | what it contributes |
|---|---|---|
| preflight | `tools/oxp3-preflight.sh --json` | the safe-to-install verdict and its `FAIL`/`WARN` rows |
| verify | `tools/oxp3-verify.sh --json` (add `--full` to pass it through) | the acceptance checks: working / failing / skipped |
| firstrun | `tools/oxp3-firstrun.sh --check-only --json` | the install step states (what is still missing) |
| status | `tools/oxp3-status.sh --json` | the fix indicators, including XMX and the gaming scheduler |

Nothing is re-implemented and no component tool is changed: this is a composition. Each tool
keeps its own exit code and `--json` schema. The only file written is the health log.

## 1. Commands

```sh
tools/oxp3-health.sh                 # default: short sectioned summary, read-only, ~7 s
tools/oxp3-health.sh --full          # pass --full to oxp3-verify.sh (invasive functional checks)
tools/oxp3-health.sh --json          # machine-readable report (schema 1)
tools/oxp3-health.sh --quiet         # only the one-line verdict
tools/oxp3-health.sh --verbose       # also the raw stage output and every skipped check
tools/oxp3-health.sh --no-preflight  # skip a stage; also --no-verify, --no-firstrun, --no-status
tools/oxp3-health.sh --log FILE      # write the full log to FILE
tools/oxp3-health.sh --selftest      # offline aggregation/verdict self-test (fixtures)

make health                          # == tools/oxp3-health.sh
```

`--json` takes precedence over `--quiet`; both still write the full log.

The default log is
`${XDG_CACHE_HOME:-/tmp}/oxp3-health/<UTC>-<pid>/health.log`; it contains the summary and
every stage's complete stdout/stderr. Nothing is cached between runs.

## 2. Sections

```
== READY TO INSTALL   yes/no (preflight safe verdict). On "no", every blocker with its fix.
== INSTALLED          done/total firstrun steps, the missing step ids, and how many hardware
                      components are fully installed (from preflight's state.* artefact counts).
== WORKING / FAILING  verify's pass/fail/skip/unknown counts and ONLY the FAIL/UNKNOWN rows
                      with their one-line remediation. SKIP rows are counted, not printed.
== WARNINGS           the non-PASS preflight WARN rows and status WARN/FAIL indicators.
== TOOL PROBLEMS      any stage that was missing, crashed or timed out (never fatal).
== NEXT               1-3 commands: blockers, then the first failure per verify group, then
                      the first missing install step.
```

`PASS`/`OK` rows are never echoed — that is the point of the tool. `--verbose` adds the skipped
rows with their hints and the raw stage reports.

## 3. Verdict and exit code

| exit | verdict | meaning |
|---:|---|---|
| 0 | `ok` | at least one stage reported, there is no preflight blocker, no verify/status `FAIL` and no tool problem |
| 1 | `attention` | a preflight blocker exists, a verify or status row is `FAIL`, or a requested stage was missing/errored/timed out |
| 2 | – | usage error (unknown option, or every stage skipped with `--no-*`) |
| 3 | `unknown` | no requested stage produced a report (e.g. every tool missing) |

A `todo` firstrun step or a `SKIP` verify row is *not* a failure: it means "not installed / not
applicable yet" and appears in `INSTALLED`/the counts. `partial` installs appear under
`INSTALLED` and `components`, and their remediation commands show up in `NEXT`.

A missing/crashing/timed-out reporter is reported under `TOOL PROBLEMS` and the rest of the
report is still produced. Missing tools are detected before spawning; each stage has a
wall-clock limit (`preflight` 20 s, `verify` 30 s, `firstrun` 20 s, `status` 20 s; `verify
--full` 240 s) so a hung tool cannot block the report. The default run on this host measures
**~6.7 s** wall clock (verify dominates at ~5.7 s).

## 4. JSON

`--json` prints one object (schema 1). The stable keys are:

```json
{
  "schema": 1, "tool": "oxp3-health", "version": "1.0.0",
  "generated_at": "…Z", "mode": "default", "root": false, "kernel": "…",
  "elapsed_ms": 6700, "log": "/tmp/oxp3-health/…/health.log",
  "stages": {"preflight": {"status": "ok", "rc": 0, "duration_ms": 300, "error": ""},
             "verify": {…}, "firstrun": {…}, "status": {…}},
  "ready_to_install": true, "blockers": [],
  "preflight": {"available": true, "safe": true, "counts": {…}, "warnings": […], "components": […]},
  "installed": {"done": 6, "total": 19, "missing": [{"id": "hw-install", "state": "todo", …}]},
  "components": {"installed": 2, "total": 8, "partial": […], "missing": […]},
  "verify": {"available": true, "counts": {"total": 72, "working": 41, "failing": 4,
             "skipped": 27, "unknown": 0}, "failing": […], "unknown": […], "skipped": […]},
  "status": {"available": true, "overall": "warn", "counts": {…}, "non_ok": […]},
  "problems": [], "next_commands": ["…"], "verdict": "attention", "exit_code": 1
}
```

`ready_to_install` is `null` when preflight did not report. `status` is one of `ok`, `skipped`,
`missing`, `error`, `timeout` per stage. Ids, titles, summaries and remediation strings are
passed through from the component tools, so a consumer can map any row back to its reporter.

## 5. Tests

* `tools/oxp3-health.sh --selftest` — 97 offline checks over five fixtures (all-pass, blockers,
  partial install, missing tools, crashing tool) plus the all-missing/all-skipped contracts.
* `tests/unit/test_health.py` — 24 offline tests: the same fixtures, the stage runner against
  stub scripts (missing, crash, garbage JSON, verdict exit, backend-start exit, timeout), the
  next-command selection, JSON schema, quiet line and the log.
* `tools/repo-test.sh --fast --only health` — runs the selftest, a live `--json` contract check
  and a live `--quiet` check (~8 s, tier `medium`; the whole `--fast` run is ~46 s).

The fixtures live in `tests/fixtures/health/*.json`; each is a stage envelope with the raw
reporter payloads and the expected verdict, so a behaviour change must update the expectation
in the same commit.
