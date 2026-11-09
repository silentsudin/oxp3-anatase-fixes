#!/usr/bin/env python3
"""oxp3-health - one read-only command for "can I install / is it working / what is left".

The three questions used to need three commands (``oxp3-preflight.sh``,
``oxp3-verify.sh``, ``oxp3-firstrun.sh --check-only``) and printed 70+ rows each,
so the four rows that mattered were buried in a wall of PASS lines. This engine
composes those reporters (plus ``oxp3-status.sh --json`` for the fix/XMX/scheduler
state) and prints a short sectioned summary:

    == READY TO INSTALL      yes/no, from preflight's safe verdict + its FAIL rows
    == INSTALLED             done/total firstrun steps, the missing ones and the
                             per-component artefact counts
    == WORKING / FAILING     verify's counts and ONLY its FAIL/UNKNOWN rows
    == WARNINGS              the non-PASS preflight and status rows
    == TOOL PROBLEMS         any stage that was missing, crashed or timed out
    == NEXT                  1-3 commands, derived from the rows above

Nothing is re-implemented: every observation comes from the existing tools, run
in their documented read-only modes. The only file this tool writes is its own
log (``${XDG_CACHE_HOME:-/tmp}/oxp3-health/<utc>-<pid>/health.log`` by default),
which carries the complete stage output; ``--verbose`` also prints it. No result
is cached: every run asks the component tools again.

A component tool that is missing, exits oddly, times out or prints invalid JSON
never aborts the run: the stage is recorded as a problem, the other stages still
report, and the summary says so. Per-stage timeouts keep a hung tool from
blocking the report.

Verdict / exit code
    attention / 1   a preflight blocker exists, a verify or status row is FAIL,
                    or a requested stage was missing/errored/timed out
    ok / 0          at least one stage reported and nothing above is true
    usage / 2       bad option
    unknown / 3     no requested stage produced a report (e.g. every tool missing)

``--quiet`` prints one verdict line; ``--json`` prints the machine-readable
report (schema 1); both still write the full log.

See docs/HEALTH.md (and docs/HANDOVER.md "start here") for the section contract,
the verdict logic and the stage timeouts.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

PROG = "oxp3-health"
VERSION = "1.0.0"
SCHEMA = 1

STAGE_ORDER = ("preflight", "verify", "firstrun", "status")
STAGE_TOOL = {
    "preflight": "oxp3-preflight.sh",
    "verify": "oxp3-verify.sh",
    "firstrun": "oxp3-firstrun.sh",
    "status": "oxp3-status.sh",
}
# Per-stage wall-clock limits. The default run measures ~7 s in total, so these
# only bite when a tool hangs; --full is allowed much longer because verify's
# functional mode measures a battery window and writes one LED frame.
STAGE_TIMEOUT = {
    "preflight": 20,
    "verify": 30,
    "firstrun": 20,
    "status": 20,
}
FULL_TIMEOUT = {"verify": 240}

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOL_DIR)

OK_STATES = ("ok", "pass")
SKIP_FIRSTRUN = ("n/a",)


# --------------------------------------------------------------------- helpers
def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _oneline(text, limit=200) -> str:
    """Flatten a possibly multi-line tool string into one short line."""
    text = " ".join(str(text or "").split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def _clean_command(text, repo=REPO) -> str:
    """A command suitable for the NEXT section: no trailing comment, repo-relative."""
    text = str(text or "").strip()
    # First line only; some remediations chain with ' ; ' and stay readable.
    text = text.splitlines()[0] if text else ""
    # Drop "   # explanation" (the comment is shown by the rows themselves).
    text = re.sub(r"\s+#.*$", "", text).strip()
    if repo:
        prefix = repo.rstrip("/") + "/"
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _stage_data(stages, name):
    """The parsed JSON payload of a stage, or None when it did not report."""
    stage = (stages or {}).get(name) or {}
    if stage.get("status") != "ok":
        return None
    data = stage.get("data")
    return data if isinstance(data, dict) else None


def _failed(stage):
    return (stage or {}).get("status") in ("missing", "error", "timeout")


# --------------------------------------------------------------------- running
def stage_argv(name, tool_dir=TOOL_DIR, full=False):
    tool = os.path.join(tool_dir, STAGE_TOOL[name])
    if name == "verify":
        argv = [tool, "--json"]
        if full:
            argv.append("--full")
        return argv
    if name == "firstrun":
        return [tool, "--check-only", "--json"]
    return [tool, "--json"]


def run_stage(name, timeout, tool_dir=TOOL_DIR, full=False, cwd=REPO):
    """Run one reporter and turn its output into a stage record. Never raises."""
    argv = stage_argv(name, tool_dir, full)
    started = time.monotonic()
    record = {
        "status": "ok",
        "rc": None,
        "duration_ms": 0,
        "stdout": "",
        "stderr": "",
        "error": "",
        "argv": argv,
    }
    tool = argv[0]
    if not os.path.isfile(tool) or not os.access(tool, os.X_OK):
        record["status"] = "missing"
        record["error"] = "%s not found or not executable" % os.path.relpath(tool, REPO)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        record["status"] = "timeout"
        record["error"] = "timed out after %ss" % timeout
        record["stdout"] = _decode(exc.stdout)
        record["stderr"] = _decode(exc.stderr)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record
    except FileNotFoundError:
        record["status"] = "missing"
        record["error"] = "%s not found" % os.path.relpath(tool, REPO)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record
    except OSError as exc:
        record["status"] = "error"
        record["error"] = "could not execute %s: %s" % (os.path.relpath(tool, REPO), exc)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        return record

    record["rc"] = proc.returncode
    record["stdout"] = proc.stdout or ""
    record["stderr"] = proc.stderr or ""
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    data, parse_error = _parse_json(record["stdout"])
    if data is None:
        record["status"] = "error"
        hint = _oneline(record["stderr"], 120)
        record["error"] = parse_error + ("; stderr: " + hint if hint else "")
    elif proc.returncode == 3:
        # 3 is "the backend could not start" for every reporter in this repo.
        record["status"] = "error"
        record["error"] = "backend could not start (rc=3): %s" % _oneline(
            record["stderr"] or record["stdout"], 160
        )
    elif proc.returncode not in (0, 1, 2):
        record["status"] = "error"
        record["error"] = "unexpected exit code %s" % proc.returncode
    else:
        # 0/1/2 are verdicts, not failures: preflight safe/unsafe, verify
        # pass/fail and "nothing passed", status ok/fail/warn.
        record["data"] = data
    return record


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _parse_json(text):
    text = (text or "").strip()
    if not text:
        return None, "no output (empty stdout)"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, "stdout is not valid JSON: %s" % _oneline(str(exc), 120)
    if not isinstance(data, dict):
        return None, "stdout JSON is not an object"
    return data, ""


def collect(tool_dir=TOOL_DIR, full=False, skip=(), cwd=REPO):
    """Run the requested stages; each is isolated and timed out."""
    stages = {}
    for name in STAGE_ORDER:
        if name in skip:
            stages[name] = {
                "status": "skipped",
                "rc": None,
                "duration_ms": 0,
                "stdout": "",
                "stderr": "",
                "error": "",
                "argv": stage_argv(name, tool_dir, full),
            }
            continue
        timeout = FULL_TIMEOUT.get(name, STAGE_TIMEOUT[name]) if full else STAGE_TIMEOUT[name]
        stages[name] = run_stage(name, timeout, tool_dir=tool_dir, full=full, cwd=cwd)
    return stages


# ----------------------------------------------------------------- aggregation
def _preflight_rows(pre):
    """Split preflight's checks into blockers/warnings plus component artefacts."""
    blockers, warnings = [], []
    components = []
    for check in _as_list(pre.get("checks")):
        state = str(check.get("state") or "").upper()
        row = {
            "id": check.get("id") or "",
            "title": check.get("title") or "",
            "summary": check.get("summary") or "",
            "remediation": check.get("remediation") or "",
        }
        if state == "FAIL":
            blockers.append(row)
        elif state == "WARN":
            warnings.append(row)
        cid = row["id"]
        if cid.startswith("state.") and cid not in ("state.bt-initramfs", "state.ostree-staged"):
            match = re.search(r"(\d+)\s*/\s*(\d+)", row["summary"])
            if match:
                have, total = int(match.group(1)), int(match.group(2))
                if total <= 0:
                    continue
                if have >= total:
                    comp_state = "installed"
                elif have > 0:
                    comp_state = "partial"
                else:
                    comp_state = "missing"
                components.append(
                    {
                        "id": cid[len("state."):],
                        "installed": have,
                        "total": total,
                        "state": comp_state,
                        "summary": row["summary"],
                    }
                )
    # A component blocker is authoritative even if the matching check row is odd.
    if not blockers:
        for comp in (pre.get("components") or {}).values():
            for cid in _as_list(comp.get("blockers")):
                blockers.append(
                    {
                        "id": str(cid),
                        "title": "",
                        "summary": "preflight blocker",
                        "remediation": "",
                    }
                )
    if pre.get("safe") is False and not blockers:
        blockers.append(
            {
                "id": "preflight.verdict",
                "title": "preflight verdict",
                "summary": "preflight reported the host unsafe",
                "remediation": "tools/oxp3-preflight.sh",
            }
        )
    return blockers, warnings, components


