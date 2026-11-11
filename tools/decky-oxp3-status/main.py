#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3-status - read-only status checks for the OneXPlayer 3 "Anatase" fixes.

One implementation, two consumers:

  * Decky Loader (``Plugin.get_status`` in this file) for the Quick Access Menu;
  * ``tools/oxp3-status.sh`` / ``python3 main.py --text|--json`` for a headless CLI.

Every check is defensive: a missing file, a permission error, a timeout or
unparseable data yields ``unknown``/``warn`` instead of an exception, because the
plugin backend may run as a different user, and a Flatpak/pressure-vessel game
may not even share our PID namespace.

The module has no third-party dependencies and never writes to the system.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.3.1"

# ---------------------------------------------------------------------------
# Recorded expectations (docs/WORKING-BUILD-2026-11-08.md, docs/xess-xmx.md).
# ---------------------------------------------------------------------------
HOST_DRIVER = "/usr/lib64/libvulkan_intel.so"
HOST_DRIVER_PREFIXES = ("5a75b6a44d285fd1", "ec418f04e589965a")
HOST_DRIVER_PREFIX = HOST_DRIVER_PREFIXES[0]

XMX_DRIVER_REL = ".cache/oxp3-fixes/xmx-driver/libvulkan_intel.so"
XMX_DRIVER_PREFIXES = ("d5e7d7677cc29882", "4b1d55ea9483417f")
XMX_DRIVER_PREFIX = XMX_DRIVER_PREFIXES[0]
XMX_ICD_REL = ".config/xess-xmx/icd-xmx.json"
XMX_ENV_REL = ".config/xess-xmx/oxp3-env.sh"
XMX_KERNEL_CACHE_REL = ".cache/xess-xmx/kernels"
XMX_COMPILE_LOG_REL = ".cache/xess-xmx/compile.log"
XMX_HELPER_MARKER = "xmx-cm-compile-nodgrf"

RUNTIME_OVERRIDES = (
    "/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides"
)
RUNTIME_DRIVER_EXPECTED = "4d043cc2851a11fd"

STEAM_OVERRIDE_REL = ".local/share/flatpak/overrides/org.anatase.Steam"
HEROIC_CONFIG_REL = ".var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig"

EXPECTED_PL1_W = 35
SCHED_UNIT = "oxp3-gaming-sched.service"
RAPL_UNIT = "rapl-power.service"
RAPL_PP1_UNIT = "oxp3-rapl-pp1.service"
SCHED_EXT_STATE = "/sys/kernel/sched_ext/state"

CYBERPUNK_RESULT_GLOB = (
    "drive_c/users/*/Documents/CD Projekt Red/Cyberpunk 2077/"
    "benchmarkResults/*/summary.json"
)

STATE_RANK = {"ok": 0, "na": 0, "unknown": 1, "warn": 2, "fail": 3}


def _env_path(name: str, default: str) -> str:
    """Directory override used by --selftest (and nothing else) so the checks
    can run against a synthetic /sys, /proc or /etc tree. Unset == real root."""
    value = os.environ.get(name, "").strip()
    return value.rstrip("/") if value else default


SYS_ROOT = _env_path("OXP3_SYS_ROOT", "/sys")
PROC_ROOT = _env_path("OXP3_PROC_ROOT", "/proc")
ETC_ROOT = _env_path("OXP3_ETC_ROOT", "/etc")
RUN_ROOT = _env_path("OXP3_RUN_ROOT", "/run")
# The host driver is a fixture in --selftest; a real run always uses HOST_DRIVER.
DRIVER_PATH = os.environ.get("OXP3_HOST_DRIVER", "").strip() or HOST_DRIVER
RUNTIME_OVERRIDES_PATH = _env_path("OXP3_RUNTIME_OVERRIDES", RUNTIME_OVERRIDES)


def _sys(rel: str) -> str:
    return SYS_ROOT + rel


def _proc_entry(pid: Any, leaf: str = "") -> str:
    base = "%s/%s" % (PROC_ROOT, pid)
    return base + ("/" + leaf if leaf else "")


def _cap_output(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# Environment keys worth showing in a report. Everything else is client/secret
# material (tokens, sockets, DBus addresses, SSH agent paths) and is dropped
# before the report leaves this process.
_ENV_REPORT_ALLOW_PREFIXES = (
    "Steam",
    "ANV_",
    "XMX_",
    "VK_",
    "VKD3D_",
    "INTC_",
    "IGC",
    "MESA_",
    "PROTON",
    "DXVK_",
    "WINE",
    "GAMEID",
    "UMU_",
    "ENABLE_",
)
_ENV_SECRET_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PASS$|APIKEY|API_KEY|AUTH|COOKIE|SESSION|CREDENTIAL|LICENSE_KEY)",
    re.IGNORECASE,
)
_ENV_MAX_ENTRIES = 64
_ENV_VALUE_MAX = 256


def sanitize_env(env: Dict[str, str]) -> Tuple[Dict[str, str], int]:
    """Return a report-safe environment view and the number of dropped keys.

    The backend still uses the complete environment internally (the XMX checks
    read variables from it); only the serialised report is filtered, so a
    clipboard/json dump cannot leak session or socket material.
    """
    out: Dict[str, str] = {}
    dropped = 0
    for key in sorted(env):
        value = env[key]
        if not key.startswith(_ENV_REPORT_ALLOW_PREFIXES):
            dropped += 1
            continue
        if _ENV_SECRET_RE.search(key):
            out[key] = "<redacted>"
            continue
        if len(out) >= _ENV_MAX_ENTRIES:
            dropped += 1
            continue
        out[key] = _cap_output(value, _ENV_VALUE_MAX)
    return out, dropped


def sanitize_cmdline(cmdline: str) -> str:
    """Strip suspected secrets from a process command line for the report."""
    parts = cmdline.split()
    clean: List[str] = []
    for part in parts:
        if "=" in part:
            key, _value = part.split("=", 1)
            if _ENV_SECRET_RE.search(key.lstrip("-")):
                clean.append("%s=<redacted>" % key)
                continue
        clean.append(part)
    return _cap_output(" ".join(clean), 512)


