#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3-firstrun - one-shot first-run evidence collector for the OneXPlayer 3 fixes.

The owner reboots once after installing the hardware fixes; this tool is the single
command that afterward (a) prints the install checklist with the live state of every
step and (b) with ``--collect`` gathers a redacted evidence bundle that can be handed
back for analysis without any further round trips.

Safety model:

  * strictly read-only: it never writes outside its own bundle directory, never
    starts/stops/restarts a unit, never touches udev, sysfs, the EC or the initramfs;
  * never suspends, never asks for root itself — privileged probes degrade to an
    explicit ``denied``/``missing`` status instead of failing the run;
  * bounded: a total byte cap (default 5 MiB) and a per-file line cap, with every
    truncation recorded in ``MANIFEST.json``;
  * redacted: usernames, home paths, hostname, MAC addresses (only the vendor OUI is
    kept), serial numbers/UUIDs and token-like strings are replaced before the bytes
    are written, and only counts — never the replaced values — are recorded.

Entry points: ``tools/oxp3-firstrun.sh`` (thin wrapper), ``python3 tools/oxp3_firstrun.py``.
Exit codes: 0 = checked/collected, 1 = a real collection failure, 2 = usage error,
3 = the backend could not start. Missing optional data is recorded, never fatal.

Docs: docs/FIRSTRUN.md. Unit tests: tests/unit/test_firstrun.py.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys
import time
import tempfile
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
SCHEMA = 1

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_LINES = 2000
MIN_MAX_BYTES = 32 * 1024  # the index (README+manifest+checklist) needs room
# Reserved for MANIFEST.json + README.txt + checklist.txt + REDACTIONS.txt.
INDEX_RESERVE = 8192
MANIFEST_RESERVE = 16384
MIN_TRUNCATE = 512

# ---------------------------------------------------------------------------
# The repository's own inventory (docs/INSTALL-HW.md, docs/DAEMONS.md,
# docs/hw/sleep-power.md). Kept here so a missing unit is reported as a named
# row instead of silently disappearing from `systemctl show`.
# ---------------------------------------------------------------------------
SYSTEM_UNITS: Tuple[str, ...] = (
    "oxp3-voltap.service",
    "oxp3-sleep-wakeup.service",
    "oxp3-sleep-pre.service",
    "oxp3-sleep-log.service",
    "oxp3-power-profile.service",
    "oxp3-power-resume.service",
    "oxp3-fan-curve.service",
    "oxp3-adaptivetdp.service",
    "oxp3-tdp-sync.service",
    "oxp3-rapl-pp1.service",
    "oxp3-gaming-sched.service",
    "oxp3-xpad-sleep.service",
    "oxp3-audio-resume@.service",
    "rapl-power.service",
)
# `systemctl show` rejects an uninstantiated template and then stops processing the
# rest of its argv, so templates are inspected through list-unit-files instead.
UNIT_TEMPLATES: Tuple[str, ...] = ("oxp3-audio-resume@.service",)
SHOWABLE_SYSTEM_UNITS: Tuple[str, ...] = tuple(
    unit for unit in SYSTEM_UNITS if "@" not in unit
)

USER_UNITS: Tuple[str, ...] = (
    "oxp3-homebtn.service",
    "oxp3-rgbd.service",
    "oxp3-audio-route.service",
)

# Absolute files this repository installs. (path, human label)
SYSTEM_FILES: Tuple[Tuple[str, str], ...] = (
    ("/usr/local/libexec/oxp3-voltap", "volume rocker daemon"),
    ("/usr/local/libexec/oxp3-sleep-wakeup", "wake-source policy helper"),
    ("/usr/local/libexec/oxp3-sleep-log", "suspend audit helper"),
    ("/usr/local/libexec/oxp3-powerctl", "power/fan/battery control"),
    ("/usr/local/libexec/oxp3-adaptivetdp", "adaptive TDP daemon"),
    ("/etc/udev/rules.d/90-oxp3-hid.rules", "hidraw access rule"),
    ("/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules", "sleep wakeup rule"),
    ("/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb", "volume force-release hwdb"),
    ("/etc/polkit-1/rules.d/49-oxp3-suspend.rules", "suspend polkit rule"),
    ("/etc/systemd/logind.conf.d/60-oxp3-sleep.conf", "logind sleep policy"),
    ("/etc/modprobe.d/oxp3-bmi160.conf", "IMU blacklist"),
    ("/etc/dracut.conf.d/60-oxp3-bt-firmware.conf", "Bluetooth initramfs drop-in"),
    ("/etc/oxp3/power.json", "saved power profile"),
)
HWD_B_GLOB = "/etc/udev/hwdb.d/84-oxp3-volume-keys*.hwdb"

# Bluetooth firmware the standalone script installs (paths only, never the bytes).
BT_FIRMWARE: Tuple[str, ...] = (
    "/usr/lib/firmware/updates/intel/ibt-00a0-0291-iml.sfi",
    "/usr/lib/firmware/updates/intel/ibt-00a0-0291-pci.sfi",
)

HOME_FILES: Tuple[Tuple[str, str], ...] = (
    (".config/oxp3-rgb.toml", "RGB keeper config"),
    (".config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf", "speaker EQ drop-in"),
    (".config/systemd/user/oxp3-homebtn.service", "home-button user unit"),
    (".config/systemd/user/oxp3-rgbd.service", "RGB keeper user unit"),
    (".config/xess-xmx/icd-xmx.json", "XMX Vulkan ICD"),
    (".config/xess-xmx/oxp3-env.sh", "XMX environment"),
    (".config/xess-xmx/flatpak-env.sh", "XMX Flatpak environment"),
    (".cache/oxp3-fixes/xmx-driver/libvulkan_intel.so", "XMX Mesa driver"),
    (".local/share/flatpak/overrides/org.anatase.Steam", "Steam Flatpak overrides"),
)

DRIVER_CANDIDATES: Tuple[str, ...] = (
    "/usr/lib64/libvulkan_intel.so",
    "/usr/lib64/dri/intel_dri.so",
)

# Environment variables that describe the *current session's* graphics stack. Values are
# paths (redacted like everything else) and feature switches; no secrets belong here.
#: set by --quiet; suppresses the [collect] progress lines on stderr
QUIET = False

ENV_PREFIXES = (
    "VK_", "INTC_", "XESS", "IGC_", "MESA_", "OCLOC", "LIBVA_", "DRI_",
    "__GL", "PROTON_", "OXP3_", "LD_LIBRARY_PATH", "VKD3D_", "DXVK_",
)

KERNEL_JOURNAL_GREP = (
    "bluetooth|hci0|btintel|btusb|suspend|resume|PM:|s2idle|snd_|sof-|"
    "pipewire|xe |i915|drm|amdgpu|platform_profile|thermal|thunderbolt|typec|"
    "ucsi|i2c_hid|hid-generic"
)

# The KDE power keys this collector reads. PowerDevil 6 keeps them in the nested
# KConfig group [AC][SuspendAndShutdown] / [Battery][SuspendAndShutdown], which a
# flat --group AC read does not see (docs/hw/sleep-power.md 2.5 / R1). Both forms
# are probed so the artifact is explicit about what KConfig returns.
POWERDEVIL_GROUPS: Tuple[str, ...] = ("AC", "Battery")
POWERDEVIL_SUBGROUP = "SuspendAndShutdown"
POWERDEVIL_KEYS: Tuple[str, ...] = (
    "PowerButtonAction", "LidAction", "AutoSuspend", "AutoSuspendAction",
)
# pgrep -a matches the (truncated) process name only; -f makes the full argv
# searchable, which these long names need or the check is a silent false negative.
POLKIT_AGENTS: Tuple[str, ...] = (
    "polkit-kde-authentication-agent-1",
    "polkit-gnome-authentication-agent-1",
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def _replace_mac(match: re.Match) -> str:
    """Keep only the vendor OUI (the first three octets): useful for a hardware
    report, not identifying for a single unit."""
    raw = match.group(0)
    sep = ":" if ":" in raw else "-"
    parts = raw.split(sep)
    return "<mac-oui:%s>" % ":".join(parts[:3]).lower()


_TOKEN_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b")),
)
_MAC_RE = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b|\b(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_SERIAL_RE = re.compile(
    r"(?i)\b(serial(?:\s*(?:number|no\.?))?|s/n)\s*[:=]\s*(?!<redacted:)([^\s,;]+)"
)
_UUID_RE = re.compile(
    r"(?i)\b([a-z0-9_.\-]*(?:uuid|partuuid|wwn|serial)=)([0-9A-Fa-f][0-9A-Fa-f\-]{7,})"
)
_BARE_UUID_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.\-]*(?:token|secret|password|passwd|passphrase|"
    r"api[_-]?key|auth[_-]?key|credential)[A-Za-z0-9_.\-]*)\s*[:=]\s*(?!<redacted:)(\S+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+(?!<redacted:)([A-Za-z0-9._~+/=\-]{8,})")
_COOKIE_RE = re.compile(r"(?im)^(\s*Cookie\s*:)\s*(?!<redacted:)\S+")


class Redactor:
    """Deterministic, idempotent text redaction.

    Applied to every byte that leaves the process, including the manifest, so a
    collector that accidentally prints a path cannot leak it. Each rule only records
    a *count*; the replaced value is never retained.
    """

    def __init__(self, literals: Sequence[Tuple[str, str]] = ()) -> None:
        # Longest literal first so a home path is replaced before the bare username.
        ordered = sorted(
            ((value, repl) for value, repl in literals if value),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._literals = ordered
        self.counts: Counter = Counter()
        self.files: Dict[str, Counter] = {}

    # -- one text -----------------------------------------------------------
    def redact(self, text: str) -> Tuple[str, Counter]:
        """Replace secrets/PII, then identity literals (home, user, host).

        Patterns run first so an address is redacted as a whole (``<email>``)
        instead of being half-replaced by the username rule. Every rule is a
        no-op on its own output, so applying ``redact`` twice is stable.
        """
        counts: Counter = Counter()
        if not text:
            return text, counts
        for name, pattern in _TOKEN_RULES:
            text, n = pattern.subn("<redacted:%s>" % name, text)
            if n:
                counts["token:" + name] += n
        text, n = _BEARER_RE.subn(r"\1 <redacted:bearer>", text)
        if n:
            counts["token:bearer"] += n
        text, n = _SECRET_ASSIGN_RE.subn(
            lambda m: "%s=<redacted:secret>" % m.group(1), text
        )
        if n:
            counts["secret-assignment"] += n
        text, n = _SERIAL_RE.subn(r"\1=<redacted:serial>", text)
        if n:
            counts["serial-number"] += n
        text, n = _UUID_RE.subn(r"\1<redacted:uuid>", text)
        if n:
            counts["uuid"] += n
        text, n = _BARE_UUID_RE.subn("<redacted:uuid>", text)
        if n:
            counts["uuid"] += n
        text, n = _EMAIL_RE.subn("<email>", text)
        if n:
            counts["email"] += n
        text, n = _COOKIE_RE.subn(r"\1 <redacted:cookie>", text)
        if n:
            counts["cookie"] += n
        text, n = _MAC_RE.subn(_replace_mac, text)
        if n:
            counts["mac"] += n
        for value, replacement in self._literals:
            if value in text:
                n = text.count(value)
                text = text.replace(value, replacement)
                counts["identity"] += n
        return text, counts

    # -- accounting ---------------------------------------------------------
    def note_file(self, relpath: str, counts: Counter) -> None:
        if counts:
            self.counts.update(counts)
            self.files[relpath] = Counter(counts)

    def report(self, max_files: int = 200) -> Dict[str, Any]:
        files: "OrderedDict[str, Dict[str, int]]" = OrderedDict()
        for relpath in sorted(self.files)[:max_files]:
            files[relpath] = dict(sorted(self.files[relpath].items()))
        return {
            "rules": dict(sorted(self.counts.items())),
            "total": int(sum(self.counts.values())),
            "files": files,
            "files_with_matches": len(self.files),
            "files_omitted": max(0, len(self.files) - max_files),
        }


def build_literals(home: Path, user: str, host: str, extra: Iterable[str] = ()) -> List[Tuple[str, str]]:
    """Identity literals for :class:`Redactor` (home paths first, then names)."""
    literals: List[Tuple[str, str]] = []
    home_s = str(home).rstrip("/")
    if home_s and home_s != "/":
        literals.append((home_s, "~"))
        # The same user may have a /home alias (or the reverse) on another image.
        if home_s.startswith("/var/home/"):
            literals.append(("/home/" + home_s[len("/var/home/"):], "~"))
        elif home_s.startswith("/home/"):
            literals.append(("/var/home/" + home_s[len("/home/"):], "~"))
    if user and len(user) >= 3 and user != "root":
        literals.append((user, "<user>"))
    if host and len(host) >= 3:
        literals.append((host, "<host>"))
        short = host.split(".")[0]
        if short and short != host and len(short) >= 3:
            literals.append((short, "<host>"))
    for value in extra:
        if value:
            literals.append((value, "<redacted>"))
    return literals


# ---------------------------------------------------------------------------
# Small data types
# ---------------------------------------------------------------------------
@dataclass
class CmdResult:
    argv: Sequence[str]
    rc: int
    out: str = ""
    err: str = ""
    timed_out: bool = False
    truncated: bool = False
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    @property
    def text(self) -> str:
        if self.err.strip():
            return (self.out.rstrip() + "\n[stderr]\n" + self.err.rstrip()).strip()
        return self.out.rstrip()

    def reason(self) -> str:
        if self.timed_out:
            return "timed out"
        if self.rc != 0:
            return "exit %d" % self.rc
        return ""


class Runner:
    """Runs read-only probe commands with a timeout and an output bound."""

    def __init__(self, max_output_bytes: int = 2 * 1024 * 1024, env: Optional[Dict[str, str]] = None):
        self.max_output_bytes = max_output_bytes
        self.env = env

    def run(self, argv: Sequence[str], timeout_s: int = 120, cwd: Optional[Path] = None) -> CmdResult:
        started = _dt.datetime.now()
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                timeout=timeout_s,
                check=False,
                cwd=str(cwd) if cwd else None,
                env=self.env,
            )
            out = proc.stdout.decode("utf-8", "replace")
            err = proc.stderr.decode("utf-8", "replace")
            rc = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else ""
            err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else ""
            rc = 124
            timed_out = True
        except FileNotFoundError:
            return CmdResult(argv, 127, "", "command not found", False, False, 0.0)
        except OSError as exc:
            return CmdResult(argv, 126, "", str(exc), False, False, 0.0)
        duration = (_dt.datetime.now() - started).total_seconds()
        truncated = False
        if len(out) > self.max_output_bytes:
            out = out[: self.max_output_bytes] + "\n... [output truncated by collector]\n"
            truncated = True
        return CmdResult(argv, rc, out, err, timed_out, truncated, duration)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def sha256_file(path: Path, max_bytes: int = 64 * 1024 * 1024) -> Optional[str]:
    try:
        st = path.stat()
        if not path.is_file() or st.st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def iso_mtime(path: Path) -> Optional[str]:
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except OSError:
        return None


