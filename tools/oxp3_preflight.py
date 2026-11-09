#!/usr/bin/env python3
"""oxp3_preflight - read-only prerequisite validator for the OneXPlayer 3 fixes.

The hardware installers in this repository write to /etc, /usr/local, udev,
polkit, logind, dracut and the user's systemd instance. A missing prerequisite
turns that into a half-install: some files land, the unit cannot start, and the
next session has to guess why. This tool derives every prerequisite from the
installers themselves and answers one question before anything is written:

    would an install complete on this host, and if not, what exactly is missing?

It never writes: no file, no unit, no udev reload, no sysfs write, no package
manager. Every probe is a read, a `command -v`, a read-only `--version`, or a
read-only journal/status query. The privileged steps are printed as copy-paste
commands (`fix:` lines) for the operator.

Usage:
    tools/oxp3-preflight.sh                 human report
    tools/oxp3-preflight.sh --json          machine-readable report
    tools/oxp3-preflight.sh --quiet         only problems + verdict
    tools/oxp3-preflight.sh --strict        exit 1 while any WARN remains
    tools/oxp3-preflight.sh --component NAME
                                            only one install path (repeatable,
                                            comma lists accepted)
    tools/oxp3-preflight.sh --selftest      parser checks + fixture verdicts
    tools/oxp3-preflight.sh --fixtures DIR  run a fixture directory
    tools/oxp3-preflight.sh --help

States:
    OK    the prerequisite is satisfied
    WARN  a degraded path the installer already handles (or a diagnostics-only
          tool), or a half-install that a re-run completes
    FAIL  a true blocker: an install would either fail or produce a component
          that cannot work at all

Verdict and exit code:
    safe    0   no FAIL (warnings are reported; --strict makes them exit 1)
    unsafe  1   at least one FAIL; the affected components would be skipped
    usage   2   unknown option / unknown component
    error   3   the probe engine could not start

See docs/PREFLIGHT.md for every check id, why it exists and its remediation.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob as globmod
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROG = "oxp3-preflight"
VERSION = "1.0.0"
SCHEMA = 1

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "preflight"

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

COMPONENTS = ("volume", "leds", "rgb", "sleep", "bluetooth", "power", "imu", "audio", "sched", "gfx", "xmx", "mango", "decky")
ROOT_COMPONENTS = ("volume", "leds", "rgb", "sleep", "bluetooth", "power", "imu", "sched", "gfx", "mango", "decky")
USER_COMPONENTS = ("leds", "rgb", "audio", "xmx")
DAEMON_COMPONENTS = ("volume", "leds", "rgb", "power")
HID_COMPONENTS = ("leds", "rgb")
UINPUT_COMPONENTS = ("volume", "leds")
EVDEV_COMPONENTS = ("volume",)
RAPL_COMPONENTS = ("power",)
IMU_COMPONENTS = ("imu",)
BT_COMPONENTS = ("bluetooth",)
AUDIO_COMPONENTS = ("audio",)
SLEEP_COMPONENTS = ("sleep",)
DECKY_COMPONENTS = ("decky",)
ALL = ("*",)

SYSTEMD_MIN = 247  # ProtectProc=invisible, used by the shipped units
HHD_MIN = (4, 1, 12)  # the version the volume-rocker patches are written for

# Files that make this checkout complete enough to install from.
REPO_REQUIRED = (
    "tools/oxp3-hw-install.sh",
    "tools/lib/install-common.sh",
    "tools/lib/decky_plugin.py",
    "tools/decky-oxp3-status/artifact.sha256",
    "tools/hhd-oxp3-voltap/install.sh",
    "tools/hhd-oxp3-voltap/oxp3_hhd_voltap/__init__.py",
    "tools/hhd-oxp3-voltap/pyproject.toml",
    "tools/oxp3-voltap/install.sh",
    "tools/oxp3-power-install.sh",
    "tools/oxp3-bt-firmware.sh",
    "tools/audio/install-user.sh",
    "tools/oxp3-hid.rules",
    "files/logind-oxp3.conf",
    "files/49-oxp3-suspend.rules",
    "files/oxp3-bmi160.conf",
)

# Install artefacts per component. 0 present = not installed; all = installed;
# anything between = a half-install the installer can complete.
COMPONENT_FILES = {
    # The supported volume path is the hhd plugin; the daemon artefacts are the
    # legacy alternative. A host with only the daemon is a "half-install" here,
    # which is accurate: re-running --volume installs the plugin (and the daemon
    # uninstall can follow).
    "volume": (
        "/usr/local/lib/python3.15/site-packages/oxp3_hhd_voltap",
        "/usr/local/libexec/oxp3-voltap",
        "/etc/systemd/system/oxp3-voltap.service",
        "/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb",
        "/etc/oxp3-voltap.conf",
    ),
    "leds": (
        "/etc/udev/rules.d/90-oxp3-hid.rules",
        "{home}/.config/systemd/user/oxp3-homebtn.service",
    ),
    "rgb": (
        "/etc/udev/rules.d/90-oxp3-hid.rules",
        "{home}/.config/oxp3-rgb.toml",
        "{home}/.config/systemd/user/oxp3-rgbd.service",
    ),
    "sleep": (
        "/usr/local/libexec/oxp3-sleep-wakeup",
        "/usr/local/libexec/oxp3-sleep-log",
        "/etc/systemd/system/oxp3-sleep-wakeup.service",
        "/etc/systemd/system/oxp3-sleep-pre.service",
        "/etc/systemd/system/oxp3-sleep-log.service",
        "/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules",
        "/etc/systemd/logind.conf.d/60-oxp3-sleep.conf",
        "/etc/polkit-1/rules.d/49-oxp3-suspend.rules",
    ),
    "bluetooth": (
        "/usr/lib/firmware/updates/intel/ibt-00a0-0291-iml.sfi",
        "/usr/lib/firmware/updates/intel/ibt-00a0-0291-pci.sfi",
        "/etc/dracut.conf.d/60-oxp3-bt-firmware.conf",
    ),
    "power": (
        "/usr/local/libexec/oxp3-powerctl",
        "/etc/systemd/system/oxp3-power-profile.service",
        "/etc/systemd/system/oxp3-power-resume.service",
    ),
    "imu": ("/etc/modprobe.d/oxp3-bmi160.conf",),
    "audio": ("{home}/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf",),
    "sched": (
        "/etc/systemd/system/oxp3-gaming-sched.service",
        "/usr/bin/scx_cosmos",
    ),
    "gfx": (
        "/etc/systemd/system/oxp3-mesa-keep.service",
        "/usr/local/libexec/oxp3-mesa-keep",
        "{home}/.cache/oxp3-fixes/known-good/host-ec418f04-libvulkan_intel.so",
    ),
    "xmx": (
        "{home}/.local/share/xess-xmx-release",
        "{home}/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so",
        "{home}/.config/xess-xmx",
    ),
    "mango": (
        "/usr/bin/mangohud",
        "/usr/bin/mangoapp",
    ),
    "decky": (
        "{home}/homebrew/plugins/oxp3-status/plugin.json",
        "{home}/homebrew/plugins/oxp3-status/.oxp3-managed",
    ),
}

BT_CACHE_FILES = ("ibt-00a0-0291-iml.sfi", "ibt-00a0-0291-pci.sfi")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    id: str
    group: str
    title: str
    state: str
    summary: str = ""
    remediation: str = ""
    components: Tuple[str, ...] = ALL

    def applies_to(self, selected: Sequence[str]) -> bool:
        if "*" in self.components:
            return True
        return any(c in self.components for c in selected)


def ok(cid: str, group: str, title: str, summary: str = "",
       components: Tuple[str, ...] = ALL) -> Check:
    return Check(cid, group, title, OK, summary, "", components)


def warn(cid: str, group: str, title: str, summary: str, remediation: str,
         components: Tuple[str, ...] = ALL) -> Check:
    return Check(cid, group, title, WARN, summary, remediation, components)


def fail(cid: str, group: str, title: str, summary: str, remediation: str,
         components: Tuple[str, ...] = ALL) -> Check:
    return Check(cid, group, title, FAIL, summary, remediation, components)


# ---------------------------------------------------------------------------
# hosts: real one, and the in-memory fixture double used by --selftest/tests
# ---------------------------------------------------------------------------
class Host:
    """Read-only view of one host, with every side effect behind an override."""

    def __init__(self, env: Optional[Dict[str, str]] = None) -> None:
        self.env = dict(env or os.environ)

    def _p(self, path: str) -> str:
        return path

    def exists(self, path: str) -> bool:
        try:
            return os.path.exists(self._p(path))
        except OSError:
            return False

    def isdir(self, path: str) -> bool:
        try:
            return os.path.isdir(self._p(path))
        except OSError:
            return False

    def read(self, path: str, limit: int = 1 << 20) -> Optional[str]:
        try:
            with open(self._p(path), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(limit)
        except (OSError, ValueError):
            return None

    def read_bytes(self, path: str, limit: int = 1 << 20) -> Optional[bytes]:
        try:
            with open(self._p(path), "rb") as fh:
                return fh.read(limit)
        except OSError:
            return None

    def glob(self, pattern: str) -> List[str]:
        try:
            return sorted(globmod.glob(self._p(pattern), recursive=True))
        except OSError:
            return []

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name, path=self.env.get("PATH", os.defpath))

    def run(self, argv: Sequence[str], timeout: int = 15) -> Tuple[int, str, str]:
        env = dict(self.env)
        env.setdefault("LC_ALL", "C")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        try:
            proc = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout,
                env=env, check=False,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError:
            return 127, "", "command not found"
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except OSError as exc:
            return 126, "", str(exc)


class FixtureHost(Host):
    """A host described by a JSON fixture. No process is ever started."""

    def __init__(self, data: Dict[str, object]) -> None:
        self.dirs = {str(p) for p in data.get("dirs", [])}
        self.files = {str(k): str(v) for k, v in (data.get("files") or {}).items()}
        self.tools = {str(t) for t in data.get("tools", [])}
        self.commands: List[Tuple[str, Dict[str, object]]] = [
            (str(k), dict(v)) for k, v in (data.get("commands") or {}).items()
        ]
        self.repo = str(data.get("repo") or "/repo")
        self.home = str(data.get("home") or "/home/tester")
        self.euid = int(data.get("euid", 0))
        self.user = str(data.get("user") or "tester")
        self.kernel = str(data.get("kernel") or "7.2.7-test.x86_64")
        self.removed = {str(p) for p in data.get("remove", [])}
        super().__init__({})
        self.files.setdefault("/proc/sys/kernel/osrelease", self.kernel + "\n")

    def exists(self, path: str) -> bool:
        if path in self.removed:
            return False
        if path in self.files or path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        if any(p.startswith(prefix) for p in list(self.files) + list(self.dirs)):
            return True
        # A fixture checkout is complete unless the fixture removes a file.
        if path.startswith(self.repo + "/"):
            return True
        return False

    def isdir(self, path: str) -> bool:
        if path in self.removed:
            return False
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self.dirs)

    def read(self, path: str, limit: int = 1 << 20) -> Optional[str]:
        if path in self.removed:
            return None
        if path in self.files:
            return self.files[path][:limit]
        if path.startswith(self.repo + "/"):
            return ""
        return None

    def read_bytes(self, path: str, limit: int = 1 << 20) -> Optional[bytes]:
        text = self.read(path, limit)
        return None if text is None else text.encode("utf-8", "replace")

    def glob(self, pattern: str) -> List[str]:
        allp = (set(self.files) | set(self.dirs)) - self.removed
        return sorted(p for p in allp if fnmatch.fnmatch(p, pattern))

    def which(self, name: str) -> Optional[str]:
        return "/usr/bin/" + name if name in self.tools else None

    def run(self, argv: Sequence[str], timeout: int = 15) -> Tuple[int, str, str]:
        key = " ".join(str(a) for a in argv)
        best: Optional[Tuple[str, Dict[str, object]]] = None
        for prefix, result in self.commands:
            if key == prefix or key.startswith(prefix):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, result)
        if best is None:
            return 127, "", "fixture: command not stubbed: " + key
        result = best[1]
        return (
            int(result.get("rc", 0)),
            str(result.get("out", "")),
            str(result.get("err", "")),
        )


# ---------------------------------------------------------------------------
# context and pure parsers (the units the --selftest pins)
# ---------------------------------------------------------------------------
def parse_passwd(text: Optional[str], user: str) -> Optional[Tuple[int, str]]:
    """Return (uid, home) for ``user`` from /etc/passwd text."""
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 6 and parts[0] == user:
            try:
                return int(parts[2]), parts[5]
            except ValueError:
                return None
    return None


def parse_systemd_version(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"systemd\s+([0-9]+)", text)
    return int(match.group(1)) if match else None


def parse_kernel_options(text: Optional[str]) -> Dict[str, str]:
    options: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and line.endswith(" is not set"):
            name = line[2:].split()[0]
            options[name] = "n"
            continue
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            options[key] = value.strip().strip('"')
    return options


def parse_unit_restart(text: str) -> List[Tuple[str, str]]:
    """Return (section, value) for every Restart= line in one unit."""
    section = ""
    found: List[Tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() == "Restart":
                found.append((section, value.strip()))
    return found


def parse_logind_options(texts: Iterable[str]) -> Dict[str, str]:
    """Effective Handle*= values from concatenated logind configuration.

    Commented assignments are compiled-in defaults and are only used when no
    uncommented file sets the key; uncommented later files win.
    """
    defaults: Dict[str, str] = {}
    current: Dict[str, str] = {}
    section = ""
    keys = {
        "HandlePowerKey", "HandleSuspendKey", "HandleLidSwitch",
        "HandleLidSwitchExternalPower", "HandleLidSwitchDocked",
        "HandleHibernateKey", "HandlePowerKeyLongPress",
    }
    for text in texts:
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section != "Login":
                continue
            commented = False
            if line.startswith("#"):
                commented = True
                line = line.lstrip("#").strip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key not in keys:
                continue
            if commented:
                defaults[key] = value
            else:
                current[key] = value
    result = dict(defaults)
    result.update(current)
    return result


def parse_hhd_version(text: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not text:
        return None
    match = re.search(r"([0-9]+)\.([0-9]+)\.([0-9]+)", text)
    if not match:
        return None
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def version_at_least(have: Optional[Tuple[int, int, int]],
                     want: Tuple[int, int, int]) -> bool:
    return bool(have) and have >= want


def stale_checkout_refs(text: str, repo: str, home: str) -> List[str]:
    """Absolute pnpm-style checkout paths in a file that are not this checkout."""
    text = text.replace("%h", home)
    refs: List[str] = []
    for match in re.finditer(r"(/[^\s\"'<>,;)]*oxp3-anatase-fixes)(?=/|\s|\"|'|$)", text):
        ref = match.group(1).rstrip("/")
        if os.path.realpath(ref) != os.path.realpath(repo):
            refs.append(ref)
    return sorted(set(refs))


def explain_verdict(checks: Sequence[Check], selected: Sequence[str],
                    strict: bool) -> Tuple[bool, int, List[str], List[str]]:
    """(safe, exit_code, skipped_components, warning_ids)."""
    fails = [c for c in checks if c.state == FAIL]
    warns = [c for c in checks if c.state == WARN]
    skipped = []
    for comp in selected:
        if any(c.state == FAIL and ("*" in c.components or comp in c.components)
               for c in checks):
            skipped.append(comp)
    safe = not fails
    if not safe:
        code = 1
    elif strict and warns:
        code = 1
    else:
        code = 0
    return safe, code, skipped, [c.id for c in warns]


class Context:
    def __init__(self, host: Host, user: str, home: str, repo: str,
                 euid: int, selected: Sequence[str], strict: bool) -> None:
        self.host = host
        self.user = user
        self.home = home
        self.repo = repo
        self.euid = euid
        self.selected = list(selected)
        self.strict = strict
        self._cache: Dict[str, object] = {}

    # -- thin host helpers --------------------------------------------------
    def exists(self, path: str) -> bool:
        return self.host.exists(path)

    def isdir(self, path: str) -> bool:
        return self.host.isdir(path)

    def read(self, path: str) -> Optional[str]:
        return self.host.read(path)

    def glob(self, pattern: str) -> List[str]:
        return self.host.glob(pattern)

    def which(self, name: str) -> Optional[str]:
        return self.host.which(name)

    def run(self, argv: Sequence[str], timeout: int = 15) -> Tuple[int, str, str]:
        return self.host.run(argv, timeout)

    def home_path(self, rel: str) -> str:
        return self.home.rstrip("/") + "/" + rel.lstrip("/")

    def cached(self, key: str, producer):
        if key not in self._cache:
            self._cache[key] = producer()
        return self._cache[key]


# ---------------------------------------------------------------------------
# shared probes
# ---------------------------------------------------------------------------
def systemd_version(ctx: Context) -> Optional[int]:
    def probe():
        for argv in (["systemctl", "--version"], ["systemd", "--version"]):
            rc, out, _err = ctx.run(argv)
            if rc == 0:
                version = parse_systemd_version(out)
                if version:
                    return version
        return None
    return ctx.cached("systemd-version", probe)


def kernel_release(ctx: Context) -> str:
    return (ctx.read("/proc/sys/kernel/osrelease") or "").strip()


def kernel_config(ctx: Context) -> Tuple[Optional[str], Dict[str, str]]:
    def probe():
        rel = kernel_release(ctx)
        candidates = ["/boot/config-" + rel, "/usr/lib/modules/" + rel + "/config"]
        candidates += ctx.glob("/usr/lib/modules/*/config")
        for path in candidates:
            text = ctx.read(path)
            if text:
                return path, parse_kernel_options(text)
        return None, {}
    return ctx.cached("kernel-config", probe)


def user_ids(ctx: Context) -> Tuple[Optional[int], str]:
    entry = parse_passwd(ctx.read("/etc/passwd"), ctx.user)
    if entry:
        return entry
    try:
        info = pwd.getpwnam(ctx.user)
        return info.pw_uid, info.pw_dir
    except (KeyError, OSError):
        return None, ctx.home


def find_vendor_hidraw(ctx: Context) -> Optional[str]:
    """The 1a86:fe00 vendor collection: HID_ID matches and it has no input child."""
    for device in sorted(ctx.glob("/sys/class/hidraw/hidraw*/device")):
        text = (ctx.read(os.path.join(device, "uevent")) or "").upper()
        if "HID_ID=0003:00001A86:0000FE00" not in text:
            continue
        if ctx.isdir(os.path.join(device, "input")):
            continue
        desc = ctx.host.read_bytes(os.path.join(device, "report_descriptor")) or b""
        if b"\x06\x00\xff" not in desc:
            # The node is still the vendor collection; the usage-page probe is
            # only used to prefer a node, exactly like tools/oxp3_hid.py.
            pass
        return "/dev/" + os.path.basename(os.path.dirname(device))
    return None


def hhd_probe(ctx: Context) -> Dict[str, object]:
    def probe():
        info: Dict[str, object] = {
            "version": None, "version_text": "", "active": "", "file_state": "",
            "pid": None, "volume_keyboard": False, "rgb": False,
            "patched": 0, "patches": 0, "grab": None,
        }
        if ctx.which("rpm"):
            rc, out, _ = ctx.run(["rpm", "-q", "--qf", "%{VERSION}", "hhd"])
            if rc == 0 and out.strip():
                info["version_text"] = out.strip()
                info["version"] = parse_hhd_version(out)
        if ctx.which("systemctl"):
            rc, out, _ = ctx.run(
                ["systemctl", "show", "-p", "ActiveState", "-p", "UnitFileState",
                 "-p", "MainPID", "hhd"]
            )
            if rc == 0:
                props = {}
                for line in out.splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        props[key.strip()] = value.strip()
                info["active"] = props.get("ActiveState", "")
                info["file_state"] = props.get("UnitFileState", "")
                try:
                    info["pid"] = int(props.get("MainPID", "0") or 0) or None
                except ValueError:
                    info["pid"] = None
        if ctx.which("journalctl"):
            rc, out, _ = ctx.run(
                ["journalctl", "-u", "hhd", "-b", "--no-pager", "-n", "5000"]
            )
            if rc == 0:
                info["volume_keyboard"] = "Handheld Daemon Volume Keyboard" in out
                info["rgb"] = bool(re.search(r"controller_rgb|LEDS\s+INFO", out))
        # The hhd package tree carries the patch target; git apply --check -R
        # is read-only and tells us whether the volume patch is already in.
        sites = []
        for pattern in ("/usr/lib/python3*/site-packages/hhd",
                        "/usr/lib64/python3*/site-packages/hhd",
                        "/usr/local/lib/python3*/site-packages/hhd"):
            sites.extend(ctx.glob(pattern))
        patches = sorted(ctx.glob(str(Path(ctx.repo) / "patches" / "hhd" / "*.patch")))
        info["patches"] = len(patches)
        if sites and patches and ctx.which("git"):
            site = sites[0]
            applied = 0
            for patch in patches:
                rc, _out, _err = ctx.run(
                    ["git", "-C", site, "apply", "--check", "-R", "-p1", patch]
                )
                if rc == 0:
                    applied += 1
            info["patched"] = applied
        # Can we see the process descriptors? Only root can, and the installer
        # runs as root; unprivileged runs report the check as unknown.
        pid = info.get("pid")
        if ctx.euid == 0 and isinstance(pid, int) and pid > 0:
            grabbed = False
            readable = False
            for fdinfo in ctx.glob("/proc/%d/fdinfo/*" % pid):
                readable = True
                text = ctx.read(fdinfo) or ""
                if re.search(r"^grab:\s*1", text, re.MULTILINE):
                    grabbed = True
            if readable:
                info["grab"] = grabbed
        return info
    return ctx.cached("hhd", probe)  # type: ignore[return-value]


def component_state(ctx: Context, component: str) -> Tuple[int, int, List[str]]:
    present: List[str] = []
    missing: List[str] = []
    for spec in COMPONENT_FILES[component]:
        path = spec.format(home=ctx.home)
        (present if ctx.exists(path) else missing).append(path)
    return len(present), len(present) + len(missing), missing


def bt_cache_present(ctx: Context) -> bool:
    cache = ctx.home_path(".cache/oxp3-bt-firmware")
    return all(ctx.exists(os.path.join(cache, name)) for name in BT_CACHE_FILES)


def bluetooth_journal_failure(ctx: Context) -> bool:
    if not ctx.which("journalctl"):
        return False
    rc, out, _ = ctx.run(["journalctl", "-k", "-b", "--no-pager"])
    return rc == 0 and "Failed to load Intel firmware file" in out


def stale_refs(ctx: Context) -> List[str]:
    refs: List[str] = []
    roots = [
        ctx.home_path(".local/share/flatpak/overrides"),
        "/var/lib/flatpak/overrides",
        ctx.home_path(".config/environment.d"),
        ctx.home_path(".config/systemd/user"),
        ctx.home_path(".config/xess-xmx"),
    ]
    for root in roots:
        for path in ctx.glob(root + "/**/*"):
            if not ctx.isdir(path):
                text = ctx.read(path)
                if text:
                    refs.extend(stale_checkout_refs(text, ctx.repo, ctx.home))
    return sorted(set(refs))


# ---------------------------------------------------------------------------
# checks, in output order
# ---------------------------------------------------------------------------
def check_core(ctx: Context) -> List[Check]:
    out: List[Check] = []

    if ctx.euid == 0:
        out.append(ok("core.root", "core", "root privilege",
                      "running as root; /etc, /usr/local and udev are writable"))
    else:
        out.append(warn(
            "core.root", "core", "root privilege",
            "running as user %s (uid %d), not root" % (ctx.user, ctx.euid),
            "sudo tools/oxp3-hw-install.sh <components>   "
            "(the installer re-runs this check as root)",
            ROOT_COMPONENTS,
        ))

    entry = parse_passwd(ctx.read("/etc/passwd"), ctx.user)
    if ctx.user == "root" or entry is None:
        out.append(fail(
            "core.user", "core", "unprivileged target user",
            "no non-root user to own the user-scope units (audio, leds, rgb): %r"
            % ctx.user,
            "run the installer with sudo from your desktop session, or pass --user NAME",
            USER_COMPONENTS + DECKY_COMPONENTS,
        ))
    else:
        out.append(ok("core.user", "core", "unprivileged target user",
                      "%s (uid %d, home %s)" % (ctx.user, entry[0], entry[1]),
                      USER_COMPONENTS + DECKY_COMPONENTS))

    if re.search(r"\s", ctx.repo):
        out.append(fail(
            "core.repo-path", "core", "checkout path",
            "the checkout path contains whitespace: %s" % ctx.repo,
            "move the checkout to a path without spaces "
            "(systemd ExecStart in the user units is unquoted)",
            USER_COMPONENTS,
        ))
    else:
        out.append(ok("core.repo-path", "core", "checkout path",
                      ctx.repo + " (no whitespace)", USER_COMPONENTS))

    missing = [rel for rel in REPO_REQUIRED if not ctx.exists(os.path.join(ctx.repo, rel))]
    if missing:
        out.append(fail(
            "core.repo", "core", "checkout completeness",
            "missing %d required file(s): %s" % (len(missing), ", ".join(missing[:5])),
            "re-clone or restore the checkout: git -C %s status" % ctx.repo,
        ))
    else:
        out.append(ok("core.repo", "core", "checkout completeness",
                      "%d required files present" % len(REPO_REQUIRED)))

    if ctx.euid == 0 or ctx.which("sudo"):
        out.append(ok("core.sudo", "core", "privilege escalation",
                      "root" if ctx.euid == 0 else "sudo available"))
    else:
        out.append(warn(
            "core.sudo", "core", "privilege escalation",
            "not root and no sudo on PATH",
            "run as root (su -) or install sudo; the hardware components write to /etc",
            ROOT_COMPONENTS,
        ))
    return out


def check_dirs(ctx: Context) -> List[Check]:
    specs = (
        ("dir.system-units", "/etc/systemd/system", "system units", FAIL,
         ("volume", "leds", "sleep", "power")),
        ("dir.udev-rules", "/etc/udev/rules.d", "udev rules", WARN,
         ("volume", "leds", "rgb", "sleep")),
        ("dir.udev-hwdb", "/etc/udev/hwdb.d", "udev hwdb", WARN, ("volume",)),
        ("dir.polkit-rules", "/etc/polkit-1/rules.d", "polkit rules", WARN,
         ("sleep",)),
        ("dir.logind-conf-d", "/etc/systemd/logind.conf.d", "logind drop-ins",
         WARN, ("sleep",)),
        ("dir.local-libexec", "/usr/local/libexec", "local helpers", WARN,
         ("volume", "sleep", "power")),
        ("dir.dracut-conf-d", "/etc/dracut.conf.d", "dracut drop-ins", WARN,
         ("bluetooth",)),
        ("dir.modprobe-d", "/etc/modprobe.d", "modprobe drop-ins", WARN, ("imu",)),
        ("dir.user-systemd", ctx.home_path(".config/systemd/user"),
         "user units", WARN, ("leds", "rgb")),
        ("dir.pipewire", ctx.home_path(".config/pipewire/pipewire.conf.d"),
         "PipeWire drop-ins", WARN, ("audio",)),
    )
    out: List[Check] = []
    for cid, path, what, state, components in specs:
        if ctx.isdir(path):
            out.append(ok(cid, "dir", what, path, components))
        elif state == FAIL:
            out.append(fail(
                cid, "dir", what, "%s is missing" % path,
                "create it: sudo install -d -m 0755 %s   "
                "(and check systemd is installed)" % path, components,
            ))
        else:
            out.append(warn(
                cid, "dir", what, "%s is missing" % path,
                "create it: sudo install -d -m 0755 %s   "
                "(the installer would create it, but a pre-created directory "
                "keeps the half-install surface small)" % path, components,
            ))
    return out


def check_systemd(ctx: Context) -> List[Check]:
    out: List[Check] = []
    version = systemd_version(ctx)
    if not ctx.which("systemctl") or version is None:
        out.append(fail(
            "systemd.present", "systemd", "systemd",
            "systemctl is missing or its version cannot be read",
            "this host is not systemd-managed; the units cannot be installed",
        ))
        out.append(fail(
            "systemd.version", "systemd", "systemd version",
            "unknown (systemd not detected)",
            "install systemd >= %d" % SYSTEMD_MIN,
        ))
    else:
        out.append(ok("systemd.present", "systemd", "systemd",
                      "systemd %d detected" % version))
        if version < SYSTEMD_MIN:
            out.append(fail(
                "systemd.version", "systemd", "systemd version",
                "systemd %d is older than %d" % (version, SYSTEMD_MIN),
                "the units use ProtectProc=invisible (systemd >= 247); "
                "update the OS or install by hand without sandboxing options",
            ))
        else:
            out.append(ok("systemd.version", "systemd", "systemd version",
                          "systemd %d (>= %d)" % (version, SYSTEMD_MIN)))

    if ctx.which("systemd-analyze"):
        out.append(ok("systemd.analyze", "systemd", "systemd-analyze",
                      "present (unit verification and cat-config available)"))
    else:
        out.append(warn(
            "systemd.analyze", "systemd", "systemd-analyze",
            "systemd-analyze is missing",
            "install the systemd package tools; tools/oxp3-verify.sh and the "
            "documented unit checks use it",
        ))

    uid, _home = user_ids(ctx)
    bus = "/run/user/%s/bus" % uid if uid is not None else ""
    if bus and ctx.exists(bus):
        out.append(ok("systemd.user-bus", "systemd", "user manager bus",
                      "%s reachable" % bus, USER_COMPONENTS))
    else:
        out.append(warn(
            "systemd.user-bus", "systemd", "user manager bus",
            "no user manager bus at %s" % (bus or "<unknown uid>"),
            "log in on the device desktop (not SSH/root) before installing "
            "leds/rgb/audio; the installer still writes the units and they "
            "start at next login", USER_COMPONENTS,
        ))

    units = []
    for pattern in ("files/*.service", "tools/*.service",
                    "tools/oxp3-voltap/*.service"):
        units.extend(ctx.glob(os.path.join(ctx.repo, pattern)))
    misplaced: List[str] = []
    in_service = 0
    for unit in units:
        text = ctx.read(unit) or ""
        for section, value in parse_unit_restart(text):
            if section != "Service":
                misplaced.append("%s: Restart=%s in [%s]" % (
                    os.path.basename(unit), value, section or "?"))
            else:
                in_service += 1
    if misplaced:
        out.append(fail(
            "systemd.restart", "systemd", "Restart= support",
            "; ".join(misplaced[:4]),
            "move Restart=/RestartSec= into [Service]; systemd ignores them in [Unit]",
        ))
    elif in_service == 0:
        out.append(warn(
            "systemd.restart", "systemd", "Restart= support",
            "no shipped unit declares Restart= in [Service]",
            "check the checkout: git -C %s status" % ctx.repo,
        ))
    else:
        out.append(ok("systemd.restart", "systemd", "Restart= support",
                      "%d Restart= directives in [Service] (supported by systemd %s)"
                      % (in_service, version if version else "?")))
    return out


def check_tools(ctx: Context) -> List[Check]:
    out: List[Check] = []

    def require(cid, title, tool, components, fix, group="tool"):
        if ctx.which(tool):
            return ok(cid, group, title, tool + " present", components)
        return fail(cid, group, title, tool + " is missing", fix, components)

    out.append(require("tool.install", "coreutils install", "install", ALL,
                       "install coreutils (the installer copies every file with install(1))"))
    out.append(require("tool.systemctl", "systemctl", "systemctl", ALL,
                       "install systemd"))
    out.append(require("tool.udevadm", "udevadm", "udevadm",
                       ("volume",) + HID_COMPONENTS + SLEEP_COMPONENTS,
                       "install systemd-udev; rules cannot be reloaded by hand"))
    out.append(require("tool.systemd-hwdb", "systemd-hwdb", "systemd-hwdb",
                       ("volume",), "install systemd; the volume hwdb must compile"))
    out.append(require("tool.python3", "python3", "python3",
                       DAEMON_COMPONENTS + DECKY_COMPONENTS,
                       "install python3 (the daemons, oxp3-powerctl and the Decky installer helper are Python)"))
    if ctx.euid == 0:
        out.append(require("tool.runuser", "runuser", "runuser", USER_COMPONENTS,
                           "install util-linux (root uses runuser for the "
                           "user-scope half)"))
    else:
        out.append(ok("tool.runuser", "tool", "runuser",
                      "not needed as the invoking user; the installer uses it "
                      "when run as root", USER_COMPONENTS))

    if ctx.which("dracut") or ctx.which("rpm-ostree"):
        tool = "dracut" if ctx.which("dracut") else "rpm-ostree"
        out.append(ok("tool.dracut", "tool", "initramfs tool", tool + " present",
                      BT_COMPONENTS))
    else:
        out.append(fail(
            "tool.dracut", "tool", "initramfs tool",
            "neither dracut nor rpm-ostree is available",
            "install dracut (or use the rpm-ostree host path); the Bluetooth "
            "firmware only reaches the initramfs through it", BT_COMPONENTS,
        ))

    if ctx.which("curl") or ctx.which("wget"):
        out.append(ok("tool.downloader", "tool", "firmware downloader",
                      "curl/wget present", BT_COMPONENTS))
    elif bt_cache_present(ctx):
        out.append(ok("tool.downloader", "tool", "firmware downloader",
                      "cached firmware present in ~/.cache/oxp3-bt-firmware",
                      BT_COMPONENTS))
    else:
        out.append(fail(
            "tool.downloader", "tool", "firmware downloader",
            "no curl/wget and no cached Bluetooth firmware",
            "install curl, or download both files first and pass "
            "--bt-from DIR to tools/oxp3-hw-install.sh", BT_COMPONENTS,
        ))

    if ctx.which("python3"):
        rc, _out, _err = ctx.run(["python3", "-c", "import evdev"])
        if rc == 0:
            out.append(ok("tool.evdev", "tool", "python3-evdev",
                          "import evdev succeeds", EVDEV_COMPONENTS))
        else:
            out.append(fail(
                "tool.evdev", "tool", "python3-evdev",
                "python3 -c 'import evdev' failed",
                "sudo rpm-ostree install python3-evdev   "
                "(or dnf install python3-evdev)", EVDEV_COMPONENTS,
            ))
        rc, _out, _err = ctx.run(["python3", "-c", "import tomllib"])
        if rc == 0:
            out.append(ok("tool.tomllib", "tool", "Python TOML parser",
                          "tomllib available", ("rgb",)))
        else:
            out.append(fail(
                "tool.tomllib", "tool", "Python TOML parser",
                "tomllib is missing (Python < 3.11)",
                "install Python >= 3.11; oxp3-rgbd.py cannot parse "
                "~/.config/oxp3-rgb.toml otherwise", ("rgb",),
            ))
    else:
        out.append(fail("tool.evdev", "tool", "python3-evdev",
                        "python3 is missing", "install python3-evdev", EVDEV_COMPONENTS))
        out.append(fail("tool.tomllib", "tool", "Python TOML parser",
                        "python3 is missing", "install Python >= 3.11", ("rgb",)))

    for cid, title, tool, components, fix in (
        ("tool.pipewire", "PipeWire", "pipewire", AUDIO_COMPONENTS,
         "install pipewire; the EQ drop-in needs a running PipeWire"),
        ("tool.wireplumber", "WirePlumber", "wireplumber", AUDIO_COMPONENTS,
         "install wireplumber; it loads the filter-chain sink"),
        ("tool.pactl", "pactl", "pactl", AUDIO_COMPONENTS,
         "install pulseaudio-utils; tools/audio/install-user.sh uses pactl to find the speaker"),
        ("tool.kreadconfig6", "kreadconfig6", "kreadconfig6", ALL,
         "install kf6-kconfig; the first-run collector reads PowerDevil with it"),
        ("tool.btmgmt", "btmgmt", "btmgmt", BT_COMPONENTS,
         "install bluez; only the post-install controller verification uses it"),
        ("tool.flatpak", "flatpak", "flatpak", ALL,
         "install flatpak; only the Steam Flatpak XMX path uses it"),
        ("tool.modprobe", "modprobe", "modprobe", IMU_COMPONENTS,
         "install kmod; the IMU blacklist is applied at boot by the kernel"),
    ):
        if ctx.which(tool):
            out.append(ok(cid, "tool", title, tool + " present", components))
        else:
            out.append(warn(cid, "tool", title, tool + " is missing", fix, components))

    # A polkit agent is not needed for the shipped local-seat rule, but without
    # one every other suspend caller (Gamemode, a second session) still fails.
    agent = None
    if ctx.which("pgrep"):
        rc, out_text, _err = ctx.run(
            ["pgrep", "-af", "polkit.*authentication-agent|polkit-agent"]
        )
        if rc == 0 and out_text.strip():
            agent = out_text.strip().splitlines()[0]
    if agent:
        out.append(ok("tool.polkit-agent", "tool", "polkit agent",
                      agent[:120], SLEEP_COMPONENTS))
    else:
        out.append(warn(
            "tool.polkit-agent", "tool", "polkit agent",
            "no polkit authentication agent is running in this session",
            "install/start polkit-kde-authentication-agent-1 (Plasma) for "
            "non-local callers; the shipped 49-oxp3-suspend.rules already "
            "covers the local active wheel seat", SLEEP_COMPONENTS,
        ))
    return out


def check_devices(ctx: Context) -> List[Check]:
    out: List[Check] = []

    hidraw = ctx.glob("/dev/hidraw*")
    if hidraw:
        out.append(ok("dev.hidraw", "dev", "hidraw nodes",
                      "%d node(s): %s" % (len(hidraw), " ".join(
                          os.path.basename(p) for p in hidraw[:5])), HID_COMPONENTS))
    else:
        out.append(warn(
            "dev.hidraw", "dev", "hidraw nodes", "no /dev/hidraw* node",
            "check the USB device (lsusb -d 1a86:fe00); the udev rule installs "
            "anyway and applies when it appears", HID_COMPONENTS,
        ))

    vendor = find_vendor_hidraw(ctx)
    if vendor:
        writable = os.access(vendor, os.W_OK)
        out.append(ok(
            "dev.vendor-hid", "dev", "vendor HID collection",
            "%s (1a86:fe00, usage page 0xFF00)%s" % (
                vendor, "" if writable else "; root-only until 90-oxp3-hid.rules"),
            HID_COMPONENTS,
        ))
    else:
        out.append(warn(
            "dev.vendor-hid", "dev", "vendor HID collection",
            "no 1a86:fe00 hidraw node without an input child",
            "the vendor RGB/home-button interface is not attached; the rule and "
            "daemons install anyway and pick it up when present", HID_COMPONENTS,
        ))

    events = ctx.glob("/dev/input/event*")
    if events:
        out.append(ok("dev.input-events", "dev", "input event devices",
                      "%d node(s)" % len(events), EVDEV_COMPONENTS))
    else:
        # The supported volume path (the hhd plugin) opens no event node; only
        # the legacy daemon needs one, so this is a WARN, not a blocker.
        out.append(warn(
            "dev.input-events", "dev", "input event devices",
            "no /dev/input/event* node",
            "check CONFIG_INPUT_EVDEV and udev; only the LEGACY oxp3-voltap "
            "daemon reads event nodes - the hhd plugin needs none",
            EVDEV_COMPONENTS,
        ))

    if ctx.exists("/dev/uinput"):
        out.append(ok("dev.uinput", "dev", "uinput", "/dev/uinput present",
                      UINPUT_COMPONENTS))
    else:
        out.append(fail(
            "dev.uinput", "dev", "uinput", "/dev/uinput is missing",
            "sudo modprobe uinput   (and CONFIG_INPUT_UINPUT); the home-button "
            "keyboard creates a uinput device", ("leds",),
        ))
        out.append(warn(
            "dev.uinput-legacy", "dev", "uinput for the legacy volume daemon",
            "/dev/uinput is missing",
            "only the LEGACY oxp3-voltap shim needs it; the hhd plugin needs "
            "neither /dev/uinput nor an event node", ("volume",),
        ))

    rapl = [p for p in ctx.glob("/sys/class/powercap/intel-rapl:*")
            if re.search(r"intel-rapl:*[0-9]+$", p)]
    if rapl:
        out.append(ok("dev.rapl", "dev", "RAPL power zones",
                      "%d zone(s)" % len(rapl), RAPL_COMPONENTS))
    else:
        out.append(fail(
            "dev.rapl", "dev", "RAPL power zones",
            "no /sys/class/powercap/intel-rapl:<n> zone",
            "check CONFIG_INTEL_RAPL / the BIOS; the power envelope and fan "
            "units have nothing to write", RAPL_COMPONENTS,
        ))

    if ctx.glob("/sys/devices/platform/oxp-platform/hwmon/hwmon*"):
        out.append(ok("dev.hwmon-oxp", "dev", "OXP EC hwmon",
                      "oxp-platform hwmon present", RAPL_COMPONENTS))
    else:
        out.append(warn(
            "dev.hwmon-oxp", "dev", "OXP EC hwmon",
            "no oxp-platform hwmon node",
            "the fan curve is opt-in and needs the oxp_ec driver; the power "
            "envelope units still work", RAPL_COMPONENTS,
        ))

    battery = ctx.glob("/sys/class/power_supply/BAT*/charge_control_end_threshold")
    if battery:
        out.append(ok("dev.battery", "dev", "battery care",
                      "%d charge-limit interface(s)" % len(battery), RAPL_COMPONENTS))
    else:
        out.append(warn(
            "dev.battery", "dev", "battery care",
            "no charge_control_end_threshold under /sys/class/power_supply/BAT*",
            "battery limit/bypass is optional; check the ACPI battery driver",
            RAPL_COMPONENTS,
        ))

    state_path = "/sys/kernel/sched_ext/state"
    if ctx.exists(state_path):
        state = (ctx.read(state_path) or "").strip()
        out.append(ok("dev.sched-ext", "dev", "sched_ext",
                      "available (state=%s)" % (state or "?")))
    else:
        out.append(warn(
            "dev.sched-ext", "dev", "sched_ext",
            "/sys/kernel/sched_ext/state is absent",
            "the gaming scheduler is opt-in; check CONFIG_SCHED_CLASS_EXT and "
            "the kernel command line",
        ))
    return out


def check_kernel(ctx: Context) -> List[Check]:
    path, options = kernel_config(ctx)
    out: List[Check] = []
    if not path:
        out.append(warn(
            "kernel.config", "kernel", "kernel configuration",
            "no readable /boot/config-* or /usr/lib/modules/*/config",
            "install the kernel-core package (or mount /boot); kernel features "
            "cannot be asserted without it",
        ))
        for cid, title, components in (
            ("kernel.hidraw", "HIDRAW", HID_COMPONENTS),
            ("kernel.uinput", "INPUT_UINPUT", UINPUT_COMPONENTS),
            ("kernel.rapl", "INTEL_RAPL", RAPL_COMPONENTS),
            ("kernel.bmi260", "BMI260", IMU_COMPONENTS),
            ("kernel.sched-ext", "SCHED_CLASS_EXT", ALL),
            ("kernel.btintel", "BT_INTEL_PCIE", BT_COMPONENTS),
        ):
            out.append(ok(cid, "kernel", title,
                          "not asserted (no kernel config)", components))
    else:
        out.append(ok("kernel.config", "kernel", "kernel configuration",
                      path))

        def module_check(cid, title, names, components, missing_state, fix):
            values = [options.get(n, "n") for n in names]
            if any(v in ("y", "m") for v in values):
                return ok(cid, "kernel", title,
                          "%s" % ", ".join("%s=%s" % (n, options.get(n, "n"))
                                           for n in names), components)
            return Check(cid, "kernel", title, missing_state,
                         "%s not enabled" % "/".join(names), fix, components)

        out.append(module_check(
            "kernel.hidraw", "HIDRAW", ("CONFIG_HIDRAW",), HID_COMPONENTS, FAIL,
            "enable CONFIG_HIDRAW in the kernel; the vendor HID interface is "
            "hidraw-only"))
        out.append(module_check(
            "kernel.uinput", "INPUT_UINPUT", ("CONFIG_INPUT_UINPUT",),
            ("leds",), FAIL,
            "enable CONFIG_INPUT_UINPUT; the home-button keyboard creates a "
            "uinput device"))
        out.append(module_check(
            "kernel.uinput-legacy", "INPUT_UINPUT", ("CONFIG_INPUT_UINPUT",),
            ("volume",), WARN,
            "only the LEGACY oxp3-voltap shim needs uinput; the hhd plugin "
            "opens no device"))
        out.append(module_check(
            "kernel.rapl", "INTEL_RAPL",
            ("CONFIG_INTEL_RAPL", "CONFIG_INTEL_RAPL_CORE"), RAPL_COMPONENTS,
            FAIL, "enable CONFIG_INTEL_RAPL (and CORE); the power envelope has "
                  "no interface otherwise"))
        out.append(module_check(
            "kernel.bmi260", "BMI260", ("CONFIG_BMI260",), IMU_COMPONENTS, WARN,
            "the Anatase kernel ships CONFIG_BMI260; without it bmi160_i2c "
            "misdetects the IMU"))
        out.append(module_check(
            "kernel.sched-ext", "SCHED_CLASS_EXT",
            ("CONFIG_SCHED_CLASS_EXT",), (), WARN,
            "sched_ext is opt-in; enable CONFIG_SCHED_CLASS_EXT to use scx_cosmos"))
        out.append(module_check(
            "kernel.btintel", "BT_INTEL_PCIE", ("CONFIG_BT_INTEL_PCIE",),
            BT_COMPONENTS, WARN,
            "the Bluetooth firmware needs btintel_pcie; check the kernel config"))
    return out


def check_state(ctx: Context) -> List[Check]:
    out: List[Check] = []
    for comp in COMPONENTS:
        present, total, missing = component_state(ctx, comp)
        if present == 0:
            out.append(ok("state." + comp, "state", comp + " install state",
                          "not installed (0/%d artefacts)" % total, (comp,)))
        elif present == total:
            out.append(ok("state." + comp, "state", comp + " install state",
                          "installed (%d/%d artefacts)" % (present, total), (comp,)))
        else:
            out.append(warn(
                "state." + comp, "state", comp + " install state",
                "half-install: %d/%d artefacts; missing %s"
                % (present, total, ", ".join(os.path.basename(p) for p in missing[:4])),
                "re-run the installer to converge: "
                "sudo tools/oxp3-hw-install.sh --%s" % comp, (comp,),
            ))

    fw = [p for p in COMPONENT_FILES["bluetooth"][:2] if ctx.exists(p)]
    dracut = ctx.exists(COMPONENT_FILES["bluetooth"][2])
    journal_fail = bluetooth_journal_failure(ctx)
    if fw and not dracut:
        out.append(warn(
            "state.bt-initramfs", "state", "Bluetooth initramfs",
            "firmware present (%d/2) but /etc/dracut.conf.d/60-oxp3-bt-firmware.conf "
            "is missing; this boot's initramfs predates it%s"
            % (len(fw), " (kernel logged a firmware load failure)" if journal_fail else ""),
            "sudo tools/oxp3-bt-firmware.sh   # writes the drop-in, then reboot",
            BT_COMPONENTS,
        ))
    elif journal_fail:
        out.append(warn(
            "state.bt-initramfs", "state", "Bluetooth initramfs",
            "the drop-in is installed but this boot still reports "
            "'Failed to load Intel firmware file'",
            "reboot to regenerate the initramfs (rpm-ostree deployments), or "
            "reload btintel_pcie: sudo tools/oxp3-bt-firmware.sh --no-initramfs",
            BT_COMPONENTS,
        ))
    elif not fw:
        out.append(ok("state.bt-initramfs", "state", "Bluetooth initramfs",
                      "firmware not installed yet; nothing stale", BT_COMPONENTS))
    else:
        out.append(ok("state.bt-initramfs", "state", "Bluetooth initramfs",
                      "firmware + dracut drop-in present, no load failure this boot",
                      BT_COMPONENTS))

    if ctx.which("rpm-ostree"):
        rc, text, _err = ctx.run(["rpm-ostree", "status", "--json"], timeout=20)
        staged = False
        try:
            data = json.loads(text) if rc == 0 and text.strip() else {}
            staged = any(d.get("staged") for d in data.get("deployments", []))
        except (ValueError, AttributeError, TypeError):
            staged = False
        if staged:
            out.append(warn(
                "state.ostree-staged", "state", "rpm-ostree deployment",
                "a deployment is staged (update or pending change)",
                "reboot into it before installing; /usr overlay writes would be "
                "lost, and hardware fixes should be re-asserted afterwards",
            ))
        else:
            out.append(ok("state.ostree-staged", "state", "rpm-ostree deployment",
                          "no staged deployment"))
    else:
        out.append(ok("state.ostree-staged", "state", "rpm-ostree deployment",
                      "not an rpm-ostree host"))
    return out


def check_conflicts(ctx: Context) -> List[Check]:
    out: List[Check] = []
    info = hhd_probe(ctx)
    active = bool(info.get("active") == "active")
    version = info.get("version")
    version_text = str(info.get("version_text") or "")
    details = []
    if info.get("volume_keyboard"):
        details.append("volume keyboard")
    if info.get("rgb"):
        details.append("RGB plugin")
    if info.get("grab") is True:
        details.append("grabs an input device")
    elif info.get("grab") is None and active:
        details.append("grab state unknown without root")

    if active:
        out.append(ok("conflict.hhd", "conflict", "HHD",
                      "active %s%s" % (version_text or "",
                                       (" (" + ", ".join(details) + ")") if details else ""),
                      ("volume", "imu", "rgb")))
    elif info.get("active") in ("inactive", "failed"):
        out.append(warn(
            "conflict.hhd", "conflict", "HHD",
            "hhd is %s" % info.get("active"),
            "start hhd for the volume-rocker/IMU path: sudo systemctl enable --now hhd",
            ("volume", "imu", "rgb"),
        ))
    else:
        out.append(warn(
            "conflict.hhd", "conflict", "HHD",
            "hhd is not installed; the volume-rocker and IMU paths are degraded",
            "install hhd (the repository's driver stack or rpm-ostree) or use "
            "the standalone oxp3-voltap path", ("volume", "imu", "rgb"),
        ))

    if version_at_least(version, HHD_MIN):
        out.append(ok("conflict.hhd-version", "conflict", "HHD version",
                      version_text + " (>= 4.1.12)", ("volume", "imu")))
    elif version:
        out.append(warn(
            "conflict.hhd-version", "conflict", "HHD version",
            "%s is older than 4.1.12" % version_text,
            "the hhd volume patches are written for 4.1.12; update hhd or use "
            "the standalone oxp3-voltap daemon", ("volume", "imu"),
        ))
    else:
        out.append(warn(
            "conflict.hhd-version", "conflict", "HHD version",
            "version unreadable",
            "rpm -q hhd   # the volume patches need 4.1.12", ("volume", "imu"),
        ))

    patched = int(info.get("patched") or 0)
    patches = int(info.get("patches") or 0)
    voltap = (ctx.exists("/usr/local/libexec/oxp3-voltap") or
              ctx.exists("/etc/systemd/system/oxp3-voltap.service"))
    plugin = ctx.exists("/usr/local/lib/python3.15/site-packages/"
                        "oxp3_hhd_voltap")
    if patches and patched == patches:
        if plugin:
            out.append(ok(
                "conflict.volume-path", "conflict", "volume implementation",
                "hhd plugin + hhd patch applied; the plugin supersedes the "
                "patch's tap policy per producer, so the key is normalised once",
                ("volume",)))
        elif voltap:
            out.append(warn(
                "conflict.volume-path", "conflict", "volume implementation",
                "the hhd volume patch is applied AND the legacy oxp3-voltap "
                "daemon is installed; with hhd running the daemon only observes",
                "the supported path is the plugin "
                "(sudo tools/hhd-oxp3-voltap/install.sh install); otherwise "
                "remove the unused one", ("volume",)))
        else:
            out.append(ok(
                "conflict.volume-path", "conflict", "volume implementation",
                "the hhd volume-rocker patch is applied; no plugin/daemon",
                "the supported upgrade is the plugin: sudo tools/oxp3-hw-install.sh --volume",
                ("volume",)))
    elif plugin:
        out.append(ok("conflict.volume-path", "conflict", "volume implementation",
                      "hhd plugin installed (supported path)", ("volume",)))
    elif voltap:
        out.append(ok("conflict.volume-path", "conflict", "volume implementation",
                      "legacy oxp3-voltap installed; hhd patch not applied "
                      "(the plugin is the supported path)", ("volume",)))
    else:
        out.append(ok("conflict.volume-path", "conflict", "volume implementation",
                      "no volume implementation installed yet", ("volume",)))

    texts = [ctx.read("/etc/systemd/logind.conf") or ""]
    for path in sorted(ctx.glob("/etc/systemd/logind.conf.d/*.conf")):
        texts.append(ctx.read(path) or "")
    effective = parse_logind_options(texts)
    ours = ctx.exists("/etc/systemd/logind.conf.d/60-oxp3-sleep.conf")
    other = [p for p in ctx.glob("/etc/systemd/logind.conf.d/*.conf")
             if not p.endswith("60-oxp3-sleep.conf")]
    if ours and effective.get("HandlePowerKey") != "suspend":
        out.append(warn(
            "conflict.logind", "conflict", "logind policy",
            "our drop-in is installed but the effective HandlePowerKey is %r"
            % effective.get("HandlePowerKey", "unset"),
            "check for a later drop-in overriding 60-oxp3-sleep.conf: "
            "systemd-analyze cat-config systemd/logind.conf", SLEEP_COMPONENTS,
        ))
    elif not ours and other:
        out.append(warn(
            "conflict.logind", "conflict", "logind policy",
            "another logind drop-in already configures Handle*= (%s)"
            % ", ".join(os.path.basename(p) for p in other),
            "--sleep adds 60-oxp3-sleep.conf; review both files and keep one "
            "policy, then restart systemd-logind", SLEEP_COMPONENTS,
        ))
    else:
        out.append(ok("conflict.logind", "conflict", "logind policy",
                      "no conflicting policy (power key default: %s)"
                      % effective.get("HandlePowerKey", "compiled default"),
                      SLEEP_COMPONENTS))

    rule = "/etc/polkit-1/rules.d/49-oxp3-suspend.rules"
    local_rule = os.path.join(ctx.repo, "files", "49-oxp3-suspend.rules")
    if ctx.exists(rule):
        installed = ctx.read(rule) or ""
        wanted = ctx.read(local_rule) or ""
        if wanted and installed.strip() != wanted.strip():
            out.append(warn(
                "conflict.polkit", "conflict", "polkit rule",
                "%s exists with different content" % rule,
                "compare and keep the intended rule: sudo diff -u %s %s   "
                "(the installer overwrites it)" % (local_rule, rule),
                SLEEP_COMPONENTS,
            ))
        else:
            out.append(ok("conflict.polkit", "conflict", "polkit rule",
                          "%s matches the repository copy" % rule, SLEEP_COMPONENTS))
    else:
        out.append(ok("conflict.polkit", "conflict", "polkit rule",
                      "no existing %s" % rule, SLEEP_COMPONENTS))

    refs = stale_refs(ctx)
    if refs:
        out.append(warn(
            "conflict.flatpak", "conflict", "stale checkout references",
            "%d override/env/user-unit reference(s) point at another checkout: %s"
            % (len(refs), ", ".join(refs[:3])),
            "re-run the installers from this checkout (they rewrite the user "
            "units and XMX env), or update the files by hand",
        ))
    else:
        out.append(ok("conflict.flatpak", "conflict", "stale checkout references",
                      "no override, environment.d or user unit points at another checkout"))

    stale_units = []
    for path in ctx.glob(ctx.home_path(".config/systemd/user/oxp3-*.service")):
        text = ctx.read(path) or ""
        for ref in stale_checkout_refs(text, ctx.repo, ctx.home):
            stale_units.append("%s -> %s" % (os.path.basename(path), ref))
    if stale_units:
        out.append(warn(
            "conflict.stale-units", "conflict", "installed user units",
            "; ".join(stale_units[:3]),
            "re-run: sudo tools/oxp3-hw-install.sh --leds --rgb   "
            "(the units are rendered for the current checkout path)",
            ("leds", "rgb"),
        ))
    else:
        out.append(ok("conflict.stale-units", "conflict", "installed user units",
                      "no installed oxp3 user unit points at another checkout",
                      ("leds", "rgb")))

    legacy = ctx.glob("/etc/udev/rules.d/70-*rapl.rules")
    if legacy:
        out.append(warn(
            "conflict.rapl-rule", "conflict", "legacy RAPL udev rule",
            "old rule still present: %s" % ", ".join(legacy),
            "remove it and restore root-only counters: "
            "sudo ./oxp3-fixes-fast.sh install (the driver installer removes it)",
            RAPL_COMPONENTS,
        ))
    else:
        out.append(ok("conflict.rapl-rule", "conflict", "legacy RAPL udev rule",
                      "no 70-*rapl.rules present", RAPL_COMPONENTS))
    return out


CHECK_FUNCS = (
    check_core, check_dirs, check_systemd, check_tools, check_devices,
    check_kernel, check_state, check_conflicts,
)


def run_checks(ctx: Context, only: Optional[str] = None) -> List[Check]:
    checks: List[Check] = []
    for func in CHECK_FUNCS:
        checks.extend(func(ctx))
    if only:
        checks = [c for c in checks if c.id == only]
    else:
        checks = [c for c in checks if c.applies_to(ctx.selected)]
    return checks


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def component_report(checks: Sequence[Check], selected: Sequence[str]) -> Dict[str, dict]:
    report: Dict[str, dict] = {}
    for comp in selected:
        # A universal WARN (a diagnostic tool, an optional device) must not
        # mark every install path degraded; a universal FAIL does block all.
        relevant = [c for c in checks
                    if comp in c.components
                    or ("*" in c.components and c.state == FAIL)]
        fails = [c.id for c in relevant if c.state == FAIL]
        warns = [c.id for c in relevant if c.state == WARN]
        present, total, _missing = (0, 0, [])
        state = "blocked" if fails else ("degraded" if warns else "ready")
        report[comp] = {
            "state": state,
            "blockers": fails,
            "warnings": warns,
        }
    return report


def render_text(ctx: Context, checks: Sequence[Check], comps: Dict[str, dict],
                safe: bool, skipped: Sequence[str], warns: Sequence[str],
                quiet: bool) -> str:
    version = systemd_version(ctx)
    lines = [
        "%s %s  repo=%s  user=%s  root=%s  systemd=%s  kernel=%s"
        % (PROG, VERSION, ctx.repo, ctx.user,
           "yes" if ctx.euid == 0 else "no", version or "?", kernel_release(ctx) or "?"),
        "",
    ]
    for check in checks:
        if quiet and check.state == OK:
            continue
        lines.append("[%-4s] %-8s %-22s %s" % (
            check.state, check.group, check.id, check.summary))
        if check.remediation and check.state != OK:
            lines.append("       fix: %s" % check.remediation)
    if not quiet:
        lines.append("")
        lines.append("== components (install readiness)")
        for comp in ctx.selected:
            entry = comps[comp]
            present, total, _missing = component_state(ctx, comp)
            install = ("not installed" if present == 0
                       else ("installed" if present == total
                             else "half-install %d/%d" % (present, total)))
            lines.append("   %-9s %-8s %s" % (comp, entry["state"], install))
    lines.append("")
    if safe:
        lines.append(
            "== verdict: SAFE - an install would complete; %d blocker(s), %d warning(s)"
            % (sum(1 for c in checks if c.state == FAIL), len(warns)))
        if warns:
            lines.append("   warnings: %s" % " ".join(warns))
            lines.append("   warnings are degraded paths the installers already "
                         "handle; --strict exits 1 while any remain")
    else:
        blockers = [c.id for c in checks if c.state == FAIL]
        lines.append(
            "== verdict: UNSAFE - refusing an install; fix the blockers first")
        lines.append("   blockers: %s" % " ".join(blockers))
        lines.append("   would skip: %s" % (" ".join(skipped) if skipped else "(none)"))
    return "\n".join(lines) + "\n"


def render_json(ctx: Context, checks: Sequence[Check], comps: Dict[str, dict],
                safe: bool, code: int, skipped: Sequence[str],
                warns: Sequence[str]) -> str:
    counts = {
        "total": len(checks),
        "ok": sum(1 for c in checks if c.state == OK),
        "warn": sum(1 for c in checks if c.state == WARN),
        "fail": sum(1 for c in checks if c.state == FAIL),
    }
    payload = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": ctx.repo,
        "user": ctx.user,
        "root": ctx.euid == 0,
        "kernel": kernel_release(ctx),
        "systemd": systemd_version(ctx),
        "selection": list(ctx.selected),
        "safe": safe,
        "verdict": "safe" if safe else "unsafe",
        "strict": ctx.strict,
        "counts": counts,
        "skipped_components": list(skipped),
        "warning_ids": list(warns),
        "components": comps,
        "checks": [
            {
                "id": c.id,
                "group": c.group,
                "title": c.title,
                "state": c.state,
                "summary": c.summary,
                "remediation": c.remediation,
                "components": list(c.components),
            }
            for c in checks
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# fixtures (--selftest and --fixtures)
# ---------------------------------------------------------------------------
def _merge(base: Dict[str, object], override: Dict[str, object]) -> Dict[str, object]:
    out = dict(base)
    for key, value in override.items():
        if key in ("expect",) or not isinstance(value, dict):
            out[key] = value
        else:
            merged = dict(out.get(key) or {})
            merged.update(value)
            out[key] = merged
    return out


def load_fixture(path: Path) -> Dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    base = data.get("base")
    if base:
        base_path = path.parent / (str(base) + ".json")
        with base_path.open(encoding="utf-8") as fh:
            data = _merge(json.load(fh), data)
    return data


def run_fixture(path: Path) -> Tuple[bool, List[str]]:
    data = load_fixture(path)
    host = FixtureHost(data)
    user = str(data.get("user") or "tester")
    home = str(data.get("home") or "/home/tester")
    repo = str(data.get("repo") or "/repo")
    euid = int(data.get("euid", 0))
    selected = tuple(data.get("selection") or COMPONENTS)
    ctx = Context(host, user, home, repo, euid, selected, False)
    checks = run_checks(ctx)
    comps = component_report(checks, selected)
    safe, code, skipped, warns = explain_verdict(checks, selected, False)
    expect = data.get("expect") or {}
    errors: List[str] = []
    if "verdict" in expect and expect["verdict"] != ("safe" if safe else "unsafe"):
        errors.append("verdict %r, expected %r"
                      % ("safe" if safe else "unsafe", expect["verdict"]))
    if "safe" in expect and bool(expect["safe"]) != safe:
        errors.append("safe=%r, expected %r" % (safe, expect["safe"]))
    if "exit" in expect and int(expect["exit"]) != code:
        errors.append("exit %d, expected %d" % (code, expect["exit"]))
    if "skipped" in expect and list(expect["skipped"]) != skipped:
        errors.append("skipped %r, expected %r" % (skipped, expect["skipped"]))
    for cid, state in (expect.get("statuses") or {}).items():
        found = next((c for c in checks if c.id == cid), None)
        if found is None:
            errors.append("status %s: check not run" % cid)
        elif found.state != state:
            errors.append("status %s: %s, expected %s" % (cid, found.state, state))
    return not errors, errors


def run_fixture_dir(directory: Path) -> int:
    if not directory.is_dir():
        print("%s: fixture directory not found: %s" % (PROG, directory), file=sys.stderr)
        return 1
    failures = 0
    total = 0
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("base"):
            continue
        total += 1
        passed, errors = run_fixture(path)
        if passed:
            print("ok   %s" % path.stem)
        else:
            failures += 1
            print("FAIL %s" % path.stem)
            for error in errors:
                print("       %s" % error)
    print("fixtures: %d case(s), %d failed" % (total, failures))
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def eq(self, label: str, actual, expected) -> None:
        if actual == expected:
            self.passed += 1
        else:
            self.failed += 1
            print("FAIL %s: expected %r, got %r" % (label, expected, actual))

    def true(self, label: str, value: bool) -> None:
        self.eq(label, bool(value), True)


def selftest() -> int:
    checks = Checker()
    checks.eq("systemd/262", parse_systemd_version("systemd 262 (262-3.fc45)"), 262)
    checks.eq("systemd/245", parse_systemd_version("systemd 245\n+PAM"), 245)
    checks.eq("systemd/none", parse_systemd_version("not systemd"), None)
    checks.eq("passwd/uid", parse_passwd(
        "root:x:0:0::/root:/bin/bash\ntester:x:1000:1000::/home/tester:/bin/bash",
        "tester"), (1000, "/home/tester"))
    checks.eq("passwd/missing", parse_passwd("root:x:0:0::/root:/bin/bash", "tester"), None)

    options = parse_kernel_options(
        "CONFIG_HIDRAW=y\n# CONFIG_INPUT_UINPUT is not set\nCONFIG_BMI260=m\n")
    checks.eq("kernel/hidraw", options.get("CONFIG_HIDRAW"), "y")
    checks.eq("kernel/uinput", options.get("CONFIG_INPUT_UINPUT"), "n")
    checks.eq("kernel/bmi260", options.get("CONFIG_BMI260"), "m")

    unit = "[Unit]\nRestart=always\n[Service]\nRestart=on-failure\n"
    checks.eq("unit/restart", parse_unit_restart(unit),
              [("Unit", "always"), ("Service", "on-failure")])
    good = parse_logind_options(["[Login]\n#HandlePowerKey=poweroff\nHandleSuspendKey=suspend\n"])
    checks.eq("logind/default", good.get("HandlePowerKey"), "poweroff")
    checks.eq("logind/override", good.get("HandleSuspendKey"), "suspend")

    checks.eq("hhd/version", parse_hhd_version("hhd-4.1.12+git.x-1.fc45"),
              (4, 1, 12))
    checks.true("hhd/at-least", version_at_least((4, 1, 12), HHD_MIN))
    checks.true("hhd/older", not version_at_least((4, 1, 11), HHD_MIN))

    refs = stale_checkout_refs(
        "ExecStart=/home/t/git/oxp3-anatase-fixes/tools/x\n"
        "ANV=/home/t/other/oxp3-anatase-fixes/tools/y\n",
        "/home/t/git/oxp3-anatase-fixes", "/home/t")
    checks.eq("stale/refs", refs, ["/home/t/other/oxp3-anatase-fixes"])

    host = FixtureHost({
        "dirs": ["/sys/class/hidraw/hidraw4/device"],
        "files": {
            "/sys/class/hidraw/hidraw4/device/uevent":
                "HID_ID=0003:00001A86:0000FE00\n",
            "/sys/class/hidraw/hidraw4/device/report_descriptor": "\x06\x00\xff",
        },
    })
    ctx = Context(host, "tester", "/home/tester", "/repo", 0, COMPONENTS, False)
    checks.eq("hid/vendor-node", find_vendor_hidraw(ctx), "/dev/hidraw4")

    # A node with an input child is not the vendor collection.
    host2 = FixtureHost({
        "dirs": ["/sys/class/hidraw/hidraw2/device/input"],
        "files": {"/sys/class/hidraw/hidraw2/device/uevent":
                  "HID_ID=0003:00001A86:0000FE00\n"},
    })
    ctx2 = Context(host2, "tester", "/home/tester", "/repo", 0, COMPONENTS, False)
    checks.eq("hid/input-child-skipped", find_vendor_hidraw(ctx2), None)

    sample_ok = [ok("a", "x", "t"), warn("b", "x", "t", "s", "f"), ok("c", "x", "t")]
    safe, code, skipped, warns = explain_verdict(sample_ok, COMPONENTS, False)
    checks.true("verdict/safe", safe)
    checks.eq("verdict/code", code, 0)
    checks.eq("verdict/warns", warns, ["b"])
    safe, code, _skipped, _warns = explain_verdict(sample_ok, COMPONENTS, True)
    checks.true("verdict/strict-safe", safe)
    checks.eq("verdict/strict-code", code, 1)
    sample_bad = [fail("a", "x", "t", "s", "f", ("volume",)), ok("c", "x", "t")]
    safe, code, skipped, _warns = explain_verdict(sample_bad, COMPONENTS, False)
    checks.true("verdict/unsafe", not safe)
    checks.eq("verdict/unsafe-code", code, 1)
    checks.eq("verdict/skipped", skipped, ["volume"])

    failures = 0
    total = 0
    if FIXTURES.is_dir():
        for path in sorted(FIXTURES.glob("*.json")):
            if path.name.startswith("base"):
                continue
            total += 1
            passed, errors = run_fixture(path)
            if passed:
                checks.passed += 1
            else:
                failures += 1
                checks.failed += 1
                print("FAIL fixture %s" % path.stem)
                for error in errors:
                    print("       %s" % error)
    print("selftest: %d checks passed, %d failed (fixtures: %d case(s))"
          % (checks.passed, checks.failed, total))
    return 0 if checks.failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def usage() -> str:
    return __doc__ or ""


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--fixtures")
    parser.add_argument("--component", action="append", default=[])
    parser.add_argument("--user")
    parser.add_argument("-h", "--help", action="store_true")
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit:
        return 2
    if unknown:
        print("%s: unknown option: %s (try --help)" % (PROG, unknown[0]),
              file=sys.stderr)
        return 2
    if args.help:
        print(usage())
        return 0
    if args.selftest:
        return selftest()
    if args.fixtures:
        return run_fixture_dir(Path(args.fixtures))

    selection: List[str] = []
    only: Optional[str] = None
    for value in args.component:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if part in COMPONENTS:
                if part not in selection:
                    selection.append(part)
            elif re.match(r"^[a-z][a-z0-9]*\.[a-z0-9.]+$", part):
                only = part
            else:
                print("%s: unknown component %r (want one of: %s)"
                      % (PROG, part, ", ".join(COMPONENTS)), file=sys.stderr)
                return 2
    if not selection:
        selection = list(COMPONENTS)

    host = Host()
    euid = os.geteuid() if hasattr(os, "geteuid") else 0
    env_user = os.environ.get("SUDO_USER", "").strip()
    try:
        current = pwd.getpwuid(euid).pw_name
        home = pwd.getpwuid(euid).pw_dir
    except (KeyError, OSError):
        current = os.environ.get("USER", "root")
        home = os.environ.get("HOME", "/root")
    user = args.user or env_user or current
    if args.user and args.user != current:
        entry = parse_passwd(Host().read("/etc/passwd"), args.user)
        if entry:
            home = entry[1]
    repo = str(REPO)
    ctx = Context(host, user, home, repo, euid, selection, bool(args.strict))
    checks = run_checks(ctx, only=only)
    if not checks:
        print("%s: no such check: %s" % (PROG, only), file=sys.stderr)
        return 2
    comps = component_report(checks, selection)
    safe, code, skipped, warns = explain_verdict(checks, selection, bool(args.strict))
    if args.json:
        sys.stdout.write(render_json(ctx, checks, comps, safe, code, skipped, warns))
    else:
        sys.stdout.write(render_text(ctx, checks, comps, safe, skipped, warns,
                                     bool(args.quiet)))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # keep the exit-code contract even on a bug
        print("%s: internal error: %s" % (PROG, exc), file=sys.stderr)
        sys.exit(3)
