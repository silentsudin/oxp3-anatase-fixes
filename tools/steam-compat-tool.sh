#!/bin/bash
# SPDX-License-Identifier: MIT
# steam-compat-tool.sh - read or set the Proton (and therefore DXVK) Steam uses for a game or as
# the default, with a backup of config.vdf.
#
# Why this exists: the same D3D11 workload runs at ~88 fps on DXVK 3.1.x and ~82 fps on 3.0.2 /
# 2.7.1 (measured, docs/DX11-PERFORMANCE.md), so the Proton choice is worth ~7% with nothing
# patched. Doing it by hand means hunting through config.vdf; this edits exactly the "0" (default)
# or one app's entry in CompatToolMapping and nothing else.
#
# Steam rewrites config.vdf when it exits, so run this with Steam CLOSED, and restart it afterwards.
#
# Usage:
#   steam-compat-tool.sh list                    installed tools and their DXVK versions
#   steam-compat-tool.sh default                 what the default is now
#   steam-compat-tool.sh set-default <tool>      e.g. "Proton - Experimental"
#   steam-compat-tool.sh set-app <appid> <tool>
#   steam-compat-tool.sh unset-app <appid>
#   steam-compat-tool.sh restore                 put the newest backup back
set -uo pipefail
steam=${STEAM_DIR:-$HOME/.local/share/Steam}
cfg=$steam/config/config.vdf
repo=$(cd "$(dirname "$0")/.." && pwd)
here=$repo

die() { echo "error: $*" >&2; exit 1; }
steam_running() { pgrep -x steam >/dev/null 2>&1 || pgrep -x steamwebhelper >/dev/null 2>&1; }

dxvk_version() {
    local d=$1 f
    for f in "$d"/files/lib64/wine/dxvk/version "$d"/files/lib/wine/dxvk/version; do
        [[ -r $f ]] || continue
        awk '{ v = $NF; gsub(/[()]/, "", v); print v }' "$f"
        return 0
    done
}

cmd_list() {
    local d v
    for d in "$steam"/compatibilitytools.d/*/ "$steam"/steamapps/common/Proton*/; do
        [[ -d $d ]] || continue
        v=$(dxvk_version "${d%/}")
        [[ -n $v ]] || continue
        printf '%-30s DXVK %s\n' "$(basename "${d%/}")" "$v"
    done
}

cmd_default() {
    [[ -r $cfg ]] || die "no $cfg"
    python3 "$repo/tools/lib/dxvk-steam-default.py" "$cfg"
}

write_tool() { # <appid|0> <tool>
    local app=$1 tool=$2
    [[ -r $cfg ]] || die "no $cfg"
    steam_running && echo "warning: Steam is running and will overwrite this on exit" >&2
    local backup; backup=$cfg.oxp3-$(date +%Y%m%d-%H%M%S)
    cp -a "$cfg" "$backup" || die "cannot back up $cfg"
    python3 - "$cfg" "$app" "$tool" <<'PY' || die "could not edit $cfg (Steam's format may have changed)"
import re, sys
path, app, tool = sys.argv[1:4]
s = open(path, errors="replace").read()
i = s.find('"CompatToolMapping"')
if i < 0:
    sys.exit("no CompatToolMapping block")
j = s.index("{", i)
depth = 0
for k in range(j, len(s)):
    if s[k] == "{":
        depth += 1
    elif s[k] == "}":
        depth -= 1
        if depth == 0:
            end = k
            break
else:
    sys.exit("unterminated CompatToolMapping")
block = s[j:end + 1]
entry = '\t\t\t\t\t"%s"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"%s"\n\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t}\n' % (app, tool)
m = re.search(r'(\n\t*)"%s"\n\t*\{[^{}]*\}' % re.escape(app), block)
if m:
    new_block = block[:m.start()] + "\n" + entry.rstrip("\n") + block[m.end():]
else:
    new_block = block[:-1] + entry + block[-1:]
open(path, "w").write(s[:j] + new_block + s[end + 1:])
print("set %s -> %s" % ("default" if app == "0" else "appid " + app, tool))
PY
    echo "backup: $backup"
}

cmd_unset_app() {
    local app=$1
    [[ -r $cfg ]] || die "no $cfg"
    local backup; backup=$cfg.oxp3-$(date +%Y%m%d-%H%M%S)
    cp -a "$cfg" "$backup" || die "cannot back up $cfg"
    python3 - "$cfg" "$app" <<'PY' || die "could not edit $cfg"
import re, sys
path, app = sys.argv[1:3]
s = open(path, errors="replace").read()
m = re.search(r'\n\t*"%s"\n\t*\{[^{}]*\}' % re.escape(app), s)
if not m:
    print("appid %s was not set" % app); raise SystemExit(0)
open(path, "w").write(s[:m.start()] + s[m.end():])
print("unset appid %s" % app)
PY
    echo "backup: $backup"
}

cmd_restore() {
    local newest; newest=$(ls -t "$cfg".oxp3-* 2>/dev/null | head -1)
    [[ -n $newest ]] || die "no backup next to $cfg"
    cp -a "$newest" "$cfg" && echo "restored $cfg from $newest"
}

case ${1:-} in
    list)        cmd_list ;;
    default)     cmd_default ;;
    set-default) [[ $# -ge 2 ]] || die "usage: $0 set-default <tool>"; write_tool 0 "$2" ;;
    set-app)     [[ $# -ge 3 ]] || die "usage: $0 set-app <appid> <tool>"; write_tool "$2" "$3" ;;
    unset-app)   [[ $# -ge 2 ]] || die "usage: $0 unset-app <appid>"; cmd_unset_app "$2" ;;
    restore)     cmd_restore ;;
    -h|--help|*) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