@dataclass
class FileInfo:
    path: str
    exists: bool
    size: Optional[int] = None
    sha256: Optional[str] = None
    mtime: Optional[str] = None
    readable: bool = False

    def line(self) -> str:
        if not self.exists:
            return "MISSING  %s" % self.path
        return "%s  %10s B  %s  %s" % (
            "present",
            self.size if self.size is not None else "?",
            self.sha256 or "<no-hash>",
            self.path,
        )


def file_info(path: Path, with_hash: bool = True) -> FileInfo:
    try:
        st = path.stat()
    except OSError:
        return FileInfo(path=str(path), exists=False)
    info = FileInfo(str(path), True, st.st_size, None, iso_mtime(path))
    info.readable = os.access(path, os.R_OK)
    if with_hash and path.is_file():
        info.sha256 = sha256_file(path)
    return info


def read_text(path: Path, limit: int = 1 << 16) -> Optional[str]:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def first_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


@dataclass
class Limits:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_lines: int = DEFAULT_MAX_LINES


@dataclass
class Artifact:
    relpath: str
    title: str
    text: str = ""
    kind: str = "text"  # "text" | "json"
    status: str = "ok"
    command: str = ""
    detail: str = ""
    priority: int = 50
    max_lines: Optional[int] = None    # per-file override, min() with Limits.max_lines
    optional: bool = True
    # filled in by the bundler
    bytes: int = 0
    lines: int = 0
    truncated: bool = False
    json_valid: Optional[bool] = None
    exit_code: Optional[int] = None
    reason: str = ""

    def header(self) -> str:
        cmd = ("\n# command: " + self.command) if self.command else ""
        return "# %s\n# status:  %s%s\n\n" % (self.title, self.status, cmd)


# ---------------------------------------------------------------------------
# Context: everything a probe may touch, injectable for tests and --selftest
# ---------------------------------------------------------------------------
@dataclass
class Context:
    prefix: str = ""                      # system path prefix for synthetic trees
    home: Path = Path("/root")
    repo: Path = REPO
    user: str = "user"
    host: str = "host"
    euid: int = 0
    runner: Runner = field(default_factory=Runner)
    env: Dict[str, str] = field(default_factory=dict)
    # Tests/--selftest pin the tool inventory; a real run uses PATH.
    available_tools: Optional[set] = None
    now: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc))

    # -- paths --------------------------------------------------------------
    def sp(self, path: str) -> Path:
        """System path, prefixed in fixture mode (``--selftest``/unit tests)."""
        return Path(self.prefix + path) if self.prefix else Path(path)

    def hp(self, rel: str) -> Path:
        return Path(rel) if os.path.isabs(rel) else self.home / rel

    def read(self, path: str, limit: int = 1 << 16) -> Optional[str]:
        return read_text(self.sp(path), limit)

    def read_home(self, rel: str, limit: int = 1 << 16) -> Optional[str]:
        return read_text(self.hp(rel), limit)

    def which(self, name: str) -> Optional[str]:
        if self.available_tools is not None:
            return name if os.path.basename(name) in self.available_tools else None
        return shutil.which(name, path=self.env.get("PATH") if self.env else None)

    def run(self, argv: Sequence[str], timeout_s: int = 120, cwd: Optional[Path] = None) -> CmdResult:
        return self.runner.run(argv, timeout_s, cwd)

    def glob(self, pattern: str) -> List[Path]:
        base = self.sp(os.path.dirname(pattern))
        try:
            return sorted(base.glob(os.path.basename(pattern)))
        except OSError:
            return []


def build_context(prefix: str = "") -> Context:
    env = dict(os.environ)
    euid = os.geteuid() if hasattr(os, "geteuid") else 0
    sudo_user = env.get("SUDO_USER", "").strip()
    try:
        user = getpass.getuser()
    except Exception:
        user = pwd.getpwuid(euid).pw_name if hasattr(os, "geteuid") else "user"
    # When run through sudo, the interesting files belong to the invoking user.
    if euid == 0 and sudo_user:
        user = sudo_user
    home = Path(env.get("OXP3_FIRSTRUN_HOME") or "").expanduser()
    if not str(home) or str(home) == ".":
        try:
            home = Path(pwd.getpwnam(user).pw_dir)
        except (KeyError, OSError):
            home = Path(env.get("HOME", "/root"))
    repo = Path(env.get("OXP3_FIRSTRUN_REPO") or REPO)
    host = first_line(read_text(Path(prefix + "/proc/sys/kernel/hostname"))) or platform.node() or "host"
    return Context(
        prefix=prefix,
        home=home,
        repo=repo,
        user=user,
        host=host,
        euid=euid,
        runner=Runner(env=env),
        env=env,
    )


# ---------------------------------------------------------------------------
# Facts: one read of the cheap, shared state (units, files, journal access)
# ---------------------------------------------------------------------------
@dataclass
class Facts:
    kernel: str = ""
    os_pretty: str = ""
    kernel_cmdline: str = ""
    boot_id: str = ""
    uptime_s: Optional[float] = None
    units_system: Dict[str, Dict[str, str]] = field(default_factory=dict)
    units_user: Dict[str, Dict[str, str]] = field(default_factory=dict)
    user_bus: bool = False
    unit_list_rc: int = 0
    journal_ok: bool = False
    journal_detail: str = ""
    files: Dict[str, FileInfo] = field(default_factory=dict)
    suspend: Dict[str, str] = field(default_factory=dict)
    power: Dict[str, str] = field(default_factory=dict)
    battery: Dict[str, str] = field(default_factory=dict)
    platform_profile: Optional[str] = None
    driver_hashes: Dict[str, Optional[str]] = field(default_factory=dict)
    git: Dict[str, str] = field(default_factory=dict)
    hwdb_paths: List[str] = field(default_factory=list)
    logind_started: Optional[str] = None

    # -- conveniences used by the checklist --------------------------------
    def unit(self, name: str, user: bool = False) -> Dict[str, str]:
        return (self.units_user if user else self.units_system).get(name, {})

    def unit_state(self, name: str, user: bool = False) -> str:
        props = self.unit(name, user)
        if not props:
            return "missing"
        load = props.get("LoadState", "")
        active = props.get("ActiveState", "")
        file_state = props.get("UnitFileState", "")
        if load == "not-found":
            return "missing"
        if active == "active":
            return "active"
        if active == "failed":
            return "failed"
        return file_state or active or "unknown"

    def file(self, path: str) -> FileInfo:
        if path not in self.files:
            self.files[path] = file_info(Path(path))
        return self.files[path]


SUSPEND_STATS: Tuple[str, ...] = (
    "success",
    "fail",
    "failed_suspend",
    "failed_resume",
    "last_failed_dev",
    "last_failed_errno",
    "last_failed_step",
    "last_hw_sleep",
    "max_hw_sleep",
    "total_hw_sleep",
)


def parse_systemctl_show(text: str) -> Dict[str, Dict[str, str]]:
    """Split `systemctl show` output into one dict per unit (blank-line separated)."""
    units: Dict[str, Dict[str, str]] = {}
    current: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            ident = current.get("Id")
            if ident:
                units[ident] = current
            current = {}
            continue
        if "=" in line and not line.startswith(" "):
            key, value = line.split("=", 1)
            current[key] = value.strip()
    ident = current.get("Id")
    if ident:
        units[ident] = current
    return units


def _show_props() -> List[str]:
    props = (
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "Restart",
        "NRestarts",
        "ExecMainStatus",
        "ExecMainCode",
    )
    return ["--property=" + ",".join(props)]


def user_unit_props(ctx: Context, unit: str) -> Dict[str, str]:
    """Query one user unit that is not part of the fixed inventory (e.g. Decky)."""
    if not ctx.which("systemctl"):
        return {}
    res = ctx.run(["systemctl", "--user", "show", "--no-pager", *_show_props(), unit], 60)
    return parse_systemctl_show(res.out).get(unit, {})


def gather_facts(ctx: Context) -> Facts:
    facts = Facts()
    facts.kernel = (ctx.read("/proc/sys/kernel/osrelease") or "").strip() or platform.release()
    rel = read_text(ctx.sp("/etc/os-release"), 8192) or ""
    for line in rel.splitlines():
        if line.startswith("PRETTY_NAME="):
            facts.os_pretty = line.split("=", 1)[1].strip().strip('"')
    facts.kernel_cmdline = (ctx.read("/proc/cmdline", 8192) or "").strip()
    facts.boot_id = (ctx.read("/proc/sys/kernel/random/boot_id") or "").strip()

    for path, key in (
        ("/sys/power/mem_sleep", "mem_sleep"),
        ("/sys/power/state", "state"),
        ("/sys/power/disk", "disk"),
        ("/sys/firmware/acpi/platform_profile", "platform_profile"),
    ):
        value = ctx.read(path, 4096)
        if value is not None:
            facts.power[key] = value.strip()
    for zone, prefix in (("intel-rapl:0", "pl"), ("intel-rapl-mmio:0", "pl_mmio")):
        for index in (0, 1, 2):
            value = ctx.read(
                "/sys/class/powercap/%s/constraint_%d_power_limit_uw" % (zone, index), 4096
            )
            if value is not None:
                facts.power["%s%d_uw" % (prefix, index + 1)] = value.strip()
    facts.platform_profile = (ctx.read("/sys/firmware/acpi/platform_profile") or "").strip() or None
    for name in SUSPEND_STATS:
        value = ctx.read("/sys/power/suspend_stats/" + name, 4096)
        if value is not None:
            facts.suspend[name] = value.strip()

    # Units: one invocation each for system and user scope.
    if ctx.which("systemctl"):
        res = ctx.run(["systemctl", "show", "--no-pager", *_show_props(),
                       *SHOWABLE_SYSTEM_UNITS], 120)
        facts.units_system = parse_systemctl_show(res.out)
        facts.unit_list_rc = res.rc
        ures = ctx.run(
            ["systemctl", "--user", "show", "--no-pager", *_show_props(), *USER_UNITS], 120
        )
        facts.user_bus = ures.rc == 0
        if facts.user_bus:
            facts.units_user = parse_systemctl_show(ures.out)
        start = ctx.run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", "systemd-logind"], 60)
        if start.ok:
            facts.logind_started = start.out.strip() or None

    # Journal readability decides whether the journal slices are worth attempting.
    if ctx.which("journalctl"):
        res = ctx.run(["journalctl", "-b", "--no-pager", "-q", "-n", "1"], 60)
        facts.journal_ok = res.ok and bool(res.out.strip())
        if not facts.journal_ok:
            facts.journal_detail = (
                "journal not readable in this context (%s); run with sudo or add the user "
                "to the systemd-journal group" % (res.reason() or "no entries")
            )
    else:
        facts.journal_detail = "journalctl is not installed"

    # Files: system, firmware, home scope.
    for path, _label in SYSTEM_FILES:
        facts.files[path] = file_info(ctx.sp(path))
    for path in BT_FIRMWARE:
        facts.files[path] = file_info(ctx.sp(path))
    for relpath, _label in HOME_FILES:
        facts.files["~/" + relpath] = file_info(ctx.hp(relpath))
    for path in DRIVER_CANDIDATES:
        facts.files[path] = file_info(ctx.sp(path))
        facts.driver_hashes[path] = facts.files[path].sha256
    facts.hwdb_paths = [str(p) for p in ctx.glob(HWD_B_GLOB)]

    # Battery / power envelope scalars used by the checklist.
    supply = ctx.sp("/sys/class/power_supply")
    try:
        names = sorted(p.name for p in supply.iterdir() if p.is_dir())
    except OSError:
        names = []
    for name in names:
        if name.startswith("BAT"):
            for field_name in (
                "status", "capacity", "charge_control_end_threshold", "charge_behaviour",
                "energy_now", "power_now", "voltage_now", "cycle_count",
            ):
                value = read_text(supply / name / field_name, 4096)
                if value is not None:
                    facts.battery["%s/%s" % (name, field_name)] = value.strip()
        elif name.startswith(("AC", "ADP")):
            value = read_text(supply / name / "online", 4096)
            if value is not None:
                facts.battery["%s/online" % name] = value.strip()

    # Repository identity (a stale checkout is a diagnostic fact, not a secret).
    if ctx.which("git") and (ctx.repo / ".git").exists():
        for key, argv in (
            ("head", ["git", "-C", str(ctx.repo), "rev-parse", "--short", "HEAD"]),
            ("branch", ["git", "-C", str(ctx.repo), "rev-parse", "--abbrev-ref", "HEAD"]),
            ("dirty", ["git", "-C", str(ctx.repo), "status", "--porcelain"]),
        ):
            res = ctx.run(argv, 60)
            if res.ok:
                facts.git[key] = res.out.strip()
        if facts.git.get("dirty"):
            facts.git["dirty_lines"] = str(len(facts.git["dirty"].splitlines()))
            facts.git["dirty"] = "\n".join(facts.git["dirty"].splitlines()[:20])

    try:
        with open(ctx.sp("/proc/uptime"), "r", encoding="utf-8", errors="replace") as handle:
            facts.uptime_s = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        facts.uptime_s = None
    return facts


# ---------------------------------------------------------------------------
# Checklist: the ordered install steps with their live state
# ---------------------------------------------------------------------------
OK, TODO, PARTIAL, PENDING, UNKNOWN, NA = "ok", "todo", "partial", "pending", "unknown", "n/a"


@dataclass
class Step:
    step_id: str
    title: str
    state: str
    detail: str = ""
    command: str = ""


def _count_present(facts: Facts, paths: Iterable[str]) -> Tuple[int, int]:
    items = list(paths)
    return sum(1 for p in items if facts.file(p).exists), len(items)


def _unit_enabled(facts: Facts, name: str, user: bool = False) -> bool:
    props = facts.unit(name, user)
    return props.get("UnitFileState") in ("enabled", "enabled-runtime", "static", "generated")


