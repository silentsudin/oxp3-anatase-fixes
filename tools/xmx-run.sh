#!/bin/bash
# SPDX-License-Identifier: MIT
# xmx-run.sh -- <command...> : run a game with the XMX environment sourced.
# Example: tools/xmx-run.sh -- tools/cp2077-direct.sh -benchmark
set -eu
envf=$HOME/.config/xess-xmx/oxp3-env.sh
[ -e "$envf" ] || { echo "error: run tools/xmx-enable.sh first" >&2; exit 1; }
# shellcheck disable=SC1090
. "$envf"
[ "${1:-}" = "--" ] && shift
exec "$@"
