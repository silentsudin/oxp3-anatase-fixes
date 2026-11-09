#!/bin/sh
# oxp3-vibrance - install/verify the OXP3 saturation shader and print the exact
# per-game launch settings for the OneXPlayer 3 on Anatase.
#
# Nothing here needs root and nothing is global: it writes files under
# ~/.local/share, ~/.config and ~/.cache only, and prints the environment for
# an individual game launch. See docs/hw/hdr-vibrance.md.
#
#   tools/display/vibrance.sh status              what is installed right now
#   tools/display/vibrance.sh install             shader + vkBasalt config
#   tools/display/vibrance.sh install --from-rpm  also unpack vkBasalt (no root)
#   tools/display/vibrance.sh test                compile the shader in gamescope
#   tools/display/vibrance.sh set 35 [-25]        bake uniform defaults (no UI)
#   tools/display/vibrance.sh launch-env          env/launch options to paste
#   tools/display/vibrance.sh uninstall           remove everything we wrote
#
# Exit codes: 0 ok, 1 failure, 2 usage.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 2
repo=$(CDPATH= cd -- "$repo/.." && pwd) || exit 2
fx_src="$repo/tools/display/oxp3-vibrance.fx"
conf_src="$repo/tools/display/vkbasalt/oxp3-vibrance.conf"

data_home=${XDG_DATA_HOME:-$HOME/.local/share}
conf_home=${XDG_CONFIG_HOME:-$HOME/.config}
cache_home=${XDG_CACHE_HOME:-$HOME/.cache}
gs_shader_dir="$data_home/gamescope/reshade/Shaders"
vk_shader_dir="$data_home/oxp3-vkbasalt/shader"
vk_layer_dir="$data_home/oxp3-vkbasalt/layer"
vk_conf="$conf_home/oxp3-vkbasalt.conf"
rpm_cache="$cache_home/oxp3-vkbasalt-rpm"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Locate a system vkBasalt installation (layer json + lib).
system_vkbasalt() {
    for d in /usr/lib64/vkBasalt /usr/lib/vkBasalt; do
        [ -f "$d/libvkbasalt.so" ] && { printf '%s\n' "$d"; return 0; }
    done
    return 1
}

local_vkbasalt() {
    [ -f "$vk_layer_dir/libvkbasalt.so" ] && { printf '%s\n' "$vk_layer_dir"; return 0; }
    return 1
}

cmd_status() {
    say "shader source : $fx_src ($([ -f "$fx_src" ] && wc -c <"$fx_src" || echo MISSING) bytes)"
    if [ -f "$gs_shader_dir/oxp3-vibrance.fx" ]; then
        say "gamescope     : installed in $gs_shader_dir"
    else
        say "gamescope     : not installed (run: $0 install)"
    fi
    if p=$(system_vkbasalt); then
        say "vkBasalt      : system install at $p"
        vk_state=system
    elif p=$(local_vkbasalt); then
        say "vkBasalt      : user install at $p"
        vk_state=local
    else
        say "vkBasalt      : not installed (repo package available - see the doc)"
        vk_state=none
    fi
    [ -f "$vk_conf" ] && say "vkBasalt cfg  : $vk_conf" || say "vkBasalt cfg  : not written"
    if pgrep -x gamescope >/dev/null 2>&1; then
        say "gamescope     : running (pid $(pgrep -x gamescope | tr '\n' ' '))"
    else
        say "gamescope     : not running (Plasma session)"
    fi
    if [ "$vk_state" = none ]; then
        say ""
        say "Vibrance is only reachable through gamescope's ReShade support right"
        say "now. To use vkBasalt instead:"
        say "  sudo rpm-ostree install vkBasalt && sudo systemctl reboot   (layered, survives updates)"
        say "  or, no root: $0 install --from-rpm"
    fi
}