def _firstrun_steps(fr):
    steps = [s for s in _as_list(fr.get("steps")) if str(s.get("state")) not in SKIP_FIRSTRUN]
    missing = []
    for step in steps:
        state = str(step.get("state") or "")
        if state in OK_STATES:
            continue
        missing.append(
            {
                "id": step.get("id") or "",
                "title": step.get("title") or "",
                "state": state,
                "detail": step.get("detail") or "",
                "command": step.get("command") or "",
            }
        )
    done = sum(1 for s in steps if str(s.get("state")) == "ok")
    return steps, done, missing


def _verify_rows(ve):
    checks = _as_list(ve.get("checks"))
    counts = ve.get("counts") or {}

    def count(key, state=None):
        if state is not None:
            return sum(1 for c in checks if str(c.get("state")) == state)
        return _int(counts.get(key), 0) if checks else 0

    failing = [
        {
            "id": c.get("id") or "",
            "group": c.get("group") or "",
            "title": c.get("title") or "",
            "summary": c.get("summary") or "",
            "detail": c.get("detail") or "",
            "remediation": c.get("remediation") or "",
        }
        for c in checks
        if str(c.get("state")) == "fail"
    ]
    unknown = [
        {
            "id": c.get("id") or "",
            "group": c.get("group") or "",
            "title": c.get("title") or "",
            "summary": c.get("summary") or "",
            "detail": c.get("detail") or "",
            "remediation": c.get("remediation") or "",
        }
        for c in checks
        if str(c.get("state")) == "unknown"
    ]
    skipped = [
        {
            "id": c.get("id") or "",
            "title": c.get("title") or "",
            "summary": c.get("summary") or "",
            "remediation": c.get("remediation") or "",
        }
        for c in checks
        if str(c.get("state")) == "skip"
    ]
    total = count("total") or len(checks)
    return {
        "available": True,
        "counts": {
            "total": total,
            "working": count("pass", "pass"),
            "failing": count("fail", "fail"),
            "skipped": count("skip", "skip"),
            "unknown": count("unknown", "unknown"),
        },
        "failing": failing,
        "unknown": unknown,
        "skipped": skipped,
    }


