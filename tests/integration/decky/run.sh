#!/usr/bin/env bash
# tests/integration/decky/run.sh - offline --root simulation of the Decky plugin component.
#
# Exercises the real tools/oxp3-hw-install.sh --decky path into a scratch prefix (no root,
# no systemd, no udev, no Decky): install, status, reinstall with a backup, uninstall,
# a missing artifact, a bad sha256, and a foreign directory with the same name that must be
# refused rather than deleted.
#
#   tests/integration/decky/run.sh
#   tools/repo-test.sh --only decky-install
#
# The test uses the repository's built/verified zip (build/oxp3-status-*.zip).  Without one it
# exits 77 with the exact build command, so a clean checkout reports SKIP instead of a false
# failure.  Nothing outside the temporary prefix is written and no network or root is needed.
set -euo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
hw=$repo/tools/oxp3-hw-install.sh
helper=$repo/tools/lib/decky_plugin.py
tmp=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-decky-integration.XXXXXX") || exit 1
trap 'rm -rf -- "$tmp"' EXIT HUP INT TERM

prefix=$tmp/prefix
mkdir -p "$prefix"
user=$(id -un)
user_home=$(getent passwd "$user" | cut -d: -f6)
home=$prefix$user_home
plugins=$home/homebrew/plugins
plugin=$plugins/oxp3-status
state=$home/.local/state/oxp3-fixes
backups=$state/decky-backups

artifact=""
for z in "$repo"/tools/decky-oxp3-status/build/oxp3-status-*.zip; do
    [ -f "$z" ] && { artifact=$z; break; }
done
if [ -z "$artifact" ]; then
    echo "SKIP: no built plugin artifact; run: (cd tools/decky-oxp3-status && ./build.sh --offline)"
    exit 77
fi

fail=0
ok() { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; fail=$((fail + 1)); }
check() { local label=$1; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi; }
run_install() { "$hw" --root "$prefix" --user "$user" --decky --decky-artifact "$artifact" "$@"; }
run_uninstall() { "$hw" --root "$prefix" --user "$user" --uninstall --decky "$@"; }

# --- 1. install -----------------------------------------------------------------------------
if run_install >"$tmp/install.log" 2>&1; then ok "install into the root prefix"
else bad "install into the root prefix"; sed 's/^/     /' "$tmp/install.log"; fi
check "plugin.json is installed" test -f "$plugin/plugin.json"
check "main.py is installed" test -f "$plugin/main.py"
check "dist/index.js is installed" test -f "$plugin/dist/index.js"
check "the ownership marker is installed" test -f "$plugin/.oxp3-managed"
check "the marker names the plugin" grep -qx 'plugin=oxp3-status' "$plugin/.oxp3-managed"
check "the marker records the artifact digest" grep -q '^artifact_sha256=[0-9a-f]\{64\}$' "$plugin/.oxp3-managed"
check "the installed manifest has the expected version" \
    grep -q '"version"[[:space:]]*:[[:space:]]*"1.1.0"' "$plugin/plugin.json"
check "the artifact digest was verified" grep -q 'recorded: .* (match)' "$tmp/install.log"
check "the restart command is printed instead of restarting" \
    grep -q 'sudo systemctl restart plugin_loader.service' "$tmp/install.log"

# --- 2. status ------------------------------------------------------------------------------
if "$hw" --root "$prefix" --user "$user" --status >"$tmp/status.log" 2>&1; then
    ok "status exits 0"
else
    bad "status exits 0"
fi
check "status reports the decky component as ok" grep -q '^  decky:.*state=ok' "$tmp/status.log"
check "status reports the installed version" grep -q 'version=1.1.0' "$tmp/status.log"
check "status reports the marker" grep -q 'marker=yes' "$tmp/status.log"

