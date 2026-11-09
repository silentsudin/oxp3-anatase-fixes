#!/bin/bash
# oxp3-hw-install.sh - one entry point for every OneXPlayer 3 hardware fix in this repo.
#
# Each component keeps its own installer/tool; this script only orchestrates them, so a single
# command installs (or removes) the whole hardware set coherently:
#
#   tools/oxp3-hw-install.sh --all             install everything (root; run it with sudo)
#   tools/oxp3-hw-install.sh --volume --leds   pick components (root)
#   tools/oxp3-hw-install.sh --audio           user scope only (no root needed)
#   tools/oxp3-hw-install.sh --uninstall --all remove everything
#   tools/oxp3-hw-install.sh --status          read-only state report
#   tools/oxp3-hw-install.sh --dry-run --all   print every command, change nothing
#
# Components and their implementation:
#   volume     tools/hhd-oxp3-voltap/install.sh      (hhd plugin + force-release hwdb;
#              the default.  `--volume-daemon` selects the legacy oxp3-voltap
#              daemon instead: tools/oxp3-voltap/install.sh, daemon + hwdb + unit.
#              A plugin change only takes effect after `systemctl restart hhd`,
#              which drops the emulated Steam controller for a few seconds.)
#   leds       this script: udev rule + user unit     (tools/oxp3-homebtn.py, oxp3-led.py)
#   sleep      this script: files/oxp3-sleep-* units, tools/oxp3-sleep-*, plus the
#              trigger policy files/logind-oxp3.conf -> /etc/systemd/logind.conf.d/ and
#              files/49-oxp3-suspend.rules -> /etc/polkit-1/rules.d/ (so sleep does not
#              depend on a desktop session asking for it)
#   bluetooth  tools/oxp3-bt-firmware.sh             (upstream firmware into updates/ + initramfs)
#   power      tools/oxp3-power-install.sh           (oxp3-powerctl, boot/resume, fan, adaptive TDP)
#   rgb        this script: shared udev rule + user config/unit (tools/oxp3-rgbd.py)
#   imu        this script: files/oxp3-bmi160.conf   (keeps the BMI260 driver bound)
#   audio      tools/audio/install-user.sh           (speaker EQ, user scope)
#   decky      this script: verified plugin zip -> ~/homebrew/plugins/oxp3-status
#              (tools/lib/decky_plugin.py; backs up an existing copy first and
#              restarts plugin_loader.service only when asked)
#
# Root model: the hardware components write to /etc, /usr/local/libexec and /sys, so they need
# root.  The user-scope halves (home-button unit, RGB keeper, speaker EQ, status) are always run
# as the *invoking* user - the one who called sudo - via runuser, never as root.  The Decky
# plugin directory is root-owned because Decky's plugin_loader runs as root, so --decky also
# needs root.  A dry run and --status never need root.
#
# Safety: no vendor blobs, no kernel or EC pokes beyond what each component's own installer does,
# idempotent (re-running changes nothing it already applied), and --root=DIR installs everything
# into a scratch prefix for offline validation.
#
# Before a real install it runs tools/oxp3-preflight.sh for the selected components: a FAIL
# aborts (the host would get a half-install), a WARN is printed and the install continues.
# --force overrides the abort; re-read docs/PREFLIGHT.md before using it.
#
# Docs: docs/INSTALL-HW.md (single install guide), docs/PREFLIGHT.md (prerequisites),
# docs/hw/README.md (state matrix).
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)

PREFIX=""
DRY_RUN=0
RGB_DAEMON=0
ACTION=install
PURGE_CONFIG=0
FORCE=0
AUDIO_ACTIVATE=0
IMU_RELOAD=0
POWER_PROFILE=""
BATTERY_LIMIT=""
BATTERY_BYPASS=""
BT_ARGS=(--persist=items)
POWER_ARGS=()
DECKY_ARTIFACT=""
DECKY_RESTART=0
VOLUME_MODE=plugin
SELECTED=()
TARGET_USER=""
ORIG_ARGS=("$@")
SUDO_SHIM_DIR=""

# fixed component order: root-heavy first, user scope last
ALL_COMPONENTS=(volume sleep bluetooth power imu leds rgb audio sched gfx xmx mango decky)

say() { printf '\n== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    awk 'NR == 1 { next } /^set -euo pipefail/ { exit } /^#/ { sub(/^# ?/, ""); print }' "$0"
    cat <<'EOF'

Options:
  --all                     every component (volume sleep bluetooth power imu leds rgb audio decky)
  --volume --leds --sleep --bluetooth --power --rgb --rgb-daemon --imu --audio --sched --gfx --xmx --mango --decky
                            select components (repeatable; --all selects every one)
  --volume-daemon           with --volume: install the LEGACY oxp3-voltap daemon
                            (grabs an input device; with hhd running it only
                            observes) instead of the hhd plugin. The plugin is
                            the default and the supported path; a plugin change
                            needs `systemctl restart hhd`, which drops the
                            emulated Steam controller (28DE:12FF) for a few
                            seconds.
  --decky-artifact PATH     install this plugin zip instead of building one; it must match the
                            sha256 recorded in tools/decky-oxp3-status/artifact.sha256
  --restart-plugin-loader   after --decky, restart plugin_loader.service (root; only when the
                            unit exists - otherwise the exact command is printed)
  --uninstall               remove the selected components (keeps user config unless --purge-config)
  --purge-config            with --uninstall: also remove ~/.config/oxp3-rgb.toml and oxp3-voltap.conf
  --status                  read-only state report, no root needed
  --dry-run, -n             print every command, execute nothing (no root needed)
  --force                   continue even when tools/oxp3-preflight.sh finds a blocker
                            (WARNs never block; see docs/PREFLIGHT.md)
  --root DIR                install into DIR instead of / (offline simulation; component
                            installers that write to / are skipped, systemd/udev are not run)
  --user NAME               user whose user-scope files are installed/removed (default: SUDO_USER)
  --audio-activate          after --audio, run tools/audio/eq-on.sh (restarts the user PipeWire)
  --imu-reload              after --imu, reload bmi160_i2c/bmi260_i2c now instead of next boot
  --fan-curve               pass --fan-curve to tools/oxp3-power-install.sh (opt-in fan daemon)
  --adaptive-tdp            pass --adaptive-tdp to tools/oxp3-power-install.sh (installed inactive)
  --power-profile NAME      after --power, save quiet|balanced|performance as the boot profile
  --battery-limit N|off     after --power, save the charge limit (50-100 or off)
  --battery-bypass on|off   after --power, save charge-inhibit ('bypass') on/off
  --bt-persist MODE         items (default) | omit | none, passed to oxp3-bt-firmware.sh
  --bt-with-ddc             also install intel/ibt-00a0-0291-pci.ddc
  --bt-no-reload            do not reload btintel_pcie during --bluetooth
  --bt-from PATH            use local firmware copies instead of downloading
  -h, --help                this help

Exit codes: 0 ok, 1 error, 2 usage.
EOF
}

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
# prefix an absolute path with --root (no-op otherwise)
p() { printf '%s%s' "$PREFIX" "$1"; }

run() {
    if (( DRY_RUN )); then
        printf '  [dry-run]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    printf '  +'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

skip_prefixed() { printf '  [root-prefix] skipped: %s\n' "$*"; }

install_file() { # mode src dst
    local mode=$1 src=$2 dst=$3
    run install -D -m"$mode" "$src" "$dst"
}

# Render one of our units for the current checkout path.  The repo path must not contain
# whitespace, or the unquoted ExecStart in these units would be split by systemd.
render_unit() { # src
    [[ $repo != *[[:space:]]* ]] ||
        die "the checkout path contains whitespace ($repo); systemd ExecStart in the user units cannot represent it - move the repo or install the user units by hand"
    sed -e "s|%h/git/linux_scratch/oxp3-anatase-fixes|$repo|g" \
        -e "s|/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes|$repo|g" "$1"
}

user_home() { # user
    getent passwd "$1" | cut -d: -f6
}

rollback_tool() { printf '%s' "$repo/tools/oxp3-rollback.sh"; }

# An earlier session wired ~/heroic-launch.sh by hand (docs/LAUNCH.md). Adopt that edit into the
# undo state so `tools/oxp3-rollback.sh restore` can put the previous launcher back; this only
# writes the state directory, it never changes the launcher itself. Idempotent.
adopt_host_edits() {
    local tool home wiring u
    tool=$(rollback_tool)
    [[ -x $tool ]] || return 0
    u=${TARGET_USER:-${SUDO_USER:-}}
    [[ -n $u && $u != root ]] || return 0
    home=$(user_home "$u" 2>/dev/null) || return 0
    [[ -n $home ]] || return 0
    wiring="$PREFIX$home/heroic-launch.sh"
    [[ -f $wiring ]] || return 0
    grep -qE 'cp2077-direct\.sh|oxp3-gamewrap\.sh|heroic://' "$wiring" || return 0
    printf '  adopting the existing Heroic launcher wiring into the undo state\n'
    if [[ -n $PREFIX ]]; then
        "$tool" adopt --root "$PREFIX" --user "$u" >/dev/null || warn "adoption failed (see $tool adopt)"
    else
        "$tool" adopt --user "$u" >/dev/null || warn "adoption failed (see $tool adopt)"
    fi
}

resolve_user() {
    local u=${TARGET_USER:-${OXP3_USER:-${SUDO_USER:-}}}
    if [[ -z $u ]]; then
        if (( EUID != 0 )); then u=$(id -un); fi
    fi
    [[ -n $u ]] ||
        die "no unprivileged user to wire the user-scope parts for; rerun with sudo from your desktop session, or pass --user NAME"
    [[ $u != root ]] ||
        die "refusing to install the user-scope parts as root; run '$0' with sudo from your user session, or pass --user NAME"
    id -u "$u" >/dev/null 2>&1 || die "no such user: $u"
    TARGET_USER=$u
}

needs_user_scope() {
    local c
    for c in "${SELECTED[@]}"; do
        case $c in leds|rgb|audio) return 0 ;; esac
    done
    return 1
}

