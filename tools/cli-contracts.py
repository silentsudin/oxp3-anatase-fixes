#!/usr/bin/env python3
"""cli-contracts - systematic CLI contract checks for every tool in this repo.

The offline suites test what each tool *does*; this tool tests how every tool
*speaks*: that it recognises ``--help``, prints usage instead of a traceback,
uses the repository's exit-code convention, and writes nothing outside a cache
or temporary directory while doing so.  It is the automated form of the manual
matrix in ``docs/state/audit-2026-11-09.txt`` and runs as the ``cli-contracts``
suite of ``tools/repo-test.sh``.

Conventions asserted (documented in ``docs/TESTING.md`` section 11):

* ``--help`` exits 0, or 2 for a usage-style help; a documented deviation must
  be listed in ``HELP_EXCEPTIONS`` with its reason, so the exception is visible
  in the code and in the output instead of being skipped silently.
* ``--help`` prints usage text (``usage``, an option list or a subcommand list),
  never a Python traceback, and never hangs.
* A flag advertised by the help text (``--selftest``, ``--check``, ``--dry-run``)
  is exercised too: exit 0 (success), 1 (verdict/failure) or 2 (usage), no
  traceback, no writes.  Probes that would download, mutate host state or need
  arguments are listed in ``PROBE_DENY`` with a reason.
* No probe creates or modifies a file in the repository.  The probes run in a
  throw-away copy of the working tree with ``HOME``, the XDG directories and
  ``TMPDIR`` redirected into a private directory, so a tool that writes to its
  cache passes and a tool that writes into the checkout fails.
* The tool list is discovered from ``git ls-files``: any new tool with a help
  path is covered automatically.  There is no hand-maintained list of tools.

Usage:

    tools/cli-contracts.py             run every probe, print one line per tool
    tools/cli-contracts.py --selftest  offline test of the classifier and helpers
    tools/cli-contracts.py --list      list the discovered tools and exit
    tools/cli-contracts.py --help      this text

Exit codes: 0 = every contract holds, 1 = at least one violation (or timeout),
2 = usage or setup error.  Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

PROG = "cli-contracts"
VERSION = "1.0.0"

REPO = pathlib.Path(__file__).resolve().parent.parent

# Library, fixture and build subtrees that contain no standalone CLI.
EXCLUDE_PREFIXES = (
    "tools/lib/",
    "tools/fingerprint/tests/",
    "tools/fingerprint/fpbf63112/",
    "tools/decky-oxp3-status/node_modules/",
    "tools/decky-oxp3-status/dist/",
    "tools/decky-oxp3-status/build/",
)

# Documented deviations from "--help exits 0 or 2 with usage text".
# path -> (accepted exit codes, usage text required, reason)
HELP_EXCEPTIONS = {
    "tools/oxp3-sagv.sh": (
        frozenset({4}), True,
        "header documents 4 = usage (status/probe codes are 0/3); unit tests pin it",
    ),
    "tools/sleep-power-measure.sh": (
        frozenset({3}), True,
        "header documents 3 = usage (audit/snapshot codes are 0)",
    ),
    "tools/display/display-mode.sh": (
        frozenset({1}), False,
        "no --help handler: an unknown subcommand exits 1 with one line; the "
        "subcommands are listed in the header",
    ),
    "tools/display/vibrance.sh": (
        frozenset({2}), False,
        "no --help handler: an unknown subcommand exits 2 (its documented usage "
        "code) without printing the header",
    ),
    "tools/ghidra-pod.sh": (
        frozenset({1}), True,
        "requires <agent-name> <binary>; ${1:?usage:...} prints the usage line "
        "and exits 1",
    ),
    "tools/oxp3-voltap/install.sh": (
        frozenset({1}), True,
        "no --help handler: a missing/unknown action prints usage and exits 1",
    ),
}

# Advertised flags whose bare invocation is not a safe, offline, fast probe.
# (path, flag) -> reason
PROBE_DENY = {
    ("tools/oxp3-bt-firmware.sh", "dry-run"):
        "downloads the two pinned firmware files to verify hashes (network)",
    ("tools/oxp3-hw-install.sh", "dry-run"):
        "needs a component selection and an unprivileged target user; the "
        "installer-dry-run suite runs --dry-run --all",
    ("tools/oxp3-firstrun.sh", "dry-run"):
        "without --collect arguments it runs the full collection (calls "
        "oxp3-verify twice); the firstrun suite runs a targeted dry run",
    ("tools/oxp3_firstrun.py", "dry-run"):
        "same backend as tools/oxp3-firstrun.sh; the firstrun suite covers it",
    ("tools/oxp3-adaptivetdp.py", "dry-run"):
        "samples a decision interval (slow); covered by the unit suite",
}

HELP_OK_CODES = frozenset({0, 2})
SECONDARY_OK_CODES = frozenset({0, 1, 2})
HELP_TIMEOUT = 15
SECONDARY_TIMEOUT = 25
TRACEBACK = "Traceback (most recent call last)"

USAGE_RE = re.compile(r"(?i)\busage\b|\boptions?\b|\bcommands?\b|subcommands?\b|--[a-z]")

# A flag advertised in an option list: "  --check ..." or "  --dry-run, -n ...".
OPTION_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:-[A-Za-z],?[ \t]+)?--(selftest|check|dry-run)(?=[ \t,=]|$)")

HELP_BRANCH_RE = re.compile(r"-h\s*\|\s*--help|--help\s*\)\s*|\"--help\"|'--help'")
ARG_USE_RE = re.compile(r"\$\{?1\b|\$@|getopts|while \[ \$#")
PY_EXEC_RE = re.compile(r"exec\s+(?:\"?\$py\"?|python3?|python)\s+.*\"\$@\"")
LAUNCHER_RE = re.compile(r"exec\s+(?:flatpak|flatpak-spawn|proton|wine)\b")
MAIN_RE = re.compile(r"^if __name__ == ['\"]__main__['\"]", re.M)
ARGPARSE_RE = re.compile(r"^(?:import argparse|from argparse import)", re.M)


class Problem(Exception):
    """Setup or usage problem (exit 2)."""


# ---------------------------------------------------------------------------
# discovery and classification
# ---------------------------------------------------------------------------
def tracked_files():
    """Repository-relative paths of every git-tracked file.

    A checkout without ``.git`` (a release tarball, or the throw-away copy the
    probes run in) falls back to walking the tree; there is no ignore file to
    honour then, so only the directories this tool never looks at are dropped.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            capture_output=True, check=False)
    except OSError:
        res = None
    if res is not None and res.returncode == 0:
        return sorted(p.decode("utf-8") for p in res.stdout.split(b"\0") if p)
    walked = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "build", "dist")]
        for name in filenames:
            walked.append(str((pathlib.Path(dirpath) / name).relative_to(REPO)))
    return sorted(walked)


