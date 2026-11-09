#!/bin/bash
# Keep the Gamemode HDR/PQ flags in /usr/libexec/anatase-gamemode across reboots.
#
# The session launcher lives in the immutable ostree deployment, so the documented
# HDR edit is written to the /usr overlay and disappears on reboot.  tools/display/
# gamemode-hdr.sh stores the patched launcher (and the original's digest) under
# /var/lib/oxp3-fixes; this restores it at boot only when the deployed launcher is
# still the version we patched, so an image update is never silently overwritten.
set -uo pipefail
state=/var/lib/oxp3-fixes
patched=$state/anatase-gamemode.hdr
orig_sha_file=$state/anatase-gamemode.orig.sha256
target=/usr/libexec/anatase-gamemode
log() { printf 'oxp3-gamemode-hdr-keep: %s\n' "$*"; }
grep -q -- '--hdr-enabled' "$target" 2>/dev/null && exit 0
[ -r "$patched" ] && [ -r "$orig_sha_file" ] || { log "no stored patched launcher"; exit 0; }
cur=$(sha256sum "$target" 2>/dev/null | cut -d' ' -f1)
want=$(cat "$orig_sha_file" 2>/dev/null)
if [ "$cur" != "$want" ]; then
    log "the deployed launcher changed (image update); not overwriting it. Re-run tools/display/gamemode-hdr.sh install"
    exit 0
fi
probe=/usr/.oxp3-rw-probe.$$
if ! install -m 000 /dev/null "$probe" 2>/dev/null; then
    rpm-ostree usroverlay >/dev/null 2>&1 || { log "usroverlay failed"; exit 0; }
    install -m 000 /dev/null "$probe" 2>/dev/null || { log "/usr still read-only"; exit 0; }
fi
rm -f "$probe"
install -m 0755 "$patched" "$target" && log "Gamemode HDR flags restored"
exit 0