needs_root_scope() {
    local c
    for c in "${SELECTED[@]}"; do
        case $c in audio) ;; *) return 0 ;; esac
    done
    return 1
}

# The Decky plugin lives under the desktop user's home, but Decky's plugin_loader runs as root
# and owns ~/homebrew/plugins: resolving the invoking user is needed for the path (not for a
# user unit), so it must happen for --decky as well as for the user-scope components.
needs_target_user() {
    needs_user_scope || component_selected decky
}

# ---------------------------------------------------------------------------------------------
# Decky plugin (tools/lib/decky_plugin.py does resolution, validation and the file moves)
# ---------------------------------------------------------------------------------------------
DECKY_ARTIFACT_FROM_BUILD=0
DECKY_HELPER="$repo/tools/lib/decky_plugin.py"
DECKY_UNIT=plugin_loader.service

decky_require_helper() {
    command -v python3 >/dev/null 2>&1 || die "python3 is required for --decky (tools/lib/decky_plugin.py)"
    [[ -f $DECKY_HELPER ]] || die "missing $DECKY_HELPER"
}

decky_helper() { python3 "$DECKY_HELPER" "$@"; }

decky_home() { user_home "$TARGET_USER"; }

decky_plugins_dir() {
    decky_require_helper
    local home override=()
    home=$(decky_home)
    [[ -n ${OXP3_DECKY_PLUGINS_DIR:-} ]] && override=(--plugins-dir "$OXP3_DECKY_PLUGINS_DIR")
    decky_helper resolve --home "$home" ${PREFIX:+--root "$PREFIX"} ${override[@]+"${override[@]}"}
}

decky_state_dir() {
    if [[ -n ${OXP3_STATE_DIR:-} ]]; then
        printf '%s' "$OXP3_STATE_DIR"
    else
        printf '%s%s/.local/state/oxp3-fixes' "$PREFIX" "$(decky_home)"
    fi
}

# Prefer the artifact we already ship: reuse build/oxp3-status-*.zip when its digest matches the
# recorded one (no build, no network, works under sudo where root has no npm cache).  Build only
# when there is no usable artifact, and when building, do it as the invoking user, not as root.
decky_recorded_digest() {
    local f="$repo/tools/decky-oxp3-status/artifact.sha256" d
    [[ -f $f ]] || return 1
    d=$(awk '/^[0-9a-f]{64}[[:space:]]/ {print $1; exit}' "$f")
    [[ -n $d ]] || return 1
    printf '%s' "$d"
}

decky_find_verified_artifact() {
    local want z got
    want=$(decky_recorded_digest) || return 1
    while IFS= read -r z; do
        [[ -n $z ]] || continue
        got=$(sha256sum "$z" 2>/dev/null | cut -d' ' -f1)
        [[ $got == "$want" ]] && { printf '%s' "$z"; return 0; }
    done < <(find "$repo/tools/decky-oxp3-status/build" -maxdepth 1 -name 'oxp3-status-*.zip' -print 2>/dev/null | sort)
    return 1
}

decky_newest_zip() {
    local z
    z=$(find "$repo/tools/decky-oxp3-status/build" -maxdepth 1 -name 'oxp3-status-*.zip' -printf '%T@ %p\n' 2>/dev/null \
        | sort -nr | head -n 1 | cut -d' ' -f2-)
    [[ -n $z ]] || return 1
    printf '%s' "$z"
}

decky_select_artifact() {
    local z
    if [[ -n $DECKY_ARTIFACT ]]; then
        [[ -f $DECKY_ARTIFACT ]] || die "the --decky-artifact file does not exist: $DECKY_ARTIFACT"
        printf '%s' "$DECKY_ARTIFACT"
        return 0
    fi
    z=$(decky_find_verified_artifact) || z=""
    if [[ -n $z ]]; then
        printf '  using the recorded plugin artifact (sha256 matches artifact.sha256)\n' >&2
        printf '%s' "$z"
        return 0
    fi
    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
        local -a buildenv=()
        if [[ -n ${SUDO_USER:-} && ${SUDO_USER:-} != root ]]; then
            local uh uhcache
            uh=$(getent passwd "$SUDO_USER" | cut -d: -f6)
            if [[ -n $uh && -d $uh ]]; then
                uhcache="$uh/.npm"
                buildenv=(env "HOME=$uh" "NPM_CONFIG_CACHE=$uhcache" "npm_config_cache=$uhcache")
            fi
        fi
        say "Decky plugin: no recorded artifact to reuse; building the package"
        if "${buildenv[@]}" "$repo/tools/decky-oxp3-status/build.sh" --offline; then
            DECKY_ARTIFACT_FROM_BUILD=1
            z=$(decky_newest_zip) || z=""
            [[ -n $z ]] || die "the plugin build produced no build/oxp3-status-*.zip"
            printf '%s' "$z"
            return 0
        fi
        warn "the plugin build failed; falling back to an existing artifact"
    else
        printf '  node/npm not found; reusing an existing plugin zip\n'
    fi
    z=$(decky_newest_zip) || z=""
    [[ -n $z ]] || die \
        "no plugin artifact available; build it as your normal user (cd tools/decky-oxp3-status && ./build.sh --offline) and update artifact.sha256, or pass --decky-artifact PATH"
    printf '%s' "$z"
}

# A reused zip must match the recorded digest; a freshly built one is verified structurally and
# only reports a digest difference (a legitimate source change produces a new artifact).
decky_verify_artifact() {
    local artifact=$1 out args=(verify-artifact --repo "$repo" --artifact "$artifact")
    (( DECKY_ARTIFACT_FROM_BUILD )) || args+=(--require-recorded)
    if ! out=$(decky_helper "${args[@]}" 2>&1); then
        printf '%s\n' "$out" >&2
        if (( DECKY_ARTIFACT_FROM_BUILD )); then
            die "the freshly built plugin artifact failed validation"
        fi
        die "the plugin artifact did not verify against the recorded sha256 (tools/decky-oxp3-status/artifact.sha256)"
    fi
    printf '%s\n' "$out" | sed 's/^/  /'
}

decky_loader_unit_exists() {
    [[ -z $PREFIX ]] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl cat "$DECKY_UNIT" >/dev/null 2>&1
}

# Never restarts anything by default: print the exact command.  --restart-plugin-loader is the
# explicit opt-in (and still refuses when the unit does not exist).
decky_restart_notice() {
    if [[ -n $PREFIX ]]; then
        printf '  restart required on the host after this --root simulation:\n'
        printf '    sudo systemctl restart %s\n' "$DECKY_UNIT"
        return 0
    fi
    if ! command -v systemctl >/dev/null 2>&1 || ! decky_loader_unit_exists; then
        printf '  %s is not installed; no restart needed (is Decky Loader present?)\n' "$DECKY_UNIT"
        return 0
    fi
    if (( ! DECKY_RESTART )); then
        printf '  restart required so Decky Loader discovers the plugin:\n'
        printf '    sudo systemctl restart %s\n' "$DECKY_UNIT"
        printf '  (Decky -> Settings -> Developer -> Reload plugins re-reads the directory, but a\n'
        printf '   changed main.py still needs the service restart)\n'
        return 0
    fi
    if (( DRY_RUN )); then
        printf '  [dry-run] systemctl restart %s\n' "$DECKY_UNIT"
        return 0
    fi
    if (( EUID != 0 )); then
        warn "--restart-plugin-loader needs root; run: sudo systemctl restart $DECKY_UNIT"
        return 0
    fi
    if run systemctl restart "$DECKY_UNIT"; then
        printf '  restarted %s\n' "$DECKY_UNIT"
    else
        warn "could not restart $DECKY_UNIT; run: sudo systemctl restart $DECKY_UNIT"
    fi
}

