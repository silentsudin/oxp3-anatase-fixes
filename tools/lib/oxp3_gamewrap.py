#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3_gamewrap - compose every per-game graphics feature into one launch.

This is the single place where the OneXPlayer 3 launch features are decided:

* XeSS XMX environment (per-app opt-in, read from a shell-style env file that
  is parsed as *data*, never sourced or evaluated);
* HDR gamescope flags, only when the user opted the game in *and* the panel
  currently reports HDR enabled (the panel is never forced);
* the vibrance ReShade effect through gamescope ``--reshade-effect`` or
  vkBasalt, whichever the config selects;
* MangoHud via its environment variable - the MangoHud config file is never
  read or written here;
* everything else (DXVK/VKD3D/Proton environment) is passed through untouched.

Two launch shapes are supported: a plain command (Proton/umu/wine, typically
already inside the Gamemode gamescope session) and a command wrapped in a new
gamescope instance. ``--dry-run`` prints the exact final argv and the env diff;
``--status`` explains every decision. The CLI entry point is
``tools/oxp3-gamewrap.sh``; the config example is
``tools/oxp3-gamewrap.toml.example`` and the operator guide ``docs/LAUNCH.md``.

Safety rules: no shell, no eval, no string interpolation into a shell; argv is
built as a list and executed with execvpe; unsupported syntax in the XMX env
file (command substitution, arbitrary variables, redirection) makes XMX fail
*closed* with a warning instead of being executed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python >= 3.11; supported hosts are Fedora 45+.
    import tomllib
except ImportError:  # pragma: no cover - only on an unsupported interpreter
    tomllib = None

PROG = "oxp3-gamewrap"
VERSION = "1.0.0"

PLACEHOLDER = "%command%"
MISSING = object()


# ---------------------------------------------------------------------------
# Defaults and config
# ---------------------------------------------------------------------------
def builtin_config() -> dict:
    """The effective defaults, also the shape of the example config."""
    return {
        "schema": 1,
        "defaults": {
            "xmx": "auto",
            "hdr": "off",
            "vibrance": "auto",
            "mangohud": "keep",
            "gamescope": "auto",
        },
        "gamescope": {
            "command": "gamescope",
            "width": 0,
            "height": 0,
            "refresh": 0,
            "hdr_args": [
                "--hdr-enabled",
                "--hdr-pq-internal-enable",
                "--hdr-sdr-content-nits",
                "200",
            ],
            "extra_args": [],
            "reshade_dir": "~/.local/share/gamescope/reshade/Shaders",
            "effect": "oxp3-vibrance.fx",
        },
        "vibrance": {
            "vkbasalt_layer": "~/.local/share/oxp3-vkbasalt/layer",
            "vkbasalt_config": "~/.config/oxp3-vkbasalt.conf",
        },
        "mangohud": {
            "env": "MANGOHUD",
            "conf": "~/.config/MangoHud/MangoHud.conf",
        },
        "xmx": {
            "env_file": "~/.config/xess-xmx/oxp3-env.sh",
            "require": [
                "VK_DRIVER_FILES",
                "ANV_CM_HELPER",
                "XMX_IGC_DIR",
                "INTC_ALT_DRIVER_EXTENSIONS_PATH",
            ],
            "verify_paths": True,
        },
        "apps": {},
    }


ENUMS = {
    ("defaults", "xmx"): ("off", "on", "auto"),
    ("defaults", "hdr"): ("off", "on"),
    ("defaults", "vibrance"): ("off", "auto", "gamescope", "vkbasalt"),
    ("defaults", "mangohud"): ("keep", "on", "off"),
    ("defaults", "gamescope"): ("auto", "on", "off"),
}

APP_ENUMS = {
    "xmx": ("off", "on", "auto"),
    "hdr": ("off", "on"),
    "vibrance": ("off", "auto", "gamescope", "vkbasalt"),
    "mangohud": ("keep", "on", "off"),
    "gamescope": ("auto", "on", "off"),
}


class ConfigError(Exception):
    """A config problem the user can fix; reported, never guessed around."""


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _check_enum(ctx: str, value, allowed) -> str:
    if value not in allowed:
        raise ConfigError("%s: %r is not one of %s" % (ctx, value, "|".join(allowed)))
    return value


