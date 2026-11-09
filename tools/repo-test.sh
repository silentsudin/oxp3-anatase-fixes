#!/usr/bin/env bash
# repo-test - one entrypoint for every offline check in this repository.
#
#   tools/repo-test.sh                 fast checks only: no network, no builds
#   tools/repo-test.sh --fast          same as the default
#   tools/repo-test.sh --full          adds the C/Decky builds and the
#                                      libfprint syntax check
#   tools/repo-test.sh --json          machine-readable result on stdout
#   tools/repo-test.sh --list          list every suite and exit
#   tools/repo-test.sh --only GLOB     run only suites whose name matches GLOB
#                                      (repeatable; shell wildcards)
#
# Exit codes: 0 = nothing failed (skips allowed and counted), 1 = at least one
# FAIL or timeout, 2 = usage/configuration error (bad option, no suite matched,
# log directory not writable, missing `timeout`).
#
# What "fast" means
#   * no network access, no package installation, no container pull;
#   * no build: the Decky bundle, the C tests and the fingerprint syntax check
#     are full-only;
#   * no writes outside the cache/log directory: Python bytecode goes to the
#     log dir, unit/hwdb scratch trees go to the log dir or $TMPDIR, and the
#     fixture-drift check compares generated data in memory instead of
#     rewriting the checked-in fixtures.
#   Full mode may write ignored build output inside the checkout
#   (tools/fingerprint/build, tools/decky-oxp3-status/{dist,build}) and reuses
#   the existing node_modules when present. It still never installs host
#   packages and never runs a network action unless OXP3_REPO_TEST_NETWORK=1
#   is set (the libfprint check is the only network-dependent suite).
#
# Every suite's stdout+stderr is captured to
#   ${OXP3_REPO_TEST_LOG_DIR:-${XDG_CACHE_HOME:-/tmp}/oxp3-repo-test}/<utc-run>/
# The run summary (table, failing command, log path) is printed at the end.
#
# Environment overrides:
#   XDG_CACHE_HOME            cache base when set (default /tmp)
#   OXP3_REPO_TEST_LOG_DIR    exact log base directory (default as above)
#   OXP3_REPO_TEST_NETWORK=1  allow the network-dependent suite (fp-libfprint)
#   TMPDIR                    scratch space for hwdb verification
#
# Suite names are stable and are what --only matches against. See
# docs/TESTING.md for the inventory, the timing tiers and how to add a suite.
set -u

export LC_ALL=C
# Never leave __pycache__ in the checkout, even for tools that import modules.
export PYTHONDONTWRITEBYTECODE=1

PROG=oxp3-repo-test
VERSION=1.0.0

self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ] && self=$resolved
fi
TOOLS_DIR=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd) || exit 2
REPO=$(CDPATH= cd -- "$TOOLS_DIR/.." && pwd) || exit 2

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }

usage() {
    awk 'NR == 1 { next } /^set -u/ { exit } /^#/ { sub(/^# ?/, ""); print }' "$self"
}

# ------------------------------------------------------------------ arguments
MODE=fast
MODE_SET=""
JSON=0
LIST=0
ONLY=()
while [ "$#" -gt 0 ]; do
    case $1 in
        --fast)
            [ "$MODE_SET" = full ] && die "--fast and --full are mutually exclusive"
            MODE=fast; MODE_SET=fast ;;
        --full)
            [ "$MODE_SET" = fast ] && die "--fast and --full are mutually exclusive"
            MODE=full; MODE_SET=full ;;
        --json) JSON=1 ;;
        --list) LIST=1 ;;
        --only)
            shift
            [ "$#" -gt 0 ] || die "--only needs a suite glob"
            ONLY+=("$1") ;;
        --only=*)
            ONLY+=("${1#*=}") ;;
        -h|--help|help) usage; exit 0 ;;
        *) printf '%s: unknown option: %s (try --help)\n' "$PROG" "$1" >&2; exit 2 ;;
    esac
    shift
done

# ------------------------------------------------------------------ helpers
have() { command -v "$1" >/dev/null 2>&1; }

# Skip a suite with an explicit, visible reason. The runner maps exit 77 to
# SKIP and copies the reason into the table/JSON - never a silent pass.
skip() { printf 'SKIP: %s\n' "$*"; exit 77; }

need() { have "$1" || skip "missing required tool: $1"; }

# Local podman image that already carries gcc+make (no pull, no dnf install).
local_build_image() {
    have podman || return 1
    podman image exists localhost/oxp3-fixes-build:f45 >/dev/null 2>&1 || return 1
    printf '%s' localhost/oxp3-fixes-build:f45
}

# Host C toolchain, or the local container image, or a SKIP.
c_toolchain() {
    if have make && { have cc || have gcc || have clang; }; then
        printf 'host'
        return 0
    fi
    local img
    img=$(local_build_image) || true
    if [ -n "$img" ]; then
        printf 'podman:%s' "$img"
        return 0
    fi
    return 1
}

# ------------------------------------------------------------------ suites
# Each suite is a function; the runner exports it and runs it in a child bash
# with a timeout. Return 0 = PASS, 77 = SKIP, anything else = FAIL.

suite_shell_syntax() {
    need bash
    need git
    cd "$REPO" || exit 1
    local n=0 bad=0 f first
    while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        first=$(head -n 1 -- "$f" 2>/dev/null || true)
        case $first in
            '#!'*bash*)
                n=$((n + 1))
                bash -n -- "$f" || { bad=$((bad + 1)); printf 'FAIL bash -n %s\n' "$f"; } ;;
            '#!'*sh*)
                n=$((n + 1))
                sh -n -- "$f" || { bad=$((bad + 1)); printf 'FAIL sh -n %s\n' "$f"; } ;;
        esac
    done < <(git ls-files -z)
    printf 'checked %d shell scripts, %d failed\n' "$n" "$bad"
    [ "$n" -gt 0 ] || skip "no tracked shell scripts found"
    [ "$bad" -eq 0 ]
}