# Did plugin_loader log this plugin?  Read-only; an unreadable journal is reported, not guessed.
decky_loader_log() {
    local out cmd=(journalctl -u "$DECKY_UNIT" -b --no-pager -n 2000)
    [[ -n $PREFIX ]] && { printf 'n/a'; return 0; }
    command -v journalctl >/dev/null 2>&1 || { printf 'no-journalctl'; return 0; }
    command -v timeout >/dev/null 2>&1 && cmd=(timeout 10 "${cmd[@]}")
    if ! out=$("${cmd[@]}" 2>/dev/null); then
        printf 'unreadable'
        return 0
    fi
    if grep -qiE 'oxp3[-_ ]?status' <<<"$out"; then
        printf 'loaded'
    else
        printf 'not-logged'
    fi
}


# As the invoking user (needed for --user systemctl/pactl/install into $HOME).  Never root.
as_user_run() {
    local u=$TARGET_USER uid home
    uid=$(id -u "$u")
    home=$(user_home "$u")
    if (( DRY_RUN )); then
        printf '  [dry-run] as %s:' "$u"
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    if [[ -n $PREFIX ]]; then
        env HOME="$PREFIX$home" XDG_CONFIG_HOME="$PREFIX$home/.config" "$@"
    elif [[ $(id -u) -eq $uid ]]; then
        env HOME="$home" XDG_CONFIG_HOME="$home/.config" \
            XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
    else
        runuser -u "$u" -- env HOME="$home" XDG_CONFIG_HOME="$home/.config" \
            XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
    fi
}

# systemctl --user for the invoking user; a missing session bus is a warning, not a failure
# (the unit is installed and starts at next login).
user_systemctl() {
    local u=$TARGET_USER uid
    uid=$(id -u "$u")
    if [[ -n $PREFIX ]]; then
        skip_prefixed "systemctl --user $*"
        return 0
    fi
    if (( DRY_RUN )); then
        printf '  [dry-run] as %s: systemctl --user' "$u"
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    if [[ ! -d /run/user/$uid ]]; then
        warn "no user manager for $u (/run/user/$uid missing); unit installed, start it after logging in: systemctl --user enable --now $1"
        return 0
    fi
    as_user_run systemctl --user "$@" ||
        warn "systemctl --user $* failed for $u (session bus?); the unit is installed and starts at next login"
}

# install/refresh one user unit (rendered) and enable it now
install_user_unit() { # src unit-name
    local src=$1 name=$2 home dst tmp
    home=$(user_home "$TARGET_USER")
    dst="$PREFIX$home/.config/systemd/user/$name"
    if (( DRY_RUN )); then
        printf '  [dry-run] render %s for %s -> %s\n' "$src" "$repo" "$dst"
        user_systemctl daemon-reload
        user_systemctl enable "$name"
        user_systemctl start "$name"
        return 0
    fi
    tmp=$(mktemp)
    render_unit "$src" >"$tmp"
    run install -D -m0644 "$tmp" "$dst"
    rm -f "$tmp"
    if (( EUID == 0 )); then
        chown "$TARGET_USER:$(id -gn "$TARGET_USER")" "$dst" 2>/dev/null || true
        # the directory chain above $HOME/.config may have been created by root
        chown "$TARGET_USER:$(id -gn "$TARGET_USER")" \
            "$PREFIX$home/.config" "$PREFIX$home/.config/systemd" \
            "$PREFIX$home/.config/systemd/user" 2>/dev/null || true
    fi
    user_systemctl daemon-reload
    user_systemctl enable "$name"
    if user_systemctl is-active --quiet "$name"; then
        user_systemctl restart "$name"
    else
        user_systemctl start "$name"
    fi
}

remove_user_unit() { # unit-name
    local name=$1 home dst
    home=$(user_home "$TARGET_USER")
    dst="$PREFIX$home/.config/systemd/user/$name"
    user_systemctl disable --now "$name" 2>/dev/null || true
    run rm -f "$dst"
    user_systemctl daemon-reload 2>/dev/null || true
}

# Component scripts call `sudo` internally.  When we are already root, put a no-op sudo wrapper
# first in PATH so they work even on images without sudo and never prompt.
setup_sudo_shim() {
    (( EUID == 0 )) || return 0
    (( DRY_RUN )) && return 0
    SUDO_SHIM_DIR=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-hw-sudo.XXXXXX")
    cat >"$SUDO_SHIM_DIR/sudo" <<'EOF'
#!/bin/sh
# oxp3-hw-install.sh: already root; make the component installer's `sudo` a no-op.
exec "$@"
EOF
    chmod 0700 "$SUDO_SHIM_DIR/sudo"
    PATH="$SUDO_SHIM_DIR:$PATH"
    export PATH
    trap 'rm -rf "$SUDO_SHIM_DIR"' EXIT
}

unit_state() { # unit -> enabled/active
    local en=- ac=-
    if [[ -n $PREFIX ]]; then
        printf 'n/a'
        return
    fi
    systemctl is-enabled --quiet "$1" 2>/dev/null && en=enabled
    systemctl is-active --quiet "$1" 2>/dev/null && ac=active
    printf '%s/%s' "$en" "$ac"
}

user_unit_state() { # unit -> enabled/active
    local u=$TARGET_USER uid out
    [[ -n $PREFIX ]] && { printf 'n/a'; return; }
    uid=$(id -u "$u" 2>/dev/null) || { printf '?'; return; }
    [[ -d /run/user/$uid ]] || { printf 'no-session'; return; }
    out=$(as_user_run systemctl --user is-enabled "$1" 2>/dev/null || true)
    [[ $out == enabled ]] || out=-
    if as_user_run systemctl --user is-active --quiet "$1" 2>/dev/null; then
        out="$out/active"
    else
        out="$out/-"
    fi
    printf '%s' "$out"
}

# ---------------------------------------------------------------------------------------------
# components: install
# ---------------------------------------------------------------------------------------------
volume_hwdb_install() {
    local hwdb=/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb
    install_file 0644 "$repo/tools/oxp3-voltap/84-oxp3-volume-keys-taps.hwdb" "$(p "$hwdb")"
    if [[ -n $PREFIX ]]; then
        skip_prefixed "systemd-hwdb update (writes /etc/udev/hwdb.bin)"
    elif (( DRY_RUN )); then
        printf '  [dry-run] systemd-hwdb update\n'
        printf '  [dry-run] udevadm trigger --subsystem-match=input --action=change\n'
    else
        run systemd-hwdb update
        run udevadm trigger --subsystem-match=input --action=change
    fi
}

volume_restart_notice() {
    cat <<EOF

  IMPORTANT: the hhd plugin is imported only when hhd starts. Restart it:
    sudo systemctl restart hhd
    Restarting hhd drops the emulated Steam controller (28DE:12FF) and the
    volume keyboard for a few seconds. Do it outside a game.
  The plugin never opens or grabs an input device. Do not grab hhd's devices -
  see docs/DAEMONS.md, "never grab what HHD owns".
EOF
}

comp_volume_install() {
    if [[ $VOLUME_MODE == daemon ]]; then
        say "volume rocker (LEGACY oxp3-voltap daemon + force-release hwdb)"
        [[ -n $PREFIX ]] && { skip_prefixed "tools/oxp3-voltap/install.sh install --hwdb (writes to /)"; return; }
        run "$repo/tools/oxp3-voltap/install.sh" install --hwdb
        warn "legacy daemon selected: with hhd running it stays in observe mode (exit 4) instead of grabbing hhd's device."
        warn "the supported path is the hhd plugin: rerun without --volume-daemon. See docs/DAEMONS.md."
        return 0
    fi
    say "volume rocker (hhd plugin + force-release hwdb)"
    if [[ -n $PREFIX ]]; then
        skip_prefixed "tools/hhd-oxp3-voltap/install.sh install (writes to /usr/local)"
        volume_hwdb_install
        volume_restart_notice
        return 0
    fi
    run "$repo/tools/hhd-oxp3-voltap/install.sh" install
    volume_hwdb_install
    if [[ -e /etc/systemd/system/oxp3-voltap.service || -e /usr/local/libexec/oxp3-voltap ]]; then
        warn "legacy oxp3-voltap daemon files are present; the plugin replaces it."
        printf '  disable/remove it: sudo systemctl disable --now oxp3-voltap ; sudo tools/oxp3-voltap/install.sh uninstall\n'
    fi
    volume_restart_notice
}

