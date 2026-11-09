#!/bin/bash
# Enable HDR/PQ output in the Gamemode session.
#
# The Anatase session launcher starts gamescope without --hdr-enabled, so Gamemode
# output is SDR even though the panel is 10-bit PQ/HDR10. This patches the launcher
# (documented in docs/hw/hdr-vibrance.md), stores the patched copy and the original
# digest under /var/lib/oxp3-fixes, and installs a boot unit that re-applies it
# because /usr is an immutable ostree tree behind an overlay that reboots clear.
#
# Usage: sudo tools/display/gamemode-hdr.sh status|install|remove
set -uo pipefail
target=/usr/libexec/anatase-gamemode
state=/var/lib/oxp3-fixes
patched=$state/anatase-gamemode.hdr
orig_sha=$state/anatase-gamemode.orig.sha256
keep=/usr/local/libexec/oxp3-gamemode-hdr-keep
unit=/etc/systemd/system/oxp3-gamemode-hdr-keep.service
repo=$(cd "$(dirname "$0")/../.." && pwd)
log() { printf 'gamemode-hdr: %s\n' "$*"; }
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }
has_flags() { grep -q -- '--hdr-enabled' "$target" 2>/dev/null; }
case ${1:-status} in
    -h|--help) usage; exit 0 ;;
esac
if [[ ${1:-status} != status && $EUID -ne 0 ]]; then
    echo "run with sudo: sudo $0 status|install|remove" >&2; exit 2
fi
ensure_rw() {
    local probe=/usr/.oxp3-rw-probe.$$
    if install -m 000 /dev/null "$probe" 2>/dev/null; then rm -f "$probe"; return 0; fi
    rpm-ostree usroverlay >/dev/null 2>&1 || return 1
    install -m 000 /dev/null "$probe" 2>/dev/null && { rm -f "$probe"; return 0; }
    return 1
}
case ${1:-status} in
    status)
        if has_flags; then log "HDR/PQ flags present in $target"; else log "HDR/PQ flags MISSING in $target (Gamemode is SDR)"; fi
        [ -r "$patched" ] && log "stored patched launcher: $patched" || log "no stored patched launcher"
        systemctl is-enabled oxp3-gamemode-hdr-keep.service >/dev/null 2>&1 && log "boot keep unit enabled" || log "boot keep unit not enabled"
        ;;
    install)
        [[ -r $target ]] || { echo "missing $target" >&2; exit 1; }
        mkdir -p "$state"
        if has_flags; then
            log "flags already present; refreshing the stored copy"
            cp -f "$target" "$patched"
        else
            if [ ! -e "$orig_sha" ]; then
                sha256sum "$target" | cut -d' ' -f1 > "$orig_sha"
                log "recorded the deployed launcher digest"
            fi
            ensure_rw || { echo "could not make /usr writable (rpm-ostree usroverlay)" >&2; exit 1; }
            python3 - "$target" <<'PY'
import sys
p = sys.argv[1]
text = open(p, encoding="utf-8").read()
if "--hdr-enabled" in text:
    sys.exit(0)
lines = text.splitlines()
out = []
inserted = False
for line in lines:
    out.append(line)
    if not inserted and "eDP-1" in line and "-O" in line and line.lstrip().startswith("-O"):
        indent = line[: len(line) - len(line.lstrip())]
        out.append(indent + "--hdr-enabled")
        out.append(indent + "--hdr-pq-internal-enable")
        out.append(indent + "--hdr-sdr-content-nits 200")
        inserted = True
if not inserted:
    sys.exit("no gamescope_args -O eDP-1 line found")
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
            [[ $? -eq 0 ]] || { echo "could not patch the launcher (no -O eDP-1 line?)" >&2; exit 1; }
            log "patched $target"
            cp -f "$target" "$patched"
        fi
        install -D -m0755 "$repo/files/display/oxp3-gamemode-hdr-keep.sh" "$keep"
        install -D -m0644 "$repo/files/display/oxp3-gamemode-hdr-keep.service" "$unit"
        systemctl daemon-reload
        systemctl enable oxp3-gamemode-hdr-keep.service
        has_flags && log "HDR/PQ enabled; it persists across reboots" || { echo "patch did not take" >&2; exit 1; }
        ;;
    remove)
        ensure_rw || true
        if [ -r "$patched" ]; then
            cp -f "$patched" "$target"
            log "kept the HDR flags (remove needs the image's original launcher)"
        fi
        systemctl disable --now oxp3-gamemode-hdr-keep.service 2>/dev/null || true
        log "boot keep unit disabled; to drop the flags re-deploy the image or restore the original launcher"
        ;;
    *) echo "usage: sudo $0 status|install|remove" >&2; exit 2 ;;
esac
