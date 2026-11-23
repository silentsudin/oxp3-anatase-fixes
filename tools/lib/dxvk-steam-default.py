#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Print Steam's default compatibility tool from config.vdf ("0" in CompatToolMapping).

Split out of install-common.sh as a file of its own: the VDF block is nested, so matching it
with a shell regex picks the wrong entry, and a script file keeps the quoting sane.
"""
from __future__ import annotations

import re
import sys


def default_tool(text: str) -> str:
    i = text.find('"CompatToolMapping"')
    if i < 0:
        return ""
    try:
        j = text.index("{", i)
    except ValueError:
        return ""
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                block = text[j:k]
                break
    else:
        return ""
    m = re.search(r'"0"\s*\{[^{}]*"name"\s*"([^"]*)"', block)
    return m.group(1) if m else ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: dxvk-steam-default.py <config.vdf>", file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], errors="replace") as f:
            print(default_tool(f.read()))
    except OSError as e:
        print(f"cannot read {sys.argv[1]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
