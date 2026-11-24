#!/bin/bash
# SPDX-License-Identifier: MIT
# xess-ab.sh - switch the XeSS/XMX configuration between known states, one command each.
#
# Why this exists: every XeSS setting used to be tested by editing the session environment or the
# package by hand and rebooting, which mixes variables and burns a game session per guess. The
# states below are the ones that have actually been measured on this machine, so a test is one
# command plus a game restart (the shim is read per launch; the session environment needs a new
# Gamemode session).
#
#   tools/xess-ab.sh status                  shim/ControlLib hashes, env file, kernel cache count
#   tools/xess-ab.sh kernels off|on          move the precompiled .cmk cache aside / back
#   tools/xess-ab.sh shim 20261029|20261120  swap the shim+ControlLib pair from the cached releases
#   tools/xess-ab.sh env pkg|pkg-kernels|pkg-nodgrf|nodgrf-kernels
#                                            rewrite ~/.config/environment.d/50-xess-xmx.conf
#   tools/xess-ab.sh backup                  copy the current state next to the release backups
#
# no root needed. Every change keeps a timestamped backup of what it replaced.
set -uo pipefail
cache=${OXP3_GPU_BENCH_CACHE:-$HOME/.cache/oxp3-fixes}
bkp=$cache/xess-backup
pkg=$HOME/.local/share/xess-xmx
kdir=$HOME/.cache/xess-xmx/kernels
envf=$HOME/.config/environment.d/50-xess-xmx.conf
mkdir -p "$bkp"
stamp=$(date +%Y%m%d-%H%M%S)

die() { echo "xess-ab: $*" >&2; exit 1; }

case ${1:-status} in
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

backup() {
    if [[ -r $envf ]]; then cp -f "$envf" "$bkp/50-xess-xmx.conf.$stamp"; fi
    if [[ -r $pkg/igdext64.dll ]]; then cp -f "$pkg/igdext64.dll" "$bkp/igdext64.dll.$stamp"; fi
    echo "xess-ab: backed up env and shim as *.$stamp"
}

KEYS="ANV_CM_HELPER ANV_CM_KERNEL_DIR XMX_IGC_DIR INTC_ALT_DRIVER_EXTENSIONS_PATH CM_MAX_GRF"

write_env() { # <body>
    mkdir -p "$(dirname "$envf")"
    { echo "# XeSS XMX session environment, written by tools/xess-ab.sh ($stamp)"; cat; } > "$envf"
    # environment.d is read at login only. The running user manager keeps its own copy
    # (systemctl --user set-environment, done by an install or xmx-session.sh) and that copy is what
    # games inherit - so without this, a switch changes nothing until the next login and every
    # measurement comes out identical. Set the live values too, and unset what the new state drops.
    local kv=() k
    while IFS= read -r k; do
        [[ -n $k ]] && kv+=( "$k" )
    done < <(grep -E '^[A-Z_]+=' "$envf")
    # shellcheck disable=SC2086
    systemctl --user unset-environment $KEYS 2>/dev/null || true
    if (( ${#kv[@]} )); then systemctl --user set-environment "${kv[@]}" 2>/dev/null || true; fi
    grep -vE '^\s*#' "$envf" | sed 's/^/  /'
    echo "  live user manager: $(systemctl --user show-environment 2>/dev/null | grep -cE '^(ANV_CM|CM_MAX_GRF|XMX_IGC|INTC_ALT)=') key(s) set"
}

cmd_status() {
    echo "package:  $pkg"
    for f in igdext64.dll ControlLib.dll; do
        [ -r "$pkg/$f" ] && printf '  %-16s %s\n' "$f" "$(sha256sum "$pkg/$f" | cut -c1-16)"
    done
    echo "env:      $envf"
    [ -r "$envf" ] && grep -vE '^\s*#' "$envf" | sed 's/^/  /' || echo "  (none)"
    if [ -d "$kdir" ]; then echo "kernels:  $(ls "$kdir" | wc -l) cached .cmk"; else echo "kernels:  no kernel directory (run-time compile)"; fi
    echo "live env: (what games inherit; environment.d is only read at login)"
    systemctl --user show-environment 2>/dev/null | grep -E '^(ANV_CM|CM_MAX_GRF|XMX_IGC|INTC_ALT|VKD3D)' | sed 's/^/  /' || echo "  (none)"
    echo "backups:  $(ls "$bkp" 2>/dev/null | wc -l) file(s) in $bkp"
}

cmd_kernels() { # off|on
    case ${1:-} in
        off)
            [ -d "$kdir" ] || { echo "xess-ab: no kernel directory to move"; return 0; }
            mv "$kdir" "$kdir.off-$stamp" && echo "xess-ab: moved $(ls "$kdir.off-$stamp" | wc -l) kernels to $kdir.off-$stamp"
            ;;
        on)
            d=$(ls -dt "$kdir".off-* 2>/dev/null | head -1)
            [ -n "$d" ] || { echo "xess-ab: no moved kernel directory to restore" >&2; return 1; }
            mv "$d" "$kdir" && echo "xess-ab: restored $(ls "$kdir" | wc -l) kernels from $d"
            ;;
        *) die "usage: xess-ab.sh kernels off|on" ;;
    esac
}

