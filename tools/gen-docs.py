#!/usr/bin/env python3
"""gen-docs - generate the OneXPlayer 3 component tables from one data file.

docs/hw/components.yaml is the single source of truth for per-component facts
(state, summary, implementation paths, doc link, root requirement, install and
verification steps, dependencies, caveats, last validation). This tool validates
that file and renders the three tables that used to be hand-maintained in
docs/hw/README.md (state matrix), docs/INSTALL-HW.md (install/verify matrix) and
README.md (summary), between explicit markers:

    <!-- BEGIN GENERATED: hw-state ... -->
    ...generated table...
    <!-- END GENERATED: hw-state -->

Prose outside the markers is never read, rewritten or reflowed. Output is
deterministic: no timestamps, no environment values, so `--check` can run in CI.

    tools/gen-docs.py              same as --check (the safe default)
    tools/gen-docs.py --check      exit 1 when a generated block is stale
    tools/gen-docs.py --write      rewrite the generated blocks in place
    tools/gen-docs.py --help

Exit codes: 0 = in sync / written; 1 = stale block (--check); 2 = usage,
I/O or validation error. Stdlib only; the YAML subset parser is in this file
(see docs/hw/REGISTRY.md for the supported subset).
"""

import datetime
import glob
import json
import os
import re
import sys

PROG = "gen-docs"
VERSION = "1.0.0"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "docs/hw/components.yaml"
VERIFY_TOOL = "tools/oxp3-verify.sh"

STATES = ("working", "partial", "blocked", "not-a-gap", "awaiting-install")
ROOT_VALUES = ("yes", "no", "partial", "not-applicable")
GROUPS = ("driver-stack", "hardware", "diagnostics")
TOP_KEYS = ("schema", "global_checks", "components", "unmapped_checks")
COMPONENT_KEYS = (
    "id", "title", "state", "group", "headline", "summary",
    "implementation", "doc", "root_required", "install", "verify",
    "dependencies", "caveats", "last_validated",
)


class Problem(Exception):
    """A validation or I/O problem reported to the user (exit 2)."""


# ---------------------------------------------------------------------------
# YAML subset parser
#
# Supported, and only this (documented in docs/hw/REGISTRY.md):
#   * block mappings and block sequences, nested by two-space indentation
#   * `key: value`, `key:` followed by an indented block, `- value`,
#     `- key: value` (mapping sequence item)
#   * plain scalars, `true`/`false`, integers, double-quoted (JSON escapes) and
#     single-quoted strings, and single-line flow sequences of scalars `[a, b]`
#   * full-line comments and ` #` trailing comments outside quotes
# Not supported (rejected with a line number): anchors, tags, block scalars
# (| and >), multi-line flow collections, duplicate mapping keys.
# ---------------------------------------------------------------------------
class YamlError(Problem):
    def __init__(self, line, message):
        Problem.__init__(self, "line %d: %s" % (line, message))


def _strip_comment(text):
    out = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _split_flow_list(text, line):
    inner = text.strip()[1:-1].strip()
    if not inner:
        return []
    items = []
    cur = []
    quote = None
    for i, ch in enumerate(inner):
        if quote:
            cur.append(ch)
            if ch == quote and (i == 0 or inner[i - 1] != "\\"):
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append(_scalar("".join(cur).strip(), line))
            cur = []
        else:
            cur.append(ch)
    if quote:
        raise YamlError(line, "unterminated quote in flow sequence")
    items.append(_scalar("".join(cur).strip(), line))
    return items


def _scalar(text, line):
    if text == "" or text in ("~", "null"):
        return None
    if text in ("{}", "[]"):
        return {} if text == "{}" else []
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError(line, "only single-line flow sequences are supported")
        return _split_flow_list(text, line)
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise YamlError(line, "bad double-quoted string: %s" % exc)
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise YamlError(line, "unterminated single-quoted string")
        return text[1:-1].replace("''", "'")
    if text in ("true", "false"):
        return text == "true"
    if re.match(r"^-?[0-9]+$", text):
        return int(text)
    return text


