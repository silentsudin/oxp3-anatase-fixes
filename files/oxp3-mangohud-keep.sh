#!/bin/bash
# SPDX-License-Identifier: MIT
# Install the patched MangoHud (Intel iGPU power/temperature + RAPL CPU power)
# from the locally built oxp3 RPMs into the writable /usr overlay, and do it
# again on every boot because rpm-ostree clears that overlay.
#
# The RPMs are built by ./oxp3-fixes-fast.sh build (patches/mangohud); this
# keeps the runtime in sync without a layered package transaction, which cannot
# replace the base mangohud on this image.
set -uo pipefail
log() { printf 'oxp3-mangohud-keep: %s\n' "$*"; }
case $(rpm -q --qf '%{RELEASE}' mangohud 2>/dev/null | head -1) in
    *oxp3*) exit 0 ;;
esac
rpms=()
shopt -s nullglob
for d in /var/home/*/.cache/oxp3-fixes/out "${HOME:-/root}/.cache/oxp3-fixes/out"; do
    rpms+=("$d"/mangohud-*oxp3*.x86_64.rpm "$d"/mangohud-*oxp3*.i686.rpm)
done
shopt -u nullglob
(( ${#rpms[@]} )) || { log "no patched RPMS in the cache"; exit 0; }
# Probe the filesystem, not "rpm-ostree status": a deployment can still report
# Unlocked: development while /usr is read-only after a reboot.
probe=/usr/.oxp3-rw-probe.$$
if ! install -m 000 /dev/null "$probe" 2>/dev/null; then
    # "Deployment is already in unlocked state" is a non-zero exit that still means
    # the overlay is (or is about to be) writable, and at boot two keep units race
    # into it. Ignore the status and decide by probing the filesystem instead.
    rpm-ostree usroverlay >/dev/null 2>&1 || true
    install -m 000 /dev/null "$probe" 2>/dev/null || { log "usroverlay did not become writable"; exit 0; }
fi
rm -f "$probe"
failed=0
for rpm in "${rpms[@]}"; do
    [ -r "$rpm" ] || continue
    rpm2cpio "$rpm" | ( cd / && cpio -idm --quiet ) || failed=1
done
(( failed )) && log "one or more RPMS failed to extract"
log "patched MangoHud installed into the /usr overlay"
exit 0
