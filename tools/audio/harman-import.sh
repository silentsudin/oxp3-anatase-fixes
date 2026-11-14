#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Import a user's own Harman AudioEFX package (GA18v3.reg, optionally
# AudioEFXService64.exe) into a local generated speaker EQ.  No vendor file or
# extracted value is copied into the repository; everything stays under
# ~/.local/state/oxp3/audio/harman/.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
state=${XDG_STATE_HOME:-$HOME/.local/state}/oxp3/audio/harman
path="" force=0 profile=loud
while [ "$#" -gt 0 ]; do
    case $1 in
        --path) shift; path=${1:-} ;;
        --path=*) path=${1#--path=} ;;
        --force) force=1 ;;
        --profile) shift; profile=${1:-loud} ;;
        --profile=*) profile=${1#--profile=} ;;
        -h|--help) cat <<'EOF'
Usage: harman-import.sh [--path PATH] [--profile loud|safe] [--force]

PATH may be a downloaded Harman APO folder, a .zip of it, or GA18v3.reg.
The files are SHA-256 checked against the validated OneXPlayer 3 package.
--force accepts a different vendor revision.
EOF
            exit 0 ;;
        *) echo "harman-import.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done
case $profile in loud|safe) ;; *) echo "harman-import.sh: bad profile $profile" >&2; exit 2 ;; esac
mkdir -p "$state"

if [ -z "$path" ]; then
    for d in "$HOME/Downloads/Harmon APO" "$HOME/Downloads/oxp3/Harmon APO" \
             "$HOME/Downloads/oxp3/harmon" "$HOME/.local/share/oxp3-fixes/harman"; do
        [ -d "$d" ] && { path=$d; break; }
    done
fi
[ -n "$path" ] || { echo "harman-import.sh: no path; pass the downloaded Harman APO folder/zip or GA18v3.reg" >&2; exit 2; }
unpack() { # <zip> -> extracted directory
    local zip=$1 out=$state/unpacked
    command -v python3 >/dev/null || { echo "harman-import.sh: python3 needed to unpack $zip" >&2; exit 2; }
    rm -rf "$out"; mkdir -p "$out"
    python3 - "$zip" "$out" <<'PYZIP' || { echo "harman-import.sh: cannot unpack $zip" >&2; exit 2; }
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PYZIP
    printf '%s' "$out"
}
find_reg() { find "$1" -maxdepth 4 -type f -iname 'GA18v3.reg' -print -quit 2>/dev/null; }
find_exe() { find "$1" -maxdepth 4 -type f -iname 'AudioEFXService64.exe' -print -quit 2>/dev/null; }

if [ -d "$path" ]; then
    dir=$path
elif [ -f "$path" ]; then
    case $(basename "$path" | tr A-Z a-z) in
        *.zip) dir=$(unpack "$path") ;;
        *.reg) reg=$path; dir=$(dirname "$path") ;;
        *.exe|*.dll) dir=$(dirname "$path") ;;
        *) echo "harman-import.sh: expected a downloaded APO folder, .zip or GA18v3.reg" >&2; exit 2 ;;
    esac
else
    echo "harman-import.sh: no such path: $path" >&2; exit 2
fi
# A downloaded package may contain the files in a subdirectory, or be a folder
# holding a zip.  Locate the preset recursively and unpack a single zip if needed.
reg=$(find_reg "$dir")
if [ -z "${reg:-}" ]; then
    z=$(find "$dir" -maxdepth 2 -type f -iname '*.zip' -print -quit 2>/dev/null)
    if [ -n "$z" ]; then dir=$(unpack "$z"); reg=$(find_reg "$dir"); fi
fi
exe=$(find_exe "$dir")
[ -n "${reg:-}" ] && [ -f "$reg" ] || {
    echo "harman-import.sh: no GA18v3.reg in $dir" >&2
    echo "  Provide the downloaded Harman APO package (folder or .zip) containing GA18v3.reg;" >&2
    echo "  the registry file alone is also accepted." >&2
    exit 2
}
[ -n "${exe:-}" ] && [ -f "$exe" ] || exe=""

# Hashes seen in the OneXPlayer 3 vendor package this installer was validated
# against.  A different vendor revision is allowed only with --force.
declare -A known=(
    [GA18v3.reg]=32cfd82c8f10a1666e19c46fb836fd6b9edc44ac19d9784ff39e7c0a0c244e28
    [AudioEFXService64.exe]=e4c28d1705f376ca4a1c6b47b12828de00aac2bf7bb22ae1d7175571afd27986
    [AudioEFXAPO64.dll]=72e65578e9a2a9483ca3b7c264d5d3294f1d1bb8a8ee88b8db715cf5473b602f
)
check() {
    local f=$1 name want="" got
    name=$(basename "$f")
    got=$(sha256sum "$f" | cut -d' ' -f1)
    printf '  %-24s %s\n' "$name" "$got"
    [[ -v known[$name] ]] && want=${known[$name]}
    if [ -n "$want" ] && [ "$got" != "$want" ]; then
        if (( force )); then
            echo "  warning: $(basename "$f") hash differs from the validated package; continuing (--force)" >&2
        else
            echo "error: $(basename "$f") hash is not the validated OneXPlayer 3 package" >&2
            echo "  expected $want" >&2
            echo "  use --force only if you trust this vendor revision" >&2
            return 1
        fi
    elif [ -z "$want" ]; then
        echo "  warning: no pinned hash for $(basename "$f"); it will not be trusted without --force" >&2
        (( force )) || return 1
    fi
}
[ -f "$dir/AudioEFXAPO64.dll" ] && check "$dir/AudioEFXAPO64.dll"
check "$reg"
[ -n "$exe" ] && check "$exe"

echo "extracting from:"
echo "  reg: $reg"
[ -n "$exe" ] && echo "  exe: $exe"
python3 "$here/apo_extract.py" --reg "$reg" ${exe:+--exe "$exe"} \
    --json "$state/apo.json" --csv "$state/bands.csv" >"$state/extract.log" 2>&1 || {
    cat "$state/extract.log" >&2
    echo "harman-import.sh: extraction failed" >&2
    exit 1
}
python3 "$here/eq-conf-gen.py" --json "$state/apo.json" --profile "$profile" \
    --out "$state/oxp3-speaker-eq.generated.conf"
printf 'harman-import: generated %s\n' "$state/oxp3-speaker-eq.generated.conf"
