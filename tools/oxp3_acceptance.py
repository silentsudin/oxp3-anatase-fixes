#!/usr/bin/env python3
"""oxp3-acceptance - evaluate the seven goal items from evidence, not memory.

The sleep/volume/LED/fingerprint/Decky work is spread over a component registry,
a verifier, a repository test runner, unit/C/integration suites and a handful of
device-only runs.  "Are we done?" should not depend on remembering which of them
was green last time.  This tool answers exactly that question:

  * it reads the component states, implementation paths, docs and verify check
    ids from ``docs/hw/components.yaml`` (never a copy), and cites the source;
  * it runs the cheapest live evidence once per source - ``oxp3-verify.sh
    --json``, ``oxp3-health.sh --json --no-verify`` and the relevant
    ``tools/repo-test.sh --fast`` suites - and nothing else;
  * it derives one structured record per goal item: what was implemented, which
    automated checks prove which part, what still needs the device (with the
    exact manual step and command), and the current verdict.

Verdict vocabulary (closed set; documented in docs/ACCEPTANCE.md)
    implemented-and-proven-offline
    awaiting-install
    awaiting-hardware
    blocked
    not-a-gap

The verdict is the *furthest evidence stage reached*, not a promise:

  * ``implemented-and-proven-offline`` means the implementation and every
    offline suite we have are present and green.  It never means "proven on the
    device": the report prints the device-check count separately and keeps a
    per-item ``complete`` flag that also needs the device evidence and the
    manual acceptance gate.  There is no "proven" claim for offline-only
    evidence anywhere in the output.
  * ``awaiting-install`` means an install step is still open, so no device
    check can pass yet.
  * ``awaiting-hardware`` means the code is in place but the missing proof
    needs the device (a real press, a real suspend, a fingerprint transcript).
  * ``blocked`` means a component is blocked in the registry or a live required
    check FAILs; ``not-a-gap`` is used only when components.yaml says the
    investigation found no gap.

Read-only by contract: the verifier, health front-end and repo-test runner are
run in their documented read-only modes; the only files this tool may write are
the ``--save`` bundle and, when asked, a sign-off file is *read* (never written).

    tools/oxp3-acceptance.sh                 evaluate now, human report
    tools/oxp3-acceptance.sh --json          machine report (schema 1)
    tools/oxp3-acceptance.sh --verbose       every mapped check, not only proof
    tools/oxp3-acceptance.sh --full          also the full-only suites + gen-docs
    tools/oxp3-acceptance.sh --save FILE     save the raw evidence bundle
    tools/oxp3-acceptance.sh --evidence FILE evaluate a saved bundle (no live run)
    tools/oxp3-acceptance.sh --selftest      offline aggregation tests (fixtures)
    tools/oxp3-acceptance.sh -h|--help

Exit: 0 = all automated evidence green (device/manual work may remain),
1 = a required check/suite failed or an evidence source is missing,
2 = usage, 3 = no evidence source could run.  Docs: docs/ACCEPTANCE.md.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

PROG = "oxp3-acceptance"
VERSION = "1.0.0"
SCHEMA = 1

VERDICTS = (
    "implemented-and-proven-offline",
    "awaiting-install",
    "awaiting-hardware",
    "blocked",
    "not-a-gap",
)
VERDICT_RANK = {name: index for index, name in enumerate(VERDICTS)}

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "acceptance"
SIGN_OFF_FILE = REPO / "docs" / "state" / "acceptance-signoffs.json"

# The seven goal items.  Only the *item* facts live here; the component state,
# implementation paths, doc link and verify ids are read from components.yaml.
ITEM_SPECS = (
    {
        "id": "volume",
        "title": "Sticky volume buttons",
        "components": ["volume"],
        # The volume install writes the libexec helper and its own hwdb; the
        # udev-rules firstrun step bundles the LED/sleep rules, so requiring it
        # here would keep the volume item "awaiting-install" after a volume-only
        # install that is in fact complete.
        "install_steps": ["hw-install"],
        "required_checks": ["vol.unit", "vol.grab", "vol.hold"],
        "offline_suites": ["unit", "verify-selftest"],
        "optional_suites": ["voltap-integration"],
        "offline_files": [
            "tests/unit/test_voltap.py",
            "tests/integration/volume/run.sh",
            "docs/hw/volume-buttons.md",
        ],
        "manual": [
            {
                "id": "volume.install",
                "phase": "install",
                "step": "install the standalone daemon and the force-release hwdb "
                        "(never combine with the hhd volume patch)",
                "command": "sudo tools/oxp3-hw-install.sh --volume",
            },
            {
                "id": "volume.hold",
                "phase": "acceptance",
                "step": "a synthetic hold must yield exactly one step, not a ramp",
                "command": "sudo tools/oxp3-verify.sh --full   # vol.hold",
                "satisfied_by": ["vol.hold"],
            },
            {
                "id": "volume.integration",
                "phase": "acceptance",
                "step": "opt-in end-to-end daemon run against a virtual uinput device",
                "command": "OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only voltap-integration",
                "satisfied_by_suite": "voltap-integration",
                "optional": True,
            },
            {
                "id": "volume.physical",
                "phase": "acceptance",
                "step": "hold the physical rocker for ~3 s; the volume moves one step, "
                        "and no key repeats after release",
                "command": "journalctl -b -u oxp3-voltap --no-pager | tail -20",
                "satisfied_by": ["vol.grab"],
                "optional": True,
            },
        ],
        "done_when": "vol.unit, vol.grab and vol.hold PASS after a real install; one "
                     "physical hold steps once; the opt-in integration suite is green.",
    },
    {
        "id": "leds",
        "title": "Home button and top RGB LEDs",
        "components": ["leds", "rgb"],
        "install_steps": ["udev-rules", "homebtn-unit", "rgb-config"],
        "required_checks": [
            "hid.rule", "hid.access", "homebtn.unit", "homebtn.mapping",
            "homebtn.press", "led.write", "rgbd.unit",
        ],
        "offline_suites": ["led-selftest", "hid-selftest", "unit", "verify-selftest"],
        "optional_suites": [],
        "offline_files": [
            "tests/unit/test_led.py",
            "tests/unit/test_hid.py",
            "tests/unit/test_rgbd.py",
            "tests/unit/test_homebtn_lifecycle.py",
            "docs/hw/home-button-leds.md",
            "docs/hw/rgb-frames-2026-11-09.txt",
        ],
        "manual": [
            {
                "id": "leds.install",
                "phase": "install",
                "step": "install the shared HID rule, the home-button unit and the RGB config",
                "command": "sudo tools/oxp3-hw-install.sh --leds --rgb && "
                           "systemctl --user enable --now oxp3-homebtn.service oxp3-rgbd.service",
            },
            {
                "id": "leds.press",
                "phase": "acceptance",
                "step": "press the home button once so the daemon decodes a real press",
                "command": "journalctl --user -b -u oxp3-homebtn --no-pager | tail",
                "satisfied_by": ["homebtn.press", "homebtn.unit"],
            },
            {
                "id": "leds.write",
                "phase": "acceptance",
                "step": "one solid frame must be accepted by the top strip",
                "command": "sudo tools/oxp3-verify.sh --full   # led.write",
                "satisfied_by": ["led.write"],
            },
        ],
        "done_when": "all leds/rgb checks PASS on the device, including one accepted "
                     "LED frame and one decoded home-button press.",
    },
    {
        "id": "sleep",
        "title": "Sleep efficiency",
        "components": ["sleep"],
        "install_steps": ["sleep-policy", "logind-restart", "sleep-units"],
        "required_checks": [
            "sleep.mem", "sleep.units", "sleep.wakeup", "sleep.trigger",
            "sleep.stats",
        ],
        "offline_suites": ["sleep-cycles", "unit", "verify-selftest"],
        "optional_suites": [],
        "offline_files": [
            "tests/unit/test_sleep_cycles.py",
            "tests/unit/test_sleep_trigger.py",
            "docs/SLEEP-TEST.md",
            "tests/fixtures/sleep-cycles",
        ],
        "manual": [
            {
                "id": "sleep.install",
                "phase": "install",
                "step": "install the sleep trigger policy and units, then restart "
                        "systemd-logind (or reboot) so the drop-in is read",
                "command": "sudo tools/oxp3-hw-install.sh --sleep && "
                           "sudo systemctl restart systemd-logind",
            },
            {
                "id": "sleep.good-cycle",
                "phase": "acceptance",
                "step": "one real cycle run on battery must print verdict GOOD "
                        "(drain < 1 %/h, no unexpected wake source)",
                "command": "sudo tools/sleep-cycle-test.sh --cycles 3",
                "satisfied_by_artifact": "sleep_cycles_good",
            },
            {
                "id": "sleep.suspend",
                "phase": "acceptance",
                "step": "one rtcwake s2idle cycle with suspend_stats/success incremented",
                "command": "sudo OXP3_ALLOW_SUSPEND=1 tools/oxp3-verify.sh --full --with-changes",
                "satisfied_by": ["sleep.stats"],
            },
        ],
        "done_when": "sleep.trigger PASS plus one clean sleep-cycle-test run with a "
                     "GOOD verdict (or sleep.suspend PASS with success>0).",
    },
    {
        "id": "audio",
        "title": "Speaker audio (Harman APO)",
        "components": ["audio"],
        "install_steps": [],
        "required_checks": [
            "aud.scripts", "aud.server", "aud.eq", "aud.dummy", "aud.model",
        ],
        "offline_suites": ["eq-model", "unit"],
        "optional_suites": ["audio-eq-isolation"],
        "offline_files": [
            "tests/unit/test_eq_measure.py",
            "docs/audio-validation.md",
            "tools/audio/test-isolation.sh",
        ],
        "manual": [
            {
                "id": "audio.listen",
                "phase": "acceptance",
                "step": "listen to a known track on the internal speakers once with the "
                        "EQ sink loaded (and toggle eq-off.sh to hear the difference)",
                "command": "tools/audio/eq-on.sh && tools/audio/eq-off.sh",
                "satisfied_by_signoff": True,
            },
        ],
        "done_when": "the EQ sink is the default and loaded, the model matches, and one "
                     "owner listening check is recorded (the model cannot hear the drivers).",
    },
    {
        "id": "fingerprint",
        "title": "Fingerprint reader",
        "components": ["fingerprint"],
        "install_steps": [],
        "required_checks": [],
        "offline_suites": ["fp-python", "fp-crypto"],
        "optional_suites": ["fp-c-tests", "fp-session"],
        "offline_files": [
            "tools/fingerprint/tests",
            "tools/fingerprint/oxp3-fp-session.sh",
            "docs/hw/fingerprint.md",
            "docs/hw/fingerprint-session.md",
        ],
        "manual": [
            {
                "id": "fingerprint.transcript",
                "phase": "acceptance",
                "step": "one guided root session against the sensor, with a transcript "
                        "(probe.ran and restore.verified) handed back",
                "command": "sudo tools/fingerprint/oxp3-fp-session.sh",
                "satisfied_by_artifact": "fingerprint_transcript",
            },
            {
                "id": "fingerprint.enrol",
                "phase": "acceptance",
                "step": "enrol/verify and a kernel driver remain unimplemented "
                        "(components.yaml state: partial) - out of scope for this transcript",
                "command": "docs/hw/fingerprint.md",
                "optional": True,
            },
        ],
        "done_when": "one fingerprint transcript with probe.ran and restore.verified; "
                     "the driver/enrol gap stays recorded as partial, not claimed.",
    },
    {
        "id": "decky",
        "title": "Decky status plugin",
        "components": ["decky"],
        "install_steps": ["decky-plugin"],
        "required_checks": ["decky.dir", "decky.backend", "decky.status"],
        "offline_suites": ["decky-selftest", "decky-cli"],
        "optional_suites": ["decky-build", "decky-package"],
        "offline_files": [
            "tools/decky-oxp3-status",
            "docs/decky-plugin.md",
        ],
        "manual": [
            {
                "id": "decky.install",
                "phase": "install",
                "step": "build the packaged plugin and copy it into the Decky homebrew "
                        "plugins directory",
                "command": "tools/decky-oxp3-status/build.sh --offline && "
                           "cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status",
            },
            {
                "id": "decky.qam",
                "phase": "acceptance",
                "step": "open the Quick Access Menu in a real Decky session and confirm "
                        "the OXP3 panel loads and refreshes",
                "command": "journalctl -b -u plugin_loader.service --no-pager | tail",
                "satisfied_by_signoff": True,
            },
        ],
        "done_when": "decky.dir/backend/status PASS and the plugin is seen loaded in a "
                     "real Decky session (QAM panel visible, Refresh works).",
    },
    {
        "id": "audit",
        "title": "Wider hardware/compatibility audit and further gaps",
        "components": [
            "gamescope", "mesa", "xess-xmx", "mangohud", "kernel-modules",
            "firmware", "rapl-power", "sched-ext", "bluetooth", "power", "fan",
            "battery", "adaptive-tdp", "sagv", "display", "imu", "camera",
            "daemons",
        ],
        "install_steps": ["bluetooth", "power-profile"],
        "required_checks": [
            "gfx.mesa", "gfx.working", "gfx.xe", "pwr.zones", "pwr.pl1",
            "bat.threshold", "dsp.panel", "dsp.hdr", "bt.files", "bt.journal",
            "bt.controller",
        ],
        "offline_suites": ["triage", "verify-selftest", "unit"],
        "optional_suites": ["gen-docs"],
        "offline_files": [
            "docs/hw/gap-audit.md",
            "docs/state/audit-2026-11-09.txt",
            "docs/hw/components.yaml",
        ],
        "manual": [
            {
                "id": "audit.bluetooth",
                "phase": "install",
                "step": "put the pinned firmware into the initramfs and reboot; the "
                        "controller must appear",
                "command": "sudo tools/oxp3-bt-firmware.sh   # then reboot",
            },
            {
                "id": "audit.scheduler",
                "phase": "install",
                "step": "opt-in: scx_cosmos is only installed by the fast installer when present",
                "command": "./oxp3-fixes-fast.sh install   # or: sudo tools/oxp3-hw-install.sh --all",
            },
            {
                "id": "audit.sagv",
                "phase": "acceptance",
                "step": "SAGV/QCLK has no Linux control (components.yaml state: blocked); "
                        "track upstream/kernel work, do not claim it",
                "command": "tools/oxp3-sagv.sh status",
                "optional": True,
            },
            {
                "id": "audit.flatpak-xmx",
                "phase": "acceptance",
                "step": "Flatpak Steam XMX environment is deployed but not trace-verified",
                "command": "tools/oxp3-flatpak-xmx.sh status --probe-sandbox",
                "optional": True,
            },
        ],
        "done_when": "every registry gap is either closed (Bluetooth controller, opt-in "
                     "scheduler, power profile) or explicitly accepted as upstream work "
                     "(SAGV); no component is blocked without a written reason.",
    },
)

# Suites that exercise repository-wide health rather than one item's proof.  A
# failure there is reported, but it does not turn an unrelated item's targeted
# evidence (e.g. the EQ model for audio) into a FAIL.
SHARED_SUITES = {"unit", "verify-selftest"}

DEFAULT_SUITES = [
    "unit", "verify-selftest", "triage", "fp-python", "fp-crypto",
    "decky-selftest", "decky-cli", "led-selftest", "hid-selftest",
    "eq-model", "sleep-cycles",
]
FULL_SUITES = DEFAULT_SUITES + [
    "fp-c-tests", "fp-session", "decky-build", "decky-package",
    "audio-eq-isolation", "gen-docs",
]
SOURCE_TIMEOUT = {"verify": 120, "health": 60, "repo_test": 420, "gen_docs": 180}


# --------------------------------------------------------------------- helpers
def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(argv, timeout, cwd=REPO):
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        rc = 124
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = "timed out after %ss" % timeout
    except OSError as exc:
        rc = 127
        out, err = "", str(exc)
    return {
        "argv": [str(a) for a in argv],
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout": out,
        "stderr": err,
    }


def _parse_json(raw):
    text = (raw or "").strip()
    if not text:
        return None, "empty stdout"
    try:
        return json.loads(text), ""
    except ValueError as exc:
        return None, "stdout is not valid JSON: %s" % str(exc)[:160]


def _source_from_run(name, run, accepted=(0, 1, 2)):
    data, error = _parse_json(run.get("stdout"))
    status = "ok"
    if run["rc"] == 124:
        status, error = "timeout", run["stderr"] or "timed out"
    elif run["rc"] == 127:
        status, error = "missing", run["stderr"]
    elif run["rc"] not in accepted:
        status, error = "error", "exit %s: %s" % (run["rc"], (run["stderr"] or "")[:160])
    elif data is None:
        status = "error"
    return {
        "status": status,
        "rc": run["rc"],
        "duration_ms": run["duration_ms"],
        "argv": run["argv"],
        "error": error,
        "data": data,
    }


def load_components(repo=REPO):
    """Parse docs/hw/components.yaml through gen-docs' own parser.

    Returns (components, error).  Using the generator's parser keeps one YAML
    dialect and one interpretation of the file; nothing is copied or re-typed.
    """
    path = pathlib.Path(repo) / "docs" / "hw" / "components.yaml"
    loader = pathlib.Path(repo) / "tools" / "gen-docs.py"
    if not path.is_file():
        return [], "docs/hw/components.yaml is missing"
    if not loader.is_file():
        return [], "tools/gen-docs.py is missing"
    try:
        spec = importlib.util.spec_from_file_location("oxp3_gen_docs_acceptance", str(loader))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        raw = module.parse_yaml(path.read_text(encoding="utf-8"))
        components = raw.get("components") or []
        if not isinstance(components, list) or not components:
            return [], "components.yaml contains no components"
        return components, ""
    except Exception as exc:  # noqa: BLE001 - defensive: report, never crash
        return [], "cannot parse components.yaml: %s" % str(exc)[:200]


def _artifact_scan(repo=REPO):
    """Detect the two filesystem evidence artefacts and the optional sign-offs."""
    artifacts = {
        "sleep_cycles_good": {"found": False, "path": "", "detail": "no measured GOOD report"},
        "fingerprint_transcript": {"found": False, "path": "", "detail": "no session summary"},
        "signoffs": {},
    }
    state = pathlib.Path(repo) / "docs" / "state"
    for report in sorted(state.glob("sleep-cycles-*/report.json")):
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        verdict = (data.get("verdict") or {}).get("label") or ""
        valid = int((data.get("summary") or {}).get("valid_cycles") or 0)
        mode = str(data.get("mode") or "")
        if verdict == "GOOD" and valid > 0 and mode not in ("no-suspend", "dry-run"):
            artifacts["sleep_cycles_good"] = {
                "found": True,
                "path": str(report.relative_to(repo)),
                "detail": "verdict GOOD, %d valid cycle(s)" % valid,
            }
            break
    for summary in sorted(state.glob("fp-session-*/summary.json")):
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        probe = data.get("probe") or {}
        restore = data.get("restore") or {}
        if probe.get("ran") and restore.get("verified"):
            artifacts["fingerprint_transcript"] = {
                "found": True,
                "path": str(summary.relative_to(repo)),
                "detail": "probe.ran and restore.verified",
            }
            break
    sign = pathlib.Path(repo) / "docs" / "state" / "acceptance-signoffs.json"
    if sign.is_file():
        try:
            data = json.loads(sign.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                artifacts["signoffs"] = data
        except (OSError, ValueError):
            artifacts["signoffs"] = {}
    return artifacts


def collect(repo=REPO, full=False, with_gen_docs=None):
    """Run the cheapest evidence sources once; always return a bundle."""
    repo = pathlib.Path(repo)
    if with_gen_docs is None:
        with_gen_docs = full
    components, components_error = load_components(repo)
    suites = FULL_SUITES if full else DEFAULT_SUITES

    verify = _source_from_run(
        "verify",
        _run([repo / "tools" / "oxp3-verify.sh", "--json"], SOURCE_TIMEOUT["verify"], repo),
    )
    health = _source_from_run(
        "health",
        _run(
            [repo / "tools" / "oxp3-health.sh", "--json", "--no-verify"],
            SOURCE_TIMEOUT["health"],
            repo,
        ),
        accepted=(0, 1, 2, 3),
    )
    repo_test = _source_from_run(
        "repo_test",
        _run(
            [repo / "tools" / "repo-test.sh", "--full" if full else "--fast",
             "--json"]
            + [arg for name in suites for arg in ("--only", name)],
            SOURCE_TIMEOUT["repo_test"],
            repo,
        ),
    )
    preflight = None
    if health["status"] != "ok" or not health.get("data"):
        preflight = _source_from_run(
            "preflight",
            _run([repo / "tools" / "oxp3-preflight.sh", "--json"], SOURCE_TIMEOUT["health"], repo),
        )
    gen_docs = None
    if with_gen_docs:
        run = _run(
            [sys.executable or "python3", repo / "tools" / "gen-docs.py", "--check"],
            SOURCE_TIMEOUT["gen_docs"],
            repo,
        )
        gen_docs = {
            "status": "ok" if run["rc"] == 0 else "error",
            "rc": run["rc"],
            "duration_ms": run["duration_ms"],
            "detail": (run["stdout"] or "").strip().splitlines()[-1:] or [""],
        }

    files = {}
    for spec in ITEM_SPECS:
        for rel in spec["offline_files"]:
            path = repo / rel
            files[rel] = path.exists()
    for component in components:
        for rel in component.get("implementation") or []:
            path = repo / str(rel)
            files[str(rel)] = path.exists() if "*" not in str(rel) else any(repo.glob(str(rel)))

    return {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "generated_at": _now(),
        "repo": str(repo),
        "kernel": os.uname().release if hasattr(os, "uname") else "",
        "root": (os.geteuid() == 0) if hasattr(os, "geteuid") else False,
        "mode": "full" if full else "default",
        "components": components,
        "components_error": components_error,
        "files": files,
        "artifacts": _artifact_scan(repo),
        "sources": {
            "verify": verify,
            "health": health,
            "repo_test": repo_test,
            "preflight": preflight,
            "gen_docs": gen_docs,
        },
    }


# ------------------------------------------------------------------ evaluation
def _component_index(components):
    return {str(c.get("id")): c for c in components or [] if isinstance(c, dict)}


def _check_index(verify):
    rows = {}
    data = (verify or {}).get("data") or {}
    for check in data.get("checks") or []:
        if isinstance(check, dict) and check.get("id"):
            rows[str(check["id"])] = check
    return rows


def _suite_index(repo_test):
    rows = {}
    data = (repo_test or {}).get("data") or {}
    for suite in data.get("suites") or []:
        if isinstance(suite, dict) and suite.get("name"):
            rows[str(suite["name"])] = suite
    return rows


def _install_index(health):
    missing = {}
    data = (health or {}).get("data") or {}
    steps = ((data.get("installed") or {}).get("missing"))
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("id"):
                missing[str(step["id"])] = str(step.get("state") or "unknown")
    return missing


def _row_state(row, default="missing"):
    return str((row or {}).get("state") or default).lower()


def _manual_satisfied(entry, checks, suites, artifacts, signoffs):
    if entry.get("optional"):
        return True
    if entry.get("satisfied_by_signoff"):
        return bool(signoffs.get(entry["id"]))
    if entry.get("satisfied_by_artifact"):
        return bool((artifacts.get(entry["satisfied_by_artifact"]) or {}).get("found"))
    if entry.get("satisfied_by_suite"):
        return _row_state(suites.get(entry["satisfied_by_suite"], {})) == "pass"
    ids = entry.get("satisfied_by") or []
    if ids:
        return all(_row_state(checks.get(cid), "missing") == "pass" for cid in ids)
    return False


def _mapped_checks(component):
    ids, commands = [], []
    for entry in component.get("verify") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id"):
            ids.append(str(entry["id"]))
        elif entry.get("command"):
            commands.append(entry)
    return ids, commands


def evaluate(bundle):
    """Pure aggregation over an evidence bundle; no host access."""
    components = bundle.get("components") or []
    components_error = str(bundle.get("components_error") or "")
    comp_index = _component_index(components)
    sources = bundle.get("sources") or {}
    verify = sources.get("verify") or {}
    health = sources.get("health") or {}
    repo_test = sources.get("repo_test") or {}
    gen_docs = sources.get("gen_docs") or None
    checks = _check_index(verify)
    suites = _suite_index(repo_test)
    install = _install_index(health)
    health_ok = bool((health or {}).get("data"))
    files = bundle.get("files") or {}
    artifacts = bundle.get("artifacts") or {}
    signoffs = (artifacts.get("signoffs") or {}) if isinstance(artifacts, dict) else {}

    report = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "generated_at": bundle.get("generated_at") or _now(),
        "evaluated_at": _now(),
        "mode": bundle.get("mode") or "default",
        "kernel": bundle.get("kernel") or "",
        "root": bool(bundle.get("root")),
        "verdict_vocabulary": list(VERDICTS),
        "sources": {
            "verify": {
                "status": verify.get("status") or "missing",
                "rc": verify.get("rc"),
                "counts": ((verify.get("data") or {}).get("counts") or {}),
                "failing": [
                    str(check.get("id"))
                    for check in (((verify.get("data") or {}).get("checks") or []))
                    if str(check.get("state")) == "fail"
                ],
                "error": verify.get("error") or "",
            },
            "health": {
                "status": health.get("status") or "missing",
                "installed": ((health.get("data") or {}).get("installed") or {}),
                "error": health.get("error") or "",
            },
            "repo_test": {
                "status": repo_test.get("status") or "missing",
                "counts": ((repo_test.get("data") or {}).get("counts") or {}),
                "error": repo_test.get("error") or "",
            },
            "components": {
                "status": "ok" if components and not components_error else "missing",
                "path": "docs/hw/components.yaml",
                "count": len(components),
                "error": components_error,
            },
            "gen_docs": gen_docs,
            "artifacts": artifacts,
        },
        "items": [],
        "missing_evidence": [],
        "counts": {},
        "exit_code": 0,
    }
    if not components:
        report["missing_evidence"].append(
            components_error or "docs/hw/components.yaml could not be read"
        )
    source_missing = [
        name for name in ("verify", "health", "repo_test")
        if (sources.get(name) or {}).get("status") != "ok"
    ]
    for name in source_missing:
        report["missing_evidence"].append("evidence source unavailable: %s" % name)

    for spec in ITEM_SPECS:
        record = {
            "id": spec["id"],
            "title": spec["title"],
            "component_states": [],
            "implementation": [],
            "entrypoint": "",
            "docs": [],
            "mapped_checks": [],
            "proof": [],
            "offline": [],
            "device": {"required": [], "passed": 0, "total": 0, "complete": False},
            "manual": [],
            "missing_evidence": [],
            "done_when": spec["done_when"],
            "verdict": "awaiting-hardware",
            "complete": False,
            "reason": "",
        }
        mapped_ids = []
        for cid in spec["components"]:
            comp = comp_index.get(cid)
            if not comp:
                record["missing_evidence"].append(
                    "component %s is not in docs/hw/components.yaml" % cid
                )
                continue
            record["component_states"].append(
                {"id": cid, "state": comp.get("state"), "doc": comp.get("doc"),
                 "summary": str(comp.get("summary") or ""),
                 "cited": "docs/hw/components.yaml#" + cid}
            )
            if comp.get("state") == "partial":
                record.setdefault("implementation_gaps", []).append(
                    {"component": cid, "summary": str(comp.get("summary") or "")}
                )
            for rel in comp.get("implementation") or []:
                if str(rel) not in record["implementation"]:
                    record["implementation"].append(str(rel))
            entry = comp.get("install") or {}
            if entry.get("entrypoint") and not record["entrypoint"]:
                record["entrypoint"] = str(entry["entrypoint"])
            if comp.get("doc") and comp["doc"] not in record["docs"]:
                record["docs"].append(str(comp["doc"]))
            ids, commands = _mapped_checks(comp)
            for cid2 in ids:
                if cid2 not in mapped_ids:
                    mapped_ids.append(cid2)
            for command in commands:
                record["proof"].append({
                    "kind": "command",
                    "command": str(command.get("command") or ""),
                    "expect": str(command.get("expect") or ""),
                    "state": "documented",
                })
        # verify rows
        for cid in mapped_ids:
            row = checks.get(cid)
            state = _row_state(row, "missing")
            record["mapped_checks"].append({
                "id": cid,
                "state": state,
                "summary": str((row or {}).get("summary") or ""),
                "detail": str((row or {}).get("detail") or ""),
                "remediation": str((row or {}).get("remediation") or ""),
            })
            if state in ("pass", "fail"):
                record["proof"].append({
                    "kind": "verify", "id": cid, "state": state,
                    "summary": str((row or {}).get("summary") or ""),
                })
        # offline suites
        suite_names = list(spec["offline_suites"]) + list(spec.get("optional_suites") or [])
        for name in suite_names:
            row = suites.get(name)
            state = _row_state(row, "not-run")
            optional = name in (spec.get("optional_suites") or [])
            shared = name in SHARED_SUITES and spec["id"] != "audit"
            record["offline"].append({
                "kind": "suite", "name": name, "state": state,
                "duration_ms": (row or {}).get("duration_ms"),
                "reason": str((row or {}).get("reason") or ""),
                "optional": optional, "shared": shared,
            })
            if optional and state == "not-run":
                continue
            if state == "missing":
                record["missing_evidence"].append("suite not in repo-test report: %s" % name)
            elif state == "fail":
                record["missing_evidence"].append("offline suite FAIL: %s" % name)
        # offline files
        for rel in spec["offline_files"]:
            present = files.get(rel)
            record["offline"].append({
                "kind": "file", "name": rel,
                "state": "present" if present else ("missing" if present is False else "unknown"),
                "optional": False,
            })
            if present is False:
                record["missing_evidence"].append("offline evidence file missing: %s" % rel)
        # device proof
        required = list(spec["required_checks"])
        record["device"]["required"] = required
        record["device"]["total"] = len(required)
        record["device"]["passed"] = sum(
            1 for cid in required if _row_state(checks.get(cid), "missing") == "pass"
        )
        record["device"]["complete"] = bool(required) and (
            record["device"]["passed"] == record["device"]["total"]
        )
        # manual entries
        for entry in spec.get("manual") or []:
            satisfied = _manual_satisfied(entry, checks, suites, artifacts, signoffs)
            record["manual"].append({
                "id": entry["id"],
                "phase": entry.get("phase") or "acceptance",
                "step": entry["step"],
                "command": entry["command"],
                "optional": bool(entry.get("optional")),
                "satisfied": satisfied,
            })
        acceptance_manual = [
            entry for entry in record["manual"]
            if entry["phase"] == "acceptance" and not entry["optional"]
        ]
        manual_pending = [entry["id"] for entry in acceptance_manual if not entry["satisfied"]]
        if not required:
            # No check can prove this item, so the device stage is complete only
            # when its recorded acceptance evidence (a transcript, a sign-off)
            # exists - never just because the offline suites pass.
            record["device"]["complete"] = bool(acceptance_manual) and not manual_pending
        # install state
        install_bad = []
        for step in spec["install_steps"]:
            state = install.get(step)
            if state is None:
                continue
            if state not in ("ok", "n/a", "done"):
                install_bad.append({"id": step, "state": state})
        if not health_ok and install_bad == []:
            pass  # no install evidence: never inferred, only reported below
        failed = [c["id"] for c in record["mapped_checks"] if c["state"] == "fail"]
        failed_suites = [s["name"] for s in record["offline"]
                         if s["kind"] == "suite" and not s["optional"]
                         and not s.get("shared") and s["state"] == "fail"]
        shared_suite_failures = [s["name"] for s in record["offline"]
                                 if s["kind"] == "suite" and s.get("shared")
                                 and s["state"] == "fail"]
        states = [entry["state"] for entry in record["component_states"]]
        verdict = ""
        if states and all(state == "not-a-gap" for state in states):
            verdict = "not-a-gap"
        elif "blocked" in states:
            verdict = "blocked"
            record["reason"] = "components.yaml marks %s blocked" % ", ".join(
                entry["id"] for entry in record["component_states"]
                if entry["state"] == "blocked"
            )
            if failed or failed_suites:
                record["reason"] += "; live evidence FAIL: %s" % ", ".join(
                    failed + failed_suites
                )
        elif failed or failed_suites:
            verdict = "blocked"
            record["reason"] = "live required evidence failed: %s" % ", ".join(
                failed + failed_suites
            )
        elif install_bad:
            verdict = "awaiting-install"
            record["reason"] = "install step(s) still open: %s" % ", ".join(
                "%s=%s" % (entry["id"], entry["state"]) for entry in install_bad
            )
        elif not record["component_states"]:
            # Without the registry there is no cited state or implementation to
            # stand on: the offline rung is never claimed from check rows alone.
            verdict = "awaiting-hardware"
            record["reason"] = (
                "component state unavailable (docs/hw/components.yaml); "
                "offline proof cannot be cited"
            )
        elif record["device"]["complete"]:
            verdict = "implemented-and-proven-offline"
            record["reason"] = (
                "implementation and offline evidence in place; %d/%d device checks PASS"
                % (record["device"]["passed"], record["device"]["total"])
            )
        else:
            verdict = "awaiting-hardware"
            pending = [cid for cid in required
                       if _row_state(checks.get(cid), "missing") != "pass"]
            if not required:
                record["reason"] = "no automated device check covers this item"
            elif not health_ok and not checks:
                record["reason"] = "no live verify evidence; device proof cannot be judged"
            else:
                record["reason"] = "device check(s) pending: %s" % ", ".join(pending)
        record["verdict"] = verdict
        if verdict == "not-a-gap":
            record["complete"] = True
        else:
            record["complete"] = (
                verdict == "implemented-and-proven-offline"
                and record["device"]["complete"]
            )
        if manual_pending:
            record["complete"] = False
            record["reason"] += "; manual acceptance pending: %s" % ", ".join(manual_pending)
        if shared_suite_failures:
            record["reason"] += "; repository-wide suite FAIL (not item-specific): %s" % ", ".join(
                shared_suite_failures)
            report["shared_suite_failures"] = sorted(
                set(report.get("shared_suite_failures") or []) | set(shared_suite_failures))
        if record["missing_evidence"]:
            record["reason"] += "; missing evidence: %s" % ", ".join(record["missing_evidence"])
        report["items"].append(record)

    by_verdict = {name: 0 for name in VERDICTS}
    for item in report["items"]:
        by_verdict[item["verdict"]] = by_verdict.get(item["verdict"], 0) + 1
    offline_ready = 0
    for item in report["items"]:
        required_suites = [
            entry for entry in item["offline"]
            if entry["kind"] == "suite" and not entry["optional"] and not entry.get("shared")
        ]
        required_files = [
            entry for entry in item["offline"]
            if entry["kind"] == "file" and not entry["optional"]
        ]
        if (all(e["state"] == "pass" for e in required_suites)
                and all(e["state"] != "missing" for e in required_files)):
            offline_ready += 1
    report["counts"] = {
        "items": len(report["items"]),
        "complete": sum(1 for item in report["items"] if item["complete"]),
        "by_verdict": by_verdict,
        "offline_ready": offline_ready,
        "automated_complete": sum(
            1 for item in report["items"]
            if item["verdict"] in ("implemented-and-proven-offline", "not-a-gap")
            and item["device"]["complete"]
        ),
    }
    # "Remaining" is derived from the records, never retyped.
    remaining = []
    for item in report["items"]:
        for entry in item["manual"]:
            if entry["phase"] == "acceptance" and not entry["satisfied"]:
                remaining.append({
                    "item": item["id"], "id": entry["id"],
                    "step": entry["step"], "command": entry["command"],
                })
    report["remaining"] = remaining
    report["verdict"] = (
        "attention" if any(item["verdict"] == "blocked" for item in report["items"])
        else ("pending" if any(item["verdict"].startswith("awaiting") for item in report["items"])
              else "ok")
    )
    report["exit_code"] = 0
    blocking = [
        cid for cid, row in checks.items()
        if _row_state(row) == "fail"
    ]
    suite_failures = [
        suite.get("name") for suite in (suites or {}).values()
        if str(suite.get("state")) == "fail"
    ]
    if report["missing_evidence"] or blocking or suite_failures:
        report["exit_code"] = 1
    if not components and not checks and not suites:
        report["exit_code"] = 3
    return report


# ------------------------------------------------------------------- rendering
def _fmt_source(name, source):
    """Format a source summary as emitted by evaluate() (counts, not raw data)."""
    source = source or {}
    status = source.get("status") or "missing"
    if status != "ok":
        return "%s=%s" % (name, status)
    if name == "verify":
        counts = source.get("counts") or {}
        return "verify=ok(%s checks: %s pass/%s fail/%s skip)" % (
            counts.get("total"), counts.get("pass"), counts.get("fail"), counts.get("skip")
        )
    if name == "health":
        installed = source.get("installed") or {}
        return "health=ok(%s/%s install steps)" % (installed.get("done"), installed.get("total"))
    if name == "repo_test":
        counts = source.get("counts") or {}
        return "repo-test=ok(%s/%s suites pass)" % (counts.get("pass"), counts.get("total"))
    return "%s=ok" % name


def render_text(report, verbose=False):
    lines = []
    counts = report["counts"]
    by = counts["by_verdict"]
    lines.append(
        "%s %s  mode=%s  kernel=%s  root=%s"
        % (PROG, report["version"], report["mode"], report["kernel"] or "?",
           "yes" if report["root"] else "no")
    )
    sources = report["sources"]
    lines.append(
        "sources: "
        + "  ".join(
            _fmt_source(name, sources.get(name) or {})
            for name in ("verify", "health", "repo_test")
        )
    )
    gen_docs = sources.get("gen_docs")
    if gen_docs:
        lines.append("         gen-docs=%s (docs/hw generated blocks)" % gen_docs.get("status"))
    lines.append("verdicts: " + " | ".join(VERDICTS))
    lines.append("")
    for index, item in enumerate(report["items"], 1):
        states = ", ".join(
            "%s=%s" % (entry["id"], entry["state"]) for entry in item["component_states"]
        ) or "component state unavailable"
        lines.append("[%s] %d. %s" % (item["verdict"], index, item["title"]))
        lines.append("        state(cited): %s   docs: %s" % (
            states, ", ".join(item["docs"]) or "docs/hw/components.yaml"))
        if item["verdict"] not in ("implemented-and-proven-offline", "not-a-gap"):
            lines.append("        why: %s" % item["reason"])
        if item["implementation"]:
            lines.append("        implemented: %s" % "; ".join(item["implementation"][:6]))
        if item.get("implementation_gaps"):
            for gap in item["implementation_gaps"][:2]:
                lines.append("        registry gap (%s, partial): %s"
                             % (gap["component"], gap["summary"][:150]))
        if item["entrypoint"]:
            lines.append("        install entry: %s" % item["entrypoint"])
        proof = item["proof"]
        offline = [entry for entry in item["offline"] if entry["kind"] == "suite"]
        offline_summary = ", ".join(
            "%s=%s%s" % (entry["name"], entry["state"].upper(),
                         " (optional)" if entry["optional"] else "")
            for entry in offline
        ) or "none"
        required_states = []
        for cid in item["device"]["required"]:
            found = next((check["state"] for check in item["mapped_checks"]
                          if check["id"] == cid), "missing")
            required_states.append("%s=%s" % (cid, found.upper()))
        lines.append("        offline proof: %s" % offline_summary)
        lines.append(
            "        device proof: %d/%d required checks PASS%s"
            % (item["device"]["passed"], item["device"]["total"],
               (" (" + ", ".join(required_states) + ")") if required_states else "")
        )
        if verbose:
            for check in item["mapped_checks"]:
                lines.append(
                    "          - %-14s %-5s %s"
                    % (check["id"], check["state"].upper(), check["summary"])
                )
            for entry in item["manual"]:
                lines.append(
                    "          * %-24s %-9s %s"
                    % (entry["id"], "done" if entry["satisfied"] else "pending",
                       entry["command"])
                )
        pending = [entry for entry in item["manual"]
                   if entry["phase"] == "acceptance" and not entry["satisfied"]]
        if pending:
            lines.append("        still needs the device:")
            for entry in pending:
                lines.append("          - %s: %s" % (entry["step"], entry["command"]))
        if item["missing_evidence"]:
            lines.append("        missing evidence: %s" % "; ".join(item["missing_evidence"]))
        lines.append("        done when: %s" % item["done_when"])
        lines.append("")
    lines.append(
        "== summary: %d items | %d complete | %s"
        % (
            counts["items"], counts["complete"],
            " | ".join(
                "%s %d" % (name, by.get(name, 0)) for name in VERDICTS
                if by.get(name)
            ) or "no evidence",
        )
    )
    lines.append(
        "== offline evidence green for %d/%d items (implementation + required offline suites)"
        % (counts.get("offline_ready", 0), counts["items"])
    )
    lines.append(
        "== device/manual acceptance complete for %d/%d items"
        % (counts["complete"], counts["items"])
    )
    if report["remaining"]:
        lines.append("== what remains (manual/device acceptance):")
        for entry in report["remaining"]:
            lines.append("   %-12s %-24s %s" % (entry["item"], entry["id"], entry["command"]))
    blocking = sorted(
        (report["sources"].get("verify") or {}).get("failing") or []
    )
    if blocking:
        lines.append("== failing live checks: %s" % ", ".join(blocking))
    if report.get("shared_suite_failures"):
        lines.append("== repository-wide suite failures (reported, not item-specific): %s"
                     % ", ".join(report["shared_suite_failures"]))
    if report["missing_evidence"]:
        lines.append("== missing evidence: %s" % "; ".join(report["missing_evidence"]))
    lines.append(
        "== no item is called device-proven by this tool; 'implemented-and-proven-offline' "
        "is an offline claim only."
    )
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- selftest
def _fixture_paths(fixtures=FIXTURES):
    return sorted(pathlib.Path(fixtures).glob("*.json"))


def selftest(fixtures=FIXTURES):
    paths = _fixture_paths(fixtures)
    passed = failed = 0
    problems = []
    if not paths:
        print("selftest: no fixtures under %s" % fixtures, file=sys.stderr)
        return 1
    for path in paths:
        name = path.stem
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
            report = evaluate(fixture)
        except Exception as exc:  # noqa: BLE001 - a fixture must never crash the tool
            failed += 1
            problems.append("%s: raised %s" % (name, exc))
            continue
        expect = fixture.get("expect") or {}
        checks = [
            ("schema", report.get("schema") == SCHEMA),
            ("items", len(report.get("items") or []) == 7),
            ("verdict vocabulary",
             all(item["verdict"] in VERDICTS for item in report["items"])),
            ("counts sum",
             sum(report["counts"]["by_verdict"].values()) == 7),
        ]
        for item_id, wanted in (expect.get("verdicts") or {}).items():
            found = next((item["verdict"] for item in report["items"]
                          if item["id"] == item_id), None)
            checks.append(("verdict %s" % item_id, found == wanted))
        if "complete" in expect:
            checks.append(("complete", report["counts"]["complete"] == expect["complete"]))
        if "automated_complete" in expect:
            checks.append(("automated_complete",
                           report["counts"]["automated_complete"] == expect["automated_complete"]))
        if "exit_code" in expect:
            checks.append(("exit_code", report["exit_code"] == expect["exit_code"]))
        if expect.get("no_device_claim"):
            checks.append((
                "no device-proven claim",
                all(not item["complete"] or item["device"]["complete"]
                    for item in report["items"]),
            ))
        for label, ok in checks:
            if ok:
                passed += 1
            else:
                failed += 1
                problems.append("%s: %s failed" % (name, label))
        if not any(label == "complete" for label in expect):
            # every fixture must at least be renderable
            text = render_text(report)
            if "== summary:" in text:
                passed += 1
            else:
                failed += 1
                problems.append("%s: render_text produced no summary" % name)
    for problem in problems:
        print("FAIL %s" % problem, file=sys.stderr)
    print("selftest: %d checks passed, %d failed (%d fixtures)"
          % (passed, failed, len(paths)))
    return 0 if failed == 0 else 1


# ------------------------------------------------------------------------ main
def _dump(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Evaluate the seven goal items from live/offline evidence.",
        add_help=True,
    )
    parser.add_argument("--json", action="store_true", help="machine report (schema 1)")
    parser.add_argument("--verbose", action="store_true", help="every mapped check and manual step")
    parser.add_argument("--full", action="store_true",
                        help="also run the full-only suites and gen-docs --check")
    parser.add_argument("--save", metavar="FILE", help="save the raw evidence bundle")
    parser.add_argument("--evidence", metavar="FILE",
                        help="evaluate a saved evidence bundle instead of running probes")
    parser.add_argument("--selftest", action="store_true",
                        help="offline aggregation tests over tests/fixtures/acceptance")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.evidence:
        try:
            bundle = json.loads(pathlib.Path(args.evidence).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            sys.stderr.write("%s: cannot read evidence bundle %s: %s\n"
                             % (PROG, args.evidence, exc))
            return 2
    else:
        bundle = collect(full=args.full)
        if args.save:
            try:
                _dump(args.save, bundle)
            except OSError as exc:
                sys.stderr.write("%s: cannot save bundle %s: %s\n" % (PROG, args.save, exc))
                return 2

    report = evaluate(bundle)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        sys.stdout.write(render_text(report, verbose=args.verbose))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
