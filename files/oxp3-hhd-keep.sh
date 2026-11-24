#!/bin/bash
# SPDX-License-Identifier: MIT
# Re-apply our hhd patches (patches/hhd, deployed to /usr/local/share/oxp3-fixes/hhd-patches)
# at every boot, before hhd starts. hhd lives in /usr, and this deployment's /usr overlay is
# discarded at reboot, so a patch applied by ./oxp3-install.sh would otherwise last only until
# the next boot - for the OneXPlayer 3 power limits that meant hhd falling back to its 40/42 W
# preset.
#
# Each patch stands alone: one that no longer fits the installed hhd (an Anatase update changed
# the file) is skipped and logged, and the others still apply. Nothing is written when every
# patch is already in.
set -uo pipefail
dir=/usr/local/share/oxp3-fixes/hhd-patches
log() { printf 'oxp3-hhd-keep: %s\n' "$*"; }

site=$(python3 -c 'import hhd, os; print(os.path.dirname(os.path.dirname(hhd.__file__)))' 2>/dev/null) ||
    { log "hhd not installed"; exit 0; }
shopt -s nullglob
patches=("$dir"/*.patch)
shopt -u nullglob
(( ${#patches[@]} )) || exit 0

todo=()
for p in "${patches[@]}"; do
    if git -C "$site" apply --check -R -p1 "$p" >/dev/null 2>&1; then
        continue
    elif git -C "$site" apply --check -p1 "$p" >/dev/null 2>&1; then
        todo+=("$p")
    else
        log "${p##*/} does not fit this hhd ($(rpm -q hhd 2>/dev/null | head -1)), skipped"
    fi
done
(( ${#todo[@]} )) || exit 0

probe=/usr/.oxp3-rw-probe.$$
if ! install -m 000 /dev/null "$probe" 2>/dev/null; then
    # "Deployment is already in unlocked state" is a non-zero exit that still means the
    # overlay is (or is about to be) writable; other keep units race into it at boot.
    rpm-ostree usroverlay >/dev/null 2>&1 || true
    install -m 000 /dev/null "$probe" 2>/dev/null || { log "usroverlay did not become writable"; exit 0; }
fi
rm -f "$probe"

for p in "${todo[@]}"; do
    if git -C "$site" apply -p1 "$p"; then
        log "applied ${p##*/}"
    else
        log "applying ${p##*/} failed"
    fi
done
find "$site/hhd" "$site/adjustor" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