def validate_config(cfg: dict) -> dict:
    """Validate the merged config. Raises ConfigError."""
    if not isinstance(cfg, dict):
        raise ConfigError("top level must be a table")
    if cfg.get("schema", 1) != 1:
        raise ConfigError("schema: expected 1, got %r" % (cfg.get("schema"),))

    for section in ("defaults", "gamescope", "vibrance", "mangohud", "xmx"):
        if not isinstance(cfg.get(section), dict):
            raise ConfigError("[%s] must be a table" % section)

    for (section, key), allowed in ENUMS.items():
        _check_enum("[%s].%s" % (section, key), cfg[section].get(key), allowed)

    gamescope = cfg["gamescope"]
    for key in ("width", "height", "refresh"):
        value = gamescope.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError("[gamescope].%s must be a non-negative integer" % key)
    for key in ("hdr_args", "extra_args"):
        value = gamescope.get(key)
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ConfigError("[gamescope].%s must be a list of strings" % key)
        if any(not x for x in value):
            raise ConfigError("[gamescope].%s must not contain empty strings" % key)
    for key in ("command", "effect", "reshade_dir"):
        if not isinstance(gamescope.get(key), str) or not gamescope[key]:
            raise ConfigError("[gamescope].%s must be a non-empty string" % key)

    for key in ("vkbasalt_layer", "vkbasalt_config"):
        if not isinstance(cfg["vibrance"].get(key), str) or not cfg["vibrance"][key]:
            raise ConfigError("[vibrance].%s must be a non-empty string" % key)

    for key in ("env", "conf"):
        if not isinstance(cfg["mangohud"].get(key), str) or not cfg["mangohud"][key]:
            raise ConfigError("[mangohud].%s must be a non-empty string" % key)

    xmx = cfg["xmx"]
    if not isinstance(xmx.get("env_file"), str) or not xmx["env_file"]:
        raise ConfigError("[xmx].env_file must be a non-empty string")
    require = xmx.get("require")
    if not isinstance(require, list) or not all(isinstance(x, str) and x for x in require):
        raise ConfigError("[xmx].require must be a list of variable names")
    if not isinstance(xmx.get("verify_paths"), bool):
        raise ConfigError("[xmx].verify_paths must be true or false")

    apps = cfg.get("apps")
    if not isinstance(apps, dict):
        raise ConfigError("[apps] must be a table")
    for name, app in apps.items():
        if not isinstance(app, dict):
            raise ConfigError("[apps.%s] must be a table" % name)
        for key, allowed in APP_ENUMS.items():
            if key in app:
                _check_enum("[apps.%s].%s" % (name, key), app[key], allowed)
        match = app.get("match", [])
        if not isinstance(match, list) or not all(
                isinstance(x, str) and x for x in match):
            raise ConfigError(
                "[apps.%s].match must be a list of non-empty strings" % name)
        unknown = set(app) - set(APP_ENUMS) - {"match"}
        if unknown:
            raise ConfigError(
                "[apps.%s]: unknown key(s): %s" % (name, ", ".join(sorted(unknown))))
    return cfg


def data_home(home, env: dict) -> str:
    return env.get("XDG_DATA_HOME") or os.path.join(str(home), ".local", "share")


def expand_path(value: str, home, env: dict) -> str:
    """Expand ~ and $HOME only; no command substitution, no globbing."""
    homes = str(home)
    out = value
    for token in ("${HOME}", "$HOME"):
        out = out.replace(token, homes)
    out = out.replace("${XDG_DATA_HOME}", data_home(home, env))
    if out == "~":
        return homes
    if out.startswith("~/"):
        return os.path.join(homes, out[2:])
    if out.startswith("~"):
        raise ConfigError("only ~ and ~/... are supported: %r" % value)
    return out


def default_config_path(home, env: dict) -> str:
    if env.get("OXP3_GAMEWRAP_CONFIG"):
        return expand_path(env["OXP3_GAMEWRAP_CONFIG"], home, env)
    base = env.get("XDG_CONFIG_HOME") or os.path.join(str(home), ".config")
    return os.path.join(base, "oxp3-gamewrap.toml")


def read_config(path: str) -> tuple[dict, bool]:
    """Return (merged config, exists). Deterministic and side-effect free."""
    if not os.path.exists(path):
        return validate_config(builtin_config()), False
    if tomllib is None:
        raise ConfigError("this Python has no tomllib (needs 3.11+)")
    try:
        with open(path, "rb") as handle:
            over = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc)) from exc
    except Exception as exc:  # tomllib.TOMLDecodeError; avoid the import
        raise ConfigError("%s: invalid TOML: %s" % (path, exc)) from exc
    return validate_config(_deep_merge(builtin_config(), over)), True


# ---------------------------------------------------------------------------
# XMX environment file: parsed as data, never executed
# ---------------------------------------------------------------------------
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# A Windows path (e.g. INTC_ALT_DRIVER_EXTENSIONS_PATH=C:/windows/...) is
# resolved inside the Wine prefix, so a host-side existence check cannot apply.
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_UNQUOTED_FORBIDDEN = (";", "|", "&", "<", ">", "`", "$(")


