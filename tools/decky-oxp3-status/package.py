#!/usr/bin/env python3
"""Build the Decky "install from file" zip for the OXP3 Status plugin.

Zip layout (plugin root at the archive root, which is what Decky Loader's
installer expects):

    plugin.json
    main.py
    README.md
    dist/index.js
    dist/index.js.map

The zip is deterministic (fixed timestamps, sorted entries) so a rebuild from
the same dist/ has the same bytes and hash. Run from the plugin directory.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILES = (
    "plugin.json",
    "main.py",
    "README.md",
    "dist/index.js",
    "dist/index.js.map",
)


def fail(message: str) -> "None":
    print("package: %s" % message, file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    manifest_path = ROOT / "plugin.json"
    if not manifest_path.is_file():
        fail("plugin.json is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        fail("plugin.json is not valid JSON: %s" % exc)
    for key in ("name", "author", "api_version"):
        if key not in manifest:
            fail("plugin.json has no %r" % key)
    if not isinstance(manifest.get("api_version"), int):
        fail("plugin.json api_version must be an integer")

    try:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        package = {}
    version = str(package.get("version") or "0.0.0")

    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        fail("missing build inputs: %s (run `npm run build` first)" % ", ".join(missing))

    index = ROOT / "dist/index.js"
    text = index.read_text(encoding="utf-8", errors="replace")
    if len(text) < 1024:
        fail("dist/index.js looks truncated (%d bytes)" % len(text))
    if "definePlugin" not in text:
        fail("dist/index.js does not contain definePlugin - wrong bundle?")

    BUILD.mkdir(parents=True, exist_ok=True)
    target = BUILD / ("oxp3-status-%s.zip" % version)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(FILES):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (ROOT / name).read_bytes())

    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    print(
        "package: %s  %d bytes  sha256 %s  (%d entries)"
        % (target.relative_to(ROOT), len(data), digest, len(FILES))
    )
    for name in FILES:
        print("  %-18s %7d bytes" % (name, (ROOT / name).stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
