#!/bin/bash
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
for d in /var/home/*/.cache/oxp3-fixes/out "$HOME/.cache/oxp3-fixes/out"; do
    rpms+=("$d"/mangohud-*oxp3*.x86_64.rpm "$d"/mangohud-*oxp3*.i686.rpm)
done
shopt -u nullglob
(( ${#rpms[@]} )) || { log "no patched RPMS in the cache"; exit 0; }
unlock=$(rpm-ostree status --booted 2>/dev/null | awk -F': ' '/Unlocked/{print $2}')
case ${unlock:-none} in
    none|"") rpm-ostree usroverlay >/dev/null 2>&1 || { log "usroverlay failed"; exit 0; } ;;
esac
failed=0
for rpm in "${rpms[@]}"; do
    [ -r "$rpm" ] || continue
    rpm2cpio "$rpm" | ( cd / && cpio -idm --quiet ) || failed=1
done
(( failed )) && log "one or more RPMS failed to extract"
log "patched MangoHud installed into the /usr overlay"
exit 0
