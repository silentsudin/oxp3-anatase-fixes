#!/usr/bin/env python3
"""Install, verify and remove the OXP3 Status Decky plugin.

Why this module exists
----------------------
Decky Loader discovers plugins by directory: it runs as root and owns
``<user home>/homebrew/plugins``, so a plain user-level copy fails with
``EACCES`` and a changed ``main.py`` is not picked up until
``plugin_loader.service`` restarts (its UI reload only re-reads the frontend
bundle).  Installing the plugin therefore needs the same care as the rest of the
hardware fixes: a deterministic artifact, a recorded hash, an ownership marker,
a backup before a replacement, and a refusal to delete somebody else's
directory.

The shell installer (``tools/oxp3-hw-install.sh``) does the user-facing work;
this module holds the resolution, validation and file-move logic so it can be
unit-tested without root, systemd, Decky or a network.

Recorded artifact
-----------------
``tools/decky-oxp3-status/artifact.sha256`` pins the sha256 of the known-good
deterministic zip.  When the installer cannot build (no node/npm), an existing
zip is installed only if its digest matches that record.  A freshly built zip is
installed as-is and the mismatch (if any) is reported, because a checkout that
changed its sources legitimately produces a new digest.

Ownership marker
----------------
An install writes ``.oxp3-managed`` into the plugin directory::

    plugin=oxp3-status
    name=OXP3 Status
    version=1.0.0
    artifact_sha256=<sha256 of the zip>
    installed_at=<UTC>
    installer=oxp3-hw-install.sh

``uninstall`` removes the directory only when that marker names this plugin and
the directory is exactly the resolved ``oxp3-status`` path.  A directory with
the same name but no marker (another plugin, a hand-made copy) is never
deleted; an install over it is refused as well.  A hand-copied *unmarked* copy
of our own plugin is "adoptable": it is backed up and replaced, never deleted in
place.

Backups
-------
Every replacement first copies the existing directory (with modes and times)
to ``<state>/decky-backups/<UTC stamp>/oxp3-status`` and writes a small
``backup.json`` beside it.  The state directory is the same one the undo
mechanism uses: ``$OXP3_STATE_DIR``, otherwise
``<home>/.local/state/oxp3-fixes``.  A failed replacement restores the old
directory; the backup remains for a manual rollback.

Exit codes (CLI): 0 ok, 1 error/missing artifact/bad digest, 2 usage,
3 refused (foreign directory or ownership marker mismatch).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PROG = "decky_plugin"

PLUGIN_DIR_NAME = "oxp3-status"
PLUGIN_NAME = "OXP3 Status"
PLUGIN_AUTHOR = "silentsudin"
API_VERSION = 1
MARKER_NAME = ".oxp3-managed"
MARKER_KIND = "oxp3-status"
DEFAULT_SUBDIR = "homebrew/plugins/" + PLUGIN_DIR_NAME
RECORDED_SHA_REL = "tools/decky-oxp3-status/artifact.sha256"
STATE_SUFFIX = ".local/state/oxp3-fixes"

EXPECTED_ENTRIES = frozenset(
    ("plugin.json", "main.py", "README.md", "dist/index.js", "dist/index.js.map")
)
MIN_INDEX_BYTES = 1024
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class PluginError(Exception):
    """A missing/corrupt artifact or an unusable target (CLI exit 1)."""


class PluginRefused(Exception):
    """A safety refusal: not our directory (CLI exit 3)."""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _expand_home(value: str, home: Path) -> str:
    value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    return value


def resolve_plugins_dir(home: Path, plugins_dir: str = "", root: str = "") -> Path:
    """Resolve the plugin directory from HOME, an override and a --root prefix.

    The default is ``<home>/homebrew/plugins/oxp3-status``.  An override may be
    absolute, start with ``~``/``$HOME``, or be relative to the home.  With a
    ``--root`` prefix the resulting absolute path is placed inside the prefix,
    unless it already is.
    """
    home = Path(home).expanduser()
    raw = (plugins_dir or DEFAULT_SUBDIR).strip()
    if not raw:
        raise PluginError("the plugin directory override is empty")
    expanded = _expand_home(raw, home)
    target = Path(expanded)
    if not target.is_absolute():
        target = home / target
    target = Path(os.path.normpath(str(target)))
    root_path = Path(root).expanduser() if root else None
    if root_path is not None:
        root_str = os.path.normpath(str(root_path))
        try:
            already = os.path.commonpath([str(target), root_str]) == root_str
        except ValueError:
            already = False
        if not already:
            target = Path(root_str) / str(target).lstrip("/")
    if str(target) in ("", "/") or target.name in ("", ".", ".."):
        raise PluginError("refusing an unsafe plugin directory: %s" % target)
    return target


def resolve_state_dir(home: Path, root: str = "", state_dir: str = "") -> Path:
    """The undo-state root; decky backups live in its ``decky-backups`` child."""
    if state_dir:
        target = Path(state_dir).expanduser()
        if not target.is_absolute():
            target = Path(home) / target
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path(home) / ".local/state")
        target = Path(base) / "oxp3-fixes"
    target = Path(os.path.normpath(str(target)))
    if root:
        root_str = os.path.normpath(str(Path(root).expanduser()))
        try:
            already = os.path.commonpath([str(target), root_str]) == root_str
        except ValueError:
            already = False
        if not already:
            target = Path(root_str) / str(target).lstrip("/")
    return target


def marker_path(plugin_dir: Path) -> Path:
    return Path(plugin_dir) / MARKER_NAME


# ---------------------------------------------------------------------------
# artifact + manifest validation
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_recorded_sha(repo: Path) -> dict:
    """Read ``tools/decky-oxp3-status/artifact.sha256`` -> {sha256, filename}."""
    path = Path(repo) / RECORDED_SHA_REL
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginError("no recorded artifact digest at %s (%s)" % (path, exc))
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        digest = parts[0].lower()
        if not _SHA_RE.match(digest):
            raise PluginError("%s: %r is not a sha256 digest" % (path, parts[0]))
        filename = parts[1] if len(parts) > 1 else ""
        return {"sha256": digest, "filename": filename}
    raise PluginError("%s has no digest line" % path)


def validate_manifest(data: object, source: str = "plugin.json") -> dict:
    """Validate the Decky manifest fields this plugin owns."""
    if not isinstance(data, dict):
        raise PluginError("%s is not a JSON object" % source)
    name = data.get("name")
    author = data.get("author")
    api = data.get("api_version")
    version = data.get("version")
    if name != PLUGIN_NAME:
        raise PluginError("%s: name must be %r, got %r" % (source, PLUGIN_NAME, name))
    if author != PLUGIN_AUTHOR:
        raise PluginError("%s: author must be %r, got %r" % (source, PLUGIN_AUTHOR, author))
    if isinstance(api, bool) or not isinstance(api, int) or api != API_VERSION:
        raise PluginError("%s: api_version must be %d, got %r" % (source, API_VERSION, api))
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise PluginError("%s: version must be a x.y.z string, got %r" % (source, version))
    return {"name": name, "author": author, "api_version": api, "version": version}


def _read_manifest_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginError("plugin.json is missing/unreadable: %s" % exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PluginError("plugin.json is not valid JSON: %s" % exc)
    return validate_manifest(data, str(path))


def validate_plugin_dir(plugin_dir: Path) -> dict:
    """Check the files Decky Loader needs in an installed plugin directory."""
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        raise PluginError("plugin directory does not exist: %s" % plugin_dir)
    info = _read_manifest_file(plugin_dir / "plugin.json")
    main_py = plugin_dir / "main.py"
    if not main_py.is_file() or main_py.stat().st_size == 0:
        raise PluginError("main.py is missing or empty in %s" % plugin_dir)
    index = plugin_dir / "dist/index.js"
    if not index.is_file():
        raise PluginError("dist/index.js is missing in %s" % plugin_dir)
    if index.stat().st_size < MIN_INDEX_BYTES:
        raise PluginError("dist/index.js looks truncated (%d bytes)" % index.stat().st_size)
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PluginError("dist/index.js is unreadable: %s" % exc)
    if "definePlugin" not in text:
        raise PluginError("dist/index.js does not contain definePlugin - wrong bundle?")
    info["dir"] = str(plugin_dir)
    return info


def verify_artifact(artifact: Path, repo: Path, recorded: dict | None = None,
                    require_recorded: bool = False) -> dict:
    """Validate one zip and (optionally) require it to match the recorded digest."""
    artifact = Path(artifact)
    if not artifact.is_file():
        raise PluginError("plugin artifact not found: %s" % artifact)
    digest = sha256_file(artifact)
    if recorded is None:
        try:
            recorded = read_recorded_sha(repo)
        except PluginError:
            if require_recorded:
                raise
            recorded = None
    if require_recorded:
        if not recorded:
            raise PluginError("no recorded artifact digest to verify %s" % artifact)
        if digest != recorded["sha256"]:
            raise PluginError(
                "artifact digest mismatch: %s\n  got      %s\n  recorded %s"
                % (artifact, digest, recorded["sha256"])
            )
    if not zipfile.is_zipfile(artifact):
        raise PluginError("artifact is not a zip file: %s" % artifact)

    with zipfile.ZipFile(artifact) as zf:
        names = zf.namelist()
        if len(names) != len(set(names)):
            raise PluginError("artifact has duplicate entries")
        for name in names:
            if name.startswith("/") or ".." in name.split("/"):
                raise PluginError("artifact has an unsafe entry: %r" % name)
        entries = frozenset(names)
        if entries != EXPECTED_ENTRIES:
            missing = sorted(EXPECTED_ENTRIES - entries)
            extra = sorted(entries - EXPECTED_ENTRIES)
            raise PluginError(
                "artifact layout is not the Decky plugin root (missing: %s; extra: %s)"
                % (", ".join(missing) or "-", ", ".join(extra) or "-")
            )
        try:
            manifest = json.loads(zf.read("plugin.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PluginError("artifact plugin.json is not valid JSON: %s" % exc)
        info = validate_manifest(manifest, "%s:plugin.json" % artifact.name)
        index = zf.read("dist/index.js")
        if len(index) < MIN_INDEX_BYTES:
            raise PluginError("artifact dist/index.js looks truncated")
        if b"definePlugin" not in index:
            raise PluginError("artifact dist/index.js does not contain definePlugin")
        if not zf.read("main.py").strip():
            raise PluginError("artifact main.py is empty")

    match = re.match(r"^oxp3-status-([^/]+)\.zip$", artifact.name)
    if match and match.group(1) != info["version"]:
        raise PluginError(
            "artifact filename says version %s but plugin.json says %s"
            % (match.group(1), info["version"])
        )
    return {
        "artifact": str(artifact),
        "sha256": digest,
        "version": info["version"],
        "recorded_sha256": (recorded or {}).get("sha256"),
        "recorded_match": bool(recorded and recorded["sha256"] == digest),
    }


# ---------------------------------------------------------------------------
# ownership marker and classification
# ---------------------------------------------------------------------------
def read_marker(plugin_dir: Path) -> dict | None:
    """Parse the ownership marker; None when absent or malformed."""
    path = marker_path(plugin_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data: dict = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            return None
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    if data.get("plugin") != MARKER_KIND or data.get("name") != PLUGIN_NAME:
        return None
    return data


def is_ours(plugin_dir: Path) -> bool:
    return read_marker(Path(plugin_dir)) is not None


def classify(plugin_dir: Path) -> str:
    """Return absent / ours / adoptable / foreign for one target directory."""
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.exists():
        return "absent"
    if not plugin_dir.is_dir():
        return "foreign"
    try:
        if is_ours(plugin_dir):
            return "ours"
    except OSError:
        return "foreign"
    if marker_path(plugin_dir).exists():
        # A marker that does not name this plugin is a tampered/foreign copy.
        return "foreign"
    try:
        validate_plugin_dir(plugin_dir)
    except (PluginError, OSError):
        return "foreign"
    return "adoptable"


def marker_text(info: dict, digest: str) -> str:
    return (
        "plugin=%s\n"
        "name=%s\n"
        "version=%s\n"
        "artifact_sha256=%s\n"
        "installed_at=%s\n"
        "installer=oxp3-hw-install.sh\n"
        % (
            MARKER_KIND,
            PLUGIN_NAME,
            info["version"],
            digest,
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ---------------------------------------------------------------------------
# backup / install / uninstall
# ---------------------------------------------------------------------------
def backup_dir(plugin_dir: Path, state_dir: Path) -> Path:
    """Copy the existing plugin directory into the decky-backups store."""
    plugin_dir = Path(plugin_dir)
    state_dir = Path(state_dir)
    base = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stamp = base
    n = 2
    dest = state_dir / "decky-backups" / stamp
    while dest.exists():
        stamp = "%s-%d" % (base, n)
        dest = state_dir / "decky-backups" / stamp
        n += 1
    dest.mkdir(parents=True)
    shutil.copytree(plugin_dir, dest / PLUGIN_DIR_NAME, symlinks=True)
    meta = {
        "origin": str(plugin_dir),
        "plugin": MARKER_KIND,
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "marker": read_marker(plugin_dir),
    }
    (dest / "backup.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return dest


def _extract(artifact: Path, destination: Path) -> None:
    with zipfile.ZipFile(artifact) as zf:
        for name in sorted(zf.namelist()):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(0o644)
        for directory in sorted(
            (p for p in destination.rglob("*") if p.is_dir()), reverse=True
        ):
            directory.chmod(0o755)
    destination.chmod(0o755)


def install(artifact: Path, home: Path, plugins_dir: str = "", root: str = "",
            state_dir: str = "", repo: Path | None = None,
            require_recorded: bool = False) -> dict:
    """Validate the zip and install it, backing up an existing directory first."""
    repo = Path(repo) if repo is not None else repo_root()
    info = verify_artifact(Path(artifact), repo, require_recorded=require_recorded)
    target = resolve_plugins_dir(Path(home), plugins_dir, root)
    state = resolve_state_dir(Path(home), root, state_dir)
    kind = classify(target)
    if kind == "foreign":
        raise PluginRefused(
            "refusing to replace %s: it has no %s marker naming %s (not ours)"
            % (target, MARKER_NAME, PLUGIN_NAME)
        )

    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PluginError(
            "cannot create %s (%s); the Decky plugins directory is root-owned - run the "
            "installer with sudo" % (parent, exc)
        )

    backup = None
    if target.exists():
        backup = backup_dir(target, state)

    tmp = parent / (".%s.install.%d" % (PLUGIN_DIR_NAME, os.getpid()))
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        tmp.mkdir(mode=0o755)
        _extract(Path(artifact), tmp)
        validate_plugin_dir(tmp)
        marker_path(tmp).write_text(marker_text(info, info["sha256"]), encoding="utf-8")
        marker_path(tmp).chmod(0o644)
        if target.exists():
            old = parent / (".%s.old.%d" % (PLUGIN_DIR_NAME, os.getpid()))
            if old.exists():
                shutil.rmtree(old)
            os.replace(target, old)
            try:
                os.replace(tmp, target)
            except Exception:
                os.replace(old, target)
                raise
            shutil.rmtree(old)
        else:
            os.replace(tmp, target)
    except OSError as exc:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise PluginError(
            "cannot install into %s (%s); the Decky plugins directory is root-owned - "
            "run the installer with sudo" % (target, exc)
        )
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    return {
        "dir": str(target),
        "version": info["version"],
        "artifact_sha256": info["sha256"],
        "previous": kind,
        "backup": str(backup) if backup else None,
    }


def uninstall(home: Path, plugins_dir: str = "", root: str = "") -> dict:
    """Remove exactly one plugin directory, and only when the marker says it is ours."""
    target = resolve_plugins_dir(Path(home), plugins_dir, root)
    kind = classify(target)
    if kind == "absent":
        return {"dir": str(target), "removed": False, "state": "absent"}
    if kind != "ours":
        raise PluginRefused(
            "refusing to delete %s: %s does not name %s (not ours)"
            % (target, MARKER_NAME, PLUGIN_NAME)
        )
    shutil.rmtree(target)
    return {"dir": str(target), "removed": True, "state": "removed"}


def status(home: Path, plugins_dir: str = "", root: str = "") -> dict:
    """Read-only state of the installed plugin directory."""
    target = resolve_plugins_dir(Path(home), plugins_dir, root)
    out: dict = {
        "dir": str(target),
        "installed": False,
        "state": "absent",
        "version": None,
        "artifact_sha256": None,
        "marker": False,
        "issues": [],
    }
    if not target.exists():
        return out
    marker = read_marker(target)
    if marker is not None:
        out["marker"] = True
        out["artifact_sha256"] = marker.get("artifact_sha256")
    try:
        info = validate_plugin_dir(target)
    except (PluginError, OSError) as exc:
        out["state"] = "foreign" if marker is None else "broken"
        out["issues"].append(str(exc))
        return out
    out["installed"] = True
    out["version"] = info["version"]
    out["state"] = "ok" if marker is not None else "unmarked"
    return out


def _status_line(info: dict) -> str:
    issues = ";".join(info.get("issues") or []) or "-"
    return (
        "installed=%s state=%s dir=%s version=%s marker=%s artifact_sha256=%s issues=%s"
        % (
            "yes" if info.get("installed") else "no",
            info.get("state", "?"),
            info.get("dir", "?"),
            info.get("version") or "-",
            "yes" if info.get("marker") else "no",
            info.get("artifact_sha256") or "-",
            issues.replace("\t", " ").replace("\n", " "),
        )
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", required=True, help="target user's home directory")
    parser.add_argument("--plugins-dir", default="",
                        help="override (default: homebrew/plugins/oxp3-status)")
    parser.add_argument("--root", default="", help="simulation prefix (--root DIR)")


def _add_state_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default="",
                        help="undo-state root (default: <home>/.local/state/oxp3-fixes)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Install, verify and remove the OXP3 Status Decky plugin.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve", help="print the resolved plugin directory")
    p.add_argument("--home", required=True)
    p.add_argument("--plugins-dir", default="")
    p.add_argument("--root", default="")

    p = sub.add_parser("verify-artifact", help="validate one zip and its recorded digest")
    p.add_argument("--artifact", required=True)
    p.add_argument("--repo", default=str(repo_root()))
    p.add_argument("--require-recorded", action="store_true",
                   help="refuse unless the digest matches the recorded value")

    p = sub.add_parser("install", help="install the plugin from a verified zip")
    _add_target_args(p)
    _add_state_arg(p)
    p.add_argument("--artifact", required=True)
    p.add_argument("--repo", default=str(repo_root()))
    p.add_argument("--require-recorded", action="store_true")

    p = sub.add_parser("uninstall", help="remove our plugin directory")
    _add_target_args(p)

    p = sub.add_parser("status", help="report the installed plugin state")
    _add_target_args(p)
    p.add_argument("--format", choices=("line", "json"), default="line")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            print(resolve_plugins_dir(Path(args.home), args.plugins_dir, args.root))
            return 0
        if args.command == "verify-artifact":
            info = verify_artifact(
                Path(args.artifact), Path(args.repo),
                require_recorded=args.require_recorded,
            )
            print("artifact: %s" % info["artifact"])
            print("version: %s" % info["version"])
            print("sha256: %s" % info["sha256"])
            if info["recorded_sha256"]:
                print(
                    "recorded: %s (%s)"
                    % (
                        info["recorded_sha256"],
                        "match" if info["recorded_match"] else "differs",
                    )
                )
            if info["recorded_sha256"] and not info["recorded_match"]:
                print(
                    "warning: the artifact digest differs from the recorded one; "
                    "installing a locally built zip is allowed, a reused one is not",
                    file=sys.stderr,
                )
            return 0
        if args.command == "install":
            info = install(
                Path(args.artifact), Path(args.home), args.plugins_dir, args.root,
                args.state_dir, Path(args.repo), args.require_recorded,
            )
            print("installed: %s" % info["dir"])
            print("version: %s" % info["version"])
            print("artifact_sha256: %s" % info["artifact_sha256"])
            print("previous: %s" % info["previous"])
            print("backup: %s" % (info["backup"] or "-"))
            return 0
        if args.command == "uninstall":
            info = uninstall(Path(args.home), args.plugins_dir, args.root)
            print("dir: %s" % info["dir"])
            print("state: %s" % info["state"])
            return 0
        if args.command == "status":
            info = status(Path(args.home), args.plugins_dir, args.root)
            if args.format == "json":
                print(json.dumps(info, indent=2))
            else:
                print(_status_line(info))
            return 0
    except PluginRefused as exc:
        print("%s: refusing: %s" % (PROG, exc), file=sys.stderr)
        return 3
    except PluginError as exc:
        print("%s: error: %s" % (PROG, exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("%s: error: %s" % (PROG, exc), file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
