#!/usr/bin/env python3
"""apply-license - apply ONE licence decision to the repository, reviewably.

This tool does not choose a licence and does not grant rights by itself.  The
owner passes ``--spdx``; the tool then makes the tree internally consistent:

* writes ``LICENSE`` with the canonical text of the primary licence;
* writes ``LICENSES/<id>.txt`` for every ``--except`` identifier, when the
  canonical text is available (fetched, or supplied with
  ``--exception-license-file``);
* adds one ``SPDX-License-Identifier`` line to every hand-written source file
  that does not have one, preserving shebangs, Python coding lines, existing
  comment blocks and module docstrings;
* never rewrites an existing SPDX identifier - a file that already declares a
  different licence is reported as a conflict and left alone, because that is
  a provenance fact, not something a bulk tool may overwrite.

MIT text is small and is authored here.  LGPL-2.1, GPL-2.0 and Apache-2.0 texts
are long, so they are NEVER typed from memory: they are fetched from the
official URL when the network works, otherwise ``--license-file`` (primary) or
``--exception-license-file ID=PATH`` is required.  Everything is stdlib-only.

Scope
-----
In scope: ``*.py *.sh *.c *.h *.java *.ts *.tsx *.js *.mjs *.fx``, both
``Makefile`` files, and extensionless scripts whose first line is a shebang
(``files/oxp3-audio-route``, ``tools/oxp3-voltap/oxp3-voltap``, ...).
Out of scope: documentation, units/rules/hwdb/conf/toml/json data, and
generated files, which are owned by their generator and must stay
byte-identical to its output (``--check`` reports them as exempt rather than
missing).

Usage
-----
    # dry run: exit 1 and list what would change; nothing is written
    tools/apply-license.py --check --spdx MIT

    # apply MIT everywhere
    tools/apply-license.py --write --spdx MIT --holder "Your Name"

    # offline: supply long licence texts instead of fetching them
    tools/apply-license.py --write --spdx LGPL-2.1-or-later \\
        --license-file /path/to/lgpl-2.1.txt

    # review the exact scope, then run --check twice (it is idempotent)
    tools/apply-license.py --list-scope --spdx MIT

Exit codes: 0 = clean (check) / applied (write); 1 = work left, missing headers
or a conflicting SPDX id; 2 = usage error, unsupported licence id, or required
licence text unavailable.
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from fnmatch import fnmatch
from pathlib import Path

# ---------------------------------------------------------------------------
# licence table: SPDX id -> canonical text source
# ---------------------------------------------------------------------------
# `osi` documents that the identifier is OSI-approved; a new id is deliberately
# not accepted without adding its canonical source here.
LICENCES = {
    "MIT": {
        "osi": True,
        "url": None,  # authored below
        "markers": ("MIT License", "Permission is hereby granted"),
    },
    "Apache-2.0": {
        "osi": True,
        "url": "https://www.apache.org/licenses/LICENSE-2.0.txt",
        "markers": ("Apache License", "Version 2.0"),
    },
    "LGPL-2.1-or-later": {
        "osi": True,
        "url": "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt",
        "markers": ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 2.1"),
    },
    "GPL-2.0-or-later": {
        "osi": True,
        "url": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt",
        "markers": ("GNU GENERAL PUBLIC LICENSE", "Version 2"),
    },
}

# Canonical MIT licence text (short; authored, not fetched).  Placeholders are
# substituted with --year / --holder.
MIT_TEXT = """MIT License

Copyright (c) <YEAR> <HOLDER>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# ---------------------------------------------------------------------------
# header styles and file selection
# ---------------------------------------------------------------------------
STYLE_BY_SUFFIX = {
    ".py": "hash",
    ".sh": "hash",
    ".c": "c",
    ".h": "c",
    ".java": "slash",
    ".ts": "slash",
    ".tsx": "slash",
    ".js": "slash",
    ".mjs": "slash",
    ".fx": "slash",
}
STYLE_BY_NAME = {"Makefile": "hash"}

