#!/usr/bin/env python3
"""oxp3-triage - turn an oxp3-firstrun evidence bundle into a ranked diagnosis.

``tools/oxp3-firstrun.sh --collect`` produces a directory of evidence. Reading it
by hand costs a session: the interesting facts are spread over ``verify/``,
``units/``, ``udev/``, ``firmware/``, ``sleep/``, ``audio/``, ``gfx/`` and the
journals, and the failure modes we already understand are invisible unless you
know what to look for.

This tool closes that gap. Input: a bundle directory, a bundle tarball, or a
directory of the loose ``docs/state/`` snapshot files. Output: a structured
report (JSON + human text) with

  * an environment summary (kernel, session, GPU, driver digests),
  * a per-fix verdict derived *from the bundle* (never by re-running a probe),
  * rules-based findings, each citing the bundle file and line it read,
  * a ranked action list with the exact command to run.

Design rules, deliberately:

  * **read-only** - it opens files and parses them; it never runs a probe, writes
    to the system, asks for root, restarts a unit or touches the network;
  * **missing data is ``unknown``, never ``pass``** - a rule whose evidence file
    is absent says so instead of inventing a conclusion;
  * **evidence pointers** - every finding names the file and the line it read, so
    the conclusion can be checked against the bundle;
  * **no rule without a file** - diagnostic knowledge is encoded as rules with a
    declared ``reads`` list (``--rules`` prints the catalogue).

Exit codes: 0 = report produced, no critical finding; 1 = at least one critical
finding (``--strict`` also for ``high``); 2 = usage error; 3 = the input could
not be read as a bundle. Docs: docs/TRIAGE.md.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
SCHEMA = 1

REPO = Path(__file__).resolve().parent.parent

# Recorded constants from the rest of the repository (docs/VALIDATION.md,
# tools/decky-oxp3-status/main.py). They are *pinned* facts: a digest that does
# not match is the finding, not a formatting difference.
MESA_PREFIX = "ec418f04e589965a"
XMX_DRIVER_PREFIX = "4b1d55ea9483417f"
XMX_HELPER_MARKER = "xmx-cm-compile-nodgrf"
EXPECTED_KERNEL_PREFIX = "7.2.7-an28"

SEVERITIES: Tuple[str, ...] = ("critical", "high", "medium", "low", "info")
SEV_PRIORITY = {"critical": 90, "high": 70, "medium": 50, "low": 30, "info": 10}

# ---------------------------------------------------------------------------
# Bundle inventory: the layout tools/oxp3_firstrun.py writes (docs/FIRSTRUN.md
# section 3), plus fallbacks for the loose files under docs/state/.
# ---------------------------------------------------------------------------
ARTIFACTS: "OrderedDict[str, Tuple[str, Tuple[str, ...]]]" = OrderedDict(
    (
        ("manifest", ("MANIFEST.json", ())),
        ("readme", ("README.txt", ())),
        ("checklist", ("checklist.txt", ())),
        ("redactions", ("REDACTIONS.txt", ())),
        ("verify_json", ("verify/verify.json", ("verify-*.json", "verify.json"))),
        ("verify_txt", ("verify/verify.txt", ("verify-*.txt",))),
        ("status_json", ("status/status.json", ("oxp3-status-*.json", "status.json"))),
        ("status_txt", ("status/hw-install-status.txt", ())),
        ("snapshot", ("state/snapshot.txt", ("snapshot-*.txt",))),
        ("units_system", ("units/system.txt", ())),
        ("units_user", ("units/user.txt", ())),
        ("udev", ("udev/rules.txt", ())),
        ("firmware", ("firmware/bluetooth.txt", ())),
        ("power", ("power/power.txt", ())),
        ("sleep", ("sleep/sleep.txt", ())),
        ("session_power", ("session/power.txt", ())),
        ("audio", ("audio/audio.txt", ())),
        ("display", ("display/display.txt", ())),
        ("xmx", ("gfx/xmx.txt", ())),
        ("decky", ("decky/decky.txt", ())),
        ("env", ("system/environment.txt", ())),
        ("hardware", ("system/hardware.txt", ())),
        ("repo_test", ("repo/repo-test-fast.txt", ())),
        ("journal_units", ("journal/oxp3-units.txt", ())),
        ("journal_user", ("journal/oxp3-user-units.txt", ())),
        ("journal_kernel", ("journal/kernel.txt", ())),
        ("journal_sleep", ("journal/sleep-tag.txt", ())),
    )
)

# Artifacts a *complete* firstrun bundle has; the report lists what is missing.
EXPECTED_ARTIFACTS = tuple(k for k in ARTIFACTS if k not in ("readme", "redactions"))


class UsageError(Exception):
    """Bad invocation (exit 2)."""


class BundleError(Exception):
    """The input is not a readable bundle (exit 3)."""


# ---------------------------------------------------------------------------
# Small value objects
# ---------------------------------------------------------------------------
def trim(text: str, limit: int = 170) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


@dataclass
class Evidence:
    """One pointer into the bundle: file, 1-based line, excerpt."""

    file: str
    line: Optional[int]
    excerpt: str

    def as_dict(self) -> Dict[str, Any]:
        return {"file": self.file, "line": self.line, "excerpt": self.excerpt}

    def where(self) -> str:
        loc = self.file if not self.line else "%s:%d" % (self.file, self.line)
        return "%s  %s" % (loc, trim(self.excerpt, 110))


@dataclass
class Action:
    command: str
    why: str
    privileged: bool = True
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "why": self.why,
            "privileged": self.privileged,
            "note": self.note,
        }


@dataclass
class Outcome:
    state: str                     # triggered | clear | unknown | not-applicable
    conclusion: str
    evidence: List[Evidence] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    confidence: str = "high"
    severity: Optional[str] = None  # overrides the rule's default

    def as_dict(self, rule: "Rule", present: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        return {
            "id": rule.id,
            "title": rule.title,
            "state": self.state,
            "severity": self.severity or rule.severity,
            "category": rule.category,
            "reads": list(rule.reads),
            "reads_present": dict(present or {}),
            "conclusion": self.conclusion,
            "confidence": self.confidence,
            "evidence": [e.as_dict() for e in self.evidence],
            "actions": [a.as_dict() for a in self.actions],
        }


@dataclass
class Rule:
    id: str
    title: str
    severity: str
    category: str
    reads: Tuple[str, ...]
    fn: Callable[["Facts"], Outcome]


RULES: List[Rule] = []


def rule(rid: str, title: str, severity: str, category: str, reads: Sequence[str]):
    """Register one diagnostic rule. ``reads`` is the bundle evidence it needs."""

    def decorate(fn: Callable[["Facts"], Outcome]) -> Callable[["Facts"], Outcome]:
        RULES.append(Rule(rid, title, severity, category, tuple(reads), fn))
        return fn

    return decorate


# ---------------------------------------------------------------------------
# Bundle access
# ---------------------------------------------------------------------------
_SAFE_MEMBER = re.compile(r"^(?!\.\.)(?!.*/\.\.(/|$))(?!.*\\\.\.\\).*$")


def _extract_tarball(archive: Path) -> Path:
    """Extract a bundle tarball into a private temp dir, refusing escapes."""
    tmp = Path(tempfile.mkdtemp(prefix="oxp3-triage-"))
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = []
            for member in tar.getmembers():
                name = member.name
                if not _SAFE_MEMBER.match(name):
                    raise BundleError("refusing tar member with a path escape: %s" % name)
                if member.issym() or member.islnk() or member.isdev():
                    raise BundleError("refusing non-regular tar member: %s" % name)
                if os.path.isabs(name):
                    raise BundleError("refusing absolute tar member: %s" % name)
                members.append(member)
            try:
                tar.extractall(str(tmp), members=members, filter="data")
            except TypeError:  # Python < 3.12 has no extraction filters
                tar.extractall(str(tmp), members=members)
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(str(tmp), ignore_errors=True)
        raise BundleError("cannot read tarball %s: %s" % (archive, exc))
    return tmp


class Bundle:
    """Read-only view of one evidence drop (directory, tarball or state dir)."""

    def __init__(self, root: Path, source: str, temp: Optional[Path] = None) -> None:
        self.root = root
        self.source = source
        self._temp = temp
        self._find_cache: Dict[str, Optional[str]] = {}
        self._text_cache: Dict[str, Optional[str]] = {}
        self._json_cache: Dict[str, Tuple[Any, Optional[str]]] = {}
        self.kind = self._detect_kind()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if self._temp is not None:
            shutil.rmtree(str(self._temp), ignore_errors=True)
            self._temp = None

    def _detect_kind(self) -> str:
        if (self.root / "MANIFEST.json").is_file():
            return "bundle"
        if self.find("verify_json") or self.find("status_json") or self.find("snapshot"):
            return "state-dir"
        return "unknown"

    # -- lookup -------------------------------------------------------------
    def find(self, key: str) -> Optional[str]:
        if key in self._find_cache:
            return self._find_cache[key]
        rel, patterns = ARTIFACTS[key]
        found: Optional[str] = None
        direct = self.root / rel
        if direct.is_file():
            found = rel
        else:
            for pattern in patterns:
                hits = sorted(glob.glob(str(self.root / pattern)))
                for hit in hits:
                    path = Path(hit)
                    if path.is_file():
                        found = str(path.relative_to(self.root))
                        break
                if found:
                    break
        self._find_cache[key] = found
        return found

    def has(self, key: str) -> bool:
        return self.find(key) is not None

    def text(self, key: str) -> Optional[str]:
        rel = self.find(key)
        if rel is None:
            return None
        if rel not in self._text_cache:
            try:
                raw = (self.root / rel).read_bytes()
            except OSError:
                self._text_cache[rel] = None
                return None
            self._text_cache[rel] = raw.decode("utf-8", "replace")
        return self._text_cache[rel]

    def lines(self, key: str) -> List[str]:
        text = self.text(key)
        return text.splitlines() if text else []

    def json(self, key: str) -> Tuple[Any, Optional[str]]:
        rel = self.find(key)
        if rel is None:
            return None, "missing"
        if rel not in self._json_cache:
            text = self.text(key)
            try:
                self._json_cache[rel] = (json.loads(text or ""), None)
            except ValueError as exc:
                self._json_cache[rel] = (None, "invalid JSON: %s" % exc)
        return self._json_cache[rel]

    # -- evidence helpers ---------------------------------------------------
    def first_line(self, key: str, pattern: str) -> Optional[Tuple[int, str]]:
        rx = re.compile(pattern)
        for number, line in enumerate(self.lines(key), 1):
            if rx.search(line):
                return number, line
        return None

    def all_lines(self, key: str, pattern: str) -> List[Tuple[int, str]]:
        rx = re.compile(pattern)
        return [
            (number, line)
            for number, line in enumerate(self.lines(key), 1)
            if rx.search(line)
        ]

    def evidence(self, key: str, pattern: str, note: str = "") -> Optional[Evidence]:
        rel = self.find(key)
        hit = self.first_line(key, pattern) if rel else None
        if not hit or not rel:
            return None
        number, line = hit
        return Evidence(rel, number, note or line.strip())

    def evidence_line(self, key: str, line: int, note: str = "") -> Optional[Evidence]:
        rel = self.find(key)
        if not rel:
            return None
        lines = self.lines(key)
        if 1 <= line <= len(lines):
            return Evidence(rel, line, note or lines[line - 1].strip())
        return Evidence(rel, None, note or "")

    def evidence_at(self, key: str, needle: str, note: str = "") -> Optional[Evidence]:
        """Evidence for the line containing ``needle`` (JSON keys, unit names)."""
        rel = self.find(key)
        if not rel:
            return None
        for number, line in enumerate(self.lines(key), 1):
            if needle in line:
                return Evidence(rel, number, note or line.strip())
        return Evidence(rel, None, note or needle)


def resolve_input(target: str) -> Bundle:
    path = Path(target).expanduser()
    if not path.exists():
        raise BundleError("no such file or directory: %s" % path)
    if path.is_dir():
        bundle = Bundle(path.resolve(), str(path))
    elif path.is_file() and tarfile.is_tarfile(str(path)):
        temp = _extract_tarball(path)
        inner = [p for p in sorted(temp.iterdir())]
        root = temp
        if len(inner) == 1 and inner[0].is_dir():
            root = inner[0]
        bundle = Bundle(root, str(path), temp)
    else:
        raise BundleError("not a bundle directory or tarball: %s" % path)
    if bundle.kind == "unknown":
        bundle.close()
        raise BundleError(
            "no oxp3-firstrun evidence found in %s (expected MANIFEST.json or "
            "verify/verify.json)" % path
        )
    return bundle


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
SECTION_RX = re.compile(r"^\s{0,2}(?:==|--)\s+(.*\S)\s*$")


def sections(bundle: Bundle, key: str) -> "OrderedDict[str, List[Tuple[int, str]]]":
    """Split one of the collector's text files on its ``== title`` / ``-- title``."""
    out: "OrderedDict[str, List[Tuple[int, str]]]" = OrderedDict()
    current = ""
    out[current] = []
    for number, line in enumerate(bundle.lines(key), 1):
        match = SECTION_RX.match(line)
        if match and not line.strip().startswith("==="):
            current = match.group(1).strip()
            out.setdefault(current, [])
            continue
        out.setdefault(current, []).append((number, line))
    return out


def section(bundle: Bundle, key: str, needle: str) -> Optional[str]:
    for name in sections(bundle, key):
        if needle.lower() in name.lower():
            return name
    return None


def section_lines(bundle: Bundle, key: str, needle: str) -> List[Tuple[int, str]]:
    name = section(bundle, key, needle)
    if name is None:
        return []
    return sections(bundle, key)[name]


def kv_from(lines: Iterable[Tuple[int, str]]) -> Dict[str, Tuple[str, int]]:
    """Parse ``  key   value`` rows into {key: (value, line)}."""
    out: Dict[str, Tuple[str, int]] = {}
    for number, line in lines:
        match = re.match(r"^\s+([a-z_]+)\s{1,}(\S.*?)\s*$", line)
        if match:
            out[match.group(1)] = (match.group(2).strip(), number)
    return out


def parse_verify(bundle: Bundle) -> Optional[Dict[str, Any]]:
    rel = bundle.find("verify_json")
    if not rel:
        return None
    data, error = bundle.json("verify_json")
    result: Dict[str, Any] = {
        "rel": rel,
        "error": error,
        "checks": {},
        "counts": {},
        "exit_code": None,
        "generated_at": None,
    }
    if not isinstance(data, dict):
        return result
    result["counts"] = data.get("counts") or {}
    result["exit_code"] = data.get("exit_code")
    result["generated_at"] = data.get("generated_at")
    for check in data.get("checks") or []:
        if isinstance(check, dict) and check.get("id"):
            result["checks"][str(check["id"])] = check
    return result


def parse_status(bundle: Bundle) -> Optional[Dict[str, Any]]:
    if not bundle.has("status_json"):
        return None
    data, error = bundle.json("status_json")
    out: Dict[str, Any] = {
        "rel": bundle.find("status_json"),
        "error": error,
        "overall": None,
        "counts": {},
        "host": {},
        "indicators": {},
    }
    if not isinstance(data, dict):
        return out
    out["overall"] = data.get("overall")
    out["counts"] = data.get("counts") or {}
    out["host"] = data.get("host") or {}
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for indicator in group.get("indicators") or []:
            if isinstance(indicator, dict) and indicator.get("id"):
                item = dict(indicator)
                item["group"] = group.get("id")
                out["indicators"][str(indicator["id"])] = item
    return out


def parse_manifest(bundle: Bundle) -> Optional[Dict[str, Any]]:
    if not bundle.has("manifest"):
        return None
    data, error = bundle.json("manifest")
    if not isinstance(data, dict):
        return {"error": error}
    data.setdefault("checklist", [])
    return data


def parse_checklist(bundle: Bundle) -> Dict[str, Dict[str, str]]:
    """{step id: {title, state, detail}} from MANIFEST.json, else checklist.txt."""
    steps: Dict[str, Dict[str, str]] = {}
    manifest = parse_manifest(bundle)
    if manifest and isinstance(manifest.get("checklist"), list):
        for step in manifest["checklist"]:
            if isinstance(step, dict) and step.get("id"):
                steps[str(step["id"])] = {
                    "title": str(step.get("title") or ""),
                    "state": str(step.get("state") or ""),
                    "detail": str(step.get("detail") or ""),
                    "command": str(step.get("command") or ""),
                }
        if steps:
            return steps
    # Fallback: the printed checklist. Titles are truncated to 34 columns, so key
    # by the normalised title and let lookups search for a substring.
    for number, line in enumerate(bundle.lines("checklist"), 1):
        match = re.match(
            r"^\s*\d+\s{2,}(.{10,40}?)\s{2,}(ok|todo|partial|pending|unknown|n/a)\s+(.*)$",
            line,
        )
        if match:
            steps["#%d" % number] = {
                "title": match.group(1).strip(),
                "state": match.group(2).strip(),
                "detail": match.group(3).strip(),
                "command": "",
            }
    return steps


def checklist_step(steps: Dict[str, Dict[str, str]], needle: str) -> Optional[Dict[str, str]]:
    for key, step in steps.items():
        if key == needle or needle.lower() in (step.get("title") or "").lower():
            return step
    return None


def parse_udev_files(bundle: Bundle) -> Dict[str, Dict[str, Any]]:
    """{path: {present, sha256, mtime}} from udev/rules.txt."""
    files: Dict[str, Dict[str, Any]] = {}
    lines = bundle.lines("udev")
    for index, line in enumerate(lines):
        match = re.match(r"^\s+(/\S+)\s+(present|MISSING)\s*(\S*)\s*$", line)
        if not match:
            continue
        path, state, digest = match.group(1), match.group(2), match.group(3)
        entry: Dict[str, Any] = {
            "present": state == "present",
            "sha256": digest or None,
            "mtime": None,
        }
        if index + 1 < len(lines):
            follow = re.match(
                r"^\s{4,}\S.*?\s{2,}(\d{4}-\d\d-\d\dT[\d:]+Z)\s+(\d+) B\s*$", lines[index + 1]
            )
            if follow:
                entry["mtime"] = follow.group(1)
        files[path] = entry
    return files