def _preflight_facts(ctx: Context) -> Optional[Dict[str, Any]]:
    """Run tools/oxp3-preflight.sh read-only and return its JSON (or None).

    Fixture mode (``ctx.prefix``) never runs the host tool: the checklist tests
    are hermetic and a synthetic tree must not probe the real machine.
    """
    if ctx.prefix:
        return None
    tool = ctx.repo / "tools" / "oxp3-preflight.sh"
    if not tool.is_file():
        return None
    res = ctx.run([str(tool), "--json"], 120, cwd=ctx.repo)
    if res.rc not in (0, 1):
        return None
    try:
        data = json.loads(res.out)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def evaluate_checklist(ctx: Context, facts: Facts) -> List[Step]:
    steps: List[Step] = []

    # 1. driver stack --------------------------------------------------------
    driver = facts.file(DRIVER_CANDIDATES[0])
    xe_loaded = ctx.sp("/sys/module/xe").is_dir()
    parts = []
    if driver.exists and driver.sha256:
        parts.append("mesa %s" % driver.sha256[:16])
    elif driver.exists:
        parts.append("mesa present (unhashed)")
    else:
        parts.append("host Mesa driver missing")
    if facts.kernel:
        parts.append("kernel %s" % facts.kernel)
    parts.append("xe %s" % ("loaded" if xe_loaded else "not loaded"))
    steps.append(
        Step(
            "driver-stack",
            "driver stack (kernel, Mesa, gamescope)",
            OK if driver.exists and xe_loaded else TODO,
            "; ".join(parts),
            "./oxp3-fixes-fast.sh build && ./oxp3-fixes-fast.sh install",
        )
    )

    # 2. hardware installer --------------------------------------------------
    present, total = _count_present(facts, [p for p, _ in SYSTEM_FILES if p.startswith("/usr/local/libexec")])
    state = OK if present == total else (PARTIAL if present else TODO)
    steps.append(
        Step(
            "hw-install",
            "hardware install (libexec helpers)",
            state,
            "%d/%d helpers installed" % (present, total),
            "sudo tools/oxp3-hw-install.sh --all",
        )
    )

    # 2b. preflight: would an install complete, and what would be skipped? ----
    pre = _preflight_facts(ctx)
    if pre is None:
        pre_state, pre_detail = UNKNOWN, "preflight not run (fixture or tool missing)"
    else:
        counts = pre.get("counts") or {}
        skipped = pre.get("skipped_components") or []
        if pre.get("safe"):
            if counts.get("warn"):
                pre_state = PARTIAL
                pre_detail = "safe; %s warning(s); would skip: %s" % (
                    counts.get("warn"),
                    ", ".join(skipped) if skipped else "none")
            else:
                pre_state = OK
                pre_detail = "safe; 0 warnings, 0 blockers"
        else:
            pre_state = TODO
            pre_detail = "unsafe: %s blocker(s); would skip: %s" % (
                counts.get("fail", "?"),
                ", ".join(skipped) if skipped else "none")
    steps.append(
        Step(
            "preflight",
            "preflight (install prerequisites)",
            pre_state,
            pre_detail,
            "tools/oxp3-preflight.sh   # fix the FAIL rows, then docs/PREFLIGHT.md",
        )
    )

    # 3. udev + hwdb rules ---------------------------------------------------
    rule_paths = [
        "/etc/udev/rules.d/90-oxp3-hid.rules",
        "/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules",
    ]
    present, total = _count_present(facts, rule_paths)
    hwdb = bool(facts.hwdb_paths)
    state = OK if present == total and hwdb else (PARTIAL if present or hwdb else TODO)
    steps.append(
        Step(
            "udev-rules",
            "udev rules + volume force-release hwdb",
            state,
            "%d/%d rules, hwdb %s" % (present, total, "present" if hwdb else "missing"),
            "sudo tools/oxp3-hw-install.sh --leds --sleep && "
            "sudo tools/oxp3-voltap/install.sh install --hwdb",
        )
    )

    # 4. sleep trigger policy ------------------------------------------------
    dropin = facts.file("/etc/systemd/logind.conf.d/60-oxp3-sleep.conf")
    polkit = facts.file("/etc/polkit-1/rules.d/49-oxp3-suspend.rules")
    state = OK if dropin.exists and polkit.exists else (PARTIAL if dropin.exists or polkit.exists else TODO)
    steps.append(
        Step(
            "sleep-policy",
            "sleep trigger policy (logind drop-in + polkit)",
            state,
            "drop-in %s, polkit %s" % ("present" if dropin.exists else "missing",
                                       "present" if polkit.exists else "missing"),
            "sudo tools/oxp3-hw-install.sh --sleep",
        )
    )

    # 5. logind restart after the drop-in ------------------------------------
    state, detail = UNKNOWN, "drop-in absent"
    if dropin.exists:
        started = facts.logind_started
        if not started:
            state, detail = UNKNOWN, "systemd-logind start time unreadable"
        else:
            # Compare the two timestamps; logind only reads the drop-in at start.
            drop_mtime = dropin.mtime
            if drop_mtime and _ts_after(drop_mtime, started):
                state, detail = PENDING, "drop-in newer than logind start (%s)" % started
            else:
                state, detail = OK, "logind started after the drop-in (%s)" % started
    steps.append(
        Step(
            "logind-restart",
            "logind restarted after the drop-in",
            state,
            detail,
            "sudo systemctl restart systemd-logind   # documented step; never run by this tool",
        )
    )

    # 6. sleep units enabled -------------------------------------------------
    wanted = ("oxp3-sleep-wakeup.service", "oxp3-sleep-pre.service", "oxp3-sleep-log.service")
    enabled = [u for u in wanted if _unit_enabled(facts, u)]
    state = OK if len(enabled) == len(wanted) else (PARTIAL if enabled else TODO)
    steps.append(
        Step(
            "sleep-units",
            "suspend policy units enabled",
            state,
            "%d/%d enabled" % (len(enabled), len(wanted)),
            "sudo systemctl enable oxp3-sleep-wakeup.service oxp3-sleep-pre.service oxp3-sleep-log.service",
        )
    )

    # 7. Bluetooth -----------------------------------------------------------
    fw = [facts.file(p) for p in BT_FIRMWARE]
    fw_n = sum(1 for i in fw if i.exists)
    dracut = facts.file("/etc/dracut.conf.d/60-oxp3-bt-firmware.conf")
    hci0 = ctx.sp("/sys/class/bluetooth/hci0").exists()
    if fw_n == len(fw) and dracut.exists:
        state = OK
    elif fw_n:
        state = PARTIAL
    else:
        state = TODO
    steps.append(
        Step(
            "bluetooth",
            "Bluetooth firmware (updates/ + initramfs)",
            state,
            "firmware %d/%d, dracut %s, hci0 %s"
            % (fw_n, len(fw), "present" if dracut.exists else "missing",
               "present" if hci0 else "absent"),
            "sudo tools/oxp3-bt-firmware.sh   # then reboot for the initramfs",
        )
    )

    # 8. power envelope ------------------------------------------------------
    profile = facts.file("/etc/oxp3/power.json")
    pl1 = facts.power.get("pl1_uw", "")
    steps.append(
        Step(
            "power-profile",
            "power envelope (PL1/PL2/PL4)",
            OK if profile.exists else TODO,
            "profile %s; PL1 live %s"
            % ("saved" if profile.exists else "not saved",
               _fmt_w(pl1)),
            "sudo /usr/local/libexec/oxp3-powerctl profile balanced --save",
        )
    )

    # 9. battery care --------------------------------------------------------
    limit = facts.battery.get("BAT0/charge_control_end_threshold")
    behaviour = facts.battery.get("BAT0/charge_behaviour")
    if limit is None:
        state, detail = NA, "no charge-limit interface (desktop or VM?)"
    elif limit.strip() == "100":
        state, detail = TODO, "charge limit at 100% (care off)"
    else:
        state, detail = OK, "limit %s%%, behaviour %s" % (limit, behaviour or "?")
    steps.append(
        Step(
            "battery-care",
            "battery charge limit / inhibit",
            state,
            detail,
            "sudo tools/oxp3-powerctl.py battery limit 80 --save",
        )
    )

    # 10. IMU blacklist ------------------------------------------------------
    bmi = facts.file("/etc/modprobe.d/oxp3-bmi160.conf")
    steps.append(
        Step(
            "imu-blacklist",
            "IMU modprobe blacklist",
            OK if bmi.exists else TODO,
            "present" if bmi.exists else "missing",
            "sudo tools/oxp3-hw-install.sh --imu",
        )
    )

    # 11. speaker EQ ---------------------------------------------------------
    eq = facts.file("~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf")
    steps.append(
        Step(
            "audio-eq",
            "speaker EQ drop-in",
            OK if eq.exists else TODO,
            "present" if eq.exists else "missing",
            "tools/audio/eq-on.sh   # no root",
        )
    )

    # 12/13. user daemons ----------------------------------------------------
    rgb = facts.file("~/.config/oxp3-rgb.toml")
    steps.append(
        Step(
            "rgb-config",
            "RGB keeper config",
            OK if rgb.exists else TODO,
            "present" if rgb.exists else "missing",
            "install -m 0644 tools/oxp3-rgbd.toml.example ~/.config/oxp3-rgb.toml "
            "&& systemctl --user enable --now oxp3-rgbd.service",
        )
    )
    homebtn_state = facts.unit_state("oxp3-homebtn.service", user=True)
    homebtn_file = facts.file("~/.config/systemd/user/oxp3-homebtn.service")
    if homebtn_state == "active":
        hb, hb_detail = OK, "unit active"
    elif homebtn_state in ("enabled", "inactive", "failed") or homebtn_file.exists:
        hb, hb_detail = PARTIAL, "unit installed, state %s" % (homebtn_state or "?")
    elif facts.user_bus:
        hb, hb_detail = TODO, "not installed"
    else:
        hb, hb_detail = UNKNOWN, "no user bus in this context"
    steps.append(
        Step(
            "homebtn-unit",
            "home button / LED user unit",
            hb,
            hb_detail,
            "sudo tools/oxp3-hw-install.sh --leds",
        )
    )

    # 14. display ------------------------------------------------------------
    hdr = ctx.read("/sys/class/drm/card1-eDP-1/hdr_output_metadata_enabled")
    icc_dir = ctx.sp("/usr/share/color/icc")
    steps.append(
        Step(
            "display",
            "HDR / ICC / vibrance tooling",
            OK if (ctx.repo / "tools/display/oxp3-vibrance.fx").exists() else TODO,
            "shader %s; HDR sysfs %s; ICC dir %s"
            % ("present" if (ctx.repo / "tools/display/oxp3-vibrance.fx").exists() else "missing",
               (hdr or "n/a").strip(), "present" if icc_dir.exists() else "absent"),
            "tools/display/display-mode.sh status   # read-only",
        )
    )

    # 15. XMX ----------------------------------------------------------------
    icd = facts.file("~/.config/xess-xmx/icd-xmx.json")
    xmx_driver = facts.file("~/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so")
    env_hit = "INTC_ALT_DRIVER_EXTENSIONS_PATH" in ctx.env or any(
        marker in value
        for key, value in ctx.env.items()
        if key.startswith(("VK_ICD", "VK_DRIVER", "INTC_", "IGC_", "OCLOC", "OXP3_"))
        for marker in ("xess-xmx", "oxp3", "icd-xmx", "icd-flatpak")
    )
    state = OK if icd.exists and xmx_driver.exists else (PARTIAL if icd.exists else TODO)
    steps.append(
        Step(
            "xmx",
            "XMX driver + per-game ICD",
            state,
            "icd %s, driver %s, session env %s"
            % ("present" if icd.exists else "missing",
               ("present " + xmx_driver.sha256[:16]) if (xmx_driver.exists and xmx_driver.sha256)
               else ("present (unhashed)" if xmx_driver.exists else "missing"),
               "set" if env_hit else "not set"),
            "tools/xmx-enable.sh   # see docs/xess-xmx.md",
        )
    )

    # 16. Decky plugin -------------------------------------------------------
    plugins = ctx.hp("homebrew/plugins")
    decky = (plugins / "oxp3-status").is_dir()
    try:
        installed = sorted(p.name for p in plugins.iterdir() if p.is_dir()) if plugins.is_dir() else []
    except OSError:
        installed = []
    steps.append(
        Step(
            "decky-plugin",
            "Decky status plugin",
            OK if decky else TODO,
            "oxp3-status %s (%d plugin(s) installed)"
            % ("installed" if decky else "not installed", len(installed)),
            "tools/decky-oxp3-status/build.sh --offline   # then install the zip from build/",
        )
    )

    # 17. suspend evidence ---------------------------------------------------
    success = facts.suspend.get("success")
    try:
        n_success = int(success or 0)
    except ValueError:
        n_success = 0
    steps.append(
        Step(
            "suspend-evidence",
            "a real suspend recorded since boot",
            OK if n_success > 0 else TODO,
            "success=%s fail=%s hw_sleep=%s"
            % (success or "?", facts.suspend.get("fail", "?"),
               facts.suspend.get("total_hw_sleep", "?")),
            "sudo systemctl suspend   # owner-run test; this tool never suspends",
        )
    )

    # 18. collector ----------------------------------------------------------
    steps.append(
        Step(
            "collector",
            "evidence bundle collected",
            PENDING,
            "run --collect to produce docs/state/firstrun-<timestamp>/",
            "tools/oxp3-firstrun.sh --collect",
        )
    )
    return steps


def _fmt_w(uw: str) -> str:
    try:
        return "%.1f W" % (int(uw) / 1_000_000)
    except (TypeError, ValueError):
        return "?"


def _parse_ts(value: str) -> Optional[_dt.datetime]:
    """Parse the timestamp formats systemd emits (locale-independent ones only)."""
    value = value.strip()
    if not value or value in ("n/a", "0", "Mon 1970-01-01 00:00:00 UTC"):
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = _dt.datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    return None


