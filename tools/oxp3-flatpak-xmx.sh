#!/bin/bash
# SPDX-License-Identifier: MIT
# oxp3-flatpak-xmx.sh - make XeSS XMX work for games started by the *Flatpak* Steam, with no filesystem
# expansion of the sandbox and no change to the Flatpak installation itself.
#
# The gap this fills: the project's XMX wiring (tools/xmx-enable.sh, the release install.sh, the host
# environment.d file and the Flatpak override that install-common.sh writes) puts the driver *environment* in
# place but never puts the shim igdext64.dll into a Proton prefix that a Flatpak game uses, and it never sets
# VK_DRIVER_FILES for the sandbox (the host's environment.d is not read inside the sandbox). A Flatpak game
# therefore runs the stock Mesa with no CM injection and no shim -> DP4a, even though everything else is set.
#
# What this script does, all in paths owned by the user:
#   1. copies the shim igdext64.dll into the game's Proton prefix DriverStore (backup: igdext64.dll.stock),
#      which is where XeSS looks when INTC_ALT_DRIVER_EXTENSIONS_PATH names it, plus ControlLib.dll into
#      system32 (the XESS_FG stand-in). Optionally also into the Flatpak Proton's own igdext folder (-m proton),
#      which only affects prefixes created *afterwards*.
#   2. writes ~/.config/xess-xmx/icd-flatpak.json (a separate ICD file; the host's icd-xmx.json is not touched)
#      and ~/.config/xess-xmx/flatpak-env.sh
#   3. prints the Steam launch options string that carries the environment into the game process. Steam launch
#      options are the documented Valve mechanism and need no override and no Flatpak permission.
#
# It never writes outside $HOME, never touches /var/lib/flatpak, /app, the app's metadata, its .desktop files or
# any Flatpak override, and never touches a Proton it does not own. --dry-run prints every action first.
#
# Usage: oxp3-flatpak-xmx.sh <command> [options]
#   status          what the Flatpak Steam environment has, what is installed, whether it is sandbox-visible
#   install         deploy the shim for one game prefix and write the env/ICD files
#   remove          undo install (restores the .stock backups, removes our files only)
#   launch-options  print the exact Steam launch options line for this host
#
# Options:
#   --appid <id>       Steam app id of the game (required by install/remove; status shows what it finds)
#   --app <flatpak>    Flatpak app id (default: auto - org.anatase.Steam, then com.valvesoftware.Steam)
#   --package <dir>    xess-xmx-linux package tree (default: newest ~/.local/share/xess-xmx-release/xess-xmx-linux-*)
#   --driver-so <so>   Vulkan driver for the ICD (default: the one ~/.config/xess-xmx/icd-xmx.json uses)
#   --mode <m>         prefix (default) | proton | both - where the shim DLL is copied
#   --dry-run          print actions, change nothing
#   --probe-sandbox    status: also start `flatpak run --command=sh` and check path visibility/executability
#   --json             status: machine-readable output
#   -h, --help         this text
#
# Environment: OXP3_FLATPAK_DATA (test fixture: use this data dir instead of ~/.var/app/<app>),
#              OXP3_PROBE_TIMEOUT (seconds for the sandbox probe, default 120).
set -eu

PROG=oxp3-flatpak-xmx
VERSION=1.0.0
DRY=0
JSON=0
PROBE=0
APPID=
APPUSER=
PKG=
DRIVER=
MODE=prefix
CMD=

say() { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   WARN %s\n' "$*" >&2; }
die() { printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }

# the leading comment block only (never the code that follows it)
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

while [ $# -gt 0 ]; do
    case "$1" in
        status|install|remove|launch-options) CMD=$1; shift ;;
        --appid) APPID=${2:-}; shift 2 ;;
        --app) APPUSER=${2:-}; shift 2 ;;
        --package) PKG=${2:-}; shift 2 ;;
        --driver-so) DRIVER=${2:-}; shift 2 ;;
        --mode) MODE=${2:-}; shift 2 ;;
        --dry-run|-n) DRY=1; shift ;;
        --probe-sandbox) PROBE=1; shift ;;
        --json) JSON=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done