def classify_tool_text(path, text):
    """Return the detected CLI interface for one file, or None.

    The result is a dict with ``kind`` (``python`` or ``shell``) and ``via``
    (why it was classified as a CLI, used in the report).  This is deliberately
    conservative: a file that only forwards "$@" to another program (a game
    launcher) is not a CLI under test.
    """
    name = path.rsplit("/", 1)[-1]
    first = text.splitlines()[0] if text else ""
    python_shebang = "python" in first and first.startswith("#!")
    if path.endswith(".py") or python_shebang:
        if not MAIN_RE.search(text):
            return None
        if ARGPARSE_RE.search(text):
            return {"kind": "python", "via": "argparse"}
        if "--help" in text:
            return {"kind": "python", "via": "manual --help"}
        return None
    if name.endswith(".sh") or text.startswith("#!"):
        if LAUNCHER_RE.search(text):
            return None
        if HELP_BRANCH_RE.search(text):
            return {"kind": "shell", "via": "-h/--help branch"}
        if PY_EXEC_RE.search(text):
            return {"kind": "shell", "via": "python backend delegation"}
        if re.search(r"(?i)\busage\b", text) and ARG_USE_RE.search(text):
            return {"kind": "shell", "via": "usage fallback"}
        return None
    return None


def is_candidate_path(path):
    if path.startswith(EXCLUDE_PREFIXES):
        return False
    if path.startswith("tools/"):
        return True
    return "/" not in path and not path.startswith(".")


