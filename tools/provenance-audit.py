#!/usr/bin/env python3
"""provenance-audit - classify every tracked file and scan for copied vendor material.

The repository promises that it contains our own code, documentation and
generated data only: no vendor binaries, no firmware blobs, no decompiled
listings, no copied third-party source beyond upstream-licensed patch context.
This tool makes that promise checkable instead of aspirational:

* classifies every tracked file as (a) our code, (b) our documentation,
  (c) generated/derived data or authored configuration, (d) third-party
  licensed context / referenced facts, or (e) unclear;
* scans every file for binary magic, vendor binary extensions, base64 blobs,
  decompiler/disassembler output and long verbatim captures, and reports each
  hit with its classification and severity;
* exits 1 only on a *hard* finding (a tracked binary, a base64 payload or a
  decompiler listing).  Informational hits - a vendor identifier cited in a
  document, a captured HID frame, a generated test vector - are listed with
  their path so a reviewer can confirm them.

Usage
-----
    tools/provenance-audit.py                 # summary + findings, exit on hard hits
    tools/provenance-audit.py --all           # every file with its category
    tools/provenance-audit.py --json          # machine-readable result
    tools/provenance-audit.py --root /tmp/x   # audit a copy

The policy this tool enforces is documented in docs/PROVENANCE.md.  It is a
read-only audit: it never edits, normalises or removes anything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

CATEGORIES = {
    "a": "our original code",
    "b": "our documentation",
    "c": "generated/derived data + authored config",
    "d": "third-party licensed context / referenced facts",
    "e": "unclear - resolve before merge",
}

CODE_SUFFIXES = {
    ".py", ".sh", ".c", ".h", ".java", ".ts", ".tsx", ".js", ".mjs", ".fx",
}
DATA_SUFFIXES = {
    ".json", ".yaml", ".yml", ".csv", ".toml", ".conf", ".service", ".rules",
    ".hwdb", ".example", ".md.example",
}
BINARY_SUFFIXES = {
    ".dll", ".exe", ".sys", ".bin", ".efi", ".rom", ".sfi", ".ddc", ".rpm",
    ".zip", ".o", ".so", ".a", ".pyc", ".png", ".jpg", ".gif", ".pdf",
}

MAGIC = {
    b"\x7fELF": "ELF executable",
    b"MZ": "PE/DOS executable",
    b"PK\x03\x04": "ZIP archive",
    b"\x89PNG": "PNG image",
    b"%PDF": "PDF document",
    b"\x1f\x8b": "gzip stream",
    b"\xd0\xcf\x11\xe0": "OLE/CFB document (DLL/EXE/MSI container)",
    b"\xca\xfe\xba\xbe": "Mach-O/Java class container",
    b"\xcf\xfa\xed\xfe": "Mach-O image",
}

# Decompiler pseudo-code markers.  A couple are legitimate evidence pointers in
# prose; a file with several of them is a listing.
DECOMP_STRONG = re.compile(r"\b(undefined[1-8]|param_[0-9]|uVar[0-9]|iVar[0-9]|local_[0-9a-f]{2})\b")
DECOMP_WEAK = re.compile(r"\bFUN_[0-9a-f]{6,}\b")
BASE64ISH = re.compile(r"^(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9])[A-Za-z0-9+/]{400,}={0,2}$")
HEXRUN = re.compile(r"[0-9a-fA-F]{200,}")
VENDOR_WORDS = re.compile(
    r"\b(OneXConsole|Betterlife|SAGVPlugin|igdext64\.dll|app\.asar|UMDF)\b")
COPYRIGHT_WORDS = re.compile(r"(All rights reserved|confidential|proprietary)", re.IGNORECASE)

SKIP_DIRS = {".git", "node_modules", "build", "dist", "__pycache__", "out",
             ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def iter_files(root: Path) -> list[str]:
    git = shutil.which("git")
    if git and (root / ".git").exists():
        proc = subprocess.run(
            [git, "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if proc.returncode == 0:
            return sorted(p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p)
    found = []
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
            return fh.readline()
    except OSError:
        return ""


def classify(rel: str, root: Path) -> str:
    """Path-based category; the rule table is repeated in docs/PROVENANCE.md."""
    path = root / rel
    name = path.name
    if rel.startswith("patches/"):
        return "d"
    if rel.endswith(".md") or name == "README.md":
        return "b"
    if rel.startswith("docs/"):
        # Markdown is prose; state dumps, CSVs, the registry and captured
        # frames are generated/measured data.
        return "b" if rel.endswith(".md") else "c"
    if rel.startswith("upstream/"):
        return "b"
    if rel.startswith("tests/"):
        return "a" if rel == "tests/verify/run.sh" else "c"
    if rel.startswith("tools/"):
        if rel.startswith("tools/fingerprint/tests/"):
            if rel.endswith(("vectors.h", "vectors.json")) or "/fixtures/" in rel:
                return "c"
        if rel in {"tools/audio/oxp3-speaker-eq.conf",
                   "tools/oxp3-rgbd.toml.example"}:
            return "c"
        if (path.suffix in CODE_SUFFIXES or name in {"Makefile", "Containerfile"}
                or first_line(path).startswith("#!")):
            return "a"
        if name in {"build.sh", "install.sh", "check-libfprint.sh", "run-c-tests.sh"}:
            return "a"
        return "c"
    if rel.startswith("files/"):
        return "a" if first_line(path).startswith("#!") else "c"
    if rel.startswith("container/"):
        return "a"
    if rel in {".gitignore"}:
        return "c"
    if rel in {"Makefile", "oxp3-fixes.sh", "oxp3-fixes-fast.sh", "state-snapshot.sh"}:
        return "a"
    return "e"


def scan(rel: str, text: str) -> list[dict]:
    """Return findings for one file.  severity is 'hard' or 'info'."""
    findings: list[dict] = []
    path = Path(rel)
    suffix = path.suffix.lower()

    if suffix in BINARY_SUFFIXES:
        findings.append({"severity": "hard", "label": "vendor/binary extension",
                         "path": rel, "line": 0})
    if text.startswith("MZ") or text.startswith("\x7fELF"):
        findings.append({"severity": "hard", "label": "binary magic", "path": rel, "line": 0})
    else:
        for magic, label in MAGIC.items():
            if text.encode("utf-8", "surrogateescape").startswith(magic):
                findings.append({"severity": "hard", "label": label, "path": rel, "line": 0})
                break

    lines = text.splitlines()
    strong = 0
    for number, line in enumerate(lines, 1):
        if DECOMP_STRONG.search(line):
            strong += 1
            if strong == 3:
                findings.append({"severity": "hard", "label": "decompiled pseudo-code listing",
                                 "path": rel, "line": number})
                break

    for number, line in enumerate(lines, 1):
        for token in re.split(r"[\s\"',;()<>\[\]]+", line):
            token = token.strip()
            if len(token) >= 400 and BASE64ISH.match(token):
                findings.append({"severity": "hard", "label": "base64-ish blob",
                                 "path": rel, "line": number})
                break
        if HEXRUN.search(line):
            findings.append({"severity": "info", "label": "long hex run (capture/vector)",
                             "path": rel, "line": number})
        if DECOMP_WEAK.search(line):
            findings.append({"severity": "info", "label": "decompiled-function identifier cited",
                             "path": rel, "line": number})
        if VENDOR_WORDS.search(line):
            findings.append({"severity": "info", "label": "vendor name referenced",
                             "path": rel, "line": number})
        if COPYRIGHT_WORDS.search(line):
            findings.append({"severity": "info", "label": "copyright/proprietary wording",
                             "path": rel, "line": number})
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--all", action="store_true", help="print every file with its category")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"provenance-audit: --root is not a directory: {root}", file=sys.stderr)
        return 2

    files = iter_files(root)
    categories: dict[str, list[str]] = defaultdict(list)
    findings: list[dict] = []
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append({"severity": "hard", "label": f"unreadable: {exc}",
                             "path": rel, "line": 0})
            continue
        categories[classify(rel, root)].append(rel)
        findings.extend(scan(rel, text))

    hard = [f for f in findings if f["severity"] == "hard"]
    info = [f for f in findings if f["severity"] == "info"]
    counts = Counter(f["label"] for f in hard)
    info_counts = Counter(f["label"] for f in info)

    if args.as_json:
        print(json.dumps({
            "root": str(root),
            "tracked_files": len(files),
            "categories": {key: len(categories.get(key, [])) for key in CATEGORIES},
            "unclear": categories.get("e", []),
            "hard_findings": hard,
            "info_findings": info,
            "hard": len(hard),
            "info": len(info),
        }, indent=2))
        return 1 if hard else 0

    print(f"provenance audit: {len(files)} tracked file(s) under {root}")
    for key, label in CATEGORIES.items():
        print(f"  ({key}) {label:48} {len(categories.get(key, [])):4d}")
    if categories.get("e"):
        print("  unclear files:")
        for rel in categories["e"]:
            print(f"    {rel}")
    for key in CATEGORIES:
        if args.all:
            for rel in categories.get(key, []):
                print(f"{key} {rel}")

    print(f"findings: {len(hard)} hard, {len(info)} informational")
    for label, count in sorted(counts.items()):
        print(f"  HARD {label}: {count}")
        for finding in [f for f in hard if f["label"] == label][:10]:
            print(f"       {finding['path']}:{finding['line']}")
    for label, count in sorted(info_counts.items()):
        print(f"  info {label}: {count}")
        for finding in [f for f in info if f["label"] == label][:3]:
            print(f"       {finding['path']}:{finding['line']}")
    if len(hard) == 0:
        print("result: clean - no tracked binary, blob or decompiled listing found")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
