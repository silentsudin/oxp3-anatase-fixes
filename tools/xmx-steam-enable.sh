#!/bin/bash
# xmx-steam-enable.sh --appid <id> [--dry-run] : enable XeSS XMX for one native Steam game.
#
# Finds the game's install directory, the directory that actually holds libxess*.dll
# (often Engine/Plugins/Runtime/Intel/XeSS/Binaries/ThirdParty/Win64), swaps those
# libraries through tools/xmx-enable.sh and writes the XMX environment into that
# app's Steam LaunchOptions, so the game starts with the modified driver, the 128-GRF
# helper and IGC ocloc without any manual wrapper. The previous localconfig.vdf is
# backed up under ~/.local/state/oxp3-fixes/.
set -uo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
steam=${STEAM_ROOT:-$HOME/.local/share/Steam}
appid="" dry=0
while [ $# -gt 0 ]; do case $1 in
    --appid) appid=${2:-}; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "usage: $0 --appid <id> [--dry-run]" >&2; exit 2 ;;
esac; done
[ -n "$appid" ] || { echo "usage: $0 --appid <id> [--dry-run]" >&2; exit 2; }
lib=""
for d in "$steam"/steamapps /var/home/*/.local/share/Steam/steamapps; do
    [ -f "$d/appmanifest_$appid.acf" ] && lib=$d && break
done
[ -n "$lib" ] || { echo "error: no appmanifest_$appid.acf under $steam or other libraries" >&2; exit 1; }
install=$(sed -n 's/^[[:space:]]*"installdir"[[:space:]]*"\(.*\)"/\1/p' "$lib/appmanifest_$appid.acf" | head -1)
[ -n "$install" ] || { echo "error: no installdir in appmanifest_$appid.acf" >&2; exit 2; }
root=$lib/common/$install
prefix=$lib/compatdata/$appid/pfx/drive_c
xess_dir=$(find "$root" -maxdepth 12 -iname 'libxess.dll' -printf '%h\n' 2>/dev/null | head -1)
[ -n "$xess_dir" ] || { echo "not an XeSS game: no libxess.dll under $root" >&2; exit 1; }
envs="VK_DRIVER_FILES=$HOME/.config/xess-xmx/icd-xmx.json XMX_IGC_DIR=$HOME/.local/share/xess-xmx/igc ANV_CM_HELPER=$repo/tools/xmx-cm-compile-nodgrf.sh VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer INTC_ALT_DRIVER_EXTENSIONS_PATH=$HOME/.local/share/xess-xmx"
echo "appid $appid  install $root"
echo "  xess dir: $xess_dir"
echo "  prefix:   $prefix"
[ -d "$prefix" ] || { echo "error: prefix $prefix missing (launch the game once first)" >&2; exit 1; }
if (( dry )); then echo "  [dry-run] would run: $repo/tools/xmx-enable.sh enable --game $xess_dir --prefix $prefix"; exit 0; fi
"$repo/tools/xmx-enable.sh" enable --game "$xess_dir" --prefix "$prefix" || exit 1
localcfg=""
for f in "$steam"/userdata/*/config/localconfig.vdf; do [ -f "$f" ] && localcfg=$f; done
[ -n "$localcfg" ] || { echo "error: no localconfig.vdf found" >&2; exit 1; }
state=$HOME/.local/state/oxp3-fixes; mkdir -p "$state"
cp -a "$localcfg" "$state/localconfig.vdf.bak-$(date +%Y%m%dT%H%M%S)"
python3 - "$localcfg" "$appid" "$envs" <<'PY'
import re, sys
path, appid, envs = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8", errors="surrogateescape").read()
# Find the app block by its appid key and replace/add LaunchOptions inside it.
pat = re.compile(r'(\n(\s*)"%s"\s*\n\s*\{)' % re.escape(appid))
m = pat.search(text)
if not m:
    sys.exit("appid %s not present in localconfig.vdf (launch the game once, then close Steam)" % appid)
start = m.end()
# block end: matching closing brace by depth
depth = 1; i = start
while i < len(text) and depth:
    if text[i] == '{': depth += 1
    elif text[i] == '}': depth -= 1
    i += 1
block = text[start:i-1]
newval = '\\t\\t\\t\\t\\t\\t"LaunchOptions"\\t\\t"env %s %%command%%"' % envs
lo = re.compile(r'\n(\s*)"LaunchOptions"\s*"[^"]*"')
if lo.search(block):
    block = lo.sub('\n' + newval, block, count=1)
else:
    block = '\n' + newval + block
text = text[:start] + block + text[i-1:]
open(path, "w", encoding="utf-8", errors="surrogateescape").write(text)
print("localconfig.vdf: LaunchOptions for %s now sets the XMX environment" % appid)
PY
echo "restart Steam (or it rewrites the config on exit) and launch appid $appid"
