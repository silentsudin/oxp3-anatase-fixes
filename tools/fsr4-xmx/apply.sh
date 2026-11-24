#!/bin/bash
# SPDX-License-Identifier: MIT
# apply.sh - make OptiScaler's "FSR 4 and DLSS on Intel XMX" upscaler work under Proton.
#
#   tools/fsr4-xmx/apply.sh --scan [--dry-run]       # every Steam game with the upscaler (what the install runs)
#   tools/fsr4-xmx/apply.sh <game folder> [--prefix <compatdata/<id>/pfx>] [--dry-run]
#
# The upscaler (OptiClick's FSR4_XMX bundle, or the Juij bundle) needs four things Wine does not give it:
#   1. Its XMX kernels are LZMS packs it unpacks with Cabinet.dll's Decompress(), which Wine lacks. Its import
#      is patched to fsr4dec.dll (same length; the original is kept as *.cabinet). Our fsr4dec.dll
#      (tools/fsr4-xmx/fsr4dec.c, built here with mingw) returns each pack from data.bin. The Juij bundle's
#      own fsr4dec.dll missed the first pack, so the XMX variants never appeared.
#   2. data.bin: the six packs unpacked. It is kept once per upscaler build in
#      ~/.cache/oxp3-fixes/fsr4-xmx/data-<build>.bin, taken from a game that already has a matching copy,
#      and hard-linked into each game. make-packs.py checks every slice against the SHA-256 the upscaler
#      verifies and writes fsr4dec-packs.txt; a data.bin of another build is refused.
#   3. igdext64.dll beside the upscaler. It searches beside itself, the system path and Intel-named
#      DriverStore folders, not the package's igd_faux.inf_1. Without it the image goes black ("[FSR4 XMX
#      FATAL] Intel DX12 extension igdext64.dll could not be resolved"), unless XeSS loaded the shim first.
#      The XeSS XMX package's shim goes there, and stale copies elsewhere are replaced by it.
#   4. The decoder next to the game's OptiScaler config as well as next to the upscaler. The loader
#      searches the game folder first.
# Nothing is touched when everything is in place. Every file replaced is kept with a suffix. No root.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/oxp3-fixes/fsr4-xmx
shim=${OXP3_XESS_DIR:-$HOME/.local/share/xess-xmx}/igdext64.dll
dry=0 scan=0 dir="" prefix=""
n=0
say() { printf '  %s\n' "$*"; }
short() { printf '%s' "${1/#$HOME/~}"; }

while (( $# )); do
    case $1 in
        --scan) scan=1 ;;
        --dry-run) dry=1 ;;
        --prefix) prefix=${2:-}; shift ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) dir=$1 ;;
    esac
    shift
done

build() {
    local dll=$cache/fsr4dec.dll
    if [[ -f $dll && $dll -nt $here/fsr4dec.c && $dll -nt $here/fsr4dec.def ]]; then
        printf '%s\n' "$dll"; return 0
    fi
    command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1 || {
        echo "fsr4-xmx: x86_64-w64-mingw32-gcc not found (mingw64-gcc); cannot build fsr4dec.dll" >&2; return 1; }
    mkdir -p "$cache"
    x86_64-w64-mingw32-gcc -O2 -Wall -shared -o "$dll.new" "$here/fsr4dec.c" "$here/fsr4dec.def" \
        -static-libgcc -Wl,--enable-stdcall-fixup >&2 && mv -f "$dll.new" "$dll" || return 1
    printf '%s\n' "$dll"
}

# the XMX upscaler, in either import form ("FSR 4 and DLSS on Intel XMX" is UTF-16 in its version
# resource; "[FSR4 XMX]" is plain ASCII)
is_xmx() { grep -aqF '[FSR4 XMX]' "$1" 2>/dev/null; }
imports_cabinet() { grep -aqF 'Cabinet.dll' "$1" 2>/dev/null && ! grep -aqF 'fsr4dec.dll' "$1" 2>/dev/null; }

