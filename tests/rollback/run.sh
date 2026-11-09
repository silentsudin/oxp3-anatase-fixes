#!/usr/bin/env bash
# tests/rollback/run.sh - offline round-trip for the undo story, under a --root prefix.
#
# Installs the hardware components into a scratch prefix (no root, no systemd, no udev), edits
# the user-owned files that the hardware installer cannot create (the Heroic launcher wrapper
# and a Heroic GamesConfig JSON), then uninstalls and restores, asserting that every file we
# modified is byte-identical to what it was before we touched it.
#
#   tests/rollback/run.sh            # exit 0 = every assertion held
#
# Nothing outside the temporary prefix is written: HOME/XDG_* are derived from the prefix and
# the rollback tool keeps its state directory inside it. No root, no network, no host change.
set -euo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-rollback.XXXXXX") || exit 1
trap 'rm -rf -- "$tmp"' EXIT INT TERM
prefix=$tmp/prefix
user=$(id -un)
user_home=$(getent passwd "$user" | cut -d: -f6)
home=$prefix$user_home
state=$home/.local/state/oxp3-fixes
rbt=$repo/tools/oxp3-rollback.sh
hw=$repo/tools/oxp3-hw-install.sh
# The Decky component is part of the same --root simulation when a built plugin zip exists.
decky_zip=$repo/tools/decky-oxp3-status/build/oxp3-status-1.1.0.zip
decky_args=()
decky_uninstall_args=()
if [ -f "$decky_zip" ]; then
    decky_args=(--decky --decky-artifact "$decky_zip")
    decky_uninstall_args=(--decky)
