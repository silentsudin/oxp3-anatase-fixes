#!/usr/bin/env python3
"""Build the synthetic evidence bundles used by tools/oxp3_acceptance.py --selftest.

The bundles are internally consistent but synthetic: they never claim to describe
this host.  Each one pins one aggregation behaviour that has already gone wrong in
this project or would silently mislead the acceptance report:

  all-green         every component working, every check passing, sign-offs recorded
                    -> all seven items complete, no missing evidence
  awaiting-install  the real components.yaml states; device checks SKIP because the
                    install steps are still open -> awaiting-install, never "proven"
  blocked           one registry-blocked component (SAGV) plus failing Bluetooth
                    checks -> the audit item is blocked and says why
  missing-evidence  no verifier/health/repo-test data at all -> no crash, no
                    "implemented-and-proven-offline" invented, gaps named

Standard library only.  Run from the repository root:

    python3 tests/fixtures/acceptance/build-fixtures.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(REPO / "tools"))

import oxp3_acceptance as ACC  # noqa: E402

DEFAULT_SUITES = sorted(set(ACC.DEFAULT_SUITES) | {
    "voltap-integration", "audio-eq-isolation", "fp-c-tests", "fp-session",
    "decky-build", "decky-package", "gen-docs",
})


def load_components():
    components, error = ACC.load_components(REPO)
    if error or not components:
        raise SystemExit("cannot load components.yaml: %s" % (error or "empty"))
    return components


def verify_ids(components):
    ids = []
    for component in components:
        for entry in component.get("verify") or []:
            if entry.get("id") and entry["id"] not in ids:
                ids.append(entry["id"])
    return ids


def check_rows(components, state="pass", overrides=None, skip_summary="not installed"):
    overrides = overrides or {}
    rows = []
    for cid in verify_ids(components):
        wanted = overrides.get(cid, state)
        rows.append({
            "id": cid,
            "group": "synthetic",
            "title": cid,
            "state": wanted,
            "summary": skip_summary if wanted == "skip" else "synthetic %s" % wanted,
            "detail": "",
            "remediation": "synthetic fixture",
        })
    return rows


def verify_source(components, state="pass", overrides=None, status="ok", rc=0):
    rows = check_rows(components, state=state, overrides=overrides)
    counts = {"total": len(rows), "pass": 0, "fail": 0, "skip": 0, "unknown": 0}
    for row in rows:
        key = row["state"] if row["state"] in counts else "unknown"
        counts[key] += 1
    return {
        "status": status,
        "rc": rc,
        "duration_ms": 10,
        "argv": ["synthetic", "oxp3-verify.sh", "--json"],
        "error": "" if status == "ok" else "synthetic missing source",
        "data": None if status != "ok" else {
            "schema": 1, "tool": "oxp3-verify", "version": "1.0.0",
            "mode": "default", "root": False, "kernel": "synthetic",
            "counts": counts, "exit_code": 0 if counts["fail"] == 0 else 1,
            "checks": rows,
        },
    }


def health_source(missing=None, status="ok"):
    missing = missing or []
    steps = []
    for entry in missing:
        steps.append({
            "id": entry[0],
            "title": entry[0],
            "state": entry[1],
            "detail": "synthetic",
            "command": "synthetic install command",
        })
    return {
        "status": status,
        "rc": 0 if status == "ok" else 3,
        "duration_ms": 5,
        "argv": ["synthetic", "oxp3-health.sh", "--json", "--no-verify"],
        "error": "" if status == "ok" else "synthetic missing source",
        "data": None if status != "ok" else {
            "schema": 1, "tool": "oxp3-health", "version": "1.0.0",
            "mode": "default", "root": False, "kernel": "synthetic",
            "installed": {"done": 19 - len(missing), "total": 19, "missing": steps},
        },
    }


def repo_test_source(states=None, status="ok"):
    states = states or {}
    suites = []
    for name in DEFAULT_SUITES:
        state = states.get(name, "pass")
        suites.append({
            "name": name, "mode": "fast", "tier": "fast",
            "state": state, "duration_ms": 5,
            "command": "synthetic %s" % name,
            "log": "/dev/null",
            "reason": "" if state == "pass" else "synthetic %s" % state,
        })
    counts = {"total": len(suites), "pass": 0, "fail": 0, "skip": 0}
    for suite in suites:
        counts[suite["state"]] += 1
    return {
        "status": status,
        "rc": 0 if status == "ok" else 3,
        "duration_ms": 5,
        "argv": ["synthetic", "repo-test.sh", "--fast", "--json"],
        "error": "" if status == "ok" else "synthetic missing source",
        "data": None if status != "ok" else {
            "schema": 1, "version": "1.0.0", "mode": "fast",
            "repo": "/synthetic", "log_dir": "/tmp",
            "counts": counts, "exit_code": 0 if counts["fail"] == 0 else 1,
            "suites": suites,
        },
    }


def files_all_present(components):
    files = {}
    for spec in ACC.ITEM_SPECS:
        for rel in spec["offline_files"]:
            files[rel] = True
    for component in components:
        for rel in component.get("implementation") or []:
            files[str(rel)] = True
    return files


def bundle(components, sources, artifacts=None, mode="default"):
    return {
        "schema": 1,
        "tool": "oxp3-acceptance",
        "version": "1.0.0",
        "generated_at": "2026-11-09T00:00:00Z",
        "repo": "/synthetic",
        "kernel": "synthetic",
        "root": False,
        "mode": mode,
        "components": components,
        "components_error": "",
        "files": files_all_present(components),
        "artifacts": artifacts or {
            "sleep_cycles_good": {"found": False, "path": "", "detail": "synthetic"},
            "fingerprint_transcript": {"found": False, "path": "", "detail": "synthetic"},
            "signoffs": {},
        },
        "sources": sources,
    }


def all_green(components):
    green = copy.deepcopy(components)
    for component in green:
        # A future registry in which every item is closed.  Not a claim about
        # this host: this bundle is synthetic by construction.
        component["state"] = "working"
    sources = {
        "verify": verify_source(green, state="pass"),
        "health": health_source([]),
        "repo_test": repo_test_source(),
        "preflight": None,
        "gen_docs": {"status": "ok", "rc": 0, "duration_ms": 5, "detail": ["ok"]},
    }
    artifacts = {
        "sleep_cycles_good": {"found": True, "path": "docs/state/sleep-cycles-synthetic/report.json",
                              "detail": "synthetic GOOD report"},
        "fingerprint_transcript": {"found": True, "path": "docs/state/fp-session-synthetic/summary.json",
                                   "detail": "synthetic verified transcript"},
        "signoffs": {
            "audio.listen": {"by": "synthetic", "date": "2026-11-09", "note": "fixture"},
            "decky.qam": {"by": "synthetic", "date": "2026-11-09", "note": "fixture"},
        },
    }
    return bundle(green, sources, artifacts), {
        "verdicts": {spec["id"]: "implemented-and-proven-offline" for spec in ACC.ITEM_SPECS},
        "complete": 7,
        "automated_complete": 7,
        "exit_code": 0,
    }


def awaiting_install(components):
    missing = [
        ("hw-install", "todo"), ("udev-rules", "todo"), ("sleep-policy", "todo"),
        ("logind-restart", "unknown"), ("sleep-units", "todo"),
        ("bluetooth", "partial"), ("power-profile", "todo"),
        ("rgb-config", "todo"), ("homebtn-unit", "todo"), ("decky-plugin", "todo"),
    ]
    # Audio is user-scope and already installed here, so its checks pass even
    # while the root-installed components are still waiting for their install.
    audio_overrides = {cid: "pass" for cid in (
        "aud.scripts", "aud.server", "aud.eq", "aud.dummy", "aud.model",
    )}
    sources = {
        "verify": verify_source(components, state="skip", overrides=audio_overrides),
        "health": health_source(missing),
        "repo_test": repo_test_source(),
        "preflight": None,
        "gen_docs": None,
    }
    return bundle(components, sources), {
        "verdicts": {
            "volume": "awaiting-install",
            "leds": "awaiting-install",
            "sleep": "awaiting-install",
            "audio": "implemented-and-proven-offline",
            "fingerprint": "awaiting-hardware",
            "decky": "awaiting-install",
            "audit": "blocked",
        },
        "complete": 0,
        "automated_complete": 1,
        "exit_code": 0,
        "no_device_claim": True,
    }


def blocked(components):
    overrides = {
        "bt.journal": "fail", "bt.controller": "fail", "bt.diag": "fail",
    }
    sources = {
        "verify": verify_source(components, state="pass", overrides=overrides, rc=1),
        "health": health_source([]),
        "repo_test": repo_test_source(),
        "preflight": None,
        "gen_docs": None,
    }
    artifacts = {
        "sleep_cycles_good": {"found": False, "path": "", "detail": "synthetic"},
        "fingerprint_transcript": {"found": False, "path": "", "detail": "synthetic"},
        "signoffs": {},
    }
    return bundle(components, sources, artifacts), {
        "verdicts": {"audit": "blocked"},
        # volume and the LEDs are fully proven by their passing device checks;
        # sleep/audio/fingerprint/Decky keep a manual acceptance gate.
        "complete": 2,
        "exit_code": 1,
        "no_device_claim": True,
    }


def missing_evidence(components):
    missing = {
        "status": "missing", "rc": 127, "duration_ms": 0,
        "argv": ["synthetic"], "error": "synthetic missing source", "data": None,
    }
    sources = {
        "verify": dict(missing), "health": dict(missing), "repo_test": dict(missing),
        "preflight": None, "gen_docs": None,
    }
    return bundle(components, sources), {
        "verdicts": {
            "volume": "awaiting-hardware",
            "leds": "awaiting-hardware",
            "sleep": "awaiting-hardware",
            "audio": "awaiting-hardware",
            "fingerprint": "awaiting-hardware",
            "decky": "awaiting-hardware",
            "audit": "blocked",
        },
        "complete": 0,
        "automated_complete": 0,
        "exit_code": 1,
        "no_device_claim": True,
    }


def main():
    components = load_components()
    scenarios = {
        "all-green": all_green,
        "awaiting-install": awaiting_install,
        "blocked": blocked,
        "missing-evidence": missing_evidence,
    }
    for name, builder in scenarios.items():
        data, expect = builder(components if name != "all-green" else components)
        data["expect"] = expect
        path = HERE / (name + ".json")
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("wrote %s (%d bytes)" % (path.name, path.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