suite_python_compile() {
    need python3
    need git
    cd "$REPO" || exit 1
    local cache=$LOGDIR/pycache
    mkdir -p "$cache" || exit 1
    local -a files=()
    local f bad=0 n=0
    while IFS= read -r -d '' f; do files+=("$f"); done < <(git ls-files -z -- '*.py')
    # Extensionless Python entry points that git ls-files '*.py' cannot see.
    for f in tools/oxp3-voltap/oxp3-voltap; do
        [ -f "$f" ] && files+=("$f")
    done
    [ "${#files[@]}" -gt 0 ] || skip "no Python files found"
    for f in "${files[@]}"; do
        n=$((n + 1))
        PYTHONPYCACHEPREFIX=$cache python3 -m py_compile -- "$f" \
            || { bad=$((bad + 1)); printf 'FAIL py_compile %s\n' "$f"; }
    done
    printf 'compiled %d Python files (bytecode cache: %s)\n' "$n" "$cache"
    [ "$bad" -eq 0 ]
}

suite_fp_python() {
    need python3
    cd "$REPO" || exit 1
    python3 -m unittest discover -s tools/fingerprint/tests -q
}

# Session crypto vectors + a 4 KiB benchmark, no device, no root.
suite_fp_crypto() {
    need python3
    cd "$REPO" || exit 1
    python3 tools/fingerprint/oxp3-fp-probe.py --crypto-selftest
}

suite_fp_fixtures() {
    need python3
    cd "$REPO" || exit 1
    # test_generated_data.py regenerates every fixture/vector in memory and
    # compares it with the checked-in copy; no file in the checkout is written.
    python3 -m unittest discover -s tools/fingerprint/tests \
        -p 'test_generated_data.py' -q
}

suite_fp_c_tests() {
    cd "$REPO" || exit 1
    local tool
    tool=$(c_toolchain) || skip "no C compiler and no local podman build image"
    case $tool in
        host)
            tools/fingerprint/tests/run-c-tests.sh ;;
        podman:*)
            PODMAN_IMAGE=${tool#podman:} tools/fingerprint/tests/run-c-tests.sh ;;
    esac
}

suite_fp_syntax() {
    cd "$REPO" || exit 1
    local tool img
    tool=$(c_toolchain) || skip "no C compiler and no local podman build image"
    case $tool in
        host)
            make -C tools/fingerprint syntax
            if [ -f /usr/include/openssl/evp.h ]; then
                make -C tools/fingerprint session-syntax
            else
                echo "session-syntax: skipped (no openssl-devel on the host)"
            fi ;;
        podman:*)
            img=${tool#podman:}
            podman run --rm --security-opt label=disable \
                -v "$REPO":/w:z -w /w/tools/fingerprint "$img" sh -c \
                'make syntax; \
                 if [ -f /usr/include/openssl/evp.h ]; then \
                     make session-syntax; \
                 else echo "session-syntax: skipped (no openssl-devel in image)"; fi' ;;
    esac
}

suite_fp_libfprint() {
    cd "$REPO" || exit 1
    local src=${LIBFPRINT_SRC:-/tmp/libfprint-1.94.9}
    [ -f "$src/libfprint/fpi-device.h" ] \
        || skip "no libfprint v1.94.9 source tree at $src (see check-libfprint.sh)"
    if have make && { have cc || have gcc || have clang; } \
        && pkg-config --exists glib-2.0 gio-2.0 gusb 2>/dev/null; then
        tools/fingerprint/tests/check-libfprint.sh
        return
    fi
    local img
    img=$(local_build_image) || true
    [ -n "$img" ] || skip "no host GLib/GUsb devel and no local podman build image"
    # The local image has no GLib/GUsb devel packages, so check-libfprint.sh
    # dnf-installs them inside the container: that is network traffic.
    [ "${OXP3_REPO_TEST_NETWORK:-0}" = 1 ] \
        || skip "container fallback needs dnf/network; set OXP3_REPO_TEST_NETWORK=1 to allow it"
    PODMAN_IMAGE=$img tools/fingerprint/tests/check-libfprint.sh
}

suite_decky_build() {
    local dir=$REPO/tools/decky-oxp3-status
    [ -f "$dir/build.sh" ] || skip "plugin build.sh is missing"
    need node
    need npm
    need python3
    cd "$dir" || exit 1
    if [ -d node_modules ]; then
        ./build.sh --no-install
        return
    fi
    # Cold checkout: use the npm cache only. A cold cache is a missing-network
    # condition and is reported as SKIP, not as a repository failure.
    local npm_log=$LOGDIR/decky-build-npm.log rc=0
    ./build.sh --offline >"$npm_log" 2>&1 || rc=$?
    cat "$npm_log"
    [ "$rc" -eq 0 ] && return 0
    if grep -qiE 'ENOTCACHED|EAI_AGAIN|ETIMEDOUT|ENOTFOUND|network|registry\.npmjs' "$npm_log"; then
        skip "npm offline install needs a warm cache/network (see $npm_log)"
    fi
    exit "$rc"
}

