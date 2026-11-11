#!/bin/sh
# SPDX-License-Identifier: MIT
# oxp3-gamewrap.sh - one entry point that composes every graphics feature we
# support for a game launch: XeSS XMX env, gamescope HDR flags, vibrance
# (gamescope ReShade or vkBasalt) and MangoHud, with everything else passed
# through untouched.
#
# The decision logic (and its offline unit tests) is tools/lib/oxp3_gamewrap.py;
# this script only locates the checkout and the interpreter. Usage:
#
#   tools/oxp3-gamewrap.sh --status
#   tools/oxp3-gamewrap.sh --dry-run --app cyberpunk2077 -- <proton> run <game>
#   tools/oxp3-gamewrap.sh -- <command...>
#
# Config: $XDG_CONFIG_HOME/oxp3-gamewrap.toml, example in
# tools/oxp3-gamewrap.toml.example, guide in docs/LAUNCH.md. No root, no
# network, no shell evaluation of config values.
set -eu

self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
bindir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 127
repo=$(CDPATH= cd -- "$bindir/.." && pwd) || exit 127
core=$repo/tools/lib/oxp3_gamewrap.py

if [ ! -f "$core" ]; then
    printf '%s: missing %s (broken checkout)\n' "${0##*/}" "$core" >&2
    exit 127
fi

py=${OXP3_PYTHON:-python3}
if ! command -v "$py" >/dev/null 2>&1; then
    printf '%s: %s not found; set OXP3_PYTHON (the Flatpak Steam sandbox needs python3)\n' \
        "${0##*/}" "$py" >&2
    exit 127
fi

exec "$py" "$core" "$@"