def _ts_after(iso_a: str, b: str) -> bool:
    """True when ISO timestamp ``iso_a`` is after systemd timestamp ``b``."""
    parsed_b = _parse_ts(b)
    if parsed_b is None:
        return False
    try:
        parsed_a = _dt.datetime.strptime(iso_a, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return False
    return parsed_a > parsed_b


CHECK_RANK = {OK: 0, NA: 0, PENDING: 1, PARTIAL: 2, UNKNOWN: 2, TODO: 3}


def render_checklist(ctx: Context, steps: Sequence[Step], facts: Optional[Facts] = None) -> str:
    counts: Counter = Counter(s.state for s in steps)
    lines = [
        "oxp3 first-run checklist - OneXPlayer 3 hardware fixes",
        "repo:   %s" % ctx.repo,
        "user:   %s   home: %s   root: %s" % (ctx.user, ctx.home, "yes" if ctx.euid == 0 else "no"),
        "kernel: %s" % (facts.kernel if facts and facts.kernel else "?"),
        "",
        " #  %-34s %-8s %s" % ("step", "state", "detail"),
        "---  %-34s %-8s %s" % ("-" * 34, "-" * 8, "-" * 30),
    ]
    for index, step in enumerate(steps, 1):
        lines.append(" %2d  %-34s %-8s %s" % (index, step.title[:34], step.state, step.detail))
    summary = ", ".join("%d %s" % (counts[k], k) for k in (OK, PENDING, PARTIAL, TODO, UNKNOWN, NA) if counts[k])
    lines += ["", "checklist: %s" % (summary or "empty")]

    actionable = [s for s in steps if s.state in (TODO, PENDING)]
    if actionable:
        lines += ["", "NEXT (in install order; documented privileged steps are never run by this tool):"]
        for step in actionable[:8]:
            lines.append("  - %s: %s" % (step.title, step.command))
    else:
        lines.append("")
        lines.append("Nothing left to install from the checklist; run --collect and hand the bundle back.")
    if ctx.euid != 0:
        lines += [
            "",
            "note: run as root only where a step needs it (see the command column); collection is",
            "      read-only and already covers everything a non-root run can read.",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------
def _missing_artifact(relpath: str, title: str, detail: str, priority: int = 50) -> Artifact:
    return Artifact(relpath, title, "", "text", "missing", "", detail, priority)


def _progress(message: str) -> None:
    """One flushed progress line on stderr (never part of the artifact bundle)."""
    if not QUIET:
        print("[collect] " + message, file=sys.stderr, flush=True)


def run_tool(
    ctx: Context,
    relpath: str,
    title: str,
    argv: Sequence[str],
    priority: int,
    timeout_s: int = 120,
    max_lines: Optional[int] = None,
    cwd: Optional[Path] = None,
    transform: Optional[Callable[[str], str]] = None,
    note_expected_exit: bool = True,
    kind: str = "text",
) -> Artifact:
    """Run a read-only tool and capture its output as one artifact."""
    tool = argv[0]
    if not ctx.which(tool) and not Path(tool).exists():
        return _missing_artifact(relpath, title, "tool not found: %s" % tool, priority)
    # Collection runs for minutes; say what is running and how long it may take,
    # and report the time each step actually took.
    _progress("run  %-34s (max %3ds) ..." % (title, timeout_s))
    started = time.monotonic()
    res = ctx.run(argv, timeout_s, cwd)
    _progress("done %-34s %5.1fs %s" % (
        title, time.monotonic() - started,
        "TIMEOUT" if res.timed_out else "rc=%d" % res.rc))
    body = res.text
    if transform:
        body = transform(body)
    if res.timed_out:
        status, detail = "timeout", "timed out after %ds" % timeout_s
    elif res.rc == 127:
        status, detail = "missing", "tool not found"
    elif not body.strip():
        status = "denied" if res.rc in (1, 126, 13) else "error"
        detail = "no output (%s)" % (res.reason() or "empty")
    else:
        # A non-zero exit is data for these tools (verify exits 1 on FAIL, repo-test
        # exits 1 when a suite fails); record it instead of treating it as a failure.
        status, detail = "ok", ""
        if res.rc != 0 and note_expected_exit:
            detail = "exit %d" % res.rc
    if kind == "json":
        body_text = body.rstrip() + "\n"
    else:
        header = "# %s\n# command: %s\n# exit:    %d%s\n\n" % (
            title, " ".join(argv), res.rc, (" (%s)" % detail) if detail else ""
        )
        body_text = header + body + "\n"
    artifact = Artifact(relpath, title, body_text, kind, status,
                        " ".join(argv), detail, priority, max_lines)
    artifact.exit_code = res.rc
    return artifact


def tool_version(ctx: Context, argv: Sequence[str]) -> str:
    """First informative line of a --version probe (many tools write it to stderr)."""
    res = ctx.run(argv, 30)
    for candidate in (res.out, res.err):
        line = first_line(candidate)
        if line:
            return line
    return "?"


def _env_subset(env: Dict[str, str]) -> str:
    rows = []
    for key in sorted(env):
        if any(key.startswith(prefix) or key == prefix for prefix in ENV_PREFIXES):
            rows.append("%s=%s" % (key, env[key]))
    return "\n".join(rows) if rows else "(none of the graphics variables are set in this session)"


def _clip(text: str, limit: int = 240) -> str:
    """One bounded, ANSI-free line (a config value can be arbitrarily long)."""
    text = strip_ansi(str(text)).rstrip()
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _session_id_from_list(text: str) -> Optional[str]:
    """Pick the desktop session id out of ``loginctl list-sessions`` output.

    ``loginctl show-session self`` fails when the collector is not run from inside
    the graphical session (a SSH/agent context), so the list is the fallback. The
    columns are ID UID USER SEAT LEADER CLASS TTY ACTIVE [IDLE SINCE]; a "user"
    class row on a seat is the seat session, a "manager" row is not.
    """
    fallback: Optional[str] = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        session_id, _uid, _user, seat, _leader, klass = parts[:6]
        if klass != "user":
            continue
        if seat not in ("-", "seat0") and fallback is None:
            fallback = session_id
        if seat == "seat0":
            return session_id
    return fallback


def _logind_session_dump(ctx: Context) -> List[str]:
    """The logind facts that decide who acts on the power/suspend/lid keys."""
    lines: List[str] = []
    if not ctx.which("loginctl"):
        lines += ["  unknown (tool missing: loginctl) - session facts not readable", ""]
        return lines
    props = ("Type", "Class", "Active", "IdleHint", "State")
    prop_args = [arg for name in props for arg in ("-p", name)]
    listed = ctx.run(["loginctl", "list-sessions"], 30)
    shown = ctx.run(["loginctl", "show-session", "self", *prop_args], 30)
    source = "self"
    self_failure = ""
    if not (shown.ok and shown.out.strip()):
        self_failure = _clip(first_line(shown.err) or shown.reason() or "no output", 160)
        session_id = _session_id_from_list(listed.out) if listed.ok else None
        if session_id:
            shown = ctx.run(["loginctl", "show-session", session_id, *prop_args], 30)
            source = session_id
    lines.append("-- loginctl show-session %s %s" % (
        source, " ".join("-p " + name for name in props)))
    if source != "self" and self_failure:
        lines.append("  (self failed: %s; using the seat session from list-sessions)"
                     % self_failure)
    if shown.ok and shown.out.strip():
        lines.extend("  " + _clip(line) for line in shown.out.splitlines() if line.strip())
    else:
        lines.append("  unknown (loginctl show-session self failed: %s)"
                     % (self_failure or _clip(first_line(shown.err) or shown.reason()
                                             or "no output", 160)))
    lines += ["", "-- loginctl list-sessions"]
    if listed.ok and listed.out.strip():
        for line in listed.out.splitlines()[:25]:
            lines.append("  " + _clip(line))
        if len(listed.out.splitlines()) > 25:
            lines.append("  ... (%d more session row(s) omitted)" % (len(listed.out.splitlines()) - 25))
    else:
        lines.append("  unknown (loginctl list-sessions failed: %s)"
                     % (_clip(first_line(listed.err) or listed.reason() or "no output", 160)))
    lines.append("")
    return lines


def _kde_power_dump(ctx: Context) -> List[str]:
    """PowerDevil's configured power-key/lid values, read through kreadconfig."""
    lines: List[str] = ["== KDE PowerDevil settings (powerdevilrc)", ""]
    tool = next((name for name in ("kreadconfig6", "kreadconfig5") if ctx.which(name)), None)
    if tool is None:
        lines += [
            "  unknown (tool missing: kreadconfig6 and kreadconfig5)",
            "  (the powerdevilrc excerpt below is still authoritative for explicit values)",
            "",
        ]
        return lines
    others = [name for name in ("kreadconfig6", "kreadconfig5")
              if name != tool and ctx.which(name)]
    lines.append("  tool: %s%s" % (
        tool, (" (fallback available: %s)" % others[0]) if others else ""))
    lines.append("  file: powerdevilrc")
    for group in POWERDEVIL_GROUPS:
        for key in POWERDEVIL_KEYS:
            argv = [tool, "--file", "powerdevilrc", "--group", group, "--key", key]
            res = ctx.run(argv, 20)
            value = _clip(strip_ansi(res.out).strip())
            if not value:
                value = "(unset)" if res.ok else "(unreadable: %s)" % (
                    res.reason() or "no output")
            lines.append("  %-38s = %s" % ("%s.%s" % (group, key), value))
            # Plasma 6 nests the same keys one group deeper; the flat read above
            # cannot see them, so probe the documented nested group too.
            nested = ctx.run(
                [tool, "--file", "powerdevilrc", "--group", group,
                 "--group", POWERDEVIL_SUBGROUP, "--key", key], 20)
            nested_value = _clip(strip_ansi(nested.out).strip()) if nested.ok else ""
            if nested_value and nested_value != value:
                lines.append("  %-38s = %s" % (
                    "%s.%s.%s" % (group, POWERDEVIL_SUBGROUP, key), nested_value))
    lines += [
        "  note: Plasma 6 stores these keys in [AC][SuspendAndShutdown] /",
        "        [Battery][SuspendAndShutdown]; AutoSuspendAction is a timeout in",
        "        seconds (0 = never), not an action.",
        "  note: kreadconfig reads the config of the user this collector runs as;",
        "        the excerpt below is from ~/.config/powerdevilrc.",
        "",
    ]
    return lines


def _powerdevilrc_dump(ctx: Context) -> List[str]:
    """A bounded excerpt of the KDE config the PowerDevil values come from."""
    rel = ".config/powerdevilrc"
    lines: List[str] = ["== ~/.config/powerdevilrc", ""]
    lines.append("  path    : ~/" + rel)
    try:
        exists = ctx.hp(rel).exists()
    except OSError:
        exists = False
    lines.append("  exists  : %s" % ("yes" if exists else "no"))
    if not exists:
        lines += [
            "  (PowerDevil has not written a config; its built-in defaults apply)",
            "",
        ]
        return lines
    body = ctx.read_home(rel)
    if body is None:
        lines += ["  unreadable (permission denied)", ""]
        return lines
    excerpt = strip_ansi(body).splitlines()[:60]
    lines.append("  excerpt : first %d line(s)" % len(excerpt))
    lines.append("  --- begin ~/.config/powerdevilrc ---")
    lines.extend("    " + _clip(line, 300) for line in excerpt)
    lines.append("  --- end ~/.config/powerdevilrc ---")
    lines.append("")
    return lines


def _polkit_agent_dump(ctx: Context) -> List[str]:
    """Whether a session polkit agent can answer an authentication prompt.

    A Gamemode/Gamescope session has none, so any polkit action that needs an
    interactive decision (notably login1.suspend on a non-active seat) is denied
    instead of prompting; the shipped polkit rule is the non-interactive path.
    """
    lines: List[str] = ["== session polkit agent (Gamemode has none)", ""]
    if not ctx.which("pgrep"):
        lines += [
            "  unknown (tool missing: pgrep)",
            "  session polkit agent: unknown (tool missing: pgrep)",
            "",
        ]
        return lines
    running: List[str] = []
    failed = False
    for pattern in POLKIT_AGENTS:
        res = ctx.run(["pgrep", "-af", pattern], 15)
        output = strip_ansi(res.out).strip()
        if res.ok and output:
            running.append(pattern)
            rows = output.splitlines()[:5]
            pid = rows[0].split()[0] if rows[0].split() else "?"
            lines.append("  %-34s : running (pid %s)" % (pattern, pid))
            lines.extend("      " + _clip(row) for row in rows)
        else:
            if res.rc not in (0, 1):
                failed = True
            lines.append("  %-34s : not running (pgrep exit %d)" % (pattern, res.rc))
    if running:
        lines.append("  session polkit agent: present")
    elif failed:
        lines.append("  session polkit agent: unknown (pgrep failed)")
    else:
        lines.append("  session polkit agent: absent")
    lines.append("")
    return lines


def _powercap_dump(ctx: Context) -> List[str]:
    lines: List[str] = []
    base = ctx.sp("/sys/class/powercap")
    try:
        zones = sorted(p for p in base.iterdir() if p.is_dir() and p.name.startswith("intel-rapl"))
    except OSError:
        return ["(no /sys/class/powercap on this kernel)"]
    for zone in zones:
        lines.append("== %s" % zone.name)
        for field_name in (
            "constraint_0_name", "constraint_0_power_limit_uw", "constraint_0_time_window_us",
            "constraint_1_name", "constraint_1_power_limit_uw", "constraint_1_time_window_us",
            "constraint_2_name", "constraint_2_power_limit_uw", "constraint_2_time_window_us",
            "energy_uj", "max_energy_range_uj",
        ):
            value = read_text(zone / field_name, 4096)
            if value is not None:
                lines.append("  %-30s %s" % (field_name, value.strip()))
    return lines


def _battery_dump(ctx: Context) -> List[str]:
    lines: List[str] = []
    base = ctx.sp("/sys/class/power_supply")
    try:
        supplies = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return ["(no /sys/class/power_supply on this system)"]
    for supply in supplies:
        lines.append("== %s" % supply.name)
        for field_name in (
            "type", "status", "online", "capacity", "charge_control_end_threshold",
            "charge_behaviour", "energy_now", "energy_full", "power_now",
            "voltage_now", "cycle_count", "manufacturer", "model_name",
        ):
            value = read_text(supply / field_name, 4096)
            if value is not None:
                lines.append("  %-28s %s" % (field_name, value.strip()))
    return lines


def _thermal_dump(ctx: Context) -> List[str]:
    lines: List[str] = []
    base = ctx.sp("/sys/class/thermal")
    try:
        zones = sorted(p for p in base.glob("thermal_zone*"))
    except OSError:
        return ["(no thermal zones)"]
    for zone in zones[:16]:
        kind = (read_text(zone / "type", 128) or "?").strip()
        temp = (read_text(zone / "temp", 128) or "?").strip()
        lines.append("  %-18s %-22s %s" % (zone.name, kind, temp))
    return lines or ["(no thermal zones)"]


def _fan_dump(ctx: Context) -> List[str]:
    lines: List[str] = []
    base = ctx.sp("/sys/class/hwmon")
    try:
        hwmons = sorted(p for p in base.glob("hwmon*"))
    except OSError:
        return ["(no hwmon)"]
    for hwmon in hwmons[:8]:
        name = (read_text(hwmon / "name", 128) or "?").strip()
        fields = []
        for field_name in ("pwm1", "pwm1_enable", "fan1_input", "temp1_input"):
            value = read_text(hwmon / field_name, 128)
            if value is not None:
                fields.append("%s=%s" % (field_name, value.strip()))
        lines.append("  %-12s %-16s %s" % (hwmon.name, name, " ".join(fields) or "(no fan/pwm nodes)"))
    return lines or ["(no hwmon devices)"]


def _wakeup_dump(ctx: Context) -> List[str]:
    lines: List[str] = []
    acpi = ctx.read("/proc/acpi/wakeup", 1 << 16)
    if acpi:
        lines.append("== /proc/acpi/wakeup (enabled entries)")
        for raw in acpi.splitlines():
            parts = raw.split()
            if parts and parts[0] != "Device" and "*enabled" in raw:
                lines.append("  " + "  ".join(parts[:4]))
    wake = ctx.sp("/sys/class/wakeup")
    try:
        events = sorted(p for p in wake.iterdir() if p.is_dir())
    except OSError:
        events = []
    if events:
        lines.append("")
        lines.append("== /sys/class/wakeup (armed sources)")
        for event in events[:40]:
            name = (read_text(event / "name", 256) or "?").strip()
            active = (read_text(event / "active_count", 64) or "?").strip()
            count = (read_text(event / "event_count", 64) or "?").strip()
            lines.append("  %-24s %-34s active=%s events=%s" % (event.name[:24], name[:34], active, count))
    return lines or ["(no wakeup information readable)"]


def collect_all(ctx: Context, facts: Facts, opts: "Options") -> List[Artifact]:
    artifacts: List[Artifact] = []

    # -- the repository's own read-only tools -------------------------------
    if ctx.which("python3") is not None:
        pass  # the tools below are shell wrappers; no python gate needed
    artifacts.append(run_tool(
        ctx, "verify/verify.json", "oxp3-verify --json",
        [str(ctx.repo / "tools/oxp3-verify.sh"), "--json"], 20, 600, kind="json",
    ))
    artifacts.append(run_tool(
        ctx, "verify/verify.txt", "oxp3-verify (human report)",
        [str(ctx.repo / "tools/oxp3-verify.sh")], 21, 600,
    ))
    artifacts.append(run_tool(
        ctx, "status/status.json", "oxp3-status --json",
        [str(ctx.repo / "tools/oxp3-status.sh"), "--json"], 22, 180, kind="json",
    ))
    artifacts.append(run_tool(
        ctx, "status/hw-install-status.txt", "hardware installer --status",
        [str(ctx.repo / "tools/oxp3-hw-install.sh"), "--status"], 23, 120,
    ))
    artifacts.append(run_tool(
        ctx, "state/snapshot.txt", "state-snapshot.sh",
        [str(ctx.repo / "state-snapshot.sh")], 24, 120,
    ))
    if opts.no_repo_test:
        artifacts.append(Artifact(
            "repo/repo-test-fast.txt", "repo-test --fast (tail)", "",
            status="skipped", detail="disabled by --no-repo-test", priority=25,
        ))
    else:
        artifact = run_tool(
            ctx, "repo/repo-test-fast.txt", "repo-test --fast (tail)",
            [str(ctx.repo / "tools/repo-test.sh"), "--fast"], 25, 240,
            transform=lambda text: "\n".join(text.splitlines()[-60:]) + "\n",
        )
        artifacts.append(artifact)

    # -- system -------------------------------------------------------------
    sys_lines = [
        "== identity",
        "kernel           : %s" % facts.kernel,
        "os               : %s" % (facts.os_pretty or "?"),
        "arch             : %s" % platform.machine(),
        "boot_id          : (redacted: bare UUIDs are treated as device identifiers)",
        "uptime_s         : %s" % (facts.uptime_s if facts.uptime_s is not None else "?"),
        "euid             : %d" % ctx.euid,
        "repo             : %s" % ctx.repo,
        "git              : %s" % json.dumps(facts.git, sort_keys=True),
        "",
        "== /proc/cmdline",
        facts.kernel_cmdline or "(unreadable)",
        "",
        "== DMI / board identifiers (values redacted by policy)",
    ]
    for dmi in ("sys_vendor", "product_name", "board_name", "bios_version", "bios_date"):
        value = ctx.read("/sys/class/dmi/id/" + dmi, 512)
        sys_lines.append("  %-16s %s" % (dmi, (value or "?").strip()))
    for dmi in ("product_serial", "board_serial", "product_uuid", "chassis_serial"):
        value = ctx.read("/sys/class/dmi/id/" + dmi, 512)
        sys_lines.append("  %-16s %s" % (dmi, "<redacted:serial>" if value else "(not readable)"))
    sys_lines += ["", "== module parameters (xe / xe.enable_*) "]
    for param in ("/sys/module/xe/parameters/enable_sagv", "/sys/module/xe/parameters/enable_panel_replay"):
        value = ctx.read(param, 512)
        if value is not None:
            sys_lines.append("  %-40s %s" % (os.path.basename(param), value.strip()))
    artifacts.append(Artifact(
        "system/environment.txt", "kernel, OS, cmdline, DMI, git", "\n".join(sys_lines) + "\n",
        priority=30,
    ))

    hw_lines: List[str] = []
    for title, argv in (
        ("lspci -nn", ["lspci", "-nn"]),
        ("lsusb", ["lsusb"]),
        ("lsusb -t", ["lsusb", "-t"]),
    ):
        hw_lines.append("== %s" % title)
        if not ctx.which(argv[0]):
            hw_lines.append("  (tool not installed: %s)" % argv[0])
            continue
        res = ctx.run(argv, 60)
        hw_lines.append(res.out.rstrip() or "  (no output; %s)" % (res.reason() or "empty"))
        hw_lines.append("")
    artifacts.append(Artifact("system/hardware.txt", "lspci / lsusb summary", "\n".join(hw_lines) + "\n",
                              priority=31))

    # -- units --------------------------------------------------------------
    unit_lines: List[str] = ["== system units (systemctl show)", ""]
    for unit in SYSTEM_UNITS:
        props = facts.unit(unit)
        if not props:
            note = ("template unit: see list-unit-files" if "@" in unit
                    else "missing (no data)")
            unit_lines.append("%-32s %s" % (unit, note))
            continue
        unit_lines.append(
            "%-32s load=%-9s file=%-10s active=%-9s sub=%-10s restarts=%-3s exec=%s"
            % (
                unit, props.get("LoadState", "-"), props.get("UnitFileState", "-"),
                props.get("ActiveState", "-"), props.get("SubState", "-"),
                props.get("NRestarts", "-"), props.get("ExecMainStatus", "-"),
            )
        )
    unit_lines += ["", "== systemctl --failed", ""]
    failed = ctx.run(["systemctl", "--failed", "--no-pager", "--no-legend"], 60) if ctx.which("systemctl") else None
    unit_lines.append((failed.out.strip() if failed else "") or "(none)")
    unit_lines += ["", "== systemctl list-unit-files 'oxp3-*'", ""]
    listed = ctx.run(["systemctl", "list-unit-files", "oxp3-*", "--no-pager", "--no-legend"], 60) if ctx.which("systemctl") else None
    unit_lines.append((listed.out.strip() if listed else "") or "(no oxp3 unit files)")
    artifacts.append(Artifact("units/system.txt", "oxp3 system units", "\n".join(unit_lines) + "\n",
                              priority=32))

    user_lines = ["== user units (systemctl --user show)", ""]
    if not facts.user_bus:
        user_lines.append("(no user systemd bus in this context: run from the desktop session)")
    for unit in USER_UNITS:
        props = facts.unit(unit, user=True)
        if not props:
            user_lines.append("%-32s %s" % (unit, "missing (no data)" if facts.user_bus else "unknown (no bus)"))
            continue
        user_lines.append(
            "%-32s load=%-9s file=%-10s active=%-9s sub=%s"
            % (unit, props.get("LoadState", "-"), props.get("UnitFileState", "-"),
               props.get("ActiveState", "-"), props.get("SubState", "-"))
        )
    user_lines += ["", "== files (user scope, from the invoking user's home)"]
    for relpath, label in HOME_FILES:
        info = facts.file("~/" + relpath)
        user_lines.append("  %-58s %s" % ("~/" + relpath, "present" if info.exists else "missing"))
        if info.exists:
            user_lines.append("      %-54s %s" % (label, info.sha256 or "<no-hash>"))
    artifacts.append(Artifact("units/user.txt", "oxp3 user units and user files", "\n".join(user_lines) + "\n",
                              priority=33))

    # -- udev / polkit / logind --------------------------------------------
    udev_lines = ["== installed rules and drop-ins", ""]
    for path, label in SYSTEM_FILES:
        if not any(path.startswith(prefix) for prefix in (
            "/etc/udev", "/etc/polkit", "/etc/systemd", "/etc/modprobe", "/etc/dracut", "/etc/oxp3",
        )):
            continue
        info = facts.file(path)
        udev_lines.append("  %-64s %-9s %s" % (path, "present" if info.exists else "MISSING",
                                               info.sha256 or ""))
        if info.exists:
            udev_lines.append("      %-60s %s  %s B" % (label, info.mtime or "?", info.size or "?"))
    udev_lines += ["", "== volume hwdb"]
    if facts.hwdb_paths:
        for path in facts.hwdb_paths:
            info = file_info(Path(path))
            udev_lines.append("  %-64s %s" % (path, info.sha256 or "<no-hash>"))
    else:
        udev_lines.append("  (no 84-oxp3-volume-keys*.hwdb installed)")
    hwdb_bin = ctx.sp("/etc/udev/hwdb.bin")
    udev_lines.append("  /etc/udev/hwdb.bin %s, mtime %s"
                      % ("present" if hwdb_bin.exists() else "MISSING", iso_mtime(hwdb_bin) or "?"))
    udev_lines += ["", "== udevadm verify on the installed rules"]
    if ctx.which("udevadm"):
        for path in (
            "/etc/udev/rules.d/90-oxp3-hid.rules",
            "/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules",
        ):
            real = ctx.sp(path)
            if not real.exists():
                udev_lines.append("  %s: not installed" % path)
                continue
            res = ctx.run(["udevadm", "verify", str(real)], 60)
            udev_lines.append("  %s: %s" % (path, res.text.strip() or "exit %d" % res.rc))
    else:
        udev_lines.append("  (udevadm not installed)")
    artifacts.append(Artifact("udev/rules.txt", "udev / polkit / logind policy files",
                              "\n".join(udev_lines) + "\n", priority=34))

    # -- firmware -----------------------------------------------------------
    fw_lines = ["== pinned Bluetooth firmware (paths and hashes only, never the bytes)", ""]
    for path in BT_FIRMWARE:
        info = facts.file(path)
        fw_lines.append("  %-70s %-9s %s B  %s"
                        % (path, "present" if info.exists else "MISSING",
                           info.size if info.size is not None else "?", info.sha256 or ""))
        if info.exists:
            fw_lines.append("      mtime %s" % (info.mtime or "?"))
    fw_lines += ["", "== module state", ""]
    for module in ("btintel_pcie", "btintel", "bluetooth"):
        present = ctx.sp("/sys/module/" + module).is_dir()
        fw_lines.append("  %-16s %s" % (module, "loaded" if present else "not loaded"))
    if ctx.which("modinfo"):
        for module in ("btintel_pcie",):
            res = ctx.run(["modinfo", "-F", "filename", module], 30)
            fw_lines.append("  modinfo %s: %s" % (module, res.out.strip() or res.text.strip() or "n/a"))
    fw_lines += ["", "== controller / initramfs drop-in", ""]
    fw_lines.append("  /sys/class/bluetooth/hci0 %s"
                    % ("present" if ctx.sp("/sys/class/bluetooth/hci0").exists() else "absent"))
    dracut = facts.file("/etc/dracut.conf.d/60-oxp3-bt-firmware.conf")
    if dracut.exists:
        fw_lines.append("  %s:" % dracut.path)
        fw_lines.extend("    " + line for line in (read_text(ctx.sp(dracut.path), 4096) or "").splitlines()[:40])
    else:
        fw_lines.append("  /etc/dracut.conf.d/60-oxp3-bt-firmware.conf MISSING")
    fw_lines.append("  (the running initramfs only carries the files after a rebuild and reboot)")
    artifacts.append(Artifact("firmware/bluetooth.txt", "Bluetooth firmware presence and hashes",
                              "\n".join(fw_lines) + "\n", priority=35))

    # -- power --------------------------------------------------------------
    power_lines = ["== platform profile", ""]
    profile = ctx.read("/sys/firmware/acpi/platform_profile")
    choices = ctx.read("/sys/firmware/acpi/platform_profile_choices")
    power_lines.append("  current : %s" % ((profile or "?").strip()))
    power_lines.append("  choices : %s" % ((choices or "?").strip()))
    power_lines += ["", "== saved profile (/etc/oxp3/power.json)"]
    saved = read_text(ctx.sp("/etc/oxp3/power.json"), 8192)
    power_lines.append(saved.rstrip() if saved else "  (not saved)")
    power_lines += ["", "== powercap / RAPL"] + _powercap_dump(ctx)
    power_lines += ["", "== battery and AC"] + _battery_dump(ctx)
    power_lines += ["", "== thermal zones"] + _thermal_dump(ctx)
    power_lines += ["", "== fan / hwmon"] + _fan_dump(ctx)
    artifacts.append(Artifact("power/power.txt", "power envelope, battery, fan, thermals",
                              "\n".join(power_lines) + "\n", priority=36))

    # -- sleep --------------------------------------------------------------
    sleep_lines = ["== sleep capability", ""]
    for path in ("/sys/power/mem_sleep", "/sys/power/state", "/sys/power/disk"):
        sleep_lines.append("  %-24s %s" % (path, (ctx.read(path) or "?").strip()))
    sleep_lines += ["", "== suspend_stats (this boot)"]
    if facts.suspend:
        for name in SUSPEND_STATS:
            if name in facts.suspend:
                sleep_lines.append("  %-20s %s" % (name, facts.suspend[name]))
    else:
        sleep_lines.append("  (no /sys/power/suspend_stats)")
    sleep_lines += ["", "== wake sources"] + _wakeup_dump(ctx)
    sleep_lines += ["", "== repository audit (tools/sleep-power-measure.sh audit, read-only)"]
    tool = ctx.repo / "tools/sleep-power-measure.sh"
    if tool.exists() and ctx.which("bash"):
        res = ctx.run(["bash", str(tool), "audit"], 120)
        sleep_lines.append(res.text.strip() or "(no output; %s)" % (res.reason() or "empty"))
    else:
        sleep_lines.append("  (tool not available)")
    artifacts.append(Artifact("sleep/sleep.txt", "suspend stats and wake sources",
                              "\n".join(sleep_lines) + "\n", priority=37))

    # -- audio --------------------------------------------------------------
    audio_lines = ["== ALSA / SOF cards", ""]
    cards = ctx.read("/proc/asound/cards", 8192)
    audio_lines.append(cards.strip() if cards else "(no /proc/asound/cards)")
    audio_lines += ["", "== PipeWire / PulseAudio", ""]
    if ctx.which("pactl"):
        for title, argv in (
            ("pactl info", ["pactl", "info"]),
            ("pactl list short sinks", ["pactl", "list", "short", "sinks"]),
            ("pactl get-default-sink", ["pactl", "get-default-sink"]),
        ):
            res = ctx.run(argv, 60)
            audio_lines.append("-- %s" % title)
            audio_lines.append(res.text.strip() or "  (no server: %s)" % (res.reason() or "empty"))
            audio_lines.append("")
        modules = ctx.run(["pactl", "list", "short", "modules"], 60)
        audio_lines.append("-- filter-chain modules")
        hits = [line for line in modules.out.splitlines() if re.search(r"filter|loopback|oxp3", line, re.I)]
        audio_lines.append("\n".join(hits[:20]) or "  (none)")
    else:
        audio_lines.append("  (pactl not installed)")
    audio_lines += ["", "== speaker EQ drop-in"]
    eq_path = "~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf"
    eq = facts.file(eq_path)
    audio_lines.append("  %s %s" % (eq_path, "present" if eq.exists else "MISSING"))
    if eq.exists:
        audio_lines.append("  sha256 %s" % (eq.sha256 or "<no-hash>"))
        audio_lines.extend("    " + line for line in (ctx.read_home(HOME_FILES[1][0], 4096) or "").splitlines()[:25])
    audio_lines += ["", "== audio units (user scope)", ""]
    for unit in ("oxp3-audio-route.service", "pipewire.service", "wireplumber.service"):
        props = facts.unit(unit, user=True)
        audio_lines.append("  %-28s %s" % (unit, props.get("ActiveState", "unknown (no bus)")))
    for tool_name, argv in (("pipewire", ["pipewire", "--version"]), ("wireplumber", ["wireplumber", "--version"])):
        if ctx.which(tool_name):
            res = ctx.run(argv, 30)
            audio_lines.append("  %s: %s" % (tool_name, res.out.strip().splitlines()[0] if res.out.strip() else "?"))
    artifacts.append(Artifact("audio/audio.txt", "audio sinks and filter-chain state",
                              "\n".join(audio_lines) + "\n", priority=38))

    # -- display ------------------------------------------------------------
    display_lines = ["== display-mode status (kscreen-doctor, ANSI stripped)", ""]
    script = ctx.repo / "tools/display/display-mode.sh"
    if script.exists() and ctx.which("bash"):
        res = ctx.run(["bash", str(script), "status"], 120)
        display_lines.append(strip_ansi(res.text).strip() or "(no output; %s)" % (res.reason() or "empty"))
    else:
        display_lines.append("  (display-mode.sh not available)")
    display_lines += ["", "== vibrance tooling"]
    vibrance = ctx.repo / "tools/display/vibrance.sh"
    if vibrance.exists() and ctx.which("bash"):
        res = ctx.run(["bash", str(vibrance), "status"], 60)
        display_lines.append(strip_ansi(res.text).strip() or "(no output)")
    else:
        display_lines.append("  (vibrance.sh not available)")
    display_lines += ["", "== ICC profiles"]
    for probe in ("/usr/share/color/icc", str(ctx.home / ".local/share/color/icc")):
        real = ctx.sp(probe) if probe.startswith("/usr") else Path(probe)
        try:
            entries = sorted(p.name for p in real.iterdir())[:20] if real.is_dir() else []
        except OSError:
            entries = []
        display_lines.append("  %-45s %s" % (probe, ", ".join(entries) or "(none)"))
    artifacts.append(Artifact("display/display.txt", "HDR / ICC / vibrance state",
                              "\n".join(display_lines) + "\n", priority=39))

    # -- XMX ----------------------------------------------------------------
    xmx_lines = ["== session environment (graphics variables only)", "", _env_subset(ctx.env), ""]
    xmx_lines += ["== driver candidates (sha256 of the shipped bytes)", ""]
    for path, digest in sorted(facts.driver_hashes.items()):
        info = facts.file(path)
        xmx_lines.append("  %-62s %-9s %s" % (path, "present" if info.exists else "MISSING", digest or ""))
    xmx_lines += ["", "== per-game ICD files"]
    for relpath in (".config/xess-xmx/icd-xmx.json", ".config/xess-xmx/oxp3-env.sh",
                    ".config/xess-xmx/flatpak-env.sh"):
        info = facts.file("~/" + relpath)
        xmx_lines.append("  %-45s %-9s %s" % ("~/" + relpath, "present" if info.exists else "MISSING",
                                              info.sha256 or ""))
    xmx_lines += ["", "== XMX kernel cache"]
    cache = ctx.hp(".cache/xess-xmx/kernels")
    try:
        kernel_files = sorted(p.name for p in cache.iterdir())[:40] if cache.is_dir() else []
    except OSError:
        kernel_files = []
    xmx_lines.append("  %s: %d entr%s" % (cache, len(kernel_files), "y" if len(kernel_files) == 1 else "ies"))
    xmx_lines.extend("    " + name for name in kernel_files[:20])
    xmx_lines += ["", "== compiler (IGC / ocloc)"]
    oclocs: List[Path] = []
    for rel in (
        ".local/share/xess-xmx/igc/neo/bin",
        ".local/share/xess-xmx/neo/bin",
    ):
        oclocs.extend(sorted(p for p in ctx.hp(rel).glob("ocloc*") if p.is_file()))
    if ctx.which("ocloc"):
        xmx_lines.append("  ocloc (PATH): " + tool_version(ctx, ["ocloc", "--version"]))
    if oclocs:
        # Do not execute it: the IGC binary needs LD_LIBRARY_PATH from its own wrapper
        # (enable.sh), so a bare run fails on libocloc.so and proves nothing.
        for ocloc in oclocs[:4]:
            info = file_info(ocloc)
            xmx_lines.append("  %-58s present %s B %s" % (
                str(ocloc), info.size if info.size is not None else "?",
                (info.sha256 or "<no-hash>")[:16]))
    else:
        xmx_lines.append("  ocloc: not found (no PATH entry, no ~/.local/share/xess-xmx install)")
    overrides = facts.file("~/.local/share/flatpak/overrides/org.anatase.Steam")
    xmx_lines += ["", "== Steam Flatpak overrides"]
    if overrides.exists:
        xmx_lines.extend("  " + line for line in (ctx.read_home(HOME_FILES[8][0], 4096) or "").splitlines()[:40])
    else:
        xmx_lines.append("  (none written)")
    artifacts.append(Artifact("gfx/xmx.txt", "XMX session environment and driver hashes",
                              "\n".join(xmx_lines) + "\n", priority=40))

    # -- decky --------------------------------------------------------------
    decky_lines = ["== Decky plugin inventory", ""]
    plugins = ctx.hp("homebrew/plugins")
    try:
        names = sorted(p.name for p in plugins.iterdir() if p.is_dir()) if plugins.is_dir() else []
    except OSError:
        names = []
    decky_lines.append("  plugin dir: %s (%d plugin(s))" % (plugins, len(names)))
    decky_lines.extend("    " + name for name in names[:40])
    decky_lines += ["", "== oxp3-status plugin"]
    plugin_dir = plugins / "oxp3-status"
    if plugin_dir.is_dir():
        for relpath in ("plugin.json", "dist/index.js", "main.py"):
            info = file_info(plugin_dir / relpath, with_hash=True)
            decky_lines.append("  %-22s %-9s %s" % (relpath, "present" if info.exists else "MISSING",
                                                    info.sha256 or ""))
        meta = read_text(plugin_dir / "plugin.json", 8192)
        if meta:
            try:
                parsed = json.loads(meta)
                decky_lines.append("  version: %s  name: %s" % (parsed.get("version", "?"), parsed.get("name", "?")))
            except ValueError:
                decky_lines.append("  plugin.json present but unparsable")
    else:
        decky_lines.append("  not installed (no ~/homebrew/plugins/oxp3-status)")
    decky_lines += ["", "== loader unit / zip"]
    props = user_unit_props(ctx, "plugin_loader.service")
    decky_lines.append("  plugin_loader.service: %s" % props.get("ActiveState", "unknown (no user bus)"))
    zips = sorted((ctx.repo / "tools/decky-oxp3-status/build").glob("oxp3-status-*.zip")) \
        if (ctx.repo / "tools/decky-oxp3-status/build").is_dir() else []
    decky_lines.append("  built zip: %s" % (", ".join(p.name for p in zips) or "(not built here)"))
    artifacts.append(Artifact("decky/decky.txt", "Decky plugin presence", "\n".join(decky_lines) + "\n",
                              priority=41))

    # -- session / power key ------------------------------------------------
    # The logind facts are cheap; the KDE half is the only place that records what
    # PowerDevil will actually do with the power button and the lid (the sleep
    # inhibitors prove it owns the events, not what it does with them).
    session_lines = ["== logind session facts", ""]
    session_lines += _logind_session_dump(ctx)
    session_lines += _kde_power_dump(ctx)
    session_lines += _powerdevilrc_dump(ctx)
    session_lines += _polkit_agent_dump(ctx)
    artifacts.append(Artifact(
        "session/power.txt",
        "logind session, PowerDevil power-key/lid policy, session polkit agent",
        "\n".join(session_lines) + "\n", priority=45,
    ))

    # -- journal ------------------------------------------------------------
    journal_specs = (
        ("journal/oxp3-units.txt", "journal for the oxp3 units",
         ["-b", "--no-pager", "-o", "short-precise", "-n", "600"]
         + [arg for unit in SYSTEM_UNITS for arg in ("-u", unit)], 70, 600),
        ("journal/kernel.txt", "kernel lines: bluetooth/suspend/audio/GPU",
         ["-b", "-k", "--no-pager", "-o", "short-precise", "-n", "800",
          "--grep=" + KERNEL_JOURNAL_GREP], 71, 800),
        ("journal/sleep-tag.txt", "oxp3-sleep tagged suspend audit",
         ["-b", "-t", "oxp3-sleep", "--no-pager", "-o", "short-precise", "-n", "200"], 72, 200),
    )
    for relpath, title, args, priority, cap in journal_specs:
        if opts.no_journal:
            artifacts.append(Artifact(relpath, title, "", status="skipped",
                                      detail="journal collection disabled by --no-journal",
                                      priority=priority, max_lines=cap))
            continue
        if not facts.journal_ok:
            artifacts.append(Artifact(relpath, title, "", status="denied",
                                      detail=facts.journal_detail, priority=priority, max_lines=cap))
            continue
        artifacts.append(run_tool(
            ctx, relpath, title, ["journalctl", *args], priority, 180, max_lines=cap,
        ))
    # The user-session journal cannot be merged into the same argv; add it as its own file.
    if opts.no_journal:
        artifacts.append(Artifact("journal/oxp3-user-units.txt", "journal for the user units", "",
                                  status="skipped", detail="journal collection disabled by --no-journal",
                                  priority=73, max_lines=400))
    elif not facts.journal_ok:
        artifacts.append(Artifact("journal/oxp3-user-units.txt", "journal for the user units", "",
                                  status="denied", detail=facts.journal_detail, priority=73, max_lines=400))
    elif not facts.user_bus:
        artifacts.append(Artifact("journal/oxp3-user-units.txt", "journal for the user units", "",
                                  status="unavailable", detail="no user systemd bus in this context",
                                  priority=73, max_lines=400))
    else:
        artifacts.append(run_tool(
            ctx, "journal/oxp3-user-units.txt", "journal for the user units",
            ["journalctl", "--user", "-b", "--no-pager", "-o", "short-precise", "-n", "400",
             *[arg for unit in USER_UNITS for arg in ("-u", unit)]],
            73, 180, max_lines=400,
        ))
    return artifacts


# ---------------------------------------------------------------------------
# Bundling: line/byte limits, redaction, manifest
# ---------------------------------------------------------------------------
class UsageError(Exception):
    """Bad invocation (exit 2)."""


class CollectionError(Exception):
    """A real collection failure: the bundle could not be written (exit 1)."""


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def truncate_lines(text: str, limit: int) -> Tuple[str, int, bool]:
    """Keep at most ``limit`` lines and append a visible truncation marker."""
    if limit <= 0:
        return "", 0, bool(text)
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, len(lines), False
    kept = lines[:limit]
    kept.append("... [truncated: showing %d of %d lines]" % (limit, len(lines)))
    return "\n".join(kept) + "\n", limit + 1, True


def default_out_dir(repo: Path, now: _dt.datetime) -> Path:
    return repo / "docs" / "state" / ("firstrun-" + now.strftime("%Y-%m-%dT%H%MZ"))


def plan_out_dir(
    requested: Optional[str], repo: Path, now: _dt.datetime, force: bool
) -> Path:
    """Decide the bundle directory, refusing to mix into unrelated content."""
    target = Path(requested).expanduser() if requested else default_out_dir(repo, now)
    if target.exists():
        if not target.is_dir():
            raise UsageError("output path exists and is not a directory: %s" % target)
        try:
            non_empty = any(target.iterdir())
        except OSError as exc:
            raise UsageError("output directory is not readable: %s (%s)" % (target, exc))
        if non_empty and not force:
            raise UsageError(
                "output directory is not empty: %s (choose another --out or pass --force)"
                % target
            )
    return target


def finalize_artifacts(
    artifacts: List[Artifact], limits: Limits, redactor: Redactor
) -> List[Artifact]:
    """Redact, cap and (for missing/denied artifacts) zero the payload of each artifact."""
    for art in artifacts:
        if art.status != "ok":
            art.text = ""
            art.bytes = 0
            art.lines = 0
            continue
        text, counts = redactor.redact(art.text)
        redactor.note_file(art.relpath, counts)
        art.text = text
        art.lines = count_lines(text)
        if art.kind == "json":
            try:
                json.loads(text)
                art.json_valid = True
            except ValueError:
                art.json_valid = False
        cap = limits.max_lines
        if art.max_lines is not None:
            cap = min(cap, art.max_lines)
        if art.kind == "json":
            # Machine data is never line-truncated: a broken JSON artifact is worse
            # than an absent one. The byte budget below can still skip it.
            pass
        else:
            text, lines, truncated = truncate_lines(text, cap)
            art.text = text
            art.lines = lines
            if truncated:
                art.truncated = True
                art.reason = (art.reason + "; " if art.reason else "") + (
                    "line cap %d" % cap
                )
        art.bytes = len(art.text.encode("utf-8"))
    return artifacts


def fit_budget(artifacts: Sequence[Artifact], budget: int) -> None:
    """Fit the payload into ``budget`` bytes, marking what had to be cut."""
    remaining = max(0, budget)
    for art in artifacts:
        if art.status != "ok":
            continue
        data = art.text.encode("utf-8")
        if len(data) <= remaining:
            art.bytes = len(data)
            remaining -= len(data)
            continue
        if art.kind == "json" or remaining < MIN_TRUNCATE:
            art.status = "skipped"
            art.reason = (art.reason + "; " if art.reason else "") + (
                "bundle size budget exhausted"
            )
            art.text = ""
            art.bytes = 0
            art.lines = 0
            continue
        marker = "\n... [truncated: bundle size budget reached]\n"
        keep = max(0, remaining - len(marker.encode("utf-8")))
        cut = data[:keep].decode("utf-8", "ignore")
        art.text = cut + marker
        art.bytes = len(art.text.encode("utf-8"))
        art.lines = count_lines(art.text)
        art.truncated = True
        art.reason = (art.reason + "; " if art.reason else "") + "bundle size budget"
        remaining -= art.bytes


def _redact_deep(redactor: Redactor, value: Any) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)[0]
    if isinstance(value, dict):
        return {redactor.redact(str(k))[0]: _redact_deep(redactor, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_deep(redactor, item) for item in value]
    return value


def render_redactions(report: Dict[str, Any]) -> str:
    lines = [
        "# Redaction report",
        "#",
        "# Applied to every byte of this bundle before it was written. Only counts are",
        "# recorded; the replaced values are not stored anywhere.",
        "",
        "rules (total %d replacement(s)):" % report.get("total", 0),
    ]
    rules: Dict[str, int] = report.get("rules", {})
    if not rules:
        lines.append("  (nothing matched)")
    for name, count in sorted(rules.items()):
        lines.append("  %-28s %d" % (name, count))
    files: Dict[str, Dict[str, int]] = report.get("files", {})
    lines += ["", "files with matches (%d):" % len(files)]
    for relpath, counts in sorted(files.items()):
        detail = ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))
        lines.append("  %-46s %s" % (relpath, detail))
    if report.get("files_omitted"):
        lines.append("  ... %d further file(s) omitted from this index" % report["files_omitted"])
    lines += [
        "",
        "Kept on purpose: sha256 digests, PCI/USB ids, kernel version, power/thermal",
        "values and the vendor OUI of MAC addresses (first three octets only).",
        "",
    ]
    return "\n".join(lines)


