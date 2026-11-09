#!/bin/bash
# Restore the recorded known-good host Mesa and Flatpak runtime Mesa at boot.
#
# rpm-ostree clears the /usr overlay ("usroverlay") on every boot, so the patched
# ec418f04 host libvulkan_intel.so installed there does not survive a reboot, and
# the Decky health line then reports a Mesa mismatch.  This runs before the
# display manager, re-enters the usroverlay only when the digest differs, restores
# the known-good binary, refreshes the Flatpak runtime override and restarts the
# extension service.  It is a no-op when everything already matches.
set -uo pipefail
log() { printf 'oxp3-mesa-keep: %s\n' "$*"; }
find_known() {
    local f
    for f in /var/home/*/.cache/oxp3-fixes/known-good "$HOME/.cache/oxp3-fixes/known-good"; do
        [ -e "$f" ] && { printf '%s' "$f"; return 0; }
    done
    return 1
}
kd=$(find_known) || { log "no known-good directory; nothing to do"; exit 0; }
host_known=$kd/host-ec418f04-libvulkan_intel.so
runtime_known=$kd/runtime-4d043cc2-libvulkan_intel.so
restore_host=0 restore_runtime=0
want_host=$(sha256sum "$host_known" 2>/dev/null | cut -d' ' -f1)
want_runtime=$(sha256sum "$runtime_known" 2>/dev/null | cut -d' ' -f1)
if [ -n "$want_host" ] && [ -r "$host_known" ]; then
    cur=$(sha256sum /usr/lib64/libvulkan_intel.so 2>/dev/null | cut -d' ' -f1)
    [ "$cur" = "$want_host" ] || restore_host=1
fi
if [ -n "$want_runtime" ] && [ -r "$runtime_known" ]; then
    cur_rt=$(sha256sum /usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides/lib64/libvulkan_intel.so 2>/dev/null | cut -d' ' -f1)
    [ "$cur_rt" = "$want_runtime" ] || restore_runtime=1
fi
(( restore_host || restore_runtime )) || exit 0
if ! rpm-ostree status 2>/dev/null | grep -q 'Unlocked: hotfix'; then
    rpm-ostree usroverlay >/dev/null 2>&1 || { log "usroverlay failed"; exit 0; }
fi
if (( restore_host )); then
    cp -f -- "$host_known" /usr/lib64/libvulkan_intel.so && log "host Mesa restored"
fi
if (( restore_runtime )); then
    rt=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides/lib64
    if [ -d "$rt" ]; then
        cp -f -- "$runtime_known" "$rt/libvulkan_intel.so" && log "Flatpak runtime Mesa restored"
        systemctl try-restart anatase-flatpak-extensions.service >/dev/null 2>&1 || true
    fi
fi
exit 0