def _expand_vars(value: str, home, lineno: int, path: str) -> str:
    homes = str(home)

    def replace(match):
        name = match.group(1) or match.group(2)
        if name == "HOME":
            return homes
        raise ConfigError(
            "%s:%d: unsupported variable $%s (only $HOME is expanded)"
            % (path, lineno, name))

    return _VAR_RE.sub(replace, value)


def parse_env_value(raw: str, home, lineno: int, path: str) -> str:
    """Parse one shell assignment RHS with a deliberately small grammar."""
    text = raw.strip()
    if not text:
        return ""
    if text[0] == "'":
        end = text.find("'", 1)
        if end < 0:
            raise ConfigError("%s:%d: unterminated single quote" % (path, lineno))
        tail = text[end + 1:].strip()
        if tail and not tail.startswith("#"):
            raise ConfigError("%s:%d: trailing text after quote" % (path, lineno))
        return text[1:end]
    if text[0] == '"':
        out = []
        i = 1
        while i < len(text):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text) and text[i + 1] in '\\"$':
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                tail = text[i + 1:].strip()
                if tail and not tail.startswith("#"):
                    raise ConfigError(
                        "%s:%d: trailing text after quote" % (path, lineno))
                return _expand_vars("".join(out), home, lineno, path)
            if ch == "`" or text[i:i + 2] == "$(":
                raise ConfigError(
                    "%s:%d: command substitution is not executed" % (path, lineno))
            out.append(ch)
            i += 1
        raise ConfigError("%s:%d: unterminated double quote" % (path, lineno))

    # Unquoted: a shell comment starts at whitespace + '#'.
    cut = len(text)
    for i, ch in enumerate(text):
        if ch == "#" and (i == 0 or text[i - 1].isspace()):
            cut = i
            break
    text = text[:cut].strip()
    for bad in _UNQUOTED_FORBIDDEN:
        if bad in text:
            raise ConfigError(
                "%s:%d: unsupported shell syntax %r (no eval, no redirection)"
                % (path, lineno, bad))
    if text.startswith("~"):
        text = expand_path(text, home, {})
    return _expand_vars(text, home, lineno, path)