def parse_firmware(bundle: Bundle) -> Dict[str, Any]:
    """Firmware presence, module state, hci0 and the dracut drop-in."""
    out: Dict[str, Any] = {
        "files": {},
        "modules": {},
        "hci0": None,
        "dracut": None,
        "dracut_body": [],
    }
    for path, state, size, digest in re.findall(
        r"^\s+(/usr/lib/firmware/\S+)\s+(present|MISSING)\s+(\S+)\s*B?\s*(\S*)\s*$",
        bundle.text("firmware") or "",
        re.M,
    ):
        out["files"][path] = {
            "present": state == "present",
            "size": size,
            "sha256": digest or None,
        }
    for module, state in re.findall(r"^\s{2}([a-z0-9_]+)\s+(loaded|not loaded)\s*$",
                                    bundle.text("firmware") or "", re.M):
        out["modules"][module] = state == "loaded"
    hit = bundle.first_line("firmware", r"/sys/class/bluetooth/hci0\s+(present|absent)")
    if hit:
        out["hci0"] = "present" in hit[1]
    hit = bundle.first_line("firmware", r"^(.*60-oxp3-bt-firmware\.conf)\s*(MISSING|:)\s*$")
    if hit:
        out["dracut"] = "missing" not in hit[1].lower()
        out["dracut_line"] = hit[0]
    if out["dracut"]:
        for number, line in enumerate(bundle.lines("firmware"), 1):
            if re.match(r"^\s{4}\S", line) and number > (out.get("dracut_line") or 0):
                out["dracut_body"].append(line.strip())
    return out


def parse_units(bundle: Bundle) -> Dict[str, Dict[str, str]]:
    units: Dict[str, Dict[str, str]] = {}
    for line in bundle.lines("units_system"):
        match = re.match(
            r"^(oxp3-\S+|rapl-power\.service)\s+load=(\S+)\s+file=(\S*)\s+active=(\S+)\s+"
            r"sub=(\S+)\s+restarts=(\S+)\s+exec=(\S+)\s*$",
            line,
        )
        if match:
            units[match.group(1)] = {
                "load": match.group(2),
                "file": match.group(3),
                "active": match.group(4),
                "sub": match.group(5),
                "restarts": match.group(6),
                "exec": match.group(7),
            }
    return units


def parse_failed_units(bundle: Bundle) -> List[str]:
    lines = section_lines(bundle, "units_system", "systemctl --failed")
    failed = []
    for _number, line in lines:
        text = line.strip()
        if text and not text.startswith("(") and ".service" in text:
            failed.append(text)
    return failed


def parse_suspend(bundle: Bundle) -> Dict[str, Any]:
    out: Dict[str, Any] = {"stats": {}, "lines": {}, "attempted": None}
    stats = kv_from(section_lines(bundle, "sleep", "suspend_stats"))
    for name, (value, number) in stats.items():
        out["stats"][name] = value
        out["lines"][name] = number
    # The audit repeats the record; keep the first (collector) section.
    if not out["stats"]:
        audit = kv_from(section_lines(bundle, "sleep", "kernel suspend record"))
        for name, (value, number) in audit.items():
            out["stats"][name] = value
            out["lines"][name] = number
    for name in ("success", "fail", "failed_suspend", "failed_resume", "last_hw_sleep",
                 "total_hw_sleep", "max_hw_sleep"):
        try:
            out["stats"][name + "_int"] = int(out["stats"].get(name, "") or 0)
        except ValueError:
            out["stats"][name + "_int"] = None
    return out


def parse_inhibitors(bundle: Bundle) -> Optional[List[Dict[str, str]]]:
    rows = section_lines(bundle, "sleep", "suspend inhibitors")
    if not rows:
        return None
    out: List[Dict[str, str]] = []
    for number, line in rows:
        stripped = line.strip()
        if not stripped or stripped.startswith("WHO") or "listed." in stripped:
            continue
        parts = stripped.split()
        if len(parts) < 4:
            continue
        mode = parts[-1]
        if mode not in ("delay", "block"):
            continue
        out.append(
            {
                "who": parts[0],
                "mode": mode,
                "handles": "handle-" in stripped,
                "what": next(
                    (token for token in parts if "handle-" in token or token == "sleep"),
                    "",
                ),
                "line": str(number),
                "raw": stripped,
            }
        )
    return out


def parse_session_power(bundle: Bundle) -> Optional[Dict[str, Any]]:
    """logind session, PowerDevil values and the session polkit agent.

    The collector writes the kreadconfig result twice (flat ``--group AC`` and the
    nested ``[AC][SuspendAndShutdown]`` group Plasma 6 actually writes) and then a
    bounded powerdevilrc excerpt. The excerpt is authoritative for explicit values,
    because a flat KConfig read cannot see the nested group.
    """
    if not bundle.has("session_power"):
        return None
    out: Dict[str, Any] = {
        "values": {},
        "excerpt_groups": {},
        "file_present": None,
        "agent": None,
        "agent_line": None,
        "logind": {},
        "logind_lines": {},
    }
    group: Optional[str] = None
    in_excerpt = False
    for number, line in enumerate(bundle.lines("session_power"), 1):
        stripped = line.strip()
        if stripped.startswith("--- begin ") and "powerdevilrc" in stripped:
            in_excerpt = True
            group = None
            continue
        if stripped.startswith("--- end ") and "powerdevilrc" in stripped:
            in_excerpt = False
            continue
        if in_excerpt:
            if stripped.startswith("[") and stripped.endswith("]"):
                group = ".".join(re.findall(r"\[([^\]]+)\]", stripped))
                out["excerpt_groups"].setdefault(group, {})
            elif "=" in stripped and group:
                key, value = stripped.split("=", 1)
                out["excerpt_groups"][group][key.strip()] = (value.strip(), number)
            continue
        match = re.match(r"^\s+exists\s*:\s*(yes|no)\s*$", line)
        if match:
            out["file_present"] = match.group(1) == "yes"
            continue
        match = re.match(
            r"^\s+(AC|Battery)\.(?:SuspendAndShutdown\.)?(\w+)\s+=\s+(.*?)\s*$", line)
        if match:
            name = "%s.%s" % (match.group(1), match.group(2))
            out["values"][name] = {
                "value": match.group(3).strip(),
                "line": number,
                "nested": "SuspendAndShutdown." in line.split("=")[0],
            }
            continue
        match = re.match(r"^\s+session polkit agent:\s*(\w+)", line)
        if match:
            out["agent"] = match.group(1).lower()
            out["agent_line"] = number
            continue
        match = re.match(r"^\s+(Type|Class|Active|IdleHint|State)=(\S+)", line)
        if match:
            out["logind"][match.group(1)] = match.group(2)
            out["logind_lines"][match.group(1)] = number
    return out


POWERDEVIL_ENUM = {
    "0": "nothing",
    "1": "sleep",
    "2": "hibernate",
    "3": "shutdown",
    "4": "lock",
    "8": "logout",
}
POWERDEVIL_WORDS = (
    ("hibernate", "hibernate"),
    ("suspend", "sleep"),
    ("sleep", "sleep"),
    ("shutdown", "shutdown"),
    ("poweroff", "shutdown"),
    ("power off", "shutdown"),
    ("shut down", "shutdown"),
    ("nothing", "nothing"),
    ("no action", "nothing"),
    ("lock", "lock"),
    ("logout", "logout"),
    ("log out", "logout"),
)


def powerdevil_action_kind(raw: str) -> str:
    """Classify a PowerDevil action value.

    PowerDevil stores an enum integer (0 nothing, 1 sleep, 2 hibernate, 3 shutdown,
    4 lock screen, 8 logout - docs/hw/sleep-power.md R1); an unset key means "use
    PowerDevil's built-in default", which the bundle cannot read.
    """
    text = (raw or "").strip().lower()
    if not text:
        return "unset"
    if text in POWERDEVIL_ENUM:
        return POWERDEVIL_ENUM[text]
    for word, kind in POWERDEVIL_WORDS:
        if word in text:
            return kind
    return "unknown"


def powerdevil_entry(
    session: Optional[Dict[str, Any]], group: str, key: str
) -> Optional[Dict[str, Any]]:
    """Resolve one PowerDevil key: nested group, flat group, then the excerpt."""
    if session is None:
        return None
    for name in ("%s.SuspendAndShutdown.%s" % (group, key), "%s.%s" % (group, key)):
        entry = session["values"].get(name)
        if entry and entry["value"] and not entry["value"].startswith("("):
            return {"group": group, "key": key, "value": entry["value"],
                    "line": entry["line"], "source": "kreadconfig"}
    for name in ("%s.SuspendAndShutdown" % group, group):
        hit = session["excerpt_groups"].get(name, {}).get(key)
        if hit and hit[0]:
            return {"group": group, "key": key, "value": hit[0],
                    "line": hit[1], "source": "powerdevilrc excerpt"}
    for name in ("%s.SuspendAndShutdown.%s" % (group, key), "%s.%s" % (group, key)):
        entry = session["values"].get(name)
        if entry and entry["value"].startswith("("):
            return {"group": group, "key": key, "value": "",
                    "line": entry["line"], "source": "kreadconfig %s" % entry["value"]}
    return {"group": group, "key": key, "value": "", "line": None,
            "source": "not recorded"}


def powerdevil_effective(
    session: Optional[Dict[str, Any]], key: str
) -> Optional[Dict[str, Any]]:
    """The AC value if explicitly set, else the Battery one, else AC's unset row."""
    if session is None:
        return None
    ac = powerdevil_entry(session, "AC", key)
    battery = powerdevil_entry(session, "Battery", key)
    if ac and ac["value"]:
        return ac
    if battery and battery["value"]:
        return battery
    return ac or battery


def parse_audio(bundle: Bundle) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "server": None,
        "default_sink": None,
        "sinks": [],
        "cards": [],
        "eq_dropin": None,
        "eq_loaded": None,
    }
    info = section_lines(bundle, "audio", "pactl info")
    for _number, line in info:
        match = re.match(r"^\s*Server String:\s*(\S+)", line)
        if match:
            out["server"] = match.group(1)
        match = re.match(r"^\s*Default Sink:\s*(\S+)", line)
        if match:
            out["default_sink"] = match.group(1)
    for _number, line in section_lines(bundle, "audio", "pactl list short sinks"):
        fields = line.strip().split("\t")
        if len(fields) >= 2:
            out["sinks"].append(fields[1].strip())
        elif line.strip():
            tokens = line.strip().split()
            if len(tokens) >= 2:
                out["sinks"].append(tokens[1].strip())
    for _number, line in section_lines(bundle, "audio", "ALSA / SOF cards"):
        if re.match(r"^\d+\s+\[", line):
            out["cards"].append(line.strip())
    hit = bundle.first_line("audio", r"51-oxp3-speaker-eq\.conf\s+(present|MISSING)")
    if hit:
        out["eq_dropin"] = "present" in hit[1]
    out["eq_loaded"] = any("oxp3_speaker_eq" in sink for sink in out["sinks"])
    return out


def parse_xmx(bundle: Bundle) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "env": {},
        "driver_hashes": {},
        "icd_present": None,
        "env_sh_present": None,
        "flatpak_env_present": None,
        "kernels": None,
        "ocloc": [],
        "ocloc_missing": None,
        "flatpak_override": [],
        "trace": False,
    }
    for _number, line in section_lines(bundle, "xmx", "session environment"):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            out["env"][match.group(1)] = match.group(2)
    for path, state, digest in re.findall(
        r"^\s+(/\S+)\s+(present|MISSING)\s*(\S*)\s*$", bundle.text("xmx") or "", re.M
    ):
        out["driver_hashes"][path] = {
            "present": state == "present",
            "sha256": digest or None,
        }
    for relpath, state, digest in re.findall(
        r"^\s+(~\S+)\s+(present|MISSING)\s*(\S*)\s*$", bundle.text("xmx") or "", re.M
    ):
        key = os.path.basename(relpath)
        if key == "icd-xmx.json":
            out["icd_present"] = state == "present"
        elif key == "oxp3-env.sh":
            out["env_sh_present"] = state == "present"
        elif key == "flatpak-env.sh":
            out["flatpak_env_present"] = state == "present"
    hit = bundle.first_line("xmx", r"kernels:\s+(\d+)\s+entr")
    if hit:
        out["kernels"] = int(hit[1].split(":")[1].split()[0])
    for number, line in enumerate(bundle.lines("xmx"), 1):
        if "ocloc" in line and ("present" in line or "not found" in line):
            out["ocloc"].append((number, line.strip()))
    if any("not found" in line for _n, line in out["ocloc"]):
        out["ocloc_missing"] = True
    elif out["ocloc"]:
        out["ocloc_missing"] = False
    override = section_lines(bundle, "xmx", "Steam Flatpak overrides")
    for _number, line in override:
        if "=" in line:
            out["flatpak_override"].append(line.strip())
    if override and not any("none written" in line for _n, line in override):
        if any("ANV_CM_HELPER" in line for line in out["flatpak_override"]):
            out["flatpak_env_present"] = True
    # Any igdext trace mention (status value, journal line, xmx text).
    for key in ("status_json", "xmx", "journal_kernel", "display"):
        text = bundle.text(key) or ""
        if "igdext_trace.log" in text:
            out["trace"] = True
            break
    return out


def parse_env(bundle: Bundle) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kernel": None, "os": None, "uptime_s": None, "cmdline": None,
                           "git": {}, "dmi": {}, "raw": {}}
    for number, line in enumerate(bundle.lines("env"), 1):
        match = re.match(r"^(\w+)\s*:\s*(.*?)\s*$", line)
        if match and match.group(1) in ("kernel", "os", "arch", "uptime_s", "euid", "repo",
                                        "git", "boot_id"):
            out["raw"][match.group(1)] = match.group(2)
            out["kernel"] = out["kernel"] or (match.group(2) if match.group(1) == "kernel" else None)
            out["os"] = out["os"] or (match.group(2) if match.group(1) == "os" else None)
            if match.group(1) == "uptime_s":
                try:
                    out["uptime_s"] = float(match.group(2))
                except ValueError:
                    out["uptime_s"] = None
            if match.group(1) == "git":
                try:
                    out["git"] = json.loads(match.group(2))
                except ValueError:
                    out["git"] = {}
    for number, line in enumerate(bundle.lines("env"), 1):
        match = re.match(r"^\s+(sys_vendor|product_name|board_name|bios_version|bios_date)\s+(.*)$",
                         line)
        if match:
            out["dmi"][match.group(1)] = match.group(2).strip()
    for _number, line in section_lines(bundle, "env", "/proc/cmdline"):
        if line.strip() and not line.startswith("=="):
            out["cmdline"] = line.strip()
            break
    return out