HID_RULES_DONE=0
install_hid_rules() {
    (( HID_RULES_DONE )) && return 0
    local dst
    dst=$(p /etc/udev/rules.d/90-oxp3-hid.rules)
    say "vendor HID access for the logged-in seat (udev rule)"
    install_file 0644 "$repo/tools/oxp3-hid.rules" "$dst"
    if [[ -n $PREFIX ]]; then
        skip_prefixed "udevadm control --reload-rules; udevadm trigger (hidraw, uinput)"
    elif (( DRY_RUN )); then
        printf '  [dry-run] udevadm control --reload-rules\n'
        printf '  [dry-run] udevadm trigger --subsystem-match=hidraw --action=change\n'
        printf '  [dry-run] udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=change\n'
    else
        run udevadm control --reload-rules
        run udevadm trigger --subsystem-match=hidraw --action=change
        run udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=change

    fi
    HID_RULES_DONE=1
}

comp_leds_install() {
    say "home button + top LEDs (udev rule + user unit)"
    install_hid_rules
    install_user_unit "$repo/tools/oxp3-homebtn.service" oxp3-homebtn.service
}

comp_sleep_install() {
    local x
    say "suspend/resume (wake-source policy + suspend log + s2idle low-power policy)"
    for x in oxp3-sleep-wakeup oxp3-sleep-log; do
        install_file 0755 "$repo/files/$x" "$(p "/usr/local/libexec/$x")"
    done
    install_file 0755 "$repo/tools/oxp3_lowpower.py" \
        "$(p /usr/local/libexec/oxp3-lowpower)"
    for x in oxp3-sleep-wakeup.service oxp3-sleep-pre.service oxp3-sleep-log.service \
             oxp3-lowpower.service oxp3-sleep-lowpower-pre.service \
             oxp3-sleep-lowpower-post.service; do
        install_file 0644 "$repo/files/$x" "$(p "/etc/systemd/system/$x")"
    done
    install_file 0644 "$repo/files/99-oxp3-sleep-wakeup.rules" \
        "$(p /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules)"
    maybe_install_trigger_policy
    if [[ -n $PREFIX ]]; then
        skip_prefixed "systemctl/udevadm for the sleep units"
        return
    fi
    run systemctl daemon-reload
    run systemctl enable --now oxp3-sleep-wakeup.service
    run systemctl enable oxp3-sleep-pre.service oxp3-sleep-log.service
    # Applies ASPM/mmc-wake/card-reader PM now (and at every boot), records the
    # pre-image in /var/lib/oxp3-lowpower/state.json, and exits 0 even when a
    # write is refused -- it can never wedge a boot.
    run systemctl enable --now oxp3-lowpower.service
    run systemctl enable oxp3-sleep-lowpower-pre.service oxp3-sleep-lowpower-post.service
    run udevadm control --reload-rules
    run udevadm trigger --action=change --subsystem-match=usb --subsystem-match=pci
    run udevadm trigger --action=change --subsystem-match=mmc_host --subsystem-match=mmc
}

# The trigger half of --sleep: what makes a *request* reach the kernel at all.
# Kept in one function so install, uninstall and --dry-run cannot drift.
TRIGGER_FILES=(
    "files/logind-oxp3.conf:/etc/systemd/logind.conf.d/60-oxp3-sleep.conf"
    "files/49-oxp3-suspend.rules:/etc/polkit-1/rules.d/49-oxp3-suspend.rules"
)
TRIGGER_LOGIND_UNIT=systemd-logind.service