def parse_env_file(path: str, home, env: dict) -> dict:
    """Parse ``export KEY=VALUE`` lines; raise ConfigError on anything else."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="strict")
    except OSError as exc:
        raise ConfigError("cannot read XMX env file %s: %s" % (path, exc)) from exc
    result = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            raise ConfigError(
                "%s:%d: line continuation is not supported" % (path, lineno))
        match = _ENV_LINE.match(line)
        if not match:
            raise ConfigError(
                "%s:%d: not an `export KEY=VALUE` line (sourcing is not used)"
                % (path, lineno))
        key, value = match.group(1), match.group(2)
        result[key] = parse_env_value(value, home, lineno, path)
    return result


# ---------------------------------------------------------------------------
# Compositor / panel HDR state (never forced; unknown means "do not add flags")
# ---------------------------------------------------------------------------
@dataclass
class PanelHdr:
    state: str = "unknown"   # "on" | "off" | "unknown"
    source: str = "none"
    detail: str = ""


def normalize_switch(value) -> str:
    if value is True or value == 1:
        return "on"
    if value is False or value == 0:
        return "off"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("on", "true", "yes", "enabled", "1"):
            return "on"
        if lowered in ("off", "false", "no", "disabled", "0"):
            return "off"
    return "unknown"


def _kscreen_hdr(env: dict) -> PanelHdr:
    exe = shutil.which("kscreen-doctor", path=env.get("PATH"))
    if not exe:
        return PanelHdr("unknown", "none", "kscreen-doctor not found")
    try:
        proc = subprocess.run(
            [exe, "-j"], capture_output=True, text=True, timeout=5, env=env)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return PanelHdr("unknown", "none", "kscreen-doctor -j failed: %s" % exc)
    outputs = [o for o in data.get("outputs", []) if o.get("enabled")]
    if not outputs:
        return PanelHdr("unknown", "none", "kscreen-doctor sees no enabled output")
    chosen = next(
        (o for o in outputs if "edp" in str(o.get("name", "")).lower()), outputs[0])
    return PanelHdr(normalize_switch(chosen.get("hdr")), "kscreen-doctor",
                    "output %s" % chosen.get("name", "?"))


def _kwin_config_hdr(home, env: dict) -> PanelHdr:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(str(home), ".config")
    path = os.path.join(base, "kwinoutputconfig.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PanelHdr("unknown", "none", "no recorded KWin output config")

    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if "highDynamicRange" in obj:
                found.append((str(obj.get("connectorName") or ""),
                              obj.get("highDynamicRange")))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    if not found:
        return PanelHdr("unknown", "none", "no HDR field in %s" % path)
    chosen = next((f for f in found if "edp" in f[0].lower()), found[0])
    return PanelHdr(normalize_switch(chosen[1]), "kwinoutputconfig",
                     "recorded %s (last Plasma session)" % (chosen[0] or "output"))


def detect_gamescope(env: dict) -> tuple[bool, str]:
    """Is this launch already inside a gamescope session/compositor?"""
    override = env.get("OXP3_IN_GAMESCOPE")
    if override is not None:
        state = normalize_switch(override)
        if state == "on":
            return True, "OXP3_IN_GAMESCOPE"
        if state == "off":
            return False, ""
    for key in ("GAMESCOPECTRL_BASELAYER_APPID", "STEAM_GAMESCOPE_HDR_SUPPORTED",
                "GAMESCOPE_WAYLAND_DISPLAY"):
        value = env.get(key)
        if not value:
            continue
        if key == "STEAM_GAMESCOPE_HDR_SUPPORTED" and normalize_switch(value) != "on":
            continue
        return True, key
    wayland = env.get("WAYLAND_DISPLAY", "")
    if "gamescope" in wayland.lower():
        return True, "WAYLAND_DISPLAY=%s" % wayland
    return False, ""


def panel_hdr(env: dict, home, inside_gamescope: bool) -> PanelHdr:
    """Probe order: explicit override, override file, kscreen, KWin config."""
    override = env.get("OXP3_PANEL_HDR")
    if override is not None:
        state = normalize_switch(override)
        if state != "unknown":
            return PanelHdr(state, "OXP3_PANEL_HDR", "environment override")
        return PanelHdr("unknown", "OXP3_PANEL_HDR",
                        "override %r did not parse as on/off" % override)
    override_file = env.get("OXP3_PANEL_HDR_FILE")
    if override_file:
        try:
            raw = Path(override_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            return PanelHdr("unknown", "OXP3_PANEL_HDR_FILE",
                            "cannot read %s: %s" % (override_file, exc))
        return PanelHdr(normalize_switch(raw), "OXP3_PANEL_HDR_FILE", override_file)
    if not inside_gamescope:
        kscreen = _kscreen_hdr(env)
        if kscreen.state != "unknown":
            return kscreen
    recorded = _kwin_config_hdr(home, env)
    if recorded.state != "unknown":
        return recorded
    return PanelHdr("unknown", "none", "no live compositor and no recorded state")


# ---------------------------------------------------------------------------
# App resolution and feature decisions
# ---------------------------------------------------------------------------
def resolve_app(cfg: dict, explicit: str | None, argv: list[str],
                env: dict) -> tuple[str | None, dict, str]:
    apps = cfg.get("apps", {})
    candidates = []
    if explicit:
        candidates.append(explicit)
        for key in ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID", "OXP3_APP"):
            if env.get(key):
                candidates.append(env[key])
    else:
        for key in ("OXP3_APP", "STEAM_COMPAT_APP_ID", "SteamAppId", "SteamGameId"):
            if env.get(key):
                candidates.append(env[key])
        for token in argv:
            candidates.append(os.path.basename(token))
    for name, app in apps.items():
        patterns = app.get("match", [])
        for candidate in candidates:
            if not candidate:
                continue
            if name == candidate:
                return name, app, "app key %s" % name
            for pattern in patterns:
                if fnmatch.fnmatchcase(candidate.lower(), pattern.lower()):
                    return name, app, "match %r" % candidate
    if explicit and explicit in apps:
        return explicit, apps[explicit], "app key %s" % explicit
    return None, {}, "no app section matched"


def _feature(app: dict, cfg: dict, key: str):
    return app.get(key, cfg["defaults"][key])


def decide_xmx(app: dict, cfg: dict, env: dict, home) -> tuple[bool, str, dict]:
    """Return (enabled, reason, env additions or empty)."""
    mode = _feature(app, cfg, "xmx")
    override = env.get("OXP3_XMX")
    if mode == "off":
        return False, "config: xmx=off (hard veto for this app)", {}
    if override is not None:
        forced = normalize_switch(override)
        if forced == "off":
            return False, "OXP3_XMX=0 override", {}
        if forced == "unknown":
            return False, "OXP3_XMX=%r is not on/off" % override, {}
    if mode == "auto" and override is None:
        return False, "xmx=auto needs OXP3_XMX=1 or a per-app xmx=on", {}
    if mode != "on" and override is None:
        return False, "not opted in for this app", {}

    env_file = env.get("OXP3_XMX_ENV_FILE") or cfg["xmx"]["env_file"]
    env_file = expand_path(env_file, home, env)
    try:
        additions = parse_env_file(env_file, home, env)
    except ConfigError as exc:
        return False, "XMX env refused: %s" % exc, {}
    missing = [key for key in cfg["xmx"]["require"] if key not in additions]
    if missing:
        return False, "XMX env missing required keys: %s" % ", ".join(missing), {}
    if cfg["xmx"]["verify_paths"]:
        for key in cfg["xmx"]["require"]:
            value = additions.get(key, "")
            path = expand_path(value, home, env) if value else ""
            if not path:
                return False, "XMX env %s is empty" % key, {}
            if _WINDOWS_PATH.match(path):
                continue  # resolved inside the Wine prefix, not on this host
            if not os.path.exists(path):
                return False, "XMX env %s does not exist: %s" % (key, path), {}
            if key == "VK_DRIVER_FILES" and not os.path.isfile(path):
                return False, "VK_DRIVER_FILES is not a file: %s" % path, {}
    return True, "env %s (%d keys)" % (env_file, len(additions)), additions


def shader_path(cfg: dict, home, env: dict) -> str:
    reshade = expand_path(cfg["gamescope"]["reshade_dir"], home, env)
    effect = cfg["gamescope"]["effect"]
    return effect if os.path.isabs(effect) else os.path.join(reshade, effect)


def vkbasalt_files(cfg: dict, home, env: dict) -> tuple[str, str]:
    return (expand_path(cfg["vibrance"]["vkbasalt_layer"], home, env),
            expand_path(cfg["vibrance"]["vkbasalt_config"], home, env))


@dataclass
class Feature:
    state: str
    detail: str


@dataclass
class Plan:
    argv: list
    env: dict
    env_diff: list
    features: dict
    notes: list
    app_name: str | None
    app: dict
    matched_by: str
    panel: PanelHdr
    gamescope: dict
    added_env: dict = field(default_factory=dict)
    home: str = ""


def diff_env(before: dict, after: dict) -> list:
    out = []
    for key in sorted(after):
        old = before.get(key, MISSING)
        if old is MISSING:
            out.append({"op": "add", "key": key, "value": after[key]})
        elif old != after[key]:
            out.append({"op": "change", "key": key, "value": after[key], "old": old})
    return out


def build_plan(cfg: dict, argv: list[str], env: dict, home,
               explicit_app: str | None = None) -> Plan:
    if not argv:
        raise ConfigError("no command to run")
    app_name, app, matched_by = resolve_app(cfg, explicit_app, argv, env)
    decisions = {}
    notes = []
    additions = {}

    inside, inside_reason = detect_gamescope(env)
    panel = panel_hdr(env, home, inside)

    hdr_mode = _feature(app, cfg, "hdr")
    vib_mode = _feature(app, cfg, "vibrance")
    gs_mode = _feature(app, cfg, "gamescope")

    gs_command = cfg["gamescope"]["command"]
    gs_available = shutil.which(gs_command, path=env.get("PATH")) is not None

    shader = shader_path(cfg, home, env)
    shader_ok = os.path.isfile(shader)
    layer, vk_conf = vkbasalt_files(cfg, home, env)
    vk_ok = (os.path.isfile(os.path.join(layer, "libvkbasalt.so"))
             and os.path.isfile(vk_conf))

    hdr_possible = hdr_mode == "on" and panel.state == "on"
    wants_gs_vibrance = vib_mode == "gamescope" and shader_ok
    auto_wrap = gs_mode == "auto" and (hdr_possible or wants_gs_vibrance)
    wrap = (not inside) and gs_available and (gs_mode == "on" or auto_wrap)
    if gs_mode == "on" and not gs_available:
        notes.append("gamescope requested but %r is not on PATH" % gs_command)
    elif (gs_mode == "auto" and hdr_possible and not gs_available):
        notes.append(
            "panel HDR is on and the game opted in, but %r is not on PATH; "
            "HDR flags not added" % gs_command)
    if gs_mode == "on" and inside:
        notes.append(
            "gamescope=on but already inside a gamescope session (%s); not nesting"
            % inside_reason)

    # -- XMX ---------------------------------------------------------------
    xmx_on, xmx_reason, xmx_env = decide_xmx(app, cfg, env, home)
    if xmx_on:
        additions.update(xmx_env)
    elif xmx_reason.startswith("XMX env"):
        notes.append(xmx_reason)
    decisions["xmx"] = Feature("on" if xmx_on else "off", xmx_reason)

    # -- HDR ---------------------------------------------------------------
    hdr_args = []
    if hdr_mode != "on":
        decisions["hdr"] = Feature("off", "config: not opted in for this app")
    elif inside:
        decisions["hdr"] = Feature(
            "session",
            "already inside gamescope (%s): HDR is a session flag, not per game"
            % inside_reason)
        notes.append(
            "HDR in the Gamemode session comes from the gamescope session flags "
            "(tools/display/display-mode.sh gamemode); the wrapper never adds "
            "them to a session it did not start")
    elif panel.state == "unknown":
        decisions["hdr"] = Feature(
            "skipped",
            "panel HDR state unknown (%s); refusing to force HDR" % panel.detail)
    elif panel.state == "off":
        decisions["hdr"] = Feature(
            "skipped", "panel reports HDR off (%s); not forcing it" % panel.source)
    elif not wrap:
        if not gs_available:
            decisions["hdr"] = Feature(
                "skipped",
                "game opted in and panel HDR is on, but %r is not on PATH"
                % gs_command)
        else:
            decisions["hdr"] = Feature(
                "skipped",
                "game opted in and panel HDR is on, but this is not a gamescope "
                "wrap (set gamescope=\"on\" to start one)")
    else:
        hdr_args = list(cfg["gamescope"]["hdr_args"])
        decisions["hdr"] = Feature(
            "added",
            "panel HDR on (%s) and app opted in; gamescope flags: %s"
            % (panel.source, " ".join(hdr_args)))

    # -- vibrance ----------------------------------------------------------
    chosen = vib_mode
    if chosen == "auto":
        if wrap and shader_ok:
            chosen = "gamescope"
        elif vk_ok:
            chosen = "vkbasalt"
        else:
            chosen = "off"
    if chosen == "gamescope":
        if not wrap:
            decisions["vibrance"] = Feature(
                "skipped",
                "gamescope reshade needs gamescope=\"on\" or auto-wrapping")
        elif not shader_ok:
            decisions["vibrance"] = Feature(
                "skipped",
                "shader not installed: %s (run tools/display/vibrance.sh install)"
                % shader)
        else:
            decisions["vibrance"] = Feature(
                "added",
                "gamescope --reshade-effect %s" % cfg["gamescope"]["effect"])
    elif chosen == "vkbasalt":
        if not vk_ok:
            decisions["vibrance"] = Feature(
                "skipped",
                "vkBasalt layer/config missing under %s "
                "(run tools/display/vibrance.sh install)" % layer)
        else:
            additions["ENABLE_VKBASALT"] = "1"
            additions["VK_ADD_IMPLICIT_LAYER_PATH"] = layer
            additions["VKBASALT_CONFIG_FILE"] = vk_conf
            decisions["vibrance"] = Feature("added", "vkBasalt layer %s" % layer)
    elif chosen == "off":
        if vib_mode == "off":
            decisions["vibrance"] = Feature("off", "config: disabled for this app")
        else:
            decisions["vibrance"] = Feature(
                "off",
                "auto found nothing to use (no gamescope wrap/shader, no vkBasalt layer)")
    else:  # pragma: no cover - validate_config rejects it
        raise ConfigError("unhandled vibrance mode %r" % chosen)

    # -- MangoHud (env only; the config file is never touched) -------------
    hud_mode = _feature(app, cfg, "mangohud")
    hud_key = cfg["mangohud"]["env"]
    hud_conf = expand_path(cfg["mangohud"]["conf"], home, env)
    conf_note = "" if os.path.exists(hud_conf) else " (config absent)"
    if hud_mode == "on":
        additions[hud_key] = "1"
        decisions["mangohud"] = Feature(
            "on", "%s=1; %s left untouched%s" % (hud_key, hud_conf, conf_note))
    elif hud_mode == "off":
        additions[hud_key] = "0"
        decisions["mangohud"] = Feature(
            "off", "%s=0; %s left untouched" % (hud_key, hud_conf))
    else:
        decisions["mangohud"] = Feature(
            "keep",
            "%s passed through; %s never read or written" % (hud_key, hud_conf))

    # -- command -----------------------------------------------------------
    final = list(argv)
    if wrap:
        prefix = [gs_command]
        width = cfg["gamescope"]["width"]
        height = cfg["gamescope"]["height"]
        refresh = cfg["gamescope"]["refresh"]
        if width:
            prefix += ["-W", str(width)]
        if height:
            prefix += ["-H", str(height)]
        if refresh:
            prefix += ["-r", str(refresh)]
        prefix += hdr_args
        prefix += list(cfg["gamescope"]["extra_args"])
        if decisions["vibrance"].state == "added":
            prefix += ["--reshade-effect", cfg["gamescope"]["effect"]]
        final = prefix + ["--"] + list(argv)

    if explicit_app and app_name is None:
        notes.append("app %r is not configured; defaults applied" % explicit_app)

    new_env = dict(env)
    new_env.update(additions)
    return Plan(
        argv=final,
        env=new_env,
        env_diff=diff_env(env, new_env),
        features=decisions,
        notes=notes,
        app_name=app_name,
        app=app,
        matched_by=matched_by,
        panel=panel,
        gamescope={
            "inside": inside,
            "inside_reason": inside_reason,
            "wrap": wrap,
            "available": gs_available,
            "mode": gs_mode,
        },
        added_env=additions,
        home=str(home),
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def plan_json(cfg_path: str, cfg_exists: bool, plan: Plan, mode: str) -> dict:
    return {
        "schema": 1,
        "version": VERSION,
        "mode": mode,
        "config": {"path": cfg_path, "exists": cfg_exists},
        "app": {
            "name": plan.app_name,
            "matched_by": plan.matched_by,
            "configured": plan.app_name is not None,
        },
        "panel_hdr": {"state": plan.panel.state, "source": plan.panel.source},
        "gamescope": plan.gamescope,
        "features": {name: {"state": f.state, "detail": f.detail}
                     for name, f in plan.features.items()},
        "env_diff": plan.env_diff,
        "command": plan.argv,
        "notes": plan.notes,
    }


def _quote(value: str) -> str:
    return shlex.quote(value)


def render_text(cfg_path: str, cfg_exists: bool, plan: Plan) -> str:
    lines = []
    lines.append("%s %s" % (PROG, VERSION))
    lines.append("config   : %s%s" % (
        cfg_path, "" if cfg_exists else "  (absent; built-in defaults)"))
    if plan.app_name:
        lines.append("app      : %s  (%s)" % (plan.app_name, plan.matched_by))
    else:
        lines.append("app      : <none>  (%s; defaults applied)" % plan.matched_by)
    lines.append("panel HDR: %s  [%s]" % (plan.panel.state, plan.panel.source))
    if plan.gamescope["inside"]:
        state = "inside gamescope (%s)" % plan.gamescope["inside_reason"]
    elif plan.gamescope["wrap"]:
        state = "wrap (not inside a gamescope session)"
    elif plan.gamescope["mode"] == "off":
        state = "no wrap (config gamescope=off)"
    else:
        state = "no wrap (nothing needs gamescope)"
    lines.append("gamescope: %s" % state)
    lines.append("features :")
    for name in ("xmx", "hdr", "vibrance", "mangohud"):
        feature = plan.features[name]
        lines.append("  %-9s %-8s %s" % (name, feature.state, feature.detail))
    if plan.env_diff:
        lines.append("env diff : +%d" % len(plan.env_diff))
        for item in plan.env_diff:
            if item["op"] == "add":
                lines.append("  + %s=%s" % (item["key"], _quote(item["value"])))
            else:
                lines.append("  ~ %s: %s -> %s" % (
                    item["key"], _quote(item["old"]), _quote(item["value"])))
    else:
        lines.append("env diff : none")
    if plan.argv == [PLACEHOLDER]:
        lines.append("command  : (none given; status only)")
    else:
        lines.append("command  :")
        lines.append("  " + " ".join(_quote(token) for token in plan.argv))
    if plan.notes:
        lines.append("notes    :")
        for note in plan.notes:
            lines.append("  - %s" % note)
    return "\n".join(lines)


def status_text(cfg_path: str, cfg_exists: bool, plan: Plan, env: dict) -> str:
    text = [render_text(cfg_path, cfg_exists, plan)]
    text.append("applied  :")
    if plan.added_env:
        for key in sorted(plan.added_env):
            text.append("  %s=%s" % (key, _quote(plan.added_env[key])))
    else:
        text.append("  none")
    if env.get("OXP3_XMX") is not None:
        text.append("override : OXP3_XMX=%s" % env["OXP3_XMX"])
    if env.get("OXP3_IN_GAMESCOPE") is not None:
        text.append("override : OXP3_IN_GAMESCOPE=%s" % env["OXP3_IN_GAMESCOPE"])
    return "\n".join(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
USAGE = """\
oxp3-gamewrap - compose XeSS XMX, HDR, vibrance and MangoHud for one game launch