[ -n "$CMD" ] || { usage; exit 2; }

HOMEDIR=${HOME:?}
CACHE=${XDG_CACHE_HOME:-$HOMEDIR/.cache}
XCFG=${XDG_CONFIG_HOME:-$HOMEDIR/.config}/xess-xmx
ICD=$XCFG/icd-flatpak.json
ENVF=$XCFG/flatpak-env.sh
STATE=$XCFG/flatpak-install.tsv
KERNELS=$CACHE/xess-xmx/kernels
LOG=$CACHE/xess-xmx/compile.log

# ---------------------------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------------------------
[ "$(id -u)" = 0 ] && die "refusing to run as root: this tool only ever writes files the user owns"

# a write path must be under $HOME and must not be inside the Flatpak installation
assert_owned_path() {
    local p=$1 real parent
    case "$p" in
        "$HOMEDIR"/*) ;;
        *) die "refusing to write outside \$HOME: $p" ;;
    esac
    case "$p" in
        # the Flatpak installation itself (never ours); ~/.var/app/<id> is the app's *data* dir and is fine
        /var/lib/flatpak/*|/usr/*|/app/*|/etc/*)
            die "refusing to write inside the Flatpak installation: $p" ;;
        "$HOMEDIR"/.local/share/flatpak/app/*|"$HOMEDIR"/.local/share/flatpak/runtime/*|"$HOMEDIR"/.local/share/flatpak/overrides/*)
            die "refusing to write inside the Flatpak installation: $p" ;;
        *.desktop)
            die "refusing to modify a .desktop file: $p" ;;
    esac
    parent=$(dirname "$p")
    # a dry run must not create anything, not even a directory
    if [ "$DRY" = 1 ]; then
        [ -d "$parent" ] || return 0
    else
        mkdir -p "$parent" 2>/dev/null || die "cannot create $parent"
    fi
    real=$(readlink -f "$parent") || die "cannot resolve $parent"
    case "$real" in
        "$HOMEDIR"/*) ;;
        *) die "refusing to write through a path that resolves outside \$HOME: $real" ;;
    esac
}

run() {
    if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi
}

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

# ---------------------------------------------------------------------------------------------
# platform discovery
# ---------------------------------------------------------------------------------------------
detect_app() {
    if [ -n "$APPUSER" ]; then echo "$APPUSER"; return; fi
    local a
    for a in org.anatase.Steam com.valvesoftware.Steam; do
        if [ -d "$HOMEDIR/.var/app/$a" ] || flatpak info "$a" >/dev/null 2>&1; then echo "$a"; return; fi
    done
    echo ""
}

APP=$(detect_app)
[ -n "$APP" ] || die "no Flatpak Steam found (looked for org.anatase.Steam and com.valvesoftware.Steam)"

if [ -n "${OXP3_FLATPAK_DATA:-}" ]; then
    DATA=$OXP3_FLATPAK_DATA
else
    DATA=$HOMEDIR/.var/app/$APP
fi
STEAM=$DATA/data/Steam
# Steam's own library list: Proton and the prefixes of a game on another drive live there
LIBRARIES=$(printf '%s\n' "$STEAM")

detect_pkg() {
    if [ -n "$PKG" ]; then echo "$PKG"; return; fi
    # priority: the installed release tree (the known-good XMX driver's package), then the checkout's own
    # package, then a downloaded archive - newest first inside each group, and only a tree that carries the
    # shim *and* a runnable compiler, since a package without ocloc compiles no kernel and XeSS falls back.
    local d
    for d in $(ls -1dt "$HOMEDIR"/.local/share/xess-xmx-release/xess-xmx-linux-* 2>/dev/null) \
             "$HOMEDIR/.local/share/xess-xmx" \
             $(ls -1dt "$HOMEDIR"/Downloads/xess-xmx-linux-* 2>/dev/null); do
        [ -d "$d" ] && [ -f "$d/igdext64.dll" ] || continue
        ls "$d"/igc/neo/bin/ocloc* >/dev/null 2>&1 || continue
        echo "$d"; return
    done
    # nothing complete: fall back to a shim-bearing tree so status can still show what is missing
    for d in $(ls -1dt "$HOMEDIR"/.local/share/xess-xmx-release/xess-xmx-linux-* "$HOMEDIR"/Downloads/xess-xmx-linux-* 2>/dev/null); do
        [ -f "$d/igdext64.dll" ] && { echo "$d"; return; }
    done
    echo ""
}

PKGDIR=$(detect_pkg)

# the driver: what the live host ICD selects, else the package's own indirection
detect_driver() {
    if [ -n "$DRIVER" ]; then echo "$DRIVER"; return; fi
    local j=$XCFG/icd-xmx.json so
    if [ -f "$j" ]; then
        so=$(sed -n 's/.*"library_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$j" | head -1)
        [ -n "$so" ] && [ -f "$so" ] && { echo "$so"; return; }
    fi
    [ -n "$PKGDIR" ] && [ -f "$PKGDIR/lib/libvulkan_intel.so" ] && { echo "$PKGDIR/lib/libvulkan_intel.so"; return; }
    echo ""
}

DRIVERSO=$(detect_driver)

helper_path() {
    local h=$HOMEDIR/git/linux_scratch/oxp3-anatase-fixes/tools/xmx-cm-compile-nodgrf.sh
    [ -x "$h" ] && { echo "$h"; return; }
    [ -n "$PKGDIR" ] && [ -f "$PKGDIR/tools/cm-compile.sh" ] && { echo "$PKGDIR/tools/cm-compile.sh"; return; }
    echo ""
}
HELPER=$(helper_path)

igc_dir() {
    local d
    for d in ${PKGDIR:+"$PKGDIR/igc"} "$HOMEDIR/.local/share/xess-xmx/igc"; do
        [ -n "$d" ] && [ -d "$d" ] || continue
        # ocloc* because the release ships ocloc-25.18.1, not ocloc
        if ls "$d"/neo/bin/ocloc* >/dev/null 2>&1; then echo "$d"; return; fi
    done
    echo ""
}
IGC=$(igc_dir)

# ---------------------------------------------------------------------------------------------
# sandbox visibility (the question this task exists for)
# ---------------------------------------------------------------------------------------------
# can the sandbox see $HOME at all? Every XMX path we rely on is below $HOME, so this single permission
# decides feasibility without any override.
sandbox_has_home() {
    local perms
    perms=$(flatpak info --show-permissions "$APP" 2>/dev/null || true)
    [ -n "$perms" ] || { echo "unknown"; return; }
    case "$perms" in
        *"filesystems="*"home"*) echo "yes" ;;
        *) echo "no" ;;
    esac
}

# the exact set of paths a launch needs, in this order
needed_paths() {
    printf '%s\t%s\n' "$ICD" "ICD json"
    [ -n "$DRIVERSO" ] && printf '%s\t%s\n' "$DRIVERSO" "driver .so"
    [ -n "$PKGDIR" ] && printf '%s\t%s\n' "$PKGDIR/igdext64.dll" "shim igdext64.dll"
    [ -n "$HELPER" ] && printf '%s\t%s\n' "$HELPER" "CM compile helper"
    [ -n "$IGC" ] && printf '%s\t%s\n' "$IGC/neo/bin" "IGC ocloc dir"
    printf '%s\t%s\n' "$KERNELS" "kernel cache (w)"
    return 0
}

probe_sandbox() {
    local script=""
    local p label
    say "sandbox probe ($APP): flatpak run --command=sh"
    if [ "$(sandbox_has_home)" != yes ]; then
        warn "the app has no 'home' filesystem permission - every path below is unreachable without an override"
    fi
    while IFS=$'\t' read -r p label; do
        case "$label" in
            *"(w)") script="$script
if [ -w \"$p\" ]; then echo 'WRITABLE $p'; else echo 'NOT-WRITABLE $p'; fi" ;;
            *) script="$script
if [ -r \"$p\" ]; then echo 'VISIBLE  $p'; else echo 'MISSING  $p'; fi" ;;
        esac
    done <<EOF
$(needed_paths)
EOF
    # the PCI id the CM helper detects (it falls back to 'ptl' when /sys is unreadable)
    script="$script
d=\$(cat /sys/class/drm/renderD*/device/device 2>/dev/null | head -1); echo \"PCI \${d:-unavailable}\"
"
    # ocloc must *run* against the runtime's own libc, not merely exist: this is the decisive check
    script="$script