def _status_rows(status):
    counts = status.get("counts") or {}
    non_ok = []
    for group in _as_list(status.get("groups")):
        for ind in _as_list(group.get("indicators")):
            state = str(ind.get("state") or "")
            if state in OK_STATES or state == "na":
                continue
            non_ok.append(
                {
                    "id": ind.get("id") or "",
                    "group": group.get("id") or "",
                    "label": ind.get("label") or "",
                    "state": state,
                    "value": ind.get("value") or "",
                    "detail": ind.get("detail") or "",
                    "source": ind.get("source") or "",
                }
            )
    return {
        "available": True,
        "overall": status.get("overall") or "",
        "counts": {
            "ok": _int(counts.get("ok"), 0),
            "warn": _int(counts.get("warn"), 0),
            "fail": _int(counts.get("fail"), 0),
            "unknown": _int(counts.get("unknown"), 0),
            "na": _int(counts.get("na"), 0),
        },
        "non_ok": non_ok,
    }


def _as_list(value):
    return value if isinstance(value, list) else []


def _next_commands(report):
    """1-3 commands to run next: blockers, then the first fail per verify group,
    then the first missing install steps. Exact duplicates are dropped."""
    commands = []

    def add(text):
        cmd = _clean_command(text)
        if cmd and cmd not in commands:
            commands.append(cmd)

    for blocker in report["blockers"]:
        add(blocker.get("remediation"))
    seen_groups = set()
    for row in report.get("verify", {}).get("failing", []):
        group = row.get("group") or "?"
        if group in seen_groups:
            continue
        if row.get("remediation"):
            seen_groups.add(group)
            add(row["remediation"])
    for step in report.get("installed", {}).get("missing", []):
        if step.get("state") in ("todo", "partial"):
            add(step.get("command"))
    if not commands:
        if report.get("ready_to_install") is True:
            commands.append("tools/oxp3-firstrun.sh --collect   # redacted evidence bundle")
        else:
            commands.append("tools/oxp3-health.sh --verbose   # full stage reports")
    return commands[:3]


