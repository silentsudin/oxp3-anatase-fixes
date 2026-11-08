#!/bin/bash
# oxp3-voltap install / uninstall / status.
#
# This script never calls sudo: run it as root yourself (the exact command is
# printed otherwise).  It touches only:
#   /usr/local/libexec/oxp3-voltap          (the daemon, /usr/local -> /var/usrlocal,
#                                            so it survives rpm-ostree updates)
#   /etc/oxp3-voltap.conf                   (only created if missing)
#   /etc/systemd/system/oxp3-voltap.service
#   /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb   (only with --hwdb)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
bin=/usr/local/libexec/oxp3-voltap
conf=/etc/oxp3-voltap.conf
unit=/etc/systemd/system/oxp3-voltap.service
hwdb=/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "this needs root; re-run:" >&2
        echo "  sudo $0 $*" >&2
        exit 1
    fi
}

usage() {
    cat <<EOF
usage: $0 install [--hwdb]   install + enable the daemon (root)
       $0 uninstall          remove daemon and unit (root)
       $0 status             show service + device state (no root needed)
       $0 --hwdb             also install the kernel force-release hwdb file
EOF
}

cmd_install() {
    local with_hwdb=$1
    need_root install
    install -d -m 0755 /usr/local/libexec
    install -m 0755 "$here/oxp3-voltap" "$bin"
    if [[ -e $conf ]]; then
        echo "keeping existing $conf"
    else
        install -m 0644 "$here/oxp3-voltap.conf" "$conf"
        echo "installed $conf"
    fi
    install -m 0644 "$here/oxp3-voltap.service" "$unit"
    systemctl daemon-reload
    systemctl enable --now oxp3-voltap.service
    if [[ $with_hwdb == 1 ]]; then
        install -m 0644 "$here/84-oxp3-volume-keys-taps.hwdb" "$hwdb"
        systemd-hwdb update
        udevadm trigger --subsystem-match=input --action=change
        echo "installed $hwdb (force-release of the volume scancodes)"
    fi
    systemctl --no-pager --lines=5 status oxp3-voltap.service || true
    cat <<EOF

verify:
  systemctl status oxp3-voltap            # active (running), no grab errors
  journalctl -u oxp3-voltap -b -f         # 'grabbed /dev/input/eventN (Handheld Daemon Volume Keyboard)'
  press volume up 3x: one step each, no ramp; hold it: it steps once per press
  stop it again:  sudo systemctl stop oxp3-voltap && press -> the bug is back
EOF
}

cmd_uninstall() {
    need_root uninstall
    systemctl disable --now oxp3-voltap.service 2>/dev/null || true
    rm -f "$unit" "$bin" "$hwdb"
    systemctl daemon-reload
    [[ -e $hwdb ]] || systemd-hwdb update || true
    udevadm trigger --subsystem-match=input --action=change 2>/dev/null || true
    echo "removed $unit $bin $hwdb (kept $conf)"
}

cmd_status() {
    systemctl --no-pager --lines=0 is-active oxp3-voltap.service 2>/dev/null || true
    systemctl --no-pager --lines=5 status oxp3-voltap.service 2>/dev/null || true
    echo "--- devices (readable without root only when the seat ACL grants it) ---"
    python3 "$here/oxp3-voltap" check || true
    echo "--- journal ---"
    journalctl -u oxp3-voltap -b --no-pager -n 12 2>/dev/null || true
}

case ${1:-} in
    install) shift; with=0; [[ ${1:-} == --hwdb ]] && with=1; cmd_install "$with" ;;
    --hwdb) cmd_install 1 ;;
    uninstall) cmd_uninstall ;;
    status) cmd_status ;;
    *) usage; exit 1 ;;
esac
