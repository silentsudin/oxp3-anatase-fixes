#!/bin/bash
# SPDX-License-Identifier: MIT
# Restore our Mesa into the writable /usr overlay at boot, for the host and for the Anatase
# Flatpak runtime overrides.
#
# rpm-ostree clears the /usr overlay ("usroverlay") on every boot, so what `install` put there
# is gone afterwards. The other keep units re-extract one package each; Mesa is a whole stack
# (libvulkan_intel.so, libgallium, the DRI/EGL/GL/GBM libraries, the ICD manifests), and
# restoring only the Vulkan driver leaves one version of it on top of the image's gallium/GL -
# measured 2026-11-23: 26.3.1 Vulkan with libgallium-26.2.6.so.
#
# So: re-enter the overlay, check that the recorded version is really there, and if not extract
# the cached Mesa RPMs with cpio (bypassing rpm's dependency resolution, like the gamescope and
# MangoHud keep units) into / and into the Flatpak runtime overrides. Idempotent: it exits
# immediately when the tree already matches.
set -uo pipefail
state=/var/lib/oxp3-fixes
verfile=$state/mesa.version
listfile=$state/mesa-flatpak.list
rt=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable
log() { printf 'oxp3-mesa-keep: %s\n' "$*"; }

[ -r "$verfile" ] || { log "no recorded Mesa version; nothing to restore"; exit 0; }
want=$(cat "$verfile")
[ -n "$want" ] || { log "empty Mesa version file; nothing to restore"; exit 0; }

# Canaries: the gallium library carries the version in its name, and the driver hash is what
# known-good/oxp3-verify compare against.
gal=/usr/lib64/libgallium-$want.so
rtdrv=$rt/overrides/lib64/libvulkan_intel.so

host_ok=0; [ -e "$gal" ] && host_ok=1
runtime_wanted=0
[ -r "$listfile" ] && [ -d "$rt/overrides" ] && runtime_wanted=1
runtime_differs() {
    (( runtime_wanted )) || return 1
    [ -e "$rtdrv" ] || return 0
    ! cmp -s /usr/lib64/libvulkan_intel.so "$rtdrv"
}
# nothing to do when the host tree is there and the runtime overrides match it
if [ "$host_ok" = 1 ] && ! runtime_differs; then exit 0; fi

rpms=()
shopt -s nullglob
for d in /var/home/*/.cache/oxp3-fixes/out "${HOME:-/root}/.cache/oxp3-fixes/out"; do
    rpms+=("$d"/mesa-*"$want"*oxp3*.x86_64.rpm "$d"/mesa-*"$want"*oxp3*.i686.rpm)
done
shopt -u nullglob
(( ${#rpms[@]} )) || { log "no cached Mesa $want RPMs; run ./oxp3-install.sh"; exit 0; }

# Probe the filesystem, not "rpm-ostree status": a deployment can still report
# Unlocked: development while /usr is read-only after a reboot.
probe=/usr/.oxp3-rw-probe.$$
if ! install -m 000 /dev/null "$probe" 2>/dev/null; then
    # "Deployment is already in unlocked state" is a non-zero exit that still means the overlay
    # is (or is about to be) writable, and at boot several keep units race into it.
    rpm-ostree usroverlay >/dev/null 2>&1 || true
    install -m 000 /dev/null "$probe" 2>/dev/null || { log "usroverlay did not become writable"; exit 0; }
fi
rm -f "$probe"

# Only extract a build that fits the booted image: cpio bypasses rpm's checks, so an RPM built
# for another Anatase image could drop libraries into /usr that the image does not have.
if ! rpm -U --test --replacepkgs --oldpackage --nosignature "${rpms[@]}" >/dev/null 2>&1; then
    log "cached Mesa $want RPMs do not fit this image; keeping the image's Mesa (run ./oxp3-install.sh)"
    exit 0
fi

failed=0
if [ "$host_ok" = 0 ]; then
    for rpm in "${rpms[@]}"; do
        [ -r "$rpm" ] || continue
        rpm2cpio "$rpm" | ( cd / && cpio -idm --quiet ) || failed=1
    done
    (( failed )) && { log "one or more Mesa RPMs failed to extract"; exit 0; }
    log "host Mesa $want restored"
fi

# Flatpak runtime overrides: the same files with the leading usr/ stripped, which is how the
# runtime is mounted inside the sandbox. The list is written by `install`.
if runtime_differs; then
    tmp=$(mktemp -d) || exit 0
    for rpm in "${rpms[@]}"; do
        [ -r "$rpm" ] || continue
        rpm2cpio "$rpm" | ( cd "$tmp" && cpio -idm --quiet ) || failed=1
    done
    ( cd "$tmp/usr" 2>/dev/null && grep -vE '^/|(^|/)\.\.(/|$)' "$listfile" |
        while IFS= read -r p; do
            [ -e "$p" ] || continue
            mkdir -p "$rt/overrides/$(dirname "$p")" &&
                cp -a --remove-destination "$p" "$rt/overrides/$p"
        done ) || failed=1
    rm -rf "$tmp"
    (( failed )) && log "one or more Mesa files failed to reach the Flatpak runtime"
    [ -e "$rtdrv" ] && log "Flatpak runtime Mesa $want restored"
fi

exit 0