def aggregate_stages(stages, full=False):
    """Pure aggregation: stage records -> the derived verdict/report body."""
    pre = _stage_data(stages, "preflight")
    ve = _stage_data(stages, "verify")
    fr = _stage_data(stages, "firstrun")
    st = _stage_data(stages, "status")

    blockers, warnings, components = ([], [], [])
    preflight_counts = {}
    if pre:
        blockers, warnings, components = _preflight_rows(pre)
        counts = pre.get("counts") or {}
        preflight_counts = {
            "total": _int(counts.get("total"), 0),
            "ok": _int(counts.get("ok"), 0),
            "warn": _int(counts.get("warn"), 0),
            "fail": _int(counts.get("fail"), 0),
        }
    if pre is None or "safe" not in pre:
        ready = None
    else:
        ready = bool(pre.get("safe")) and not blockers

    installed = {"done": 0, "total": 0, "missing": []}
    firstrun_counts = {}
    if fr:
        steps, done, missing = _firstrun_steps(fr)
        installed = {"done": done, "total": len(steps), "missing": missing}
        counts = fr.get("counts") or {}
        firstrun_counts = {key: _int(value, 0) for key, value in counts.items()}

    verify = {"available": False, "counts": {}, "failing": [], "unknown": [], "skipped": []}
    if ve:
        verify = _verify_rows(ve)

    status = {"available": False, "overall": "", "counts": {}, "non_ok": []}
    if st:
        status = _status_rows(st)

    comp_installed = sum(1 for c in components if c["state"] == "installed")
    comp_partial = [c for c in components if c["state"] == "partial"]
    comp_missing = [c["id"] for c in components if c["state"] == "missing"]

    problems = []
    for name in STAGE_ORDER:
        stage = stages.get(name) or {}
        if _failed(stage):
            problems.append(
                {
                    "stage": name,
                    "status": stage.get("status"),
                    "rc": stage.get("rc"),
                    "error": stage.get("error") or "",
                }
            )

    usable = [name for name in STAGE_ORDER if (stages.get(name) or {}).get("status") == "ok"]
    status_fail = sum(1 for row in status.get("non_ok", []) if row.get("state") == "fail")
    if not usable:
        verdict, exit_code = "unknown", 3
    elif problems or blockers or verify.get("counts", {}).get("failing", 0) or status_fail:
        verdict, exit_code = "attention", 1
    else:
        verdict, exit_code = "ok", 0

    report = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "mode": "full" if full else "default",
        "root": _is_root(),
        "kernel": _kernel(),
        "ready_to_install": ready,
        "blockers": blockers,
        "preflight": {
            "available": pre is not None,
            "safe": None if pre is None else bool(pre.get("safe")),
            "counts": preflight_counts,
            "warnings": warnings,
            "components": components,
        },
        "installed": installed,
        "firstrun": {
            "available": fr is not None,
            "counts": firstrun_counts,
            "missing": installed["missing"],
        },
        "components": {
            "installed": comp_installed,
            "total": len(components),
            "partial": comp_partial,
            "missing": comp_missing,
        },
        "verify": verify,
        "status": status,
        "problems": problems,
        "verdict": verdict,
        "exit_code": exit_code,
    }
    report["next_commands"] = _next_commands(report)
    return report


