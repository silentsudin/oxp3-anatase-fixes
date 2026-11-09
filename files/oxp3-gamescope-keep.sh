#!/bin/bash
# Install the patched gamescope (PQ HDR on the AMS881KB01 OLED) into the writable
# /usr overlay, and re-apply it on every boot because rpm-ostree clears the overlay.
#
# The packaged build is gamescope-…oxp3 (patches/gamescope/0001-oxp3-hdr-pq.patch);
# the image ships the stock lodos build, which has no PQ HDR and shows either a black
# or a washed-out Gamemode output with --hdr-enabled.
set -uo pipefail
state=/var/lib/oxp3-fixes
sha_file=$state/gamescope.sha256
bin=/usr/bin/gamescope
log() { printf 'oxp3-gamescope-keep: %s\n' "$*"; }
case $(rpm -q --qf '%{RELEASE}\n' gamescope 2>/dev/null | head -1) in
    *oxp3*) exit 0 ;;
esac
want=""
[ -r "$sha_file" ] && want=$(cat "$sha_file")
if [ -n "$want" ] && [ "$(sha256sum "$bin" 2>/dev/null | cut -d' ' -f1)" = "$want" ]; then exit 0; fi
rpms=()
shopt -s nullglob
for d in /var/home/*/.cache/oxp3-fixes/out "${HOME:-/root}/.cache/oxp3-fixes/out"; do
    rpms+=("$d"/gamescope-*oxp3*.x86_64.rpm "$d"/gamescope-*oxp3*.i686.rpm)
done
shopt -u nullglob
(( ${#rpms[@]} )) || { log "no patched gamescope RPM in the cache"; exit 0; }
probe=/usr/.oxp3-rw-probe.$$
if ! install -m 000 /dev/null "$probe" 2>/dev/null; then
    rpm-ostree usroverlay >/dev/null 2>&1 || { log "usroverlay failed"; exit 0; }
    install -m 000 /dev/null "$probe" 2>/dev/null || { log "/usr still read-only"; exit 0; }
fi
rm -f "$probe"
mkdir -p "$state"
failed=0
for rpm in "${rpms[@]}"; do
    [ -r "$rpm" ] || continue
    rpm2cpio "$rpm" | ( cd / && cpio -idm --quiet ) || failed=1
done
(( failed )) && { log "one or more RPMS failed to extract"; exit 0; }
sha256sum "$bin" 2>/dev/null | cut -d' ' -f1 > "$sha_file"
log "patched gamescope installed (PQ HDR build)"
exit 0
