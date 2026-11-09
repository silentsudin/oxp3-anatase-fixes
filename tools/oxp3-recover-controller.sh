#!/bin/bash
# Recover the HHD-emulated controller after the oxp3-voltap grab incident.
# Stops and disables our legacy daemon, restarts HHD, waits for the controller and reports.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo $0" >&2; exit 2; }

echo "== stopping the legacy volume daemon (it was grabbing HHD's own device)"
systemctl disable --now oxp3-voltap.service 2>&1 | tail -2 || true

echo "== restarting HHD"
systemctl restart hhd.service

echo "== waiting for the emulated controller"
ok=no
for _ in $(seq 1 20); do
    sleep 1
    if lsusb 2>/dev/null | grep -qi '28de:12ff'; then ok=yes; break; fi
done

echo
echo "== hhd: $(systemctl is-active hhd.service)"
if [[ $ok == yes ]]; then
    echo "== controller: PRESENT (28de:12ff)"
else
    echo "== controller: NOT PRESENT yet"
    echo "   last hhd lines:"
    journalctl -u hhd.service -b --no-pager 2>/dev/null | tail -8
fi
echo
echo "== our verifier view of the HHD checks"
repo=$(cd "$(dirname "$0")/.." && pwd)
"$repo/tools/oxp3-verify.sh" 2>/dev/null | grep -E 'hhd\.|summary:' | tail -6 || true
[[ $ok == yes ]] || exit 1