maybe_install_trigger_policy() {
    local spec src dst
    say "suspend trigger policy (logind handles + polkit rule, no desktop session needed)"
    for spec in "${TRIGGER_FILES[@]}"; do
        src=${spec%%:*}
        dst=${spec#*:}
        install_file 0644 "$repo/$src" "$(p "$dst")"
    done
    if [[ -n $PREFIX ]]; then
        skip_prefixed "restart of $TRIGGER_LOGIND_UNIT (the drop-in is read at start)"
        return
    fi
    # polkitd watches /etc/polkit-1/rules.d and re-reads it by itself, so only the
    # logind drop-in needs the service to be told. Do not restart logind from a
    # running desktop session: the drop-in is picked up on the next start, and
    # `systemd-analyze cat-config systemd/logind.conf` already proves the parse.
    printf '  note: %s reads its drop-ins at start; apply the power-key policy with\n' "$TRIGGER_LOGIND_UNIT"
    printf '        sudo systemctl restart %s   (sessions survive; inhibitor locks are dropped and\n' "$TRIGGER_LOGIND_UNIT"
    printf '        re-acquired by their owners) or simply reboot. Verify with\n'
    printf '        systemd-analyze cat-config systemd/logind.conf | grep -A6 "^\\[Login\\]"\n'
}

comp_bluetooth_install() {
    say "Bluetooth firmware (upstream linux-firmware set into /usr/lib/firmware/updates)"
    if [[ -n $PREFIX ]]; then
        skip_prefixed "tools/oxp3-bt-firmware.sh ${BT_ARGS[*]} (writes to /)"
        return
    fi
    run "$repo/tools/oxp3-bt-firmware.sh" "${BT_ARGS[@]}"

    # The probe runs before switch_root and btintel_pcie has no MODULE_FIRMWARE()
    # entries, so dracut cannot carry the blobs: reload after the real root is up.
    say "Bluetooth boot-time reload (picks the firmware up after switch_root)"
    run install -D -m 0755 "$repo/files/oxp3-bt-reload.sh" "$(p /usr/local/libexec/oxp3-bt-reload)"
    run install -D -m 0644 "$repo/files/oxp3-bt-reload.service" "$(p /etc/systemd/system/oxp3-bt-reload.service)"
    if [[ -z $PREFIX ]]; then
        run systemctl daemon-reload
        run systemctl enable --now oxp3-bt-reload.service
    fi
}

comp_power_install() {
    say "power envelope + fan (PL1/PL2/PL4 units, battery limit optional)"
    if [[ -n $PREFIX ]]; then
        if (( ${#POWER_ARGS[@]} )); then
            skip_prefixed "tools/oxp3-power-install.sh install ${POWER_ARGS[*]} (writes to /)"
        else
            skip_prefixed "tools/oxp3-power-install.sh install (writes to /)"
        fi
    else
        run "$repo/tools/oxp3-power-install.sh" install "${POWER_ARGS[@]}"
        if [[ -n $POWER_PROFILE ]]; then
            run "$(p /usr/local/libexec/oxp3-powerctl)" profile "$POWER_PROFILE" --save
        fi
        if [[ -n $BATTERY_LIMIT ]]; then
            run "$(p /usr/local/libexec/oxp3-powerctl)" battery limit "$BATTERY_LIMIT" --save
        fi
        if [[ -n $BATTERY_BYPASS ]]; then
            run "$(p /usr/local/libexec/oxp3-powerctl)" battery bypass "$BATTERY_BYPASS" --save
        fi
    fi
}

comp_imu_install() {
    say "IMU: keep the BMI260 driver bound (bmi160_i2c blacklist)"
    install_file 0644 "$repo/files/oxp3-bmi160.conf" "$(p /etc/modprobe.d/oxp3-bmi160.conf)"
    if (( IMU_RELOAD )) && [[ -z $PREFIX ]]; then
        run bash -c 'modprobe -r bmi160_i2c 2>/dev/null || true; modprobe bmi260_i2c 2>/dev/null || true'
    else
        printf '  blacklist is read at boot; apply now with: sudo modprobe -r bmi160_i2c; sudo modprobe bmi260_i2c\n'
    fi
}

comp_rgb_install() {
    say "body RGB (HHD owns the zones by default)"
    install_hid_rules
    if (( ! RGB_DAEMON )); then
        # HHD addresses all five zones itself; our per-zone daemon re-applies its
        # own frames after HHD and therefore pins any zone listed in the config.
        # Keep HHD as the single owner unless the user explicitly opts in.
        printf '  HHD configures the RGB zones; no per-zone daemon installed\n'
        printf '  per-zone custom frames: re-run with --rgb-daemon (or tools/oxp3-rgbd.py)\n'
        if [[ -e $HOME/.config/systemd/user/oxp3-rgbd.service || -e ${PREFIX:-}/etc/systemd/user/oxp3-rgbd.service ]]; then
            printf '  note: oxp3-rgbd is installed from an earlier run; hand the zones back with\n'
            printf '        tools/oxp3-rgb-handback.sh\n'
        fi
        return 0
    fi
    local home dst
    home=$(user_home "$TARGET_USER")
    dst="$PREFIX$home/.config/oxp3-rgb.toml"
    if [[ -e $dst ]]; then
        printf '  keeping existing %s\n' "$dst"
    else
        as_user_run install -D -m0644 "$repo/tools/oxp3-rgbd.toml.example" "$dst"
    fi
    install_user_unit "$repo/tools/oxp3-rgbd.service" oxp3-rgbd.service
}

comp_audio_install() {
    say "speaker EQ (user scope, no root)"
    as_user_run "$repo/tools/audio/install-user.sh"
    if (( AUDIO_ACTIVATE )) && [[ -z $PREFIX ]]; then
        as_user_run "$repo/tools/audio/eq-on.sh" ||
            warn "the EQ drop-in is installed; run tools/audio/eq-on.sh after logging in"
    else
        printf '  activate with: tools/audio/eq-on.sh   (restarts the user PipeWire, makes the EQ sink default)\n'
    fi
}

comp_sched_install() {
    say "gaming CPU scheduler (scx_cosmos, sched_ext)"
    if ! command -v scx_cosmos >/dev/null 2>&1; then
        warn "scx_cosmos is not installed; layer it with: sudo rpm-ostree install scx-scheds (then reboot)"
        return 0
    fi
    run install -D -m0644 "$repo/files/oxp3-gaming-sched.service" \
        "$PREFIX/etc/systemd/system/oxp3-gaming-sched.service"
    if (( DRY_RUN )); then
        printf '  [dry-run] systemctl daemon-reload ; systemctl enable --now oxp3-gaming-sched\n'
        return 0
    fi
    systemctl daemon-reload
    systemctl enable --now oxp3-gaming-sched.service
    if systemctl is-active --quiet oxp3-gaming-sched.service; then
        printf '  oxp3-gaming-sched is active (scx_cosmos)\n'
    else
        warn "the unit is installed but not active; check: systemctl status oxp3-gaming-sched"
    fi
}

comp_gfx_install() {
    say "known-good host Mesa (Anatase XMX build)"
    local tool=$repo/tools/oxp3-mesa-restore.sh
    [[ -x $tool ]] || { warn "missing $tool"; return 0; }
    if (( DRY_RUN )); then
        printf '  [dry-run] %s status ; %s restore ; install oxp3-mesa-keep.service + /usr/local/libexec/oxp3-mesa-keep\n' "$tool" "$tool"
        return 0
    fi
    if "$tool" restore; then
        printf '  host Mesa restored to the recorded build\n'
    else
        warn "could not restore the known-good Mesa (see the tool output above)"
    fi
    if (( ! DRY_RUN )); then
        install -D -m0755 "$repo/files/oxp3-mesa-keep.sh" "$PREFIX/usr/local/libexec/oxp3-mesa-keep"
        install -D -m0644 "$repo/files/oxp3-mesa-keep.service" \
            "$PREFIX/etc/systemd/system/oxp3-mesa-keep.service"
        systemctl daemon-reload
        systemctl enable oxp3-mesa-keep.service
        printf '  oxp3-mesa-keep.service enabled: the recorded Mesa is restored on every boot after the overlay is cleared\n'
    fi
}

comp_xmx_install() {
    say "XeSS XMX for Flatpak Steam (sandbox env + Proton shim)"
    local tool=$repo/tools/oxp3-flatpak-xmx.sh
    [[ -x $tool ]] || { warn "missing $tool"; return 0; }
    if (( DRY_RUN )); then
        printf '  [dry-run] as %s: %s install\n' "$TARGET_USER" "$tool"
        return 0
    fi
    # install is per game prefix: cover every Proton prefix that already exists.
    local home base appids=() d
    home=$(user_home "$TARGET_USER")
    for base in "$home/.var/app/org.anatase.Steam" "$home/.var/app/com.valvesoftware.Steam"; do
        [ -d "$base" ] || continue
        base=$base/.local/share/Steam/steamapps/compatdata
        for d in "$base"/*; do
            [ -d "$d" ] && appids+=("$(basename "$d")")
        done
    done
    # The host environment is written even before a game exists: the "session env
    # not set" state is what the wrapper sources, independent of a per-game shim.
    as_user_run "$repo/tools/xmx-write-env.sh" || warn "could not write ~/.config/xess-xmx/oxp3-env.sh"
    if (( ${#appids[@]} == 0 )); then
        as_user_run "$tool" status | sed 's/^/  /'
        printf '  no Flatpak Steam Proton prefix exists yet, so there is no game shim to place.\n'
        printf '  the per-game shim is added by %s install --appid <id> after the first launch.\n' "$(basename "$tool")"
        printf '  launch the game through tools/xmx-run.sh (or its printed launch option) to use the XMX path.\n'
        return 0
    fi
    for d in "${appids[@]}"; do
        printf '  appid %s\n' "$d"
        as_user_run "$tool" install --appid "$d" || warn "install failed for appid $d"
    done
}

comp_mango_install() {
    say "MangoHud with Intel iGPU stats (patched build)"
    local rel cache rpms=0
    rel=$(rpm -q --qf '%{RELEASE}' mangohud 2>/dev/null | head -1)
    case $rel in
        *oxp3*) printf '  patched mangohud already installed (%s)\n' "$rel"; return 0 ;;
    esac
    cache=$HOME/.cache/oxp3-fixes/out
    [[ -d $cache ]] || cache=/var/home/"${TARGET_USER}"/.cache/oxp3-fixes/out
    shopt -s nullglob
    set -- "$cache"/mangohud-*oxp3*.x86_64.rpm "$cache"/mangohud-*oxp3*.i686.rpm
    rpms=$#
    shopt -u nullglob
    if (( rpms == 0 )); then
        warn "no patched mangohud RPM in $cache; build it with ./oxp3-fixes-fast.sh build"
        return 0
    fi
    if (( DRY_RUN )); then
        printf '  [dry-run] extract %d mangohud RPM(s) into the /usr overlay + enable oxp3-mangohud-keep.service\n' "$rpms"
        return 0
    fi
    # rpm-ostree cannot replace the base mangohud here ("cannot install both ... from @System"), and
    # the host already uses the writable /usr overlay for Mesa; extract the patched build the same way
    # and repeat it at boot via the keep unit, because the overlay is cleared on reboot.
    install -D -m0755 "$repo/files/oxp3-mangohud-keep.sh" "$PREFIX/usr/local/libexec/oxp3-mangohud-keep"
    install -D -m0644 "$repo/files/oxp3-mangohud-keep.service" \
        "$PREFIX/etc/systemd/system/oxp3-mangohud-keep.service"
    systemctl daemon-reload
    systemctl enable oxp3-mangohud-keep.service
    if "$PREFIX/usr/local/libexec/oxp3-mangohud-keep"; then
        printf '  patched MangoHud extracted; oxp3-mangohud-keep.service re-applies it on every boot\n'
    else
        warn "MangoHud extraction failed; see the output above"
    fi
}

comp_decky_install() {
    say "Decky status plugin (verified zip -> the Decky plugins directory)"
    local target artifact out
    target=$(decky_plugins_dir)
    [[ -n $target ]] || die "could not resolve the Decky plugins directory"
    printf '  plugin directory: %s\n' "$target"
    if (( DRY_RUN )); then
        if [[ -n $DECKY_ARTIFACT ]]; then
            printf '  [dry-run] verify and install artifact: %s\n' "$DECKY_ARTIFACT"
        else
            printf '  [dry-run] build tools/decky-oxp3-status/build.sh --offline when node+npm exist,\n'
            printf '            otherwise reuse a build/oxp3-status-*.zip verified against\n'
            printf '            tools/decky-oxp3-status/artifact.sha256\n'
        fi
        printf '  [dry-run] back up an existing oxp3-status directory into %s/decky-backups/\n' "$(decky_state_dir)"
        printf '  [dry-run] validate plugin.json (name/author/version/api_version) and dist/index.js\n'
        decky_restart_notice
        return 0
    fi
    artifact=$(decky_select_artifact)
    printf '  artifact: %s\n' "$artifact"
    decky_verify_artifact "$artifact"

    local state args=(install --repo "$repo" --artifact "$artifact" --home "$(decky_home)")
    [[ -n $PREFIX ]] && args+=(--root "$PREFIX")
    [[ -n ${OXP3_DECKY_PLUGINS_DIR:-} ]] && args+=(--plugins-dir "$OXP3_DECKY_PLUGINS_DIR")
    state=$(decky_state_dir)
    [[ -n $state ]] && args+=(--state-dir "$state")
    if ! out=$(decky_helper "${args[@]}" 2>&1); then
        printf '%s\n' "$out" >&2
        die "the Decky plugin was not installed (directory left as it was)"
    fi
    printf '%s\n' "$out" | sed 's/^/  /'
    if (( EUID == 0 )) && [[ -z $PREFIX ]]; then
        # the Decky plugins directory is normally root-owned; keep the state tree writable by the
        # invoking user so a later unprivileged status/rollback can read the backups
        chown -R "$TARGET_USER:$(id -gn "$TARGET_USER")" "$state/decky-backups" 2>/dev/null || true
    fi
    decky_restart_notice
}

comp_decky_uninstall() {
    say "Decky status plugin"
    local target out args=(uninstall --home "$(decky_home)")
    target=$(decky_plugins_dir)
    printf '  plugin directory: %s\n' "$target"
    [[ -n $PREFIX ]] && args+=(--root "$PREFIX")
    [[ -n ${OXP3_DECKY_PLUGINS_DIR:-} ]] && args+=(--plugins-dir "$OXP3_DECKY_PLUGINS_DIR")
    if (( DRY_RUN )); then
        printf '  [dry-run] verify %s/.oxp3-managed and remove %s\n' "$target" "$target"
        decky_restart_notice
        return 0
    fi
    if ! out=$(decky_helper "${args[@]}" 2>&1); then
        printf '%s\n' "$out" >&2
        die "refusing to remove the Decky plugin directory (see the message above)"
    fi
    printf '%s\n' "$out" | sed 's/^/  /'
    printf '  backups of replaced copies stay in %s/decky-backups/\n' "$(decky_state_dir)"
    decky_restart_notice
}

# ---------------------------------------------------------------------------------------------
# components: uninstall
# ---------------------------------------------------------------------------------------------
comp_volume_uninstall() {
    say "volume rocker"
    [[ -n $PREFIX ]] && { skip_prefixed "plugin + daemon uninstall (writes to /)"; return; }
    if [[ $VOLUME_MODE == daemon ]]; then
        run "$repo/tools/oxp3-voltap/install.sh" uninstall
    else
        run "$repo/tools/hhd-oxp3-voltap/install.sh" uninstall
        # A leftover legacy daemon would still be able to grab an input device;
        # remove it too so uninstalling the component leaves no grabber behind.
        if [[ -e /etc/systemd/system/oxp3-voltap.service || -e /usr/local/libexec/oxp3-voltap ]]; then
            warn "also removing the legacy oxp3-voltap daemon"
            run "$repo/tools/oxp3-voltap/install.sh" uninstall
        fi
        run rm -f "$(p /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb)"
        if (( DRY_RUN )); then
            printf '  [dry-run] systemd-hwdb update; udevadm trigger --subsystem-match=input --action=change\n'
        else
            run systemd-hwdb update || true
            run udevadm trigger --subsystem-match=input --action=change || true
        fi
    fi
    if (( PURGE_CONFIG )); then
        run rm -f "$(p /etc/oxp3-voltap.conf)"
    else
        printf '  keeping /etc/oxp3-voltap.conf (--purge-config removes it)\n'
    fi
}

# remove the shared HID rule only when neither user daemon needs it any more
maybe_remove_hid_rule() {
    local home other dst
    home=$(user_home "$TARGET_USER")
    if [[ $1 == leds ]]; then
        other="$PREFIX$home/.config/systemd/user/oxp3-rgbd.service"
    else
        other="$PREFIX$home/.config/systemd/user/oxp3-homebtn.service"
    fi
    if [[ -e $other ]]; then
        printf '  keeping 90-oxp3-hid.rules (still needed by %s)\n' "${other##*/}"
        return 0
    fi
    dst=$(p /etc/udev/rules.d/90-oxp3-hid.rules)
    run rm -f "$dst"
    if [[ -n $PREFIX ]]; then
        skip_prefixed "udevadm control --reload-rules after removing the HID rule"
    else
        run udevadm control --reload-rules
    fi
}

comp_leds_uninstall() {
    say "home button unit"
    remove_user_unit oxp3-homebtn.service
    maybe_remove_hid_rule leds
}

comp_sleep_uninstall() {
    local x spec dst
    say "suspend/resume units"
    if [[ -z $PREFIX && -x /usr/local/libexec/oxp3-lowpower ]]; then
        # put the recorded pre-images back before the script disappears
        run /usr/local/libexec/oxp3-lowpower undo
    fi
    if [[ -n $PREFIX ]]; then
        skip_prefixed "systemctl disable --now for the sleep units"
    else
        run systemctl disable --now oxp3-sleep-wakeup.service oxp3-sleep-pre.service \
            oxp3-sleep-log.service oxp3-lowpower.service oxp3-sleep-lowpower-pre.service \
            oxp3-sleep-lowpower-post.service
    fi
    for x in oxp3-sleep-wakeup.service oxp3-sleep-pre.service oxp3-sleep-log.service \
             oxp3-lowpower.service oxp3-sleep-lowpower-pre.service \
             oxp3-sleep-lowpower-post.service; do
        run rm -f "$(p "/etc/systemd/system/$x")"
    done
    for x in oxp3-sleep-wakeup oxp3-sleep-log oxp3-lowpower; do
        run rm -f "$(p "/usr/local/libexec/$x")"
    done
    run rm -f "$(p /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules)"
    say "suspend trigger policy (removed: logind and polkit go back to their defaults)"
    for spec in "${TRIGGER_FILES[@]}"; do
        dst=${spec#*:}
        run rm -f "$(p "$dst")"
    done
    if [[ -n $PREFIX ]]; then
        skip_prefixed "systemctl daemon-reload / udevadm reload after removing the sleep units"
    else
        run systemctl daemon-reload
        run udevadm control --reload-rules
        run udevadm trigger --action=change --subsystem-match=usb --subsystem-match=pci
        printf '  note: %s keeps the trigger policy it read at start until it is restarted or the\n' "$TRIGGER_LOGIND_UNIT"
        printf '        machine reboots; until then a bare power key is handled by KDE, not logind.\n'
    fi
}

comp_bluetooth_uninstall() {
    say "Bluetooth firmware"
    if [[ -n $PREFIX ]]; then
        skip_prefixed "removal of the dracut conf and the firmware files (writes to /)"
        return
    fi
    run systemctl disable --now oxp3-bt-reload.service 2>/dev/null || true
    run rm -f "$(p /etc/systemd/system/oxp3-bt-reload.service)" "$(p /usr/local/libexec/oxp3-bt-reload)"
    local conf tool
    conf=$(p /etc/dracut.conf.d/60-oxp3-bt-firmware.conf)
    tool=$(rollback_tool)
    if [[ -x $tool ]] && "$tool" has-record "$conf" 2>/dev/null; then
        run "$tool" restore-file "$conf" || warn "could not restore $conf; remove it by hand"
    else
        run rm -f "$conf"
    fi
    if rpm -q --quiet oxp3-firmware 2>/dev/null; then
        warn "the oxp3-firmware RPM owns the firmware files in this deployment; not deleting them by hand"
        printf '  remove the layered package instead: ./oxp3-fixes-fast.sh uninstall   (or oxp3-fixes.sh uninstall)\n'
    else
        local f
        for f in ibt-00a0-0291-iml.sfi ibt-00a0-0291-pci.sfi ibt-00a0-0291-pci.ddc; do
            run rm -f "$(p "/usr/lib/firmware/updates/intel/$f")"
        done
    fi
    printf '  the initramfs keeps its copy until the next deployment; a reboot restores whatever\n'
    printf '  linux-firmware ships (Bluetooth is then dead again on this image - that is stock).\n'
}

comp_power_uninstall() {
    say "power envelope + fan units"
    [[ -n $PREFIX ]] && { skip_prefixed "tools/oxp3-power-install.sh uninstall (writes to /)"; return; }
    local ctl=/usr/local/libexec/oxp3-powerctl
    # Undo the runtime state *before* removing the tool that can undo it: the charge limit and
    # charge-inhibit live in the EC and outlive a reboot, the RAPL limits are re-initialised by
    # the firmware at boot. All three are tolerant of an already-clean system.
    if [[ -x $ctl ]]; then
        run "$ctl" battery bypass off || warn "could not clear the charge-inhibit (check: $ctl battery status)"
        run "$ctl" battery limit off || warn "could not clear the charge limit (check: $ctl battery status)"
        run "$ctl" restore-baseline || warn "could not restore the RAPL baseline (a reboot also does)"
    fi
    run "$repo/tools/oxp3-power-install.sh" uninstall
    printf '  /etc/oxp3/ (power.json, baseline, fan curve) is deliberately kept; the live PL1/PL2/PL4\n'
    printf '  were re-applied from the saved baseline. If that failed, the firmware reinitialises them\n'
    printf '  at the next reboot.\n'
}

comp_imu_uninstall() {
    local conf
    say "IMU modprobe blacklist"
    conf=$(p /etc/modprobe.d/oxp3-bmi160.conf)
    if [[ -f $conf ]] && ! cmp -s "$repo/files/oxp3-bmi160.conf" "$conf"; then
        warn "$conf differs from ours; keeping it (remove it by hand if it is stale)"
    else
        run rm -f "$conf"
    fi
    printf '  affects the next boot only; the running bmi260 driver is left alone.\n'
}

comp_rgb_uninstall() {
    local home cfg
    say "body RGB keeper"
    remove_user_unit oxp3-rgbd.service
    if (( PURGE_CONFIG )); then
        home=$(user_home "$TARGET_USER")
        cfg="$PREFIX$home/.config/oxp3-rgb.toml"
        run rm -f "$cfg"
    else
        printf '  keeping the RGB config (--purge-config removes it)\n'
    fi
    maybe_remove_hid_rule rgb
}

comp_audio_uninstall() {
    local home cfg
    say "speaker EQ (user scope)"
    home=$(user_home "$TARGET_USER")
    cfg="$PREFIX$home/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf"
    if [[ -n $PREFIX ]]; then
        run rm -f "$cfg"
        return
    fi
    if [[ -d /run/user/$(id -u "$TARGET_USER") ]] && as_user_run "$repo/tools/audio/eq-off.sh"; then
        :
    else
        warn "eq-off.sh could not reach the user session; removing the drop-in directly"
        run rm -f "$cfg"
        printf '  restart with: systemctl --user restart pipewire wireplumber\n'
    fi
}

# ---------------------------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------------------------
# Read-only prerequisite validator (docs/PREFLIGHT.md). It runs for real installs only:
# a dry run must stay a plan, --root is an offline prefix, and uninstall removes what is
# there. A FAIL would produce a half-install, so it aborts unless --force; WARNs are the
# degraded paths the component installers already handle and are printed, not fatal.
run_preflight() {
    local pf=$repo/tools/oxp3-preflight.sh rc=0
    local args=(--quiet)
    [[ -x $pf ]] || { warn "tools/oxp3-preflight.sh is missing; skipping the prerequisite check"; return 0; }

    for c in "${SELECTED[@]}"; do args+=(--component "$c"); done
    if [[ -n ${TARGET_USER:-} ]]; then args+=(--user "$TARGET_USER"); fi

    printf '\n== preflight (%s)\n' "${SELECTED[*]}"
    set +e
    "$pf" "${args[@]}"
    rc=$?
    set -e
    case $rc in
        0) ;;
        1)
            if (( FORCE )); then
                warn "preflight found a blocker; continuing because --force was given"
            else
                warn "refusing to install: the preflight found a true blocker."
                warn "fix the FAIL rows above, or re-run with --force to override"
                warn "(docs/PREFLIGHT.md lists every check and its remediation)."
                return 1
            fi
            ;;
        2) warn "preflight usage error; continuing without its verdict" ;;
        *) warn "preflight could not complete (exit $rc); continuing without its verdict" ;;
    esac
    return 0
}