def _parse_iso(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Facts: everything the rules may read, parsed once
# ---------------------------------------------------------------------------
@dataclass
class Facts:
    bundle: Bundle
    verify: Optional[Dict[str, Any]]
    status: Optional[Dict[str, Any]]
    manifest: Optional[Dict[str, Any]]
    checklist: Dict[str, Dict[str, str]]
    udev: Dict[str, Dict[str, Any]]
    firmware: Dict[str, Any]
    units: Dict[str, Dict[str, str]]
    failed_units: List[str]
    suspend: Dict[str, Any]
    inhibitors: Optional[List[Dict[str, str]]]
    session_power: Optional[Dict[str, Any]]
    audio: Dict[str, Any]
    xmx: Dict[str, Any]
    env: Dict[str, Any]
    boot_wall: Optional[_dt.datetime]
    _cache: Dict[str, Any] = field(default_factory=dict)

    # -- convenience --------------------------------------------------------
    def check(self, cid: str) -> Optional[Dict[str, Any]]:
        return (self.verify or {}).get("checks", {}).get(cid)

    def checks(self, prefixes: Sequence[str]) -> List[Dict[str, Any]]:
        out = []
        for cid, check in sorted((self.verify or {}).get("checks", {}).items()):
            if any(cid == p or cid.startswith(p) for p in prefixes):
                out.append(check)
        return out

    def indicator(self, iid: str) -> Optional[Dict[str, Any]]:
        return (self.status or {}).get("indicators", {}).get(iid)

    def step(self, sid: str) -> Optional[Dict[str, str]]:
        return checklist_step(self.checklist, sid)

    def ev_check(self, cid: str) -> Optional[Evidence]:
        check = self.check(cid)
        if check is None:
            return None
        note = "%s %s: %s" % (cid, check.get("state"), check.get("summary") or "")
        detail = (check.get("detail") or "").strip()
        if detail:
            note += " \u2014 " + detail
        return self.bundle.evidence_at("verify_json", '"%s"' % cid, note)

    def ev_indicator(self, iid: str) -> Optional[Evidence]:
        indicator = self.indicator(iid)
        if indicator is None:
            return None
        note = "%s %s: %s" % (iid, indicator.get("state"), indicator.get("value") or "")
        return self.bundle.evidence_at("status_json", '"%s"' % iid, note)

    def has(self, key: str) -> bool:
        return self.bundle.has(key)

    def line_count(self, key: str) -> int:
        return len(self.bundle.lines(key))


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _xmx_installed(f: Facts) -> bool:
    """Is XMX configured at all? (it is per-game opt-in, not a boot default)"""
    if f.xmx.get("icd_present"):
        return True
    indicator = f.indicator("xmx.icd")
    if indicator is not None and indicator.get("state") in ("ok", "warn"):
        return True
    return any(check.get("state") in ("pass", "fail") for check in f.checks(["gfx.xmx."]))


def _firmware_present(f: Facts) -> Optional[bool]:
    files = f.firmware.get("files") or {}
    if files:
        return all(entry.get("present") for entry in files.values())
    check = f.check("bt.files")
    if check is not None:
        if check.get("state") == "pass":
            return True
        if check.get("state") == "fail":
            return False
    return None


def _bt_load_failure(f: Facts) -> Optional[Evidence]:
    hit = f.bundle.first_line("journal_kernel", r"Failed to load Intel firmware file")
    if hit:
        return f.bundle.evidence_line("journal_kernel", hit[0])
    check = f.check("bt.journal")
    if check is not None and check.get("state") == "fail":
        return f.ev_check("bt.journal")
    return None


def _logind_dropin(f: Facts) -> Optional[Dict[str, Any]]:
    path = "/etc/systemd/logind.conf.d/60-oxp3-sleep.conf"
    if f.udev:
        return f.udev.get(path)
    hit = f.bundle.first_line("udev", re.escape(path) + r"\s+(present|MISSING)")
    if hit:
        return {"present": "present" in hit[1], "mtime": None}
    return None


def _polkit_rule(f: Facts) -> Optional[Dict[str, Any]]:
    path = "/etc/polkit-1/rules.d/49-oxp3-suspend.rules"
    if f.udev:
        return f.udev.get(path)
    hit = f.bundle.first_line("udev", re.escape(path) + r"\s+(present|MISSING)")
    if hit:
        return {"present": "present" in hit[1], "mtime": None}
    return None


def _skip_category(check: Dict[str, Any]) -> str:
    """Classify a SKIP row so 'not installed' never reads as 'broken'."""
    if check.get("state") != "skip":
        return ""
    text = ((check.get("summary") or "") + " " + (check.get("detail") or "")).lower()
    # "not installed (opt-in)" is an opt-in component, not a missing install step,
    # so the opt-in/gated test comes first.
    for needle in ("opt-in", "gated"):
        if needle in text:
            return "opt-in"
    for needle in (
        "not installed",
        "unit not installed",
        "udev rule not installed",
        "no user config",
        "daemon not running",
        "no dracut drop-in",
        "trigger policy not installed",
        "sleep policy not installed",
        "no saved profile",
        "eq not installed",
        "no lifecycle daemon active",
    ):
        if needle in text:
            return "not-installed"
    for needle in ("needs root", "requires root", "root-only", "not privileged"):
        if needle in text:
            return "needs-root"
    for needle in ("no suspend attempt", "no lifecycle journal", "no press", "no user bus",
                   "no user systemd instance", "no audio seat", "not present", "not found"):
        if needle in text:
            return "no-evidence"
    return "unknown"


# -- Bluetooth --------------------------------------------------------------
@rule(
    "bt.initramfs-stale",
    "Bluetooth firmware is present but this boot's initramfs predates it",
    "critical",
    "bluetooth",
    ("firmware", "journal_kernel", "verify_json", "udev"),
)
def bt_initramfs_stale(f: Facts) -> Outcome:
    present = _firmware_present(f)
    failure = _bt_load_failure(f)
    if present is None:
        return Outcome("unknown", "cannot tell whether the firmware files are installed "
                                 "(no firmware/bluetooth.txt and no bt.files row)")
    if not present:
        return Outcome("not-applicable", "the firmware files are not installed; see "
                                         "bt.firmware-missing")
    if failure is None:
        return Outcome("clear", "the firmware files are present and no load failure was "
                                "recorded in this boot's kernel log")
    evidence = [failure]
    fw_hit = f.bundle.first_line(
        "firmware", r"ibt-00a0-0291-(?:iml|pci)\.sfi\s+present"
    )
    if fw_hit:
        evidence.append(f.bundle.evidence_line("firmware", fw_hit[0]))
    dracut = f.firmware.get("dracut")
    dracut_line = f.firmware.get("dracut_line")
    if dracut_line:
        evidence.append(
            f.bundle.evidence_line(
                "firmware", dracut_line,
                "dracut drop-in %s" % ("present" if dracut else "MISSING"),
            )
        )
    controller = f.check("bt.controller")
    if controller is not None and controller.get("state") == "fail":
        evidence.append(f.ev_check("bt.controller"))

    actions = [
        Action(
            "sudo tools/oxp3-bt-firmware.sh",
            "installs the pinned blobs, writes the dracut drop-in and enables the "
            "ostree initramfs",
        ),
        Action("sudo systemctl reboot", "the running initramfs only carries the files "
                                        "after a rebuild + reboot"),
    ]
    detail = "firmware present"
    drop = (f.udev or {}).get("/etc/dracut.conf.d/60-oxp3-bt-firmware.conf")
    drop_mtime = _parse_iso((drop or {}).get("mtime"))
    if dracut is False:
        detail = "the dracut drop-in is missing, so a rebuilt initramfs still cannot carry it"
    elif dracut and drop_mtime and f.boot_wall and drop_mtime < f.boot_wall:
        detail = (
            "the drop-in predates this boot and the load still failed, so the initramfs "
            "was not regenerated (ostree initramfs step), not a stale file"
        )
        actions.insert(0, Action("sudo rpm-ostree initramfs --enable",
                                 "regenerate the initramfs from the drop-in"))
    elif dracut and f.boot_wall and (drop_mtime is None or drop_mtime >= f.boot_wall):
        detail = (
            "the drop-in exists but was written after this boot's initramfs, so the "
            "running initramfs cannot include it yet"
        )
    return Outcome(
        "triggered",
        "Both pinned blobs are on disk, yet the kernel logged a firmware load failure "
        "(-ENOENT) and no controller came up. btintel_pcie probes inside the initramfs, "
        "where /usr/lib/firmware only contains what dracut packed, so the running "
        "initramfs predates the files. " + detail.capitalize() + ". Reloading the module "
        "cannot fix this boot; the initramfs must be rebuilt and the machine rebooted.",
        evidence,
        actions,
        confidence="high" if f.has("journal_kernel") else "medium",
    )


@rule(
    "bt.firmware-missing",
    "the pinned Bluetooth firmware is not installed",
    "high",
    "bluetooth",
    ("firmware", "verify_json"),
)
def bt_firmware_missing(f: Facts) -> Outcome:
    present = _firmware_present(f)
    if present is None:
        return Outcome("unknown", "no firmware evidence in this bundle")
    if present:
        return Outcome("clear", "both pinned blobs are present")
    evidence = []
    hit = f.bundle.first_line("firmware", r"ibt-\S+\.sfi\s+MISSING")
    if hit:
        evidence.append(f.bundle.evidence_line("firmware", hit[0]))
    check = f.check("bt.files")
    if check is not None:
        evidence.append(f.ev_check("bt.files"))
    return Outcome(
        "triggered",
        "At least one of the two pinned blobs is missing from /usr/lib/firmware/updates/"
        "intel, so btintel_pcie cannot load them on any boot. Install them and reboot.",
        evidence,
        [Action("sudo tools/oxp3-bt-firmware.sh",
                "installs the pinned firmware and the dracut drop-in"),
         Action("sudo systemctl reboot", "the initramfs must be regenerated and re-read")],
    )


@rule(
    "bt.no-controller",
    "the Bluetooth radio does not enumerate",
    "high",
    "bluetooth",
    ("firmware", "verify_json", "journal_kernel"),
)
def bt_no_controller(f: Facts) -> Outcome:
    check = f.check("bt.controller")
    hci0 = f.firmware.get("hci0")
    if check is None and hci0 is None:
        return Outcome("unknown", "no controller evidence (no bt.controller row, no hci0 line)")
    if check is not None and check.get("state") == "pass":
        return Outcome("clear", "btmgmt reports a controller")
    if _firmware_present(f) and _bt_load_failure(f):
        return Outcome("not-applicable", "the load failure explains the missing controller; "
                                         "see bt.initramfs-stale")
    evidence = []
    if check is not None:
        evidence.append(f.ev_check("bt.controller"))
    if hci0 is not None:
        hit = f.bundle.first_line("firmware", r"/sys/class/bluetooth/hci0\s+(present|absent)")
        if hit:
            evidence.append(f.bundle.evidence_line("firmware", hit[0]))
    return Outcome(
        "triggered",
        "No controller answers (hci0 without firmware, or no hci0 at all) and no load "
        "failure was recorded. Reload the driver once; if it still fails, the firmware/"
        "initramfs path is the cause.",
        evidence,
        [Action("sudo modprobe -r btintel_pcie btintel && sudo modprobe btintel_pcie",
                "re-probe the card now, with the files already on disk"),
         Action("sudo systemctl start bluetooth", "bring the userspace stack up")],
        confidence="medium",
    )


@rule(
    "bt.dracut-missing",
    "the initramfs drop-in for the Bluetooth firmware is not installed",
    "medium",
    "bluetooth",
    ("firmware", "udev"),
)
def bt_dracut_missing(f: Facts) -> Outcome:
    dracut = f.firmware.get("dracut")
    if dracut is None:
        dracut = (f.udev or {}).get("/etc/dracut.conf.d/60-oxp3-bt-firmware.conf", {}).get("present")
    if dracut is None:
        return Outcome("unknown", "no dracut drop-in evidence in this bundle")
    if dracut:
        return Outcome("clear", "the dracut drop-in is installed")
    present = _firmware_present(f)
    evidence = []
    hit = f.bundle.first_line("firmware", r"60-oxp3-bt-firmware\.conf\s+MISSING")
    if hit:
        evidence.append(f.bundle.evidence_line("firmware", hit[0]))
    hit = f.bundle.first_line("udev", r"60-oxp3-bt-firmware\.conf\s+MISSING")
    if hit:
        evidence.append(f.bundle.evidence_line("udev", hit[0]))
    return Outcome(
        "triggered",
        "The blobs are on disk (%s) but the dracut drop-in is missing, so a rebuilt "
        "initramfs will not carry them: the next reboot re-breaks Bluetooth. This is the "
        "initramfs root cause, not the files." % (
            "2/2 present" if present else "not installed"
        ),
        evidence,
        [Action("sudo tools/oxp3-bt-firmware.sh",
                "writes the drop-in and enables the ostree initramfs"),
         Action("sudo systemctl reboot", "the initramfs change applies on the next boot")],
    )


# -- Sleep / power key ------------------------------------------------------
def _power_action_phrase(kind: str) -> str:
    return {
        "sleep": "suspends",
        "hibernate": "hibernates (unavailable here: /sys/power/disk is [disabled])",
        "shutdown": "shuts the machine down",
        "nothing": "does nothing",
        "lock": "locks the screen",
        "logout": "logs out",
        "unknown": "runs an action that is not Sleep",
        "unset": "leaves the action to PowerDevil's built-in default",
    }.get(kind, "does something else")


@rule(
    "power.power-key-poweroff",
    "the power key would power the machine off instead of suspending",
    "critical",
    "sleep",
    ("session_power", "sleep", "udev", "verify_json"),
)
def power_key_poweroff(f: Facts) -> Outcome:
    session = f.session_power
    if session is None:
        return Outcome(
            "unknown",
            "session/power.txt is not in this bundle, so PowerDevil's effective "
            "PowerButtonAction/LidAction cannot be read; the logind policy alone does not "
            "decide the key while PowerDevil owns it. Collect again with "
            "tools/oxp3-firstrun.sh --collect.",
        )
    if f.inhibitors is None:
        return Outcome(
            "unknown",
            "session/power.txt is present, but sleep/sleep.txt has no inhibitor list, so "
            "whether PowerDevil owns handle-power-key/handle-lid-switch is unknown.",
        )
    owners = [
        row for row in f.inhibitors
        if row["mode"] == "block" and row["handles"]
        and "powerdevil" in row["who"].lower()
    ]
    evidence: List[Evidence] = []
    for row in owners[:1]:
        line = int(row.get("line") or 0)
        if line:
            evidence.append(f.bundle.evidence_line("sleep", line))
    dropin = _logind_dropin(f)
    if dropin is None:
        return Outcome(
            "unknown",
            "PowerDevil owns the keys, but there is no logind drop-in evidence in the bundle.",
        )
    hit = f.bundle.first_line(
        "udev", r"logind\.conf\.d/60-oxp3-sleep\.conf\s+(present|MISSING)")
    if hit:
        evidence.append(f.bundle.evidence_line(
            "udev", hit[0],
            "logind policy: " + ("present" if dropin["present"] else "MISSING")))
    check = f.check("sleep.trigger")
    if check is not None:
        evidence.append(f.ev_check("sleep.trigger"))

    pba = powerdevil_effective(session, "PowerButtonAction")
    lid = powerdevil_effective(session, "LidAction")
    auto = powerdevil_effective(session, "AutoSuspend")
    for entry in (pba, lid, auto):
        if entry and entry.get("line"):
            evidence.append(f.bundle.evidence_line(
                "session_power", entry["line"],
                "%s.%s = %s (%s)" % (entry["group"], entry["key"],
                                     entry["value"] or "(unset)", entry["source"])))
    pba_value = (pba or {}).get("value") or ""
    lid_value = (lid or {}).get("value") or ""
    auto_value = (auto or {}).get("value") or ""
    pba_group = (pba or {}).get("group") or "AC"
    lid_group = (lid or {}).get("group") or "AC"
    pba_kind = powerdevil_action_kind(pba_value)
    lid_kind = powerdevil_action_kind(lid_value)
    parsed = (
        "powerdevilrc: [%s] PowerButtonAction=%s (%s), [%s] LidAction=%s (%s), "
        "AutoSuspend=%s" % (
            pba_group, pba_value or "(unset)", pba_kind,
            lid_group, lid_value or "(unset)", lid_kind,
            auto_value or "(unset)",
        )
    )

    if not owners:
        return Outcome(
            "unknown",
            "PowerDevil does not hold a block inhibitor on handle-power-key/"
            "handle-lid-switch in this bundle, so the KDE action is not what decides the "
            "key; logind's own policy is covered by sleep.trigger-uninstalled. " + parsed,
        )

    if dropin["present"]:
        if pba_kind == "sleep":
            return Outcome(
                "clear",
                "PowerDevil owns the key events and is explicitly set to Sleep, so the "
                "power key suspends through PowerDevil; the logind drop-in "
                "(HandlePowerKey=suspend) is the fallback for a session where PowerDevil is "
                "not running. " + parsed,
                evidence,
            )
        if pba_kind == "unset":
            return Outcome(
                "unknown",
                "PowerDevil owns the key events and the logind drop-in is present, but "
                "PowerButtonAction is not recorded in powerdevilrc, so PowerDevil's "
                "built-in default decides the key and the bundle cannot read it. " + parsed,
                evidence,
            )
        return Outcome(
            "triggered",
            "PowerDevil owns handle-power-key/handle-suspend-key/handle-lid-switch with a "
            "block inhibitor and powerdevilrc sets PowerButtonAction=%s, so on the KDE "
            "desktop PowerDevil wins the event and the power key %s instead of "
            "suspending; the logind drop-in (HandlePowerKey=suspend) is present but only "
            "governs a session where PowerDevil is not running (console, Gamescope-only). "
            "LidAction=%s (%s)." % (
                pba_value or "(unset)", _power_action_phrase(pba_kind),
                lid_value or "(unset)", lid_kind),
            evidence,
            [Action(
                "System Settings > Power Management > Energy Saving: set 'When power "
                "button pressed' = Sleep and 'When laptop lid closed' = Sleep (AC and "
                "Battery)",
                "PowerDevil owns the key event, so the KCM is what decides it",
                privileged=False),
             Action("kreadconfig6 --file powerdevilrc --group AC --key PowerButtonAction",
                    "read the effective KDE action back (empty = KDE default)",
                    privileged=False)],
            severity="medium",
        )

    if pba_kind == "sleep":
        return Outcome(
            "clear",
            "PowerDevil owns the key events and is explicitly set to Sleep, so the power "
            "key suspends while Plasma runs. There is no logind drop-in, so a session "
            "without PowerDevil still falls back to logind's compiled "
            "HandlePowerKey=poweroff; that missing policy is sleep.trigger-uninstalled's "
            "finding. " + parsed,
            evidence,
        )
    if pba_kind == "unset":
        return Outcome(
            "unknown",
            "PowerDevil owns the key events and there is no logind drop-in, but "
            "PowerButtonAction is not recorded in powerdevilrc, so the bundle cannot tell "
            "whether the key sleeps, powers off or does nothing (PowerDevil's built-in "
            "default applies). The missing logind policy is sleep.trigger-uninstalled's "
            "finding. " + parsed,
            evidence,
        )
    return Outcome(
        "triggered",
        "There is no logind drop-in and PowerDevil owns handle-power-key with a block "
        "inhibitor, and powerdevilrc sets PowerButtonAction=%s: the power key %s instead "
        "of suspending. With PowerDevil gone (console, Gamescope-only, a rescue session) "
        "logind's compiled default for HandlePowerKey is poweroff, so the same key shuts "
        "the machine down. LidAction=%s (%s), AutoSuspend=%s." % (
            pba_value or "(unset)", _power_action_phrase(pba_kind),
            lid_value or "(unset)", lid_kind, auto_value or "(unset)"),
        evidence,
        [Action(
            "System Settings > Power Management > Energy Saving: set 'When power button "
            "pressed' = Sleep (AC and Battery) and 'When laptop lid closed' = Sleep",
            "PowerDevil owns the key event, so the KCM is authoritative",
            privileged=False),
         Action("kreadconfig6 --file powerdevilrc --group AC --key PowerButtonAction",
                "read the effective KDE action back (empty = KDE default)",
                privileged=False),
         Action("sudo tools/oxp3-hw-install.sh --sleep",
                "installs the logind drop-in (HandlePowerKey=suspend) and the polkit rule "
                "for a session without PowerDevil"),
         Action("sudo systemctl restart systemd-logind", "logind reads the drop-in at start")],
    )


@rule(
    "polkit.no-session-agent",
    "no session polkit agent can answer an authentication prompt",
    "medium",
    "sleep",
    ("session_power",),
)
def polkit_no_session_agent(f: Facts) -> Outcome:
    session = f.session_power
    if session is None or not session.get("agent"):
        return Outcome(
            "unknown",
            "the session polkit agent state is not recorded in the bundle "
            "(session/power.txt absent or without the pgrep probe).",
        )
    state = session["agent"]
    line = session.get("agent_line")
    evidence = [f.bundle.evidence_line("session_power", line)] if line else []
    if state == "present":
        return Outcome(
            "clear",
            "a session polkit authentication agent is running, so an interactive prompt "
            "is possible on the desktop. A Gamescope/Gamemode session still has no agent "
            "of its own; there the shipped polkit rule is the non-interactive path.",
            evidence,
        )
    if state != "absent":
        return Outcome(
            "unknown",
            "the session polkit agent state is %r, not a clear present/absent result."
            % state,
            evidence,
        )
    return Outcome(
        "triggered",
        "no session polkit authentication agent is running (pgrep found neither "
        "polkit-kde-authentication-agent-1 nor polkit-gnome-authentication-agent-1). "
        "In a Gamescope/Gamemode session there is no agent at all, so a polkit action "
        "that needs an interactive decision - including login1.suspend when the request "
        "is not from the active seat - is denied instead of prompting. That is why the "
        "shipped 49-oxp3-suspend.rules must grant the wheel group without a prompt.",
        evidence,
        [Action("pkcheck --action-id org.freedesktop.login1.suspend --process $$",
                "show which polkit decision applies to this session", privileged=False),
         Action("systemctl --user status plasma-polkit-agent",
                "on Plasma this user unit is the session agent; start it if it is not "
                "running", privileged=False),
         Action("sudo tools/oxp3-hw-install.sh --sleep",
                "installs the polkit rule that permits wheel without a prompt (the "
                "Gamemode path)")],
    )


@rule(
    "sleep.trigger-uninstalled",
    "no suspend trigger policy that works without a desktop session",
    "high",
    "sleep",
    ("udev", "verify_json", "checklist"),
)
def sleep_trigger_uninstalled(f: Facts) -> Outcome:
    dropin = _logind_dropin(f)
    polkit = _polkit_rule(f)
    if dropin is None and polkit is None:
        return Outcome("unknown", "no logind/polkit policy evidence in this bundle")
    check = f.check("sleep.trigger")
    if check is not None and check.get("state") == "pass":
        return Outcome("clear", "sleep.trigger passes: the policy is installed and permitted")
    if (dropin and dropin["present"]) and (polkit and polkit["present"]):
        return Outcome(
            "clear",
            "the logind drop-in and the polkit rule are both installed",
        )
    evidence: List[Evidence] = []
    hit = f.bundle.first_line("udev", r"logind\.conf\.d/60-oxp3-sleep\.conf\s+(present|MISSING)")
    if hit:
        evidence.append(f.bundle.evidence_line("udev", hit[0]))
    hit = f.bundle.first_line("udev", r"49-oxp3-suspend\.rules\s+(present|MISSING)")
    if hit:
        evidence.append(f.bundle.evidence_line("udev", hit[0]))
    if check is not None:
        evidence.append(f.ev_check("sleep.trigger"))
    return Outcome(
        "triggered",
        "Without the logind drop-in + polkit rule, `sleep.trigger` has no permitted path "
        "that does not need a desktop session: a console/Gamescope session or a scripted "
        "`systemctl suspend` can be denied, and the power key has no policy.",
        evidence,
        [Action("sudo tools/oxp3-hw-install.sh --sleep",
                "installs files/logind-oxp3.conf and files/polkit-oxp3-suspend.rules"),
         Action("sudo systemctl restart systemd-logind", "logind reads the drop-in at start")],
    )


@rule(
    "sleep.logind-not-restarted",
    "the logind drop-in is newer than the running logind",
    "high",
    "sleep",
    ("checklist", "manifest", "udev"),
)
def sleep_logind_not_restarted(f: Facts) -> Outcome:
    step = f.step("logind-restart") or f.step("logind restarted after the drop-in")
    if step is None:
        return Outcome("unknown", "the checklist row for the logind restart is missing")
    state = (step.get("state") or "").lower()
    dropin = _logind_dropin(f)
    evidence = []
    hit = f.bundle.first_line("checklist", r"logind restarted")
    if hit:
        evidence.append(f.bundle.evidence_line("checklist", hit[0]))
    if state == "pending":
        evidence.append(Evidence(f.bundle.find("manifest") or "MANIFEST.json", None,
                                 "checklist step logind-restart: %s" % step.get("detail")))
        return Outcome(
            "triggered",
            "The drop-in is installed but was written after systemd-logind started, and "
            "logind only reads its configuration at start. Until it is restarted (or the "
            "machine is rebooted) every logind policy in this bundle is inert.",
            evidence,
            [Action("sudo systemctl restart systemd-logind",
                    "logind re-reads /etc/systemd/logind.conf.d at start")],
        )
    if state in ("ok", "n/a"):
        if dropin is not None and not dropin.get("present"):
            return Outcome(
                "unknown",
                "the checklist says the drop-in was read, but udev/rules.txt says it is "
                "not installed: the two sources disagree (%s)" % (step.get("detail") or "?"),
                [],
                confidence="low",
            )
        return Outcome("clear", "logind started after the drop-in (%s)"
                       % (step.get("detail") or "no detail in the row"))
    return Outcome(
        "unknown",
        "the checklist reports '%s' for the logind restart (%s)"
        % (state or "?", step.get("detail") or "no detail"),
        evidence,
        confidence="medium",
    )


@rule(
    "sleep.no-attempt",
    "no suspend has completed on this boot",
    "info",
    "sleep",
    ("sleep", "checklist", "verify_json"),
)
def sleep_no_attempt(f: Facts) -> Outcome:
    stats = f.suspend.get("stats") if f.suspend else None
    if not stats or "success" not in stats:
        step = f.step("suspend-evidence")
        check = f.check("sleep.stats")
        if step is None and check is None:
            return Outcome("unknown", "no suspend statistics in this bundle")
        evidence = []
        if step is not None:
            hit = f.bundle.first_line("checklist", r"suspend recorded|suspend")
            if hit:
                evidence.append(f.bundle.evidence_line("checklist", hit[0]))
        if check is not None:
            evidence.append(f.ev_check("sleep.stats"))
        conclusion = ("The bundle has no suspend counter, so nothing here proves or "
                      "disproves suspend.")
        return Outcome("unknown", conclusion, evidence, confidence="medium")
    success = stats.get("success_int") or 0
    fail = stats.get("fail_int") or 0
    hw = stats.get("last_hw_sleep_int") or 0
    if success > 0 and (hw > 0 or fail == 0):
        evidence = [f.bundle.evidence_line("sleep", f.suspend["lines"].get("success", 0))
                    if f.suspend["lines"].get("success") else f.bundle.evidence_at(
                        "sleep", "success")]
        return Outcome(
            "clear",
            "a real suspend completed on this boot (success=%d, last_hw_sleep=%d ns)"
            % (success, hw),
            [e for e in evidence if e],
        )
    evidence = []
    if f.suspend["lines"].get("success"):
        evidence.append(f.bundle.evidence_line("sleep", f.suspend["lines"]["success"]))
    step = f.step("suspend-evidence")
    if step is not None:
        hit = f.bundle.first_line("checklist", r"suspend recorded")
        if hit:
            evidence.append(f.bundle.evidence_line("checklist", hit[0]))
    action = Action("sudo systemctl suspend   # owner-run test; the collector never suspends",
                    "one real suspend/resume cycle is the only proof")
    return Outcome(
        "triggered",
        "suspend_stats/success is still 0 and no failed_* counter moved, so nothing has "
        "asked the kernel to sleep on this boot. That is not proof that suspend is broken "
        "- it means the trigger path was not exercised. The hardware-sleep numbers are "
        "not evidence of a sleep either: `max_hw_sleep` is a reporting ceiling set at "
        "probe, not a duration (docs/hw/sleep-power.md 2.6), so `success=0` with a "
        "non-zero `max_hw_sleep` is coherent, not stale. Note the known trap: "
        "`journalctl -k` implies `-b`, so history needs `-b all`.",
        evidence,
        [action],
        severity="medium" if _sleep_trigger_broken(f) else "info",
    )


def _sleep_trigger_broken(f: Facts) -> bool:
    dropin = _logind_dropin(f)
    polkit = _polkit_rule(f)
    return not (dropin and dropin.get("present")) or not (polkit and polkit.get("present"))


@rule(
    "sleep.stats-inconsistent",
    "hardware-sleep counters without a completed suspend",
    "info",
    "sleep",
    ("sleep", "verify_json"),
)
def sleep_stats_inconsistent(f: Facts) -> Outcome:
    """Explain `last_hw_sleep`/`max_hw_sleep` when `success` is 0.

    The three hw_* attributes are not written by the generic suspend path: only the
    platform PMC driver reports them, and `max_hw_sleep` is a probe-time reporting
    ceiling, not a measurement (docs/hw/sleep-power.md 2.6). A non-zero max with
    success=0 is therefore coherent; a non-zero `last_hw_sleep` with success=0 needs
    this boot's own journal and CLOCK_BOOTTIME before it means anything.
    """
    stats = (f.suspend or {}).get("stats") or {}
    if "success" not in stats:
        return Outcome("unknown", "no suspend statistics in this bundle")
    success = stats.get("success_int") or 0
    fail = stats.get("fail_int") or 0
    last = stats.get("last_hw_sleep_int") or 0
    total = stats.get("total_hw_sleep_int") or 0
    peak = stats.get("max_hw_sleep_int") or 0
    if success > 0:
        return Outcome(
            "clear",
            "the counters describe a completed cycle (success=%d, fail=%d, "
            "last_hw_sleep=%d us, max_hw_sleep=%d us); judge the duration against "
            "CLOCK_BOOTTIME, not against max_hw_sleep" % (success, fail, last, peak),
        )
    if last == 0 and peak == 0:
        return Outcome(
            "clear",
            "no completed suspend and no hardware-sleep number: the tuple is coherent "
            "with a boot that never suspended",
        )
    lines = (f.suspend or {}).get("lines", {})
    evidence = []
    for name in ("success", "fail", "last_hw_sleep", "total_hw_sleep", "max_hw_sleep"):
        if lines.get(name):
            evidence.append(f.bundle.evidence_line("sleep", lines[name]))
    actions = [
        Action("journalctl -b -k | grep -c 'PM: suspend entry'",
               "this boot's kernel log is the direct record of a suspend entry; 0 "
               "matches success=0 (use -b all for history)", privileged=False),
        Action("python3 -c 'import time; print(time.clock_gettime(time.CLOCK_BOOTTIME) "
               "- time.clock_gettime(time.CLOCK_MONOTONIC))'",
               "suspended time since boot: CLOCK_BOOTTIME counts through suspend, "
               "CLOCK_MONOTONIC does not; 0 s means no s2idle time elapsed this boot",
               privileged=False),
        Action("sudo tools/sleep-cycle-test.sh --cycles 1",
               "the authoritative per-cycle BOOTTIME-MONOTONIC measurement; "
               "max_hw_sleep is not a duration"),
    ]
    if last > 0:
        return Outcome(
            "triggered",
            "success=0 but last_hw_sleep=%d us (total_hw_sleep=%d us): only the "
            "platform PMC's resume hook writes those, not the generic suspend path, so "
            "a cycle that reached S0ix and then aborted can leave them non-zero without "
            "counting a success. They are plain in-memory counters (zeroed at boot, "
            "never persisted, sysfs is read-only), so this cannot be a value from an "
            "earlier boot. Compare this boot's journal and BOOTTIME first."
            % (last, total),
            [item for item in evidence if item],
            actions,
            confidence="medium",
        )
    return Outcome(
        "triggered",
        "success=0 with max_hw_sleep=%d us: max_hw_sleep is a hardware reporting "
        "ceiling, not a measured sleep - intel_pmc_core sets it at probe to "
        "(2^32-1) x 122 us = 523986009990 us (about 6.1 days), AMD uses U64_MAX. It "
        "belongs to no suspend, is not persisted, and must never be read as one; with "
        "last_hw_sleep/total_hw_sleep at 0 the tuple is coherent with a boot that never "
        "suspended. The earlier '~524 s' reading was a us/ns unit error." % peak,
        [item for item in evidence if item],
        actions,
    )


@rule(
    "sleep.attempt-failed",
    "a suspend attempt was recorded and did not complete",
    "high",
    "sleep",
    ("sleep", "verify_json"),
)
def sleep_attempt_failed(f: Facts) -> Outcome:
    stats = (f.suspend or {}).get("stats") or {}
    if not stats:
        return Outcome("unknown", "no suspend statistics in this bundle")
    fail = stats.get("fail_int")
    failed_suspend = stats.get("failed_suspend_int")
    failed_resume = stats.get("failed_resume_int")
    success = stats.get("success_int")
    hw = stats.get("last_hw_sleep_int")
    if fail is None or success is None:
        return Outcome("unknown", "suspend counters are not readable in this bundle")
    reasons = []
    if (fail or 0) > 0:
        reasons.append("fail=%s" % fail)
    if (failed_suspend or 0) > 0:
        reasons.append("failed_suspend=%s" % failed_suspend)
    if (failed_resume or 0) > 0:
        reasons.append("failed_resume=%s" % failed_resume)
    if (success or 0) > 0 and not hw:
        reasons.append("success>0 with last_hw_sleep=0 (the abort-in-3s pattern)")
    if not reasons:
        return Outcome("clear", "no failed suspend counters")
    evidence = []
    for name in ("fail", "failed_suspend", "last_failed_dev", "last_failed_step"):
        if f.suspend["lines"].get(name):
            evidence.append(f.bundle.evidence_line("sleep", f.suspend["lines"][name]))
    return Outcome(
        "triggered",
        "The kernel recorded a failed suspend (%s). Read last_failed_dev/last_failed_step "
        "for the device that refused." % ", ".join(reasons),
        evidence,
        [Action("cat /sys/power/suspend_stats/last_failed_dev "
                "/sys/power/suspend_stats/last_failed_step", "the kernel names the failing "
                "device/step", privileged=False),
         Action("sudo journalctl -k -b | grep -E 'PM:|s2idle|suspend'",
                "the resume/abort reason is in the kernel ring for this boot")],
    )


@rule(
    "sleep.block-inhibitor",
    "a block inhibitor keeps sleep from being requested",
    "medium",
    "sleep",
    ("sleep",),
)
def sleep_block_inhibitor(f: Facts) -> Outcome:
    if f.inhibitors is None:
        return Outcome("unknown", "no inhibitor list in the bundle")
    blocks = [row for row in f.inhibitors if row["mode"] == "block"]
    if not blocks:
        return Outcome("clear", "no block inhibitor")
    powerdevil = [row for row in blocks if "powerdevil" in row["who"].lower()]
    other = [row for row in blocks if "powerdevil" not in row["who"].lower()]
    if not other:
        return Outcome(
            "clear",
            "the only block inhibitor is PowerDevil's handle-* claim, which is KDE doing "
            "its job (see power.power-key-poweroff for the consequence)",
        )
    evidence = [f.bundle.evidence_line("sleep", int(row["line"])) for row in other[:2]]
    return Outcome(
        "triggered",
        "A non-desktop block inhibitor (%s) is held on sleep, so logind will not act on a "
        "session/idle suspend request while it runs." % ", ".join(row["who"] for row in other),
        [e for e in evidence if e],
        [Action("systemd-inhibit --list", "re-read the holders before chasing the kernel",
                privileged=False)],
    )


# -- XMX --------------------------------------------------------------------
@rule(
    "xmx.ocloc-missing",
    "the XMX compiler (ocloc) is missing, so every CM compile fails",
    "critical",
    "graphics",
    ("xmx", "status_json", "verify_json"),
)
def xmx_ocloc_missing(f: Facts) -> Outcome:
    if not _xmx_installed(f):
        return Outcome("not-applicable", "XMX is not configured on this host")
    missing = f.xmx.get("ocloc_missing")
    indicator = f.indicator("xmx.igc")
    check = f.check("gfx.xmx.igc")
    if indicator is not None:
        if indicator.get("state") in ("ok", "warn") and not missing:
            return Outcome("clear", "ocloc is present (%s)" % indicator.get("value"))
        if indicator.get("state") in ("fail", "unknown") or missing:
            evidence = [f.ev_indicator("xmx.igc")]
            if check is not None:
                evidence.append(f.ev_check(check["id"]))
            return _ocloc_outcome(evidence)
    if check is not None:
        if check.get("state") == "pass":
            return Outcome("clear", "gfx.xmx.igc passes (%s)" % check.get("summary"))
        if check.get("state") in ("fail", "skip"):
            return _ocloc_outcome([f.ev_check("gfx.xmx.igc")])
    if missing:
        return _ocloc_outcome([
            f.bundle.evidence_at("xmx", "ocloc: not found",
                                 "ocloc: not found (no PATH entry, no XMX install)")
        ])
    return Outcome("unknown", "no ocloc evidence (no status indicator, no gfx/xmx.txt line)")


def _ocloc_outcome(evidence: Sequence[Optional[Evidence]]) -> Outcome:
    return Outcome(
        "triggered",
        "No working ocloc means every CM kernel compile fails (`cm-compile: ocloc not "
        "found`), XeSS aborts at init and the game silently resets upscaling back to Off. "
        "There is no visible error, which is why this is a critical finding and not a "
        "cosmetic one.",
        [e for e in evidence if e],
        [Action("tools/xmx-enable.sh   # refuses to run without a working ocloc",
                "re-asserts the XMX environment and checks the compiler first",
                privileged=False),
         Action("tools/get-igc.sh   # fetches IGC 2.10.10 + neo/bin/ocloc (~95 MB)",
                "the release archive contains no compiler runtime", privileged=False)],
    )


@rule(
    "xmx.driver-mismatch",
    "the XMX Vulkan driver is not the recorded release build",
    "high",
    "graphics",
    ("status_json", "verify_json", "xmx"),
)
def xmx_driver_mismatch(f: Facts) -> Outcome:
    if not _xmx_installed(f):
        return Outcome("not-applicable", "XMX is not configured on this host")
    indicator = f.indicator("xmx.driver")
    value = (indicator or {}).get("value") or ""
    check = f.check("gfx.xmx.driver")
    hash_value = None
    for pattern in (r"([0-9a-f]{16})\s*\(", r"([0-9a-f]{16})"):
        match = re.search(pattern, value) or re.search(pattern, (check or {}).get("summary") or "")
        if match:
            hash_value = match.group(1)
            break
    if indicator is not None and indicator.get("state") == "ok" and "(match)" in value:
        return Outcome("clear", "xmx.driver matches the recorded build (%s)" % value)
    if check is not None and check.get("state") == "pass" and "match" in (
        (check.get("summary") or "") + (check.get("detail") or "")
    ).lower():
        return Outcome("clear", "gfx.xmx.driver passes (%s)" % check.get("summary"))
    bad_state = (indicator or {}).get("state") in ("warn", "fail") or (
        check is not None and check.get("state") == "fail"
    )
    if not bad_state and hash_value is None:
        return Outcome("unknown", "no driver digest in this bundle")
    if hash_value and hash_value == XMX_DRIVER_PREFIX and not bad_state:
        return Outcome("clear", "the driver digest is the recorded %s" % XMX_DRIVER_PREFIX)
    evidence = [f.ev_indicator("xmx.driver")] if indicator else []
    if check is not None:
        evidence.append(f.ev_check("gfx.xmx.driver"))
    return Outcome(
        "triggered",
        "The per-game XMX driver is not %s (got %s). XeSS initialisation fails or "
        "artefacts on a different build, and the game falls back to Off; the shim's CM "
        "path only works with the recorded release binary." % (
            XMX_DRIVER_PREFIX, hash_value or "an unknown digest"),
        [e for e in evidence if e],
        [Action("tools/xmx-enable.sh", "re-points VK_DRIVER_FILES at the recorded release "
                                       "driver and re-checks the digests", privileged=False)],
    )


@rule(
    "xmx.helper-no-nodgrf",
    "the CM compile helper does not filter -doubleGRF",
    "high",
    "graphics",
    ("status_json", "xmx", "verify_json"),
)
def xmx_helper_no_nodgrf(f: Facts) -> Outcome:
    if not _xmx_installed(f):
        return Outcome("not-applicable", "XMX is not configured on this host")
    indicator = f.indicator("xmx.helper")
    check = f.check("gfx.xmx.helper")
    helper = None
    if indicator is not None:
        helper = str(indicator.get("value") or "")
    elif check is not None:
        helper = str(check.get("summary") or "") + str(check.get("detail") or "")
    env_helper = f.xmx.get("env", {}).get("ANV_CM_HELPER")
    if env_helper and not helper:
        helper = env_helper
    if helper is None:
        return Outcome("unknown", "no ANV_CM_HELPER / xmx.helper evidence")
    if XMX_HELPER_MARKER in helper:
        return Outcome("clear", "the helper is the 128-GRF one (%s)" % trim(helper, 80))
    evidence = []
    if indicator is not None:
        evidence.append(f.ev_indicator("xmx.helper"))
    if check is not None:
        evidence.append(f.ev_check("gfx.xmx.helper"))
    if env_helper:
        hit = f.bundle.first_line("xmx", r"^ANV_CM_HELPER=")
        if hit:
            evidence.append(f.bundle.evidence_line("xmx", hit[0]))
    return Outcome(
        "triggered",
        "ANV_CM_HELPER is '%s', not the %s helper: kernels are compiled with -doubleGRF "
        "(256-GRF on Xe3), which produces white-dot artefacts and drops frames. The "
        "shipping configuration uses the 128-GRF filter." % (
            trim(os.path.basename(helper) or helper, 60), XMX_HELPER_MARKER),
        [e for e in evidence if e],
        [Action("ANV_CM_HELPER=%s" % (REPO / "tools/xmx-cm-compile-nodgrf.sh"),
                "put this in the launch environment (~/.config/xess-xmx/oxp3-env.sh) "
                "instead of cm-compile.sh", privileged=False),
         Action("rm -rf ~/.cache/xess-xmx/kernels",
                "the old -doubleGRF cached kernels must be rebuilt", privileged=False)],
    )


@rule(
    "xmx.flatpak-helper",
    "the Steam Flatpak override cannot produce a working XMX launch",
    "medium",
    "graphics",
    ("status_json", "xmx"),
)
def xmx_flatpak_helper(f: Facts) -> Outcome:
    indicator = f.indicator("flatpak.steam_env")
    override = f.xmx.get("flatpak_override") or []
    helper = next((line.split("=", 1)[1] for line in override if line.startswith("ANV_CM_HELPER")
                   ), None)
    if indicator is None and helper is None:
        return Outcome("unknown", "no Steam Flatpak override evidence in this bundle")
    if indicator is not None and indicator.get("state") == "ok":
        return Outcome("clear", "flatpak.steam_env is ok (%s)" % indicator.get("value"))
    if helper is None and indicator is None:
        return Outcome("unknown", "no override content")
    values = " ".join(override)
    problems = []
    if helper and XMX_HELPER_MARKER not in helper:
        problems.append("ANV_CM_HELPER=%s is not the 128-GRF helper" % helper)
    if "VK_DRIVER_FILES" not in values:
        problems.append("no VK_DRIVER_FILES (the override cannot select the XMX driver)")
    if "INTC_ALT" not in values:
        problems.append("no INTC_ALT_DRIVER_EXTENSIONS_PATH (the shim cannot be loaded)")
    if not problems:
        return Outcome("clear", "the Flatpak override carries the expected three variables")
    evidence = []
    if indicator is not None:
        evidence.append(f.ev_indicator("flatpak.steam_env"))
    for line in override:
        if line.startswith("ANV_CM_HELPER"):
            hit = f.bundle.first_line("xmx", re.escape(line[:30]))
            if hit:
                evidence.append(f.bundle.evidence_line("xmx", hit[0]))
    return Outcome(
        "triggered",
        "The Flatpak Steam override is incomplete: %s. A launch that relies on it would "
        "compile -doubleGRF kernels and never load the shim, so XMX cannot work there. "
        "Per-game launch options override it; the override itself is the stale part."
        % "; ".join(problems),
        [e for e in evidence if e],
        [Action("edit ~/.local/share/flatpak/overrides/org.anatase.Steam",
                "either trim the stale ANV_CM_HELPER or complete the three variables; see "
                "docs/hw/flatpak-xmx.md", privileged=False)],
    )


@rule(
    "xmx.trace-absent",
    "no igdext trace: the shim was not loaded for the last launch",
    "low",
    "graphics",
    ("status_json", "verify_json", "xmx"),
)
def xmx_trace_absent(f: Facts) -> Outcome:
    if not _xmx_installed(f):
        return Outcome("not-applicable", "XMX is not configured on this host")
    indicator = f.indicator("xmx.trace")
    check = f.check("gfx.xmx.trace")
    env = f.xmx.get("env") or {}
    if indicator is not None:
        if indicator.get("state") == "ok":
            return Outcome("clear", "an igdext trace was found (%s)" % indicator.get("value"))
        evidence = [f.ev_indicator("xmx.trace")]
        if check is not None:
            evidence.append(f.ev_check("gfx.xmx.trace"))
        missing_alt = "INTC_ALT_DRIVER_EXTENSIONS_PATH" not in env
        if missing_alt:
            hypothesis = (
                "INTC_ALT_DRIVER_EXTENSIONS_PATH is not in the collected session env, so a "
                "launch from this shell would not find the shim at all; the shim is only "
                "reachable through the launcher's per-game environment."
            )
        else:
            hypothesis = (
                "INTC_ALT is set, so the shim should load; either the launch did not have "
                "XeSS enabled/tracing on, or the path does not resolve inside the Wine "
                "prefix (a Windows-style path, forward slashes)."
            )
        return Outcome(
            "triggered",
            "No igdext_trace.log under any known prefix: the shim was not loaded by the "
            "last launch (or tracing was not requested - it is opt-in, so this is expected "
            "unless you enabled it). " + hypothesis,
            [e for e in evidence if e],
            [Action("XESS_LOG=1 <launch the game once with XeSS enabled>",
                    "one traced launch is the only proof that the shim loaded",
                    privileged=False),
             Action("ls -l '<prefix>/drive_c/igdext_trace.log'",
                    "the shim writes the trace inside the game's Wine prefix",
                    privileged=False)],
            severity="low" if not missing_alt else "medium",
            confidence="medium",
        )
    if f.xmx.get("trace"):
        return Outcome("clear", "an igdext_trace.log reference is present in the bundle")
    return Outcome("unknown", "no xmx.trace indicator or trace reference in this bundle")


# -- Audio ------------------------------------------------------------------
@rule(
    "audio.wedged",
    "only the Dummy Output exists although a sound card is present",
    "high",
    "audio",
    ("audio", "verify_json"),
)
def audio_wedged(f: Facts) -> Outcome:
    audio = f.audio or {}
    server = audio.get("server")
    sinks = audio.get("sinks") or []
    default = audio.get("default_sink")
    hardware = [s for s in sinks if "auto_null" not in s and "dummy" not in s.lower()]
    check = f.check("aud.dummy")
    if not sinks and not audio.get("cards") and check is None:
        return Outcome("unknown", "no audio evidence in this bundle")
    if hardware:
        return Outcome("clear", "a hardware/filter sink exists (%s)" % ", ".join(hardware[:3]))
    if check is not None and check.get("state") == "pass":
        return Outcome("clear", "aud.dummy passes (%s)" % check.get("summary"))
    if not server:
        evidence = []
        hit = f.bundle.first_line("audio", r"no server|pactl not installed")
        if hit:
            evidence.append(f.bundle.evidence_line("audio", hit[0]))
        if check is not None:
            evidence.append(f.ev_check("aud.dummy"))
        return Outcome(
            "unknown",
            "No audio server is visible in this context, so 'no audio seat here' and 'the "
            "SOF card is wedged' cannot be told apart. Re-collect from the desktop session "
            "before rebooting.",
            evidence,
            confidence="low",
        )
    evidence = []
    hit = f.bundle.first_line("audio", r"Default Sink:\s*(auto_null|.*dummy)")
    if hit:
        evidence.append(f.bundle.evidence_line("audio", hit[0]))
    hit = f.bundle.first_line("audio", r"^\s*\d+\s+\[")
    if hit:
        evidence.append(f.bundle.evidence_line("audio", hit[0],
                                               "ALSA card visible: " + hit[1].strip()))
    if check is not None:
        evidence.append(f.ev_check("aud.dummy"))
    cards = audio.get("cards") or []
    if cards:
        conclusion = (
            "The sound card is present (%s) but only auto_null/Dummy Output is exposed, so "
            "the SOF/PipeWire side is wedged - the documented throwaway-instance trap. A "
            "reboot is the known recovery." % trim(cards[0], 60)
        )
        actions = [Action("sudo systemctl reboot", "recover the wedged SOF/PipeWire state"),
                   Action("journalctl --user -u pipewire -n 50",
                          "worth keeping as evidence before the reboot", privileged=False)]
    else:
        conclusion = (
            "No ALSA card at all and only auto_null: the SOF driver did not bind this boot, "
            "which is a different fault from a wedged card (check the kernel log for sof-*)."
        )
        actions = [Action("sudo journalctl -k -b | grep -iE 'sof|snd_'",
                          "the card enumeration failure is in the kernel ring", privileged=False),
                   Action("sudo systemctl reboot", "re-probe the card")]
    return Outcome("triggered", conclusion, [e for e in evidence if e], actions)


@rule(
    "audio.eq-inactive",
    "the speaker EQ drop-in is installed but the filter sink is not loaded",
    "medium",
    "audio",
    ("audio", "verify_json"),
)
def audio_eq_inactive(f: Facts) -> Outcome:
    audio = f.audio or {}
    dropin = audio.get("eq_dropin")
    if dropin is None:
        return Outcome("unknown", "no speaker EQ drop-in evidence (audio/audio.txt missing?)")
    if not dropin:
        return Outcome("clear", "the EQ drop-in is not installed (opt-in)")
    if audio.get("eq_loaded"):
        return Outcome("clear", "oxp3_speaker_eq.sink is loaded")
    check = f.check("aud.eq")
    if check is not None and check.get("state") == "pass":
        return Outcome("clear", "aud.eq passes")
    evidence = []
    hit = f.bundle.first_line("audio", r"51-oxp3-speaker-eq\.conf\s+present")
    if hit:
        evidence.append(f.bundle.evidence_line("audio", hit[0]))
    hit = f.bundle.first_line("audio", r"pactl list short sinks")
    if hit:
        evidence.append(f.bundle.evidence_line("audio", hit[0], "sink list follows"))
    if check is not None:
        evidence.append(f.ev_check("aud.eq"))
    return Outcome(
        "triggered",
        "The filter-chain drop-in exists but no oxp3_speaker_eq sink is present, so the "
        "speaker runs without the recovered Harman EQ.",
        [e for e in evidence if e],
        [Action("tools/audio/eq-on.sh", "loads the filter sink and makes it the default",
                privileged=False)],
    )


# -- Volume -----------------------------------------------------------------
@rule(
    "volume.degraded-busy",
    "the volume daemon is DEGRADED: every candidate device is held",
    "high",
    "volume",
    ("journal_units", "journal_user", "verify_json"),
)
def volume_degraded_busy(f: Facts) -> Outcome:
    hits = []
    for key in ("journal_units", "journal_user"):
        for number, line in f.bundle.all_lines(key, r"DEGRADED:"):
            hits.append((key, number, line))
    if not hits:
        check = f.check("vol.topology")
        if check is not None and check.get("state") == "fail":
            return Outcome(
                "triggered",
                "vol.topology predicts a grab conflict: the device the daemon would pick "
                "is already held (hhd). Pick one path - hhd's patched rocker or the "
                "standalone daemon - never both.",
                [f.ev_check("vol.topology")],
                [Action("tools/oxp3-voltap check --kv", "shows the selection and the holder",
                        privileged=False)],
                severity="medium",
            )
        if not f.has("journal_units") and not f.has("journal_user"):
            return Outcome("unknown", "no journal for the daemon units in this bundle")
        return Outcome("clear", "no DEGRADED line from the volume daemon")
    busy = [(k, n, l) for (k, n, l) in hits if "degraded-busy" in l or "EBUSY" in l]
    evidence = [f.bundle.evidence_line(k, n) for (k, n, _l) in (busy or hits)[:2]]
    if busy:
        return Outcome(
            "triggered",
            "Every volume-device candidate is held or refused (EBUSY). hhd grabs the AT "
            "keyboard at startup, so the standalone daemon's topology assumption (hhd "
            "virtual first, physical only when free) does not hold on this host. Pick one "
            "path: hhd's patched rocker OR oxp3-voltap, not both.",
            [e for e in evidence if e],
            [Action("tools/oxp3-voltap check --kv",
                    "prints role/selection/holder without root", privileged=False),
             Action("journalctl -u hhd -b --no-pager | tail -20",
                    "shows hhd holding the device", privileged=False)],
        )
    return Outcome(
        "triggered",
        "The volume daemon logged DEGRADED and found no usable candidate device: the "
        "rocker stays on the stock (buggy) path. It retries, so this is not silence.",
        [e for e in evidence if e],
        [Action("tools/oxp3-voltap check --kv", "shows why no candidate was selected",
                privileged=False),
         Action("sudo systemctl restart oxp3-voltap.service", "re-run discovery once the "
                "device exists")],
    )


@rule(
    "volume.not-installed",
    "the volume-rocker fix is not installed",
    "info",
    "volume",
    ("verify_json", "checklist"),
)
def volume_not_installed(f: Facts) -> Outcome:
    checks = f.checks(["vol."])
    if not checks:
        return Outcome("unknown", "no volume checks in this bundle")
    # vol.topology / vol.holder are read-only capability rows: they can pass while
    # nothing from this fix is installed, so presence is judged on the install rows.
    install_rows = [c for c in checks if c["id"] in ("vol.unit", "vol.grab", "vol.hwdb",
                                                     "vol.hold")]
    if any(check.get("state") in ("pass", "fail") for check in install_rows):
        return Outcome("clear", "the volume fix is present (%s)" % ", ".join(
            "%s=%s" % (c["id"], c["state"]) for c in checks))
    evidence = [f.ev_check("vol.unit")] if f.check("vol.unit") else []
    step = f.step("udev-rules")
    if step is not None:
        hit = f.bundle.first_line("checklist", r"udev rules")
        if hit:
            evidence.append(f.bundle.evidence_line("checklist", hit[0]))
    return Outcome(
        "triggered",
        "No volume fix is installed (all vol.* rows SKIP with 'not installed'): the rocker "
        "still ramps on a hold, which is the stock bug. This is an install step, not a "
        "failure of something installed.",
        [e for e in evidence if e],
        [Action("sudo tools/oxp3-voltap/install.sh install --hwdb",
                "the standalone daemon (pick this OR the hhd patch)")],
    )


# -- Environment / driver stack --------------------------------------------
@rule(
    "env.mesa-mismatch",
    "the host Mesa driver is not the recorded build",
    "medium",
    "graphics",
    ("status_json", "verify_json", "xmx"),
)
def env_mesa_mismatch(f: Facts) -> Outcome:
    indicator = f.indicator("mesa.host")
    check = f.check("gfx.mesa")
    value = ((indicator or {}).get("value") or "") + " " + ((check or {}).get("summary") or "")
    match = re.search(r"([0-9a-f]{16})", value)
    digest = match.group(1) if match else None
    if indicator is not None and indicator.get("state") == "ok":
        return Outcome("clear", "mesa.host matches (%s)" % indicator.get("value"))
    if check is not None and check.get("state") == "pass":
        return Outcome("clear", "gfx.mesa passes (%s)" % check.get("summary"))
    if digest is None:
        return Outcome("unknown", "no host driver digest in this bundle")
    if digest == MESA_PREFIX:
        return Outcome("clear", "the host driver is %s" % MESA_PREFIX)
    evidence = []
    if indicator is not None:
        evidence.append(f.ev_indicator("mesa.host"))
    if check is not None:
        evidence.append(f.ev_check("gfx.mesa"))
    return Outcome(
        "triggered",
        "The host Vulkan driver prefix is %s, not the recorded %s (Mesa 27.0.0-dev in the "
        "/usr overlay). The driver stack is wiped or re-layered by every Anatase update, so "
        "it must be re-asserted. gamescope/HDR behaviour is not covered by the old build."
        % (digest, MESA_PREFIX),
        [e for e in evidence if e],
        [Action("./oxp3-fixes-fast.sh build && ./oxp3-fixes-fast.sh install",
                "rebuild/reinstall the pinned driver stack (the installer escalates itself)")],
    )


@rule(
    "env.kernel-mismatch",
    "the running kernel is not the recorded one",
    "low",
    "system",
    ("env", "manifest", "verify_json"),
)
def env_kernel_mismatch(f: Facts) -> Outcome:
    kernel = f.env.get("kernel") or (f.manifest or {}).get("kernel")
    if not kernel:
        kernel = ((f.indicator("sys.kernel") or {}).get("value") or "").strip() or None
    if not kernel:
        return Outcome("unknown", "no kernel version in this bundle")
    if EXPECTED_KERNEL_PREFIX in kernel:
        return Outcome("clear", "kernel is %s" % kernel)
    return Outcome(
        "triggered",
        "The kernel is %s, not the recorded %s. kmod-oxp3, the patched xe.ko and the "
        "firmware live under /usr and /lib/modules; a kernel change from an Anatase update "
        "means the driver stack must be re-asserted before any of the graphics checks mean "
        "anything." % (kernel, EXPECTED_KERNEL_PREFIX),
        [f.bundle.evidence_at("env", "kernel", "kernel: " + kernel)],
        [Action("rpm-ostree status --booted", "confirm the deployment and LocalPackages",
                privileged=False),
         Action("./oxp3-fixes-fast.sh build && ./oxp3-fixes-fast.sh install",
                "re-assert the driver stack against the new kernel")],
    )


# -- Verification summary --------------------------------------------------
# UNKNOWN rows that another rule explains in its own words; they must not also
# produce verify.unknown. sleep.stats is explained by sleep.stats-inconsistent
# (success=0 with a hardware-sleep number is a counter-semantics question, not a
# harness failure).
COVERED_UNKNOWNS = {"aud.dummy", "sleep.stats"}

COVERED_FAILS = {
    "bt.journal",
    "bt.controller",
    "bt.diag",
    "aud.dummy",
    "sleep.stats",
    "sleep.trigger",
    "vol.topology",
    "gfx.xmx.trace",
    "gfx.xmx.driver",
    "gfx.xmx.helper",
    "gfx.xmx.igc",
    "gfx.mesa",
    "pwr.pl1",
    "aud.eq",
}


@rule(
    "verify.failures",
    "the acceptance harness reports failures no other rule explains",
    "high",
    "system",
    ("verify_json",),
)
def verify_failures(f: Facts) -> Outcome:
    if not f.verify or f.verify.get("error") or not f.verify.get("checks"):
        return Outcome("unknown", "no usable verify/verify.json in this bundle")
    fails = [c for c in f.verify["checks"].values() if c.get("state") == "fail"]
    if not fails:
        return Outcome("clear", "no FAIL rows")
    unexplained = [c for c in fails if c["id"] not in COVERED_FAILS]
    if not unexplained:
        return Outcome(
            "clear",
            "every FAIL row is covered by a specific rule (%s)"
            % ", ".join(c["id"] for c in fails),
        )
    evidence = [f.ev_check(c["id"]) for c in unexplained[:4]]
    return Outcome(
        "triggered",
        "The verifier FAILs %s with no specific rule: read the remediation field of each "
        "row (it is the documented fix)." % ", ".join(c["id"] for c in unexplained),
        [e for e in evidence if e],
        [Action(trim(unexplained[0].get("remediation") or "", 160) or
                "tools/oxp3-verify.sh", "the harness prints the fix next to the row")],
        severity="medium",
    )


@rule(
    "verify.not-installed",
    "components are simply not installed (SKIP is not a failure)",
    "medium",
    "system",
    ("verify_json", "checklist"),
)
def verify_not_installed(f: Facts) -> Outcome:
    if not f.verify or f.verify.get("error") or not f.verify.get("checks"):
        return Outcome("unknown", "no usable verify/verify.json in this bundle")
    groups: "OrderedDict[str, int]" = OrderedDict()
    for check in f.verify["checks"].values():
        if _skip_category(check) == "not-installed":
            group = check.get("group") or "?"
            groups[group] = groups.get(group, 0) + 1
    if not groups:
        return Outcome("clear", "no 'not installed' SKIP rows")
    evidence = []
    for cid in ("vol.unit", "hid.rule", "sleep.units", "decky.dir"):
        check = f.check(cid)
        if check is not None and _skip_category(check) == "not-installed":
            ev = f.ev_check(cid)
            if ev:
                evidence.append(ev)
    step = f.step("hw-install")
    if step is not None and (step.get("state") or "").lower() in ("todo", "partial"):
        hit = f.bundle.first_line("checklist", r"hardware install")
        if hit:
            evidence.append(f.bundle.evidence_line("checklist", hit[0]))
    return Outcome(
        "triggered",
        "%d check(s) SKIP because the component is not installed (%s). That is the "
        "documented install state, not a defect: the harness would FAIL if something were "
        "installed and broken." % (
            sum(groups.values()),
            ", ".join("%s=%d" % (g, n) for g, n in groups.items()),
        ),
        evidence,
        [Action("sudo tools/oxp3-hw-install.sh --all",
                "installs the hardware-fix set (review with --dry-run first)"),
         Action("tools/oxp3-firstrun.sh --collect",
                "re-collect after the reboot to compare pre/post", privileged=False)],
    )


@rule(
    "verify.unknown",
    "the harness could not assert some rows",
    "low",
    "system",
    ("verify_json",),
)
def verify_unknown(f: Facts) -> Outcome:
    if not f.verify or f.verify.get("error") or not f.verify.get("checks"):
        return Outcome("unknown", "no usable verify/verify.json in this bundle")
    unknowns = [
        c for c in f.verify["checks"].values()
        if c.get("state") == "unknown" and c["id"] not in COVERED_UNKNOWNS
    ]
    if not unknowns:
        return Outcome("clear", "no UNKNOWN rows outside the rules that explain them")
    evidence = [f.ev_check(c["id"]) for c in unknowns[:4]]
    return Outcome(
        "triggered",
        "%d UNKNOWN row(s): %s. These are readable-but-not-assertable states (the harness "
        "says why in the detail); they are neither pass nor fail." % (
            len(unknowns),
            ", ".join("%s (%s)" % (c["id"], c.get("summary")) for c in unknowns),
        ),
        [e for e in evidence if e],
        [Action(trim(unknowns[0].get("remediation") or "", 160) or "tools/oxp3-verify.sh",
                "the row's remediation field names the manual check")],
    )


@rule(
    "verify.optins",
    "opt-in components are not enabled (expected on this machine)",
    "info",
    "system",
    ("verify_json",),
)
def verify_optins(f: Facts) -> Outcome:
    if not f.verify or f.verify.get("error") or not f.verify.get("checks"):
        return Outcome("unknown", "no usable verify/verify.json in this bundle")
    optins = [c for c in f.verify["checks"].values() if _skip_category(c) == "opt-in"]
    if not optins:
        return Outcome("clear", "no opt-in SKIP rows")
    evidence = [f.ev_check(c["id"]) for c in optins[:4]]
    return Outcome(
        "clear",
        "Opt-in components are off by design and are not findings: %s. A healthy machine "
        "shows these SKIPs; enable them only if you want the feature."
        % ", ".join(c["id"] for c in optins),
        [e for e in evidence if e],
        [],
    )


@rule(
    "decky.not-installed",
    "the Decky status plugin is not installed",
    "low",
    "decky",
    ("verify_json", "decky"),
)
def decky_not_installed(f: Facts) -> Outcome:
    checks = f.checks(["decky."])
    if not checks:
        return Outcome("unknown", "no decky checks in this bundle")
    presence = [f.check(cid) for cid in ("decky.dir", "decky.backend")]
    if any(check is not None and check.get("state") in ("pass", "fail") for check in presence):
        return Outcome("clear", "the plugin is present (%s)" % ", ".join(
            "%s=%s" % (c["id"], c["state"]) for c in checks))
    if any(check.get("state") == "fail" for check in checks):
        return Outcome("triggered", "the plugin directory exists but the backend does not "
                                    "import; see decky.backend",
                       [f.ev_check("decky.backend")],
                       [Action("python3 -c 'import main'   # in the plugin directory",
                               "the backend's import error is the finding",
                               privileged=False)])
    hit = f.bundle.first_line("decky", r"not installed")
    evidence = []
    if hit:
        evidence.append(f.bundle.evidence_line("decky", hit[0]))
    evidence.append(f.ev_check("decky.dir")) if f.check("decky.dir") else None
    return Outcome(
        "triggered",
        "No ~/homebrew/plugins/oxp3-status: the Decky panel (the same status backend the "
        "verifier uses) is not installed.",
        [e for e in evidence if e],
        [Action("cp -r tools/decky-oxp3-status ~/homebrew/plugins/oxp3-status",
                "install the plugin directory", privileged=False)],
    )


# -- Bundle hygiene --------------------------------------------------------
@rule(
    "bundle.gaps",
    "the evidence bundle is incomplete, so some conclusions are limited",
    "low",
    "bundle",
    ("manifest", "verify_json", "status_json"),
)
def bundle_gaps(f: Facts) -> Outcome:
    gaps = f._cache.get("gaps") or []
    if not gaps:
        return Outcome("clear", "every expected artifact is present")
    # A gap names a file that is *not* in the bundle, so the evidence points at the
    # artifact that records the inventory (the manifest), not at the absent path.
    anchor = f.bundle.find("manifest") or f.bundle.find("checklist") or \
        f.bundle.find("verify_json")
    evidence = []
    for gap in gaps[:4]:
        rel = gap.get("file")
        if rel and (f.bundle.root / rel).is_file():
            evidence.append(Evidence(rel, None, gap["detail"]))
        elif anchor:
            evidence.append(Evidence(
                anchor, None,
                "missing artifact %s: %s" % (gap.get("artifact") or "?", gap["detail"]),
            ))
    return Outcome(
        "triggered",
        "%d evidence gap(s): %s. Rules that need those files report `unknown` instead of "
        "guessing." % (
            len(gaps),
            "; ".join("%s: %s" % (g.get("artifact") or "?", g.get("detail")) for g in gaps[:4]),
        ),
        evidence,
        [Action("tools/oxp3-firstrun.sh --collect",
                "collect a complete bundle from the desktop session", privileged=False)],
        severity="low",
    )


@rule(
    "units.failed",
    "one or more oxp3 units are in the failed state",
    "high",
    "system",
    ("units_system", "journal_units"),
)
def units_failed(f: Facts) -> Outcome:
    if f.failed_units is not None and not f.has("units_system"):
        return Outcome("unknown", "no units/system.txt in this bundle")
    if not f.failed_units:
        return Outcome("clear", "systemctl --failed is empty")
    evidence = []
    for name in f.failed_units[:3]:
        hit = f.bundle.first_line("units_system", re.escape(name.split()[0]))
        if hit:
            evidence.append(f.bundle.evidence_line("units_system", hit[0]))
    first = f.failed_units[0].split()[0]
    return Outcome(
        "triggered",
        "Failed units: %s. A unit that failed once at boot can stay dead (ConditionPathExists "
        "gates start, not health), so the device may look 'not installed'." %
        ", ".join(u.split()[0] for u in f.failed_units[:5]),
        [e for e in evidence if e],
        [Action("systemctl --failed --no-pager", "the exact unit list", privileged=False),
         Action("sudo systemctl restart %s" % first, "retry it once, then read the log"),
         Action("journalctl -u %s -b --no-pager | tail -40" % first,
                "the failure reason is in this boot's journal", privileged=False)],
    )


@rule(
    "udev.hwdb-not-compiled",
    "the volume-key hwdb is newer than the compiled hwdb.bin",
    "medium",
    "volume",
    ("udev",),
)
def udev_hwdb_not_compiled(f: Facts) -> Outcome:
    hwdb = None
    for path, entry in (f.udev or {}).items():
        if "84-oxp3-volume-keys" in path:
            hwdb = entry
            break
    hit = f.bundle.first_line("udev", r"/etc/udev/hwdb\.bin\s+present,\s+mtime\s+(\S+)")
    compiled = _parse_iso(hit[1].split("mtime")[-1].strip()) if hit else None
    if hwdb is None and hit is None:
        return Outcome("unknown", "no hwdb evidence in this bundle")
    if hwdb is None or not hwdb.get("present"):
        return Outcome("clear", "no volume-key hwdb installed")
    if compiled is None:
        return Outcome("unknown", "hwdb.bin mtime is unreadable")
    source = _parse_iso(hwdb.get("mtime"))
    if source and source > compiled:
        return Outcome(
            "triggered",
            "The .hwdb source is newer than /etc/udev/hwdb.bin, so the force-release rule "
            "is not compiled in yet and a volume hold can still ramp.",
            [f.bundle.evidence_at("udev", "84-oxp3-volume-keys")],
            [Action("sudo systemd-hwdb update && sudo udevadm trigger --subsystem-match=input",
                    "recompile and re-apply the hwdb")],
        )
    return Outcome("clear", "hwdb.bin is newer than the source (compiled)")


@rule(
    "power.pl1-mismatch",
    "the two RAPL zones disagree about PL1",
    "high",
    "power",
    ("verify_json", "power"),
)
def power_pl1_mismatch(f: Facts) -> Outcome:
    check = f.check("pwr.pl1")
    if check is None:
        return Outcome("unknown", "no pwr.pl1 row in this bundle")
    if check.get("state") == "pass":
        return Outcome("clear", "PL1 matches on both zones (%s)" % check.get("summary"))
    if check.get("state") != "fail":
        return Outcome("unknown", "pwr.pl1 is %s" % check.get("state"))
    return Outcome(
        "triggered",
        "MSR and MMIO PL1 disagree, and the lower one caps the package: the saved profile "
        "is not actually in force.",
        [f.ev_check("pwr.pl1")],
        [Action("sudo systemctl restart oxp3-tdp-sync.service", "re-asserts the paired limits"),
         Action("sudo /usr/local/libexec/oxp3-powerctl status", "read back both zones")],
    )


@rule(
    "power.profile-unsaved",
    "no power profile has been saved",
    "low",
    "power",
    ("checklist", "verify_json", "power"),
)
def power_profile_unsaved(f: Facts) -> Outcome:
    check = f.check("pwr.profile")
    step = f.step("power-profile")
    if check is None and step is None:
        return Outcome("unknown", "no pwr.profile row and no checklist step")
    if check is not None and check.get("state") == "pass":
        return Outcome("clear", "the saved profile is applied")
    if step is not None and (step.get("state") or "").lower() == "ok":
        return Outcome("clear", "the saved profile is applied (%s)" % step.get("detail"))
    hit = f.bundle.first_line("power", r"\(not saved\)")
    evidence = []
    if hit:
        evidence.append(f.bundle.evidence_line("power", hit[0]))
    if check is not None:
        evidence.append(f.ev_check("pwr.profile"))
    if not evidence:
        return Outcome(
            "unknown",
            "the checklist row is '%s' and the bundle has no power/power.txt or pwr.profile "
            "row to confirm it" % ((step or {}).get("state") or "absent"),
            confidence="low",
        )
    return Outcome(
        "triggered",
        "No /etc/oxp3/power.json: the boot profile (PL1/PL2/PL4 + fan mode) is whatever the "
        "last writer left, so a reboot can change the power envelope.",
        [e for e in evidence if e],
        [Action("sudo /usr/local/libexec/oxp3-powerctl profile balanced --save",
                "persists the profile so boot re-applies it")],
        severity="low",
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def build_facts(bundle: Bundle) -> Facts:
    verify = parse_verify(bundle)
    status = parse_status(bundle)
    manifest = parse_manifest(bundle)
    env = parse_env(bundle)
    boot_wall = None
    if manifest and manifest.get("generated_at") and env.get("uptime_s") is not None:
        generated = _parse_iso(str(manifest.get("generated_at")))
        if generated:
            boot_wall = generated - _dt.timedelta(seconds=env["uptime_s"])
    facts = Facts(
        bundle=bundle,
        verify=verify,
        status=status,
        manifest=manifest,
        checklist=parse_checklist(bundle),
        udev=parse_udev_files(bundle),
        firmware=parse_firmware(bundle),
        units=parse_units(bundle),
        failed_units=parse_failed_units(bundle),
        suspend=parse_suspend(bundle),
        inhibitors=parse_inhibitors(bundle),
        session_power=parse_session_power(bundle),
        audio=parse_audio(bundle),
        xmx=parse_xmx(bundle),
        env=env,
        boot_wall=boot_wall,
    )
    facts._cache["gaps"] = find_gaps(bundle, manifest, verify, status)
    return facts


def find_gaps(
    bundle: Bundle,
    manifest: Optional[Dict[str, Any]],
    verify: Optional[Dict[str, Any]],
    status: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    gaps: List[Dict[str, Any]] = []
    if bundle.kind == "state-dir":
        gaps.append(
            {
                "artifact": "bundle layout",
                "file": None,
                "detail": "input is a docs/state snapshot, not a firstrun bundle: units, "
                          "udev, firmware, sleep, audio and journal evidence are absent",
            }
        )
    for key in EXPECTED_ARTIFACTS:
        if bundle.has(key):
            continue
        gaps.append(
            {
                "artifact": key,
                "file": ARTIFACTS[key][0],
                "detail": "missing; rules that read it report unknown",
            }
        )
    if verify and verify.get("error"):
        gaps.append({"artifact": "verify_json", "file": verify.get("rel"),
                     "detail": verify["error"]})
    if status is not None and status.get("error"):
        gaps.append({"artifact": "status_json", "file": status.get("rel"),
                     "detail": status["error"]})
    if manifest and isinstance(manifest.get("journal"), dict):
        if not manifest["journal"].get("available"):
            gaps.append(
                {
                    "artifact": "journal",
                    "file": "MANIFEST.json",
                    "detail": "journal not available to the collector (%s)"
                              % (manifest["journal"].get("detail") or "?"),
                }
            )
    for entry in (manifest or {}).get("artifacts") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") not in (None, "ok"):
            gaps.append(
                {
                    "artifact": entry.get("path") or "?",
                    "file": entry.get("path"),
                    "detail": "status=%s%s" % (
                        entry.get("status"),
                        (" (%s)" % entry["reason"]) if entry.get("reason") else "",
                    ),
                }
            )
        elif entry.get("truncated"):
            gaps.append(
                {
                    "artifact": entry.get("path") or "?",
                    "file": entry.get("path"),
                    "detail": "truncated: %s" % (entry.get("reason") or "unknown reason"),
                }
            )
    return gaps


def analyse(bundle: Bundle) -> Dict[str, Any]:
    facts = build_facts(bundle)
    results: List[Tuple[Rule, Outcome]] = []
    for item in RULES:
        if not any(facts.has(key) for key in item.reads):
            outcome = Outcome(
                "unknown",
                "no evidence: none of the files this rule reads is in the bundle (%s)"
                % ", ".join(ARTIFACTS[key][0] for key in item.reads),
            )
        else:
            try:
                outcome = item.fn(facts)
            except Exception as exc:  # a rule must never take the report down
                outcome = Outcome(
                    "unknown", "rule failed to evaluate: %s: %s" % (type(exc).__name__, exc)
                )
        results.append((item, outcome))

    findings: List[Dict[str, Any]] = []
    for order, (item, outcome) in enumerate(results):
        if outcome.state != "triggered":
            continue
        severity = outcome.severity or item.severity
        findings.append(
            {
                "id": "F%d" % (len(findings) + 1),
                "rule": item.id,
                "title": item.title,
                "severity": severity,
                "priority": SEV_PRIORITY.get(severity, 10),
                "category": item.category,
                "conclusion": outcome.conclusion,
                "confidence": outcome.confidence,
                "evidence": [e.as_dict() for e in outcome.evidence],
                "actions": [a.as_dict() for a in outcome.actions],
                "_order": order,
            }
        )
    findings.sort(key=lambda item: (-item["priority"], item["_order"]))
    for index, finding in enumerate(findings, 1):
        finding["id"] = "F%d" % index
        finding.pop("_order", None)

    actions: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for finding in findings:
        for action in finding["actions"]:
            command = action["command"]
            if command in seen:
                if finding["rule"] not in seen[command]["rules"]:
                    seen[command]["rules"].append(finding["rule"])
                continue
            record = {
                "rank": 0,
                "command": command,
                "why": action["why"],
                "privileged": action["privileged"],
                "note": action["note"],
                "priority": finding["priority"],
                "severity": finding["severity"],
                "rules": [finding["rule"]],
            }
            seen[command] = record
            actions.append(record)
    actions.sort(key=lambda item: -item["priority"])
    for index, action in enumerate(actions, 1):
        action["rank"] = index

    report = {
        "schema": SCHEMA,
        "tool": "oxp3-triage",
        "version": VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": {
            "path": bundle.source,
            "kind": bundle.kind,
            "root": str(bundle.root),
            "files": count_files(bundle.root),
        },
        "bundle": bundle_summary(facts),
        "environment": environment_summary(facts),
        "fixes": fix_verdicts(facts),
        "findings": findings,
        "actions": actions,
        "rules": [
            outcome.as_dict(item, {key: facts.has(key) for key in item.reads})
            for item, outcome in results
        ],
        "gaps": facts._cache.get("gaps") or [],
        "notes": build_notes(facts, findings),
    }
    return report


def count_files(root: Path) -> int:
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(str(root)):
        total += len(filenames)
    return total


def bundle_summary(facts: Facts) -> Dict[str, Any]:
    manifest = facts.manifest or {}
    verify = facts.verify or {}
    return {
        "tool": manifest.get("tool"),
        "version": manifest.get("version"),
        "generated_at": manifest.get("generated_at"),
        "mode": manifest.get("mode"),
        "root": manifest.get("root"),
        "kernel": manifest.get("kernel") or facts.env.get("kernel"),
        "os": manifest.get("os") or facts.env.get("os"),
        "counts": manifest.get("counts") or {},
        "journal": manifest.get("journal") or {},
        "partial_data": manifest.get("partial_data"),
        "limits": manifest.get("limits") or {},
        "verify": {
            "counts": verify.get("counts") or {},
            "exit_code": verify.get("exit_code"),
            "present": bool(verify and verify.get("checks")),
        },
    }


def environment_summary(facts: Facts) -> Dict[str, Any]:
    status = facts.status or {}
    indicators = status.get("indicators") or {}
    kernel = (
        facts.env.get("kernel")
        or (facts.manifest or {}).get("kernel")
        or (facts.verify or {}).get("kernel")
        or (indicators.get("sys.kernel") or {}).get("value")
    )
    os_name = facts.env.get("os") or (facts.manifest or {}).get("os")
    gpu = (indicators.get("sys.gpu") or {}).get("value")
    if not gpu:
        hit = facts.bundle.first_line("hardware", r"(VGA|Display).*controller")
        gpu = hit[1].strip() if hit else None
    mesa = (indicators.get("mesa.host") or {}).get("value")
    xmx_driver = (indicators.get("xmx.driver") or {}).get("value")
    session = session_summary(facts)
    boot = {
        "uptime_s": facts.env.get("uptime_s"),
        "boot_wall_utc": facts.boot_wall.strftime("%Y-%m-%dT%H:%M:%SZ")
        if facts.boot_wall
        else None,
        "journal_available": ((facts.manifest or {}).get("journal") or {}).get("available"),
        "git": facts.env.get("git") or {},
        "cmdline": facts.env.get("cmdline"),
        "dmi": facts.env.get("dmi") or {},
    }
    return {
        "kernel": kernel,
        "os": os_name,
        "session": session,
        "gpu": gpu,
        "mesa": mesa,
        "xmx_driver": xmx_driver,
        "cpu": (indicators.get("sys.cpu") or {}).get("value"),
        "battery": (indicators.get("sys.battery") or {}).get("value"),
        "thermal": (indicators.get("sys.thermal") or {}).get("value"),
        "boot": boot,
    }


def session_summary(facts: Facts) -> Dict[str, Any]:
    env_section = facts.xmx.get("env") or {}
    sources: List[str] = []
    session_type = "unknown"
    desktop = "unknown"
    for name in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        if name in env_section:
            value = env_section[name]
            sources.append("gfx/xmx.txt (%s=%s)" % (name, value))
            if name == "XDG_SESSION_TYPE" and value:
                session_type = value.strip().lower()
            elif name in ("WAYLAND_DISPLAY", "DISPLAY") and session_type == "unknown":
                session_type = "wayland" if name == "WAYLAND_DISPLAY" else "x11"
    logind_type = (facts.session_power or {}).get("logind", {}).get("Type", "")
    if session_type == "unknown" and logind_type in ("wayland", "x11", "mir"):
        session_type = logind_type
        sources.append("session/power.txt (logind Type=%s)" % logind_type)
    display_text = facts.bundle.text("display") or ""
    if "kscreen" in display_text.lower() or "connected outputs" in display_text.lower():
        desktop = "KDE Plasma (kscreen output)"
        sources.append("display/display.txt (kscreen-doctor output)")
        if session_type == "unknown":
            session_type = "wayland"
    if facts.has("units_user") and "no user systemd bus" not in (
        facts.bundle.text("units_user") or ""
    ):
        sources.append("units/user.txt (user session bus reachable)")
    gamescope = "no evidence"
    for key in ("display", "repo_test", "env", "journal_kernel", "checklist", "xmx"):
        hit = facts.bundle.first_line(key, r"(?i)gamescope")
        if hit:
            gamescope = "mentioned in %s:%d" % (facts.bundle.find(key), hit[0])
            sources.append("%s:%d (gamescope)" % (facts.bundle.find(key), hit[0]))
            break
    return {
        "type": session_type,
        "desktop": desktop,
        "gamescope": gamescope,
        "confidence": "inferred" if sources else "unknown",
        "sources": sources,
    }


# (id, title, checks that describe the fix, checks that prove it is *installed*)
# The presence list matters: a read-only capability row (e.g. vol.topology, which
# passes without the daemon) must not turn "not installed" into "partial".
FIXES: Tuple[Tuple[str, str, Tuple[str, ...], Optional[Tuple[str, ...]]], ...] = (
    ("volume", "Volume rocker",
     ("vol.unit", "vol.grab", "vol.hwdb", "vol.topology", "vol.holder", "vol.hold"),
     ("vol.unit", "vol.grab", "vol.hwdb", "vol.hold")),
    ("leds", "Home button + top LEDs",
     ("hid.rule", "led.status", "led.zones", "hid.access", "homebtn.unit",
      "homebtn.mapping", "homebtn.press", "led.write"),
     ("hid.rule", "homebtn.unit", "homebtn.mapping", "led.write")),
    ("rgb", "Body RGB keeper", ("rgbd.unit", "rgbd.check"), ("rgbd.unit", "rgbd.check")),
    ("daemons", "Daemon lifecycle (resume/hotplug)",
     ("life.fd", "life.reconnect", "life.sim"), ("life.fd", "life.reconnect")),
    ("sleep", "Suspend + wake policy",
     ("sleep.mem", "sleep.stats", "sleep.units", "sleep.wakeup", "sleep.trigger",
      "sleep.audit", "sleep.suspend"),
     ("sleep.units", "sleep.wakeup", "sleep.trigger", "sleep.suspend")),
    ("power", "Power envelope + fan",
     ("pwr.zones", "pwr.pl1", "pwr.profile", "pwr.fan", "pwr.adaptive", "pwr.rapl"),
     ("pwr.profile", "pwr.adaptive", "pwr.rapl")),
    ("battery", "Battery care", ("bat.threshold", "bat.behaviour"), None),
    ("audio", "Speaker EQ",
     ("aud.scripts", "aud.server", "aud.eq", "aud.dummy", "aud.model", "aud.activate"),
     ("aud.eq", "aud.activate")),
    ("display", "Display HDR / ICC / vibrance",
     ("dsp.hdr", "dsp.icc", "dsp.panel", "dsp.vibrance"), None),
    ("graphics", "Mesa + XMX + xe", ("gfx.",), ("gfx.",)),
    ("sched", "sched_ext gaming scheduler", ("sched.",), ("sched.",)),
    ("decky", "Decky status plugin",
     ("decky.dir", "decky.backend", "decky.status"),
     ("decky.dir", "decky.backend")),
    ("imu", "IMU", ("imu.selftest", "imu.iio"), None),
    ("bluetooth", "Bluetooth firmware", ("bt.",), ("bt.files", "bt.dracut")),
)


def _match_checks(facts: Facts, spec: Sequence[str]) -> List[Dict[str, Any]]:
    out = []
    for cid, check in sorted((facts.verify or {}).get("checks", {}).items()):
        if any(cid.startswith(item) if item.endswith(".") else cid == item for item in spec):
            out.append(check)
    return out


def fix_verdicts(facts: Facts) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    verify = facts.verify
    for fix_id, title, check_spec, presence_spec in FIXES:
        checks = _match_checks(facts, check_spec)
        presence = checks if presence_spec is None else _match_checks(facts, presence_spec)
        entry: Dict[str, Any] = {
            "id": fix_id,
            "title": title,
            "checks": {
                "total": len(checks),
                "pass": sum(1 for c in checks if c.get("state") == "pass"),
                "fail": sum(1 for c in checks if c.get("state") == "fail"),
                "skip": sum(1 for c in checks if c.get("state") == "skip"),
                "unknown": sum(1 for c in checks if c.get("state") == "unknown"),
            },
            "installed": None,
            "evidence": [],
            "notes": [],
            "checks_present": [c["id"] for c in checks if c.get("state") == "pass"],
            "checks_missing": [c["id"] for c in checks if c.get("state") == "skip"],
        }
        categories = Counter(_skip_category(c) for c in checks if c.get("state") == "skip")
        categories.pop("", None)
        if categories:
            entry["notes"].append(
                "skips: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(categories.items()))
            )
        installed = any(c.get("state") == "pass" for c in presence)
        entry["installed"] = installed
        if not checks:
            entry["state"] = "unknown"
            entry["why"] = (
                "no verifier rows for this component in the bundle (a state-dir input or an "
                "old harness)"
                if verify is not None
                else "no verify/verify.json in this bundle"
            )
        elif entry["checks"]["fail"]:
            entry["state"] = "broken"
            entry["why"] = "installed but the probe contradicts it"
            entry["evidence"] = [
                facts.ev_check(c["id"]) for c in checks if c.get("state") == "fail"
            ][:3]
        elif installed:
            if categories.get("not-installed") or entry["checks"]["unknown"]:
                entry["state"] = "partial"
                entry["why"] = "installed, but part of the set is not (or cannot be asserted)"
            else:
                entry["state"] = "ok"
                entry["why"] = "present and proven by the bundle"
        elif entry["checks"]["pass"]:
            entry["state"] = "not-installed" if categories.get("not-installed") else "partial"
            entry["why"] = (
                "only read-only capability rows pass (%s) - the fix itself is not installed"
                % ", ".join(c["id"] for c in checks if c.get("state") == "pass")
            )
        elif categories.get("not-installed"):
            entry["state"] = "not-installed"
            entry["why"] = "not installed (SKIP is the documented state, not a failure)"
        elif categories.get("opt-in"):
            entry["state"] = "not-installed"
            entry["why"] = "opt-in component, not enabled"
        else:
            entry["state"] = "unknown"
            entry["why"] = "readable but not assertable (%s)" % (
                ", ".join(sorted(categories)) or "no reason in the row"
            )
        if not entry["evidence"]:
            for cid in ("vol.unit", "hid.rule", "sleep.units", "bt.files", "aud.eq",
                        "gfx.xmx.driver", "pwr.zones", "decky.dir", "sched.state",
                        "life.fd", "rgbd.unit", "imu.selftest"):
                check = facts.check(cid)
                if check is not None and any(
                    cid.startswith(item) if item.endswith(".") else cid == item
                    for item in check_spec
                ):
                    ev = facts.ev_check(cid)
                    if ev:
                        entry["evidence"].append(ev)
                    break
        entry["evidence"] = [e if isinstance(e, dict) else e.as_dict()
                             for e in entry["evidence"] if e]
        out.append(entry)
    return out


def build_notes(facts: Facts, findings: Sequence[Dict[str, Any]]) -> List[str]:
    notes: List[str] = []
    if facts.bundle.kind == "state-dir":
        notes.append(
            "input is a docs/state snapshot (verify/status files only), so the file-level "
            "rules cannot fire; their results say `unknown` by design."
        )
    triggered = [f["rule"] for f in findings]
    if not findings:
        notes.append(
            "no rule fired: every rule whose evidence was present evaluated to clear."
        )
    if "power.power-key-poweroff" in triggered:
        if facts.session_power is not None:
            pba = powerdevil_effective(facts.session_power, "PowerButtonAction") or {}
            lid = powerdevil_effective(facts.session_power, "LidAction") or {}
            notes.append(
                "session/power.txt was read: PowerDevil PowerButtonAction=%s, LidAction=%s "
                "(the finding cites those lines)."
                % (pba.get("value") or "(unset)", lid.get("value") or "(unset)")
            )
        else:
            notes.append(
                "session/power.txt is not in the bundle, so the effective KDE "
                "power-key/lid action could not be read; the finding is based on the "
                "inhibitor + logind policy only. Re-collect with "
                "tools/oxp3-firstrun.sh --collect to include it."
            )
    if "polkit.no-session-agent" in triggered:
        notes.append(
            "no session polkit authentication agent was found; in a Gamescope/Gamemode "
            "session that is expected, and the shipped polkit rule is the only path that "
            "does not need an interactive prompt."
        )
    if facts.verify is not None and not facts.verify.get("checks"):
        notes.append("verify/verify.json is present but carries no checks (unparsable?).")
    if facts.env.get("uptime_s") is None:
        notes.append("uptime unknown, so the 'mtime vs boot' comparisons are unavailable.")
    return notes


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_text(report: Dict[str, Any], top: int = 0) -> str:
    env = report["environment"]
    bundle = report["bundle"]
    input_info = report["input"]
    lines: List[str] = []
    add = lines.append

    add("oxp3 triage - %s" % input_info["path"])
    add("=" * 72)
    add("input     : %s (%s), %d file(s)" % (input_info["kind"], input_info["root"],
                                             input_info["files"]))
    if bundle.get("tool"):
        add("bundle    : %s %s, collected %s (mode=%s, root=%s)" % (
            bundle.get("tool"), bundle.get("version") or "?",
            bundle.get("generated_at") or "?", bundle.get("mode") or "?",
            bundle.get("root"),
        ))
    else:
        add("bundle    : no MANIFEST.json (a docs/state snapshot, not a firstrun bundle)")
    add("kernel    : %s" % (env.get("kernel") or "unknown"))
    add("os        : %s" % (env.get("os") or "unknown"))
    session = env.get("session") or {}
    add("session   : %s / %s, gamescope: %s (%s)" % (
        session.get("type"), session.get("desktop") or "unknown",
        session.get("gamescope"), session.get("confidence"),
    ))
    add("gpu       : %s" % (env.get("gpu") or "unknown"))
    add("mesa      : %s" % (env.get("mesa") or "unknown"))
    add("xmx driver: %s" % (env.get("xmx_driver") or "unknown"))
    counts = bundle.get("counts") or {}
    add("evidence  : %s ok / %s missing / %s denied / %s truncated; journal %s" % (
        counts.get("ok", "?"), counts.get("missing", 0), counts.get("denied", 0),
        counts.get("truncated", 0),
        "available" if (bundle.get("journal") or {}).get("available") else "not available",
    ))
    if bundle.get("verify"):
        v = bundle["verify"]
        add("verifier  : %s checks: %s pass, %s fail, %s skip, %s unknown (exit %s)" % (
            v.get("counts", {}).get("total", "?"),
            v.get("counts", {}).get("pass", "?"), v.get("counts", {}).get("fail", "?"),
            v.get("counts", {}).get("skip", "?"), v.get("counts", {}).get("unknown", "?"),
            v.get("exit_code"),
        ))
    add("")

    add("FIXES (derived from this bundle, nothing re-run)")
    add("-" * 72)
    for entry in report["fixes"]:
        c = entry["checks"]
        add("  %-10s %-13s %d checks: %d pass, %d fail, %d skip, %d unknown" % (
            entry["id"], entry["state"], c["total"], c["pass"], c["fail"], c["skip"],
            c["unknown"],
        ))
        if entry.get("why") and entry["state"] != "ok":
            add("  %-10s %s" % ("", entry["why"]))
        for note in entry.get("notes") or []:
            add("  %-10s %s" % ("", note))
    add("")

    findings = report["findings"]
    add("FINDINGS (%d, ranked)" % len(findings))
    add("-" * 72)
    if not findings:
        add("  none - no rule fired. See the rule summary and evidence gaps below.")
    shown = findings if not top else findings[:top]
    for finding in shown:
        add("[%s] %s  %s" % (finding["id"], finding["severity"].upper(), finding["title"]))
        add("     rule: %s  (confidence: %s)" % (finding["rule"], finding["confidence"]))
        for chunk in wrap(finding["conclusion"], 70):
            add("     " + chunk)
        for evidence in finding["evidence"]:
            where = evidence["file"] + (":%d" % evidence["line"] if evidence["line"] else "")
            add("     evidence: %s  %s" % (where, trim(evidence["excerpt"], 100)))
        for action in finding["actions"]:
            add("     do: %s%s" % (action["command"],
                                   "" if action["privileged"] else "   (no root)"))
    if top and len(findings) > top:
        add("  ... %d more finding(s); run without --top to see them." % (len(findings) - top))
    add("")

    add("ACTIONS (ranked)")
    add("-" * 72)
    if not report["actions"]:
        add("  nothing to do from this bundle.")
    for action in report["actions"]:
        tag = "user-run" if action["privileged"] else "no root"
        add(" %2d. %s" % (action["rank"], action["command"]))
        add("     why: %s  [%s; %s]" % (trim(action["why"], 100), action["severity"], tag))
    add("")

    rules = report["rules"]
    states = Counter(item["state"] for item in rules)
    add("RULES: %d evaluated - %d triggered, %d clear, %d unknown, %d not-applicable" % (
        len(rules), states.get("triggered", 0), states.get("clear", 0),
        states.get("unknown", 0), states.get("not-applicable", 0),
    ))
    unknown = [item for item in rules if item["state"] == "unknown"]
    for item in unknown[:6]:
        add("  unknown: %-26s %s" % (item["id"], trim(item["conclusion"], 90)))
    if len(unknown) > 6:
        add("  unknown: ... and %d more (see --json)" % (len(unknown) - 6))
    add("")

    gaps = report["gaps"]
    add("EVIDENCE GAPS (%d)" % len(gaps))
    add("-" * 72)
    if not gaps:
        add("  none.")
    for gap in gaps[:12]:
        add("  %-28s %s" % (gap.get("artifact") or "?", trim(gap.get("detail") or "", 90)))
    if len(gaps) > 12:
        add("  ... and %d more" % (len(gaps) - 12))
    add("")
    for note in report.get("notes") or []:
        add("note: %s" % note)
    if report.get("notes"):
        add("")
    add("json: rerun with --json (or --out-dir DIR for triage.json + triage.txt)")
    add("rules: %d registered; docs/TRIAGE.md has the catalogue" % len(RULES))
    return "\n".join(lines) + "\n"


def wrap(text: str, width: int) -> List[str]:
    words = str(text).split()
    out: List[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            out.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        out.append(current)
    return out or [""]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
FIXTURES = REPO / "tests" / "fixtures" / "firstrun"

# (fixture, required finding rules, required fix states)
EXPECTATIONS: Tuple[Tuple[str, Tuple[str, ...], Dict[str, str]], ...] = (
    (
        "healthy",
        (),
        {fix: "ok" for fix in ("volume", "leds", "rgb", "daemons", "sleep", "power",
                               "battery", "audio", "display", "graphics", "decky", "imu",
                               "bluetooth")},
    ),
    (
        "preinstall",
        ("bt.initramfs-stale", "power.power-key-poweroff", "sleep.trigger-uninstalled",
         "bt.dracut-missing", "sleep.no-attempt", "sleep.stats-inconsistent",
         "xmx.flatpak-helper", "xmx.trace-absent", "verify.not-installed",
         "decky.not-installed", "power.profile-unsaved", "volume.not-installed",
         "polkit.no-session-agent"),
        {"volume": "not-installed", "leds": "not-installed", "rgb": "not-installed",
         "daemons": "not-installed", "sleep": "not-installed", "power": "partial",
         "battery": "ok", "audio": "ok", "display": "ok", "graphics": "ok",
         "sched": "not-installed", "decky": "not-installed", "imu": "ok",
         "bluetooth": "broken"},
    ),
    (
        "bt-not-loaded",
        ("bt.initramfs-stale",),
        {"bluetooth": "broken", "volume": "ok", "leds": "ok", "sleep": "ok",
         "audio": "ok", "graphics": "ok"},
    ),
    (
        "xmx-no-trace",
        ("xmx.ocloc-missing", "xmx.driver-mismatch", "xmx.helper-no-nodgrf",
         "xmx.flatpak-helper", "xmx.trace-absent"),
        {"graphics": "broken", "volume": "ok", "audio": "ok"},
    ),
    (
        "audio-wedged",
        ("audio.wedged", "audio.eq-inactive"),
        {"audio": "broken", "bluetooth": "ok", "sleep": "ok"},
    ),
    (
        "sleep-never-triggered",
        ("sleep.logind-not-restarted", "power.power-key-poweroff", "sleep.no-attempt",
         "sleep.stats-inconsistent"),
        {"sleep": "partial", "bluetooth": "ok"},
    ),
    (
        "sleep-hw-counter",
        ("bt.initramfs-stale", "power.power-key-poweroff", "sleep.trigger-uninstalled",
         "bt.dracut-missing", "sleep.no-attempt", "sleep.stats-inconsistent",
         "xmx.flatpak-helper", "xmx.trace-absent", "verify.not-installed",
         "decky.not-installed", "power.profile-unsaved", "volume.not-installed",
         "polkit.no-session-agent"),
        {"volume": "not-installed", "leds": "not-installed", "rgb": "not-installed",
         "daemons": "not-installed", "sleep": "not-installed", "power": "partial",
         "battery": "ok", "audio": "ok", "display": "ok", "graphics": "ok",
         "sched": "not-installed", "decky": "not-installed", "imu": "ok",
         "bluetooth": "broken"},
    ),
    (
        "volume-degraded",
        ("volume.degraded-busy",),
        {"volume": "broken", "leds": "ok", "sleep": "ok"},
    ),
    (
        "missing-files",
        ("bundle.gaps",),
        {fix: "unknown" for fix in ("volume", "leds", "sleep", "audio", "graphics",
                                     "bluetooth", "decky", "power")},
    ),
)


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def _selftest_ok(condition: bool, label: str, checks: List[Tuple[bool, str]]) -> None:
    checks.append((bool(condition), label))


def selftest() -> int:
    checks: List[Tuple[bool, str]] = []
    if not FIXTURES.is_dir():
        print("selftest: fixtures missing: %s" % FIXTURES, file=sys.stderr)
        return 1
    names = [name for name, _rules, _fixes in EXPECTATIONS]
    missing = sorted(name for name in names if not fixture_path(name).is_dir())
    _selftest_ok(not missing, "all %d fixtures exist (%s)" % (len(names), ", ".join(names)),
                 checks)
    if missing:
        for ok, label in checks:
            if not ok:
                print("  FAIL %s" % label)
        print("selftest: 0 checks passed, %d failed (missing fixtures: %s)"
              % (len(checks), ", ".join(missing)))
        return 1

    reports: Dict[str, Dict[str, Any]] = {}
    for name, expected_rules, expected_fixes in EXPECTATIONS:
        bundle = resolve_input(str(fixture_path(name)))
        try:
            report = analyse(bundle)
        finally:
            bundle.close()
        reports[name] = report

        fired = [finding["rule"] for finding in report["findings"]]
        _selftest_ok(
            sorted(fired) == sorted(expected_rules),
            "%s: findings are exactly %s (got %s)"
            % (name, ", ".join(sorted(expected_rules)) or "none",
               ", ".join(sorted(fired)) or "none"),
            checks,
        )
        for fix_id, want in expected_fixes.items():
            got = next((f["state"] for f in report["fixes"] if f["id"] == fix_id), "?")
            _selftest_ok(got == want, "%s: fix %s is %s (got %s)" % (name, fix_id, want, got),
                         checks)
        # Every finding must cite a file that exists in the bundle, with a valid line.
        for finding in report["findings"]:
            _selftest_ok(bool(finding["evidence"]),
                         "%s: %s cites evidence" % (name, finding["rule"]), checks)
            for ev in finding["evidence"]:
                path = fixture_path(name) / (ev["file"] or "")
                _selftest_ok(path.is_file(),
                             "%s: %s evidence file exists (%s)" % (name, finding["rule"],
                                                                   ev["file"]), checks)
                if ev["line"] and path.is_file():
                    line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
                    _selftest_ok(1 <= ev["line"] <= line_count,
                                 "%s: %s line %s within %d" % (name, finding["rule"],
                                                                ev["line"], line_count), checks)
        # Priority order must be monotonic and actions ranked by priority.
        priorities = [finding["priority"] for finding in report["findings"]]
        _selftest_ok(priorities == sorted(priorities, reverse=True),
                     "%s: findings ranked by priority" % name, checks)
        acts = [action["priority"] for action in report["actions"]]
        _selftest_ok(acts == sorted(acts, reverse=True), "%s: actions ranked by priority" % name,
                     checks)
        _selftest_ok(all(action["rank"] == index for index, action in
                         enumerate(report["actions"], 1)),
                     "%s: action ranks are contiguous" % name, checks)
        _selftest_ok(all(f["id"] == "F%d" % index for index, f in
                         enumerate(report["findings"], 1)),
                     "%s: finding ids are contiguous" % name, checks)

    # The pre-install state must read as "not installed yet", never as invented failures.
    pre = reports["preinstall"]
    text = render_text(pre)
    _selftest_ok("not-installed" in text, "preinstall: text says not-installed", checks)
    critical = {finding["rule"] for finding in pre["findings"]
                if finding["severity"] == "critical"}
    _selftest_ok(
        critical <= {"bt.initramfs-stale", "power.power-key-poweroff"},
        "preinstall: criticals are only the Bluetooth initramfs and the power-key policy "
        "(got %s)" % (", ".join(sorted(critical)) or "none"),
        checks,
    )
    _selftest_ok(
        not any(rule in {f["rule"] for f in pre["findings"]} for rule in
                ("verify.failures", "units.failed", "env.mesa-mismatch", "audio.wedged")),
        "preinstall: no failure is invented by a rule that should be clear",
        checks,
    )
    # Healthy must be silent, and missing evidence must never read as a pass.
    _selftest_ok(not reports["healthy"]["findings"], "healthy: no findings", checks)
    rules_missing = reports["missing-files"]["rules"]
    clears_without_evidence = [
        item["id"] for item in rules_missing
        if item["state"] == "clear" and not any(item.get("reads_present", {}).values())
    ]
    _selftest_ok(
        not clears_without_evidence,
        "missing-files: no rule reports clear without a file it reads (got %s)"
        % (", ".join(clears_without_evidence) or "none"),
        checks,
    )
    _selftest_ok(
        all(item["state"] in ("unknown", "not-applicable")
            for item in rules_missing
            if not any(item.get("reads_present", {}).values())),
        "missing-files: every rule whose evidence is absent is unknown/not-applicable",
        checks,
    )
    _selftest_ok(
        [f["rule"] for f in reports["missing-files"]["findings"]] == ["bundle.gaps"],
        "missing-files: only the evidence-gap finding is emitted",
        checks,
    )
    _selftest_ok(
        any(gap["detail"].startswith("missing") for gap in reports["missing-files"]["gaps"]),
        "missing-files: gaps list the absent artifacts",
        checks,
    )

    # Tarball input must give the same verdicts as the directory.
    temp = Path(tempfile.mkdtemp(prefix="oxp3-triage-selftest-"))
    try:
        archive = temp / "preinstall.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(str(fixture_path("preinstall")), arcname="preinstall")
        bundle = resolve_input(str(archive))
        try:
            packed = analyse(bundle)
        finally:
            bundle.close()
        _selftest_ok(
            [f["rule"] for f in packed["findings"]] == [f["rule"] for f in pre["findings"]],
            "tarball: same findings as the directory",
            checks,
        )
        _selftest_ok(packed["input"]["kind"] == "bundle", "tarball: detected as a bundle", checks)
    finally:
        shutil.rmtree(str(temp), ignore_errors=True)

    # A real docs/state snapshot must be readable and must not invent conclusions.
    state = REPO / "docs" / "state"
    if state.is_dir() and any(state.glob("verify-*.json")):
        bundle = resolve_input(str(state))
        try:
            report = analyse(bundle)
        finally:
            bundle.close()
        _selftest_ok(report["input"]["kind"] == "state-dir",
                     "state-dir: detected as a docs/state snapshot", checks)
        verdicts = {entry["id"]: entry["state"] for entry in report["fixes"]}
        _selftest_ok(verdicts.get("bluetooth") == "broken",
                     "state-dir: bluetooth is broken (three real failures)", checks)
        for fix_id in ("volume", "leds", "sleep", "decky"):
            _selftest_ok(verdicts.get(fix_id) in ("not-installed", "partial", "unknown"),
                         "state-dir: %s is not invented as ok/broken (got %s)"
                         % (fix_id, verdicts.get(fix_id)), checks)
        for finding in report["findings"]:
            _selftest_ok(bool(finding["evidence"]),
                         "state-dir: %s cites evidence" % finding["rule"], checks)
    else:
        _selftest_ok(False, "state-dir: docs/state has a verify-*.json snapshot", checks)

    failed = [label for ok, label in checks if not ok]
    for ok, label in checks:
        if not ok:
            print("  FAIL %s" % label)
    print("selftest: %d checks passed, %d failed" % (len(checks) - len(failed), len(failed)))
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oxp3-triage.py",
        description="Analyse an oxp3-firstrun evidence bundle and print a ranked diagnosis.",
        epilog="Exit codes: 0 report, 1 critical finding, 2 usage, 3 unreadable input. "
               "Docs: docs/TRIAGE.md",
    )
    parser.add_argument("bundle", nargs="?", help="bundle directory or tarball")
    parser.add_argument("--json", action="store_true", help="print the JSON report instead of text")
    parser.add_argument("--out-dir", metavar="DIR",
                        help="write DIR/triage.json and DIR/triage.txt")
    parser.add_argument("--json-out", metavar="PATH", help="write the JSON report to PATH")
    parser.add_argument("--text-out", metavar="PATH", help="write the text report to PATH")
    parser.add_argument("--top", type=int, default=0, metavar="N",
                        help="print at most N findings (default: all)")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 on a high finding too, not only critical")
    parser.add_argument("--list-rules", action="store_true", help="print the rule catalogue and exit")
    parser.add_argument("--selftest", action="store_true", help="run the offline self-test")
    parser.add_argument("--version", action="version", version="oxp3-triage %s" % VERSION)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return selftest()
    if args.list_rules:
        print("%-26s %-9s %-10s %s" % ("id", "severity", "category", "reads / title"))
        for item in RULES:
            print("%-26s %-9s %-10s %s" % (item.id, item.severity, item.category,
                                           item.title))
            print("%-26s %-9s %-10s reads: %s" % ("", "", "", ", ".join(item.reads)))
        return 0
    if not args.bundle:
        print("oxp3-triage: no bundle given (try --help)", file=sys.stderr)
        return 2
    try:
        bundle = resolve_input(args.bundle)
    except BundleError as exc:
        print("oxp3-triage: %s" % exc, file=sys.stderr)
        return 3
    except UsageError as exc:
        print("oxp3-triage: %s" % exc, file=sys.stderr)
        return 2
    try:
        report = analyse(bundle)
    finally:
        bundle.close()

    text = render_text(report, top=args.top)
    payload = json.dumps(report, indent=2, sort_keys=False) + "\n"

    if args.out_dir:
        out = Path(args.out_dir).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "triage.json").write_text(payload, encoding="utf-8")
            (out / "triage.txt").write_text(text, encoding="utf-8")
        except OSError as exc:
            print("oxp3-triage: cannot write %s: %s" % (out, exc), file=sys.stderr)
            return 2
    try:
        if args.json_out:
            Path(args.json_out).expanduser().write_text(payload, encoding="utf-8")
        if args.text_out:
            Path(args.text_out).expanduser().write_text(text, encoding="utf-8")
    except OSError as exc:
        print("oxp3-triage: cannot write the report: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(payload)
    else:
        sys.stdout.write(text)
        if args.out_dir:
            print("written: %s and %s" % (Path(args.out_dir) / "triage.json",
                                          Path(args.out_dir) / "triage.txt"))

    severities = {finding["severity"] for finding in report["findings"]}
    if "critical" in severities:
        return 1
    if args.strict and "high" in severities:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