cmd_install() {
    from_rpm=0
    for a in "$@"; do [ "$a" = "--from-rpm" ] && from_rpm=1; done

    [ -f "$fx_src" ] || { say "missing $fx_src" >&2; exit 1; }
    mkdir -p "$gs_shader_dir" "$vk_shader_dir" "$(dirname "$vk_conf")"
    install -m 0644 "$fx_src" "$gs_shader_dir/oxp3-vibrance.fx"
    install -m 0644 "$fx_src" "$vk_shader_dir/oxp3-vibrance.fx"
    say "installed shader:"
    say "  $gs_shader_dir/oxp3-vibrance.fx   (gamescope --reshade-effect)"
    say "  $vk_shader_dir/oxp3-vibrance.fx   (vkBasalt)"

    if [ "$from_rpm" = 1 ]; then
        unpack_vkbasalt
    fi

    layer=
    if layer=$(system_vkbasalt); then
        inc=/usr/share/vkBasalt/shader
    elif layer=$(local_vkbasalt); then
        inc="$vk_layer_dir"
    else
        layer=$vk_layer_dir
        inc=$vk_shader_dir
    fi
    sed -e "s|@FX@|$vk_shader_dir/oxp3-vibrance.fx|g" \
        -e "s|@INCLUDE@|$inc|g" "$conf_src" >"$vk_conf"
    say "wrote vkBasalt config: $vk_conf"
    say ""
    say "Per-game launch (Steam launch options), host Steam:"
    say "  ENABLE_VKBASALT=1 VK_ADD_IMPLICIT_LAYER_PATH='$layer' VKBASALT_CONFIG_FILE='$vk_conf' %command%"
}

unpack_vkbasalt() {
    have dnf5 || { say "dnf5 not found; install vkBasalt another way" >&2; return 1; }
    have rpm2cpio || { say "rpm2cpio not found" >&2; return 1; }
    have cpio || { say "cpio not found" >&2; return 1; }
    rm -rf "$rpm_cache"
    mkdir -p "$rpm_cache"
    say "downloading vkBasalt rpm (no root needed) ..."
    dnf5 download --destdir "$rpm_cache" vkBasalt >/dev/null
    rpm=$(ls "$rpm_cache"/vkBasalt-*.x86_64.rpm 2>/dev/null | head -1)
    if [ -z "$rpm" ]; then rpm=$(ls "$rpm_cache"/vkBasalt-*.rpm 2>/dev/null | head -1); fi
    [ -n "$rpm" ] || { say "no vkBasalt rpm downloaded" >&2; return 1; }
    rm -rf "$vk_layer_dir"
    mkdir -p "$vk_layer_dir"
    ( cd "$vk_layer_dir" && rpm2cpio "$rpm" | cpio -idm --quiet --no-absolute-filenames )
    libdir="$vk_layer_dir/usr/lib64"
    [ -d "$libdir" ] || libdir="$vk_layer_dir/usr/lib"
    if [ ! -f "$libdir/vkBasalt/libvkbasalt.so" ]; then
        say "unexpected rpm layout under $vk_layer_dir" >&2
        return 1
    fi
    # Flat layout (json + .so side by side) so VK_ADD_IMPLICIT_LAYER_PATH works.
    cp -f "$libdir/vkBasalt/"* "$vk_layer_dir/" 2>/dev/null || true
    say "unpacked vkBasalt into $vk_layer_dir"
    say "  library: $vk_layer_dir/libvkbasalt.so"
}

cmd_test() {
    [ -f "$gs_shader_dir/oxp3-vibrance.fx" ] || { say "run '$0 install' first" >&2; exit 1; }
    have gamescope || { say "gamescope not installed" >&2; exit 1; }
    if ! have glxgears; then
        say "glxgears not found; cannot drive a frame through gamescope" >&2
        exit 1
    fi
    say "compiling the shader through gamescope (nested window, ~10 s) ..."
    log=$(mktemp)
    # A client must present a frame before gamescope compiles the effect.
    timeout 15 gamescope -W 480 -H 320 -w 480 -h 320 \
        --reshade-effect oxp3-vibrance.fx -- glxgears -geometry 240x160 >"$log" 2>&1 || true
    if grep -q 'Failed to parse' "$log"; then
        say "FAIL:"
        grep 'Failed to parse' "$log" | head -3
        rm -f "$log"
        return 1
    fi
    grep -q 'Using technique: Oxp3Vibrance' "$log" \
        || { say "shader parsed but our technique was not selected:"; grep -i reshade "$log" | head -5; rm -f "$log"; return 1; }
    say "OK: gamescope parsed the shader, selected technique Oxp3Vibrance and compiled the pass"
    rm -f "$log"
}