# the game's OptiScaler folder: the nearest folder above the upscaler with OptiScaler.ini (Trails: the
# game folder; Talos: Binaries/Win64)
game_dir() {
    local d=${1%/*}
    while [[ $d == */* && $d != */steamapps/common ]]; do
        [[ -f $d/OptiScaler.ini ]] && { printf '%s\n' "$d"; return 0; }
        d=${d%/*}
    done
    return 1
}

link_or_copy() { ln -f "$1" "$2" 2>/dev/null || cp -f "$1" "$2"; }

# a data.bin for this upscaler build: the cached one, else a verified copy in a game folder (which
# then seeds the cache)
data_for() { # <upscaler> -> path
    local up=$1 k c f
    k=$(python3 -I "$here/make-packs.py" --key "$up" 2>/dev/null) || return 1
    c=$cache/data-$k.bin
    if [[ -f $c ]]; then printf '%s\n' "$c"; return 0; fi
    for f in "${seeds[@]}"; do
        python3 -I "$here/make-packs.py" "$up" "$f" /dev/null >/dev/null 2>&1 || continue
        (( dry )) && { printf '%s\n' "$f"; return 0; }
        mkdir -p "$cache" && link_or_copy "$f" "$c" && say "cached a verified data.bin for this build: $(short "$c")" >&2
        printf '%s\n' "$c"; return 0
    done
    return 1
}

fix_folder() { # <folder> <upscaler> <data.bin> <fsr4dec.dll>
    local fd=$1 up=$2 data=$3 dll=$4 f
    if (( dry )); then
        [[ -f $fd/fsr4dec-packs.txt && -f $fd/data.bin ]] && cmp -s "$dll" "$fd/fsr4dec.dll" ||
            say "[dry-run] would put fsr4dec.dll, data.bin and fsr4dec-packs.txt in $(short "$fd")"
        return 0
    fi
    [[ -f $data ]] || { say "$(short "$data"): missing - left alone"; return 0; }
    if [[ ! -f $fd/data.bin ]] || ! cmp -s "$data" "$fd/data.bin"; then
        # the new one first, then the swap: a failure never leaves the folder without data.bin
        link_or_copy "$data" "$fd/data.bin.new" || { say "$(short "$fd"): cannot place data.bin"; return 0; }
        [[ -f $fd/data.bin ]] && mv -f "$fd/data.bin" "$fd/data.bin.bundle"
        mv -f "$fd/data.bin.new" "$fd/data.bin"; n=$((n + 1))
    fi
    python3 -I "$here/make-packs.py" "$up" "$fd/data.bin" "$fd/fsr4dec-packs.txt.new" >/dev/null || {
        rm -f "$fd/fsr4dec-packs.txt.new"; say "$(short "$fd"): data.bin does not match - left alone"; return 0; }
    if cmp -s "$fd/fsr4dec-packs.txt.new" "$fd/fsr4dec-packs.txt"; then rm -f "$fd/fsr4dec-packs.txt.new"
    else mv -f "$fd/fsr4dec-packs.txt.new" "$fd/fsr4dec-packs.txt"; n=$((n + 1)); fi
    f=$fd/fsr4dec.dll
    if ! cmp -s "$dll" "$f"; then
        [[ -f $f ]] && ! grep -aq 'fsr4dec (oxp3)' "$f" && cp -f "$f" "$f.bundle"
        cp -f "$dll" "$f"; n=$((n + 1))
        say "$(short "$fd"): our fsr4dec.dll"
    fi
}

shim_to() { # <igdext64.dll path>: the package's shim, keeping another release of ours as .stale
    local f=$1
    [[ -f $shim ]] || return 0
    cmp -s "$f" "$shim" 2>/dev/null && return 0
    if [[ -f $f ]]; then
        grep -aq 'IGDEXT_' "$f" || return 0           # only our shim's older releases, never Intel's
        (( dry )) && { say "[dry-run] would replace $(short "$f") with the package shim"; return 0; }
        cp -f "$f" "$f.stale"
    fi
    (( dry )) && { say "[dry-run] would put the package shim at $(short "$f")"; return 0; }
    cp -f "$shim" "$f" && n=$((n + 1)) && say "$(short "$f"): XeSS XMX package shim"
}

apply_upscaler() { # <upscaler> [prefix]
    local up=$1 pfx=${2:-} gd data dll
    gd=$(game_dir "$up") || { say "$(short "$up"): no OptiScaler.ini above it - skipped"; return 0; }
    echo "== ${gd##*/common/} (${up#"$gd"/})"
    if imports_cabinet "$up"; then
        if (( dry )); then say "[dry-run] would patch its Cabinet.dll import to fsr4dec.dll"
        else
            cp -f "$up" "$up.cabinet"
            python3 -I - "$up" <<'PY' || { say "import patch failed"; return 0; }
import sys
p = sys.argv[1]; b = open(p, "rb").read()
assert b.count(b"Cabinet.dll\x00") == 1
open(p, "wb").write(b.replace(b"Cabinet.dll\x00", b"fsr4dec.dll\x00"))
PY
            n=$((n + 1)); say "Cabinet.dll import -> fsr4dec.dll (original kept as .cabinet)"
        fi
    fi
    data=$(data_for "$up") || {
        say "no data.bin for this upscaler build yet - XMX packs cannot load"
        say "(it is taken from any game whose data.bin verifies against this build)"; return 0; }
    dll=$(build) || return 1
    fix_folder "$gd" "$up" "$data" "$dll"
    [[ ${up%/*} != "$gd" ]] && fix_folder "${up%/*}" "$up" "$data" "$dll"
    shim_to "${up%/*}/igdext64.dll"
    local f
    for f in "$gd"/[Oo]pti[Ss]caler/igdext64.dll "$gd"/[Oo]pti[Ss]caler/custom/*/ffx/igdext64.dll \
             ${pfx:+"$pfx/drive_c/windows/system32/igdext64.dll"}; do
        [[ -f $f ]] && shim_to "$f"
    done
    return 0
}

seeds=()
upscalers=()
collect() { # <steamapps/common root>
    local f
    while IFS= read -r -d '' f; do
        is_xmx "$f" && upscalers+=("$f")
    done < <(find "$1" -maxdepth 9 -name amd_fidelityfx_upscaler_dx12.dll -size +40M -print0 2>/dev/null)
    while IFS= read -r -d '' f; do seeds+=("$f"); done \
        < <(find "$1" -maxdepth 9 -name data.bin -size +100M -print0 2>/dev/null)
}

if (( scan )); then
    libs=$(bash "${OXP3_XESS_DIR:-$HOME/.local/share/xess-xmx}/tools/steam-libraries.sh" 2>/dev/null || true)
    [[ -n $libs ]] || libs=$HOME/.local/share/Steam
    while IFS= read -r lib; do [[ -n $lib && -d $lib/steamapps/common ]] && collect "$lib/steamapps/common"; done <<< "$libs"
    (( ${#upscalers[@]} )) || { say "no game with OptiScaler's FSR 4 XMX upscaler"; exit 0; }
    for up in "${upscalers[@]}"; do
        lib=${up%%/steamapps/common/*}
        game=${up#"$lib"/steamapps/common/}; game=${game%%/*}
        id=$(grep -l "\"installdir\"[[:space:]]*\"$game\"" "$lib"/steamapps/appmanifest_*.acf 2>/dev/null | head -1)
        id=${id##*appmanifest_}; id=${id%.acf}
        apply_upscaler "$up" "${id:+$lib/steamapps/compatdata/$id/pfx}"
    done
else
    [[ -n $dir && -d $dir ]] || { sed -n '4,5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
    collect "$dir"
    (( ${#upscalers[@]} )) || { say "no FSR 4 XMX upscaler under $(short "$dir")"; exit 0; }
    for up in "${upscalers[@]}"; do apply_upscaler "$up" "$prefix"; done
fi
(( n )) || say "already in place"
exit 0