def build_manifest(
    ctx: Context,
    facts: Facts,
    limits: Limits,
    artifacts: Sequence[Artifact],
    steps: Sequence[Step],
    redaction: Dict[str, Any],
    out: Path,
    mode: str,
    notes: Sequence[str],
) -> Dict[str, Any]:
    counts: Counter = Counter(a.status for a in artifacts)
    journal = {
        "available": facts.journal_ok,
        "detail": facts.journal_detail or "ok",
        "files": [a.relpath for a in artifacts if a.relpath.startswith("journal/")],
    }
    manifest = {
        "schema": SCHEMA,
        "tool": "oxp3-firstrun",
        "version": VERSION,
        "generated_at": ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "out": str(out),
        "repo": str(ctx.repo),
        "root": ctx.euid == 0,
        "kernel": facts.kernel,
        "os": facts.os_pretty,
        "limits": {"max_bytes": limits.max_bytes, "max_lines": limits.max_lines},
        "counts": {
            "artifacts": len(artifacts),
            "ok": counts.get("ok", 0),
            "missing": counts.get("missing", 0),
            "denied": counts.get("denied", 0),
            "unavailable": counts.get("unavailable", 0),
            "timeout": counts.get("timeout", 0),
            "error": counts.get("error", 0),
            "skipped": counts.get("skipped", 0),
            "truncated": sum(1 for a in artifacts if a.truncated),
        },
        "journal": journal,
        "redaction": redaction,
        "checklist": [
            {"id": s.step_id, "title": s.title, "state": s.state, "detail": s.detail,
             "command": s.command}
            for s in steps
        ],
        "artifacts": [
            {
                "path": a.relpath,
                "title": a.title,
                "kind": a.kind,
                "status": a.status,
                "bytes": a.bytes,
                "lines": a.lines,
                "truncated": a.truncated,
                "json_valid": a.json_valid,
                "exit_code": a.exit_code,
                "reason": a.reason,
                "command": a.command,
            }
            for a in artifacts
        ],
        "notes": list(notes),
        "complete": True,
        "partial_data": any(
            a.status not in ("ok",) or a.truncated for a in artifacts
        ),
    }
    return manifest


