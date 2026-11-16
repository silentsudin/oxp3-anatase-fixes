#!/bin/sh
# SPDX-License-Identifier: MIT
# game-pcores.sh - run one game (and its children) on the P-cores.
#
# This is the Linux equivalent of the Windows "disable E-cores for this game"
# workaround: the whole process tree is confined to the P-cores (cpu0-1 on the
# OneXPlayer 3), so a latency-critical main thread cannot be placed on an E or
# LP-E core.  Everything the process starts inherits the affinity.
#
#   tools/game-pcores.sh -- %command%
#   tools/game-pcores.sh --cpus 0-1 -- /path/to/game
#
# In a Steam shortcut put this in Launch Options:
#   /path/to/oxp3-anatase-fixes/tools/game-pcores.sh -- %command%
#
# Caveat: only two P-cores exist here (no SMT).  A game whose worker threads do
# not fit in them can lose throughput; keep this for the titles where the main
# thread placement is the problem (CPU-bound recomp/Unity titles, Elden Ring).
set -eu
cpus=${OXP3_PCORES:-0-1}
dry=0
while [ $# -gt 0 ]; do
    case $1 in
        --cpus) cpus=$2; shift 2 ;;
        --cpus=*) cpus=${1#*=}; shift ;;
        --dry-run) dry=1; shift ;;
        --) shift; break ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) break ;;
    esac
done
[ $# -gt 0 ] || { echo "usage: $0 [--cpus LIST] [--dry-run] -- COMMAND..." >&2; exit 2; }
printf 'game-pcores: P-cores %s -> %s\n' "$cpus" "$*" >&2
if [ "$dry" = 1 ]; then
    exit 0
fi
exec taskset -c "$cpus" "$@"