usage:
  oxp3-gamewrap.sh --dry-run [--app NAME] [--config PATH] -- COMMAND [ARG...]
  oxp3-gamewrap.sh --status  [--app NAME] [--config PATH] [--json]
  oxp3-gamewrap.sh [--app NAME] [--config PATH] -- COMMAND [ARG...]

Options:
  --app NAME       per-app section to use (default: match the command/SteamAppId)
  --config PATH    config file (default: $XDG_CONFIG_HOME/oxp3-gamewrap.toml)
  --home PATH      home directory used to expand ~ paths (default: $HOME)
  --dry-run, -n    print the exact argv and env diff; run nothing
  --status         explain every feature decision; run nothing
  --json           with --dry-run/--status: emit one JSON object
  --version        print the version
  --help           print this text

Environment overrides (documented in docs/LAUNCH.md):
  OXP3_XMX=1|0            force XMX on/off (config xmx="off" still vetoes)
  OXP3_XMX_ENV_FILE       XMX env file to parse instead of the configured one
  OXP3_PANEL_HDR=on|off   panel HDR state (for tests/headless); never guessed
  OXP3_PANEL_HDR_FILE     file containing one line: on|off
  OXP3_IN_GAMESCOPE=1     treat this launch as inside a gamescope session
  OXP3_GAMEWRAP_CONFIG    config path
  OXP3_PYTHON             interpreter used by tools/oxp3-gamewrap.sh