def render_readme(
    out: Path, manifest: Dict[str, Any], steps: Sequence[Step]
) -> str:
    counts = manifest["counts"]
    redaction = manifest["redaction"]
    archive = out.parent / (out.name + ".tar.gz")
    lines = [
        "oxp3 first-run evidence bundle",
        "==============================",
        "",
        "generated : %s" % manifest["generated_at"],
        "tool      : %s %s" % (manifest["tool"], manifest["version"]),
        "mode      : %s" % manifest["mode"],
        "kernel    : %s" % manifest["kernel"],
        "",
        "What this is",
        "-------------",
        "One read-only snapshot of the OneXPlayer 3 hardware fixes: the verifier, the",
        "status backend, unit states, udev/polkit/logind drop-ins, firmware presence and",
        "sha256 digests, power/battery/thermal state, suspend statistics and wake sources,",
        "the logind session and PowerDevil's power-key/lid policy (with a powerdevilrc",
        "excerpt and the session polkit agent state), audio filter-chain state,",
        "HDR/ICC/vibrance state, the session's XMX environment, the Decky plugin",
        "inventory, the kernel command line, lspci/lsusb, and bounded journal slices for",
        "our units and for the bluetooth/suspend/audio/GPU kernel messages.",
        "",
        "Nothing in the bundle was produced by writing to the system: the collector only",
        "reads files and runs read-only probes. No firmware blob, no binary and no vendor",
        "code is included - firmware appears as a path, a size and a sha256.",
        "",
        "Contents",
        "--------",
        "  MANIFEST.json          machine summary (this file's structured twin)",
        "  checklist.txt          the ordered install checklist with the state at collection",
        "  REDACTIONS.txt         which rules fired, how often, in which files",
        "  verify/                oxp3-verify.json + human report",
        "  status/                oxp3-status.json + hardware installer status",
        "  units/                 oxp3 system/user unit states",
        "  udev/                  installed rules, polkit rule, logind drop-in, hwdb",
        "  firmware/              Bluetooth firmware presence + sha256 (never the bytes)",
        "  power/                 RAPL/powercap, battery, fan, thermal zones",
        "  sleep/                 suspend stats, wake sources, the repo's sleep audit",
        "  audio/                 sinks, filter-chain modules, speaker EQ drop-in",
        "  display/               HDR/ICC state, vibrance tooling",
        "  gfx/                   XMX session environment + driver hashes",
        "  decky/                 Decky plugin inventory",
        "  session/               logind session, PowerDevil keys + powerdevilrc, polkit agent",
        "  system/                kernel, cmdline, DMI (serials redacted), lspci/lsusb",
        "  repo/                  repo-test --fast tail",
        "  journal/               bounded journal slices",
        "",
        "Redaction",
        "---------",
        "Usernames, home paths, hostname, MAC addresses (only the vendor OUI is kept),",
        "serial numbers/UUIDs and token-like strings were replaced before writing.",
        "%d replacement(s) across %d rule(s) in %d file(s); see REDACTIONS.txt." % (
            redaction.get("total", 0), len(redaction.get("rules", {})),
            int(redaction.get("files_with_matches", 0)),
        ),
        "sha256 digests are kept unchanged on purpose (they identify bytes, not people).",
        "",
        "Limits",
        "------",
        "  total cap   : %d bytes (bundle is %s)" % (
            manifest["limits"]["max_bytes"],
            _human_bytes(sum(int(a.get("bytes", 0)) for a in manifest["artifacts"])),
        ),
        "  line cap    : %d lines per text file" % manifest["limits"]["max_lines"],
        "  truncations : %d artifact(s)" % counts["truncated"],
        "",
        "Hand it back",
        "------------",
        "  tar -C %s -czf %s %s" % (out.parent, archive, out.name),
        "  sha256sum %s" % archive,
        "",
        "Send the archive (or just the directory) as-is. If you prefer not to share the",
        "journals, delete the journal/ subdirectory first; MANIFEST.json still records",
        "that they existed.",
        "",
        "Reading it in a later session",
        "------------------------------",
        "Start with MANIFEST.json: counts, checklist, and per-artifact status/limit info.",
        "Then verify/verify.json (the acceptance rows), units/system.txt (did the units",
        "load), power/power.txt (live PL1/PL2/PL4), session/power.txt (who owns the",
        "power key and what PowerDevil is set to) and journal/kernel.txt (the boot-level",
        "story). docs/FIRSTRUN.md has the full reading order and the open points.",
        "",
    ]
    todo = [s for s in steps if s.state in (TODO, PENDING)]
    if todo:
        lines += ["Still open at collection time", "-----------------------------"]
        for step in todo:
            lines.append("  %-28s %s" % (step.state, step.title))
            lines.append("      %s" % step.command)
        lines.append("")
    return "\n".join(lines)