O=\$(ls ${IGC:-/nonexistent}/neo/bin/ocloc* 2>/dev/null | head -1)
if [ -n \"\$O\" ]; then LD_LIBRARY_PATH=${IGC:-/nonexistent}/igc/local/lib:${IGC:-/nonexistent}/neo/lib/x86_64-linux-gnu \"\$O\" >/dev/null 2>&1; r=\$?; case \$r in 0|1) echo 'OCLOC-RUNS ok' ;; *) echo \"OCLOC-RUNS failed (exit \$r)\" ;; esac; else echo 'OCLOC-RUNS no-ocloc'; fi
"
    if [ "$DRY" = 1 ]; then
        printf '   [dry-run] flatpak run --command=sh %s -c <%d probes>\n' "$APP" "$(printf '%s\n' "$script" | wc -l)"
        return 0
    fi
    timeout "${OXP3_PROBE_TIMEOUT:-120}" flatpak run --command=sh "$APP" -c "$script" 2>&1 | sed 's/^/   /'
}

# ---------------------------------------------------------------------------------------------
# prefixes and Protons of the Flatpak Steam
# ---------------------------------------------------------------------------------------------
find_prefixes() {
    local dir pf
    for dir in $LIBRARIES; do
        for pf in "$dir"/steamapps/compatdata/*/pfx; do
            [ -d "$pf/drive_c" ] && printf '%s\n' "$pf"
        done
    done
    return 0
}

prefix_of_appid() {
    local dir pf
    for dir in $LIBRARIES; do
        pf=$dir/steamapps/compatdata/$1/pfx
        [ -d "$pf/drive_c" ] && { echo "$pf"; return 0; }
    done
    return 1
}

find_flatpak_protons() {
    local p
    for p in "$STEAM"/steamapps/common/Proton* "$STEAM"/compatibilitytools.d/*; do
        [ -f "$p/proton" ] && printf '%s\n' "$p"
    done
    return 0
}

# Proton keeps its own igdext copy, which it installs into a prefix's DriverStore *when that prefix is created*
proton_igdext_dir() { echo "$1/files/lib/wine/igdext/x86_64-windows"; }

# the DriverStore folder that holds (or must hold) the shim: the ones this prefix already has, else the
# conventional igd_faux.inf_1 name that the release enable.sh uses
prefix_shim_dirs() {
    local pfx=$1 s32=$1/drive_c/windows/system32 d found=
    [ -d "$s32" ] || return 0
    for d in "$s32"/driverstore*; do
        [ -d "$d" ] || continue
        for found in "$d"/filerepository/*; do
            [ -d "$found" ] || continue
            case "${found##*/}" in *[Ii][Gg][Dd]*) printf '%s\n' "$found" ;; esac
        done
    done
    return 0
}