# ---------------------------------------------------------------------------------------------
# install / uninstall driver
# ---------------------------------------------------------------------------------------------
component_selected() {
    local c
    for c in "${SELECTED[@]}"; do [[ $c == "$1" ]] && return 0; done
    return 1
}

run_selected() {
    local action=$1 c
    for c in "${ALL_COMPONENTS[@]}"; do
        component_selected "$c" || continue
        "comp_${c}_${action}"
    done
}

print_verify_hints() {
    if [[ -n $PREFIX ]]; then
        say "verify"
        printf '  (--root %s: systemd/udev were not touched, so verification belongs on the real host)\n' "$PREFIX"
        return 0
    fi
    say "verify"
    component_selected volume && printf '  systemctl status hhd; tools/hhd-oxp3-voltap/install.sh status; tools/oxp3-verify.sh (hhd.*, vol.*)\n'
    component_selected leds && printf '  systemctl --user status oxp3-homebtn; tools/oxp3-led.py status\n'
    component_selected sleep && printf '  systemctl status oxp3-sleep-wakeup; tools/sleep-power-measure.sh audit\n'
    component_selected bluetooth && printf '  bluetoothctl show; ls /sys/class/bluetooth\n'
    component_selected power && printf '  /usr/local/libexec/oxp3-powerctl status; systemctl status oxp3-power-profile\n'
    component_selected imu && printf '  tools/oxp3-imu.py selftest\n'
    component_selected rgb && printf '  systemctl --user status oxp3-rgbd; tools/oxp3-rgbd.py --check\n'
    component_selected audio && printf '  pactl list sinks short | grep oxp3_speaker_eq; tools/audio/eq_response.py\n'
    component_selected decky && printf '  tools/oxp3-hw-install.sh --status; tools/oxp3-verify.sh (decky.dir, decky.loaded)\n'
    printf '  tools/oxp3-status.sh          # the full read-only matrix (docs/INSTALL-HW.md)\n'
}

