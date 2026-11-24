#!/bin/bash
# SPDX-License-Identifier: MIT
# xmx-steam-enable.sh --appid <id> | --all [--dry-run]
#
# Enable the XeSS XMX path for native Steam titles. For each game that actually
# ships libxess*.dll this finds the library directory (often under
# Engine/Plugins/Runtime/Intel/XeSS/...), swaps it through tools/xmx-enable.sh
# (originals kept in .xmx-orig) and writes the XMX environment into that app's
# Steam LaunchOptions, after backing up localconfig.vdf. Games without XeSS are
# skipped, so --all is safe to re-run.
set -uo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
steam_roots=("${STEAM_ROOT:-$HOME/.local/share/Steam}" "$HOME/.var/app/org.anatase.Steam/.local/share/Steam")
appid="" all=0 dry=0
while [ $# -gt 0 ]; do case $1 in
    --appid) appid=${2:-}; shift 2 ;;
    --all) all=1; shift ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: $0 --appid <id> | --all [--dry-run]" >&2; exit 2 ;;
esac; done
[ -n "$appid" ] || (( all )) || { echo "usage: $0 --appid <id> | --all [--dry-run]" >&2; exit 2; }

find_lib() { # <appid> -> steamapps dir
    local d
    for d in "${steam_roots[@]}"/steamapps /var/home/*/.local/share/Steam/steamapps \
             /var/home/*/.var/app/org.anatase.Steam/.local/share/Steam/steamapps; do
        [ -f "$d/appmanifest_$1.acf" ] && { echo "$d"; return 0; }
    done
    return 1
}
localconfig() {
    local f
    for f in "${steam_roots[@]}"/userdata/*/config/localconfig.vdf /var/home/*/.local/share/Steam/userdata/*/config/localconfig.vdf; do
        [ -f "$f" ] && { echo "$f"; return 0; }
    done
    return 1
}

enable_one() { # <appid> ; 0 enabled/skipped, 1 error
    local id=$1 lib install root prefix xess_dir localcfg state envs
    lib=$(find_lib "$id") || { echo "  $id: no appmanifest"; return 1; }
    install=$(sed -n 's/^[[:space:]]*"installdir"[[:space:]]*"\(.*\)"/\1/p' "$lib/appmanifest_$id.acf" | head -1)
    [ -n "$install" ] || { echo "  $id: no installdir"; return 1; }
    root=$lib/common/$install
    [ -d "$root" ] || { echo "  $id: install dir missing"; return 1; }
    xess_dir=$(find "$root" -maxdepth 12 -iname 'libxess.dll' -printf '%h\n' 2>/dev/null | head -1)
    [ -n "$xess_dir" ] || { echo "  $id: not an XeSS game"; return 0; }
    prefix=$lib/compatdata/$id/pfx/drive_c
    [ -d "$prefix" ] || { echo "  $id: prefix missing (launch it once first)"; return 1; }
    localcfg=$(localconfig) || { echo "  $id: no localconfig.vdf (launch Steam once)"; return 1; }
    envs="VK_DRIVER_FILES=$HOME/.config/xess-xmx/icd-xmx.json VK_ICD_FILENAMES=$HOME/.config/xess-xmx/icd-xmx.json ANV_CM_KERNEL_DIR=$HOME/.cache/xess-xmx/kernels XMX_IGC_DIR=$HOME/.local/share/xess-xmx/igc ANV_CM_HELPER=$repo/tools/xmx-cm-compile-nodgrf.sh VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer INTC_ALT_DRIVER_EXTENSIONS_PATH=$HOME/.local/share/xess-xmx force_vk_vendor=0"
    echo "  $id: $install"
    echo "        xess: $xess_dir"
    if (( dry )); then echo "        [dry-run] would enable"; return 0; fi
    "$repo/tools/xmx-enable.sh" enable --game "$xess_dir" --prefix "$prefix" >/dev/null || {
        echo "        xmx-enable failed"; return 1; }
    state=$HOME/.local/state/oxp3-fixes; mkdir -p "$state"
    cp -a "$localcfg" "$state/localconfig.vdf.bak-$(date +%Y%m%dT%H%M%S)"
    python3 - "$localcfg" "$id" "$envs" <<'PY' || { echo "        localconfig edit failed"; return 1; }
import re, sys
path, appid, envs = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8", errors="surrogateescape").read()
m = re.search(r'(\n(\s*)"%s"\s*\n\s*\{)' % re.escape(appid), text)
if not m:
    sys.exit("appid %s not present in localconfig.vdf (launch the game once, then close Steam)" % appid)
start = m.end(); depth = 1; i = start
while i < len(text) and depth:
    if text[i] == '{': depth += 1
    elif text[i] == '}': depth -= 1
    i += 1
block = text[start:i-1]
newval = '\\t\\t\\t\\t\\t\\t"LaunchOptions"\\t\\t"env %s %%command%%"' % envs
lo = re.compile(r'\n(\s*)"LaunchOptions"\s*"[^"]*"')
block = lo.sub('\n' + newval, block, count=1) if lo.search(block) else '\n' + newval + block
open(path, "w", encoding="utf-8", errors="surrogateescape").write(text[:start] + block + text[i-1:])
print("        LaunchOptions updated")
PY
    echo "        enabled (restart Steam so it re-reads the config)"
    return 0
}

# System mode: the host ANV carries the CM injection, so the session environment, the shim in
# Proton and its launch hook cover every XeSS game. The per-game options this tool used to write
# select the retired split driver (VK_DRIVER_FILES=icd-xmx.json): the game gets no Vulkan driver.
if grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so 2>/dev/null; then
    echo "XeSS XMX runs session-wide here (the host Mesa carries the CM injection): no launch options needed."
    echo "./oxp3-install.sh clears launch options an older version of this tool wrote."
    exit 0
fi

rc=0
if (( all )); then
    seen=0
    for d in "${steam_roots[@]}"/steamapps /var/home/*/.local/share/Steam/steamapps \
             /var/home/*/.var/app/org.anatase.Steam/.local/share/Steam/steamapps; do
        for m in "$d"/appmanifest_*.acf; do
            [ -f "$m" ] || continue
            id=$(basename "$m"); id=${id#appmanifest_}; id=${id%.acf}
            seen=$((seen + 1))
            enable_one "$id" || rc=1
        done
    done
    (( seen )) || { echo "no Steam appmanifests found"; exit 1; }
    echo "checked $seen installed Steam app(s)"
else
    enable_one "$appid" || rc=1
fi
if pgrep -x steam >/dev/null 2>&1 || pgrep -f '[s]teamwebhelper' >/dev/null 2>&1; then
    echo "note: Steam is running; LaunchOptions are re-read on restart and Steam rewrites localconfig on exit, so restart it."
fi
exit $rc
