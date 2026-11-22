#!/bin/bash
# SPDX-License-Identifier: MIT
# Enable HDR/PQ output in the Gamemode session, persistently.
#
# The stock launcher starts gamescope without --hdr-enabled, so Gamemode output is
# SDR even though the panel is 10-bit PQ/HDR10. We keep a patched copy of the
# launcher in /usr/local/libexec (persistent across reboots and image updates,
# because /usr/local is a symlink to /var/usrlocal) and override the session unit
# with a systemd drop-in in /etc/systemd/user. Nothing is written to the immutable
# /usr tree and nothing lives on the rpm-ostree overlay.
#
# Usage: sudo tools/display/gamemode-hdr.sh status|install|remove
set -uo pipefail
source_launcher=/usr/libexec/anatase-gamemode
target=/usr/local/libexec/anatase-gamemode-hdr
dropin=/etc/systemd/user/anatase-gamemode.service.d/90-oxp3-hdr.conf
state=/var/lib/oxp3-fixes
repo=$(cd "$(dirname "$0")/../.." && pwd)
log() { printf 'gamemode-hdr: %s\n' "$*"; }
has_flags() { grep -q -- '--hdr-enabled' "$target" 2>/dev/null &&
    grep -q -- '--adaptive-sync' "$target" 2>/dev/null; }
case ${1:-status} in
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac
if [[ ${1:-status} != status && $EUID -ne 0 ]]; then
    echo "run with sudo: sudo $0 status|install|remove" >&2; exit 2
fi
case ${1:-status} in
    status)
        if has_flags; then log "patched launcher has the HDR/PQ flags ($target)"; else log "patched launcher missing or lacks flags ($target)"; fi
        if [ -r "$dropin" ]; then log "session override present ($dropin)"; else log "session override missing ($dropin)"; fi
        if grep -q "ExecStart=$target" "$dropin" 2>/dev/null; then log "override points at the patched launcher"; fi
        ;;
    install)
        [[ -r $source_launcher ]] || { echo "missing $source_launcher" >&2; exit 1; }
        mkdir -p /usr/local/libexec "$(dirname "$dropin")" "$state"
        python3 - "$source_launcher" "$target" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
text = text.replace("--hdr-pq-internal-enable", "--adaptive-sync")
if "--adaptive-sync" not in text:
    out, inserted = [], False
    for line in text.splitlines():
        out.append(line)
        if not inserted and "--backend drm" in line:
            out.append("    --adaptive-sync")
            inserted = True
    if not inserted:
        sys.exit("no DRM backend argument found")
    text = "\n".join(out) + "\n"
if "--hdr-enabled" not in text:
    out, inserted = [], False
    for line in text.splitlines():
        out.append(line)
        if not inserted and "-O" in line and "eDP-1" in line and line.lstrip().startswith("-O"):
            indent = line[: len(line) - len(line.lstrip())]
            out += [indent + "--hdr-enabled",
                    indent + "--hdr-sdr-content-nits 200"]
            inserted = True
    if not inserted:
        sys.exit("no gamescope_args -O eDP-1 line found")
    text = "\n".join(out) + "\n"
open(dst, "w", encoding="utf-8").write(text)
PY
        [[ $? -eq 0 ]] || { echo "could not build the patched launcher" >&2; exit 1; }
        chmod 0755 "$target"
        has_flags || { echo "patched launcher has no HDR flags" >&2; exit 1; }
        cat > "$dropin" <<CONF
# OneXPlayer 3: run the Gamemode launcher with HDR/PQ output.
# The patched copy of /usr/libexec/anatase-gamemode lives in /usr/local/libexec
# (persistent across reboots and image updates); this drop-in points the session
# unit at it. Revert: sudo tools/display/gamemode-hdr.sh remove
[Service]
ExecStart=
ExecStart=$target
CONF
        cp -f "$source_launcher" "$state/anatase-gamemode.orig"
        log "patched launcher installed at $target"
        log "session unit override written at $dropin"
        if [[ -n ${SUDO_USER:-} ]]; then
            systemctl --user --machine="$SUDO_USER@" daemon-reload 2>/dev/null || \
                log "log in again (or run systemctl --user daemon-reload) to pick up the override"
        else
            log "run systemctl --user daemon-reload in your session (or log in again)"
        fi
        ;;
    remove)
        rm -f "$dropin" "$target"
        log "HDR override removed; the next session uses the stock launcher"
        if [[ -n ${SUDO_USER:-} ]]; then
            systemctl --user --machine="$SUDO_USER@" daemon-reload 2>/dev/null || true
        fi
        ;;
    *) echo "usage: sudo $0 status|install|remove" >&2; exit 2 ;;
esac