do_install_or_uninstall() {
    local action=$1
    [[ ${#SELECTED[@]} -gt 0 ]] || die "no component selected; use --all or one of --volume --leds --sleep --bluetooth --power --rgb --imu --audio --decky (see --help)"

    # user-scope target: only resolve when something needs it (audio is user-only; leds/rgb
    # install a user unit and therefore also need it; decky needs the home that owns the
    # root-owned Decky plugins directory)
    if needs_target_user; then
        resolve_user
    fi

    # root policy: dry run and --root never need root; only --audio may run unprivileged
    if (( EUID != 0 )) && [[ -z $PREFIX ]] && (( !DRY_RUN )); then
        if needs_root_scope; then
            local q=()
            local a
            for a in "${ORIG_ARGS[@]}"; do q+=("$(printf '%q' "$a")"); done
            die "the selected hardware components write to /etc and /usr/local and need root; rerun as:
  sudo $0 ${q[*]}"
        fi
    fi

    if (( DRY_RUN )); then
        printf 'preflight: skipped in --dry-run (nothing is written)\n'
    elif [[ -n $PREFIX ]]; then
        printf 'preflight: skipped in --root mode (offline prefix)\n'
    elif [[ $action == install ]]; then
        run_preflight || return $?
    fi

    if (( EUID == 0 )) && [[ -z $PREFIX ]] && (( !DRY_RUN )); then
        setup_sudo_shim
    fi

    if (( DRY_RUN )); then
        printf 'dry run: nothing is changed. Components: %s\n' "${SELECTED[*]}"
    fi

    run_selected "$action"

    if (( !DRY_RUN )); then
        if [[ $action == install ]]; then
            adopt_host_edits
            print_verify_hints
            if [[ -z $PREFIX ]] && { component_selected bluetooth || component_selected imu; }; then
                printf '\nReboot needed for: the Bluetooth initramfs (regenerated at the next deployment) '
                printf 'and the IMU blacklist.\n'
            fi
        else
            say "uninstall complete"
            printf '  user config kept unless --purge-config was given; /etc/oxp3 kept by the power uninstaller.\n'
            if [[ -x $(rollback_tool) ]]; then
                printf '  in-place host edits (launcher, Heroic GamesConfig, dracut drop-in, hhd):\n'
                printf '    %s restore\n' "$(rollback_tool)"
            fi
        fi
    fi
}

# ---------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------
yes_no() { if [[ -e "$1" ]]; then printf yes; else printf no; fi; }
prefixed_yes_no() { yes_no "$(p "$1")"; }

decky_status_line() {
    local line args
    if ! command -v python3 >/dev/null 2>&1 || [[ ! -f $DECKY_HELPER ]]; then
        printf 'state=unknown (python3/helper missing)'
        return 0
    fi
    args=(status --home "$(decky_home)" --format line)
    [[ -n $PREFIX ]] && args+=(--root "$PREFIX")
    [[ -n ${OXP3_DECKY_PLUGINS_DIR:-} ]] && args+=(--plugins-dir "$OXP3_DECKY_PLUGINS_DIR")
    line=$(decky_helper "${args[@]}" 2>/dev/null) || line='state=error (status failed)'
    printf '%s' "$line"
}

print_status() {
    local home f n lim beh
    if [[ -z $TARGET_USER ]]; then
        TARGET_USER=${SUDO_USER:-$(id -un)}
        [[ $TARGET_USER == root ]] && TARGET_USER=$(id -un)
    fi
    home=$(user_home "$TARGET_USER" 2>/dev/null || echo "$HOME")

    local where="live system"
    [[ -n $PREFIX ]] && where="root-prefix $PREFIX"
    say "oxp3 hardware fixes ($where)"
    local pyver site
    pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3.15)
    site="$PREFIX/usr/local/lib/python$pyver/site-packages"
    printf '  volume:       hhd-plugin=%s daemon-unit=%s daemon-binary=%s\n' \
        "$(yes_no "$site/oxp3_hhd_voltap")" \
        "$(prefixed_yes_no /etc/systemd/system/oxp3-voltap.service)" \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-voltap)"
    printf '  libexec:      vol=%s sleep-wakeup=%s sleep-log=%s lowpower=%s powerctl=%s adaptivetdp=%s\n' \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-voltap)" \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-sleep-wakeup)" \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-sleep-log)" \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-lowpower)" \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-powerctl)" \
        "$(prefixed_yes_no /usr/local/libexec/oxp3-adaptivetdp)"
    printf '  udev rules:   hid=%s sleep-wakeup=%s volume-hwdb=%s\n' \
        "$(prefixed_yes_no /etc/udev/rules.d/90-oxp3-hid.rules)" \
        "$(prefixed_yes_no /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules)" \
        "$(prefixed_yes_no /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb)"
    printf '  sleep trigger: logind-drop-in=%s polkit-rule=%s\n' \
        "$(prefixed_yes_no /etc/systemd/logind.conf.d/60-oxp3-sleep.conf)" \
        "$(prefixed_yes_no /etc/polkit-1/rules.d/49-oxp3-suspend.rules)"
    printf '  units:        vol=%s sleep=%s pre=%s log=%s lowpower=%s post=%s profile=%s resume=%s fan=%s adaptivetdp=%s\n' \
        "$(unit_state oxp3-voltap.service)" "$(unit_state oxp3-sleep-wakeup.service)" \
        "$(unit_state oxp3-sleep-pre.service)" "$(unit_state oxp3-sleep-log.service)" \
        "$(unit_state oxp3-lowpower.service)" "$(unit_state oxp3-sleep-lowpower-post.service)" \
        "$(unit_state oxp3-power-profile.service)" "$(unit_state oxp3-power-resume.service)" \
        "$(unit_state oxp3-fan-curve.service)" "$(unit_state oxp3-adaptivetdp.service)"
    printf '  user units:   homebtn=%s rgbd=%s (user %s)\n' \
        "$(user_unit_state oxp3-homebtn.service)" "$(user_unit_state oxp3-rgbd.service)" "$TARGET_USER"
    printf '  power:        profile-file=%s live PL1/PL2/PL4=%s/%s/%s W\n' \
        "$(prefixed_yes_no /etc/oxp3/power.json)" \
        "$(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))" \
        "$(( $(cat /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))" \
        "$(( $(cat /sys/class/powercap/intel-rapl:0/constraint_2_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))"
    lim=$(cat /sys/class/power_supply/BAT0/charge_control_end_threshold 2>/dev/null || echo n/a)
    beh=$(cat /sys/class/power_supply/BAT0/charge_behaviour 2>/dev/null || echo n/a)
    printf '  battery:      limit=%s%% behaviour=%s\n' "$lim" "$beh"
    n=0
    for f in ibt-00a0-0291-iml.sfi ibt-00a0-0291-pci.sfi; do
        [[ -e "$(p "/usr/lib/firmware/updates/intel/$f")" ]] && n=$((n + 1))
    done
    printf '  bluetooth:    fw-in-updates=%s/2 dracut-conf=%s hci0=%s\n' "$n" \
        "$(prefixed_yes_no /etc/dracut.conf.d/60-oxp3-bt-firmware.conf)" \
        "$(yes_no /sys/class/bluetooth/hci0)"
    printf '  imu:          modprobe-blacklist=%s\n' \
        "$(prefixed_yes_no /etc/modprobe.d/oxp3-bmi160.conf)"
    printf '  rgb:          config=%s effect=%s\n' \
        "$(yes_no "$PREFIX$home/.config/oxp3-rgb.toml")" \
        "$(sed -n 's/^[[:space:]]*effect[[:space:]]*=[[:space:]]*//p' "$PREFIX$home/.config/oxp3-rgb.toml" 2>/dev/null | head -1 | tr -d '"' | tr -d "'" | sed 's/[[:space:]]*$//')"
    printf '  audio:        eq-dropin=%s\n' \
        "$(yes_no "$PREFIX$home/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf")"
    printf '  decky:        %s loader-log=%s\n' \
        "$(decky_status_line)" "$(decky_loader_log)"
}