def _is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:  # pragma: no cover - non-POSIX
        return False


def _kernel() -> str:
    try:
        return os.uname().release
    except AttributeError:  # pragma: no cover
        return "?"


# --------------------------------------------------------------------- render
def _stage_line(stages):
    parts = []
    for name in STAGE_ORDER:
        stage = stages.get(name) or {}
        status = stage.get("status") or "?"
        seconds = _int(stage.get("duration_ms"), 0) / 1000.0
        parts.append("%s %s %.1fs" % (name, status, seconds))
    return "stages: " + " | ".join(parts)


def _fill(text, width=112, indent="      "):
    """Wrap one logical line at a word boundary, keeping the leading indent."""
    import textwrap

    return textwrap.fill(
        text,
        width=width,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ).splitlines()


def _shorten_repo(text, repo=REPO):
    """Show repo paths relative to the checkout in a row's remediation."""
    text = str(text or "")
    prefix = (repo or "").rstrip("/") + "/"
    if prefix.strip("/"):
        text = text.replace(prefix, "")
    return _oneline(text)


def _missing_ids(rows):
    return [row.get("id") for row in rows if row.get("id")]


def render_text(report, stages=None, verbose=False):
    stages = stages or {}
    out = []
    header = "%s %s  kernel=%s  mode=%s  root=%s  elapsed=%.1fs" % (
        PROG,
        report["version"],
        report["kernel"],
        report["mode"],
        "yes" if report["root"] else "no",
        _int(report.get("elapsed_ms"), 0) / 1000.0,
    )
    out.append(header)
    if report.get("log"):
        out.append("log: %s" % report["log"])
    out.append(_stage_line(stages))

    problems = report.get("problems") or []
    if problems:
        out.append("")
        out.append("== TOOL PROBLEMS (%d)" % len(problems))
        for problem in problems:
            detail = problem.get("error") or problem.get("status") or ""
            out.append("   ! %-9s %s" % (problem["stage"], _oneline(detail, 180)))

    out.append("")
    ready = report.get("ready_to_install")
    if ready is True:
        counts = report["preflight"].get("counts") or {}
        out.append(
            "== READY TO INSTALL: yes (preflight safe; %d warning(s))"
            % _int(counts.get("warn"), 0)
        )
    elif ready is False:
        out.append("== READY TO INSTALL: no (%d blocker(s))" % len(report["blockers"]))
        for blocker in report["blockers"]:
            out.append("   BLOCK  %s  %s" % (blocker["id"], _oneline(blocker["summary"], 140)))
            if blocker.get("remediation"):
                out.append("          fix: %s" % _shorten_repo(blocker["remediation"]))
    else:
        out.append("== READY TO INSTALL: unknown (preflight unavailable)")

    installed = report["installed"]
    if installed.get("total"):
        line = "== INSTALLED: %d/%d steps" % (installed["done"], installed["total"])
        missing = _missing_ids(installed["missing"])
        if missing:
            out.extend(_fill(line + " \u2014 missing: " + " ".join(missing), indent="      "))
        else:
            out.append(line + " (nothing left)")
    else:
        out.append("== INSTALLED: unknown (firstrun unavailable)")
    comps = report["components"]
    if comps.get("total"):
        detail = "%d/%d fully installed" % (comps["installed"], comps["total"])
        extras = []
        if comps["missing"]:
            extras.append("missing: " + " ".join(comps["missing"]))
        if comps["partial"]:
            extras.append(
                "partial: "
                + " ".join("%s %d/%d" % (c["id"], c["installed"], c["total"]) for c in comps["partial"])
            )
        if extras:
            out.extend(_fill("   components: %s \u2014 " % detail + " \u2014 ".join(extras)))
        else:
            out.append("   components: %s" % detail)

    verify = report.get("verify") or {}
    if verify.get("available"):
        counts = verify["counts"]
        out.append(
            "== WORKING / FAILING: %d working, %d failing, %d skipped, %d unknown (%d checks)"
            % (
                counts["working"],
                counts["failing"],
                counts["skipped"],
                counts["unknown"],
                counts["total"],
            )
        )
        for row in verify.get("failing", []):
            out.append("   FAIL     %s  %s" % (row["id"], _oneline(row["summary"], 130)))
            if row.get("remediation"):
                out.append("            fix: %s" % _shorten_repo(row["remediation"]))
        for row in verify.get("unknown", []):
            out.append("   UNKNOWN  %s  %s" % (row["id"], _oneline(row["summary"], 130)))
            if row.get("remediation"):
                out.append("            hint: %s" % _shorten_repo(row["remediation"]))
        if verify.get("skipped") and not verbose:
            out.append(
                "   skipped: %d check(s) not installed/not applicable "
                "(--verbose lists each with its hint)" % len(verify["skipped"])
            )
    else:
        out.append("== WORKING / FAILING: unavailable (verify did not report)")

    warnings = list(report["preflight"].get("warnings") or [])
    if warnings or report["status"].get("non_ok"):
        out.append("== WARNINGS (%d)" % (len(warnings) + len(report["status"].get("non_ok") or [])))
        for row in warnings:
            out.append("   WARN     preflight/%s  %s" % (row["id"], _oneline(row["summary"], 130)))
            if row.get("remediation"):
                out.append("            fix: %s" % _shorten_repo(row["remediation"]))
        for row in report["status"].get("non_ok") or []:
            label = row.get("label") or row["id"]
            out.append(
                "   %-8s status/%s  %s: %s"
                % (row["state"].upper(), row["id"], _oneline(label, 60), _oneline(row["value"], 90))
            )
            if row.get("detail"):
                out.append("            detail: %s" % _oneline(row["detail"], 160))

    if verbose and verify.get("skipped"):
        out.append("")
        out.append("== SKIPPED (%d) \u2014 not installed, not applicable or not privileged"
                   % len(verify["skipped"]))
        for row in verify["skipped"]:
            out.append("   SKIP     %s  %s" % (row["id"], _oneline(row["summary"], 110)))
            if row.get("remediation"):
                out.append("            hint: %s" % _shorten_repo(row["remediation"]))

    out.append("")
    out.append("== NEXT")
    for index, command in enumerate(report["next_commands"], 1):
        out.append("   %d. %s" % (index, command))
    return "\n".join(out)