# Generated files are owned by their generator; a hand-written header here
# would be erased on the next regeneration.
GENERATED = ()

SPDX_RE = re.compile(r"SPDX-License-Identifier:\s*([A-Za-z0-9.+-]+)")
CODING_RE = re.compile(r"^#.*coding[:=]\s*[-\w.]+")

SKIP_DIRS = {".git", "node_modules", "build", "dist", "__pycache__", "out",
             ".venv", "venv", ".mypy_cache", ".pytest_cache"}
DEFAULT_HOLDER = "The oxp3-anatase-fixes authors"


class UsageError(Exception):
    """Bad command line / unavailable canonical text (exit 2)."""


def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    raise UsageError(message)


# ---------------------------------------------------------------------------
# file enumeration
# ---------------------------------------------------------------------------
def iter_files(root: Path) -> list[str]:
    """Repository-relative paths, tracked by git when possible, else walked."""
    git = shutil.which("git")
    if git and (root / ".git").exists():
        proc = subprocess.run(
            [git, "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if proc.returncode == 0:
            return sorted(p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p)
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found.append(rel.as_posix())
    return found


def first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().rstrip("\n")
    except OSError:
        return ""


def style_of(root: Path, rel: str) -> str | None:
    path = root / rel
    if path.name in STYLE_BY_NAME:
        return STYLE_BY_NAME[path.name]
    suffix = path.suffix
    if suffix in STYLE_BY_SUFFIX:
        return STYLE_BY_SUFFIX[suffix]
    if first_line(path).startswith("#!"):
        return "hash"
    return None


def scope(root: Path) -> tuple[list[str], list[str]]:
    """Return (in-scope source files, generated files that look in scope)."""
    sources: list[str] = []
    generated: list[str] = []
    for rel in iter_files(root):
        if style_of(root, rel) is None:
            continue
        if rel in GENERATED:
            generated.append(rel)
        else:
            sources.append(rel)
    return sources, generated


# ---------------------------------------------------------------------------
# expected identifier per file
# ---------------------------------------------------------------------------
def check_id(spdx: str, where: str = "--spdx") -> str:
    if spdx not in LICENCES:
        die(f"unsupported SPDX id {spdx!r}; supported: {', '.join(sorted(LICENCES))}")
    if not LICENCES[spdx].get("osi"):
        die(f"{where}: {spdx!r} is not marked OSI-approved in this tool's table")
    return spdx


def parse_exceptions(specs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for spec in specs:
        if "=" not in spec:
            die(f"--except needs GLOB=SPDX (got {spec!r})")
        glob, spdx = spec.rsplit("=", 1)
        if not glob:
            die(f"--except needs a non-empty glob (got {spec!r})")
        check_id(spdx, "--except")
        out.append((glob, spdx))
    return out


def expected_for(rel: str, primary: str, exceptions: list[tuple[str, str]]) -> str:
    for glob, spdx in exceptions:
        if fnmatch(rel, glob):
            return spdx
    return primary


# ---------------------------------------------------------------------------
# header read / insert
# ---------------------------------------------------------------------------
def existing_spdx(text: str) -> str | None:
    for line in text.splitlines()[:10]:
        match = SPDX_RE.search(line)
        if match:
            return match.group(1)
    return None


def header_line(style: str, spdx: str) -> str:
    if style == "c":
        return f"/* SPDX-License-Identifier: {spdx} */"
    prefix = "#" if style == "hash" else "//"
    return f"{prefix} SPDX-License-Identifier: {spdx}"


def insert_header(text: str, style: str, spdx: str) -> str:
    lines = text.splitlines(keepends=True)
    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
        # PEP 263 coding line must stay within the first two lines.
        if style == "hash" and len(lines) > 1 and CODING_RE.match(lines[1]):
            index = 2
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    lines.insert(index, header_line(style, spdx) + newline)
    return "".join(lines)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# canonical licence text
# ---------------------------------------------------------------------------
def fetch_text(spdx: str) -> str:
    url = LICENCES[spdx]["url"]
    if not url:
        die(f"{spdx} has no remote canonical text; this should not happen")
    request = urllib.request.Request(
        url, headers={"User-Agent": "oxp3-apply-license/1 (offline-first repo tool)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(300_000)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        die(f"could not fetch {spdx} text from {url} ({exc}); "
            f"re-run with --license-file PATH (primary) or "
            f"--exception-license-file {spdx}=PATH (exception)")
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    markers = LICENCES[spdx]["markers"]
    missing = [m for m in markers if m not in text]
    if missing:
        die(f"fetched text for {spdx} does not look canonical (missing {missing!r}); "
            f"refusing to write it - use --license-file PATH instead")
    if not text.endswith("\n"):
        text += "\n"
    return text


def read_license_file(path: str, label: str) -> str:
    file = Path(path)
    if not file.is_file():
        die(f"{label} file does not exist: {path}")
    text = file.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if not text.strip():
        die(f"{label} file is empty: {path}")
    if not text.endswith("\n"):
        text += "\n"
    return text


def canonical_text(spdx: str, holder: str, year: int,
                   license_file: str | None,
                   exception_files: dict[str, str]) -> str:
    if spdx in exception_files:
        return read_license_file(exception_files[spdx], spdx)
    if license_file and spdx != "MIT":
        return read_license_file(license_file, spdx)
    if spdx == "MIT":
        if license_file:
            return read_license_file(license_file, "MIT")
        return MIT_TEXT.replace("<YEAR>", str(year)).replace("<HOLDER>", holder)
    return fetch_text(spdx)


# ---------------------------------------------------------------------------
# write helpers
# ---------------------------------------------------------------------------
def write_if_changed(path: Path, text: str, force: bool) -> bool:
    """Return True when the file was written, False when already identical."""
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="replace")
        if current == text:
            return False
        if not force:
            die(f"refusing to overwrite {path} (different content); pass --force "
                f"or remove it deliberately")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def looks_like(spdx: str, text: str) -> bool:
    return all(marker in text for marker in LICENCES[spdx]["markers"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply one SPDX licence decision (LICENSE + per-file headers).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----\n", 1)[-1],
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="dry run (default): verify LICENSE and headers, write nothing")
    mode.add_argument("--write", action="store_true",
                      help="write LICENSE/LICENSES and add missing SPDX headers")
    parser.add_argument("--spdx", help="primary SPDX id, e.g. MIT")
    parser.add_argument("--holder", default=DEFAULT_HOLDER,
                        help=f'copyright holder for MIT (default: "{DEFAULT_HOLDER}")')
    parser.add_argument("--year", type=int, default=datetime.date.today().year,
                        help="copyright year for MIT (default: current year)")
    parser.add_argument("--except", dest="exceptions", action="append", default=[], metavar="GLOB=SPDX",
                        help="per-path licence override, repeatable, e.g. "
                             "'patches/**=GPL-2.0-only'")
    parser.add_argument("--license-file", metavar="PATH",
                        help="canonical text for the primary id instead of fetching it")
    parser.add_argument("--exception-license-file", action="append", default=[],
                        metavar="ID=PATH", help="canonical text for an exception id")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="repository root (default: parent of tools/)")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing LICENSE/LICENSES text with different content")
    parser.add_argument("--list-scope", action="store_true",
                        help="print every in-scope file and its expected SPDX id, then exit")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    write = args.write and not args.check
    root = Path(args.root).resolve()
    if not root.is_dir():
        die(f"--root is not a directory: {root}")
    if not args.spdx:
        die("--spdx is required (this tool will not choose a licence for you)")
    check_id(args.spdx)
    exceptions = parse_exceptions(args.exceptions)

    exception_files: dict[str, str] = {}
    for spec in args.exception_license_file:
        if "=" not in spec:
            die(f"--exception-license-file needs ID=PATH (got {spec!r})")
        spdx, path = spec.split("=", 1)
        check_id(spdx, "--exception-license-file")
        exception_files[spdx] = path

    sources, generated = scope(root)
    expected = {rel: expected_for(rel, args.spdx, exceptions) for rel in sources}

    if args.list_scope:
        for rel in sources:
            print(f"{expected[rel]:18} {rel}")
        for rel in generated:
            print(f"{'(generated)':18} {rel}")
        print(f"\n{len(sources)} file(s) in scope, {len(generated)} generated/exempt")
        return 0

    # ---- inspect current state -------------------------------------------
    missing: list[str] = []
    conflicts: list[tuple[str, str]] = []
    ok = 0
    for rel, want in expected.items():
        text = read_text(root / rel)
        have = existing_spdx(text)
        if have is None:
            missing.append(rel)
        elif have != want:
            conflicts.append((rel, have))
        else:
            ok += 1

    license_path = root / "LICENSE"
    license_state = "missing"
    if license_path.is_file():
        text = license_path.read_text(encoding="utf-8", errors="replace")
        license_state = "ok" if looks_like(args.spdx, text) else "unexpected-content"

    summary = (
        f"scope {len(sources)} file(s) (+{len(generated)} generated exempt): "
        f"{ok} header(s) ok, {len(missing)} missing, {len(conflicts)} conflict(s); "
        f"LICENSE {license_state}"
    )

    # ---- check mode ------------------------------------------------------
    if not write:
        print(summary)
        if not args.quiet:
            for rel in missing[:15]:
                print(f"  missing   {rel}  -> {expected[rel]}")
            if len(missing) > 15:
                print(f"  ... and {len(missing) - 15} more (see --list-scope)")
            for rel, have in conflicts:
                print(f"  conflict  {rel}  has {have}, expected {expected[rel]}")
        if license_state == "missing":
            print("  LICENSE is missing; --write would create it")
        elif license_state == "unexpected-content":
            print(f"  LICENSE does not look like {args.spdx}; --write --force would replace it")
        return 0 if not missing and not conflicts and license_state == "ok" else 1

    # ---- write mode ------------------------------------------------------
    holder = args.holder or DEFAULT_HOLDER
    primary_text = canonical_text(args.spdx, holder, args.year,
                                  args.license_file, exception_files)
    wrote_license = write_if_changed(license_path, primary_text, args.force)
    if not args.quiet:
        print(f"{'wrote' if wrote_license else 'kept'}   LICENSE ({args.spdx})")

    exception_ids = sorted({spdx for _, spdx in exceptions} - {args.spdx})
    for spdx in exception_ids:
        text = canonical_text(spdx, holder, args.year, None, exception_files)
        path = root / "LICENSES" / f"{spdx}.txt"
        wrote = write_if_changed(path, text, args.force)
        if not args.quiet:
            print(f"{'wrote' if wrote else 'kept'}   LICENSES/{spdx}.txt")

    added = 0
    for rel in missing:
        path = root / rel
        text = read_text(path)
        path.write_text(insert_header(text, style_of(root, rel) or "hash", expected[rel]),
                        encoding="utf-8")
        added += 1
    for rel, have in conflicts:
        print(f"conflict  {rel}: keeps its existing {have} (not overwritten)", file=sys.stderr)

    print(f"scope {len(sources)} file(s) (+{len(generated)} generated exempt): "
          f"{ok} header(s) already ok, {added} added, {len(conflicts)} conflict(s); "
          f"LICENSE {'written' if wrote_license else 'kept'}")
    if conflicts:
        print("Resolve the conflicts by hand or add a matching --except; nothing else changed.",
              file=sys.stderr)
        return 1
    print("next: re-run the same command with --check (it must be clean and is idempotent)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except UsageError as exc:
        print(f"apply-license: {exc}", file=sys.stderr)
        sys.exit(2)