cmd_shim() { # 20261029|20261120
    local v=${1:-} n src
    case $v in 20261029|20261120) ;; *) die "usage: xess-ab.sh shim 20261029|20261120" ;; esac
    for n in igdext64.dll ControlLib.dll; do
        src=$bkp/$n.$v
        [ -s "$src" ] || die "missing $src (extract it from the cached release zip)"
        cp -f "$src" "$pkg/$n"
        printf '  %-16s %s\n' "$n" "$(sha256sum "$pkg/$n" | cut -c1-16)"
    done
    echo "xess-ab: shim pair set to release $v (a game restart is enough)"
}

cmd_env() { # pkg|pkg-kernels|nodgrf-kernels
    local mode=${1:-} helper=pkg
    case $mode in
        pkg)            write_env <<EOF
ANV_CM_HELPER=$pkg/tools/cm-compile.sh
XMX_IGC_DIR=$pkg/igc
INTC_ALT_DRIVER_EXTENSIONS_PATH=$pkg
EOF
            ;;
        pkg-kernels)    write_env <<EOF
ANV_CM_KERNEL_DIR=$kdir
ANV_CM_HELPER=$pkg/tools/cm-compile.sh
XMX_IGC_DIR=$pkg/igc
INTC_ALT_DRIVER_EXTENSIONS_PATH=$pkg
EOF
            ;;
        pkg-nodgrf)     write_env <<EOF
ANV_CM_HELPER=$pkg/tools/cm-compile-nodgrf.sh
XMX_IGC_DIR=$pkg/igc
INTC_ALT_DRIVER_EXTENSIONS_PATH=$pkg
EOF
            ;;
        nodgrf-kernels) write_env <<EOF
ANV_CM_KERNEL_DIR=$kdir
ANV_CM_HELPER=$(cd "$(dirname "$0")/.." && pwd)/tools/xmx-cm-compile-nodgrf.sh
XMX_IGC_DIR=$pkg/igc
INTC_ALT_DRIVER_EXTENSIONS_PATH=$pkg
VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer
EOF
            ;;
        *) die "usage: xess-ab.sh env pkg|pkg-kernels|pkg-nodgrf|nodgrf-kernels" ;;
    esac
    echo "xess-ab: environment set to '$mode' (takes effect at the next Gamemode start)"
}

case ${1:-status} in
    status)          cmd_status ;;
    backup)          backup ;;
    kernels)         shift; cmd_kernels "$@" ;;
    shim)            shift; backup; cmd_shim "$@" ;;
    env)             shift; backup; cmd_env "$@" ;;
    *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