# ---------------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------------
add_component() {
    local c=$1 x
    for x in "${SELECTED[@]}"; do [[ $x == "$c" ]] && return 0; done
    SELECTED+=("$c")
}

while (( $# )); do
    case $1 in
        --volume|--leds|--sleep|--bluetooth|--power|--rgb|--rgb-daemon|--imu|--audio|--sched|--gfx|--xmx|--mango|--decky)
            add_component "${1#--}"
            [[ $1 == --volume ]] && VOLUME_MODE=plugin ;;
        --volume-daemon)
            add_component volume; VOLUME_MODE=daemon ;;
        --all) for c in "${ALL_COMPONENTS[@]}"; do add_component "$c"; done ;;
        --uninstall|--remove) ACTION=uninstall ;;
        --status) ACTION=status ;;
        --dry-run|-n) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        --purge-config) PURGE_CONFIG=1 ;;
        --decky-artifact) [[ $# -ge 2 ]] || die "--decky-artifact needs a zip path"
                          DECKY_ARTIFACT=$2; shift ;;
        --decky-artifact=*) DECKY_ARTIFACT=${1#*=} ;;
        --restart-plugin-loader) DECKY_RESTART=1 ;;
        --root) [[ $# -ge 2 ]] || die "--root needs a directory"; PREFIX=${2%/}; shift
                [[ -n $PREFIX ]] || die "--root=/ is not a simulation"; ;;
        --root=*) PREFIX=${1#*=}; PREFIX=${PREFIX%/}
                  [[ -n $PREFIX ]] || die "--root=/ is not a simulation" ;;
        --user) [[ $# -ge 2 ]] || die "--user needs a name"; TARGET_USER=$2; shift ;;
        --user=*) TARGET_USER=${1#*=} ;;
        --audio-activate) AUDIO_ACTIVATE=1 ;;
        --imu-reload) IMU_RELOAD=1 ;;
        --fan-curve) POWER_ARGS+=(--fan-curve) ;;
        --adaptive-tdp) POWER_ARGS+=(--adaptive-tdp) ;;
        --power-profile) [[ $# -ge 2 ]] || die "--power-profile needs quiet|balanced|performance"
                         POWER_PROFILE=$2; shift ;;
        --battery-limit) [[ $# -ge 2 ]] || die "--battery-limit needs 50-100 or off"; BATTERY_LIMIT=$2; shift ;;
        --battery-bypass) [[ $# -ge 2 ]] || die "--battery-bypass needs on or off"; BATTERY_BYPASS=$2; shift ;;
        --bt-persist) [[ $# -ge 2 ]] || die "--bt-persist needs items|omit|none"
                       BT_ARGS=(--persist="$2"); shift ;;
        --bt-persist=*) BT_ARGS=(--persist="${1#*=}") ;;
        --bt-with-ddc) BT_ARGS+=(--with-ddc) ;;
        --bt-no-reload) BT_ARGS+=(--no-reload) ;;
        --bt-from) [[ $# -ge 2 ]] || die "--bt-from needs a path"; BT_ARGS+=(--from "$2"); shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'error: unknown option: %s (try --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

if [[ $ACTION != status ]]; then
    case $POWER_PROFILE in ""|quiet|balanced|performance) ;; *) die "--power-profile must be quiet, balanced or performance" ;; esac
    case $BATTERY_LIMIT in ""|off|[5-9][0-9]|100) ;; *) die "--battery-limit must be 50-100 or off" ;; esac
    case $BATTERY_BYPASS in ""|on|off) ;; *) die "--battery-bypass must be on or off" ;; esac

    if [[ -n $POWER_PROFILE || -n $BATTERY_LIMIT || -n $BATTERY_BYPASS || ${#POWER_ARGS[@]} -gt 0 ]]; then
        component_selected power || die "the power options (--power-profile/--battery-*/--fan-curve/--adaptive-tdp) need --power"
        [[ $ACTION == install ]] || die "the power options only apply to install, not --uninstall"
    fi
    if (( AUDIO_ACTIVATE )); then
        component_selected audio || die "--audio-activate needs --audio"
    fi
    if (( IMU_RELOAD )); then
        component_selected imu || die "--imu-reload needs --imu"
    fi
    if [[ -n $DECKY_ARTIFACT ]]; then
        component_selected decky || die "--decky-artifact needs --decky"
        [[ $ACTION == install ]] || die "--decky-artifact only applies to install, not --uninstall"
        [[ -f $DECKY_ARTIFACT ]] || die "--decky-artifact file does not exist: $DECKY_ARTIFACT"
    fi
    if (( DECKY_RESTART )); then
        component_selected decky || die "--restart-plugin-loader needs --decky"
    fi
fi

# an environment prefix is the same simulation as --root (used by tests/scripts)
if [[ -z $PREFIX && -n ${OXP3_ROOT:-} ]]; then
    PREFIX=${OXP3_ROOT%/}
    [[ -n $PREFIX ]] || die "OXP3_ROOT=/ is not a simulation"
fi

if [[ -n $PREFIX ]]; then
    [[ -d $PREFIX ]] || die "--root directory does not exist: $PREFIX"
    PREFIX=$(CDPATH= cd -- "$PREFIX" && pwd)
fi

if [[ $ACTION == status ]]; then
    print_status
    exit 0
fi

do_install_or_uninstall "$ACTION"