# --- 3. reinstall (backup created) ----------------------------------------------------------
if [ -d "$backups" ]; then bad "no backup exists before the first reinstall"; else ok "no backup before the first reinstall"; fi
if run_install >"$tmp/reinstall.log" 2>&1; then ok "reinstall into the root prefix"
else bad "reinstall into the root prefix"; sed 's/^/     /' "$tmp/reinstall.log"; fi
check "the reinstall reports the previous copy as ours" grep -q '^  previous: ours' "$tmp/reinstall.log"
check "the reinstall created a backup" grep -q '^  backup: ' "$tmp/reinstall.log"
count=$(find "$backups" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
check_eq "exactly one backup directory" 1 "$count"
backup_plugin=$(find "$backups" -mindepth 2 -maxdepth 3 -type d -name oxp3-status 2>/dev/null | head -n 1)
check "the backup contains plugin.json" test -f "$backup_plugin/plugin.json"
check "the backup contains the marker" test -f "$backup_plugin/.oxp3-managed"
check "the backup metadata names its origin" grep -q "\"origin\": \"$plugin\"" \
    "$(dirname "$backup_plugin")/backup.json"

# --- 4. uninstall ---------------------------------------------------------------------------
if run_uninstall >"$tmp/uninstall.log" 2>&1; then ok "uninstall from the root prefix"
else bad "uninstall from the root prefix"; sed 's/^/     /' "$tmp/uninstall.log"; fi
check "the plugin directory is removed" test ! -e "$plugin"
check "the plugins parent directory is kept" test -d "$plugins"
check "the backup is kept after uninstall" test -d "$backups"
if run_uninstall >"$tmp/uninstall2.log" 2>&1; then ok "a second uninstall is a no-op"
else bad "a second uninstall is a no-op"; fi
check "the second uninstall reports absent" grep -q '^  state: absent' "$tmp/uninstall2.log"

# --- 5. missing artifact --------------------------------------------------------------------
set +e
"$hw" --root "$prefix" --user "$user" --decky --decky-artifact "$tmp/does-not-exist.zip" \
    >"$tmp/missing.log" 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ]; then ok "a missing artifact is refused (rc=$rc)"; else bad "a missing artifact is refused"; fi
check "the missing artifact is named in the error" grep -q 'does-not-exist.zip' "$tmp/missing.log"
check "nothing was installed from a missing artifact" test ! -e "$plugin"

# --- 6. bad sha256 --------------------------------------------------------------------------
cp "$artifact" "$tmp/tampered.zip"
printf 'x' >>"$tmp/tampered.zip"
set +e
"$hw" --root "$prefix" --user "$user" --decky --decky-artifact "$tmp/tampered.zip" \
    >"$tmp/badsha.log" 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ]; then ok "a tampered artifact is refused (rc=$rc)"; else bad "a tampered artifact is refused"; fi
check "the digest mismatch is explained" grep -q 'digest mismatch' "$tmp/badsha.log"
check "nothing was installed from a bad artifact" test ! -e "$plugin"

# --- 7. foreign directory with the same name -------------------------------------------------
mkdir -p "$plugin"
printf '{"name": "Not Our Plugin", "author": "someone"}\n' >"$plugin/plugin.json"
printf 'keep me\n' >"$plugin/notes.txt"
set +e
run_uninstall >"$tmp/foreign-uninstall.log" 2>&1
rc_uninstall=$?
"$hw" --root "$prefix" --user "$user" --decky --decky-artifact "$artifact" \
    >"$tmp/foreign-install.log" 2>&1
rc_install=$?
set -e
if [ "$rc_uninstall" -ne 0 ]; then ok "uninstall refuses a foreign directory (rc=$rc_uninstall)";
else bad "uninstall refuses a foreign directory"; fi
check "the foreign plugin.json is untouched" grep -q 'Not Our Plugin' "$plugin/plugin.json"
check "the foreign notes are untouched" test -f "$plugin/notes.txt"
check "the refusal names the marker" grep -q '.oxp3-managed' "$tmp/foreign-uninstall.log"
if [ "$rc_install" -ne 0 ]; then ok "install refuses a foreign directory (rc=$rc_install)";
else bad "install refuses a foreign directory"; fi
check "the foreign directory survives the refused install" test -f "$plugin/notes.txt"

# --- 8. dry run mentions the component -------------------------------------------------------
if "$hw" --root "$prefix" --user "$user" --dry-run --all >"$tmp/dry.log" 2>&1; then
    ok "--dry-run --all still plans cleanly"
else
    bad "--dry-run --all still plans cleanly"
fi
check "--dry-run --all includes the decky component" grep -q 'Decky status plugin' "$tmp/dry.log"
check "--dry-run --all does not build or write" test ! -e "$plugin/.oxp3-managed"

printf '\n%s: %d assertion(s) failed\n' "${0##*/}" "$fail"
[ "$fail" -eq 0 ]