def _split_key(text, line):
    """Split 'key: rest' on the first colon outside quotes."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].strip(), text[i + 1:].strip()
    raise YamlError(line, "expected 'key: value'")


def parse_yaml(text):
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw.split("#", 1)[0]:
            raise YamlError(n, "tabs are not allowed for indentation")
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.lstrip(" "), n))
    if not lines:
        raise Problem("%s is empty" % DATA)
    value, idx = _parse_block(lines, 0, lines[0][0])
    if idx != len(lines):
        raise YamlError(lines[idx][2], "unexpected indentation")
    return value


def _parse_block(lines, idx, indent):
    if lines[idx][0] != indent:
        raise YamlError(lines[idx][2], "bad indentation")
    if lines[idx][1] == "-" or lines[idx][1].startswith("- "):
        return _parse_seq(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_seq(lines, idx, indent):
    out = []
    while idx < len(lines) and lines[idx][0] == indent and (
            lines[idx][1] == "-" or lines[idx][1].startswith("- ")):
        _, content, lineno = lines[idx]
        rest = content[1:].strip()
        if not rest:
            if idx + 1 >= len(lines) or lines[idx + 1][0] <= indent:
                raise YamlError(lineno, "sequence item has no value")
            value, idx = _parse_block(lines, idx + 1, lines[idx + 1][0])
            out.append(value)
            continue
        key, _sep, tail = _maybe_key(rest, lineno)
        if key is None:
            out.append(_scalar(rest, lineno))
            idx += 1
            continue
        # "- key: value" opens a mapping whose remaining keys sit at indent + 2
        item = {}
        if tail != "":
            item[key] = _scalar(tail, lineno)
            idx += 1
        elif idx + 1 < len(lines) and lines[idx + 1][0] > indent + 2:
            item[key], idx = _parse_block(lines, idx + 1, lines[idx + 1][0])
        else:
            item[key] = None
            idx += 1
        if idx < len(lines) and lines[idx][0] == indent + 2 \
                and not lines[idx][1].startswith("-"):
            more, idx = _parse_map(lines, idx, indent + 2)
            for k, v in more.items():
                if k in item:
                    raise YamlError(lineno, "duplicate key %r in sequence item" % k)
                item[k] = v
        out.append(item)
    return out, idx


def _maybe_key(text, line):
    try:
        key, rest = _split_key(text, line)
    except YamlError:
        return None, None, None
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", key):
        return None, None, None
    return key, ":", rest


def _parse_map(lines, idx, indent):
    out = {}
    while idx < len(lines) and lines[idx][0] == indent:
        content, lineno = lines[idx][1], lines[idx][2]
        if content.startswith("-"):
            raise YamlError(lineno, "unexpected sequence item in a mapping")
        key, rest = _split_key(content, lineno)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", key):
            raise YamlError(lineno, "invalid key %r" % key)
        if key in out:
            raise YamlError(lineno, "duplicate key %r" % key)
        if rest == "":
            if idx + 1 < len(lines) and lines[idx + 1][0] > indent:
                value, idx = _parse_block(lines, idx + 1, lines[idx + 1][0])
            else:
                value = None
                idx += 1
        else:
            value = _scalar(rest, lineno)
            idx += 1
        out[key] = value
    return out, idx


# ---------------------------------------------------------------------------
# verification-check inventory (same extraction rules as oxp3-verify.sh)
# ---------------------------------------------------------------------------
def declared_checks():
    path = os.path.join(REPO, VERIFY_TOOL)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise Problem("cannot read %s: %s" % (VERIFY_TOOL, exc))
    ids = set()
    for line in text.splitlines():
        m = re.match(r"^[ \t]*(?:record|simple_ind)[ \t]+([a-z][a-z0-9._]*)[ \t]", line)
        if m:
            ids.add(m.group(1))
    if not ids:
        raise Problem("no check ids parsed from %s - did its record() format change?" % VERIFY_TOOL)
    return ids


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _need_mapping(value, where, errors, required=(), optional=()):
    if not isinstance(value, dict):
        errors.append("%s: expected a mapping" % where)
        return False
    unknown = [k for k in value if k not in required and k not in optional]
    for key in unknown:
        errors.append("%s: unknown key %r" % (where, key))
    for key in required:
        if key not in value:
            errors.append("%s: missing required key %r" % (where, key))
    return True


def _need_str(value, where, errors, nonempty=True):
    if not isinstance(value, str):
        errors.append("%s: expected a string" % where)
        return False
    if nonempty and not value.strip():
        errors.append("%s: must not be empty" % where)
        return False
    if "\n" in value:
        errors.append("%s: must be a single line" % where)
        return False
    return True


def _need_str_list(value, where, errors, allow_empty=True):
    if not isinstance(value, list):
        errors.append("%s: expected a list" % where)
        return False
    if not value and not allow_empty:
        errors.append("%s: must not be empty" % where)
        return False
    ok = True
    for i, item in enumerate(value):
        ok = _need_str(item, "%s[%d]" % (where, i), errors) and ok
    return ok


def _check_path(pattern, where, errors):
    if not isinstance(pattern, str) or not pattern:
        errors.append("%s: expected a non-empty path" % where)
        return
    if pattern.startswith("/") or pattern.startswith("~") or ".." in pattern.split("/"):
        errors.append("%s: %r must be a repository-relative path" % (where, pattern))
        return
    if "*" in pattern or "?" in pattern:
        matches = glob.glob(os.path.join(REPO, pattern))
    else:
        matches = [os.path.join(REPO, pattern)] if os.path.exists(os.path.join(REPO, pattern)) else []
    if not matches:
        errors.append("%s: %r does not exist in the repository" % (where, pattern))


def validate(raw, checks):
    errors = []
    if not isinstance(raw, dict):
        raise Problem("%s: top level must be a mapping" % DATA)
    for key in raw:
        if key not in TOP_KEYS:
            errors.append("%s: unknown top-level key %r" % (DATA, key))
    for key in ("schema", "components"):
        if key not in raw:
            errors.append("%s: missing top-level key %r" % (DATA, key))
    if raw.get("schema") != 1:
        errors.append("%s: schema must be 1" % DATA)

    unmapped = raw.get("unmapped_checks", {}) or {}
    if not isinstance(unmapped, dict):
        errors.append("%s: unmapped_checks must be a mapping" % DATA)
        unmapped = {}
    for cid, reason in unmapped.items():
        if cid not in checks:
            errors.append("%s: unmapped_checks names %r, which is not declared in %s"
                          % (DATA, cid, VERIFY_TOOL))
        if not isinstance(reason, str) or not reason.strip():
            errors.append("%s: unmapped_checks[%s] needs a reason" % (DATA, cid))

    global_checks = raw.get("global_checks", []) or []
    if not isinstance(global_checks, list):
        errors.append("%s: global_checks must be a list" % DATA)
        global_checks = []
    for i, item in enumerate(global_checks):
        where = "%s: global_checks[%d]" % (DATA, i)
        if _need_mapping(item, where, errors, required=("label", "command", "expect")):
            for key in ("label", "command", "expect"):
                _need_str(item.get(key), "%s.%s" % (where, key), errors)

    components = raw.get("components")
    if not isinstance(components, list) or not components:
        raise Problem("%s: 'components' must be a non-empty list" % DATA)

    seen_ids = {}
    used_checks = {}
    docs = []
    for index, comp in enumerate(components):
        where = "%s: components[%d]" % (DATA, index)
        if not _need_mapping(comp, where, errors, required=COMPONENT_KEYS, optional=("area",)):
            continue
        cid = comp.get("id")
        if _need_str(cid, where + ".id", errors):
            if not re.match(r"^[a-z][a-z0-9-]*$", cid):
                errors.append("%s.id: %r must match ^[a-z][a-z0-9-]*$" % (where, cid))
            if cid in seen_ids:
                errors.append("%s.id: duplicate id %r (first at components[%d])"
                              % (where, cid, seen_ids[cid]))
            else:
                seen_ids[cid] = index
                where = "%s (%s)" % (where, cid)

        _need_str(comp.get("title"), where + ".title", errors)
        if _need_str(comp.get("summary"), where + ".summary", errors):
            if len(comp["summary"]) > 400:
                errors.append("%s.summary: %d characters; keep it to one sentence (<= 400)"
                              % (where, len(comp["summary"])))

        state = comp.get("state")
        if state not in STATES:
            errors.append("%s.state: %r is not one of %s" % (where, state, ", ".join(STATES)))
        group = comp.get("group")
        if group not in GROUPS:
            errors.append("%s.group: %r is not one of %s" % (where, group, ", ".join(GROUPS)))
        headline = comp.get("headline")
        if not isinstance(headline, bool):
            errors.append("%s.headline: must be true or false" % where)

        impl = comp.get("implementation")
        if _need_str_list(impl, where + ".implementation", errors, allow_empty=False):
            for i, path in enumerate(impl):
                _check_path(path, "%s.implementation[%d]" % (where, i), errors)

        doc = comp.get("doc")
        if _need_str(doc, where + ".doc", errors):
            docs.append((where, doc))
            if not os.path.exists(os.path.join(REPO, doc)):
                errors.append("%s.doc: %r does not exist" % (where, doc))
            elif not doc.endswith(".md"):
                errors.append("%s.doc: %r is not a Markdown file" % (where, doc))

        root = comp.get("root_required")
        if root not in ROOT_VALUES:
            errors.append("%s.root_required: %r is not one of %s"
                          % (where, root, ", ".join(ROOT_VALUES)))
        if root == "not-applicable" and group != "hardware":
            errors.append("%s.root_required: not-applicable is only for hardware components" % where)

        install = comp.get("install")
        if _need_mapping(install, where + ".install", errors,
                         required=("summary",), optional=("entrypoint", "steps")):
            _need_str(install.get("summary"), where + ".install.summary", errors)
            if "entrypoint" in install:
                _need_str(install.get("entrypoint"), where + ".install.entrypoint", errors)
            if "steps" in install:
                _need_str_list(install.get("steps"), where + ".install.steps", errors)

        verify = comp.get("verify")
        if not isinstance(verify, list) or not verify:
            errors.append("%s.verify: expected a non-empty list" % where)
        else:
            for i, item in enumerate(verify):
                vw = "%s.verify[%d]" % (where, i)
                if not isinstance(item, dict):
                    errors.append("%s: expected a mapping with id or command" % vw)
                    continue
                extra = [k for k in item if k not in ("id", "command", "expect", "root")]
                for key in extra:
                    errors.append("%s: unknown key %r" % (vw, key))
                has_id = "id" in item
                has_cmd = "command" in item
                if has_id == has_cmd:
                    errors.append("%s: exactly one of id or command is required" % vw)
                if has_id:
                    if _need_str(item.get("id"), vw + ".id", errors):
                        if item["id"] not in checks:
                            errors.append("%s.id: %r is not declared in %s"
                                          % (vw, item["id"], VERIFY_TOOL))
                        elif item["id"] in used_checks:
                            errors.append("%s.id: %r is already used by %s; each check maps to one "
                                          "component" % (vw, item["id"], used_checks[item["id"]]))
                        else:
                            used_checks[item["id"]] = where
                if has_cmd:
                    _need_str(item.get("command"), vw + ".command", errors)
                if "expect" in item:
                    _need_str(item.get("expect"), vw + ".expect", errors)
                elif has_cmd:
                    errors.append("%s.expect: a raw command needs the expected result" % vw)
                if "root" in item and not isinstance(item["root"], bool):
                    errors.append("%s.root: must be true or false" % vw)

        deps = comp.get("dependencies")
        _need_str_list(deps, where + ".dependencies", errors)
        lv = comp.get("last_validated")
        if _need_mapping(lv, where + ".last_validated", errors, required=("date", "evidence")):
            date = lv.get("date")
            if _need_str(date, where + ".last_validated.date", errors):
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    errors.append("%s.last_validated.date: %r is not YYYY-MM-DD" % (where, date))
            if _need_str(lv.get("evidence"), where + ".last_validated.evidence", errors):
                _need_str_list([lv["evidence"]], where + ".last_validated.evidence", errors)

        _need_str_list(comp.get("caveats"), where + ".caveats", errors)

        # A non-hardware component is only rendered by the README summary table,
        # so without headline it would live in the registry but in no generated
        # block - almost always a mistake.
        if group != "hardware" and not headline:
            errors.append("%s: not in any generated block - hardware components appear in the "
                          "hw matrix; set headline: true for the README table" % where)

    # dependencies must resolve to known components, never to themselves
    ids = set(seen_ids)
    for index, comp in enumerate(components):
        if not isinstance(comp, dict):
            continue
        cid = comp.get("id")
        for dep in comp.get("dependencies") or []:
            if dep == cid:
                errors.append("%s: dependencies: component depends on itself" % cid)
            elif dep not in ids:
                errors.append("%s: dependencies: unknown component %r" % (cid, dep))

    # the registry must cover every declared verification check
    for cid in sorted(checks):
        if cid not in used_checks and cid not in unmapped:
            errors.append("%s: check %r is declared in %s but no component maps it; add it to a "
                          "verify list or to unmapped_checks" % (DATA, cid, VERIFY_TOOL))

    # every doc link must resolve to a tracked Markdown file
    for where, doc in docs:
        if doc.endswith(".md") and not doc.startswith("docs/"):
            errors.append("%s.doc: %r should be a docs/ path" % (where, doc))

    if errors:
        raise Problem("\n".join(errors))
    return components, unmapped, global_checks


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _code_path(path):
    full = os.path.join(REPO, path)
    shown = path + "/" if os.path.isdir(full) and not path.endswith("/") else path
    return "`%s`" % shown


def _link(doc, from_dir):
    rel = os.path.relpath(os.path.join(REPO, doc), os.path.join(REPO, from_dir))
    return "[%s](%s)" % (os.path.basename(doc), rel.replace(os.sep, "/"))


def _state(comp):
    return comp["state"]


def _root(comp):
    return "n/a" if comp["root_required"] == "not-applicable" else comp["root_required"]


def render_hw_state(components, _unmapped, _global):
    rows = [c for c in components if c["group"] == "hardware"]
    out = [
        "| Topic | State | Summary | Our implementation | Doc | Needs root? |",
        "|---|---|---|---|---|---|",
    ]
    for c in rows:
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            _cell(c["title"]), _cell(_state(c)), _cell(c["summary"]),
            ", ".join(_code_path(p) for p in c["implementation"]),
            _link(c["doc"], "docs/hw"), _cell(_root(c)),
        ))
    caveats = [(c["title"], c["caveats"]) for c in rows if c.get("caveats")]
    if caveats:
        out += ["", "Recorded caveats:", ""]
        for title, items in caveats:
            out.append("- **%s**: %s" % (_cell(title), _cell("; ".join(items))))
    return "\n".join(out) + "\n"


def render_install_verify(components, _unmapped, global_checks):
    rows = [c for c in components if c["group"] == "hardware"]
    out = []
    if global_checks:
        out += [
            "Start with the whole-system checks; then the per-component rows.",
            "",
            "| Whole-system check | Command | Expected |",
            "|---|---|---|",
        ]
        for item in global_checks:
            out.append("| %s | `%s` | %s |" % (
                _cell(item["label"]), item["command"].replace("`", "'"), _cell(item["expect"])))
        out.append("")
    out += [
        "| Component | Install |",
        "|---|---|",
    ]
    for c in rows:
        entry = (c["install"] or {}).get("entrypoint")
        out.append("| %s | %s |" % (
            _cell(c["title"]),
            "`%s`" % entry.replace("`", "'") if entry else _cell(c["install"]["summary"]),
        ))
    out += [
        "",
        "Manual install steps, opt-out flags, uninstall and per-component rollback are in "
        "sections 2, 3, 6 and 7 below; the machine-readable form is "
        "[`docs/hw/REGISTRY.md`](hw/REGISTRY.md).",
        "",
        "| Component | State | Verify | Expected | Root |",
        "|---|---|---|---|---|",
    ]
    for c in rows:
        for item in c["verify"]:
            if "id" in item:
                verify = "`%s`" % item["id"]
            else:
                verify = "cmd: `%s`" % item["command"].replace("`", "'")
            expect = item.get("expect") or "PASS in `%s --json`" % VERIFY_TOOL
            root = bool(item.get("root", False))
            out.append("| %s | %s | %s | %s | %s |" % (
                _cell(c["title"]), _cell(_state(c)), _cell(verify),
                _cell(expect), "yes" if root else "no",
            ))
    out += [
        "",
        "Verify rows with a backticked id are checks in `%s` (`--json` for machines, "
        "`--full` for the invasive ones); `cmd:` rows are direct read-only commands. Every "
        "component below is installed through `tools/oxp3-hw-install.sh` unless its row in "
        "[`docs/hw/REGISTRY.md`](hw/REGISTRY.md) says otherwise." % VERIFY_TOOL,
    ]
    return "\n".join(out) + "\n"


def render_readme_summary(components, _unmapped, _global):
    rows = [c for c in components if c["headline"]]
    out = [
        "| Part | State | Fixes | How it is installed |",
        "|---|---|---|---|",
    ]
    for c in rows:
        entry = c["install"].get("entrypoint")
        how = c["install"]["summary"]
        if entry:
            how = "%s (`%s`)" % (how, entry)
        out.append("| %s | %s | %s | %s |" % (
            _cell(c["title"]), _cell(_state(c)), _cell(c["summary"]), _cell(how),
        ))
    return "\n".join(out) + "\n"


BLOCKS = (
    # name, target file, renderer, one-line description
    ("hw-state", "docs/hw/README.md", render_hw_state,
     "state matrix of the hardware components"),
    ("install-verify", "docs/INSTALL-HW.md", render_install_verify,
     "install/verify matrix with the oxp3-verify.sh check ids"),
    ("readme-summary", "README.md", render_readme_summary,
     "short summary table of the headline components"),
)

BEGIN_RE = re.compile(r"^<!-- BEGIN GENERATED: (?P<name>[a-z-]+)(?:[ ].*)? -->$")
END_RE = re.compile(r"^<!-- END GENERATED: (?P<name>[a-z-]+) -->$")


def block_bounds(text, name):
    """Return (begin_end, end_start) offsets of the generated inner content."""
    begin_end = None
    for m in re.finditer(r"^.*$", text, re.M):
        line = m.group(0)
        bm = BEGIN_RE.match(line)
        if bm and bm.group("name") == name and begin_end is None:
            begin_end = m.end() + 1 if m.end() < len(text) else m.end()
            continue
        em = END_RE.match(line)
        if em and em.group("name") == name and begin_end is not None:
            return begin_end, m.start()
    return None


def stale_blocks(components, unmapped, global_checks):
    """Return per-block status: (name, target, state, current_inner, wanted)."""
    results = []
    for name, target, render, _desc in BLOCKS:
        path = os.path.join(REPO, target)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise Problem("cannot read %s: %s" % (target, exc))
        _check_markers(text, target, name)
        wanted = render(components, unmapped, global_checks)
        bounds = block_bounds(text, name)
        if bounds is None:
            results.append((name, target, "missing", None, wanted))
            continue
        current = text[bounds[0]:bounds[1]]
        results.append((name, target, "ok" if current == wanted else "stale", current, wanted))
    return results


def _check_markers(text, target, name):
    begins = len(re.findall(r"^<!-- BEGIN GENERATED: %s(?:[ ].*)? -->$" % re.escape(name),
                            text, re.M))
    ends = len(re.findall(r"^<!-- END GENERATED: %s -->$" % re.escape(name), text, re.M))
    if begins > 1 or ends > 1:
        raise Problem("%s: block %r has %d BEGIN and %d END markers; exactly one pair is allowed"
                      % (target, name, begins, ends))
    if begins != ends:
        raise Problem("%s: block %r has %d BEGIN but %d END markers"
                      % (target, name, begins, ends))


def first_difference(current, wanted):
    old = current.splitlines()
    new = wanted.splitlines()
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else "<missing line>"
        b = new[i] if i < len(new) else "<missing line>"
        if a != b:
            return i + 1, a, b
    return 0, "", ""


def write_blocks(components, unmapped, global_checks):
    changed = []
    for name, target, render, _desc in BLOCKS:
        path = os.path.join(REPO, target)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise Problem("cannot read %s: %s" % (target, exc))
        wanted = render(components, unmapped, global_checks)
        bounds = block_bounds(text, name)
        if bounds is None:
            raise Problem("%s: markers for block %r not found; add\n"
                          "  <!-- BEGIN GENERATED: %s -->\n  <!-- END GENERATED: %s -->\n"
                          "around the generated table (see docs/hw/REGISTRY.md)"
                          % (target, name, name, name))
        current = text[bounds[0]:bounds[1]]
        if current == wanted:
            continue
        new_text = text[:bounds[0]] + wanted + text[bounds[1]:]
        tmp = path + ".gen-docs.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            os.replace(tmp, path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise Problem("cannot write %s: %s" % (target, exc))
        changed.append((name, target))
    return changed


def usage():
    print("""%s %s - generate the OXP3 component tables from %s

  tools/gen-docs.py            validate, then report stale blocks (--check default)
  tools/gen-docs.py --check    exit 1 when a generated block is out of date
  tools/gen-docs.py --write    rewrite the generated blocks in place
  tools/gen-docs.py --help     this text

