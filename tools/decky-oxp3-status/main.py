#!/usr/bin/env python3
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
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Recorded expectations (docs/WORKING-BUILD-2026-11-08.md, docs/xess-xmx.md).
# ---------------------------------------------------------------------------
HOST_DRIVER = "/usr/lib64/libvulkan_intel.so"
HOST_DRIVER_PREFIX = "ec418f04e589965a"

XMX_DRIVER_REL = ".cache/oxp3-fixes/xmx-driver/libvulkan_intel.so"
XMX_DRIVER_PREFIX = "4b1d55ea9483417f"
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
    host_ok = host_prefix == HOST_DRIVER_PREFIX
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
            if prefix == HOST_DRIVER_PREFIX:
                parts.append("%s -> host driver %s" % (Path(lib).name, prefix))
                state = "ok"
            elif prefix == XMX_DRIVER_PREFIX:
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
            if same or sha256_prefix(lib) == XMX_DRIVER_PREFIX:
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
        "env_sources": [
            {"name": src["name"], "live": src["live"], "keys": len(src["env"])}
            for src in sources
        ],
        "groups": groups,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Decky Loader backend
# ---------------------------------------------------------------------------
class Plugin:
    """Decky Loader entry point. All methods return JSON-serialisable data."""

    async def _main(self) -> None:  # noqa: D401 - Decky lifecycle
        return None

    async def _unload(self) -> None:
        return None

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