def sanitize_game(game: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not game:
        return None
    copy = dict(game)
    env, dropped = sanitize_env(game.get("env") or {})
    copy["env"] = env
    copy["env_omitted"] = dropped
    if "cmdline" in copy and isinstance(copy["cmdline"], str):
        copy["cmdline"] = sanitize_cmdline(copy["cmdline"])
    return copy


# ---------------------------------------------------------------------------
# Small defensive helpers
# ---------------------------------------------------------------------------
def _read_bytes(path: Any, limit: Optional[int] = None) -> Optional[bytes]:
    try:
        with open(str(path), "rb") as fh:
            return fh.read() if limit is None else fh.read(limit)
    except (OSError, ValueError):
        return None


def _read_text(path: Any, limit: int = 262144) -> Optional[str]:
    data = _read_bytes(path, limit)
    if data is None:
        return None
    return data.decode("utf-8", "replace")


def _read_int(path: Any) -> Optional[int]:
    text = _read_text(path, 128)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _read_float(path: Any) -> Optional[float]:
    text = _read_text(path, 128)
    if text is None:
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


def _run(argv: List[str], timeout: float = 2.0) -> Optional[Tuple[int, str, str]]:
    """Run a short read-only command; None means "could not run"."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            errors="replace",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return None


_hash_cache: Dict[Tuple[str, int, int], str] = {}


def sha256_hex(path: Any) -> Optional[str]:
    """Full sha256, cached by (path, mtime_ns, size); None when unreadable."""
    try:
        st = os.stat(str(path))
        if not os.path.isfile(str(path)):
            return None
        key = (os.path.realpath(str(path)), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    cached = _hash_cache.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(str(path), "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    # Bounded: --watch runs for hours and every distinct (path, mtime, size)
    # must not grow the process without limit.
    if len(_hash_cache) >= 64:
        _hash_cache.clear()
    _hash_cache[key] = value
    return value


def sha256_prefix(path: Any, chars: int = 16) -> Optional[str]:
    full = sha256_hex(path)
    return full[:chars] if full else None


_MESA_VERSION_RE = re.compile(rb"Mesa\s+(\d+\.\d+(?:\.\d+)?)")
_version_cache: Dict[Tuple[str, int, int], Optional[str]] = {}


def mesa_version(path: Any) -> Optional[str]:
    """Best-effort Mesa version string embedded in a driver .so (cheap scan)."""
    try:
        st = os.stat(str(path))
        key = (os.path.realpath(str(path)), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    if key in _version_cache:
        return _version_cache[key]
    found: Optional[str] = None
    try:
        with open(str(path), "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                match = _MESA_VERSION_RE.search(tail + chunk)
                if match:
                    found = match.group(1).decode("ascii", "replace")
                    break
                tail = chunk[-64:]
    except OSError:
        found = None
    _version_cache[key] = found
    return found


def _age_text(mtime: Optional[float]) -> str:
    if not mtime:
        return "unknown age"
    delta = max(0.0, time.time() - mtime)
    if delta < 120:
        return "just now"
    if delta < 7200:
        return "%d min ago" % int(delta // 60)
    if delta < 172800:
        return "%d h ago" % int(delta // 3600)
    return "%d d ago" % int(delta // 86400)


def _expand(value: str, home: Path) -> str:
    value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    return value


# ---------------------------------------------------------------------------
# User/home resolution: the backend may run as root with HOME=/root while the
# games and the configured environment belong to the desktop user.
# ---------------------------------------------------------------------------
def candidate_homes(explicit: Optional[str] = None) -> List[Path]:
    homes: List[Path] = []
    if explicit:
        homes.append(Path(explicit))
    for env in ("OXP3_STATUS_HOME", "OXP3_HOME", "DECKY_USER_HOME"):
        if os.environ.get(env):
            homes.append(Path(os.environ[env]))
    if os.environ.get("HOME"):
        homes.append(Path(os.environ["HOME"]))
    for user_env in ("SUDO_USER", "DECKY_USER"):
        if os.environ.get(user_env):
            try:
                homes.append(Path(pwd.getpwnam(os.environ[user_env]).pw_dir))
            except KeyError:
                pass
    for base in ("/var/home", "/home"):
        try:
            for entry in sorted(os.listdir(base)):
                path = Path(base) / entry
                if path.is_dir() and not entry in ("lost+found",):
                    homes.append(path)
        except OSError:
            pass
    try:
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except KeyError:
        pass
    unique: List[Path] = []
    seen = set()
    for home in homes:
        key = str(home)
        if key not in seen:
            seen.add(key)
            unique.append(home)
    return unique


def resolve_home(explicit: Optional[str] = None) -> Tuple[Path, List[str]]:
    notes: List[str] = []
    best: Optional[Path] = None
    best_score = -1
    # An explicit --home is authoritative: do not silently "upgrade" it to a
    # better-scoring candidate, or a test/alternate home would be ignored.
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if explicit_path.is_dir():
            notes.append("using explicit home: %s" % explicit_path)
            return explicit_path, notes
        notes.append("explicit home is not a directory, falling back: %s" % explicit)

    for home in candidate_homes(explicit):
        score = 0
        if (home / XMX_ENV_REL).exists() or (home / XMX_ICD_REL).exists():
            score += 5
        if (home / ".cache/oxp3-fixes").is_dir():
            score += 3
        if (home / HEROIC_CONFIG_REL).is_dir():
            score += 1
        if (home / "homebrew/plugins").is_dir():
            score += 1
        if score > best_score and home.is_dir():
            best, best_score = home, score
    if best is None:
        best = Path(os.environ.get("HOME") or Path.home())
        notes.append("could not resolve a desktop home; using %s" % best)
    elif best != Path(os.environ.get("HOME") or "/nonexistent"):
        notes.append("resolved desktop home: %s" % best)
    return best, notes


# ---------------------------------------------------------------------------
# Indicator model
# ---------------------------------------------------------------------------
def _as_text(value: Any) -> str:
    """Coerce a probe result to text so the report is always JSON-serialisable.

    A probe that accidentally hands back a Path, bytes or a number must not turn
    the whole report into a TypeError inside the Decky backend.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        if isinstance(value, os.PathLike):
            return os.fspath(value)
    except TypeError:
        pass
    return str(value)


def ind(
    key: str,
    label: str,
    state: str,
    value: Any,
    detail: Any = None,
    source: Any = None,
) -> Dict[str, Any]:
    if state not in STATE_RANK:
        state = "unknown"
    return {
        "id": _as_text(key),
        "label": _as_text(label),
        "state": state,
        "value": _as_text(value),
        "detail": None if detail is None else _as_text(detail),
        "source": None if source is None else _as_text(source),
    }


def group(gid: str, title: str, indicators: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = "ok"
    for item in indicators:
        if STATE_RANK.get(item["state"], 1) > STATE_RANK.get(state, 0):
            state = item["state"]
    return {"id": gid, "title": title, "state": state, "indicators": indicators}


# ---------------------------------------------------------------------------
# Environment sources: live process env, Heroic config, Steam Flatpak override,
# and the XMX env file. Never execute any of them.
# ---------------------------------------------------------------------------
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    text = _read_text(path, 65536)
    if not text:
        return out
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def parse_flatpak_override(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    text = _read_text(path, 65536)
    if not text:
        return out
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section == "Environment" and "=" in stripped:
            key, value = stripped.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def read_heroic_games(home: Path) -> List[Dict[str, Any]]:
    games: List[Dict[str, Any]] = []
    folder = home / HEROIC_CONFIG_REL
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return games
    for path in files[:200]:
        text = _read_text(path, 1024 * 1024)
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        for appid, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            env: Dict[str, str] = {}
            env_vars = cfg.get("envVars")
            if isinstance(env_vars, dict):
                for key, value in env_vars.items():
                    if isinstance(value, str):
                        env[str(key)] = value
            for list_key in ("enviromentOptions", "environmentOptions"):
                entries = cfg.get(list_key)
                if isinstance(entries, list):
                    for entry in entries:
                        if (
                            isinstance(entry, dict)
                            and isinstance(entry.get("key"), str)
                            and isinstance(entry.get("value"), str)
                        ):
                            env[entry["key"]] = entry["value"]
            wrapper = cfg.get("wrapperOptions")
            wrappers = []
            if isinstance(wrapper, list):
                for item in wrapper:
                    if isinstance(item, dict) and isinstance(item.get("value"), str):
                        wrappers.append(item["value"])
                    elif isinstance(item, str):
                        wrappers.append(item)
            games.append(
                {
                    "appid": str(appid),
                    "file": str(path),
                    "use_game_mode": bool(cfg.get("useGameMode") is True),
                    "wrappers": wrappers,
                    "wine_prefix": cfg.get("winePrefix")
                    if isinstance(cfg.get("winePrefix"), str)
                    else None,
                    "env": env,
                }
            )
    return games


def match_heroic_game(
    games: List[Dict[str, Any]], game: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not games:
        return None
    if game:
        appid = game.get("appid")
        if appid:
            for cfg in games:
                if cfg.get("appid") == appid:
                    return cfg
        token = (game.get("name") or "").strip().lower()
        # Compare against path *components*, never a raw substring: a title
        # called "game" must not match every prefix under ".../Games/...".
        if len(token) >= 3:
            for cfg in games:
                base = os.path.basename((cfg.get("wine_prefix") or "").rstrip("/")).lower()
                if base == token or (len(token) >= 5 and token in base):
                    return cfg
            for cfg in games:
                for wrapper in cfg.get("wrappers", []):
                    stem = os.path.splitext(os.path.basename(str(wrapper)))[0].lower()
                    if stem == token:
                        return cfg
    # No live game: the launcher config that carries XMX variables is the best
    # evidence of what a normal launch will do. With a live game present this
    # fallback must NOT fire: another title's environment would be attributed
    # to the running game.
    if game:
        return None
    for cfg in games:
        if cfg.get("env", {}).get("VK_DRIVER_FILES"):
            return cfg
    return None


def build_env_sources(
    home: Path, game: Optional[Dict[str, Any]], heroic: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Ordered env layers and a merged view (first live/configured wins)."""
    sources: List[Dict[str, Any]] = []
    if game and game.get("env"):
        sources.append(
            {"name": "game process env", "live": True, "env": game["env"]}
        )
    cfg = match_heroic_game(heroic, game)
    if cfg:
        sources.append(
            {
                "name": "Heroic config %s" % cfg["appid"],
                "live": False,
                "env": cfg.get("env", {}),
            }
        )
    steam = parse_flatpak_override(home / STEAM_OVERRIDE_REL)
    if steam:
        sources.append({"name": "Steam Flatpak override", "live": False, "env": steam})
    env_file = parse_env_file(home / XMX_ENV_REL)
    if env_file:
        sources.append({"name": XMX_ENV_REL, "live": False, "env": env_file})

    merged: Dict[str, Dict[str, Any]] = {}
    for source in sources:
        for key, raw in source["env"].items():
            if not isinstance(raw, str):
                continue
            # An empty variable (common in inherited process environments) is
            # "not set" for our purposes: it must not shadow a configured value
            # from the launcher/override layers.
            if not raw.strip():
                continue
            value = _expand(raw, home)
            current = merged.get(key)
            if current is None or (source["live"] and not current["live"]):
                merged[key] = {
                    "value": value,
                    "source": source["name"],
                    "live": source["live"],
                }
    return sources, merged


# ---------------------------------------------------------------------------
# Process/game detection
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    return os.path.isdir(_proc_entry(pid))


def _proc_cmdline(pid: int) -> List[str]:
    data = _read_bytes(_proc_entry(pid, "cmdline"), 65536)
    if not data:
        return []
    return [part.decode("utf-8", "replace") for part in data.split(b"\0") if part]


def _proc_comm(pid: int) -> str:
    text = _read_text(_proc_entry(pid, "comm"), 64)
    return text.strip() if text else ""


def _proc_environ(pid: int) -> Dict[str, str]:
    data = _read_bytes(_proc_entry(pid, "environ"), 2 * 1024 * 1024)
    if not data:
        return {}
    out: Dict[str, str] = {}
    for part in data.split(b"\0"):
        if b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        try:
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        except Exception:
            continue
    return out


_WINE_COMMS = {
    "wine",
    "wine64",
    "wine-preloader",
    "wine64-preloader",
    "wineserver",
    "proton",
}
_PRETTY_GAMES = {
    "cyberpunk2077": "Cyberpunk 2077",
    "cyberpunk2077.exe": "Cyberpunk 2077",
}


def _pretty_game_name(exe: str) -> str:
    base = os.path.basename(exe)
    stem = os.path.splitext(base)[0]
    pretty = _PRETTY_GAMES.get(base.lower()) or _PRETTY_GAMES.get(stem.lower())
    if pretty:
        return pretty
    return stem.replace("_", " ").strip() or base


def detect_game(home: Path) -> Optional[Dict[str, Any]]:
    try:
        target_uid = home.stat().st_uid
    except OSError:
        target_uid = os.getuid()

    best: Optional[Tuple[int, int, List[str], str]] = None
    try:
        pids = [name for name in os.listdir(PROC_ROOT) if name.isdigit()]
    except OSError:
        return None
    for name in pids:
        pid = int(name)
        try:
            if os.stat(_proc_entry(pid)).st_uid != target_uid:
                continue
        except OSError:
            continue
        args = _proc_cmdline(pid)
        if not args:
            continue
        comm = _proc_comm(pid)
        score = 0
        matched_exe = ""
        for arg in args:
            low = arg.lower()
            if low.endswith(".exe"):
                if "cyberpunk2077" in os.path.basename(low):
                    score = max(score, 100)
                    matched_exe = arg
                elif "/steamapps/common/" in low or "/games/" in low or "\\games\\" in low:
                    score = max(score, 50)
                    matched_exe = matched_exe or arg
        if comm in _WINE_COMMS:
            score = max(score, 30)
        if score <= 0:
            continue
        # Prefer the strongest hint, then the newest (highest) pid.
        if best is None or (score, pid) > (best[0], best[1]):
            best = (score, pid, args, matched_exe or comm)

    if best is None:
        return None

    _, pid, args, exe = best
    env = _proc_environ(pid)
    appid = env.get("SteamAppId") or env.get("SteamGameId")
    prefix = env.get("STEAM_COMPAT_DATA_PATH")
    if prefix:
        prefix_path = Path(prefix)
    else:
        prefix_path = None
    name = _pretty_game_name(exe)
    return {
        "detected": True,
        "name": name,
        "pid": pid,
        "exe": exe,
        "appid": appid,
        "prefix": str(prefix_path) if prefix_path else None,
        "env": env,
        "env_readable": bool(env),
        "cmdline": " ".join(args)[:512],
    }


# ---------------------------------------------------------------------------
# Prefix discovery, benchmark and shim trace
# ---------------------------------------------------------------------------
def candidate_prefixes(
    home: Path,
    game: Optional[Dict[str, Any]],
    heroic: List[Dict[str, Any]],
    limit: int = 120,
) -> List[Path]:
    candidates: List[Path] = []

    def add(path: Optional[Path]) -> None:
        if not path:
            return
        try:
            if path.is_dir():
                candidates.append(path)
        except OSError:
            pass

    if game and game.get("prefix"):
        base = Path(game["prefix"])
        add(base)
        add(base / "pfx")
        add(base / "drive_c")

    for cfg in heroic:
        prefix = cfg.get("wine_prefix")
        if prefix:
            path = Path(_expand(prefix, home))
            add(path)
            add(path / "pfx")

    for base in (home / "Games/Heroic/Prefixes", home / "Games/Heroic/Prefixes"):
        try:
            for entry in sorted(base.iterdir()):
                add(entry / "pfx")
                add(entry)
        except OSError:
            pass

    steam_compat = home / ".local/share/Steam/steamapps/compatdata"
    try:
        compat_entries = sorted(steam_compat.iterdir())[:200]
    except OSError:
        compat_entries = []
    for entry in compat_entries:
        add(entry / "pfx")

    unique: List[Path] = []
    seen = set()
    for path in candidates:
        key = os.path.realpath(str(path))
        if key not in seen and len(unique) < limit:
            seen.add(key)
            unique.append(path)
    return unique


def find_latest_benchmark(prefixes: List[Path]) -> Optional[Dict[str, Any]]:
    latest: Optional[Dict[str, Any]] = None
    for prefix in prefixes:
        try:
            matches = list(prefix.glob(CYBERPUNK_RESULT_GLOB))
        except (OSError, ValueError):
            continue
        for summary_path in matches:
            try:
                mtime = summary_path.stat().st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest["mtime"]:
                latest = {"path": str(summary_path), "mtime": mtime}
    return latest


def parse_benchmark(summary_path: str) -> Optional[Dict[str, Any]]:
    text = _read_text(summary_path, 2 * 1024 * 1024)
    if not text:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    data = raw.get("Data") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return None

    def num(key: str) -> Optional[float]:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    def flag(key: str) -> bool:
        return data.get(key) is True

    upscaling = data.get("upscalingType")
    label = None
    if flag("XeSSEnabled"):
        label = "XeSS"
    elif flag("DLSSEnabled"):
        label = "DLSS"
    elif flag("FSR4Enabled"):
        label = "FSR4"
    elif flag("FSR3Enabled"):
        label = "FSR3"
    elif flag("FSR2Enabled"):
        label = "FSR2"
    elif flag("MFXEnabled"):
        label = "MFX"
    elif upscaling == 0:
        label = "off"
    return {
        "path": summary_path,
        "mtime": os.stat(summary_path).st_mtime,
        "upscaling_type": upscaling,
        "upscaling_label": label,
        "xess": flag("XeSSEnabled"),
        "xess_quality": data.get("XeSSQuality"),
        "average_fps": num("averageFps"),
        "min_fps": num("minFps"),
        "max_fps": num("maxFps"),
        "gpu": data.get("gpuName"),
        "resolution": "%sx%s" % (data.get("renderWidth"), data.get("renderHeight")),
        "preset": data.get("presetName"),
        "benchmark_name": os.path.basename(os.path.dirname(summary_path)),
    }


def find_trace_log(prefixes: List[Path]) -> Optional[Dict[str, Any]]:
    for prefix in prefixes:
        for rel in (
            prefix / "drive_c/igdext_trace.log",
            prefix / "igdext_trace.log",
            prefix / "drive_c/users/steamuser/igdext_trace.log",
        ):
            try:
                if rel.is_file():
                    stat = rel.stat()
                    return {
                        "path": str(rel),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# System probes
# ---------------------------------------------------------------------------
def unit_state(unit: str) -> Dict[str, str]:
    result = _run(
        [
            "systemctl",
            "show",
            "--no-pager",
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "UnitFileState",
            "--",
            unit,
        ]
    )
    info = {"LoadState": "unknown", "ActiveState": "unknown", "UnitFileState": ""}
    if not result:
        return info
    _, stdout, _ = result
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in info:
                info[key] = value.strip()
    return info


def _unit_indicator(key: str, label: str, unit: str, optional: bool = False) -> Dict[str, Any]:
    info = unit_state(unit)
    load, active = info["LoadState"], info["ActiveState"]
    detail = "LoadState=%s ActiveState=%s UnitFileState=%s" % (
        load,
        active,
        info["UnitFileState"] or "-",
    )
    if load == "not-found":
        state = "warn" if not optional else "unknown"
        return ind(key, label, state, "not installed", detail, "systemctl show %s" % unit)
    if active == "active":
        return ind(key, label, "ok", "active", detail, "systemctl show %s" % unit)
    state = "unknown" if optional else "warn"
    return ind(key, label, state, active, detail, "systemctl show %s" % unit)


def _sched_indicators() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    state_path = _sys("/kernel/sched_ext/state")
    state_text = _read_text(state_path, 64)
    if state_text is None:
        items.append(
            ind("sched.state", "sched_ext state", "unknown", "unreadable", None, state_path)
        )
    else:
        state = state_text.strip() or "unknown"
        items.append(
            ind(
                "sched.state",
                "sched_ext state",
                "ok" if state == "enabled" else "warn",
                state,
                "EEVDF is the kernel fallback when nothing is loaded." if state != "enabled" else None,
                state_path,
            )
        )

    running: List[int] = []
    try:
        pids = [name for name in os.listdir(PROC_ROOT) if name.isdigit()]
    except OSError:
        pids = []
    for name in pids:
        pid = int(name)
        comm = _proc_comm(pid)
        if comm == "scx_cosmos":
            running.append(pid)
        else:
            args = _proc_cmdline(pid)
            if args and os.path.basename(args[0]) == "scx_cosmos":
                running.append(pid)
    if running:
        items.append(
            ind(
                "sched.scx_cosmos",
                "scx_cosmos running",
                "ok",
                "pid %s" % ", ".join(str(p) for p in running[:3]),
                "scx_cosmos -c 0 -p 0 is the shipped gaming profile.",
                PROC_ROOT,
            )
        )
    else:
        items.append(
            ind(
                "sched.scx_cosmos",
                "scx_cosmos running",
                "warn",
                "not running",
                "Without a loaded scheduler the kernel uses EEVDF.",
                PROC_ROOT,
            )
        )
    items.append(_unit_indicator("sched.unit", "oxp3-gaming-sched.service", SCHED_UNIT))
    return items


def _mesa_indicators(
    home: Path, merged_env: Dict[str, Dict[str, Any]], game: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    host_prefix = sha256_prefix(DRIVER_PATH)
    host_ok = host_prefix in HOST_DRIVER_PREFIXES
    # A readable but different build is a degraded host (warn); a missing or
    # unreadable driver is the failure the docs describe.
    items.append(
        ind(
            "mesa.host",
            "host libvulkan_intel.so",
            "ok" if host_ok else ("warn" if host_prefix else "fail"),
            ("%s (match)" % host_prefix)
            if host_ok
            else ("%s (expected %s)" % (host_prefix, HOST_DRIVER_PREFIX))
            if host_prefix
            else "missing/unreadable",
            "path: %s\nsize: %s\nfull sha256: %s"
            % (
                DRIVER_PATH,
                _stat_size(DRIVER_PATH),
                sha256_hex(DRIVER_PATH) or "unknown",
            ),
            DRIVER_PATH,
        )
    )

    keys = [key for key in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES") if key in merged_env]
    if not keys:
        items.append(
            ind(
                "mesa.env",
                "driver override env",
                "ok",
                "unset (system default driver)",
                "No live game and no configured launcher override in scope."
                if not game
                else "The running game has no VK_DRIVER_FILES/VK_ICD_FILENAMES.",
                "VK_DRIVER_FILES / VK_ICD_FILENAMES",
            )
        )
        return items

    for key in keys:
        entry = merged_env[key]
        resolved = resolve_icd_entry(entry["value"], home)
        parts = []
        if "pressure-vessel" in entry["value"]:
            items.append(
                ind(
                    "mesa.env.%s" % key.lower(),
                    "driver override (%s)" % key,
                    "ok",
                    "Steam Linux Runtime private ICD (managed by Steam; not a host override)",
                    "value: %s\nsource: %s" % (entry["value"], entry["source"]),
                    entry["source"],
                )
            )
            continue
        state = "warn"
        if not resolved:
            parts.append("unresolvable value")
            state = "fail"
        for item in resolved:
            lib = item.get("library")
            if not lib:
                parts.append("%s (no library_path)" % item.get("icd"))
                continue
            prefix = sha256_prefix(lib)
            if prefix in HOST_DRIVER_PREFIXES:
                parts.append("%s -> host driver %s" % (Path(lib).name, prefix))
                state = "ok"
            elif prefix in XMX_DRIVER_PREFIXES:
                parts.append("%s -> XMX driver %s" % (Path(lib).name, prefix))
                state = "ok"
            elif prefix:
                parts.append("%s -> %s (unknown build)" % (Path(lib).name, prefix))
                state = "warn"
            else:
                parts.append("%s -> missing" % lib)
                state = "warn"
        items.append(
            ind(
                "mesa.env.%s" % key.lower(),
                "driver override (%s)" % key,
                state,
                "; ".join(parts) or entry["value"],
                "value: %s\nsource: %s%s"
                % (entry["value"], entry["source"], " (live process env)" if entry["live"] else ""),
                entry["source"],
            )
        )
    return items


def _stat_size(path: Any) -> str:
    try:
        return str(os.stat(str(path)).st_size)
    except OSError:
        return "unknown"


def resolve_icd_entry(value: str, home: Path) -> List[Dict[str, Optional[str]]]:
    out: List[Dict[str, Optional[str]]] = []
    for raw in value.split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(_expand(raw, home))
        if path.suffix.lower() == ".json":
            text = _read_text(path, 65536)
            library: Optional[str] = None
            if text:
                try:
                    data = json.loads(text)
                    library = (
                        data.get("ICD", {}).get("library_path")
                        if isinstance(data, dict)
                        else None
                    )
                except ValueError:
                    library = None
            if isinstance(library, str):
                library = _expand(library, home)
            out.append({"icd": str(path), "library": library})
        else:
            out.append({"icd": None, "library": str(path)})
    return out


def _xmx_indicators(
    home: Path,
    merged_env: Dict[str, Dict[str, Any]],
    prefixes: List[Path],
    benchmark: Optional[Dict[str, Any]],
    xess_title: bool = True,
    has_game: bool = False,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    xmx_driver = home / XMX_DRIVER_REL

    def env(key: str) -> Optional[Dict[str, Any]]:
        return merged_env.get(key)

    def missing_state() -> str:
        """XMX is wired per XeSS title: absence is only a warning where it is
        expected (Cyberpunk 2077 is the recorded reference title, and with no
        game at all we describe the configured machine stack)."""
        return "warn" if (xess_title or not has_game) else "na"

    def missing_note(what: str) -> str:
        if xess_title:
            return "%s is missing for the reference XeSS title." % what
        if not has_game:
            return "%s is not set in the configured launch environment." % what
        return (
            "%s is not set: this game is not launched with the XMX stack "
            "(XMX applies to XeSS titles; the system driver is used)." % what
        )

    # VK_DRIVER_FILES -> icd-xmx.json or the XMX driver itself
    entry = env("VK_DRIVER_FILES") or env("VK_ICD_FILENAMES")
    if not entry:
        items.append(
            ind(
                "xmx.icd",
                "VK_DRIVER_FILES (XMX ICD)",
                missing_state(),
                "not set",
                missing_note("VK_DRIVER_FILES"),
                "VK_DRIVER_FILES",
            )
        )
    else:
        resolved = resolve_icd_entry(entry["value"], home)
        points_to_icd = "icd-xmx.json" in entry["value"]
        points_to_driver: Optional[str] = None
        for item in resolved:
            lib = item.get("library")
            if not lib:
                continue
            try:
                same = os.path.realpath(lib) == os.path.realpath(str(xmx_driver))
            except OSError:
                same = False
            if same or sha256_prefix(lib) in XMX_DRIVER_PREFIXES:
                points_to_driver = lib
        ok = points_to_icd or points_to_driver is not None
        items.append(
            ind(
                "xmx.icd",
                "VK_DRIVER_FILES (XMX ICD)",
                "ok" if ok else missing_state(),
                (
                    "icd-xmx.json"
                    + (" -> xmx driver" if points_to_driver else "")
                    if ok
                    else "points elsewhere"
                ),
                "value: %s\nresolved: %s\nsource: %s"
                % (
                    entry["value"],
                    ", ".join(
                        "%s -> %s" % (item.get("icd") or "-", item.get("library") or "?")
                        for item in resolved
                    )
                    or "unresolved",
                    entry["source"],
                ),
                entry["source"],
            )
        )

    helper = env("ANV_CM_HELPER")
    if not helper:
        items.append(
            ind(
                "xmx.helper",
                "ANV_CM_HELPER",
                missing_state(),
                "not set",
                missing_note("ANV_CM_HELPER"),
                "ANV_CM_HELPER",
            )
        )
    else:
        value = helper["value"]
        marker_ok = XMX_HELPER_MARKER in value
        exists = os.path.isfile(value)
        state = "ok" if marker_ok and exists else "warn"
        note = []
        if not marker_ok:
            note.append("does not select the 128-GRF helper (release cm-compile.sh compiles -doubleGRF kernels)")
        if not exists:
            note.append("helper file is missing")
        items.append(
            ind(
                "xmx.helper",
                "ANV_CM_HELPER (128-GRF)",
                state,
                os.path.basename(value) if value else value,
                "value: %s\n%s\nsource: %s"
                % (value, "\n".join(note), helper["source"]),
                helper["source"],
            )
        )

    igc = env("XMX_IGC_DIR")
    if not igc:
        items.append(
            ind(
                "xmx.igc",
                "XMX_IGC_DIR",
                missing_state(),
                "not set",
                missing_note("XMX_IGC_DIR") + " Without ocloc every CM compile fails.",
                "XMX_IGC_DIR",
            )
        )
    else:
        base = Path(igc["value"])
        oclocs = sorted(str(p) for p in base.glob("neo/bin/ocloc*") if p.is_file())
        oclocs += sorted(str(p) for p in base.glob("igc/neo/bin/ocloc*") if p.is_file())
        exists = base.is_dir()
        state = "ok" if exists and oclocs else "warn"
        items.append(
            ind(
                "xmx.igc",
                "XMX_IGC_DIR (ocloc)",
                state,
                (os.path.basename(oclocs[0]) if oclocs else "no ocloc"),
                "dir: %s\nexists: %s\nocloc: %s\nsource: %s"
                % (base, exists, oclocs[0] if oclocs else "not found", igc["source"]),
                igc["source"],
            )
        )

    shim = env("INTC_ALT_DRIVER_EXTENSIONS_PATH")
    if not shim:
        items.append(
            ind(
                "xmx.shim",
                "INTC_ALT_DRIVER_EXTENSIONS_PATH",
                missing_state(),
                "not set",
                missing_note("INTC_ALT_DRIVER_EXTENSIONS_PATH")
                + " XeSS finds igdext64.dll through this directory.",
                "INTC_ALT_DRIVER_EXTENSIONS_PATH",
            )
        )
    else:
        base = Path(shim["value"])
        dll = base / "igdext64.dll"
        ok = dll.is_file()
        items.append(
            ind(
                "xmx.shim",
                "INTC_ALT_DRIVER_EXTENSIONS_PATH",
                "ok" if ok else "warn",
                "igdext64.dll present" if ok else "igdext64.dll missing",
                "dir: %s\nsource: %s" % (base, shim["source"]),
                shim["source"],
            )
        )

    vkd3d = env("VKD3D_DISABLE_EXTENSIONS")
    if not vkd3d:
        items.append(
            ind(
                "xmx.vkd3d",
                "VKD3D_DISABLE_EXTENSIONS",
                missing_state(),
                "not set",
                missing_note("VKD3D_DISABLE_EXTENSIONS")
                + " XMX needs the classic descriptor heap.",
                "VKD3D_DISABLE_EXTENSIONS",
            )
        )
    else:
        ok = "VK_EXT_descriptor_buffer" in vkd3d["value"]
        items.append(
            ind(
                "xmx.vkd3d",
                "VKD3D_DISABLE_EXTENSIONS",
                "ok" if ok else "warn",
                vkd3d["value"],
                "source: %s" % vkd3d["source"],
                vkd3d["source"],
            )
        )

    driver_prefix = sha256_prefix(xmx_driver)
    driver_state = (
        "ok"
        if driver_prefix == XMX_DRIVER_PREFIX
        else ("warn" if driver_prefix else "warn")
    )
    items.append(
        ind(
            "xmx.driver",
            "XMX driver build",
            driver_state,
            ("%s (match)" % driver_prefix)
            if driver_prefix == XMX_DRIVER_PREFIX
            else ("%s (expected %s)" % (driver_prefix, XMX_DRIVER_PREFIX))
            if driver_prefix
            else "missing/unreadable",
            "path: %s\nsize: %s" % (str(xmx_driver), _stat_size(str(xmx_driver))),
            str(xmx_driver),
        )
    )

    kernel_dir = home / XMX_KERNEL_CACHE_REL
    kernels: List[Path] = []
    try:
        kernels = [p for p in kernel_dir.glob("*.cmk") if p.is_file()]
    except OSError:
        kernels = []
    compile_log = home / XMX_COMPILE_LOG_REL
    log_text = _read_text(compile_log, 512 * 1024)
    failures = 0
    last_failure = ""
    if log_text:
        lines = log_text.splitlines()
        for line in lines:
            if "FAILED" in line:
                failures += 1
                last_failure = line.strip()[:200]
    newest = max((p.stat().st_mtime for p in kernels), default=0.0)
    k_state = "ok" if kernels and failures == 0 else "warn"
    items.append(
        ind(
            "xmx.kernels",
            "CM kernel cache",
            k_state,
            "%d kernel(s)" % len(kernels),
            "dir: %s\nnewest: %s\ncompile log failures: %d%s"
            % (
                str(kernel_dir),
                _age_text(newest),
                failures,
                ("\nlast failure: %s" % last_failure) if last_failure else "",
            ),
            str(kernel_dir),
        )
    )

    trace = find_trace_log(prefixes)
    if trace:
        items.append(
            ind(
                "xmx.trace",
                "igdext_trace.log",
                "ok" if time.time() - trace["mtime"] < 7 * 86400 else "warn",
                "%s, %s" % (_stat_size(trace["path"]), _age_text(trace["mtime"])),
                "path: %s" % trace["path"],
                trace["path"],
            )
        )
    else:
        items.append(
            ind(
                "xmx.trace",
                "igdext_trace.log",
                missing_state(),
                "not found in the prefix",
                "The shim writes C:\\igdext_trace.log only when tracing is enabled; "
                "absence is expected on a plain launch.",
                
                ", ".join(str(p) for p in prefixes[:3]) or "no prefix found",
            )
        )

    if benchmark and has_game and not xess_title:
        items.append(
            ind(
                "xmx.benchmark",
                "latest Cyberpunk benchmark",
                "na",
                "not for this title",
                "The newest summary is from Cyberpunk 2077; it is not evidence for the "
                "running game: %s" % benchmark.get("path"),
                benchmark.get("path"),
            )
        )
    elif benchmark:
        label = benchmark.get("upscaling_label") or "type %s" % benchmark.get("upscaling_type")
        xess_ok = benchmark.get("xess") is True
        state = "ok" if xess_ok else "warn"
        value = "%s (upscalingType %s), avg %.1f FPS" % (
            label,
            benchmark.get("upscaling_type"),
            benchmark.get("average_fps") or 0.0,
        )
        items.append(
            ind(
                "xmx.benchmark",
                "latest Cyberpunk benchmark",
                state,
                value,
                "file: %s\nrun: %s\navg/min/max: %.1f / %.1f / %.1f FPS\nresolution: %s\npreset: %s\nGPU: %s"
                % (
                    benchmark.get("path"),
                    benchmark.get("benchmark_name"),
                    benchmark.get("average_fps") or 0.0,
                    benchmark.get("min_fps") or 0.0,
                    benchmark.get("max_fps") or 0.0,
                    benchmark.get("resolution"),
                    benchmark.get("preset"),
                    benchmark.get("gpu"),
                ),
                benchmark.get("path"),
            )
        )
    else:
        items.append(
            ind(
                "xmx.benchmark",
                "latest Cyberpunk benchmark",
                "na" if (has_game and not xess_title) else "unknown",
                "no benchmark summary found",
                "Searched Cyberpunk benchmarkResults under the known Heroic/Steam prefixes.",
                None,
            )
        )
    return items


def _fix_indicators(
    home: Path, game: Optional[Dict[str, Any]], heroic: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    modinfo = _run(["modinfo", "-F", "filename", "xe"], timeout=2.0)
    if modinfo and modinfo[0] == 0:
        filename = modinfo[1].strip()
        layered = "/updates/" in filename
        items.append(
            ind(
                "xe.module",
                "patched xe module",
                "ok" if layered else "fail",
                "layered (%s)" % os.path.basename(filename) if layered else "stock module",
                "modinfo -F filename xe: %s" % filename,
                "modinfo",
            )
        )
    else:
        items.append(
            ind("xe.module", "patched xe module", "unknown", "modinfo unavailable", None, "modinfo")
        )

    items.append(_unit_indicator("rapl.service", "RAPL powercap service", RAPL_UNIT))
    rapl_values: Dict[str, float] = {}
    for name in ("package-0", "core", "uncore"):
        value = _read_float(RUN_ROOT + "/rapl-power/%s" % name)
        if value is not None:
            rapl_values[name] = value
    if rapl_values:
        items.append(
            ind(
                "rapl.values",
                "RAPL power now",
                "ok",
                ", ".join("%s %.1f W" % (k, v) for k, v in sorted(rapl_values.items())),
                "1 s averages published unprivileged by rapl-power.",
                RUN_ROOT + "/rapl-power",
            )
        )
    else:
        items.append(
            ind(
                "rapl.values",
                "RAPL power now",
                "unknown",
                "no /run/rapl-power values",
                "rapl-power.service publishes these for MangoHud and this plugin.",
                RUN_ROOT + "/rapl-power",
            )
        )

    pp1 = unit_state(RAPL_PP1_UNIT)
    uncore_ok = "uncore" in rapl_values
    items.append(
        ind(
            "rapl.pp1",
            "oxp3-rapl-pp1.service",
            "ok" if uncore_ok else ("warn" if pp1["LoadState"] != "not-found" else "unknown"),
            pp1["ActiveState"] + ("; uncore zone present" if uncore_ok else "; no uncore zone"),
            "LoadState=%s; unit reloads intel_rapl_msr once at boot when the GPU zone is missing."
            % pp1["LoadState"],
            "systemctl show %s" % RAPL_PP1_UNIT,
        )
    )

    pl1_msr = _read_int(_sys("/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"))
    pl1_mmio = _read_int(_sys("/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw"))
    values = [v for v in (pl1_msr, pl1_mmio) if v is not None]
    if not values:
        items.append(
            ind("power.pl1", "PL1 power limit", "unknown", "unreadable", None, "intel-rapl")
        )
    else:
        ok = all(abs(v - EXPECTED_PL1_W * 1_000_000) < 1000 for v in values)
        items.append(
            ind(
                "power.pl1",
                "PL1 power limit",
                "ok" if ok else "warn",
                " / ".join("%.2f W" % (v / 1_000_000) for v in values),
                "expected %d W; MSR=%s, MMIO=%s"
                % (
                    EXPECTED_PL1_W,
                    ("%.2f W" % (pl1_msr / 1_000_000)) if pl1_msr is not None else "unreadable",
                    ("%.2f W" % (pl1_mmio / 1_000_000)) if pl1_mmio is not None else "unreadable",
                ),
                "intel-rapl:0 / intel-rapl-mmio:0",
            )
        )

    profile = (_read_text(_sys("/firmware/acpi/platform_profile"), 64) or "").strip()
    choices = (_read_text(_sys("/firmware/acpi/platform_profile_choices"), 128) or "").strip()
    if not profile:
        items.append(
            ind("power.profile", "platform_profile", "unknown", "unreadable", None, "ACPI")
        )
    else:
        items.append(
            ind(
                "power.profile",
                "platform_profile",
                "ok" if profile == "balanced" else "warn",
                profile,
                "The recorded working build uses balanced; choices: %s" % (choices or "unknown"),
                _sys("/firmware/acpi/platform_profile"),
            )
        )

    true_games = [cfg for cfg in heroic if cfg.get("use_game_mode")]
    gamemoderun = [
        cfg
        for cfg in heroic
        if any("gamemoderun" in str(w) for w in cfg.get("wrappers", []))
    ]
    if not heroic:
        items.append(
            ind(
                "heroic.gamemode",
                "Heroic GameMode",
                "unknown",
                "no Heroic configs found",
                None,
                str(home / HEROIC_CONFIG_REL),
            )
        )
    else:
        state = "ok" if not true_games and not gamemoderun else "warn"
        value = (
            "off for all %d game(s)" % len(heroic)
            if state == "ok"
            else "%d useGameMode, %d gamemoderun wrapper(s)" % (len(true_games), len(gamemoderun))
        )
        items.append(
            ind(
                "heroic.gamemode",
                "Heroic GameMode",
                state,
                value,
                "gamemoderun costs ~12-15 W on the 35 W package (41.2 -> 53.2 FPS when disabled)."
                if state != "ok"
                else "No Heroic game enables GameMode.",
                str(home / HEROIC_CONFIG_REL),
            )
        )

    overrides = Path(RUNTIME_OVERRIDES_PATH)
    runtime_driver = overrides / "lib64/libvulkan_intel.so"
    if runtime_driver.is_file():
        prefix = sha256_prefix(runtime_driver)
        state = "ok" if prefix == RUNTIME_DRIVER_EXPECTED else "warn"
        expat = (overrides / "lib64/libexpat.so.1").exists()
        items.append(
            ind(
                "flatpak.runtime",
                "Flatpak runtime override",
                state,
                "%s%s" % (prefix or "unreadable", " (recorded)" if state == "ok" else " (unexpected)"),
                "path: %s\nlibexpat override present: %s\n"
                "Restart anatase-flatpak-extensions.service after changing this."
                % (runtime_driver, expat),
                str(runtime_driver),
            )
        )
    else:
        items.append(
            ind(
                "flatpak.runtime",
                "Flatpak runtime override",
                "fail",
                "missing",
                "expected %s with the patched driver" % runtime_driver,
                str(overrides),
            )
        )

    steam_env = parse_flatpak_override(home / STEAM_OVERRIDE_REL)
    if steam_env:
        helper = steam_env.get("ANV_CM_HELPER", "")
        state = "ok" if XMX_HELPER_MARKER in helper else "warn"
        items.append(
            ind(
                "flatpak.steam_env",
                "Steam Flatpak XMX env",
                state,
                os.path.basename(helper) if helper else "no ANV_CM_HELPER",
                "Flatpak Steam is not supported by the XMX project; the override currently uses %s "
                "(-doubleGRF kernels) and VKD3D_DISABLE_EXTENSIONS=%s."
                % (helper or "no helper", steam_env.get("VKD3D_DISABLE_EXTENSIONS", "-")),
                str(home / STEAM_OVERRIDE_REL),
            )
        )
    return items


def _system_indicators(home: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    kernel = os.uname().release
    os_name = ""
    try:
        text = (Path(ETC_ROOT + "/os-release").read_text(encoding="utf-8", errors="replace"))
        for line in text.splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    items.append(
        ind(
            "sys.kernel",
            "kernel",
            "ok",
            kernel,
            "os: %s" % (os_name or "unknown"),
            "uname",
        )
    )

    policies = sorted(Path(_sys("/devices/system/cpu/cpufreq")).glob("policy*"))
    governors: Dict[str, int] = {}
    epps: Dict[str, int] = {}
    detail_lines = []
    for policy in policies:
        gov = (_read_text(policy / "scaling_governor", 64) or "unknown").strip()
        epp = (_read_text(policy / "energy_performance_preference", 64) or "unknown").strip()
        governors[gov] = governors.get(gov, 0) + 1
        epps[epp] = epps.get(epp, 0) + 1
        if len(detail_lines) < 24:
            detail_lines.append("%s: governor=%s epp=%s" % (policy.name, gov, epp))
    if not policies:
        items.append(
            ind("sys.cpu", "CPU governor / EPP", "unknown", "cpufreq not available", None, SYS_ROOT)
        )
    else:
        expected = (
            set(governors) == {"powersave"} and set(epps) == {"balance_power"}
        )
        summary = "governor " + ", ".join(
            "%s x%d" % (k, v) for k, v in sorted(governors.items())
        )
        summary += "; EPP " + ", ".join("%s x%d" % (k, v) for k, v in sorted(epps.items()))
        items.append(
            ind(
                "sys.cpu",
                "CPU governor / EPP",
                "ok" if expected else "warn",
                summary,
                "Recorded good config: powersave + balance_power (per-cluster EPP measured worse).\n"
                + "\n".join(detail_lines),
                _sys("/devices/system/cpu/cpufreq"),
            )
        )

    pci_id = ""
    driver = ""
    for render in sorted(Path(_sys("/class/drm")).glob("renderD*")):
        uevent = _read_text(render / "device/uevent", 4096) or ""
        if "DRIVER=" in uevent:
            for line in uevent.splitlines():
                if line.startswith("PCI_ID="):
                    pci_id = line.split("=", 1)[1].strip()
                if line.startswith("DRIVER="):
                    driver = line.split("=", 1)[1].strip()
            break
    version = mesa_version(DRIVER_PATH)
    rpm_version = None
    rpm = _run(["rpm", "-q", "--qf", "%{VERSION}", "mesa-vulkan-drivers"], timeout=2.0)
    if rpm and rpm[0] == 0 and rpm[1].strip():
        rpm_version = rpm[1].strip().splitlines()[0]
    if driver:
        items.append(
            ind(
                "sys.gpu",
                "GPU / Mesa",
                "ok",
                "%s (%s), Mesa %s" % (pci_id or "PCI", driver, version or "unknown"),
                "host driver reports Mesa %s; rpm mesa-vulkan-drivers=%s "
                "(the runtime/host override is why these differ)"
                % (version or "unknown", rpm_version or "unknown"),
                DRIVER_PATH,
            )
        )
    else:
        items.append(
            ind("sys.gpu", "GPU / Mesa", "unknown", "no DRM render node", None, _sys("/class/drm"))
        )

    temps: List[Tuple[str, float]] = []
    try:
        hwmons = sorted(Path(_sys("/class/hwmon")).glob("hwmon*"))
    except OSError:
        hwmons = []
    for hwmon in hwmons:
        name = (_read_text(hwmon / "name", 64) or "").strip()
        if name not in ("coretemp", "xe", "amdgpu", "acpitz"):
            continue
        for temp_input in sorted(hwmon.glob("temp*_input")):
            value = _read_int(temp_input)
            if value is None:
                continue
            label = (_read_text(str(temp_input).replace("_input", "_label"), 64) or "").strip()
            label = label or name
            temps.append(("%s %s" % (name, label), value / 1000.0))
    zones = []
    try:
        for zone in sorted(Path(_sys("/class/thermal")).glob("thermal_zone*")):
            ztype = (_read_text(zone / "type", 64) or "").strip()
            value = _read_int(zone / "temp")
            if value is not None and ztype in ("x86_pkg_temp", "TCPU"):
                zones.append((ztype, value / 1000.0))
    except OSError:
        pass
    package = None
    for label, value in temps:
        if "Package id 0" in label:
            package = value
            break
    max_temp = max([v for _, v in temps + zones], default=None)
    if max_temp is None:
        items.append(
            ind("sys.thermal", "temperature", "unknown", "no sensors readable", None, _sys("/class/hwmon"))
        )
    else:
        state = "ok" if max_temp < 90 else ("warn" if max_temp < 100 else "fail")
        items.append(
            ind(
                "sys.thermal",
                "temperature",
                state,
                "CPU package %.0f°C, max %.0f°C" % (package, max_temp) if package else "max %.0f°C" % max_temp,
                "\n".join("%s: %.1f°C" % (label, value) for label, value in temps[:12]),
                _sys("/class/hwmon"),
            )
        )

    battery = None
    try:
        battery = next(
            (p for p in sorted(Path(_sys("/class/power_supply")).glob("BAT*"))), None
        )
    except OSError:
        battery = None
    if battery:
        capacity = _read_int(battery / "capacity")
        status = (_read_text(battery / "status", 32) or "").strip()
        power = _read_int(battery / "power_now")
        watts = None
        if power is not None:
            watts = power / 1_000_000.0
        else:
            current = _read_int(battery / "current_now")
            voltage = _read_int(battery / "voltage_now")
            if current is not None and voltage is not None:
                watts = current * voltage / 1e12
        items.append(
            ind(
                "sys.battery",
                "battery",
                "ok" if capacity is not None else "unknown",
                "%s%%, %s%s" % (capacity, status or "?",
                                (", %.1f W" % watts) if watts else ""),
                "path: %s" % battery,
                str(battery),
            )
        )
    else:
        items.append(
            ind("sys.battery", "battery", "unknown", "no battery", None, _sys("/class/power_supply"))
        )
    return items


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def collect_status(home_override: Optional[str] = None) -> Dict[str, Any]:
    notes: List[str] = []
    home, home_notes = resolve_home(home_override)
    notes.extend(home_notes)

    game = None
    try:
        game = detect_game(home)
    except Exception as exc:  # never let process scanning break the report
        notes.append("game detection failed: %s" % exc)
    if game is None:
        notes.append(
            "no live game process visible; XMX environment is reported from the configured "
            "launcher (a pressure-vessel Flatpak game may live in another PID namespace)"
        )

    heroic = read_heroic_games(home)
    sources, merged_env = build_env_sources(home, game, heroic)
    prefixes = candidate_prefixes(home, game, heroic)
    benchmark = find_latest_benchmark(prefixes)
    parsed_benchmark = parse_benchmark(benchmark["path"]) if benchmark else None

    # Cyberpunk 2077 is the recorded XeSS reference title (appid 1423049311 in
    # the Heroic direct-Proton path); for any other running game the XMX rows
    # describe a stack that is intentionally not armed.
    xess_title = bool(
        game
        and (
            "cyberpunk" in (game.get("name") or "").lower()
            or (game.get("appid") or "") == "1423049311"
        )
    )

    groups: List[Dict[str, Any]] = []
    builders = [
        ("mesa", "Mesa driver", lambda: _mesa_indicators(home, merged_env, game)),
        (
            "xmx",
            "XeSS XMX",
            lambda: _xmx_indicators(
                home, merged_env, prefixes, parsed_benchmark, xess_title, game is not None
            ),
        ),
        ("sched", "CPU scheduler", _sched_indicators),
        ("fixes", "Other fixes", lambda: _fix_indicators(home, game, heroic)),
        ("system", "System", lambda: _system_indicators(home)),
    ]
    for gid, title, build in builders:
        try:
            indicators = build()
        except Exception as exc:  # defensive: one broken probe must not blank the UI
            indicators = [
                ind(gid + ".error", "%s checks" % title, "unknown", "check failed: %s" % exc)
            ]
        groups.append(group(gid, title, indicators))

    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0, "na": 0}
    for grp in groups:
        for item in grp["indicators"]:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
    if counts["fail"]:
        overall = "fail"
    elif counts["warn"]:
        overall = "warn"
    elif counts["unknown"]:
        overall = "unknown"
    else:
        overall = "ok"

    try:
        username = pwd.getpwuid(home.stat().st_uid).pw_name
    except (OSError, KeyError):
        username = str(home)

    return {
        "schema": 1,
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": overall,
        "counts": counts,
        "host": {
            "user": username,
            "home": str(home),
            "kernel": os.uname().release,
            "hostname": os.uname().nodename,
        },
        "game": sanitize_game(game),
        # Runtime truth for XeSS/XMX: mapped libxess, mapped XMX driver, shim.
        "xess": collect_xess(home),
        "env_sources": [
            {"name": src["name"], "live": src["live"], "keys": len(src["env"])}
            for src in sources
        ],
        "groups": groups,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Overview: the two lines at the top of the panel.
#
#   health  one headline derived from `tools/oxp3-verify.sh --json` (FAIL first,
#           then UNKNOWN, then SKIPs that carry an actionable hint) plus the
#           counts and the short issue list the panel expands.
#   xess    whether the running game loads XeSS and whether the XMX stack
#           (ICD, driver, helper or shim) is actually part of that launch.
#
# Both are read-only and defensive: a missing verifier, an unreadable /proc
# entry or a game that lives in another PID namespace degrades to an explicit
# label, never to an exception and never to a claim that a SKIP/UNKNOWN check
# is working.
# ---------------------------------------------------------------------------
_OVERVIEW_ISSUE_LIMIT = 5
_OVERVIEW_HEADLINE_IDS = 3
_OVERVIEW_HEALTH_TTL = 90.0
_OVERVIEW_XESS_TTL = 3.0
_OVERVIEW_HEALTH_CACHE: Dict[str, Any] = {"ts": 0.0, "value": None}
_OVERVIEW_XESS_CACHE: Dict[str, Any] = {"key": None, "ts": 0.0, "value": None}

# Environment keys that can carry a Vulkan layer or ICD referencing XeSS.
_XESS_ENV_KEYS = (
    "VK_ICD_FILENAMES",
    "VK_DRIVER_FILES",
    "VK_LAYER_PATH",
    "VK_ADD_LAYER_PATH",
    "VK_INSTANCE_LAYERS",
    "VK_LOADER_LAYERS_ENABLE",
)
# The per-game ICD files the XMX work installs (`docs/hw/xess-xmx.md` and the
# Flatpak variant in `docs/hw/flatpak-xmx.md`).
_XMX_ICD_MARKERS = ("icd-xmx.json", "icd-flatpak.json")
# Environment that points at the XMX driver/helper/shim stack.
_XMX_ENV_KEYS = (
    "LD_PRELOAD",
    "ANV_CM_HELPER",
    "ANV_CM_KERNEL_DIR",
    "XMX_IGC_DIR",
    "INTC_ALT_DRIVER_EXTENSIONS_PATH",
    "OXP3_XMX_ENV_FILE",
)
_STEAM_ANCESTOR_MARKERS = ("steam", "reaper", "gamescope", "proton")


def _overview_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _overview_short(value: Any, limit: int = 120) -> str:
    text = _as_text(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _overview_zero_counts() -> Dict[str, int]:
    return {"total": 0, "pass": 0, "fail": 0, "skip": 0, "unknown": 0}


def _overview_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and value >= 0 else default


# -- health -----------------------------------------------------------------
def _health_check_items(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Normalise a raw schema-1 verify report or the compact run result."""
    items: List[Dict[str, str]] = []
    raw_checks = payload.get("checks")
    if isinstance(raw_checks, list):
        for check in raw_checks:
            if not isinstance(check, dict):
                continue
            state = _as_text(check.get("state")).strip().lower()
            items.append(
                {
                    "id": _overview_short(check.get("id") or "?", 80),
                    "state": state,
                    "message": _overview_short(
                        check.get("summary") or check.get("title") or "", 160
                    ),
                    "hint": _overview_short(
                        check.get("remediation") or check.get("hint") or "", 240
                    ),
                }
            )
        return items
    for key, state in (
        ("failures", "fail"),
        ("unknowns", "unknown"),
        ("skips", "skip"),
    ):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "id": _overview_short(item.get("id") or "?", 80),
                    "state": state,
                    "message": _overview_short(
                        item.get("summary") or item.get("title") or "", 160
                    ),
                    "hint": _overview_short(item.get("hint") or "", 240),
                }
            )
    return items


def _health_counts(payload: Dict[str, Any], items: List[Dict[str, str]]) -> Dict[str, int]:
    if isinstance(payload.get("checks"), list):
        return _verification_counts(payload)
    declared = payload.get("counts") or {}
    counts = {
        "pass": _overview_int(declared.get("pass")),
        "fail": _overview_int(declared.get("fail")),
        "skip": _overview_int(declared.get("skip")),
        "unknown": _overview_int(declared.get("unknown")),
    }
    total = declared.get("total")
    counts["total"] = _overview_int(total, sum(counts.values()))
    return counts


def _health_headline(ids: List[str], prefix: str) -> str:
    shown = ids[:_OVERVIEW_HEADLINE_IDS]
    hidden = len(ids) - len(shown)
    text = "%s%s" % (prefix, ", ".join(shown))
    if hidden > 0:
        text += " +%d more" % hidden
    return _overview_short(text, 180)


def build_health(
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    *,
    checked_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Turn one verify payload (raw schema-1 report or compact result) into
    the two-line panel contract. Pure: no I/O, so it is directly testable."""
    if not isinstance(payload, dict):
        payload = {}
    items = _health_check_items(payload)
    counts = _health_counts(payload, items)
    total = counts["total"]
    fail_items = [item for item in items if item["state"] == "fail"]
    unknown_hinted = [
        item for item in items if item["state"] == "unknown" and item["hint"]
    ]
    unknown_plain = [
        item for item in items if item["state"] == "unknown" and not item["hint"]
    ]
    skip_hinted = [item for item in items if item["state"] == "skip" and item["hint"]]
    unknown_items = unknown_hinted + unknown_plain

    if error is not None or (not items and total <= 0):
        message = ""
        if isinstance(error, dict):
            message = _overview_short(error.get("message") or error.get("code") or "", 140)
        if not message:
            message = "oxp3-verify.sh returned no checks"
        return {
            "schema": 1,
            "state": "error",
            "headline": _overview_short("Verification unavailable: " + message, 180),
            "summary": "the acceptance harness could not be read",
            "counts": _overview_zero_counts(),
            "issues": [],
            "issues_total": 0,
            "issue_overflow": 0,
            "available": False,
            "error": error
            if isinstance(error, dict)
            else {"code": "no-checks", "message": message},
            "checked_at": checked_at or _overview_iso(),
            "mode": _as_text(payload.get("mode") or ""),
            "cached": False,
            "stale": False,
        }

    if counts["fail"] > 0:
        state = "fail"
        if fail_items:
            headline = _health_headline(
                [item["id"] for item in fail_items], "Needs attention: "
            )
        else:
            headline = "Needs attention: %d failing check(s)" % counts["fail"]
        ranked = fail_items + unknown_hinted + unknown_plain + skip_hinted
    elif counts["unknown"] > 0:
        state = "warn"
        if unknown_items:
            headline = _health_headline(
                [item["id"] for item in unknown_items], "Needs attention: "
            )
        else:
            headline = "Needs attention: %d check(s) could not be verified" % counts[
                "unknown"
            ]
        ranked = unknown_hinted + unknown_plain + skip_hinted
    elif counts["pass"] > 0:
        state = "ok"
        headline = "All fixes applied and working"
        ranked = []
    else:
        state = "warn"
        headline = "Nothing verified: %d check(s) were skipped" % counts["skip"]
        ranked = []

    issues = ranked[:_OVERVIEW_ISSUE_LIMIT]
    overflow = max(0, len(ranked) - len(issues))
    summary = "%d fail · %d unknown · %d skip · %d pass" % (
        counts["fail"],
        counts["unknown"],
        counts["skip"],
        counts["pass"],
    )
    return {
        "schema": 1,
        "state": state,
        "headline": headline,
        "summary": summary,
        "counts": counts,
        "issues": issues,
        "issues_total": len(ranked),
        "issue_overflow": overflow,
        "available": True,
        "error": None,
        "checked_at": _as_text(payload.get("finished_at") or payload.get("generated_at"))
        or checked_at
        or _overview_iso(),
        "mode": _as_text(payload.get("mode") or ""),
        "cached": False,
        "stale": False,
    }


def _health_timeout() -> float:
    raw = os.environ.get("OXP3_OVERVIEW_TIMEOUT", "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 120.0
    return max(10.0, min(value, 600.0))


def collect_health(
    verify_report: Optional[Dict[str, Any]] = None,
    *,
    verifier: Optional[str] = None,
    timeout: Optional[float] = None,
    use_cache: bool = True,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Headline health. Runs the verifier at most once per TTL; `refresh`
    reuses the last manual verification result instead of running it again."""
    if isinstance(verify_report, dict):
        return build_health(verify_report)

    now = time.monotonic()
    cached = _OVERVIEW_HEALTH_CACHE.get("value")
    if use_cache and not refresh and isinstance(cached, dict):
        if now - float(_OVERVIEW_HEALTH_CACHE.get("ts") or 0.0) < _OVERVIEW_HEALTH_TTL:
            out = dict(cached)
            out["cached"] = True
            return out

    last = _VERIFY_LAST
    last_fresh = (
        isinstance(last, dict)
        and last.get("ok")
        and now - float(_VERIFY_LAST_TS or 0.0) < _OVERVIEW_HEALTH_TTL
    )
    if refresh and last_fresh:
        health = build_health(last)
    else:
        result = run_verification_once(
            "quick", verifier=verifier, timeout=timeout if timeout else _health_timeout()
        )
        if result.get("ok"):
            health = build_health(result)
        else:
            err = result.get("error") or {
                "code": "unknown",
                "message": "verification failed",
            }
            if err.get("code") == "busy" and isinstance(last, dict) and last.get("ok"):
                health = build_health(last)
                health["stale"] = True
            else:
                health = build_health(None, err)

    if use_cache and health.get("state") != "error":
        _OVERVIEW_HEALTH_CACHE["ts"] = time.monotonic()
        _OVERVIEW_HEALTH_CACHE["value"] = dict(health)
    return health


# -- live XeSS/XMX detection ------------------------------------------------
def _proc_leaf_text(pid: int, leaf: str, limit: int = 262144) -> Optional[str]:
    return _read_text(_proc_entry(pid, leaf), limit)


def _proc_ppid(pid: int) -> Optional[int]:
    text = _proc_leaf_text(pid, "stat", 8192)
    if text and ")" in text:
        fields = text.rsplit(")", 1)[1].split()
        if len(fields) >= 2:
            try:
                return int(fields[1])
            except ValueError:
                pass
    text = _proc_leaf_text(pid, "status", 16384)
    if text:
        for line in text.splitlines():
            if line.startswith("PPid:"):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    return None
    return None


def _clean_map_path(path: str) -> str:
    value = path.strip()
    if value.endswith(" (deleted)"):
        value = value[: -len(" (deleted)")]
    return value if value.startswith("/") else ""


def _proc_map_paths(pid: int) -> List[str]:
    """Absolute paths mapped by a process, from map_files and maps. Unreadable
    entries are skipped; the result is deduplicated and order-preserving."""
    paths: List[str] = []
    seen = set()

    def add(value: str) -> None:
        path = _clean_map_path(value)
        if not path or path in seen:
            return
        seen.add(path)
        paths.append(path)

    base = _proc_entry(pid, "map_files")
    try:
        for name in os.listdir(base):
            try:
                target = os.readlink(os.path.join(base, name))
            except OSError:
                continue
            add(target)
    except OSError:
        pass
    text = _proc_leaf_text(pid, "maps", 2 * 1024 * 1024)
    if text:
        for line in text.splitlines():
            fields = line.split(None, 5)
            if len(fields) >= 6:
                add(fields[5])
    return paths


def _is_xess_library(path: str) -> bool:
    name = os.path.basename(path).lower()
    if not name:
        return False
    if name.startswith("libxess"):
        return True
    return name.startswith("xess") and ".so" in name


def _is_xmx_driver(path: str, home: Path) -> bool:
    if not path:
        return False
    low = path.lower()
    if "xmx-driver" in low or "/xess-xmx/" in low:
        return True
    try:
        target = os.path.realpath(str(home / XMX_DRIVER_REL))
        return os.path.realpath(path) == target
    except OSError:
        return False


def _env_xess_hits(env: Dict[str, str]) -> List[str]:
    hits: List[str] = []
    for key in _XESS_ENV_KEYS:
        value = env.get(key) or ""
        if "xess" in value.lower():
            hits.append("%s=%s" % (key, _overview_short(value, 100)))
    return hits


def _env_xmx_hit(env: Dict[str, str], home: Path) -> str:
    for key in ("VK_ICD_FILENAMES", "VK_DRIVER_FILES"):
        value = env.get(key) or ""
        if not value:
            continue
        low = value.lower()
        if any(marker in low for marker in _XMX_ICD_MARKERS):
            return "%s=%s" % (key, _overview_short(value, 100))
        if "xess-xmx" in low or "xmx-driver" in low:
            return "%s=%s" % (key, _overview_short(value, 100))
        for item in resolve_icd_entry(value, home):
            library = item.get("library") or ""
            if _is_xmx_driver(library, home):
                return "%s=%s -> XMX driver" % (key, _overview_short(value, 90))
    for key in _XMX_ENV_KEYS:
        value = env.get(key) or ""
        low = value.lower()
        if "xmx" in low or "xess-xmx" in low or "igdext" in low:
            return "%s=%s" % (key, _overview_short(value, 100))
    return ""


def _process_tree(pid: int, ppid_map: Dict[int, Optional[int]]) -> List[int]:
    children: Dict[int, List[int]] = {}
    for child, parent in ppid_map.items():
        if parent is not None:
            children.setdefault(parent, []).append(child)
    out: List[int] = []
    seen = set()
    stack = [pid]
    while stack and len(out) < 512:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        stack.extend(children.get(current) or [])
    return out


def _steam_ancestor(
    pid: int,
    ppid_map: Dict[int, Optional[int]],
    comm_cache: Dict[int, str],
    cmd_cache: Dict[int, List[str]],
) -> bool:
    current = ppid_map.get(pid)
    seen = set()
    while current and current not in seen and len(seen) < 8:
        seen.add(current)
        if current not in comm_cache:
            comm_cache[current] = _proc_comm(current)
        if current not in cmd_cache:
            cmd_cache[current] = _proc_cmdline(current)
        blob = (comm_cache[current] + " " + " ".join(cmd_cache[current])).lower()
        if any(marker in blob for marker in _STEAM_ANCESTOR_MARKERS):
            return True
        current = ppid_map.get(current)
    return False


def _game_name(pid: int, cmdline: List[str], comm: str) -> Optional[str]:
    for arg in cmdline:
        if arg.lower().endswith(".exe"):
            return _pretty_game_name(arg.replace("\\", "/"))
    for arg in cmdline:
        match = re.search(r"/steamapps/common/([^/]+)/", arg.replace("\\", "/"), re.I)
        if match:
            return match.group(1).replace("_", " ").strip()
    if comm and comm.lower() not in _WINE_COMMS and comm.lower() not in (
        "steam",
        "steamwebhelper",
        "reaper",
        "python3",
        "bash",
        "sh",
    ):
        return _pretty_game_name(comm)
    return None


def _process_signals(pid: int) -> Dict[str, Any]:
    env = _proc_environ(pid)
    maps = _proc_map_paths(pid)
    comm = _proc_comm(pid)
    cmdline = _proc_cmdline(pid)
    return {
        "pid": pid,
        "env": env,
        "maps": maps,
        "comm": comm,
        "cmdline": cmdline,
        "steam_appid": env.get("SteamAppId") or env.get("SteamGameId") or "",
        "xess_libs": [path for path in maps if _is_xess_library(path)],
        "xess_env": _env_xess_hits(env),
    }


def _xmx_hit_for(sig: Dict[str, Any], home: Path) -> str:
    hit = _env_xmx_hit(sig.get("env") or {}, home)
    if hit:
        return hit
    for path in sig.get("maps") or []:
        if _is_xmx_driver(path, home):
            return "mapped %s" % os.path.basename(path)
    return ""


def _xess_label(state: str, game: Optional[str]) -> str:
    name = game or ""
    if state == "xess-on-xmx":
        return "XeSS XMX: active (%s)" % name if name else "XeSS XMX: active"
    if state == "xess-on-standard":
        return (
            "XeSS: detected, XMX shim NOT active (%s)" % name
            if name
            else "XeSS: detected, XMX shim NOT active"
        )
    if state == "xess-off":
        return "XeSS: current game does not use it"
    return "No game running"


def _no_game(evidence: str) -> Dict[str, Any]:
    return {
        "state": "no-game",
        "game": None,
        "pid": None,
        "label": _xess_label("no-game", None),
        "evidence": _overview_short(evidence, 160),
        "xess_loaded": [],
        "xmx_evidence": "",
    }


def collect_xess(home: Optional[Path] = None, *, use_cache: bool = True) -> Dict[str, Any]:
    """Classify the running game's XeSS path. Never raises on /proc access."""
    try:
        resolved = Path(home) if home is not None else resolve_home()[0]
    except Exception:  # noqa: BLE001 - never break the panel over a home probe
        resolved = Path(os.path.expanduser("~"))
    key = (str(PROC_ROOT), str(resolved))
    now = time.monotonic()
    cached = _OVERVIEW_XESS_CACHE.get("value")
    if (
        use_cache
        and _OVERVIEW_XESS_CACHE.get("key") == key
        and isinstance(cached, dict)
        and now - float(_OVERVIEW_XESS_CACHE.get("ts") or 0.0) < _OVERVIEW_XESS_TTL
    ):
        return dict(cached)
    try:
        value = _collect_xess_uncached(resolved)
    except Exception as exc:  # noqa: BLE001 - a broken /proc must not crash Decky
        value = _no_game("XeSS detection failed: %s" % _overview_short(exc, 120))
    if use_cache:
        _OVERVIEW_XESS_CACHE["key"] = key
        _OVERVIEW_XESS_CACHE["ts"] = time.monotonic()
        _OVERVIEW_XESS_CACHE["value"] = dict(value)
    return value


def _collect_xess_uncached(home: Path) -> Dict[str, Any]:
    try:
        names = os.listdir(PROC_ROOT)
    except OSError as exc:
        return _no_game("cannot list %s: %s" % (PROC_ROOT, exc))
    pids = sorted(int(name) for name in names if name.isdigit())
    if len(pids) > 8000:
        pids = pids[-8000:]
    if not pids:
        return _no_game("no processes visible in %s" % PROC_ROOT)

    ppid_map: Dict[int, Optional[int]] = {pid: _proc_ppid(pid) for pid in pids}
    signals: Dict[int, Dict[str, Any]] = {pid: _process_signals(pid) for pid in pids}
    comm_cache: Dict[int, str] = {}
    cmd_cache: Dict[int, List[str]] = {}

    best: Optional[Tuple[int, int]] = None  # (score, pid)
    best_xess: List[str] = []
    best_xess_env: List[str] = []
    best_xmx = ""
    for pid in pids:
        sig = signals[pid]
        blob = " ".join(sig["cmdline"]).lower()
        game_shape = bool(
            sig["xess_libs"]
            or sig["comm"].lower() in _WINE_COMMS
            or any(arg.lower().endswith(".exe") for arg in sig["cmdline"])
            or "/steamapps/common/" in blob
            or "\\steamapps\\common\\" in blob
        )
        steam_child = bool(
            sig["steam_appid"]
            and _steam_ancestor(pid, ppid_map, comm_cache, cmd_cache)
        )
        if not (game_shape or steam_child):
            continue
        tree = _process_tree(pid, ppid_map)
        tree_xess: List[str] = []
        tree_xess_env: List[str] = []
        tree_xmx = ""
        for member in tree:
            msig = signals.get(member)
            if not msig:
                continue
            tree_xess.extend(msig["xess_libs"])
            tree_xess_env.extend(msig["xess_env"])
            if not tree_xmx:
                tree_xmx = _xmx_hit_for(msig, home)
        score = 0
        if sig["steam_appid"]:
            score += 20
            if _steam_ancestor(pid, ppid_map, comm_cache, cmd_cache):
                score += 10
        if sig["xess_libs"]:
            score += 40
        if tree_xess:
            score += 30
        if sig["comm"].lower() in _WINE_COMMS:
            score += 5
        if sig["xess_env"]:
            score += 5
        if best is None or (score, pid) > best:
            best = (score, pid)
            best_xess = tree_xess or list(sig["xess_libs"])
            best_xess_env = tree_xess_env or list(sig["xess_env"])
            best_xmx = tree_xmx

    if best is None:
        return _no_game("no Steam game process and no libxess mapping found")

    pid = best[1]
    sig = signals[pid]
    game = _game_name(pid, sig["cmdline"], sig["comm"])
    if best_xess or best_xess_env:
        state = "xess-on-xmx" if best_xmx else "xess-on-standard"
        if best_xess:
            evidence = "%s in pid %d" % (os.path.basename(best_xess[0]), pid)
        else:
            evidence = "%s in pid %d" % (best_xess_env[0], pid)
        if best_xmx:
            evidence += "; " + best_xmx
        else:
            evidence += "; no XMX ICD/driver/shim in the launch environment"
    else:
        state = "xess-off"
        evidence = "pid %d (%s) has no libxess mapping or XeSS layer" % (
            pid,
            game or sig["comm"] or "game",
        )
        if best_xmx:
            evidence += "; XMX stack is configured but XeSS is not loaded"
    return {
        "state": state,
        "game": game,
        "pid": pid,
        "label": _xess_label(state, game),
        "evidence": _overview_short(evidence, 180),
        "xess_loaded": [os.path.basename(path) for path in best_xess[:3]],
        "xmx_evidence": _overview_short(best_xmx, 140),
    }


def collect_overview(
    home: Optional[Path] = None,
    *,
    verifier: Optional[str] = None,
    verify_report: Optional[Dict[str, Any]] = None,
    use_cache: bool = True,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Everything the two top lines need, in one read-only payload."""
    try:
        health = collect_health(
            verify_report,
            verifier=verifier,
            use_cache=use_cache,
            refresh=refresh,
        )
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        health = build_health(
            None, {"code": "internal", "message": _overview_short(exc, 200)}
        )
    try:
        xess = collect_xess(home, use_cache=use_cache)
    except Exception as exc:  # noqa: BLE001
        xess = _no_game("XeSS detection failed: %s" % _overview_short(exc, 120))
    return {"schema": 1, "health": health, "xess": xess}


# ---------------------------------------------------------------------------
# Verification action: run tools/oxp3-verify.sh on demand.
#
# The Decky backend normally runs as root under plugin_loader.service. This
# action deliberately drops to the desktop user, uses a minimal environment,
# passes an argv list (never a shell string), serialises runs and caps both
# output and wall-clock time. It never reboots, installs or escalates itself:
# "full"/"changes" are refused unless the caller explicitly confirmed in the
# panel or the operator set OXP3_STATUS_ALLOW_ESCALATION=1.
# ---------------------------------------------------------------------------
VERIFY_MODES: Dict[str, Tuple[Tuple[str, ...], int]] = {
    "quick": (("--json",), 180),
    "full": (("--full", "--json"), 900),
    "changes": (("--full", "--with-changes", "--json"), 1200),
}
VERIFY_ESCALATING = ("full", "changes")
_VERIFY_MAX_BYTES = 1024 * 1024
_VERIFY_MAX_LINES = 20000
_VERIFY_MAX_ITEMS = 8
_VERIFY_TEXT_CAP = 240
_VERIFY_STDERR_CAP = 8192
_VERIFY_LOCK = threading.Lock()
_VERIFY_LAST: Optional[Dict[str, Any]] = None
_VERIFY_LAST_TS = 0.0


def _verify_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _verify_text(value: Any, limit: int = _VERIFY_TEXT_CAP) -> str:
    text = _as_text(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _verify_zero_counts() -> Dict[str, int]:
    return {"total": 0, "pass": 0, "fail": 0, "skip": 0, "unknown": 0}


def _verify_error(
    mode: str,
    code: str,
    message: str,
    *,
    started_ts: float,
    duration: float,
    output_truncated: bool = False,
) -> Dict[str, Any]:
    now = _verify_now()
    return {
        "schema": 1,
        "ok": False,
        "mode": mode,
        "overall": "error",
        "counts": _verify_zero_counts(),
        "exit_code": None,
        "failures": [],
        "skips": [],
        "error": {"code": code, "message": _verify_text(message, 400)},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_ts)),
        "finished_at": now,
        "duration_s": round(duration, 3),
        "root": False,
        "with_changes": False,
        "output_truncated": bool(output_truncated),
    }


def _remember_verification(result: Dict[str, Any]) -> Dict[str, Any]:
    global _VERIFY_LAST, _VERIFY_LAST_TS
    _VERIFY_LAST = result
    _VERIFY_LAST_TS = time.monotonic()
    return result


def _verification_env(
    home: Path, uid: Optional[int], user: Optional[str]
) -> Dict[str, str]:
    paths = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin"]
    local_bin = home / ".local/bin"
    try:
        if local_bin.is_dir():
            paths.insert(0, str(local_bin))
    except OSError:
        pass
    env = {
        "PATH": ":".join(paths),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SHELL": "/bin/bash",
    }
    if user:
        env["USER"] = user
        env["LOGNAME"] = user
    # systemctl --user needs the user's runtime dir and session bus even when the
    # harness is started from the plugin backend; without them the user units
    # (home button, RGB keeper, speaker gain) read as "no user systemd instance".
    runtime_uid = uid if uid is not None else os.geteuid()
    try:
        runtime = Path("/run/user/%d" % runtime_uid)
        if runtime.is_dir():
            env["XDG_RUNTIME_DIR"] = str(runtime)
            bus = runtime / "bus"
            if bus.exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s" % bus
    except OSError:
        pass
    return env


def _deck_identity(home: Path) -> Optional[Tuple[int, int, str]]:
    """The unprivileged desktop identity the harness should run as."""
    for var in ("DECKY_USER", "SUDO_USER", "USER", "LOGNAME"):
        name = os.environ.get(var, "").strip()
        if not name:
            continue
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            continue
        if pw.pw_uid != 0:
            return pw.pw_uid, pw.pw_gid, pw.pw_name
    try:
        pw = pwd.getpwuid(os.stat(str(home)).st_uid)
    except (KeyError, OSError):
        return None
    if pw.pw_uid == 0:
        return None
    return pw.pw_uid, pw.pw_gid, pw.pw_name


def _verification_script(home: Path) -> Optional[Path]:
    """Locate tools/oxp3-verify.sh: the checkout itself, an explicit
    OXP3_STATUS_REPO, or the usual desktop checkout locations."""
    roots: List[Path] = []
    explicit = os.environ.get("OXP3_STATUS_REPO", "").strip()
    if explicit:
        roots.append(Path(explicit))
    roots.append(Path(__file__).resolve().parent.parent.parent)
    roots.append(home / "git/linux_scratch/oxp3-anatase-fixes")
    roots.append(home / "oxp3-anatase-fixes")
    roots.append(home / ".local/share/oxp3-anatase-fixes")
    for root in roots:
        script = root / "tools/oxp3-verify.sh"
        try:
            if script.is_file() and os.access(str(script), os.X_OK):
                return script
        except OSError:
            continue
    return None


def _drain_stream(stream: Any, cap: int, keep_tail: bool = False) -> Tuple[bytes, bool, int]:
    """Read a child pipe while keeping at most ``cap`` bytes in memory.

    Bytes beyond the cap are read and discarded so a chatty tool can never
    block on a full pipe or grow our memory; ``keep_tail`` retains the last
    bytes (stderr diagnostics) instead of the first ones (JSON payload).
    """
    kept = bytearray() if keep_tail else []
    kept_len = 0
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if keep_tail:
                kept.extend(chunk)
                kept_len = len(kept)
                if kept_len > cap:
                    del kept[: kept_len - cap]
                    kept_len = cap
            elif kept_len < cap:
                take = chunk[: cap - kept_len]
                kept.append(take)
                kept_len += len(take)
    except (OSError, ValueError):
        pass
    if keep_tail:
        return bytes(kept), total > cap, total
    return b"".join(kept), total > cap, total


def _parse_verification(report: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(report, dict):
        return None
    if not isinstance(report.get("schema"), int):
        return None
    if not isinstance(report.get("checks"), list):
        return None
    if not isinstance(report.get("counts"), dict):
        return None
    return report


def _verification_items(
    report: Dict[str, Any],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    """(failures, skips, unknowns), each capped for the panel."""
    failures: List[Dict[str, str]] = []
    skips: List[Dict[str, str]] = []
    unknowns: List[Dict[str, str]] = []
    buckets = {"fail": failures, "skip": skips, "unknown": unknowns}
    for check in report.get("checks") or []:
        if not isinstance(check, dict):
            continue
        state = str(check.get("state") or "").lower()
        bucket = buckets.get(state)
        if bucket is None:
            continue
        bucket.append(
            {
                "id": _verify_text(check.get("id") or "?", 80),
                "title": _verify_text(check.get("title") or "", 120),
                "summary": _verify_text(check.get("summary") or "", 160),
                "hint": _verify_text(check.get("remediation") or "", _VERIFY_TEXT_CAP),
            }
        )
    return (
        failures[:_VERIFY_MAX_ITEMS],
        skips[:_VERIFY_MAX_ITEMS],
        unknowns[:_VERIFY_MAX_ITEMS],
    )


def _verification_counts(report: Dict[str, Any]) -> Dict[str, int]:
    tally = {"pass": 0, "fail": 0, "skip": 0, "unknown": 0}
    for check in report.get("checks") or []:
        if not isinstance(check, dict):
            continue
        state = str(check.get("state") or "").lower()
        if state in tally:
            tally[state] += 1
    declared = report.get("counts") or {}
    counts: Dict[str, int] = {}
    for key in ("pass", "fail", "skip", "unknown"):
        value = declared.get(key)
        counts[key] = value if isinstance(value, int) and value >= 0 else tally[key]
    counts["total"] = counts["pass"] + counts["fail"] + counts["skip"] + counts["unknown"]
    return counts


def run_verification_once(
    mode: str = "quick",
    confirm: bool = False,
    *,
    verifier: Optional[str] = None,
    timeout: Optional[float] = None,
    run_as_deck_user: bool = True,
    allow_escalation: Optional[bool] = None,
) -> Dict[str, Any]:
    """Run the verification harness once and return a compact, JSON-safe result.

    ``verifier``/``timeout``/``run_as_deck_user``/``allow_escalation`` are test
    seams; the Decky method only passes ``mode`` and ``confirm``. Never raises.
    """
    global _VERIFY_LAST

    started_ts = time.time()
    started = time.monotonic()
    mode = (mode or "quick").strip().lower()
    if mode not in VERIFY_MODES:
        return _remember_verification(
            _verify_error(
                mode,
                "bad-mode",
                "unknown verification mode %r (use quick, full or changes)" % mode,
                started_ts=started_ts,
                duration=time.monotonic() - started,
            )
        )
    flags, default_timeout = VERIFY_MODES[mode]
    if allow_escalation is None:
        allow_escalation = os.environ.get("OXP3_STATUS_ALLOW_ESCALATION", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    if mode in VERIFY_ESCALATING and not (confirm or allow_escalation):
        return _remember_verification(
            _verify_error(
                mode,
                "escalation-required",
                "%s can change live state; confirm it in the panel or set "
                "OXP3_STATUS_ALLOW_ESCALATION=1" % mode,
                started_ts=started_ts,
                duration=time.monotonic() - started,
            )
        )

    try:
        run_timeout = float(timeout) if timeout is not None else float(default_timeout)
    except (TypeError, ValueError):
        run_timeout = float(default_timeout)
    run_timeout = max(1.0, min(run_timeout, 3600.0))
    timeout_s = run_timeout

    if not _VERIFY_LOCK.acquire(blocking=False):
        return _remember_verification(
            _verify_error(
                mode,
                "busy",
                "another verification run is already in progress",
                started_ts=started_ts,
                duration=time.monotonic() - started,
            )
        )

    try:
        home, _notes = resolve_home()
        script: Optional[Path]
        if verifier:
            script = Path(verifier)
        else:
            script = _verification_script(home)
        if script is None or not script.is_file() or not os.access(str(script), os.X_OK):
            return _remember_verification(
                _verify_error(
                    mode,
                    "missing-verifier",
                    "tools/oxp3-verify.sh was not found or is not executable; set "
                    "OXP3_STATUS_REPO to the repository checkout",
                    started_ts=started_ts,
                    duration=time.monotonic() - started,
                )
            )

        identity: Optional[Tuple[int, int, str]] = None
        popen_kwargs: Dict[str, Any] = {}
        if run_as_deck_user and os.geteuid() == 0:
            identity = _deck_identity(home)
            if identity is None:
                return _remember_verification(
                    _verify_error(
                        mode,
                        "no-deck-user",
                        "refusing to run the harness as root; could not resolve an "
                        "unprivileged desktop user for %s" % home,
                        started_ts=started_ts,
                        duration=time.monotonic() - started,
                    )
                )
            uid, gid, name = identity
            popen_kwargs = {"user": uid, "group": gid, "extra_groups": []}
        else:
            name = os.environ.get("USER") or os.environ.get("LOGNAME") or ""

        child_env = _verification_env(
            home,
            identity[0] if identity else None,
            identity[2] if identity else (name or None),
        )
        repo_dir = script.resolve().parent.parent
        argv = [str(script)] + list(flags)

        timed_out = False
        rc: Optional[int] = None
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=str(repo_dir),
                env=child_env,
                start_new_session=True,
                close_fds=True,
                **popen_kwargs,
            )
        except (OSError, ValueError, TypeError) as exc:
            return _remember_verification(
                _verify_error(
                    mode,
                    "spawn-failed",
                    "could not start the verification harness: %s" % exc,
                    started_ts=started_ts,
                    duration=time.monotonic() - started,
                )
            )

        drained: Dict[str, Tuple[bytes, bool]] = {}

        def _collect(key: str, stream: Any, cap: int, keep_tail: bool) -> None:
            data, capped, _total = _drain_stream(stream, cap, keep_tail)
            drained[key] = (data, capped)

        readers = [
            threading.Thread(
                target=_collect,
                args=("stdout", proc.stdout, _VERIFY_MAX_BYTES, False),
                daemon=True,
            ),
            threading.Thread(
                target=_collect,
                args=("stderr", proc.stderr, _VERIFY_STDERR_CAP, True),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        raw, out_capped = drained.get("stdout", (b"", False))
        err_raw, err_capped = drained.get("stderr", (b"", False))
        output_truncated = bool(
            out_capped or err_capped or raw.count(b"\n") > _VERIFY_MAX_LINES
        )
        stderr_text = err_raw.decode("utf-8", "replace")

        duration = time.monotonic() - started
        stderr_tail = _verify_text(stderr_text[-400:], 400)
        if timed_out:
            return _remember_verification(
                _verify_error(
                    mode,
                    "timeout",
                    "the verification harness did not finish within %.0f s and was "
                    "killed%s" % (timeout_s, (": " + stderr_tail) if stderr_tail else ""),
                    started_ts=started_ts,
                    duration=duration,
                    output_truncated=output_truncated,
                )
            )

        report = None
        try:
            report = _parse_verification(json.loads(raw.decode("utf-8", "replace")))
        except (ValueError, UnicodeDecodeError):
            report = None

        if rc == 3:
            return _remember_verification(
                _verify_error(
                    mode,
                    "verifier-error",
                    "the verification harness exited 3 (usage or internal error)%s"
                    % ((": " + stderr_tail) if stderr_tail else ""),
                    started_ts=started_ts,
                    duration=duration,
                    output_truncated=output_truncated,
                )
            )
        if report is None:
            message = "the harness did not emit the schema-1 JSON report (exit %s)" % rc
            if stderr_tail:
                message += ": " + stderr_tail
            return _remember_verification(
                _verify_error(
                    mode,
                    "bad-json",
                    message,
                    started_ts=started_ts,
                    duration=duration,
                    output_truncated=output_truncated,
                )
            )

        counts = _verification_counts(report)
        failures, skips, unknowns = _verification_items(report)
        if rc == 1 or counts["fail"]:
            overall = "fail"
        elif rc == 2 or counts["pass"] == 0:
            overall = "skip"
        else:
            overall = "pass"
        reported = report.get("exit_code")
        result = {
            "schema": 1,
            "ok": True,
            "mode": mode,
            "overall": overall,
            "counts": counts,
            "exit_code": rc,
            "reported_exit_code": reported if isinstance(reported, int) else None,
            "failures": failures,
            "skips": skips,
            "unknowns": unknowns,
            "error": None,
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_ts)
            ),
            "finished_at": _verify_now(),
            "duration_s": round(duration, 3),
            "root": bool(report.get("root")),
            "with_changes": bool(report.get("with_changes")) or mode == "changes",
            "output_truncated": output_truncated,
        }
        return _remember_verification(result)
    except Exception as exc:  # never take the plugin host down
        return _remember_verification(
            _verify_error(
                mode,
                "internal",
                "verification failed unexpectedly: %s" % exc,
                started_ts=started_ts,
                duration=time.monotonic() - started,
            )
        )
    finally:
        _VERIFY_LOCK.release()


def get_last_verification() -> Dict[str, Any]:
    """Last compact result (or available=False); safe to call at any time."""
    last = _VERIFY_LAST
    return {
        "available": last is not None,
        "running": _VERIFY_LOCK.locked(),
        "last": dict(last) if last is not None else None,
    }


# ---------------------------------------------------------------------------
# Decky Loader backend
# ---------------------------------------------------------------------------
def _report_payload(status: Any, overview: Any) -> Dict[str, Any]:
    """One self-describing document: the panel report plus the overview."""
    return {
        "schema": 1,
        "kind": "oxp3-status-report",
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "overview": overview,
    }


class Plugin:
    """Decky Loader entry point. All methods return JSON-serialisable data."""

    async def _main(self) -> None:  # noqa: D401 - Decky lifecycle
        return None

    async def _unload(self) -> None:
        return None

    async def save_report(self) -> Dict[str, Any]:
        """Write the full JSON report into the owner's home and return the path.

        The panel used to hand the JSON to the clipboard, which is useless on a
        handheld in Gamemode and cannot be handed over for offline analysis. This
        writes ~/oxp3-reports/oxp3-report-<UTC>.json plus latest.json (same bytes)
        so the owner can attach it or point an agent at it.
        """
        try:
            loop = asyncio.get_event_loop()
            status = await loop.run_in_executor(None, collect_status)
            try:
                overview = await loop.run_in_executor(None, collect_overview)
            except Exception:  # the report is still useful without the overview
                overview = {}
            home, _notes = resolve_home()
            payload = _report_payload(status, overview)
            text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            target = Path(home) / "oxp3-reports"
            target.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            path = target / ("oxp3-report-%s.json" % stamp)
            tmp = target / (".tmp-%s" % stamp)
            tmp.write_text(text, encoding="utf-8")
            os.replace(str(tmp), str(path))
            latest = target / "latest.json"
            latest.write_text(text, encoding="utf-8")
            ident = _deck_identity(Path(home))
            if ident and os.geteuid() == 0:
                for item in (target, path, latest):
                    try:
                        os.chown(str(item), ident[0], ident[1])
                    except OSError:
                        pass
            for item in (path, latest):
                try:
                    os.chmod(str(item), 0o644)
                except OSError:
                    pass
            return {
                "ok": True,
                "path": str(path),
                "latest": str(latest),
                "bytes": len(text.encode("utf-8")),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        except Exception as exc:  # never crash the plugin host
            return {"ok": False, "error": str(exc)}

    async def get_status(self) -> Dict[str, Any]:
        # Fast hashes are cached in-process (by path+mtime+size), but the first
        # call still reads ~70 MB of drivers and shells out to systemctl.
        # Run it off the loader's event loop so other plugins stay responsive.
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, collect_status)
        except Exception as exc:  # never crash the plugin host
            return {
                "schema": 1,
                "version": VERSION,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "overall": "unknown",
                "counts": {"ok": 0, "warn": 0, "fail": 0, "unknown": 1, "na": 0},
                "host": {"user": "", "home": "", "kernel": os.uname().release},
                "game": None,
                "env_sources": [],
                "groups": [
                    group(
                        "error",
                        "Status",
                        [ind("status.error", "status collection", "unknown", str(exc))],
                    )
                ],
                "notes": ["status collection failed: %s" % exc],
            }

    async def get_overview(self, refresh: bool = False) -> Dict[str, Any]:
        """Two-line overview: fix health + the running game's XeSS/XMX path.

        Read-only. The health side runs tools/oxp3-verify.sh --json at most
        once per 90 s (cached), and `refresh=True` reuses the last manual
        verification run instead of starting another one. Every /proc read is
        bounded and skippable, so this can never take the loader down.
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: collect_overview(refresh=bool(refresh))
            )
        except Exception as exc:  # never crash the plugin host
            return {
                "schema": 1,
                "health": build_health(
                    None, {"code": "internal", "message": _overview_short(exc, 200)}
                ),
                "xess": _no_game(
                    "XeSS detection failed: %s" % _overview_short(exc, 120)
                ),
            }

    async def get_info(self) -> Dict[str, Any]:
        home, notes = resolve_home()
        return {
            "version": VERSION,
            "home": str(home),
            "notes": notes,
            "expected": {
                "host_driver_prefix": HOST_DRIVER_PREFIX,
                "xmx_driver_prefix": XMX_DRIVER_PREFIX,
                "runtime_driver_prefix": RUNTIME_DRIVER_EXPECTED,
                "pl1_watts": EXPECTED_PL1_W,
            },
        }

    async def run_verification(
        self, mode: str = "quick", confirm: bool = False
    ) -> Dict[str, Any]:
        """Run tools/oxp3-verify.sh and return the compact result.

        Off the loader event loop; the backend caps output/time, serialises
        runs and refuses full/changes unless the caller confirmed in the UI or
        the operator enabled OXP3_STATUS_ALLOW_ESCALATION=1.
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: run_verification_once(mode, confirm)
            )
        except Exception as exc:  # never crash the plugin host
            return {
                "schema": 1,
                "ok": False,
                "mode": str(mode),
                "overall": "error",
                "counts": _verify_zero_counts(),
                "exit_code": None,
                "failures": [],
                "skips": [],
                "error": {"code": "internal", "message": _verify_text(exc, 400)},
                "started_at": _verify_now(),
                "finished_at": _verify_now(),
                "duration_s": 0.0,
                "root": False,
                "with_changes": False,
                "output_truncated": False,
            }

    async def get_verification(self) -> Dict[str, Any]:
        return get_last_verification()


# ---------------------------------------------------------------------------
# Headless CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# --selftest: every check against synthetic /sys, /proc, /run and HOME trees.
# No hardware, no systemd, no privileges: the fixture scripts are ours.
# ---------------------------------------------------------------------------
_FIXTURE_GAME_PID = 4242
_FIXTURE_SCX_PID = 4343
_FIXTURE_GLOBALS = (
    "SYS_ROOT",
    "PROC_ROOT",
    "ETC_ROOT",
    "RUN_ROOT",
    "DRIVER_PATH",
    "RUNTIME_OVERRIDES_PATH",
)


def _fixture_write(path: Path, data: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


class _World:
    """A fabricated machine: sysfs/procfs/run plus one user home."""

    def __init__(
        self,
        root: Path,
        *,
        sched: str = "enabled",
        title: str = "Cyberpunk 2077",
        xmx: bool = True,
        trace: bool = True,
        benchmark: bool = True,
        corrupt: bool = False,
    ) -> None:
        self.root = root
        self.home = root / "home"
        self.sys = root / "sys"
        self.proc = root / "proc"
        self.etc = root / "etc"
        self.run = root / "run"
        self.bin = root / "bin"
        self.sched = sched
        self.title = title
        self.xmx = xmx
        self.trace = trace
        self.benchmark = benchmark
        self.corrupt = corrupt
        self.prefix = self.home / "Games/Heroic/Prefixes" / title

    # -- files ---------------------------------------------------------
    def build(self) -> "_World":
        home = self.home
        home.mkdir(parents=True, exist_ok=True)
        (self.root / "flatpak-runtime/lib64").mkdir(parents=True)
        (self.home / "usr/lib64").mkdir(parents=True)
        (home / "usr/lib64/libvulkan_intel.so").write_bytes(
            b"\x7fELF\x00fixture host driver Mesa 25.2.2 (ours, synthetic)"
        )
        (self.root / "flatpak-runtime/lib64/libvulkan_intel.so").write_bytes(
            b"\x7fELF\x00fixture runtime driver (ours, synthetic)"
        )
        self._home_files()
        self._proc()
        self._sys()
        self._etc_run()
        self._bin_scripts()
        return self

    def _home_files(self) -> None:
        home = self.home
        xmx_driver = home / XMX_DRIVER_REL
        xmx_driver.parent.mkdir(parents=True, exist_ok=True)
        xmx_driver.write_bytes(b"\x7fELF\x00fixture XMX driver (ours, synthetic)")
        icd = home / XMX_ICD_REL
        library = str(xmx_driver) if self.xmx else "/nonexistent/libvulkan_intel.so"
        payload = json.dumps({"ICD": {"library_path": library}})
        _fixture_write(icd, payload if not self.corrupt else "{not json")
        if self.xmx:
            helper = home / ".cache/xess-xmx/tools/xmx-cm-compile-nodgrf.sh"
            _fixture_write(helper, "#!/bin/sh\nexit 0\n", 0o755)
            (home / "igc/neo/bin").mkdir(parents=True, exist_ok=True)
            (home / "igc/neo/bin/ocloc").write_text("#!/bin/sh\nexit 0\n")
            os.chmod(home / "igc/neo/bin/ocloc", 0o755)
            (home / "shim").mkdir(parents=True, exist_ok=True)
            (home / "shim/igdext64.dll").write_bytes(b"fixture placeholder, not a vendor DLL")
            (home / XMX_KERNEL_CACHE_REL).mkdir(parents=True, exist_ok=True)
            (home / XMX_KERNEL_CACHE_REL / "fixture.cmk").write_bytes(b"fixture cm kernel")
            _fixture_write(home / XMX_COMPILE_LOG_REL, "compiled 1 kernel\n")
            _fixture_write(
                home / XMX_ENV_REL,
                'export ANV_CM_HELPER="$HOME/.cache/xess-xmx/tools/xmx-cm-compile-nodgrf.sh"\n',
            )
        if self.xmx:
            override_env = (
                "[Environment]\nANV_CM_HELPER=%s\nVKD3D_DISABLE_EXTENSIONS=%s\n"
                % (
                    home / ".cache/xess-xmx/tools/xmx-cm-compile-nodgrf.sh",
                    "VK_EXT_descriptor_buffer",
                )
            )
        else:
            override_env = "[Environment]\n"
        _fixture_write(home / STEAM_OVERRIDE_REL, override_env)
        self.prefix.mkdir(parents=True, exist_ok=True)
        (self.prefix / "pfx").mkdir(exist_ok=True)
        (self.prefix / "drive_c").mkdir(exist_ok=True)
        if self.trace:
            (self.prefix / "drive_c/igdext_trace.log").write_text("fixture trace\n")
        if self.benchmark:
            summary = (
                self.prefix
                / "drive_c/users/holzr/Documents/CD Projekt Red/Cyberpunk 2077/"
                "benchmarkResults/run1/summary.json"
            )
            data = {
                "Data": {
                    "XeSSEnabled": True,
                    "upscalingType": 1,
                    "averageFps": 58.25,
                    "minFps": 44.0,
                    "maxFps": 77.5,
                    "renderWidth": 1920,
                    "renderHeight": 1080,
                    "presetName": "Ray Tracing: Ultra",
                    "gpuName": "Intel Arc B390",
                }
            }
            _fixture_write(summary, json.dumps(data) if not self.corrupt else "{not json")
        heroic_dir = home / HEROIC_CONFIG_REL
        cfg = {
            "1423049311": {
                "useGameMode": False,
                "winePrefix": str(self.prefix),
                "wrapperOptions": [],
                "envVars": (
                    {
                        "VK_DRIVER_FILES": str(home / XMX_ICD_REL),
                        "VKD3D_DISABLE_EXTENSIONS": "VK_EXT_descriptor_buffer",
                    }
                    if self.xmx
                    else {}
                ),
            }
        }
        _fixture_write(heroic_dir / "fixture.json", json.dumps(cfg))

    def _environ(self) -> bytes:
        home = self.home
        appid = "1423049311" if self.title == "Cyberpunk 2077" else "620"
        pairs = [
            ("SteamAppId", appid),
            ("SteamGameId", appid),
            ("STEAM_COMPAT_DATA_PATH", str(self.prefix)),
            ("GITHUB_TOKEN", "ghp_fixture_secret_do_not_print"),
            ("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus"),
            ("SSH_AUTH_SOCK", "/tmp/ssh-fixture/agent"),
        ]
        if self.xmx:
            pairs.extend(
                [
                    ("VK_DRIVER_FILES", str(home / XMX_ICD_REL)),
                    (
                        "ANV_CM_HELPER",
                        str(home / ".cache/xess-xmx/tools/xmx-cm-compile-nodgrf.sh"),
                    ),
                    ("XMX_IGC_DIR", str(home / "igc")),
                    (
                        "INTC_ALT_DRIVER_EXTENSIONS_PATH",
                        str(home / "shim"),
                    ),
                    ("VKD3D_DISABLE_EXTENSIONS", "VK_EXT_descriptor_buffer"),
                ]
            )
        return b"\0".join(
            ("%s=%s" % (key, value)).encode("utf-8") for key, value in pairs
        ) + b"\0"

    def _proc(self) -> None:
        home = self.home
        exe_name = "Cyberpunk2077.exe" if self.title == "Cyberpunk 2077" else "portal2.exe"
        exe = str(home / "Games" / "steamapps/common" / self.title / exe_name)
        game = self.proc / str(_FIXTURE_GAME_PID)
        game.mkdir(parents=True, exist_ok=True)
        (game / "comm").write_text("wine64\n")
        (game / "cmdline").write_bytes(
            ("%s\0--api_token=fixture-supersecret\0" % exe).encode("utf-8")
        )
        (game / "environ").write_bytes(self._environ())
        scx = self.proc / str(_FIXTURE_SCX_PID)
        scx.mkdir(parents=True, exist_ok=True)
        (scx / "comm").write_text("scx_cosmos\n")
        (scx / "cmdline").write_bytes(b"scx_cosmos\0-c\00\0-p\00\0")
        (scx / "environ").write_bytes(b"")

    def _sys(self) -> None:
        _fixture_write(self.sys / "kernel/sched_ext/state", self.sched + "\n")
        _fixture_write(self.sys / "class/powercap/intel-rapl:0/constraint_0_power_limit_uw", "35000000\n")
        _fixture_write(
            self.sys / "class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw",
            "35000000\n",
        )
        _fixture_write(self.sys / "firmware/acpi/platform_profile", "balanced\n")
        _fixture_write(
            self.sys / "firmware/acpi/platform_profile_choices", "low-power balanced performance\n"
        )
        for index in range(2):
            policy = self.sys / ("devices/system/cpu/cpufreq/policy%d" % index)
            _fixture_write(policy / "scaling_governor", "powersave\n")
            _fixture_write(policy / "energy_performance_preference", "balance_power\n")
        _fixture_write(
            self.sys / "class/drm/renderD128/device/uevent",
            "DRIVER=xe\nPCI_CLASS=30000\nPCI_ID=8086:B080\n",
        )
        _fixture_write(self.sys / "class/hwmon/hwmon0/name", "coretemp\n")
        _fixture_write(self.sys / "class/hwmon/hwmon0/temp1_label", "Package id 0\n")
        _fixture_write(self.sys / "class/hwmon/hwmon0/temp1_input", "55000\n")
        _fixture_write(self.sys / "class/thermal/thermal_zone0/type", "x86_pkg_temp\n")
        _fixture_write(self.sys / "class/thermal/thermal_zone0/temp", "60000\n")
        _fixture_write(self.sys / "class/power_supply/BAT0/capacity", "88\n")
        _fixture_write(self.sys / "class/power_supply/BAT0/status", "Discharging\n")
        _fixture_write(self.sys / "class/power_supply/BAT0/power_now", "12000000\n")

    def _etc_run(self) -> None:
        _fixture_write(self.etc / "os-release", 'PRETTY_NAME="Anatase fixture"\n')
        for name in ("package-0", "core", "uncore"):
            _fixture_write(self.run / ("rapl-power/%s" % name), "12.5\n")

    def _bin_scripts(self) -> None:
        self.bin.mkdir(parents=True, exist_ok=True)
        sched_active = "active" if self.sched == "enabled" else "inactive"
        systemctl = (
            "#!/bin/sh\nunit=''\nfor a in \"$@\"; do unit=\"$a\"; done\n"
            "case \"$unit\" in\n"
            "  oxp3-gaming-sched.service) echo LoadState=loaded; "
            "echo ActiveState=%s; echo UnitFileState=enabled;;\n"
            "  rapl-power.service|oxp3-rapl-pp1.service) echo LoadState=loaded; "
            "echo ActiveState=active; echo UnitFileState=enabled;;\n"
            "  *) echo LoadState=not-found; echo ActiveState=inactive; echo UnitFileState=;;\n"
            "esac\n" % sched_active
        )
        modinfo = (
            "#!/bin/sh\n"
            "echo /lib/modules/7.2.7-an28.fc45.x86_64/updates/drivers/gpu/drm/xe/xe.ko\n"
        )
        rpm = "#!/bin/sh\necho 25.2.2\n"
        for name, body in (("systemctl", systemctl), ("modinfo", modinfo), ("rpm", rpm)):
            _fixture_write(self.bin / name, body, 0o755)


@contextlib.contextmanager
def _fixture_globals(world: "_World"):
    saved = {name: globals()[name] for name in _FIXTURE_GLOBALS}
    saved_path = os.environ.get("PATH", "")
    globals().update(
        {
            "SYS_ROOT": str(world.sys),
            "PROC_ROOT": str(world.proc),
            "ETC_ROOT": str(world.etc),
            "RUN_ROOT": str(world.run),
            "DRIVER_PATH": str(world.home / "usr/lib64/libvulkan_intel.so"),
            "RUNTIME_OVERRIDES_PATH": str(world.root / "flatpak-runtime"),
        }
    )
    os.environ["PATH"] = str(world.bin) + ":" + saved_path
    _hash_cache.clear()
    _version_cache.clear()
    try:
        yield
    finally:
        globals().update(saved)
        os.environ["PATH"] = saved_path
        _hash_cache.clear()
        _version_cache.clear()


class _Checks:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.total = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.total += 1
        if not ok:
            self.failures.append("%s%s" % (name, (": " + detail) if detail else ""))

    def eq(self, name: str, actual: Any, expected: Any) -> None:
        self.check(name, actual == expected, "got %r, want %r" % (actual, expected))


def _indicator_map(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for grp in report["groups"] for item in grp["indicators"]}


def verification_selftest() -> Tuple[int, List[str]]:
    """Stub-verifier coverage for run_verification_once. No hardware, root or
    network: each fixture is a throw-away executable that prints a synthetic
    schema-1 report (or misbehaves) and exits with the documented code."""
    global _VERIFY_LAST
    checks = _Checks()
    _VERIFY_LAST = None

    def entry(cid: str, state: str, summary: str = "", remediation: str = "") -> Dict[str, Any]:
        return {
            "id": cid,
            "group": "demo",
            "title": "demo " + cid,
            "state": state,
            "summary": summary,
            "detail": "",
            "remediation": remediation,
        }

    def report(counts: Dict[str, int], entries: List[Dict[str, Any]], exit_code: int,
               mode: str = "quick") -> str:
        return json.dumps(
            {
                "schema": 1,
                "tool": "oxp3-verify",
                "version": "1.0.0",
                "generated_at": "2026-11-09T00:00:00Z",
                "mode": mode,
                "root": False,
                "with_changes": mode == "changes",
                "kernel": "fixture",
                "counts": counts,
                "exit_code": exit_code,
                "checks": entries,
            }
        )

    with tempfile.TemporaryDirectory(prefix="oxp3-verifysel-") as tmp:
        stubs = Path(tmp)

        def stub(name: str, payload: Optional[str], rc: int = 0, sleep: float = 0.0,
                 argv_log: Optional[Path] = None, junk: int = 0) -> Path:
            lines = ["#!/usr/bin/env python3", "import sys, time"]
            if argv_log is not None:
                lines.append(
                    "open(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')" % str(argv_log)
                )
            if junk:
                lines.append("sys.stdout.write('x' * %d)" % junk)
            if sleep:
                lines.append("time.sleep(%r)" % sleep)
            if payload is not None:
                lines.append("sys.stdout.write(%r)" % payload)
            lines.append("sys.exit(%d)" % rc)
            path = stubs / name
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(path, 0o755)
            return path

        def run(script: Any, mode: str = "quick", confirm: bool = False,
                timeout: Optional[float] = None, allow: Optional[bool] = None) -> Dict[str, Any]:
            return run_verification_once(
                mode,
                confirm,
                verifier=str(script),
                timeout=timeout,
                run_as_deck_user=False,
                allow_escalation=allow,
            )

        ok = report(
            {"total": 2, "pass": 2, "fail": 0, "skip": 0, "unknown": 0},
            [entry("demo.ok", "pass"), entry("demo.two", "pass")],
            0,
        )
        res = run(stub("pass.py", ok, 0))
        checks.eq("verify: pass overall", res["overall"], "pass")
        checks.check("verify: pass ok", res["ok"] is True)
        checks.eq("verify: pass counts", res["counts"]["pass"], 2)
        checks.eq("verify: pass failures", res["failures"], [])
        checks.eq("verify: process exit", res["exit_code"], 0)
        checks.check("verify: result is JSON", bool(json.dumps(res)))
        last = get_last_verification()
        checks.check("verify: last available", last["available"] is True)
        checks.eq("verify: last overall", (last["last"] or {}).get("overall"), "pass")

        bad = report(
            {"total": 2, "pass": 1, "fail": 1, "skip": 0, "unknown": 0},
            [entry("demo.ok", "pass"), entry("demo.bad", "fail", "broken", "fix it now")],
            1,
        )
        res = run(stub("fail.py", bad, 1))
        checks.eq("verify: fail overall", res["overall"], "fail")
        checks.eq("verify: failing id", res["failures"][0]["id"], "demo.bad")
        checks.eq("verify: failing hint", res["failures"][0]["hint"], "fix it now")
        checks.eq("verify: fail counts", res["counts"]["fail"], 1)

        skipped = report(
            {"total": 2, "pass": 0, "fail": 0, "skip": 2, "unknown": 0},
            [entry("vol.unit", "skip", "not installed"), entry("bt.files", "skip")],
            2,
        )
        res = run(stub("skip.py", skipped, 2))
        checks.eq("verify: skip overall", res["overall"], "skip")
        checks.eq("verify: skip id", res["skips"][0]["id"], "vol.unit")
        checks.eq("verify: skip counts", res["counts"]["skip"], 2)

        slow = stub("slow.py", ok, 0, sleep=5.0)
        started = time.monotonic()
        res = run(slow, timeout=0.5)
        elapsed = time.monotonic() - started
        checks.eq("verify: timeout code", (res.get("error") or {}).get("code"), "timeout")
        checks.check("verify: timeout is bounded", elapsed < 4.5, "%.2fs" % elapsed)

        res = run(stub("badjson.py", "this is not json", 0))
        checks.eq("verify: bad-json code", (res.get("error") or {}).get("code"), "bad-json")
        res = run(stub("huge.py", None, 0, junk=1200000))
        checks.eq("verify: capped output code", (res.get("error") or {}).get("code"), "bad-json")
        checks.check("verify: capped output flagged", res["output_truncated"] is True)

        argv_log = stubs / "argv.log"
        flags_py = stub("flags.py", ok, 0, argv_log=argv_log)
        run(flags_py, "quick", timeout=5)
        checks.eq("verify: quick argv", argv_log.read_text(encoding="utf-8").strip(), "--json")
        argv_log.unlink()
        res = run(flags_py, "full", confirm=True, timeout=5)
        checks.eq(
            "verify: full argv",
            argv_log.read_text(encoding="utf-8").strip(),
            "--full --json",
        )
        checks.eq("verify: full mode", res["mode"], "full")
        argv_log.unlink()
        res = run(flags_py, "changes", confirm=True, timeout=5)
        checks.eq(
            "verify: changes argv",
            argv_log.read_text(encoding="utf-8").strip(),
            "--full --with-changes --json",
        )
        checks.eq("verify: changes overall", res["overall"], "pass")

        res = run(flags_py, "full", confirm=False, allow=False, timeout=5)
        checks.eq(
            "verify: escalation refused",
            (res.get("error") or {}).get("code"),
            "escalation-required",
        )
        old = os.environ.get("OXP3_STATUS_ALLOW_ESCALATION")
        os.environ["OXP3_STATUS_ALLOW_ESCALATION"] = "1"
        try:
            res = run(flags_py, "full", confirm=False, allow=None, timeout=5)
        finally:
            if old is None:
                os.environ.pop("OXP3_STATUS_ALLOW_ESCALATION", None)
            else:
                os.environ["OXP3_STATUS_ALLOW_ESCALATION"] = old
        checks.eq("verify: setting allows escalation", res["overall"], "pass")

        with _VERIFY_LOCK:
            res = run(flags_py, "quick", timeout=5)
        checks.eq("verify: busy code", (res.get("error") or {}).get("code"), "busy")

        res = run(stubs / "missing.sh")
        checks.eq(
            "verify: missing-verifier code", (res.get("error") or {}).get("code"),
            "missing-verifier",
        )
        res = run(flags_py, "bogus")
        checks.eq("verify: bad-mode code", (res.get("error") or {}).get("code"), "bad-mode")

    return checks.total, checks.failures


def _health_fixture_entry(
    cid: str, state: str, summary: str = "", hint: str = ""
) -> Dict[str, Any]:
    return {
        "id": cid,
        "group": "demo",
        "title": "demo " + cid,
        "state": state,
        "summary": summary,
        "detail": "",
        "remediation": hint,
    }


def _health_fixture_report(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"total": len(entries), "pass": 0, "fail": 0, "skip": 0, "unknown": 0}
    for entry in entries:
        if entry["state"] in counts:
            counts[entry["state"]] += 1
    return {
        "schema": 1,
        "tool": "oxp3-verify",
        "version": "1.0.0",
        "generated_at": "2026-11-09T00:00:00Z",
        "mode": "quick",
        "root": False,
        "with_changes": False,
        "kernel": "fixture",
        "counts": counts,
        "exit_code": 0,
        "checks": entries,
    }


def _proc_fixture(
    root: Path,
    pid: int,
    *,
    comm: str = "",
    args: Optional[List[str]] = None,
    environ: Optional[Dict[str, str]] = None,
    maps: str = "",
    ppid: int = 1,
) -> Path:
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "comm").write_text((comm or "fixture") + "\n", encoding="utf-8")
    (entry / "cmdline").write_bytes(
        b"\0".join(arg.encode("utf-8") for arg in (args or [])) + b"\0"
    )
    pairs = ["%s=%s" % (key, value) for key, value in (environ or {}).items()]
    (entry / "environ").write_bytes(
        b"\0".join(pair.encode("utf-8") for pair in pairs) + (b"\0" if pairs else b"")
    )
    (entry / "maps").write_text(maps, encoding="utf-8")
    (entry / "stat").write_text(
        "%d (%s) S %d 0 0 0 -1 0 0 0 0 0 0 0 0 0 0 0 0 1\n" % (pid, comm or "fixture", ppid),
        encoding="utf-8",
    )
    return entry


@contextlib.contextmanager
def _proc_fixture_globals(root: Path):
    saved = PROC_ROOT
    globals()["PROC_ROOT"] = str(root)
    _OVERVIEW_XESS_CACHE["key"] = None
    _OVERVIEW_XESS_CACHE["ts"] = 0.0
    _OVERVIEW_XESS_CACHE["value"] = None
    try:
        yield
    finally:
        globals()["PROC_ROOT"] = saved
        _OVERVIEW_XESS_CACHE["key"] = None
        _OVERVIEW_XESS_CACHE["ts"] = 0.0
        _OVERVIEW_XESS_CACHE["value"] = None


def report_selftest() -> List[str]:
    errors: List[str] = []
    payload = _report_payload({"overall": "ok"}, {"health": {"headline": "x"}})
    if payload.get("kind") != "oxp3-status-report":
        errors.append("report payload kind")
    if payload.get("version") != VERSION:
        errors.append("report payload version")
    if "generated_at" not in payload or not str(payload["generated_at"]).endswith("Z"):
        errors.append("report payload timestamp")
    if payload.get("status", {}).get("overall") != "ok":
        errors.append("report payload status")
    if payload.get("overview", {}).get("health", {}).get("headline") != "x":
        errors.append("report payload overview")
    if not callable(getattr(Plugin, "save_report", None)):
        errors.append("save_report method missing")
    return errors


def overview_selftest() -> Tuple[int, List[str]]:
    """Synthetic verify reports and synthetic /proc trees for the two top
    lines. Nothing here reads the real /proc of a game or runs the harness."""
    checks = _Checks()

    # -- health: nothing failed -> the green headline --------------------
    health = build_health(
        _health_fixture_report(
            [
                _health_fixture_entry("vol.unit", "pass"),
                _health_fixture_entry("xmx.icd", "pass"),
            ]
        )
    )
    checks.eq("health: all-pass state", health["state"], "ok")
    checks.eq(
        "health: all-pass headline", health["headline"], "All fixes applied and working"
    )
    checks.eq("health: all-pass issues", health["issues"], [])
    checks.eq("health: all-pass counts", health["counts"]["pass"], 2)

    # -- health: FAIL first, hint preserved ------------------------------
    health = build_health(
        _health_fixture_report(
            [
                _health_fixture_entry("led.sides", "pass"),
                _health_fixture_entry(
                    "vol.unit", "fail", "volume keys on the wrong sink", "install the udev rule"
                ),
                _health_fixture_entry(
                    "aud.sink", "unknown", "only auto_null is visible", "run inside the game session"
                ),
            ]
        )
    )
    checks.eq("health: fail state", health["state"], "fail")
    checks.check(
        "health: fail headline",
        health["headline"].startswith("Needs attention:")
        and "vol.unit" in health["headline"],
        health["headline"],
    )
    checks.eq("health: fail first id", health["issues"][0]["id"], "vol.unit")
    checks.eq("health: fail first hint", health["issues"][0]["hint"], "install the udev rule")
    checks.eq("health: fail counts", health["counts"]["fail"], 1)
    checks.check(
        "health: skip/unknown never counted as pass",
        health["counts"]["pass"] == 1 and health["counts"]["unknown"] == 1,
    )

    # -- health: UNKNOWN only -> amber, never "all working" --------------
    health = build_health(
        _health_fixture_report(
            [
                _health_fixture_entry(
                    "aud.sink", "unknown", "only auto_null is visible", "run in a graphical session"
                )
            ]
        )
    )
    checks.eq("health: unknown-only state", health["state"], "warn")
    checks.check(
        "health: unknown-only is not all-working",
        "All fixes applied and working" not in health["headline"],
        health["headline"],
    )
    checks.check("health: unknown-only names the check", "aud.sink" in health["headline"])
    checks.eq("health: unknown-only hint", health["issues"][0]["hint"], "run in a graphical session")

    # -- health: SKIP only -> nothing was verified -----------------------
    health = build_health(
        _health_fixture_report(
            [
                _health_fixture_entry(
                    "gfx.xmx.trace", "skip", "no trace this launch", "launch with XESS_LOG=1"
                )
            ]
        )
    )
    checks.eq("health: skip-only state", health["state"], "warn")
    checks.check(
        "health: skip-only headline",
        health["headline"].startswith("Nothing verified"),
        health["headline"],
    )
    checks.check(
        "health: skip-only not all-working",
        "All fixes applied and working" not in health["headline"],
    )

    # -- health: issue ordering ------------------------------------------
    ordered = build_health(
        _health_fixture_report(
            [
                _health_fixture_entry("skip.one", "skip", "skipped", "optional hint"),
                _health_fixture_entry("unk.one", "unknown", "cannot tell", "remedy one"),
                _health_fixture_entry("fail.one", "fail", "broken", "fix one"),
            ]
        )
    )
    checks.eq(
        "health: fail then unknown then skip",
        [item["id"] for item in ordered["issues"]],
        ["fail.one", "unk.one", "skip.one"],
    )

    # -- health: issue truncation ----------------------------------------
    many = _health_fixture_report(
        [
            _health_fixture_entry("fail.%02d" % index, "fail", "broken", "fix %d" % index)
            for index in range(8)
        ]
    )
    health = build_health(many)
    checks.eq("health: issues truncated to 5", len(health["issues"]), 5)
    checks.eq("health: issues total", health["issues_total"], 8)
    checks.eq("health: issue overflow", health["issue_overflow"], 3)
    checks.eq("health: first fail kept", health["issues"][0]["id"], "fail.00")
    checks.check(
        "health: headline truncation marker",
        "+5 more" in health["headline"],
        health["headline"],
    )

    # -- health: verifier failure -> explicit error line -----------------
    health = build_health(
        None, {"code": "missing-verifier", "message": "tools/oxp3-verify.sh was not found"}
    )
    checks.eq("health: error state", health["state"], "error")
    checks.check(
        "health: error headline", "Verification unavailable" in health["headline"]
    )
    checks.check("health: error available flag", health["available"] is False)

    # -- health: the compact run result is accepted too ------------------
    compact = {
        "mode": "quick",
        "counts": {"total": 3, "pass": 1, "fail": 1, "skip": 0, "unknown": 1},
        "failures": [
            {"id": "vol.unit", "summary": "broken", "title": "", "hint": "fix it"}
        ],
        "unknowns": [
            {"id": "sleep.mem", "summary": "unreadable", "title": "", "hint": "check kernel"}
        ],
        "skips": [],
    }
    health = build_health(compact)
    checks.eq("health: compact state", health["state"], "fail")
    checks.eq("health: compact first id", health["issues"][0]["id"], "vol.unit")
    checks.eq("health: compact unknown id", health["issues"][1]["id"], "sleep.mem")

    # -- XeSS/XMX detection against synthetic /proc trees ----------------
    with tempfile.TemporaryDirectory(prefix="oxp3-overview-") as tmp:
        base = Path(tmp)
        home_dir = base / "home"

        empty = base / "empty-proc"
        empty.mkdir()
        with _proc_fixture_globals(empty):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: no game state", xess["state"], "no-game")
        checks.eq("xess: no game label", xess["label"], "No game running")

        no_xess = base / "no-xess-proc"
        _proc_fixture(
            no_xess,
            100,
            comm="wine64",
            args=[
                "Z:\\home\\deck\\Games\\steamapps\\common\\Portal 2\\portal2.exe",
                "-novid",
            ],
            environ={"SteamAppId": "620", "SteamGameId": "620"},
            maps="7f00-7f10 r-xp 00000000 00:00 0 /usr/lib/libc.so.6\n",
        )
        with _proc_fixture_globals(no_xess):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: game without XeSS", xess["state"], "xess-off")
        checks.eq(
            "xess: game without XeSS label",
            xess["label"],
            "XeSS: current game does not use it",
        )
        checks.eq("xess: game name", xess["game"], "portal2")

        xmx_proc = base / "xmx-proc"
        _proc_fixture(
            xmx_proc,
            200,
            comm="wine64",
            args=[
                "Z:\\home\\deck\\Games\\steamapps\\common\\Cyberpunk 2077\\bin\\x64\\Cyberpunk2077.exe"
            ],
            environ={
                "SteamAppId": "1423049311",
                "VK_DRIVER_FILES": str(home_dir / XMX_ICD_REL),
            },
            maps=(
                "7f00-7f10 r-xp 00000000 00:00 0 "
                "/opt/steam/steamapps/common/Cyberpunk 2077/bin/x64/libxess_dx12.so\n"
            ),
        )
        with _proc_fixture_globals(xmx_proc):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: XMX env state", xess["state"], "xess-on-xmx")
        checks.check(
            "xess: XMX env label",
            xess["label"].startswith("XeSS XMX: active"),
            xess["label"],
        )
        checks.check(
            "xess: XMX evidence names the ICD",
            "icd-xmx.json" in (xess["xmx_evidence"] or ""),
            xess["xmx_evidence"],
        )
        checks.eq("xess: XMX game name", xess["game"], "Cyberpunk 2077")

        standard_proc = base / "standard-proc"
        _proc_fixture(
            standard_proc,
            300,
            comm="wine64",
            args=[
                "Z:\\home\\deck\\Games\\steamapps\\common\\Cyberpunk 2077\\bin\\x64\\Cyberpunk2077.exe"
            ],
            environ={"SteamAppId": "1423049311"},
            maps="7f00-7f10 r-xp 00000000 00:00 0 /opt/libxess.so\n",
        )
        with _proc_fixture_globals(standard_proc):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: standard state", xess["state"], "xess-on-standard")
        checks.eq(
            "xess: standard label",
            xess["label"],
            "XeSS: detected, XMX shim NOT active (Cyberpunk 2077)",
        )

        layer_proc = base / "layer-proc"
        _proc_fixture(
            layer_proc,
            350,
            comm="wine64",
            args=["Z:\\games\\steamapps\\common\\Demo\\demo.exe"],
            environ={
                "SteamAppId": "9",
                "VK_LAYER_PATH": "/opt/xess/VK_LAYER_INTEL_xess.so",
            },
            maps="7f00-7f10 r-xp 00000000 00:00 0 /usr/lib/libc.so.6\n",
        )
        with _proc_fixture_globals(layer_proc):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: Vulkan layer env state", xess["state"], "xess-on-standard")
        checks.check(
            "xess: Vulkan layer env evidence",
            "VK_LAYER_PATH" in (xess["evidence"] or ""),
            xess["evidence"],
        )

        maps_proc = base / "maps-proc"
        _proc_fixture(
            maps_proc,
            400,
            comm="Cyberpunk2077.exe",
            args=["Cyberpunk2077.exe"],
            environ={},
            maps=(
                "7f00-7f10 r-xp 00000000 00:00 0 /usr/lib/libxess.so\n"
                "7f20-7f30 r-xp 00000000 00:00 0 %s\n" % (home_dir / XMX_DRIVER_REL)
            ),
        )
        with _proc_fixture_globals(maps_proc):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: mapped libxess state", xess["state"], "xess-on-xmx")
        checks.check(
            "xess: mapped XMX driver evidence",
            "libvulkan_intel.so" in (xess["xmx_evidence"] or ""),
            xess["xmx_evidence"],
        )

        child_proc = base / "child-proc"
        _proc_fixture(
            child_proc,
            500,
            comm="wine64",
            args=["Z:\\games\\steamapps\\common\\Demo Game\\demo.exe"],
            environ={"SteamAppId": "9"},
            maps="7f00-7f10 r-xp 00000000 00:00 0 /usr/lib/libc.so.6\n",
        )
        _proc_fixture(
            child_proc,
            501,
            comm="wine64",
            args=["demo.exe"],
            environ={},
            maps="7f00-7f10 r-xp 00000000 00:00 0 /opt/libxess_dx11.so\n",
            ppid=500,
        )
        with _proc_fixture_globals(child_proc):
            xess = collect_xess(home_dir, use_cache=False)
        checks.eq("xess: child mapping state", xess["state"], "xess-on-standard")
        checks.eq("xess: child mapping game", xess["game"], "demo")

        unreadable = base / "unreadable-proc"
        entry = _proc_fixture(
            unreadable,
            600,
            comm="wine64",
            args=["game.exe"],
            environ={"SteamAppId": "1"},
            maps="7f00-7f10 r-xp 00000000 00:00 0 /opt/libxess.so\n",
        )
        for name in ("maps", "environ", "cmdline", "stat"):
            os.chmod(entry / name, 0)
        (unreadable / "601").mkdir()
        os.chmod(unreadable / "601", 0)
        with _proc_fixture_globals(unreadable):
            try:
                xess = collect_xess(home_dir, use_cache=False)
                checks.check(
                    "xess: unreadable entries are skipped",
                    xess["state"] in ("no-game", "xess-off", "xess-on-standard"),
                    xess["state"],
                )
            except Exception as exc:  # pragma: no cover - selftest failure path
                checks.check("xess: unreadable entries do not raise", False, repr(exc))
        for name in ("maps", "environ", "cmdline", "stat"):
            os.chmod(entry / name, 0o644)
        os.chmod(unreadable / "601", 0o755)

    return checks.total, checks.failures


def run_selftest() -> int:
    """Exercise every probe against synthetic fixtures. Returns 0/1."""
    checks = _Checks()

    with tempfile.TemporaryDirectory(prefix="oxp3-selftest-") as tmp:
        root = Path(tmp)
        healthy = _World(root / "healthy", sched="enabled").build()
        with _fixture_globals(healthy):
            report = collect_status(str(healthy.home))
        states = _indicator_map(report)
        dumped = json.dumps(report)

        checks.eq("healthy: schema", report["schema"], 1)
        checks.eq("healthy: game name", (report.get("game") or {}).get("name"), "Cyberpunk 2077")
        checks.eq("healthy: game appid", (report.get("game") or {}).get("appid"), "1423049311")
        checks.eq("healthy: sched state", states["sched.state"]["state"], "ok")
        checks.eq("healthy: scx process", states["sched.scx_cosmos"]["state"], "ok")
        checks.eq("healthy: sched unit", states["sched.unit"]["state"], "ok")
        checks.eq("healthy: xe module", states["xe.module"]["state"], "ok")
        checks.eq("healthy: pl1", states["power.pl1"]["state"], "ok")
        checks.eq("healthy: platform profile", states["power.profile"]["state"], "ok")
        checks.eq("healthy: cpufreq", states["sys.cpu"]["state"], "ok")
        checks.eq("healthy: gpu", states["sys.gpu"]["state"], "ok")
        checks.eq("healthy: thermal", states["sys.thermal"]["state"], "ok")
        checks.eq("healthy: battery", states["sys.battery"]["state"], "ok")
        checks.eq("healthy: rapl values", states["rapl.values"]["state"], "ok")
        checks.eq("healthy: xmx icd", states["xmx.icd"]["state"], "ok")
        checks.eq("healthy: xmx helper", states["xmx.helper"]["state"], "ok")
        checks.eq("healthy: xmx igc", states["xmx.igc"]["state"], "ok")
        checks.eq("healthy: xmx shim", states["xmx.shim"]["state"], "ok")
        checks.eq("healthy: xmx vkd3d", states["xmx.vkd3d"]["state"], "ok")
        checks.eq("healthy: xmx kernels", states["xmx.kernels"]["state"], "ok")
        checks.eq("healthy: xmx trace", states["xmx.trace"]["state"], "ok")
        checks.eq("healthy: xmx benchmark", states["xmx.benchmark"]["state"], "ok")
        checks.eq("healthy: flatpak steam env", states["flatpak.steam_env"]["state"], "ok")
        checks.eq("healthy: heroic gamemode", states["heroic.gamemode"]["state"], "ok")
        # Cryptographic prefixes cannot be forged: synthetic drivers must be
        # reported as a mismatch, never as the recorded build.
        checks.eq("healthy: host driver mismatch", states["mesa.host"]["state"], "warn")
        checks.eq("healthy: xmx driver mismatch", states["xmx.driver"]["state"], "warn")
        checks.eq("healthy: runtime driver mismatch", states["flatpak.runtime"]["state"], "warn")
        checks.check(
            "healthy: no fail verdict on a complete fixture",
            not any(item["state"] == "fail" for item in states.values()),
            ", ".join(key for key, item in states.items() if item["state"] == "fail"),
        )
        checks.eq("healthy: overall", report["overall"], "warn")
        checks.eq(
            "healthy: counts sum",
            sum(report["counts"].values()),
            sum(len(grp["indicators"]) for grp in report["groups"]),
        )

        game = report.get("game") or {}
        env = game.get("env") or {}
        checks.check("leak: secret token not in report", "ghp_fixture_secret_do_not_print" not in dumped)
        checks.check("leak: DBus address not in report", "DBUS_SESSION_BUS_ADDRESS" not in env)
        checks.check("leak: ssh agent not in report", "SSH_AUTH_SOCK" not in env)
        checks.check("leak: cmdline token redacted", "fixture-supersecret" not in dumped)
        checks.check("leak: report env filtered", int(game.get("env_omitted") or 0) >= 3)
        checks.check("report env keeps XMX keys", "ANV_CM_HELPER" in env)
        checks.check("report is JSON-serialisable", '"schema": 1' in json.dumps(report))

        # -- degraded: non-XeSS title, sched_ext disabled ------------------
        degraded = _World(
            root / "degraded",
            sched="disabled",
            title="Portal 2",
            xmx=False,
            trace=False,
        ).build()
        with _fixture_globals(degraded):
            drep = collect_status(str(degraded.home))
        dstates = _indicator_map(drep)
        checks.eq("degraded: sched state", dstates["sched.state"]["state"], "warn")
        checks.eq("degraded: xmx helper na", dstates["xmx.helper"]["state"], "na")
        checks.eq("degraded: xmx trace na", dstates["xmx.trace"]["state"], "na")
        checks.eq("degraded: xmx benchmark na", dstates["xmx.benchmark"]["state"], "na")
        checks.eq("degraded: game name", (drep.get("game") or {}).get("name"), "portal2")
        checks.eq("degraded: overall", drep["overall"], "warn")

        # -- corrupt inputs must not raise -------------------------------
        corrupt = _World(root / "corrupt", corrupt=True).build()
        with _fixture_globals(corrupt):
            try:
                crep = collect_status(str(corrupt.home))
                checks.check("corrupt: report still produced", isinstance(crep.get("groups"), list))
                checks.eq(
                    "corrupt: counts sum",
                    sum(crep["counts"].values()),
                    sum(len(grp["indicators"]) for grp in crep["groups"]),
                )
            except Exception as exc:  # pragma: no cover - selftest failure path
                checks.check("corrupt: no exception", False, repr(exc))

        # -- empty home, no fixture files at all --------------------------
        empty_home = root / "empty-home"
        empty_home.mkdir()
        with _fixture_globals(healthy):
            erep = collect_status(str(empty_home))
        checks.check("empty home: report produced", isinstance(erep.get("groups"), list))
        checks.eq("empty home: schema", erep["schema"], 1)
        checks.eq(
            "empty home: counts sum",
            sum(erep["counts"].values()),
            sum(len(grp["indicators"]) for grp in erep["groups"]),
        )
        text = render_text(erep, color=False)
        checks.check("text render: has header", "OXP3 status" in text)

    # -- exit codes ------------------------------------------------------
    checks.eq("exit: ok", _exit_code({"counts": {"ok": 3}}, strict=False), 0)
    checks.eq("exit: fail", _exit_code({"counts": {"fail": 1}}, strict=False), 1)
    checks.eq("exit: strict warn", _exit_code({"counts": {"warn": 1}}, strict=True), 2)
    checks.eq("exit: strict unknown", _exit_code({"counts": {"unknown": 1}}, strict=True), 2)
    checks.eq("exit: strict na", _exit_code({"counts": {"na": 2}}, strict=True), 0)
    checks.eq("exit: lenient warn", _exit_code({"counts": {"warn": 2}}, strict=False), 0)

    # -- sanitizer unit checks ------------------------------------------
    env, dropped = sanitize_env(
        {
            "SteamAppId": "1",
            "ANV_CM_HELPER": "/x",
            "GITHUB_TOKEN": "secret",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
            "VK_SESSION_TOKEN": "secret2",
        }
    )
    checks.eq("sanitize: allowlist keeps Steam env", env.get("SteamAppId"), "1")
    checks.check("sanitize: drops unrelated env", "DBUS_SESSION_BUS_ADDRESS" not in env)
    checks.check("sanitize: redacts allowlisted secret keys", env.get("VK_SESSION_TOKEN") == "<redacted>")
    checks.eq("sanitize: counted drops", dropped, 2)
    checks.eq(
        "sanitize: cmdline", sanitize_cmdline("game.exe --api_token=abc --normal=1"),
        "game.exe --api_token=<redacted> --normal=1",
    )

    # -- verification action (stub verifier, no subprocess privileges) ---
    verify_total, verify_failures = verification_selftest()
    checks.total += verify_total
    checks.failures.extend(verify_failures)

    # -- two-line overview: synthetic verify reports + synthetic /proc ---
    overview_total, overview_failures = overview_selftest()
    checks.total += overview_total
    checks.failures.extend(overview_failures)

    # -- JSON report export (payload shape + the save method) -----------
    report_errors = report_selftest()
    checks.total += 6
    checks.failures.extend(report_errors)

    if checks.failures:
        print("selftest: %d check(s), %d FAILED" % (checks.total, len(checks.failures)))
        for failure in checks.failures:
            print("  FAIL  %s" % failure)
        return 1
    print("selftest: %d checks passed (synthetic /sys, /proc, /run, HOME)" % checks.total)
    return 0


_GLYPHS = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "unknown": "?   ", "na": "----"}
_COLORS = {
    "ok": "\033[32m",
    "warn": "\033[33m",
    "fail": "\033[31m",
    "unknown": "\033[90m",
    "na": "\033[90m",
}
_RESET = "\033[0m"


def render_text(report: Dict[str, Any], color: bool = False) -> str:
    def paint(state: str, text: str) -> str:
        if not color:
            return text
        return _COLORS.get(state, "") + text + _RESET

    lines: List[str] = []
    host = report.get("host", {})
    lines.append(
        "OXP3 status %s  user=%s  kernel=%s"
        % (report.get("generated_at", ""), host.get("user", "?"), host.get("kernel", "?"))
    )
    game = report.get("game")
    if game:
        lines.append(
            "game: %s  pid=%s  exe=%s%s"
            % (
                game.get("name"),
                game.get("pid"),
                os.path.basename(game.get("exe") or ""),
                "" if game.get("env_readable") else "  (env unreadable)",
            )
        )
    else:
        lines.append("game: none visible (configured launcher env is used)")
    counts = report.get("counts", {})
    lines.append(
        "overall: %s  (%d ok, %d warn, %d fail, %d unknown)"
        % (
            report.get("overall", "?").upper(),
            counts.get("ok", 0),
            counts.get("warn", 0),
            counts.get("fail", 0),
            counts.get("unknown", 0),
        )
    )
    for grp in report.get("groups", []):
        lines.append("")
        lines.append("[%s] %s" % (grp.get("state", "?").upper(), grp.get("title")))
        for item in grp.get("indicators", []):
            state = item.get("state", "unknown")
            lines.append(
                "  %s  %-32s %s"
                % (paint(state, _GLYPHS.get(state, "?   ")), item.get("label", ""), item.get("value", ""))
            )
            if item.get("source"):
                lines.append("        source: %s" % item["source"])
    for note in report.get("notes", []):
        lines.append("")
        lines.append("note: %s" % note)
    return "\n".join(lines)


def _exit_code(report: Dict[str, Any], strict: bool) -> int:
    counts = report.get("counts", {})
    if counts.get("fail", 0):
        return 1
    if strict and (counts.get("warn", 0) or counts.get("unknown", 0)):
        return 2
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only status checks for the OXP3 Anatase fixes "
        "(also the Decky plugin backend)."
    )
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--text", action="store_true", help="print a human-readable report (default)")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS", help="refresh every N seconds")
    parser.add_argument("--strict", action="store_true", help="exit 2 when anything is warn/unknown")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--home", metavar="PATH", help="desktop user's home to inspect")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run every check against a synthetic /sys, /proc and HOME fixture (no hardware)",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(VERSION)
        return 0

    if args.selftest:
        return run_selftest()

    color = sys.stdout.isatty() and not args.no_color and not args.json
    while True:
        try:
            report = collect_status(args.home)
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # last-resort guard: a CLI must not traceback
            print("oxp3-status: internal error: %s" % exc, file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=False))
        else:
            print(render_text(report, color=color))
        if args.watch <= 0:
            return _exit_code(report, args.strict)
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
        if not args.json:
            print()
        # loop forever; --watch is for a terminal


if __name__ == "__main__":
    sys.exit(main())