# vkBasalt and gamescope's ReShade pass both have no in-game uniform editor, so
# the value a game runs with is the uniform default inside the .fx.  Rewrite it
# in the installed copies only (the repo source stays neutral).
cmd_set() {
    [ $# -ge 1 ] || die "usage: $0 set <vibrance -100..100> [saturation -100..100]"
    vib=$1
    sat=${2:-}
    for v in "$vib" ${sat:-}; do
        case $v in ''|*[!0-9-]*) die "values must be integers in -100..100" ;; esac
        [ "$v" -ge -100 ] && [ "$v" -le 100 ] || die "values must be integers in -100..100"
    done
    for f in "$gs_shader_dir/oxp3-vibrance.fx" "$vk_shader_dir/oxp3-vibrance.fx"; do
        [ -f "$f" ] || continue
        awk -v vib="$vib" -v sat="$sat" '
            /^uniform float (Vibrance|Saturation) </ { want = ($3 == "Vibrance") ? "vib" : "sat"; print; next }
            want != "" && /^> = / {
                if (want == "vib" && vib != "") print "> = " vib ".0;";
                else if (want == "sat" && sat != "") print "> = " sat ".0;";
                else print;
                want = "";
                next
            }
            { print }
        ' "$f" >"$f.new" && mv "$f.new" "$f"
        say "set defaults in $f: Vibrance=${vib}${sat:+ Saturation=$sat}"
    done
}

cmd_launch_env() {
    layer=
    if layer=$(system_vkbasalt); then :; elif layer=$(local_vkbasalt); then :; else layer=$vk_layer_dir; fi
    say "# vkBasalt (host Steam / Heroic / direct Proton):"
    say "export ENABLE_VKBASALT=1"
    say "export VK_ADD_IMPLICIT_LAYER_PATH='$layer'"
    say "export VKBASALT_CONFIG_FILE='$vk_conf'"
    say ""
    say "# Flatpak Steam: the sandbox needs the layer and the shader visible, e.g."
    say "flatpak override --user --filesystem='$data_home/oxp3-vkbasalt:ro' org.anatase.Steam"
    say "flatpak override --user --env=ENABLE_VKBASALT=1 org.anatase.Steam"
    say "flatpak override --user --env=VK_ADD_IMPLICIT_LAYER_PATH='$layer' org.anatase.Steam"
    say "flatpak override --user --env=VKBASALT_CONFIG_FILE='$vk_conf' org.anatase.Steam"
    say ""
    say "# gamescope alternative (Steam Gamemode or a nested session):"
    say "gamescope --reshade-effect oxp3-vibrance.fx ..."
}

cmd_uninstall() {
    rm -f "$gs_shader_dir/oxp3-vibrance.fx" "$vk_shader_dir/oxp3-vibrance.fx" "$vk_conf"
    rmdir "$vk_shader_dir" 2>/dev/null || true
    rm -rf "$vk_layer_dir" "$rpm_cache"
    say "removed the shader, the vkBasalt config and any unpacked vkBasalt copy"
    say "(a system vkBasalt from rpm-ostree is untouched)"
}

[ $# -ge 1 ] || { sed -n '3,15p' "$0"; exit 2; }
sub=$1
shift
case $sub in
    status)     cmd_status ;;
    install)    cmd_install "$@" ;;
    test)       cmd_test ;;
    set)        cmd_set "$@" ;;
    launch-env) cmd_launch_env ;;
    uninstall)  cmd_uninstall ;;
    *)          say "unknown subcommand: $sub" >&2; exit 2 ;;
esac