suite_decky_package() {
    local dir=$REPO/tools/decky-oxp3-status
    [ -f "$dir/package.py" ] || skip "package.py is missing"
    [ -f "$dir/dist/index.js" ] \
        || skip "dist/index.js is missing; run --full (decky-build) first"
    need python3
    cd "$dir" || exit 1
    python3 package.py || exit 1
    local zip
    zip=$(ls -1t build/oxp3-status-*.zip 2>/dev/null | head -n 1) || true
    [ -n "$zip" ] || { printf 'FAIL no build/oxp3-status-*.zip produced\n'; exit 1; }
    local h1 h2
    h1=$(sha256sum -- "$zip" | cut -d' ' -f1)
    python3 package.py >/dev/null || exit 1
    h2=$(sha256sum -- "$zip" | cut -d' ' -f1)
    printf 'artifact %s\nfirst  %s\nsecond %s\n' "$zip" "$h1" "$h2"
    [ "$h1" = "$h2" ] || { printf 'FAIL package is not deterministic\n'; exit 1; }
}

suite_decky_selftest() {
    local dir=$REPO/tools/decky-oxp3-status
    [ -f "$dir/main.py" ] || skip "plugin main.py is missing"
    need python3
    cd "$dir" || exit 1
    python3 main.py --selftest
}

suite_decky_cli() {
    local dir=$REPO/tools/decky-oxp3-status
    [ -f "$dir/main.py" ] || skip "plugin main.py is missing"
    need python3
    cd "$dir" || exit 1
    local out=$LOGDIR/decky-cli.json err=$LOGDIR/decky-cli.stderr rc=0
    python3 main.py --json --no-color >"$out" 2>"$err" || rc=$?
    python3 -m json.tool "$out" >/dev/null 2>&1 \
        || { printf 'FAIL CLI --json did not emit valid JSON (exit %d)\n' "$rc"; exit 1; }
    printf 'CLI: valid JSON, %s bytes, live-check exit %d (not asserted)\n' \
        "$(wc -c <"$out" | tr -d ' ')" "$rc"
    case $rc in
        0|1|2) return 0 ;;
        *) return 1 ;;
    esac
}

suite_verify_selftest() {
    [ -x "$REPO/tools/oxp3-verify.sh" ] || skip "tools/oxp3-verify.sh is missing"
    cd "$REPO" || exit 1
    tools/oxp3-verify.sh --selftest
}

suite_verify_fixtures() {
    [ -x "$REPO/tests/verify/run.sh" ] || skip "tests/verify/run.sh is missing"
    cd "$REPO" || exit 1
    tests/verify/run.sh
}

suite_rollback() {
    [ -x "$REPO/tools/oxp3-rollback.sh" ] || skip "tools/oxp3-rollback.sh is missing"
    [ -x "$REPO/tests/rollback/run.sh" ] || skip "tests/rollback/run.sh is missing"
    cd "$REPO" || exit 1
    # Installs into a --root prefix, edits the user-owned files, uninstalls and asserts a
    # byte-identical restore. No root, no systemd, no udev, nothing outside $TMPDIR.
    tests/rollback/run.sh
}

suite_installer_status() {
    [ -x "$REPO/tools/oxp3-hw-install.sh" ] || skip "tools/oxp3-hw-install.sh is missing"
    cd "$REPO" || exit 1
    tools/oxp3-hw-install.sh --status
}

suite_installer_dry_run() {
    [ -x "$REPO/tools/oxp3-hw-install.sh" ] || skip "tools/oxp3-hw-install.sh is missing"
    # The installer needs an unprivileged target user even for a dry run. A
    # container started as root with no SUDO_USER/--user cannot provide one;
    # that is an environment gap, not a repository failure.
    if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ] \
       && [ -z "${TARGET_USER:-}" ] && [ -z "${OXP3_USER:-}" ]; then
        skip "root context with no SUDO_USER/--user; installer --dry-run needs an unprivileged target user"
    fi
    cd "$REPO" || exit 1
    local out
    out=$(tools/oxp3-hw-install.sh --dry-run --all)
    printf '%s\n' "$out" | tail -n 5
    printf '%s\n' "$out" | grep -q 'preflight: skipped in --dry-run' \
        || { printf 'FAIL the dry run did not skip preflight\n'; return 1; }
    tools/oxp3-hw-install.sh --help | grep -q -- '--force' \
        || { printf 'FAIL --help does not document --force\n'; return 1; }
}