Generated blocks (markers, never hand-edited):
  hw-state        docs/hw/README.md      state matrix
  install-verify  docs/INSTALL-HW.md     install/verify matrix
  readme-summary  README.md              summary table

Exit: 0 in sync/written, 1 stale block, 2 usage or validation error.
Data file and schema: %s (documented in docs/hw/REGISTRY.md).""" % (
        PROG, VERSION, DATA, DATA))


def main(argv):
    mode = "check"
    for arg in argv[1:]:
        if arg in ("-h", "--help"):
            usage()
            return 0
        elif arg == "--check":
            mode = "check"
        elif arg == "--write":
            mode = "write"
        else:
            sys.stderr.write("%s: unknown argument %r (try --help)\n" % (PROG, arg))
            return 2
    try:
        with open(os.path.join(REPO, DATA), encoding="utf-8") as fh:
            raw = parse_yaml(fh.read())
        checks = declared_checks()
        components, unmapped, global_checks = validate(raw, checks)
        if mode == "write":
            changed = write_blocks(components, unmapped, global_checks)
            for name, target in changed:
                print("updated %-14s %s" % (name, target))
            if not changed:
                print("all %d generated blocks are up to date" % len(BLOCKS))
            print("%s: %d components, %d mapped checks in %s"
                  % (DATA, len(components), len(checks), VERIFY_TOOL))
            return 0
        results = stale_blocks(components, unmapped, global_checks)
        bad = 0
        for name, target, state, current, wanted in results:
            if state == "ok":
                print("ok       %-14s %s" % (name, target))
                continue
            bad += 1
            if state == "missing":
                print("MISSING  %-14s %s (markers not found)" % (name, target))
                continue
            print("STALE    %-14s %s" % (name, target))
            line, old, new = first_difference(current, wanted)
            print("         first difference at block line %d:" % line)
            print("         - %s" % old)
            print("         + %s" % new)
        if bad:
            print("\n%d of %d generated block(s) are stale; run tools/gen-docs.py --write"
                  % (bad, len(results)))
            return 1
        print("all %d generated blocks are up to date (%d components, %d checks)"
              % (len(results), len(components), len(checks)))
        return 0
    except Problem as exc:
        sys.stderr.write("%s: %s\n" % (PROG, exc))
        return 2
    except OSError as exc:
        sys.stderr.write("%s: %s\n" % (PROG, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