def render_json(report):
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def quiet_line(report):
    installed = report["installed"]
    verify = report.get("verify") or {}
    counts = verify.get("counts") or {}
    ready = report.get("ready_to_install")
    ready_text = "yes" if ready is True else ("no" if ready is False else "unknown")
    return (
        "health: READY %s | INSTALLED %d/%d | WORKING %d FAILING %d SKIP %d | verdict %s"
        % (
            ready_text,
            _int(installed.get("done"), 0),
            _int(installed.get("total"), 0),
            _int(counts.get("working"), 0),
            _int(counts.get("failing"), 0),
            _int(counts.get("skipped"), 0),
            report["verdict"],
        )
    )


# ------------------------------------------------------------------------- log
def default_log_path():
    base = os.environ.get("OXP3_HEALTH_LOG_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or "/tmp", "oxp3-health"
    )
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(base, "%s-%d" % (stamp, os.getpid()), "health.log")


def write_log(path, report, stages):
    """Write the complete report plus every stage's raw output. Never raises."""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(render_text(report, stages, verbose=True))
            handle.write("\n\n# ===== raw stage output =====\n")
            for name in STAGE_ORDER:
                stage = stages.get(name) or {}
                handle.write(
                    "\n## %s: status=%s rc=%s duration_ms=%s\n"
                    % (name, stage.get("status"), stage.get("rc"), stage.get("duration_ms"))
                )
                if stage.get("argv"):
                    handle.write("# argv: %s\n" % " ".join(stage["argv"]))
                if stage.get("error"):
                    handle.write("# error: %s\n" % stage["error"])
                if stage.get("stderr"):
                    handle.write("# stderr:\n%s\n" % stage["stderr"].rstrip())
                if stage.get("stdout"):
                    handle.write(stage["stdout"].rstrip() + "\n")
        return path
    except OSError as exc:
        sys.stderr.write("%s: could not write log %s: %s\n" % (PROG, path, exc))
        return ""