def _human_bytes(value: int) -> str:
    if value < 1024:
        return "%d B" % value
    if value < 1024 * 1024:
        return "%.1f KiB" % (value / 1024)
    return "%.1f MiB" % (value / (1024 * 1024))


def write_bundle(
    ctx: Context,
    out: Path,
    artifacts: List[Artifact],
    steps: Sequence[Step],
    facts: Facts,
    limits: Limits,
    redactor: Redactor,
    mode: str,
    dry_run: bool,
    notes: Sequence[str] = (),
) -> Dict[str, Any]:
    """Write (or, with ``dry_run``, only plan) the bundle. Returns the manifest."""
    if limits.max_bytes < MIN_MAX_BYTES:
        raise UsageError("--max-bytes must be at least %d" % MIN_MAX_BYTES)
    ordered = [a for _, a in sorted(enumerate(artifacts), key=lambda pair: (pair[1].priority, pair[0]))]
    fit_budget(ordered, max(0, limits.max_bytes - (INDEX_RESERVE + MANIFEST_RESERVE)))

    checklist_out, _ = redactor.redact(render_checklist(ctx, steps, facts))
    redaction_report = redactor.report()
    redactions_text, _ = redactor.redact(render_redactions(redaction_report))

    payloads: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
    payloads["checklist.txt"] = ("text", checklist_out)
    for art in ordered:
        if art.status == "ok" and art.text:
            payloads[art.relpath] = (art.kind, art.text)
    payloads["REDACTIONS.txt"] = ("text", redactions_text)

    manifest: Dict[str, Any] = {}
    manifest_text = ""
    readme_text = ""
    dropped: List[str] = []

    def build(notes_now: Sequence[str]) -> Tuple[Dict[str, Any], str, str, int]:
        """Build manifest + README and size the whole bundle (self-consistent)."""
        manifests = build_manifest(
            ctx, facts, limits, ordered, steps, redaction_report, out, mode, notes_now
        )
        payload_total = sum(len(p.encode("utf-8")) for _kind, p in payloads.values())
        # README depends on the manifest; the manifest counts the README and itself,
        # so converge on a stable total (a couple of iterations at most).
        guess = payload_total + 4096
        text = ""
        readme = ""
        for _ in range(4):
            manifests["files_total"] = len(payloads) + 2
            manifests["bytes_total"] = guess
            readme, _ = redactor.redact(render_readme(out, manifests, steps))
            readme_len = len(readme.encode("utf-8"))
            text, _ = redactor.redact(json.dumps(manifests, indent=2) + "\n")
            actual = payload_total + readme_len + len(text.encode("utf-8"))
            if actual == guess:
                break
            guess = actual
        return manifests, text, readme, guess

    total = 0
    for _attempt in range(20):
        notes_now = list(notes)
        if dropped:
            notes_now = notes_now + ["dropped: " + ", ".join(dropped)]
        manifest, manifest_text, readme_text, total = build(notes_now)
        if total <= limits.max_bytes:
            break
        candidates = [rel for rel in payloads if rel != "checklist.txt"]
        if not candidates:
            raise CollectionError(
                "bundle cannot fit in --max-bytes %d (index and manifest alone exceed it)"
                % limits.max_bytes
            )
        victim = candidates[-1]
        payloads.pop(victim)
        dropped.append(victim)
        for art in ordered:
            if art.relpath == victim:
                art.status = "skipped"
                art.text = ""
                art.bytes = 0
                art.reason = (art.reason + "; " if art.reason else "") + "bundle size budget"

    payloads["README.txt"] = ("text", readme_text)
    payloads["MANIFEST.json"] = ("json", manifest_text)
    if not dry_run:
        try:
            out.mkdir(parents=True, exist_ok=True)
            for relpath, (_kind, payload) in payloads.items():
                target = out / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload.encode("utf-8"))
        except OSError as exc:
            raise CollectionError("cannot write bundle to %s: %s" % (out, exc))

    manifest["files_total"] = len(payloads)
    manifest["bytes_total"] = total
    manifest["dry_run"] = dry_run
    manifest["notes"] = list(manifest.get("notes", []))
    if dropped:
        manifest["notes"] = manifest["notes"] + ["dropped: " + ", ".join(dropped)]
    return manifest