fi
decky_plugin=$home/homebrew/plugins/oxp3-status
fail=0
ok()   { printf 'ok   %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; fail=$((fail + 1)); }
check() { # LABEL COMMAND...
    local label=$1; shift
    if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi
}
check_eq() { # LABEL EXPECTED ACTUAL
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

launcher=$home/heroic-launch.sh
heroic_dir=$home/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig
heroic=$heroic_dir/1423049311.json
artifact=$home/.config/xess-xmx/oxp3-env.sh

mkdir -p "$home" "$heroic_dir" "$home/.config/xess-xmx" "$prefix"

# --- the fake host state before anything runs ------------------------------------------------
# The launcher is already the wrapper (the state this host was in: the wiring was done by hand)
# and a .bak-* sibling holds what was there before that, exactly like ~/heroic-launch.sh.bak-1716.
cp "$repo/tools/heroic-launch.sh" "$launcher"
chmod 0755 "$launcher"
printf '#!/bin/sh\n# stock launcher the wrapper replaced\nexec flatpak run com.heroicgameslauncher.hgl "$@"\n' >"$launcher.bak-1716"
chmod 0755 "$launcher.bak-1716"
printf '{\n  "1423049311": {\n    "envVars": {\n      "VK_DRIVER_FILES": "/home/u/icd.json",\n      "force_vk_vendor": "0"\n    },\n    "useGameMode": false\n  },\n  "version": "v0",\n  "explicit": true\n}' >"$heroic"
printf 'export A=1\n' >"$artifact"
cp -a "$launcher.bak-1716" "$tmp/launcher.before"
cp -a "$heroic" "$tmp/heroic.before"
launcher_sha=$(sha256sum "$launcher.bak-1716" | cut -d' ' -f1)
launcher_current_sha=$(sha256sum "$launcher" | cut -d' ' -f1)
heroic_sha=$(sha256sum "$heroic" | cut -d' ' -f1)

# --- 1. before the install: our in-place edits are known but not backed up --------------------
set +e
"$rbt" verify --root "$prefix" >"$tmp/verify-before.txt" 2>&1
rc=$?
set -e
check_eq "verify before the install exits 1 (edits unrecorded)" 1 "$rc"
check "verify lists the unbacked launcher" grep -q '^UNBACKED .*heroic-launch\.sh$' "$tmp/verify-before.txt"
check "verify lists the unbacked Heroic JSON" grep -q '^UNBACKED .*1423049311\.json$' "$tmp/verify-before.txt"

# --- 2. install into the prefix (the hardware installer adopts the existing wiring) -----------
if ! "$hw" --root "$prefix" "${decky_args[@]+"${decky_args[@]}"}" --sleep --leds --rgb --imu --audio >"$tmp/install.log" 2>&1; then
    bad "hardware install into the prefix"
    sed 's/^/     /' "$tmp/install.log"
else
    ok "hardware install into the prefix"
fi
check "the install adopted the launcher wiring" grep -q 'adopting the existing Heroic launcher wiring' "$tmp/install.log"
check "adoption created the manifest under the prefix" test -f "$state/manifest.tsv"
if [ "${#decky_args[@]}" -gt 0 ]; then
    check "the Decky plugin is installed in the prefix" test -f "$decky_plugin/plugin.json"
    check "the Decky plugin carries our ownership marker" test -f "$decky_plugin/.oxp3-managed"
fi

# --- 3. perform the in-place edits (launcher wrapper + Heroic env cleanup) -------------------
if ! "$rbt" apply --root "$prefix" --heroic-env-clean 1423049311 >"$tmp/apply.log" 2>&1; then
    bad "apply the in-place edits"
    sed 's/^/     /' "$tmp/apply.log"
else
    ok "apply the in-place edits"
fi
check "the launcher is still the wrapper (apply had nothing to change)" \
    cmp -s "$launcher" "$repo/tools/heroic-launch.sh"
check "the Heroic JSON lost its per-game XMX envVars" bash -c "! grep -q VK_DRIVER_FILES '$heroic'"
check "the pre-edit image of the launcher is on disk" \
    test -n "$(find "$state/backups" -path '*/home/heroic-launch.sh' -type f -print -quit)"
check "verify now passes" "$rbt" verify --root "$prefix"
set +e
"$rbt" verify --root "$prefix" >/dev/null 2>&1
rc=$?
set -e
check_eq "verify after recording exits 0" 0 "$rc"

# --- 4. a second install+apply must be idempotent (no new backup, same file list) ------------
rows_before=$(wc -l <"$state/manifest.tsv")
find "$prefix" -type f | sort >"$tmp/tree-before.txt"
"$hw" --root "$prefix" "${decky_args[@]+"${decky_args[@]}"}" --sleep --leds --rgb --imu --audio >/dev/null 2>&1
"$rbt" apply --root "$prefix" --heroic-env-clean 1423049311 >/dev/null 2>&1
rows_after=$(wc -l <"$state/manifest.tsv")
check_eq "a second run does not add a backup" "$rows_before" "$rows_after"
find "$prefix" -type f -not -path "$state/*" | sort >"$tmp/tree-after.txt"
check_eq "a second run changes no installed file" \
    "$(grep -v "$state/" "$tmp/tree-before.txt")" "$(cat "$tmp/tree-after.txt")"
if [ "${#decky_args[@]}" -gt 0 ]; then
    check "the Decky reinstall backed up the previous copy" \
        bash -c "ls -d '$state/decky-backups'/*/oxp3-status >/dev/null 2>&1"
fi

# --- 5. uninstall the hardware components ----------------------------------------------------
if ! "$hw" --root "$prefix" --uninstall "${decky_uninstall_args[@]+"${decky_uninstall_args[@]}"}" --sleep --leds --rgb --imu --audio >"$tmp/uninstall.log" 2>&1; then
    bad "hardware uninstall from the prefix"
    sed 's/^/     /' "$tmp/uninstall.log"
else
    ok "hardware uninstall from the prefix"
fi
check "the sleep units are gone" test ! -e "$prefix/etc/systemd/system/oxp3-sleep-wakeup.service"
check "the IMU blacklist is gone" test ! -e "$prefix/etc/modprobe.d/oxp3-bmi160.conf"
check "the speaker EQ drop-in is gone" test ! -e "$home/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf"
if [ "${#decky_args[@]}" -gt 0 ]; then
    check "the Decky plugin directory is gone" test ! -e "$decky_plugin"
    check "the Decky backup survives the uninstall" \
        bash -c "ls -d '$state/decky-backups'/*/oxp3-status >/dev/null 2>&1"
fi

# --- 6. restore every in-place edit ----------------------------------------------------------
if ! "$rbt" restore --root "$prefix" >"$tmp/restore.log" 2>&1; then
    bad "restore the in-place edits"
    sed 's/^/     /' "$tmp/restore.log"
else
    ok "restore the in-place edits"
fi

# --- 7. byte-identical restoration -----------------------------------------------------------
check_eq "the launcher is byte-identical to before the install" "$launcher_sha" \
    "$(sha256sum "$launcher" | cut -d' ' -f1)"
check_eq "the Heroic GamesConfig JSON is byte-identical" "$heroic_sha" \
    "$(sha256sum "$heroic" | cut -d' ' -f1)"
cmp -s "$launcher" "$tmp/launcher.before" && ok "launcher cmp(1) matches" || bad "launcher cmp(1) differs"
cmp -s "$heroic" "$tmp/heroic.before" && ok "Heroic JSON cmp(1) matches" || bad "Heroic JSON cmp(1) differs"
[ ! -e "$artifact" ] && ok "the adopted artifact we created was removed" || bad "the adopted artifact still exists"
check_eq "the launcher kept its executable mode" 755 "$(stat -c '%a' "$launcher")"

# --- 8. the state stays inside the prefix and the undo is recorded ---------------------------
case $state in "$prefix"/*) ok "the undo state is inside the --root prefix" ;;
                  *) bad "the undo state escaped the prefix: $state" ;; esac
check "the manifest records the restores" grep -q $'\trestored\t' "$state/manifest.tsv"
check "a second restore is a no-op" "$rbt" restore --root "$prefix"
check "verify passes after the restore" "$rbt" verify --root "$prefix"
check "status --json is valid JSON" bash -c "'$rbt' status --root '$prefix' --json | python3 -m json.tool"

printf '\n%s: %d assertion(s) failed\n' "${0##*/}" "$fail"
[ "$fail" -eq 0 ]