def enumerate_tools(paths=None):
    """Return (tools, skipped): tools is [(path, kind, via)]."""
    tools, skipped = [], []
    for path in paths if paths is not None else tracked_files():
        if not is_candidate_path(path):
            continue
        full = REPO / path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise Problem("cannot read %s: %s" % (path, exc))
        detected = classify_tool_text(path, text)
        if detected is None:
            skipped.append(path)
            continue
        tools.append((path, detected["kind"], detected["via"]))
    return tools, skipped


def executable_command(path, kind):
    full = REPO / path
    if kind == "python" or not os.access(full, os.X_OK):
        return [sys.executable, str(full)]
    return [str(full)]


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------
class Sandbox:
    """A throw-away copy of the working tree plus isolated HOME/cache/tmp."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="oxp3-cli-contracts-"))
        self.tree = self.root / "repo"
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        for directory in (self.home / ".cache", self.home / ".config",
                          self.home / ".local" / "data",
                          self.home / ".local" / "state"):
            directory.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "data"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "TMPDIR": str(self.tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C",
        })
        self.env.pop("OXP3_PYTHON", None)

    def copy_tree(self):
        """Copy the tracked and untracked-but-not-ignored working tree."""
        listing = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=False)
        if listing.returncode != 0:
            raise Problem("git ls-files failed while building the probe tree")
        self.tree.mkdir(parents=True, exist_ok=True)
        for raw in listing.stdout.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8")
            src = REPO / rel
            if not src.is_file():
                continue
            dst = self.tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- write detection ---------------------------------------------------
    def snapshot(self):
        """path -> (size, mtime_ns) for every file in the probe tree."""
        state = {}
        for dirpath, _dirnames, filenames in os.walk(self.tree):
            for name in filenames:
                full = pathlib.Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                state[str(full.relative_to(self.tree))] = (st.st_size, st.st_mtime_ns)
        return state

    @staticmethod
    def changed(before, after):
        """Repository-relative paths created, removed or modified."""
        changed = []
        for path in sorted(set(before) | set(after)):
            if before.get(path) != after.get(path):
                changed.append(path)
        return changed

    def run_probe(self, path, kind, args, timeout):
        """Run one tool probe inside the sandbox; returns (rc, output)."""
        tree_path = self.tree / path
        if kind == "python" or not os.access(tree_path, os.X_OK):
            cmd = [sys.executable, str(tree_path)] + args
        else:
            cmd = [str(tree_path)] + args
        try:
            res = subprocess.run(
                cmd, cwd=str(self.tree), env=self.env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "TIMEOUT after %ds" % timeout
        except OSError as exc:
            return None, "cannot execute: %s" % exc
        return res.returncode, (res.stdout or "") + (res.stderr or "")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def advertised_flags(help_output):
    """Flags advertised in an option list, in a stable order."""
    found = {m.group(1) for m in OPTION_LINE_RE.finditer(help_output)}
    return [flag for flag in ("selftest", "check", "dry-run") if flag in found]


def check_help(path, rc, output):
    """Return a list of violation strings for one --help invocation."""
    codes, need_usage, reason = HELP_EXCEPTIONS.get(path, (HELP_OK_CODES, True, ""))
    problems = []
    if rc is None:
        return ["--help: %s" % output]
    if TRACEBACK in output:
        problems.append("--help: traceback")
    if rc not in codes:
        allowed = "/".join(str(c) for c in sorted(codes))
        extra = " (%s)" % reason if reason else ""
        problems.append("--help: exit %d, expected %s%s" % (rc, allowed, extra))
    if need_usage and not USAGE_RE.search(output):
        problems.append("--help: no usage text")
    if not output.strip():
        problems.append("--help: no output")
    return problems


def check_secondary(path, flag, rc, output):
    problems = []
    if rc is None:
        return ["--%s: %s" % (flag, output)]
    if TRACEBACK in output:
        problems.append("--%s: traceback" % flag)
    if rc not in SECONDARY_OK_CODES:
        problems.append("--%s: exit %d, expected 0/1/2" % (flag, rc))
    return problems


def run(verbose=True):
    tools, skipped = enumerate_tools()
    if not tools:
        raise Problem("no CLI tools discovered - did the classification change?")

    # Every documented deviation must point at a discovered tool, otherwise it
    # silently rots when a path is renamed.
    discovered = {path for path, _kind, _via in tools}
    stale = sorted(set(HELP_EXCEPTIONS) - discovered)
    stale += sorted({path for path, _flag in PROBE_DENY} - discovered)
    if stale:
        raise Problem(
            "HELP_EXCEPTIONS/PROBE_DENY name files that are not discovered CLI "
            "tools: %s" % ", ".join(sorted(set(stale))))

    sandbox = Sandbox()
    violations = []
    help_runs = secondary_runs = denied = 0
    try:
        sandbox.copy_tree()
        for path, kind, via in tools:
            problems = []
            before = sandbox.snapshot()
            rc, output = sandbox.run_probe(path, kind, ["--help"], HELP_TIMEOUT)
            help_runs += 1
            problems += check_help(path, rc, output)
            flags = advertised_flags(output) if rc is not None else []
            for flag in flags:
                reason = PROBE_DENY.get((path, flag))
                if reason is not None:
                    denied += 1
                    if verbose:
                        print("skip  %-46s --%-8s (%s)" % (path, flag, reason))
                    continue
                b2 = sandbox.snapshot()
                rc2, out2 = sandbox.run_probe(
                    path, kind, ["--" + flag], SECONDARY_TIMEOUT)
                secondary_runs += 1
                problems += check_secondary(path, flag, rc2, out2)
                touched = Sandbox.changed(b2, sandbox.snapshot())
                if touched:
                    problems.append("--%s: wrote %s"
                                    % (flag, ", ".join(touched[:5])))
            touched = Sandbox.changed(before, sandbox.snapshot())
            if touched:
                problems.append("wrote repository files: %s" % ", ".join(touched[:5]))
            if problems:
                violations.append((path, problems))
                if verbose:
                    print("FAIL  %-46s %s" % (path, "; ".join(problems)))
            elif verbose:
                note = " [%s]" % via
                if path in HELP_EXCEPTIONS:
                    note += " [documented help-code deviation]"
                if flags:
                    note += " [%s]" % " ".join("--" + f for f in flags)
                print("ok    %-46s%s" % (path, note))
    finally:
        sandbox.close()

    print("\n%d CLI tools discovered (%d without a help path skipped); "
          "%d --help probes, %d advertised-flag probes, %d denied with a reason."
          % (len(tools), len(skipped), help_runs, secondary_runs, denied))
    if violations:
        print("%d tool(s) violate the CLI contract:" % len(violations))
        for path, items in violations:
            print("  %s" % path)
            for item in items:
                print("    - %s" % item)
        return 1
    print("all CLI contracts hold (exit codes, usage text, no traceback, no writes)")
    return 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def selftest():
    checks = []

    def check(name, condition):
        checks.append((name, bool(condition)))

    check("usage regex matches 'usage:'", USAGE_RE.search("usage: tool [-h]"))
    check("usage regex matches an option list", USAGE_RE.search("  --json  machine output"))
    check("usage regex rejects a bare error line",
          not USAGE_RE.search("boom"))

    shell_help = "#!/bin/bash\ncase $1 in\n  -h|--help) usage; exit 0 ;;\nesac\n"
    check("classifies a shell help branch",
          classify_tool_text("tools/x.sh", shell_help) ==
          {"kind": "shell", "via": "-h/--help branch"})
    shell_backend = "#!/bin/sh\nexec python3 \"$core\" \"$@\"\n"
    check("classifies a python backend wrapper",
          classify_tool_text("tools/y.sh", shell_backend) ==
          {"kind": "shell", "via": "python backend delegation"})
    launcher = ("#!/bin/bash\n# Usage: pass a URL\nurl=$1\nexec flatpak-spawn --host run "
                "\"$@\"\n")
    check("excludes a game launcher", classify_tool_text("tools/z.sh", launcher) is None)
    check("excludes a sourced library", not is_candidate_path("tools/lib/install-common.sh"))
    check("excludes the fingerprint fixture tree",
          not is_candidate_path("tools/fingerprint/tests/run-c-tests.sh"))

    py_help = "#!/usr/bin/env python3\nimport argparse\nif __name__ == '__main__':\n    pass\n"
    check("classifies an argparse python CLI",
          classify_tool_text("tools/a.py", py_help) ==
          {"kind": "python", "via": "argparse"})
    py_manual = ("#!/usr/bin/env python3\nimport sys\nif __name__ == '__main__':\n"
                 "    if '--help' in sys.argv:\n        print('usage: a')\n")
    check("classifies a manual --help python CLI",
          classify_tool_text("tools/b.py", py_manual)["via"] == "manual --help")
    check("rejects a python module without a main guard",
          classify_tool_text("tools/c.py", "import argparse\n") is None)

    help_out = "usage: x [-h] [--check] [--dry-run]\n  --dry-run   print only\n"
    check("finds advertised flags in option lines",
          advertised_flags(help_out) == ["dry-run"])
    check("ignores flags mentioned in prose",
          advertised_flags("runs the backend --selftest and writes\n") == [])
    check("does not match --check-only as --check",
          advertised_flags("  --check-only   explicit\n") == [])

    check("every help exception is an existing file",
          all((REPO / path).is_file() for path in HELP_EXCEPTIONS))
    check("every denied probe is an existing file",
          all((REPO / path).is_file() for path, _flag in PROBE_DENY))
    check("help exception reasons are non-empty",
          all(reason for _codes, _usage, reason in HELP_EXCEPTIONS.values()))

    # Sandbox write detection, without running any tool.
    with tempfile.TemporaryDirectory(prefix="oxp3-cli-contracts-selftest-") as tmp:
        tree = pathlib.Path(tmp)
        (tree / "a.txt").write_text("one", encoding="utf-8")
        class _Tmp:  # minimal stand-in for Sandbox.snapshot()
            pass
        fake = _Tmp()
        fake.tree = tree
        before = Sandbox.snapshot(fake)
        (tree / "a.txt").write_text("two", encoding="utf-8")
        (tree / "b.txt").write_text("new", encoding="utf-8")
        after = Sandbox.snapshot(fake)
        changed = Sandbox.changed(before, after)
        check("snapshot detects a modified file", "a.txt" in changed)
        check("snapshot detects a created file", "b.txt" in changed)

    tools, _skipped = enumerate_tools()
    check("discovers at least 20 CLI tools", len(tools) >= 20)
    check("discovers tools/repo-test.sh",
          any(path == "tools/repo-test.sh" for path, _k, _v in tools))
    check("every discovered tool is a file",
          all((REPO / path).is_file() for path, _kind, _via in tools))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print("%s %s" % ("ok  " if ok else "FAIL", name))
    print("selftest: %d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv):
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="check the CLI contract of every tool in the repository "
                    "(help, exit codes, no traceback, no writes)")
    parser.add_argument("--selftest", action="store_true",
                        help="test the classifier and helpers offline, run no tool")
    parser.add_argument("--list", action="store_true",
                        help="list the discovered tools and exit")
    parser.add_argument("--version", action="version",
                        version="%s %s" % (PROG, VERSION))
    args = parser.parse_args(argv[1:])
    try:
        if args.selftest:
            return selftest()
        if args.list:
            tools, skipped = enumerate_tools()
            for path, kind, via in tools:
                print("%-48s %-6s %s" % (path, kind, via))
            print("\n%d tools, %d files without a help path" % (len(tools), len(skipped)))
            return 0
        return run()
    except Problem as exc:
        sys.stderr.write("%s: %s\n" % (PROG, exc))
        return 2
    except OSError as exc:
        sys.stderr.write("%s: %s\n" % (PROG, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