suite_unit_verify() {
    need systemd-analyze
    need sed
    local tmp=$LOGDIR/units
    rm -rf -- "$tmp"
    mkdir -p "$tmp" || exit 1
    local f b n=0 bad=0
    for f in "$REPO"/files/*.service "$REPO"/tools/oxp3-voltap/*.service \
             "$REPO"/tools/oxp3-homebtn.service "$REPO"/tools/oxp3-rgbd.service; do
        [ -f "$f" ] || continue
        b=$(basename -- "$f")
        case $b in *@.service) b=${b%@.service}@test.service ;; esac
        sed -E 's#^(ExecStart|ExecStop|ExecStartPre|ExecStartPost|ExecReload)=.*#\1=/bin/true#' \
            -- "$f" >"$tmp/$b" || exit 1
    done
    for f in "$tmp"/*.service; do
        [ -f "$f" ] || continue
        n=$((n + 1))
        b=$(basename -- "$f")
        case $b in
            oxp3-audio-route.service|oxp3-homebtn.service|oxp3-rgbd.service)
                systemd-analyze --user verify "$f" \
                    || { bad=$((bad + 1)); printf 'FAIL user unit %s\n' "$b"; } ;;
            *)
                systemd-analyze verify "$f" \
                    || { bad=$((bad + 1)); printf 'FAIL system unit %s\n' "$b"; } ;;
        esac
    done
    printf 'verified %d unit files (Exec*= stubbed), %d failed\n' "$n" "$bad"
    [ "$n" -gt 0 ] || skip "no unit files found"
    [ "$bad" -eq 0 ]
}

suite_udev_rules() {
    need udevadm
    local f n=0 bad=0
    for f in "$REPO"/files/*.rules "$REPO"/tools/*.rules; do
        [ -f "$f" ] || continue
        # files/ also carries polkit rules (the .rules suffix is required by both
        # polkit and udev). polkit's rules.d is JavaScript, so udevadm cannot parse
        # it: keep it out of this suite instead of special-casing a file name.
        if grep -q 'polkit\.addRule' -- "$f" 2>/dev/null; then
            printf 'skip  polkit rule (not a udev rule): %s\n' "${f#"$REPO"/}"
            continue
        fi
        n=$((n + 1))
        udevadm verify -- "$f" || { bad=$((bad + 1)); printf 'FAIL udevadm verify %s\n' "$f"; }
    done
    printf 'verified %d udev rules files, %d failed\n' "$n" "$bad"
    [ "$n" -gt 0 ] || skip "no .rules files found"
    [ "$bad" -eq 0 ]
}

suite_hwdb_compile() {
    need systemd-hwdb
    local f root n=0 bad=0
    for f in "$REPO"/files/*.hwdb "$REPO"/tools/oxp3-voltap/*.hwdb; do
        [ -f "$f" ] || continue
        n=$((n + 1))
        root=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-hwdb.XXXXXX") || exit 1
        mkdir -p "$root/etc/udev/hwdb.d" || { rm -rf -- "$root"; exit 1; }
        cp -- "$f" "$root/etc/udev/hwdb.d/" || { rm -rf -- "$root"; exit 1; }
        if ! systemd-hwdb --root="$root" --strict update; then
            bad=$((bad + 1)); printf 'FAIL systemd-hwdb %s\n' "$f"
        elif [ ! -s "$root/etc/udev/hwdb.bin" ]; then
            bad=$((bad + 1)); printf 'FAIL no hwdb.bin for %s\n' "$f"
        fi
        rm -rf -- "$root"
    done
    printf 'compiled %d hwdb files, %d failed\n' "$n" "$bad"
    [ "$n" -gt 0 ] || skip "no .hwdb files found"
    [ "$bad" -eq 0 ]
}

suite_shellcheck() {
    have shellcheck \
        || skip "shellcheck not installed (rpm-ostree install ShellCheck; no network used here)"
    need git
    cd "$REPO" || exit 1
    local -a files=()
    local f first
    while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        first=$(head -n 1 -- "$f" 2>/dev/null || true)
        case $first in
            '#!'*bash*|'#!'*sh*) files+=("$f") ;;
        esac
    done < <(git ls-files -z)
    [ "${#files[@]}" -gt 0 ] || skip "no shell scripts found"
    shellcheck -x "${files[@]}"
}

suite_status_selftest() {
    [ -x "$REPO/tools/oxp3-status.sh" ] || skip "tools/oxp3-status.sh is missing"
    need python3
    cd "$REPO" || exit 1
    tools/oxp3-status.sh --selftest
}

suite_led_selftest() {
    [ -f "$REPO/tools/oxp3-led.py" ] || skip "tools/oxp3-led.py is missing"
    need python3
    cd "$REPO" || exit 1
    python3 tools/oxp3-led.py selftest
}

suite_hid_selftest() {
    [ -f "$REPO/tools/oxp3_hid.py" ] || skip "tools/oxp3_hid.py is missing"
    need python3
    cd "$REPO" || exit 1
    python3 tools/oxp3_hid.py
}

suite_unit() {
    [ -d "$REPO/tests/unit" ] || skip "tests/unit is missing"
    need python3
    cd "$REPO" || exit 1
    # Pure stdlib unittest; synthetic sysfs/config fixtures only, no hardware,
    # no root, no network. Bytecode is disabled by the runner's environment.
    python3 -m unittest discover -s tests/unit -q
}

suite_gamewrap() {
    [ -x "$REPO/tools/oxp3-gamewrap.sh" ] || skip "tools/oxp3-gamewrap.sh is missing"
    need python3
    cd "$REPO" || exit 1
    # Decision-table tests: synthetic HOME/config/panel state, stub commands.
    python3 -m unittest discover -s tests/unit -p 'test_gamewrap.py' -q
    # The shell entry point's own usage and status paths.
    tools/oxp3-gamewrap.sh --help >/dev/null
    tools/oxp3-gamewrap.sh --status --json >/dev/null
}

suite_eq_model() {
    [ -f "$REPO/tools/audio/eq_response.py" ] || skip "tools/audio/eq_response.py is missing"
    need python3
    cd "$REPO" || exit 1
    python3 tools/audio/eq_response.py
}

suite_firstrun() {
    [ -x "$REPO/tools/oxp3-firstrun.sh" ] || skip "tools/oxp3-firstrun.sh is missing"
    need python3
    cd "$REPO" || exit 1
    # 1. Offline self-test: synthetic fixtures + stub commands, no host access.
    tools/oxp3-firstrun.sh --selftest
    # 2. The checklist must be read-only and emit valid JSON.
    local out=$LOGDIR/firstrun-checklist.json
    tools/oxp3-firstrun.sh --check-only --json >"$out" \
        || { printf 'FAIL --check-only --json exited non-zero\n'; exit 1; }
    python3 -m json.tool "$out" >/dev/null \
        || { printf 'FAIL --check-only --json did not emit valid JSON\n'; exit 1; }
    # 3. A dry run plans the bundle and must not create it.
    local dir=$LOGDIR/firstrun-dry
    rm -rf -- "$dir"
    tools/oxp3-firstrun.sh --dry-run --no-journal --no-repo-test --out "$dir" >/dev/null \
        || { printf 'FAIL --dry-run exited non-zero\n'; exit 1; }
    [ ! -e "$dir" ] || { printf 'FAIL dry-run wrote %s\n' "$dir"; exit 1; }
    printf 'selftest ok; checklist JSON ok; dry-run wrote nothing\n'
}

suite_wizard() {
    [ -x "$REPO/tools/oxp3-wizard.sh" ] || skip "tools/oxp3-wizard.sh is missing"
    [ -x "$REPO/tests/wizard/run.sh" ] || skip "tests/wizard/run.sh is missing"
    need python3
    cd "$REPO" || exit 1
    # 1. Offline self-test of the state machine and the JSON writer.
    tools/oxp3-wizard.sh --selftest
    # 2. The plan must list every phase and write nothing.
    local plan=$LOGDIR/wizard-plan.txt
    rm -f -- "$plan"
    tools/oxp3-wizard.sh --non-interactive --state-dir "$LOGDIR/wizard-plan" >"$plan" \
        || { printf 'FAIL --non-interactive exited non-zero\n'; exit 1; }
    grep -q '\[phase 10/10\]' "$plan" \
        || { printf 'FAIL plan did not list all phases\n'; exit 1; }
    [ ! -e "$LOGDIR/wizard-plan" ] \
        || { printf 'FAIL plan mode created %s\n' "$LOGDIR/wizard-plan"; exit 1; }
    # 3. Scripted-stdin end-to-end: answers, JSON schema, resume, restore, suspend gate.
    tests/wizard/run.sh
}

suite_triage() {
    [ -x "$REPO/tools/oxp3-triage.py" ] || skip "tools/oxp3-triage.py is missing"
    need python3
    cd "$REPO" || exit 1
    # 1. Offline self-test: the eight synthetic bundles, findings + priorities.
    tools/oxp3-triage.py --selftest
    # 2. Every fixture must be readable and emit valid JSON.
    local name out=$LOGDIR/triage
    mkdir -p "$out"
    for name in healthy preinstall bt-not-loaded xmx-no-trace audio-wedged \
                sleep-never-triggered volume-degraded missing-files; do
        tools/oxp3-triage.py --json "tests/fixtures/firstrun/$name" >"$out/$name.json" \
            || rc=$?
        # exit 1 means "a critical finding exists" - that is data, not a failure.
        if [ "${rc:-0}" -gt 1 ]; then
            printf 'FAIL triage exited %s on %s\n' "$rc" "$name"
            exit 1
        fi
        rc=0
        python3 -m json.tool "$out/$name.json" >/dev/null \
            || { printf 'FAIL triage emitted invalid JSON for %s\n' "$name"; exit 1; }
    done
    # 3. The healthy fixture must be silent, the pre-install one must not be.
    python3 - "$out" <<'PYEOF'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
healthy = json.loads((out / "healthy.json").read_text())
pre = json.loads((out / "preinstall.json").read_text())
if healthy["findings"]:
    raise SystemExit("FAIL healthy fixture produced findings: %s"
                     % [f["rule"] for f in healthy["findings"]])
rules = [f["rule"] for f in pre["findings"]]
for required in ("bt.initramfs-stale", "verify.not-installed"):
    if required not in rules:
        raise SystemExit("FAIL preinstall fixture did not report %s" % required)
states = {f["id"]: f["state"] for f in pre["fixes"]}
if states.get("volume") != "not-installed":
    raise SystemExit("FAIL preinstall volume verdict is %r" % states.get("volume"))
missing = json.loads((out / "missing-files.json").read_text())
if any(f["state"] != "unknown" for f in missing["fixes"]):
    raise SystemExit("FAIL missing-files fixture invented a non-unknown verdict")
print("selftest ok; 8 fixtures valid; healthy silent; preinstall not-installed")
PYEOF
}

suite_preflight() {
    [ -x "$REPO/tools/oxp3-preflight.sh" ] || skip "tools/oxp3-preflight.sh is missing"
    [ -d "$REPO/tests/fixtures/preflight" ] || skip "tests/fixtures/preflight is missing"
    if [ -x "$REPO/tests/preflight/run.sh" ]; then
        "$REPO/tests/preflight/run.sh"
    else
        "$REPO/tools/oxp3-preflight.sh" --selftest
        "$REPO/tools/oxp3-preflight.sh" --fixtures "$REPO/tests/fixtures/preflight"
    fi
    # A live report is data, not an assertion: the verdict depends on the host.
    local out=$LOGDIR/preflight.json
    "$REPO/tools/oxp3-preflight.sh" --json >"$out" || true
    [ -s "$out" ] || { printf 'FAIL preflight wrote no JSON\n'; return 1; }
    python3 - "$out" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
assert data.get("schema") == 1, data.get("schema")
assert data.get("tool") == "oxp3-preflight"
assert isinstance(data.get("checks"), list) and data["checks"]
assert data["counts"]["total"] == len(data["checks"])
assert data["safe"] is (data["counts"]["fail"] == 0)
PY
}

suite_gen_docs() {
    [ -f "$REPO/tools/gen-docs.py" ] || skip "tools/gen-docs.py is missing"
    [ -f "$REPO/docs/hw/components.yaml" ] || skip "docs/hw/components.yaml is missing"
    need python3
    cd "$REPO" || exit 1
    # Read-only: validates the registry and fails when a generated block in
    # README.md / docs/hw/README.md / docs/INSTALL-HW.md is stale.
    python3 tools/gen-docs.py --check
}

# ------------------------------------------------------------------ registry
# name | mode (fast/full) | tier (fast/medium/slow-network) | timeout s |
# network (0/1) | one-line description | command shown when it fails
SUITES=()
declare -A MODE_OF=() TIER_OF=() TIMEOUT_OF=() NET_OF=() DESC_OF=() CMD_OF=()

def_suite() {
    local name=$1 mode=$2 tier=$3 timeout=$4 net=$5 desc=$6 cmd=$7
    SUITES+=("$name")
    MODE_OF[$name]=$mode
    TIER_OF[$name]=$tier
    TIMEOUT_OF[$name]=$timeout
    NET_OF[$name]=$net
    DESC_OF[$name]=$desc
    CMD_OF[$name]=$cmd
}

def_suite shell-syntax fast fast 60 0 \
    "bash -n / sh -n on every tracked shell script" \
    "bash -n / sh -n (git ls-files)"
def_suite python-compile fast fast 120 0 \
    "py_compile every tracked .py plus the extensionless oxp3-voltap" \
    "PYTHONPYCACHEPREFIX=<logs>/pycache python3 -m py_compile <files>"
def_suite fp-python fast fast 120 0 \
    "fingerprint protocol + session crypto Python unittest suite (172 tests)" \
    "python3 -m unittest discover -s tools/fingerprint/tests -q"
def_suite fp-crypto fast fast 120 0 \
    "session crypto (SM2/SM3/SM4/KDF) against published vectors + benchmark" \
    "python3 tools/fingerprint/oxp3-fp-probe.py --crypto-selftest"
def_suite fp-fixtures fast fast 120 0 \
    "fingerprint fixture/vector drift, regenerated in memory" \
    "python3 -m unittest discover -s tools/fingerprint/tests -p test_generated_data.py -q"
def_suite fp-c-tests full fast 300 0 \
    "fingerprint C protocol tests (host cc or local podman image)" \
    "tools/fingerprint/tests/run-c-tests.sh"
def_suite fp-syntax full fast 180 0 \
    "compile-check the libfprint driver (plain + BF63112_ENABLE_SESSION) and the OpenSSL session module" \
    "make -C tools/fingerprint syntax session-syntax (or the same in the local podman image)"
def_suite fp-libfprint full slow-network 900 1 \
    "compile-check the driver against a real libfprint v1.94.9 tree" \
    "tools/fingerprint/tests/check-libfprint.sh"
def_suite decky-build full fast 600 0 \
    "Decky plugin: typecheck, rollup, smoke test, backend selftest" \
    "cd tools/decky-oxp3-status && ./build.sh --offline (or --no-install)"
def_suite decky-package full fast 120 0 \
    "Decky plugin package: byte-identical zip on a second run" \
    "python3 tools/decky-oxp3-status/package.py (twice)"
def_suite decky-selftest fast fast 120 0 \
    "Decky backend --selftest (synthetic /sys, /proc, HOME + stub verifier)" \
    "python3 tools/decky-oxp3-status/main.py --selftest"
def_suite decky-cli fast fast 120 0 \
    "Decky CLI --json emits valid JSON (live exit code not asserted)" \
    "python3 tools/decky-oxp3-status/main.py --json --no-color"
def_suite verify-selftest fast fast 120 0 \
    "oxp3-verify parser unit tests against synthetic fixtures (157 checks)" \
    "tools/oxp3-verify.sh --selftest"
def_suite verify-fixtures fast fast 120 0 \
    "the tests/verify/run.sh fixture runner (same selftest entrypoint)" \
    "tests/verify/run.sh"
def_suite rollback fast fast 120 0 \
    "undo round-trip under a --root prefix: install, edit, uninstall, byte-identical restore" \
    "tests/rollback/run.sh"
def_suite installer-status fast fast 120 0 \
    "hardware installer read-only status report" \
    "tools/oxp3-hw-install.sh --status"
def_suite installer-dry-run fast fast 120 0 \
    "hardware installer full dry run (every command, no write)" \
    "tools/oxp3-hw-install.sh --dry-run --all"
def_suite unit-verify fast fast 180 0 \
    "systemd-analyze verify on all units (Exec*= stubbed to /bin/true)" \
    "systemd-analyze verify <stubbed units>"
def_suite udev-rules fast fast 60 0 \
    "udevadm verify on every .rules file" \
    "udevadm verify files/*.rules tools/*.rules"
def_suite hwdb-compile fast fast 60 0 \
    "systemd-hwdb --strict compilation of every .hwdb file" \
    "systemd-hwdb --root=<tmp> --strict update"
def_suite shellcheck fast fast 180 0 \
    "shellcheck -x on every shell script (SKIP when not installed)" \
    "shellcheck -x <tracked shell scripts>"
def_suite status-selftest fast fast 120 0 \
    "headless status backend self-test (90 checks)" \
    "tools/oxp3-status.sh --selftest"
def_suite led-selftest fast fast 60 0 \
    "RGB encoder self-test against captured frames" \
    "python3 tools/oxp3-led.py selftest"
def_suite hid-selftest fast fast 60 0 \
    "vendor HID encoder self-test" \
    "python3 tools/oxp3_hid.py"
def_suite unit fast fast 120 0 \
    "offline unit tests for the Python tooling (stdlib unittest, synthetic fixtures)" \
    "python3 -m unittest discover -s tests/unit -q"
def_suite gamewrap fast fast 120 0 \
    "game launch wrapper: feature decisions, HDR gating, argv safety" \
    "python3 -m unittest discover -s tests/unit -p test_gamewrap.py -q"
def_suite eq-model fast fast 60 0 \
    "speaker EQ biquad model table" \
    "python3 tools/audio/eq_response.py"
def_suite firstrun fast medium 120 0 \
    "first-run collector: offline selftest, checklist JSON, dry-run writes nothing" \
    "tools/oxp3-firstrun.sh --selftest && ... --check-only --json && ... --dry-run"
def_suite wizard fast fast 300 0 \
    "hardware validation wizard: plan, dry-run, scripted stdin, JSON schema, resume, restore" \
    "tools/oxp3-wizard.sh --selftest && tools/oxp3-wizard.sh --non-interactive && tests/wizard/run.sh"
def_suite triage fast fast 120 0 \
    "evidence-bundle triage: rule selftest, 8 fixture bundles, JSON contract" \
    "tools/oxp3-triage.py --selftest && ... 8 fixtures --json"
def_suite preflight fast fast 120 0 \
    "preflight prerequisite validator: parser/fixture selftest + live JSON schema" \
    "tools/oxp3-preflight.sh --selftest && --fixtures tests/fixtures/preflight && --json"
def_suite gen-docs fast fast 60 0 \
    "generated component tables match docs/hw/components.yaml" \
    "python3 tools/gen-docs.py --check"

# ------------------------------------------------------------------ selection
# Fail fast on a registry typo instead of reporting a confusing FAIL at run time.
for name in "${SUITES[@]}"; do
    declare -F "suite_${name//-/_}" >/dev/null 2>&1 \
        || die "internal error: no suite function for '$name'"
done

matches_only() { # NAME
    [ "${#ONLY[@]}" -eq 0 ] && return 0
    local g
    for g in "${ONLY[@]}"; do
        # shellcheck disable=SC2053  # intentional: $g is a user glob
        [[ $1 == $g ]] && return 0
    done
    return 1
}

selected=()
for name in "${SUITES[@]}"; do
    matches_only "$name" && selected+=("$name")
done
[ "${#selected[@]}" -gt 0 ] || die "no suite matches ${ONLY[*]:-<all>} (see --list)"

# --list never creates a log directory and never runs a command.
if [ "$LIST" -eq 1 ]; then
    printf '%-19s %-5s %-12s %7s %-3s  %s\n' \
        "suite" "mode" "tier" "timeout" "net" "description"
    printf '%-19s %-5s %-12s %7s %-3s  %s\n' \
        "-------------------" "-----" "------------" "-------" "---" "-----------"
    fast_n=0 full_n=0
    for name in "${selected[@]}"; do
        [ "${MODE_OF[$name]}" = fast ] && fast_n=$((fast_n + 1))
        full_n=$((full_n + 1))
        printf '%-19s %-5s %-12s %6ss %-3s  %s\n' \
            "$name" "${MODE_OF[$name]}" "${TIER_OF[$name]}" \
            "${TIMEOUT_OF[$name]}" \
            "$( [ "${NET_OF[$name]}" = 1 ] && echo net || echo - )" \
            "${DESC_OF[$name]}"
    done
    printf '\n%d suite(s) selected: %d run in --fast, %d in --full.\n' \
        "$full_n" "$fast_n" "$full_n"
    printf 'Network-dependent suites run only with --full OXP3_REPO_TEST_NETWORK=1.\n'
    exit 0
fi

# ------------------------------------------------------------------ log dir
LOG_BASE=${OXP3_REPO_TEST_LOG_DIR:-${XDG_CACHE_HOME:-/tmp}/oxp3-repo-test}
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null) || RUN_ID=run
LOGDIR=$LOG_BASE/$RUN_ID-$$
mkdir -p -- "$LOGDIR" 2>/dev/null || die "cannot create log directory: $LOGDIR"

# The child bash needs these helpers and variables; suite functions are
# exported one at a time by the runner.
export REPO LOGDIR MODE
export OXP3_REPO_TEST_NETWORK="${OXP3_REPO_TEST_NETWORK:-0}"
export -f have skip need local_build_image c_toolchain

if ! have timeout; then
    die "the 'timeout' command (GNU coreutils) is required"
fi

# ------------------------------------------------------------------ runner
R_NAME=(); R_STATE=(); R_MS=(); R_REASON=(); R_LOG=(); R_CMD=()

now_ms() { date +%s%N 2>/dev/null | cut -c1-13; }  # ns -> ms resolution

run_suite() { # NAME
    local name=$1 fn="suite_${1//-/_}"
    local mode=${MODE_OF[$name]} timeout_s=${TIMEOUT_OF[$name]}
    local net=${NET_OF[$name]}
    local log=$LOGDIR/$name.log
    local ms=0 rc=0 state reason="" start end

    {
        printf '# suite:  %s\n' "$name"
        printf '# mode:   %s (this run: %s)\n' "$mode" "$MODE"
        printf '# command: %s\n' "${CMD_OF[$name]}"
        printf '# log:    %s\n\n' "$log"
    } >"$log"

    if [ "$mode" = full ] && [ "$MODE" = fast ]; then
        reason="needs --full (build or network-dependent)"
        printf 'SKIP: %s\n' "$reason" >>"$log"
        state=SKIP
    elif [ "$net" = 1 ] && [ "$OXP3_REPO_TEST_NETWORK" != 1 ]; then
        reason="network-dependent; set OXP3_REPO_TEST_NETWORK=1 to allow"
        printf 'SKIP: %s\n' "$reason" >>"$log"
        state=SKIP
    else
        export -f "$fn" 2>/dev/null || true
        start=$(now_ms)
        timeout --kill-after=10 "$timeout_s" bash -uc "$fn" >>"$log" 2>&1
        rc=$?
        end=$(now_ms)
        ms=$((end - start))
        if [ "$rc" -eq 0 ]; then
            state=PASS
        elif [ "$rc" -eq 77 ]; then
            state=SKIP
            reason=$(sed -n 's/^SKIP: //p' "$log" | tail -n 1)
            [ -n "$reason" ] || reason="skipped without a reason (bug in the suite)"
        else
            state=FAIL
            case $rc in
                124|137|143) reason="timeout after ${timeout_s}s" ;;
                *) reason="exit $rc" ;;
            esac
        fi
    fi

    R_NAME+=("$name"); R_STATE+=("$state"); R_MS+=("$ms")
    R_REASON+=("$reason"); R_LOG+=("$log"); R_CMD+=("${CMD_OF[$name]}")
}

for name in "${selected[@]}"; do
    run_suite "$name"
done

# ------------------------------------------------------------------ report
fmt_dur() { # MS
    printf '%d.%03ds' "$(( $1 / 1000 ))" "$(( $1 % 1000 ))"
}

trunc() { # STRING MAX
    if [ "${#1}" -le "$2" ]; then printf '%s' "$1"; else printf '%s...' "${1:0:$(( $2 - 3 ))}"; fi
}

pass_n=0 fail_n=0 skip_n=0
for i in "${!R_NAME[@]}"; do
    case ${R_STATE[$i]} in
        PASS) pass_n=$((pass_n + 1)) ;;
        FAIL) fail_n=$((fail_n + 1)) ;;
        SKIP) skip_n=$((skip_n + 1)) ;;
    esac
done
total=${#R_NAME[@]}
[ "$fail_n" -eq 0 ] && exit_code=0 || exit_code=1

if [ "$JSON" -eq 1 ]; then
    json_escape() {
        local s=$1
        s=${s//\\/\\\\}
        s=${s//\"/\\\"}
        s=$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037')
        s=${s//$'\t'/ }
        printf '%s' "$s"
    }
    printf '{\n'
    printf '  "schema": 1,\n'
    printf '  "version": "%s",\n' "$VERSION"
    printf '  "mode": "%s",\n' "$MODE"
    printf '  "repo": "%s",\n' "$(json_escape "$REPO")"
    printf '  "log_dir": "%s",\n' "$(json_escape "$LOGDIR")"
    printf '  "counts": {"total": %d, "pass": %d, "fail": %d, "skip": %d},\n' \
        "$total" "$pass_n" "$fail_n" "$skip_n"
    printf '  "exit_code": %d,\n' "$exit_code"
    printf '  "suites": [\n'
    sep=""
    for i in "${!R_NAME[@]}"; do
        printf '%s    {"name": "%s", "mode": "%s", "tier": "%s", "state": "%s", "duration_ms": %d, "command": "%s", "log": "%s", "reason": "%s"}' \
            "$sep" "$(json_escape "${R_NAME[$i]}")" \
            "$(json_escape "${MODE_OF[${R_NAME[$i]}]}")" \
            "$(json_escape "${TIER_OF[${R_NAME[$i]}]}")" \
            "$(json_escape "${R_STATE[$i]}")" "${R_MS[$i]}" \
            "$(json_escape "${R_CMD[$i]}")" \
            "$(json_escape "${R_LOG[$i]}")" \
            "$(json_escape "${R_REASON[$i]}")"
        sep=$',\n'
    done
    printf '\n  ]\n}\n'
else
    printf '%s %s  mode=%s  repo=%s\n' "$PROG" "$VERSION" "$MODE" "$REPO"
    printf '%-19s %-5s %-5s %9s  %s\n' "suite" "mode" "state" "time" "note"
    printf '%-19s %-5s %-5s %9s  %s\n' \
        "-------------------" "-----" "-----" "---------" "----"
    for i in "${!R_NAME[@]}"; do
        note=${R_REASON[$i]}
        [ "${R_STATE[$i]}" = FAIL ] && note="FAILED - see log"
        printf '%-19s %-5s %-5s %9s  %s\n' \
            "${R_NAME[$i]}" "${MODE_OF[${R_NAME[$i]}]}" "${R_STATE[$i]}" \
            "$(fmt_dur "${R_MS[$i]}")" "$(trunc "$note" 72)"
    done
    printf '\n== %d suite(s): %d pass, %d fail, %d skip -> exit %d\n' \
        "$total" "$pass_n" "$fail_n" "$skip_n" "$exit_code"
    printf '   logs: %s\n' "$LOGDIR"
    if [ "$fail_n" -gt 0 ]; then
        printf '\nFailing suites (command and log):\n'
        for i in "${!R_NAME[@]}"; do
            [ "${R_STATE[$i]}" = FAIL ] || continue
            printf '  %s\n    command: %s\n    reason:  %s\n    log:     %s\n' \
                "${R_NAME[$i]}" "${R_CMD[$i]}" "${R_REASON[$i]}" "${R_LOG[$i]}"
        done
    fi
fi

# A durable summary next to the logs, useful when stdout is a CI transcript.
{
    printf 'mode=%s repo=%s\n' "$MODE" "$REPO"
    printf 'counts: total=%d pass=%d fail=%d skip=%d exit=%d\n' \
        "$total" "$pass_n" "$fail_n" "$skip_n" "$exit_code"
    for i in "${!R_NAME[@]}"; do
        printf '%s\t%s\t%s\t%s\n' \
            "${R_NAME[$i]}" "${R_STATE[$i]}" "${R_MS[$i]}" "${R_REASON[$i]}"
    done
} >"$LOGDIR/summary.txt" 2>/dev/null || true

exit "$exit_code"