# record what we put where, so remove can restore exactly that (never in --dry-run, which writes nothing)
record_install() {
    [ "$DRY" = 1 ] && return 0
    local p=$1 s
    s=$(sha "$p") || return 0
    printf 'install\t%s\t%s\n' "$p" "$s" >> "$STATE"
}

# a deployment step's outcome, phrased for a dry run as well
did() { if [ "$DRY" = 1 ]; then printf '   would %s\n' "$*"; else printf '   %s\n' "$*"; fi; }

# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
cmd_status() {
    local home_perm protos=() pfx
    if [ "$JSON" = 1 ]; then
        :
    else
        say "$PROG $VERSION"
        info "Flatpak app : $APP"
        info "data dir    : $DATA"
        info "flatpak pkg : ${PKGDIR:-NOT FOUND}"
        info "driver .so  : ${DRIVERSO:-NOT FOUND}"
        info "helper      : ${HELPER:-NOT FOUND}"
        info "IGC (ocloc) : ${IGC:-NOT FOUND}"
        info "kernel cache: $KERNELS ($(ls "$KERNELS" 2>/dev/null | wc -l) kernels)"
        info "trace log   : $LOG"
    fi
    home_perm=$(sandbox_has_home)
    [ "$JSON" = 1 ] || info "sandbox home: $home_perm"

    [ "$JSON" = 1 ] || say "Flatpak Proton installs"
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        protos+=("$p")
        local d shimstate=stock
        d=$(proton_igdext_dir "$p")
        if [ -f "$d/igdext64.dll" ]; then
            if grep -aq 'IGDEXT_' "$d/igdext64.dll" 2>/dev/null; then shimstate=shim; fi
        else
            shimstate=absent
        fi
        [ "$JSON" = 1 ] || info "${p##*/}: $shimstate"
    done <<EOF
$(find_flatpak_protons)
EOF
    [ ${#protos[@]} -eq 0 ] && { [ "$JSON" = 1 ] || info "none - Flatpak Steam has no Proton installed (never launched a Proton game)"; }

    [ "$JSON" = 1 ] || say "Game prefixes and shim state"
    local n=0
    while IFS= read -r pfx; do
        [ -n "$pfx" ] || continue
        n=$((n + 1))
        local appid dirs=() d state=missing
        appid=$(basename "$(dirname "$pfx")")
        while IFS= read -r d; do [ -n "$d" ] && dirs+=("$d"); done <<EOF
$(prefix_shim_dirs "$pfx")
EOF
        local i
        for i in "${dirs[@]}"; do
            if [ -f "$i/igdext64.dll" ]; then
                if grep -aq 'IGDEXT_' "$i/igdext64.dll" 2>/dev/null; then state=shim; else state=foreign; fi
            fi
        done
        [ "$JSON" = 1 ] || info "appid $appid: $state ($pfx)"
        # a trace only proves the shim loaded when it is newer than the prefix's last launch
        local tr=$pfx/drive_c/igdext_trace.log
        if [ -f "$tr" ]; then
            [ "$JSON" = 1 ] || info "  trace: $(stat -c '%y' "$tr" | cut -c1-19) $(wc -l < "$tr") lines"
        fi
    done <<EOF
$(find_prefixes)
EOF
    [ "$n" -eq 0 ] && { [ "$JSON" = 1 ] || info "none - no Proton prefix exists in the Flatpak Steam data dir"; }

    [ "$JSON" = 1 ] || say "Our deployment"
    if [ -f "$ENVF" ]; then
        [ "$JSON" = 1 ] || info "env file: $ENVF"
    else
        [ "$JSON" = 1 ] || info "env file: not written (run install)"
    fi
    [ -f "$ICD" ] && { [ "$JSON" = 1 ] || info "ICD: $ICD"; } || { [ "$JSON" = 1 ] || info "ICD: not written (run install)"; }
    if [ "$JSON" = 1 ]; then
        cat <<JSON
{"app":"$APP","data_dir":"$DATA","package":"$PKGDIR","driver":"$DRIVERSO","helper":"$HELPER","igc":"$IGC",
 "kernels":$(ls "$KERNELS" 2>/dev/null | wc -l),"sandbox_home":"$home_perm",
 "flatpak_protons":${#protos[@]},"prefixes":$n,
 "env_file":$([ -f "$ENVF" ] && echo true || echo false),"icd":$([ -f "$ICD" ] && echo true || echo false)}
JSON
    fi

    [ "$PROBE" = 1 ] && probe_sandbox
    return 0
}

write_env_files() {
    assert_owned_path "$ICD"
    assert_owned_path "$ENVF"
    local icd_body
    icd_body=$(printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s","api_version":"1.4.0"}}\n' "$DRIVERSO")
    if [ "$DRY" = 1 ]; then
        printf '   [dry-run] write %s\n' "$ICD"
        printf '   [dry-run] write %s\n' "$ENVF"
        return 0
    fi
    printf '%s' "$icd_body" > "$ICD"
    mkdir -p "$KERNELS"
    {
        echo "# Generated by $PROG $VERSION - the environment a Flatpak Steam game needs for XeSS XMX."
        echo "# Steam does not source this file; the launch options printed by '$PROG launch-options' carry the"
        echo "# same variables. Absolute paths: inside the sandbox \$HOME is the same directory as on the host."
        printf 'export VK_DRIVER_FILES=%s\n' "$ICD"
        printf 'export ANV_CM_KERNEL_DIR=%s\n' "$KERNELS"
        printf 'export ANV_CM_HELPER=%s\n' "$HELPER"
        printf 'export XMX_IGC_DIR=%s\n' "$IGC"
        echo  'export VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_EXT_descriptor_heap'
        echo  'export force_vk_vendor=0'
        # a Windows path: XeSS resolves INTC_ALT_DRIVER_EXTENSIONS_PATH inside the Wine prefix. Forward slashes
        # so the value survives Steam's launch-option parsing.
        echo  'export INTC_ALT_DRIVER_EXTENSIONS_PATH=C:/windows/system32/driverstore/filerepository/igd_faux.inf_1'
    } > "$ENVF"
    return 0
}

cmd_install() {
    [ -n "$APPID" ] || die "--appid <Steam app id> is required for install"
    [ -n "$PKGDIR" ] || die "no xess-xmx-linux package found; set --package <dir>"
    [ -f "$PKGDIR/igdext64.dll" ] || die "missing $PKGDIR/igdext64.dll"
    [ -n "$DRIVERSO" ] || die "no Vulkan driver found; set --driver-so <libvulkan_intel.so>"
    [ -f "$DRIVERSO" ] || die "driver does not exist: $DRIVERSO"
    [ -n "$HELPER" ] || die "no CM compile helper found"
    [ -n "$IGC" ] || die "no IGC/ocloc found: XMX cannot compile kernels without it (run tools/get-igc.sh)"
    case "$MODE" in prefix|proton|both) ;; *) die "--mode must be prefix, proton or both" ;; esac

    local pfx
    pfx=$(prefix_of_appid "$APPID") || die "no prefix for appid $APPID under $STEAM/steamapps/compatdata (start the game once)"
    info "prefix: $pfx"

    local shimsha; shimsha=$(sha "$PKGDIR/igdext64.dll")
    [ "$DRY" = 1 ] || mkdir -p "$XCFG"

    # 1. the shim in the prefix DriverStore (this is what XeSS actually finds)
    if [ "$MODE" = prefix ] || [ "$MODE" = both ]; then
        local dirs=() d
        while IFS= read -r d; do [ -n "$d" ] && dirs+=("$d"); done <<EOF
$(prefix_shim_dirs "$pfx")
EOF
        if [ ${#dirs[@]} -eq 0 ]; then
            dirs=("$pfx/drive_c/windows/system32/driverstore/filerepository/igd_faux.inf_1")
            info "no DriverStore igd folder in the prefix; creating igd_faux.inf_1 (what the release enable.sh uses)"
        fi
        for d in "${dirs[@]}"; do
            assert_owned_path "$d/"
            if [ -f "$d/igdext64.dll" ] && [ "$(sha "$d/igdext64.dll")" = "$shimsha" ]; then
                info "shim already current: $d"
                continue
            fi
            if [ -f "$d/igdext64.dll" ] && [ ! -f "$d/igdext64.dll.stock" ]; then
                run cp -a "$d/igdext64.dll" "$d/igdext64.dll.stock"
                did "back up the original as igdext64.dll.stock"
            fi
            run cp -a "$PKGDIR/igdext64.dll" "$d/igdext64.dll"
            record_install "$d/igdext64.dll"
            did "install shim: $d/igdext64.dll"
        done
        # ControlLib: the XESS_FG override stand-in (only when it is ours or absent)
        local s32=$pfx/drive_c/windows/system32
        if [ -f "$PKGDIR/ControlLib.dll" ] && [ -d "$s32" ]; then
            if [ ! -f "$s32/ControlLib.dll" ] || grep -aq 'XESS_FG' "$s32/ControlLib.dll" 2>/dev/null; then
                assert_owned_path "$s32/ControlLib.dll"
                run cp -a "$PKGDIR/ControlLib.dll" "$s32/ControlLib.dll"
                record_install "$s32/ControlLib.dll"
                did "install ControlLib: $s32/ControlLib.dll"
            else
                warn "$s32/ControlLib.dll is not ours - left alone (XESS_FG override will not work there)"
            fi
        fi
    fi

    # 2. optionally into the Flatpak Proton itself (only new prefixes pick it up)
    if [ "$MODE" = proton ] || [ "$MODE" = both ]; then
        local p d
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            d=$(proton_igdext_dir "$p")
            assert_owned_path "$d/"
            [ -f "$d/igdext64.dll" ] && [ ! -f "$d/igdext64.dll.stock" ] && run cp -a "$d/igdext64.dll" "$d/igdext64.dll.stock"
            run cp -a "$PKGDIR/igdext64.dll" "$d/igdext64.dll"
            record_install "$d/igdext64.dll"
            did "install shim into Proton ${p##*/}"
        done <<EOF
$(find_flatpak_protons)
EOF
    fi

    # 3. env + ICD
    write_env_files
    say "launch options for appid $APPID (Steam -> Properties -> General -> Launch options)"
    cmd_launch_options
    printf '\n'
    info "verify: run the game, then look at $pfx/drive_c/igdext_trace.log (needs XESS_LOG=1, included above)"
    return 0
}

cmd_remove() {
    [ -f "$STATE" ] || { info "nothing recorded in $STATE - nothing to remove"; return 0; }
    local kind path
    # reverse order: the files we installed, restored from .stock when there is one
    tac "$STATE" | while IFS=$'\t' read -r kind path ssha; do
        [ "$kind" = install ] || continue
        [ -n "$path" ] || continue
        case "$path" in "$HOMEDIR"/*) ;; *) warn "skip path outside \$HOME: $path"; continue ;; esac
        [ -f "$path" ] || continue
        if [ -f "$path.stock" ]; then
            run mv -f "$path.stock" "$path"
            did "restore $path"
        else
            run rm -f "$path"
            did "remove $path"
        fi
    done
    [ "$DRY" = 1 ] || rm -f "$STATE"
    if [ "$DRY" = 1 ]; then
        printf '   [dry-run] remove %s %s\n' "$STATE" "$ENVF"
    else
        rm -f "$ENVF"
        info "removed $ENVF (the ICD $ICD is kept: it is inert without VK_DRIVER_FILES)"
    fi
    return 0
}

cmd_launch_options() {
    [ -n "$DRIVERSO" ] || { warn "no driver resolved"; return 1; }
    [ -n "$HELPER" ] || { warn "no helper resolved"; return 1; }
    [ -n "$IGC" ] || { warn "no IGC resolved"; return 1; }
    [ -n "$APPID" ] || [ -f "$ICD" ] || { warn "run install once (or pass --appid) so the ICD exists"; }
    printf '%s\n' "XESS_LOG=1 VK_DRIVER_FILES=$ICD ANV_CM_KERNEL_DIR=$KERNELS ANV_CM_HELPER=$HELPER XMX_IGC_DIR=$IGC VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_EXT_descriptor_heap force_vk_vendor=0 INTC_ALT_DRIVER_EXTENSIONS_PATH=C:/windows/system32/driverstore/filerepository/igd_faux.inf_1 %command%"
    return 0
}

case "$CMD" in
    status) cmd_status ;;
    install) cmd_install ;;
    remove) cmd_remove ;;
    launch-options) cmd_launch_options ;;
    *) die "unknown command: $CMD" ;;
esac
