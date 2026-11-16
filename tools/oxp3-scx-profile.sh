#!/bin/bash
# SPDX-License-Identifier: MIT
# oxp3-scx-profile.sh - switch the gaming sched_ext profile on the OneXPlayer 3.
#
#   sudo tools/oxp3-scx-profile.sh gaming     # scx_cosmos -c 0 -p 0            (current default)
#   sudo tools/oxp3-scx-profile.sh lowlat     # + -m performance --primary-overload-us 1000
#   sudo tools/oxp3-scx-profile.sh lowlat-fast# + -P (prefer higher-ranked idle cores)
#   sudo tools/oxp3-scx-profile.sh eevdf      # stop/disable sched_ext, use kernel EEVDF
#   sudo tools/oxp3-scx-profile.sh status
#
# The lowlat profiles are the official scx_loader "lowlatency" idea: contain
# tasks to the fastest CPUs (P-cores) but let them spill when the primary domain
# is overloaded.  Use them for titles whose main thread must stay on a P-core
# (CPU-bound recomp/Unity ports, Elden Ring); keep gaming for GPU-bound titles.
#
# Writes a drop-in for oxp3-gaming-sched.service and restarts it.  Revert with
# `gaming`; the kernel falls back to EEVDF if scx_cosmos fails to load.
set -euo pipefail
dropin=/etc/systemd/system/oxp3-gaming-sched.service.d/50-profile.conf
unit=oxp3-gaming-sched.service
say() { printf '== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo $0 ...)"

mode=${1:-status}
case $mode in
gaming)
    exec_line=/usr/bin/scx_cosmos\ -c\ 0\ -p\ 0
    ;;
lowlat)
    exec_line=/usr/bin/scx_cosmos\ -c\ 0\ -p\ 0\ -m\ performance\ --primary-overload-us\ 1000
    ;;
lowlat-fast)
    exec_line=/usr/bin/scx_cosmos\ -c\ 0\ -p\ 0\ -m\ performance\ --primary-overload-us\ 1000\ -P
    ;;
eevdf)
    say "disabling $unit (kernel EEVDF)"
    systemctl disable --now "$unit" || true
    exit 0
    ;;
status)
    say "unit"; systemctl show "$unit" -p ActiveState -p ExecStart --no-pager 2>/dev/null || true
    say "loaded scheduler"; cat /sys/kernel/sched_ext/root/ops 2>/dev/null || echo none
    say "drop-in"; cat "$dropin" 2>/dev/null || echo "(none)"
    exit 0
    ;;
*) die "unknown mode '$mode' (gaming|lowlat|lowlat-fast|eevdf|status)" ;;
esac

mkdir -p "$(dirname "$dropin")"
cat > "$dropin" <<CONF
[Service]
ExecStart=
ExecStart=$exec_line
CONF
say "writing $dropin"
cat "$dropin"
systemctl daemon-reload
systemctl restart "$unit"
sleep 1
say "loaded scheduler"
cat /sys/kernel/sched_ext/root/ops 2>/dev/null || echo none