Exit codes: 0 ok, 1 launch/exec failure, 2 usage or config error.
"""


def parse_cli(args: list[str]) -> dict:
    opts = {
        "mode": "run", "json": False, "config": None, "home": None,
        "app": None, "command": [], "show_help": False, "show_version": False,
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg in ("--help", "-h"):
            opts["show_help"] = True
            return opts
        if arg in ("--version", "-V"):
            opts["show_version"] = True
            return opts
        if arg in ("--dry-run", "-n"):
            if opts["mode"] == "status":
                raise ConfigError("--dry-run and --status are mutually exclusive")
            opts["mode"] = "dry-run"
        elif arg == "--status":
            if opts["mode"] == "dry-run":
                raise ConfigError("--dry-run and --status are mutually exclusive")
            opts["mode"] = "status"
        elif arg == "--json":
            opts["json"] = True
        elif arg in ("--config", "--home", "--app"):
            if i + 1 >= len(args):
                raise ConfigError("%s needs a value" % arg)
            opts[arg[2:]] = args[i + 1]
            i += 1
        elif arg.startswith("--config=") or arg.startswith("--home=") \
                or arg.startswith("--app="):
            key, value = arg[2:].split("=", 1)
            if not value:
                raise ConfigError("%s needs a value" % arg)
            opts[key] = value
        elif arg.startswith("-") and arg != "-":
            raise ConfigError("unknown option %r (options must precede --)" % arg)
        else:
            break  # first non-option token starts the command
        i += 1
    opts["command"] = args[i:]
    return opts


def _home(opts: dict, env: dict) -> str:
    home = opts["home"] or env.get("HOME") or str(Path.home())
    if not os.path.isabs(home):
        raise ConfigError("--home must be an absolute path")
    return os.path.abspath(home)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = parse_cli(args)
    except ConfigError as exc:
        print("%s: %s" % (PROG, exc), file=sys.stderr)
        return 2
    if opts["show_help"]:
        print(USAGE)
        return 0
    if opts["show_version"]:
        print("%s %s" % (PROG, VERSION))
        return 0
    if opts["json"] and opts["mode"] == "run":
        print("%s: --json only applies to --status/--dry-run" % PROG,
              file=sys.stderr)
        return 2
    if opts["mode"] == "run" and not opts["command"]:
        print(USAGE, file=sys.stderr)
        return 2
    if opts["mode"] == "status" and opts["command"]:
        print("%s: --status takes no command" % PROG, file=sys.stderr)
        return 2
    if opts["mode"] == "dry-run" and not opts["command"]:
        print("%s: --dry-run needs a command after --" % PROG, file=sys.stderr)
        return 2

    env = dict(os.environ)
    try:
        home = _home(opts, env)
        env["HOME"] = home
        cfg_path = opts["config"] or default_config_path(home, env)
        cfg_path = expand_path(cfg_path, home, env)
        cfg, exists = read_config(cfg_path)
        command = opts["command"] or [PLACEHOLDER]
        plan = build_plan(cfg, command, env, home, opts["app"])
    except ConfigError as exc:
        print("%s: %s" % (PROG, exc), file=sys.stderr)
        return 2

    if opts["mode"] in ("dry-run", "status"):
        if opts["json"]:
            print(json.dumps(plan_json(cfg_path, exists, plan, opts["mode"]),
                             indent=2))
        elif opts["mode"] == "status":
            print(status_text(cfg_path, exists, plan, env))
        else:
            print(render_text(cfg_path, exists, plan))
        return 0

    for note in plan.notes:
        print("%s: %s" % (PROG, note), file=sys.stderr)
    try:
        os.execvpe(plan.argv[0], plan.argv, plan.env)
    except OSError as exc:
        print("%s: cannot exec %s: %s" % (PROG, plan.argv[0], exc),
              file=sys.stderr)
        return 1
    return 1  # pragma: no cover - execvpe never returns on success


if __name__ == "__main__":
    sys.exit(main())