def render_summary(manifest: Dict[str, Any], out: Path, dry_run: bool) -> str:
    lines = []
    counts = manifest["counts"]
    lines.append("")
    lines.append("%s %s" % ("dry-run (nothing written):" if dry_run else "bundle:", out))
    lines.append(
        "  %d artifact(s): %d ok, %d missing, %d denied, %d unavailable, %d skipped, %d truncated"
        % (
            counts["artifacts"], counts["ok"], counts["missing"], counts["denied"],
            counts["unavailable"], counts["skipped"], counts["truncated"],
        )
    )
    redaction = manifest["redaction"]
    lines.append(
        "  redaction: %d replacement(s) in %d rule(s), %d file(s)"
        % (redaction.get("total", 0), len(redaction.get("rules", {})),
           int(redaction.get("files_with_matches", 0)))
    )
    lines.append(
        "  size: %s of a %s cap, %d file(s) in the bundle"
        % (_human_bytes(int(manifest.get("bytes_total", 0))),
           _human_bytes(int(manifest["limits"]["max_bytes"])),
           int(manifest.get("files_total", 0)))
    )
    for art in manifest["artifacts"]:
        if art["status"] != "ok":
            lines.append("  %-10s %-34s %s" % (art["status"], art["path"], art["reason"] or art["title"]))
        elif art["truncated"]:
            lines.append("  %-10s %-34s %s" % ("truncated", art["path"], art["reason"]))
    if not dry_run:
        lines.append("")
        lines.append("hand back:")
        lines.append(
            "  tar -C %s -czf /tmp/oxp3-firstrun-%s.tar.gz %s && sha256sum /tmp/oxp3-firstrun-%s.tar.gz"
            % (out.parent, out.name, out.name, out.name)
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Self-test (offline; synthetic fixtures + stub commands)
# ---------------------------------------------------------------------------
class _Checker:
    def __init__(self) -> None:
        self.total = 0
        self.failures: List[str] = []

    def ok(self, condition: bool, label: str) -> None:
        self.total += 1
        if not condition:
            self.failures.append(label)

    def eq(self, label: str, got: Any, want: Any) -> None:
        self.total += 1
        if got != want:
            self.failures.append("%s: got %r want %r" % (label, got, want))


class StubRunner:
    """Command double for --selftest and the unit tests (no host access)."""

    def __init__(self, responses: Sequence[Tuple[str, CmdResult]] = ()) -> None:
        self.responses = list(responses)
        self.seen: List[str] = []
        self.default_rc = 0

    def run(self, argv: Sequence[str], timeout_s: int = 120, cwd: Optional[Path] = None) -> CmdResult:
        key = " ".join(str(a) for a in argv)
        self.seen.append(key)
        for prefix, result in self.responses:
            if key.startswith(prefix):
                return result
        return CmdResult(argv, self.default_rc, "", "")


_SELFTEST_REDACTION = (
    ("ssh: user holzr at anatase-oxp3.lan", "holzr", "<user>"),
    ("home is /var/home/holzr/git/x", "/var/home/holzr", "~/"),
    ("host anatase-oxp3", "anatase-oxp3", "<host>"),
    ("mac 9c:6b:00:11:22:33", "9c:6b:00:11:22:33", "<mac-oui:9c:6b:00>"),
    ("mac 9C-6B-00-44-55-66", "9c-6b-00-44-55-66", "<mac-oui:9c:6b:00>"),
    ("Serial Number: ABC123XY", "ABC123XY", "<redacted:serial>"),
    ("root=UUID=a245ffab-5073-45ee-b3ed-e7ecdec29275 ro", "a245ffab-5073", "<redacted:uuid>"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123", "<redacted:github-token>"),
    ("key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "<redacted:aws-key>"),
    ("Authorization: Bearer abcdef0123456789", "abcdef0123456789", "<redacted:bearer>"),
    ("password=hunter2", "hunter2", "<redacted:secret>"),
    ("api_key: sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz", "<redacted:openai-key>"),
    ("mail holzr@example.com", "holzr@example.com", "<email>"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefgh", "eyJhbGciOiJIUzI1NiJ9", "<redacted:jwt>"),
)


def _selftest_redaction(checks: _Checker) -> Redactor:
    literals = build_literals(Path("/var/home/holzr"), "holzr", "anatase-oxp3")
    redactor = Redactor(literals)
    for text, forbidden, required in _SELFTEST_REDACTION:
        out, counts = redactor.redact(text)
        checks.ok(forbidden not in out, "redaction leaves no raw value in: %r -> %r" % (text, out))
        checks.ok(required in out, "redaction marker %r for %r (got %r)" % (required, text, out))
        checks.ok(counts, "redaction counts recorded for %r" % text)
        again, _ = redactor.redact(out)
        checks.ok(again == out, "redaction is idempotent for %r" % text)
    digest = "sha256: " + "a" * 64
    out, _ = redactor.redact(digest)
    checks.eq("sha256 digests are preserved", out, digest)
    return redactor


def _selftest_limits(checks: _Checker) -> None:
    text = "".join("line %d\n" % i for i in range(50))
    out, lines, truncated = truncate_lines(text, 10)
    checks.ok(truncated and lines == 11, "line cap truncates and counts lines")
    checks.ok("truncated: showing 10 of 50" in out, "line cap marker is visible")
    out2, lines2, truncated2 = truncate_lines("a\nb\n", 10)
    checks.ok(not truncated2 and lines2 == 2, "line cap keeps short files unchanged")

    redactor = Redactor(build_literals(Path("/var/home/tester"), "tester", "box"))
    artifacts = [
        Artifact("a.txt", "a", "x" * 4000 + "\n", priority=1),
        Artifact("b.txt", "b", "y" * 4000 + "\n", priority=2),
        Artifact("c.json", "c", json.dumps({"k": "v" * 4000}) + "\n", kind="json", priority=3),
    ]
    limits = Limits(max_bytes=6000, max_lines=100)
    finalize_artifacts(artifacts, limits, redactor)
    fit_budget(artifacts, 1000)
    total = sum(a.bytes for a in artifacts)
    checks.ok(total <= 1000, "byte budget is never exceeded (%d <= 1000)" % total)
    checks.ok(any(a.truncated or a.status == "skipped" for a in artifacts),
              "budget records a truncation or skip")
    checks.ok(all(a.status in ("ok", "skipped") for a in artifacts), "budget only ok/skipped statuses")
    json_art = [a for a in artifacts if a.kind == "json"][0]
    checks.ok(json_art.status == "skipped", "json artifacts are never truncated")


def _selftest_bundle(checks: _Checker, root: Path) -> None:
    now = _dt.datetime(2026, 11, 9, 3, 50, tzinfo=_dt.timezone.utc)
    home = root / "home" / "tester"
    repo = home / "repo"
    (repo / "docs" / "state").mkdir(parents=True, exist_ok=True)
    (home / ".config").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "oxp3-rgb.toml").write_text("effect = 'wave'\n", encoding="utf-8")
    (home / ".config" / "powerdevilrc").write_text(
        "[AC][SuspendAndShutdown]\nAutoSuspendAction=0\n", encoding="utf-8")

    runner = StubRunner([
        ("systemctl show -p ActiveEnterTimestamp",
         CmdResult(["systemctl"], 0, "Sun 2026-11-09 01:00:00 UTC\n")),
        ("systemctl show", CmdResult(["systemctl"], 0, (
            "Id=oxp3-voltap.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
            "UnitFileState=enabled\nNRestarts=0\nExecMainStatus=0\n\n"
        ))),
        ("systemctl --user show", CmdResult(["systemctl"], 0, (
            "Id=oxp3-homebtn.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
            "UnitFileState=enabled\n\n"
        ))),
        ("journalctl -b --no-pager -q -n 1", CmdResult(["journalctl"], 0, "entry\n")),
        ("journalctl", CmdResult(["journalctl"], 0, "Nov 09 03:00:00 box kernel: suspend entry\n")),
        ("pactl", CmdResult(["pactl"], 1, "", "Connection refused")),
        ("lspci", CmdResult(["lspci"], 0, "00:00.0 Host bridge\n")),
        ("lsusb", CmdResult(["lsusb"], 0, "Bus 001 Device 001: ID 1d6b:0002\n")),
        ("ocloc", CmdResult(["ocloc"], 0, "ocloc version 25.18.1\n")),
    ])
    ctx = Context(
        prefix=str(root / "sysroot"),
        home=home,
        repo=repo,
        user="tester",
        host="box",
        euid=1000,
        runner=runner,
        env={"PATH": "/usr/bin", "HOME": str(home), "VK_ICD_FILENAMES": str(home / ".config/xess-xmx/icd-xmx.json")},
        now=now,
    )
    # Minimal fixture tree so the checklist sees a mix of ok/todo/pending.
    sysroot = Path(ctx.prefix)
    for rel, text in (
        ("etc/udev/rules.d/90-oxp3-hid.rules", 'SUBSYSTEM=="hidraw"'),
        ("etc/systemd/logind.conf.d/60-oxp3-sleep.conf", "[Login]\nHandlePowerKey=suspend\n"),
        ("usr/local/libexec/oxp3-powerctl", "#!/bin/sh\n"),
        ("etc/modprobe.d/oxp3-bmi160.conf", "blacklist bmi160_i2c\n"),
        ("sys/firmware/acpi/platform_profile", "balanced\n"),
        ("sys/power/mem_sleep", "[s2idle] deep\n"),
        ("sys/power/suspend_stats/success", "1\n"),
    ):
        path = sysroot / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    facts = gather_facts(ctx)
    steps = evaluate_checklist(ctx, facts)
    checks.ok(any(s.step_id == "sleep-policy" and s.state == PARTIAL for s in steps),
              "checklist: partial sleep policy is reported")
    checks.ok(any(s.step_id == "logind-restart" and s.state == PENDING for s in steps),
              "checklist: drop-in newer than logind start is pending")
    checks.ok(any(s.step_id == "suspend-evidence" and s.state == OK for s in steps),
              "checklist: recorded suspend is ok")

    artifacts = collect_all(ctx, facts, Options(collect=True, no_repo_test=True))
    checks.ok(any(a.relpath == "units/system.txt" for a in artifacts), "collection includes unit state")
    checks.ok(any(a.relpath == "journal/kernel.txt" for a in artifacts), "collection includes the kernel journal")
    checks.ok(any(a.status == "skipped" for a in artifacts), "collection records skipped optional data")
    session = next((a for a in artifacts if a.relpath == "session/power.txt"), None)
    checks.ok(session is not None, "collection includes the session power policy")
    if session is not None:
        checks.ok("== logind session facts" in session.text,
                  "session artifact carries the logind session facts")
        checks.ok("PowerButtonAction" in session.text,
                  "session artifact carries the PowerDevil key values")
        checks.ok("AutoSuspendAction=0" in session.text,
                  "session artifact carries the powerdevilrc excerpt")
        checks.ok("== session polkit agent" in session.text,
                  "session artifact carries the polkit agent check")

    redactor = Redactor(build_literals(ctx.home, ctx.user, ctx.host))
    limits = Limits(max_bytes=200_000, max_lines=200)
    finalize_artifacts(artifacts, limits, redactor)
    out = repo / "docs" / "state" / "firstrun-test"
    manifest = write_bundle(ctx, out, artifacts, steps, facts, limits, redactor,
                            "collect", dry_run=True)
    checks.ok(not out.exists(), "dry-run writes nothing")
    checks.ok(manifest["counts"]["artifacts"] == len(artifacts), "dry-run still reports every artifact")
    checks.ok(manifest["redaction"]["total"] > 0, "manifest records redaction counts")

    manifest = write_bundle(ctx, out, artifacts, steps, facts, limits, redactor,
                            "collect", dry_run=False)
    checks.ok((out / "MANIFEST.json").is_file(), "real run writes MANIFEST.json")
    checks.ok((out / "checklist.txt").is_file(), "real run writes checklist.txt")
    parsed = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    checks.eq("manifest schema", parsed["schema"], SCHEMA)
    checks.ok("tester" not in (out / "MANIFEST.json").read_text(encoding="utf-8"),
              "manifest does not leak the username")
    checks.ok("tester" not in (out / "README.txt").read_text(encoding="utf-8"),
              "README does not leak the username")
    checks.ok("~/repo/docs/state/firstrun-test" in json.dumps(parsed), "manifest paths are redacted to ~")
    sizes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    checks.ok(sizes <= 200_000, "bundle total stays under the cap (%d)" % sizes)

    # A second run over the same directory must be idempotent in content.
    first = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    write_bundle(ctx, out, artifacts, steps, facts, limits, redactor,
                 "collect", dry_run=False)
    second = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    checks.eq("re-running produces the same file set", sorted(first), sorted(second))

    # Refuse to mix into a non-empty directory unless --force is given.
    try:
        plan_out_dir(str(out), repo, now, force=False)
        checks.ok(False, "non-empty output dir is refused without --force")
    except UsageError:
        checks.ok(True, "non-empty output dir is refused without --force")
    planned = plan_out_dir(str(out), repo, now, force=True)
    checks.eq("--force reuses the directory", planned, out)


def run_selftest() -> int:
    checks = _Checker()
    _selftest_redaction(checks)
    _selftest_limits(checks)
    with tempfile.TemporaryDirectory(prefix="oxp3-firstrun-selftest-") as tmp:
        _selftest_bundle(checks, Path(tmp))
    if checks.failures:
        for failure in checks.failures[:20]:
            print("FAIL %s" % failure)
        print("selftest: %d check(s), %d FAILED" % (checks.total, len(checks.failures)))
        return 1
    print("selftest: %d checks passed (synthetic fixtures, stub commands, no host access)" % checks.total)
    return 0


# ---------------------------------------------------------------------------
# Options and CLI
# ---------------------------------------------------------------------------
@dataclass
class Options:
    collect: bool = False
    out: Optional[str] = None
    json: bool = False
    no_journal: bool = False
    no_repo_test: bool = False
    dry_run: bool = False
    check_only: bool = False
    force: bool = False
    max_bytes: int = DEFAULT_MAX_BYTES
    max_lines: int = DEFAULT_MAX_LINES
    selftest: bool = False


def parse_args(argv: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(
        prog="oxp3-firstrun",
        description="One-shot first-run checklist and redacted evidence collector "
                    "for the OneXPlayer 3 hardware fixes (read-only).",
        epilog="Docs: docs/FIRSTRUN.md",
    )
    parser.add_argument("--collect", action="store_true",
                        help="gather the evidence bundle (default: print the checklist)")
    parser.add_argument("--check-only", action="store_true",
                        help="print the checklist and stop (same as the default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan the bundle, run every read-only probe, write nothing")
    parser.add_argument("--out", metavar="DIR",
                        help="bundle directory (default docs/state/firstrun-<UTC timestamp>)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable checklist (or MANIFEST) on stdout")
    parser.add_argument("--no-journal", action="store_true",
                        help="skip the journal slices (they are recorded as skipped)")
    parser.add_argument("--no-repo-test", action="store_true",
                        help="skip the (slow) repo-test --fast tail")
    parser.add_argument("--force", action="store_true",
                        help="write into a non-empty --out directory")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="total bundle cap in bytes (default %d)" % DEFAULT_MAX_BYTES)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                        help="per-file line cap (default %d)" % DEFAULT_MAX_LINES)
    parser.add_argument("--selftest", action="store_true",
                        help="run the offline self-test against synthetic fixtures")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = parser.parse_args(list(argv))

    if args.max_bytes < MIN_MAX_BYTES:
        parser.error("--max-bytes must be at least %d" % MIN_MAX_BYTES)
    if args.max_lines < 10:
        parser.error("--max-lines must be at least 10")
    opts = Options(
        collect=args.collect,
        out=args.out,
        json=args.json,
        no_journal=args.no_journal,
        no_repo_test=args.no_repo_test,
        dry_run=args.dry_run,
        check_only=args.check_only,
        force=args.force,
        max_bytes=args.max_bytes,
        max_lines=args.max_lines,
        selftest=args.selftest,
    )
    if opts.check_only and opts.collect:
        parser.error("--check-only and --collect are mutually exclusive")
    if opts.check_only:
        opts.collect = False
        opts.dry_run = False
    return opts


def _checklist_json(ctx: Context, facts: Facts, steps: Sequence[Step]) -> Dict[str, Any]:
    counts: Counter = Counter(s.state for s in steps)
    return {
        "schema": SCHEMA,
        "tool": "oxp3-firstrun",
        "version": VERSION,
        "mode": "check-only",
        "generated_at": ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": ctx.euid == 0,
        "kernel": facts.kernel,
        "journal_available": facts.journal_ok,
        "counts": {state: counts[state] for state in (OK, PENDING, PARTIAL, TODO, UNKNOWN, NA)},
        "steps": [
            {"id": s.step_id, "title": s.title, "state": s.state, "detail": s.detail,
             "command": s.command}
            for s in steps
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = parse_args(argv)
    except SystemExit as exc:  # argparse already printed the usage/help message
        code = exc.code
        return 0 if code in (0, None) else int(code)
    if opts.selftest:
        return run_selftest()

    ctx = build_context(prefix=os.environ.get("OXP3_FIRSTRUN_ROOT", "").rstrip("/"))
    try:
        facts = gather_facts(ctx)
        steps = evaluate_checklist(ctx, facts)
    except Exception as exc:  # a probe bug must not masquerade as a clean run
        print("oxp3-firstrun: checklist failed: %s" % exc, file=sys.stderr)
        return 1

    if not opts.collect and not opts.dry_run:
        if opts.json:
            print(json.dumps(_checklist_json(ctx, facts, steps), indent=2))
        else:
            print(render_checklist(ctx, steps, facts), end="")
        return 0

    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        out = plan_out_dir(opts.out, ctx.repo, now, opts.force or opts.dry_run)
    except UsageError as exc:
        print("oxp3-firstrun: %s" % exc, file=sys.stderr)
        return 2

    if not opts.json:
        print(render_checklist(ctx, steps, facts), end="")

    notes: List[str] = []
    if opts.no_journal:
        notes.append("journal collection disabled by --no-journal")
    if opts.no_repo_test:
        notes.append("repo-test --fast skipped by --no-repo-test")
    if not facts.journal_ok:
        notes.append("journal unavailable: %s" % (facts.journal_detail or "unknown reason"))
    if not facts.user_bus:
        notes.append("no user systemd bus: user units are reported from the filesystem only")

    try:
        artifacts = collect_all(ctx, facts, opts)
    except Exception as exc:
        print("oxp3-firstrun: collection failed: %s" % exc, file=sys.stderr)
        return 1

    serials: List[str] = []
    for dmi in ("product_serial", "board_serial", "chassis_serial"):
        value = (ctx.read("/sys/class/dmi/id/" + dmi, 512) or "").strip()
        if len(value) >= 6:
            serials.append(value)
    redactor = Redactor(build_literals(ctx.home, ctx.user, ctx.host, serials))
    limits = Limits(max_bytes=opts.max_bytes, max_lines=opts.max_lines)
    finalize_artifacts(artifacts, limits, redactor)

    for step in steps:
        if step.step_id == "collector":
            step.state = PENDING if opts.dry_run else OK
            step.detail = ("dry run: nothing written" if opts.dry_run
                           else "bundle written to %s" % out)
            step.command = "" if not opts.dry_run else "tools/oxp3-firstrun.sh --collect"

    mode = "dry-run" if opts.dry_run else "collect"
    try:
        manifest = write_bundle(
            ctx, out, artifacts, steps, facts, limits, redactor, mode, opts.dry_run, notes
        )
    except UsageError as exc:
        print("oxp3-firstrun: %s" % exc, file=sys.stderr)
        return 2
    except CollectionError as exc:
        print("oxp3-firstrun: %s" % exc, file=sys.stderr)
        return 1

    if opts.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(render_summary(manifest, out, opts.dry_run), end="")

    # Only a bundle that produced nothing at all counts as a real failure.
    if manifest["counts"]["ok"] == 0:
        print("oxp3-firstrun: no usable artifact was collected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted: no bundle was finalised (partial work is in the transcript "
              "directory, if one was created)", file=sys.stderr)
        sys.exit(130)