# -------------------------------------------------------------------- selftest
class _Check:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def eq(self, label, expected, actual):
        if expected == actual:
            self.passed += 1
        else:
            self.failed += 1
            print("FAIL %s: expected %r, got %r" % (label, expected, actual))

    def true(self, label, value):
        self.eq(label, True, bool(value))


def _load_fixture(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run_selftest(fixtures_dir):
    import glob

    if not os.path.isdir(fixtures_dir):
        sys.stderr.write("%s: fixtures not found: %s\n" % (PROG, fixtures_dir))
        return 3
    paths = sorted(glob.glob(os.path.join(fixtures_dir, "*.json")))
    if not paths:
        sys.stderr.write("%s: no fixtures in %s\n" % (PROG, fixtures_dir))
        return 3
    check = _Check()
    for path in paths:
        fixture = _load_fixture(path)
        name = fixture.get("name") or os.path.basename(path)
        expect = fixture.get("expect") or {}
        stages = fixture.get("stages") or {}
        report = build_report(stages)
        for key in ("ready_to_install", "verdict", "exit_code"):
            if key in expect:
                check.eq("%s/%s" % (name, key), expect[key], report[key])
        if "installed_done" in expect:
            check.eq("%s/installed_done" % name, expect["installed_done"], report["installed"]["done"])
        if "installed_total" in expect:
            check.eq("%s/installed_total" % name, expect["installed_total"], report["installed"]["total"])
        if "missing_step_ids" in expect:
            check.eq(
                "%s/missing_step_ids" % name,
                expect["missing_step_ids"],
                _missing_ids(report["installed"]["missing"]),
            )
        if "blocker_ids" in expect:
            check.eq("%s/blocker_ids" % name, expect["blocker_ids"], [b["id"] for b in report["blockers"]])
        if "failing_ids" in expect:
            check.eq(
                "%s/failing_ids" % name,
                expect["failing_ids"],
                [row["id"] for row in report["verify"].get("failing", [])],
            )
        if "problem_stages" in expect:
            check.eq(
                "%s/problem_stages" % name,
                expect["problem_stages"],
                [p["stage"] for p in report["problems"]],
            )
        if "components_installed" in expect:
            check.eq(
                "%s/components_installed" % name,
                expect["components_installed"],
                report["components"]["installed"],
            )
        if "components_total" in expect:
            check.eq("%s/components_total" % name, expect["components_total"], report["components"]["total"])
        # Every scenario must render a parseable JSON report and a bounded NEXT list.
        try:
            encoded = render_json(report)
            parsed = json.loads(encoded)
            check.eq("%s/json-schema" % name, SCHEMA, parsed.get("schema"))
            check.eq("%s/json-tool" % name, PROG, parsed.get("tool"))
            check.true("%s/json-stages" % name, isinstance(parsed.get("stages"), dict))
            check.true("%s/next-bounded" % name, 1 <= len(report["next_commands"]) <= 3)
        except (TypeError, ValueError) as exc:  # pragma: no cover - fixture bug
            check.eq("%s/json-renders" % name, "ok", "error: %s" % exc)
        text = render_text(report, stages)
        check.true("%s/text-has-ready" % name, "READY TO INSTALL:" in text)
        check.true("%s/text-has-next" % name, "== NEXT" in text)
        check.eq("%s/quiet-one-line" % name, 1, len(quiet_line(report).splitlines()))

    # Contract checks that do not depend on a fixture.
    all_missing = {
        name: {"status": "missing", "error": "not found", "rc": None, "duration_ms": 0}
        for name in STAGE_ORDER
    }
    empty = aggregate_stages(all_missing)
    check.eq("all-missing/verdict", "unknown", empty["verdict"])
    check.eq("all-missing/exit", 3, empty["exit_code"])
    check.eq("all-missing/ready", None, empty["ready_to_install"])
    check.true("all-missing/problems", len(empty["problems"]) == len(STAGE_ORDER))
    check.eq("all-missing/next", 1, len(empty["next_commands"]))

    skip_all = {
        name: {"status": "skipped", "error": "", "rc": None, "duration_ms": 0}
        for name in STAGE_ORDER
    }
    skipped = aggregate_stages(skip_all)
    check.eq("all-skipped/verdict", "unknown", skipped["verdict"])
    check.eq("all-skipped/no-problems", 0, len(skipped["problems"]))

    if check.failed:
        print("selftest: %d checks passed, %d failed" % (check.passed, check.failed))
        return 1
    print("selftest: %d checks passed (%d fixtures)" % (check.passed, len(paths)))
    return 0


# ------------------------------------------------------------------------ main
def build_report(stages, full=False, elapsed_ms=0, log_path=""):
    report = aggregate_stages(stages, full=full)
    report["generated_at"] = _now_iso()
    report["elapsed_ms"] = int(elapsed_ms)
    report["log"] = log_path
    report["stages"] = {
        name: {
            "status": (stages.get(name) or {}).get("status"),
            "rc": (stages.get(name) or {}).get("rc"),
            "duration_ms": _int((stages.get(name) or {}).get("duration_ms"), 0),
            "error": (stages.get(name) or {}).get("error") or "",
        }
        for name in STAGE_ORDER
    }
    return report


def _verbose_dump(stages):
    chunks = []
    for name in STAGE_ORDER:
        stage = stages.get(name) or {}
        if not (stage.get("stdout") or stage.get("stderr")):
            continue
        chunks.append(
            "== VERBOSE: %s (status=%s rc=%s)" % (name, stage.get("status"), stage.get("rc"))
        )
        if stage.get("stderr"):
            chunks.append("# stderr:\n" + stage["stderr"].rstrip())
        if stage.get("stdout"):
            chunks.append(stage["stdout"].rstrip())
        chunks.append("")
    return "\n".join(chunks)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="One read-only health front-end: can I install, is it working, what is left.",
        epilog=(
            "Exit: 0 ok, 1 attention (blocker, FAIL row, or a tool problem), 2 usage, "
            "3 no stage produced a report. Read-only. Full detail goes to the log; "
            "--verbose also prints it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--full", action="store_true",
                        help="pass --full to oxp3-verify.sh (functional/invasive checks)")
    parser.add_argument("--json", action="store_true", help="machine-readable report (schema 1)")
    parser.add_argument("--quiet", action="store_true", help="print only the one-line verdict")
    parser.add_argument("--verbose", action="store_true",
                        help="also print every stage's raw output and the skipped checks")
    parser.add_argument("--no-preflight", action="store_true", help="skip the preflight stage")
    parser.add_argument("--no-verify", action="store_true", help="skip the verify stage")
    parser.add_argument("--no-firstrun", action="store_true", help="skip the firstrun stage")
    parser.add_argument("--no-status", action="store_true", help="skip the status stage")
    parser.add_argument("--log", metavar="FILE",
                        help="write the full log here (default: a per-run cache directory)")
    parser.add_argument("--selftest", action="store_true",
                        help="offline aggregation/verdict self-test (no host access)")
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, VERSION))
    args = parser.parse_args(argv)

    fixtures_dir = os.path.join(REPO, "tests", "fixtures", "health")
    if args.selftest:
        return run_selftest(fixtures_dir)

    skip = set()
    if args.no_preflight:
        skip.add("preflight")
    if args.no_verify:
        skip.add("verify")
    if args.no_firstrun:
        skip.add("firstrun")
    if args.no_status:
        skip.add("status")
    if len(skip) == len(STAGE_ORDER):
        sys.stderr.write("%s: every stage is skipped (--no-*), nothing to report\n" % PROG)
        return 2

    started = time.monotonic()
    stages = collect(full=args.full, skip=skip)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log_target = args.log or default_log_path()
    report = build_report(stages, args.full, elapsed_ms=elapsed_ms, log_path=log_target)
    report["generated_at"] = _now_iso()
    log_path = write_log(log_target, report, stages)
    report["log"] = log_path

    if args.json:
        sys.stdout.write(render_json(report))
    elif args.quiet:
        print(quiet_line(report))
    else:
        sys.stdout.write(render_text(report, stages, verbose=args.verbose) + "\n")
        if args.verbose:
            sys.stdout.write("\n" + _verbose_dump(stages))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
