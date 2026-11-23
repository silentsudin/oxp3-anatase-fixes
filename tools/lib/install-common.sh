#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# tools/lib/install-common.sh - the build/install helpers shared by oxp3-fixes.sh and
# oxp3-fixes-fast.sh. Sourced by both; it holds every helper that was byte-identical in the two
# installers, so a fix to the shared path is made once. The two scripts keep their own
# configuration, their own Mesa pipeline and their own install/uninstall flow; the reasoning and
# the classification of every function are in docs/MAINTAINABILITY.md.
#
# CONTRACT (interface version 1)
# ------------------------------
# The caller MUST:
#   1. run under bash with `set -euo pipefail` before sourcing this file. This file does not
#      change shell options, so a caller that forgets them gets different failure behaviour;
#   2. set OXP3_INSTALL_COMMON_API=1 (the version below). A mismatch is refused by the guard;
#   3. set the configuration variables listed in install_common_guard() before it calls a
#      function. The values are read when a function runs, not when this file is sourced, so the
#      definitions may appear after the `source` line;
#   4. define build_mesa() itself - the one genuinely different build step (RPM packaging in
#      oxp3-fixes.sh, podman/meson in oxp3-fixes-fast.sh). It is the library's only caller hook;
#   5. call install_common_guard() once, after its configuration and local functions are defined
#      and before it dispatches a command. The guard refuses a caller with a wrong API version, a
#      missing configuration variable or a missing build_mesa().
#
# SOURCING SIDE EFFECTS: none. This file defines functions only: it sets no shell options, no
# variables, no traps, no umask and no locale, it runs no command, reads no file and prints
# nothing. Paths are always taken from the caller's variables (its `here`, $CACHE, $OUT, ...), so
# it can be sourced from any working directory.
#
# SIDE EFFECTS OF THE FUNCTIONS (grouped; each function documents anything unusual):
#   build_*, fetch_*, seed_*   network downloads into $CACHE (verified against pinned hashes),
#                              build trees in $CACHE/rpmbuild, packages in $OUT, podman
#                              images/containers named by the caller. No root, no host change.
#   install_*, do_mok_setup    root writes through sudo: /etc, /usr, /usr/local, /run and
#                              rpm-ostree layering; /usr overlay via rpm-ostree usroverlay.
#   oxp3_* (undo section)      user-owned state only: <state>/backups/<stamp>/<key> and
#                              <state>/manifest.tsv, plus the restore target the caller names.
#                              Never /etc, never the checkout. docs/ROLLBACK.md is the contract.
#   status_*, do_status        read-only: rpm, rpm-ostree --booted, systemctl is-*, /sys, /run.
#   remove_*, uninstall_*      undo the corresponding install_*; idempotent, tolerate absence.
#
# IDEMPOTENCY: every install path checks the installed state first, so re-running an install is
# safe and a second run changes nothing it already applied (that is what the QA --root cycle
# relies on). Builds overwrite their own staging directories. do_mok_setup keeps an existing
# key that the machine keyring accepts and only replaces one it would reject. Uninstall paths
# tolerate missing files and units.
#
# LOCKING: this library takes no lock, and neither installer did before the extraction.
# Concurrent runs would share $CACHE/$OUT and the caller's podman container names; run one
# build/install at a time. A caller that needs more may take its own lock before calling in -
# no function here waits for, creates or removes one.
#
# LOGGING: say() prints "\n== <message>" to stdout as a phase heading; die() prints
# "error: <message>" to stderr and exits 1; the helpers print indented detail lines to stdout
# and warnings to stderr. Nothing is written to a log file or to syslog from here.
#
# COMPATIBILITY: bash, because the two installers already require bash features ([[ ]], arrays,
# process substitution, local). It is not POSIX sh. tools/oxp3-hw-install.sh intentionally does
# not use it (it orchestrates the component installers instead of reusing their helpers); the one
# other sourcer is tools/oxp3-rollback.sh, which uses only the oxp3_* undo section above, takes no
# caller configuration and therefore does not call the guard.

# Fail fast if the caller does not satisfy the contract above. Called by the caller once,
# immediately before its command dispatch; never from this file at source time.
install_common_guard() {
    local want=1 v n
    if [[ ${OXP3_INSTALL_COMMON_API:-} != "$want" ]]; then
        die "install-common API mismatch: ${0##*/} declares OXP3_INSTALL_COMMON_API=${OXP3_INSTALL_COMMON_API:-<unset>}, this library is version $want (update the caller or the library)"
    fi
    for v in here CACHE OUT MOK_DIR ANATASE_REPO PATCHWORK_REPO FEDORA IMAGE CTR KMOD_NAME STAMP \
             SKIP_KERNEL SKIP_GAMESCOPE SKIP_MANGOHUD SKIP_FIRMWARE SKIP_DMC SKIP_MESA SKIP_XESS \
             SKIP_FLATPAK_GL MESA_COMMIT MESA_URL \
             XESS_COMMIT XESS_SRC_URL XESS_ZIP_URL XESS_ZIP_SHA XESS_DLLS XESS_PKG \
             FIRMWARE FW_NAME HHD_PATCHES VOLUME_HWDB FLATPAK_RT FLATPAK_MESA_LIST \
             FLATPAK_RT_FILES EXTRA_UNITS; do
        [[ -v $v ]] || die "install-common contract: ${0##*/} must set \$$v before using the shared helpers"
    done
    for v in KMODS FIRMWARE HHD_PATCHES EXTRA_UNITS; do
        eval "n=\${#$v[@]}"
        (( n > 0 )) || die "install-common contract: ${0##*/} must set a non-empty \$$v array"
    done
    declare -F build_mesa >/dev/null ||
        die "install-common contract: ${0##*/} must define build_mesa() (the caller-specific Mesa build)"
}

# ---------------------------------------------------------------------------------------------
# shell primitives
# ---------------------------------------------------------------------------------------------

say() { printf '\n== %s\n' "$*"; }

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Run one long build command with a visible heartbeat instead of a silent screen.
# The command's output goes to <log>; a ticker shows elapsed time and the last log line.
# Without a terminal (CI, pipes, tool calls) it behaves like a plain redirect, so logs stay clean.
progress_run() { # <label> <log> <command...>
    local label=$1 log=$2; shift 2
    if [[ ! -t 2 ]]; then
        "$@" > "$log" 2>&1
        return $?
    fi
    local start=$SECONDS pid rc elapsed line bar lastline=""
    "$@" > "$log" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        elapsed=$(( SECONDS - start ))
        line=$(tail -n 1 "$log" 2>/dev/null | tr '\r' ' ' | tr -s ' ')
        bar=""
        if [[ $line =~ \[[[:space:]]*([0-9]+)/([0-9]+)\] ]]; then
            local done=${BASH_REMATCH[1]} total=${BASH_REMATCH[2]} pct filled fill
            (( total > 0 )) && pct=$(( done * 100 / total )) || pct=0
            filled=$(( pct / 5 ))
            fill=$(printf '%*s' "$filled" '' | tr ' ' '#')
            bar=$(printf '[%-20s] %3d%% ' "$fill" "$pct")
        fi
        if [[ $line != "$lastline" ]]; then
            lastline=$line
            line=$(printf '%s' "$line" | cut -c1-100)
        else
            line=""
        fi
        printf '\r\033[K   [%3d:%02d] %s%s%s' \
            $(( elapsed / 60 )) $(( elapsed % 60 )) "$bar" "$label" "${line:+ - $line}" >&2
        sleep 5
    done
    if wait "$pid"; then rc=0; else rc=$?; fi
    printf '\r\033[K' >&2
    return "$rc"
}

# Per-component build cache: <OUT>/.cache/<name> holds the hash of every input that changes the
# produced RPMs. A rerun with the same inputs skips the expensive build but never reuses output
# from different patches, packaging or container image.
cache_valid() { # <name> <key>
    local f="$OUT/.cache/$1"
    [[ -f $f && $(<"$f") == "$2" ]]
}
cache_store() { # <name> <key>
    mkdir -p "$OUT/.cache"
    printf '%s\n' "$2" > "$OUT/.cache/$1"
}

# The Anatase kernel in the current deployment (not necessarily the booted one).
anatase_kver() { rpm -q kernel-core --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' | head -1; }

# ---------------------------------------------------------------------------------------------
# undo state: a timestamped backup and manifest row for every file we change in place
# ---------------------------------------------------------------------------------------------
# This is the reverse half of an installer. A file we *create* needs no copy (its undo is a
# removal); a file we *modify* has content the user may want back, so it is copied first. The
# scheme is plain files under one state directory, so an undo survives a reboot and a new
# checkout:
#
#   <state>/backups/<UTC stamp>[-N]/<key>  the content the file had before we touched it
#                                          (cp -a: bytes, mode and mtime are preserved)
#   <state>/manifest.tsv                   append-only log, one row per edit:
#     ts  tool  event  key  logical  real  post_sha  note
#
# event is one of
#   backup   the file existed; its previous content is at backups/<ts>/<key>
#   created  the file did not exist; undo removes it
#   adopted  a pre-existing edit of ours (made by hand before this mechanism existed); the
#            previous content comes from a sibling .bak* when one exists, otherwise the row
#            only records that the file is ours (restore is then a no-op, reported as such)
#   restored a restore we performed (post_sha is what we wrote back)
#   removed  a removal we performed for a file we had created
#
# key is "home/<rel>" for a file under the target user's home and "abs/<path>" otherwise. An
# absolute $OXP3_ROOT prefix is stripped before the mapping and $OXP3_HOME overrides the home
# that the key is relative to, so a `--root DIR` simulation produces the same keys as the host
# and a restore rebuilds the destination from today's prefix/HOME instead of a stale path.
#
# State directory: $OXP3_STATE_DIR, else <SUDO_USER home>/.local/state/oxp3-fixes when running
# as root from a sudo session, else /var/lib/oxp3-fixes when root, else
# ${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-fixes. Never /etc, never the checkout.
#
# Restore rules (never lose user data):
#   current == the pre-image       -> nothing to do (already original)
#   current == what we wrote       -> put the backup back, byte-identical
#   current is anything else       -> the user edited after us: leave it, report it; --force
#                                     first saves the user's version as a new backup, then
#                                     restores the original
# A file we created is removed only while it still matches what we wrote.
#
# The functions below take every path as an argument, print status lines and return codes
# (0 changed, 10 nothing to do, 12 skipped because the user edited, 13 no record, 14 failed).
# They create files only under the state directory (plus the restore target), so sourcing this
# file is still free of side effects and a dry run is possible through oxp3_restore_file's
# --dry-run.

oxp3_sha() {
    [[ -f $1 ]] || return 0
    sha256sum -- "$1" 2>/dev/null | cut -d' ' -f1
}

oxp3_state_dir() {
    local h=""
    if [[ -n ${OXP3_STATE_DIR:-} ]]; then printf '%s' "$OXP3_STATE_DIR"; return 0; fi
    if (( EUID == 0 )); then
        if [[ -n ${SUDO_USER:-} && ${SUDO_USER:-} != root ]]; then
            h=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6) || h=""
        fi
        if [[ -n $h ]]; then printf '%s' "$h/.local/state/oxp3-fixes"; else printf '%s' /var/lib/oxp3-fixes; fi
    else
        printf '%s' "${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-fixes"
    fi
}

# Create the state tree and print its path. As root from a sudo session the new entries are
# given back to $SUDO_USER, so the next unprivileged run can keep appending to the manifest.
oxp3_state_init() {
    local st=${1:-$(oxp3_state_dir)} owner=""
    mkdir -p -- "$st/backups" 2>/dev/null || return 1
    [[ -e $st/manifest.tsv ]] || : >"$st/manifest.tsv" 2>/dev/null || return 1
    (( EUID == 0 )) && [[ -n ${SUDO_USER:-} && ${SUDO_USER:-} != root ]] && owner=$SUDO_USER
    if [[ -n $owner ]]; then
        chown -R -- "$owner": "$st" 2>/dev/null || true
    fi
    printf '%s' "$st"
}

oxp3_manifest_path() { printf '%s/manifest.tsv' "$(oxp3_state_dir)"; }

oxp3_stamp() { date -u +%Y%m%dT%H%M%SZ; }

# Map an absolute path to its store key (the simulation prefix is stripped first).
oxp3_key() {
    local p=${1%/} root=${OXP3_ROOT:-} home=${OXP3_HOME:-$HOME}
    [[ -n $root ]] && p=${p#"$root"}
    case $p in
        "$home"/*) printf 'home/%s' "${p#"$home"/}" ;;
        *) printf 'abs/%s' "${p#/}" ;;
    esac
}

# Copy the pre-image of PATH into a fresh backup directory.
# Prints "<stamp>\t<key>\t<pre_sha>"; pre_sha is empty when PATH did not exist.
oxp3_backup_image() { # PATH [TOOL]
    local path=$1 st base ts key dir n=2 pre=""
    st=$(oxp3_state_init) || return 1
    base=$(oxp3_stamp); ts=$base
    dir="$st/backups/$ts"
    while [[ -e $dir ]]; do ts="$base-$n"; dir="$st/backups/$ts"; n=$((n + 1)); done
    key=$(oxp3_key "$path")
    mkdir -p -- "$dir/$(dirname -- "$key")" || return 1
    if [[ -f $path ]]; then
        cp -a -- "$path" "$dir/$key" || return 1
        pre=$(oxp3_sha "$dir/$key")
    fi
    printf '%s\t%s\t%s\n' "$ts" "$key" "$pre"
}

# Copy an explicit earlier version (a sibling .bak file) as the pre-image of PATH: used to
# adopt an edit that was made before the manifest existed.
oxp3_adopt_image() { # PATH PREIMAGE
    local path=$1 pre=$2 st base ts key dir n=2 sha=""
    st=$(oxp3_state_init) || return 1
    base=$(oxp3_stamp); ts=$base
    dir="$st/backups/$ts"
    while [[ -e $dir ]]; do ts="$base-$n"; dir="$st/backups/$ts"; n=$((n + 1)); done
    key=$(oxp3_key "$path")
    mkdir -p -- "$dir/$(dirname -- "$key")" || return 1
    if [[ -f $pre ]]; then
        cp -a -- "$pre" "$dir/$key" || return 1
        sha=$(oxp3_sha "$dir/$key")
    fi
    printf '%s\t%s\t%s\n' "$ts" "$key" "$sha"
}

# Append one row to the manifest. event=backup when a pre-image exists for the stamp, else the
# caller chooses (created/adopted). post_sha is the content PATH has now ("-" when absent).
oxp3_manifest_record() { # PATH TOOL STAMP EVENT [NOTE]
    local path=$1 tool=${2:-${0##*/}} ts=$3 event=$4 note=${5:-} st key dir post logical
    [[ -n $ts ]] || return 1
    st=$(oxp3_state_dir)
    [[ -f $st/manifest.tsv ]] || return 1
    key=$(oxp3_key "$path")
    dir="$st/backups/$ts"
    if [[ $event == auto ]]; then
        if [[ -f $dir/$key ]]; then event=backup; else event=created; fi
    fi
    post=$(oxp3_sha "$path"); [[ -n $post ]] || post=-
    logical=$path
    [[ -n ${OXP3_ROOT:-} ]] && logical=${path#"${OXP3_ROOT}"}
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$ts" "$tool" "$event" "$key" "$logical" "$path" "$post" "$note" >>"$st/manifest.tsv"
}

# Manifest rows for one key (oldest first); empty when there is no record.
oxp3_rows_for_key() { # KEY
    local st; st=$(oxp3_state_dir)
    [[ -f $st/manifest.tsv ]] || return 0
    awk -F'\t' -v k="$1" '$4 == k' "$st/manifest.tsv"
}

# Every key that has a record, in install order (oldest first).
oxp3_manifest_keys() {
    local st; st=$(oxp3_state_dir)
    [[ -f $st/manifest.tsv ]] || return 0
    awk -F'\t' 'NF >= 4 && $4 != "" && !seen[$4]++ { print $4 }' "$st/manifest.tsv"
}

oxp3_record_restored() { # PATH STAMP CONTENT_SHA
    local path=$1 ts=$2 st; st=$(oxp3_state_dir)
    [[ -f $st/manifest.tsv ]] || return 1
    printf '%s\t%s\trestored\t%s\t%s\t%s\t%s\t%s\n' \
        "$ts" "oxp3-rollback" "$(oxp3_key "$path")" "$path" "$path" "${3:--}" \
        >>"$st/manifest.tsv"
}

# Put back the newest recorded pre-image of PATH. Prints one status line and returns
# 0 changed, 10 nothing to do, 12 user edited after us (skipped), 13 no record, 14 failed.
oxp3_restore_file() { # PATH [--force] [--dry-run]
    local path=$1 force=0 dry=0 a st key rows wrow last row ts event post pre cur store dir rc=0
    shift || true
    for a in "$@"; do
        case $a in
            --force) force=1 ;;
            --dry-run) dry=1 ;;
        esac
    done
    st=$(oxp3_state_dir); key=$(oxp3_key "$path")
    rows=$(oxp3_rows_for_key "$key")
    if [[ -z $rows ]]; then
        printf 'no-record\t%s\tno manifest entry for %s\n' "$path" "$key"
        return 13
    fi
    wrow=$(printf '%s\n' "$rows" | awk -F'\t' '$3 == "backup" || $3 == "created" || $3 == "adopted"' | tail -n 1)
    if [[ -z $wrow ]]; then
        printf 'noop\t%s\talready restored (no edit record left)\n' "$path"
        return 10
    fi
    ts=$(printf '%s' "$wrow" | cut -f1)
    event=$(printf '%s' "$wrow" | cut -f3)
    post=$(printf '%s' "$wrow" | cut -f7)
    dir="$st/backups/$ts"; store="$dir/$key"
    # A later restore/removal row means the last thing we did is already the final state.
    last=$(printf '%s\n' "$rows" | tail -n 1)
    if [[ $(printf '%s' "$last" | cut -f3) == restored && $(printf '%s' "$last" | cut -f1) != $ts ]]; then
        local lastsha cur2
        lastsha=$(printf '%s' "$last" | cut -f7); cur2=$(oxp3_sha "$path"); [[ -n $cur2 ]] || cur2=-
        if [[ $lastsha == "$cur2" ]]; then
            printf 'noop\t%s\talready restored (content matches the recorded restore)\n' "$path"
            return 10
        fi
    fi
    cur=$(oxp3_sha "$path")
    if [[ $event == created ]]; then
        if [[ -z $cur ]]; then
            printf 'noop\t%s\talready absent\n' "$path"
            return 10
        fi
        if [[ $post != - && $cur != "$post" ]]; then
            if (( !force )); then
                printf 'skipped\t%s\tcreated by us but edited since (current %.12s, recorded %.12s); nothing removed\n' \
                    "$path" "$cur" "$post"
                return 12
            fi
            local rok; rok=$(oxp3_backup_image "$path" oxp3-rollback) || true
            oxp3_manifest_record "$path" oxp3-rollback "${rok%%$'\t'*}" backup \
                "user version saved before a forced restore" 2>/dev/null || true
        fi
        if (( dry )); then
            printf 'would-remove\t%s\twe created it\n' "$path"
            return 0
        fi
        if ! rm -f -- "$path"; then
            printf 'failed\t%s\tcould not remove (permissions? run with sudo)\n' "$path"
            return 14
        fi
        oxp3_manifest_record "$path" oxp3-rollback "$(oxp3_stamp)" removed "removed a file we created"
        printf 'removed\t%s\twe had created it\n' "$path"
        return 0
    fi
    if [[ ! -f $store ]]; then
        printf 'noop\t%s\tadopted edit with no earlier image; nothing to restore\n' "$path"
        return 10
    fi
    pre=$(oxp3_sha "$store")
    if [[ -n $cur && $cur == "$pre" ]]; then
        printf 'noop\t%s\talready the backed-up content\n' "$path"
        return 10
    fi
    if [[ -n $cur && $post != - && $cur != "$post" ]]; then
        if (( !force )); then
            printf 'skipped\t%s\tuser edited after us (current %.12s, ours %.12s); not overwritten\n' \
                "$path" "$cur" "$post"
            return 12
        fi
        local rok; rok=$(oxp3_backup_image "$path" oxp3-rollback) || true
        oxp3_manifest_record "$path" oxp3-rollback "${rok%%$'\t'*}" backup \
            "user version saved before a forced restore" 2>/dev/null || true
    fi
    if (( dry )); then
        printf 'would-restore\t%s\tfrom backups/%s\n' "$path" "$ts"
        return 0
    fi
    mkdir -p -- "$(dirname -- "$path")" 2>/dev/null || true
    if ! cp -a -- "$store" "$path"; then
        printf 'failed\t%s\tcould not restore (permissions? run with sudo)\n' "$path"
        return 14
    fi
    oxp3_record_restored "$path" "$(oxp3_stamp)" "$(oxp3_sha "$path")"
    printf 'restored\t%s\tfrom backups/%s (%s)\n' "$path" "$ts" "$event"
    return 0
}

# Restore every recorded path, newest install first, and print a summary.
# Returns 0 when everything is restored or already original, 3 when at least one path was
# skipped because the user edited it after us, 1 on a hard failure.
oxp3_restore_all() { # [--force] [--dry-run] [--only SUBSTRING]
    local force=0 dry=0 only="" keys=() i changed=0 noop=0 skipped=0 failed=0 line code kind
    local -a opts=()
    while (( $# )); do
        case $1 in
            --force) force=1; shift ;;
            --dry-run) dry=1; shift ;;
            --only) only=${2:-}; shift 2 || shift ;;
            --only=*) only=${1#--only=}; shift ;;
            *) shift ;;
        esac
    done
    (( force )) && opts+=(--force)
    (( dry )) && opts+=(--dry-run)
    mapfile -t keys < <(oxp3_manifest_keys)
    for (( i=${#keys[@]} - 1; i >= 0; i-- )); do
        if [[ -n $only && ${keys[i]} != *"$only"* ]]; then continue; fi
        if line=$(oxp3_restore_file "$(oxp3_logical_to_real "${keys[i]}")" ${opts[@]+"${opts[@]}"}); then
            code=0
        else
            code=$?
        fi
        printf '%s\n' "$line"
        kind=${line%%$'\t'*}
        case $kind in
            restored|would-restore|removed|would-remove) changed=$((changed + 1)) ;;
            noop|no-record) noop=$((noop + 1)) ;;
            skipped) skipped=$((skipped + 1)) ;;
            *) failed=$((failed + 1)) ;;
        esac
        (( code == 14 )) && failed=$((failed + 1))
    done
    printf 'undo: %d changed, %d already done, %d skipped (user edits), %d failed\n' \
        "$changed" "$noop" "$skipped" "$failed"
    (( failed )) && return 1
    (( skipped )) && return 3
    return 0
}

# Rebuild the real path of a key from today's prefix/HOME.
oxp3_logical_to_real() { # KEY
    local key=$1 root=${OXP3_ROOT:-} home=${OXP3_HOME:-$HOME}
    case $key in
        home/*) printf '%s%s/%s' "$root" "$home" "${key#home/}" ;;
        abs/*) printf '%s/%s' "$root" "${key#abs/}" ;;
        *) printf '%s' "$key" ;;
    esac
}

# ---------------------------------------------------------------------------------------------
# build container: every build step runs inside podman, so the host needs no build toolchain
# ---------------------------------------------------------------------------------------------

ctr() { podman exec -w "$CACHE" "$CTR" "$@"; }

ctr_root() { podman exec -u 0 -w "$CACHE" "$CTR" "$@"; }

ensure_container() {
    mkdir -p "$CACHE" "$OUT"
    # rebuild the image when the Containerfile changes
    local sum; sum=$(sha256sum "$here/container/Containerfile" | cut -c1-12)
    if [[ $(podman image inspect "$IMAGE" --format '{{index .Labels "oxp3.containerfile"}}' 2>/dev/null) != "$sum" ]]; then
        podman rm -f "$CTR" >/dev/null 2>&1 || true
        podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    fi
    if ! podman image exists "$IMAGE"; then
        say "creating build image $IMAGE (once, a few minutes)"
        podman build --build-arg FEDORA="$FEDORA" --label oxp3.containerfile="$sum" -t "$IMAGE" \
            -f "$here/container/Containerfile" "$here/container"
    fi
    # recreate the container if it was made with another MOK directory
    if podman container exists "$CTR" &&
       ! podman inspect "$CTR" --format '{{range .Mounts}}{{.Destination}} {{end}}' | grep -qF "$MOK_DIR"; then
        podman rm -f "$CTR" >/dev/null
    fi
    if ! podman container exists "$CTR"; then
        podman run -d --name "$CTR" --userns=keep-id --security-opt label=disable \
            -v "$CACHE:$CACHE" -v "$here:$here:ro" -v "$MOK_DIR:$MOK_DIR:ro" \
            "$IMAGE" sleep infinity >/dev/null 2>&1 || {
            # MOK dir may not exist when only userspace parts are built
            podman run -d --name "$CTR" --userns=keep-id --security-opt label=disable \
                -v "$CACHE:$CACHE" -v "$here:$here:ro" "$IMAGE" sleep infinity >/dev/null; }
    fi
    podman start "$CTR" >/dev/null
}

# ---------------------------------------------------------------------------------------------
# gamescope / MangoHud: Anatase's packaging + our patches, rebuilt as RPMs
# ---------------------------------------------------------------------------------------------

# The anatase-org/anatase commit the booted image was built from (its OCI
# org.opencontainers.image.revision label); empty when the image carries none.
anatase_image_revision() {
    rpm-ostree status --booted --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = [x for x in json.load(sys.stdin)["deployments"] if x.get("booted")][0]
    meta = d.get("base-commit-meta") or {}
    for key in ("ostree.manifest", "ostree.container.image-config"):
        raw = meta.get(key)
        if not raw:
            continue
        m = json.loads(raw)
        labels = dict(m.get("annotations") or {}, **((m.get("config") or {}).get("Labels") or {}))
        rev = labels.get("org.opencontainers.image.revision", "")
        if len(rev) == 40 and all(c in "0123456789abcdef" for c in rev):
            print(rev); break
except Exception:
    pass' 2>/dev/null || true
}

# Anatase's packaging at the commit the BOOTED IMAGE was built from, not the repo tip: our RPMs
# must link against the libraries that image ships. On 2026-11-21 the tip moved gamescope to an0.8
# (wlroots 0.20, libliftoff), which the Stable image 20261109.7 lacks: that RPM failed rpm's
# dependency check, and the boot-time keep unit would have extracted it anyway.
fetch_anatase() {
    local rev what
    rev=$(anatase_image_revision)
    if [[ ! -d $CACHE/anatase/.git ]]; then
        git init -q "$CACHE/anatase"
        git -C "$CACHE/anatase" remote add origin "$ANATASE_REPO"
    fi
    if [[ -n $rev ]]; then
        git -C "$CACHE/anatase" fetch -q --depth 1 origin "$rev" ||
            die "cannot fetch Anatase packaging $rev (the booted image's revision) from $ANATASE_REPO"
        what="booted image's revision"
    else
        echo "  WARNING: the booted image names no packaging revision; building from the tip of $ANATASE_REPO" >&2
        git -C "$CACHE/anatase" fetch -q --depth 1 origin HEAD
        what="repository tip"
    fi
    git -C "$CACHE/anatase" reset -q --hard FETCH_HEAD
    echo "anatase packaging: $(git -C "$CACHE/anatase" log -1 --format='%h %cd %s' --date=short) ($what)"
}

# Add Patch<N> lines for our patches to an Anatase spec, after its last Patch line (or its last
# Source line), numbered from 2000 - or above the spec's highest number - so %autosetup/%autopatch
# apply them after the packaging's own patches, in file order. This used to anchor on a specific
# "overrides.patch" line, which Anatase dropped from gamescope.spec with the an0.8 rebase
# (2026-11-21). Refuses a spec whose %prep does not apply patches automatically: our Patch lines
# would then be silently ignored.
spec_add_patches() { # <spec> <patch basenames...>
    python3 - "$@" <<'PYEOF'
import re, sys
path, names = sys.argv[1], sys.argv[2:]
s = open(path).read()
autosetup = re.search(r'(?m)^%autosetup\b.*$', s)
if not re.search(r'(?m)^%autopatch\b', s) and not (autosetup and not re.search(r'\s-N\b', autosetup.group(0))):
    sys.exit(f"{path}: %prep applies no patches through %autosetup/%autopatch - Anatase packaging changed")
# existing patch numbers; an unnumbered "Patch:" takes the previous number + 1 (first is 0)
nums, last = [], -1
for m in re.finditer(r'(?m)^Patch(\d*):', s):
    last = int(m.group(1)) if m.group(1) else last + 1
    nums.append(last)
start = max([2000] + [n + 1 for n in nums])
add = "".join(f"Patch{start + i}: {n}\n" for i, n in enumerate(names))
anchors = list(re.finditer(r'(?m)^Patch\d*:.*\n', s)) or list(re.finditer(r'(?m)^Source\d*:.*\n', s))
if not anchors:
    sys.exit(f"{path}: no Patch or Source line - Anatase packaging changed")
pos = anchors[-1].end()
open(path, 'w').write(s[:pos] + add + s[pos:])
PYEOF
}

# Would these RPMs install on the booted system? rpm's own transaction check against a copy of
# the host rpmdb (no root, no lock). Catches a build that linked against libraries the image
# lacks - e.g. the shared build container gaining wlroots 0.20 from another spec's builddep -
# before such an RPM reaches `install` or the boot-time keep units.
rpms_fit_host() { # <rpm files...>
    local db rc=0
    db=$(mktemp -d "${TMPDIR:-/tmp}/oxp3-rpmdb.XXXXXX")
    if cp -a "$(rpm -E %_dbpath)/." "$db/"; then
        rpm --dbpath "$db" -U --test --replacepkgs --oldpackage --nosignature "$@" || rc=1
    else
        rc=1
    fi
    rm -rf "$db"
    return "$rc"
}

# build_card <name> <patch dir>
# The patches are <patch dir>/<spec Version>/*.patch when that directory exists (gamescope: one
# set per Anatase gamescope version, so an image update picks the matching set), otherwise
# <patch dir>/*.patch. A version with neither stops the build instead of shipping it unpatched.
build_card() {
    local name=$1 pdir=$2
    local top="$CACHE/rpmbuild/$name" spec key ver
    ver=$(sed -n 's/^Version:[[:space:]]*//p' "$CACHE/anatase/cards/gaming/$name/$name.spec" | head -1)
    local patches=()
    if [[ -d $pdir/$ver ]]; then
        patches=( "$pdir/$ver"/*.patch )
    else
        shopt -s nullglob; patches=( "$pdir"/*.patch ); shopt -u nullglob
        if (( ! ${#patches[@]} )); then
            local have; have=$(cd "$pdir" && ls -d */ 2>/dev/null | tr -d / | tr '\n' ' ')
            die "$name: Anatase's packaging is $name $ver, but $pdir has patches only for: ${have:-none}. Rebase them to $ver (as $pdir/$ver/)."
        fi
    fi
    set -- "${patches[@]}"
    key=$( { git -C "$CACHE/anatase" rev-parse HEAD 2>/dev/null || true
             find "$CACHE/anatase/cards/gaming/$name" -type f -print 2>/dev/null | sort | xargs -r sha256sum
             sha256sum "$@" 2>/dev/null || true
             sha256sum "$here/container/Containerfile" 2>/dev/null || true
           } | sha256sum | cut -c1-32 )
    if cache_valid "$name" "$key" && compgen -G "$OUT/$name-*.rpm" >/dev/null; then
        echo "  $name: build cached (${key:0:8})"; return 0
    fi
    rm -rf "$top"; mkdir -p "$top/SOURCES"
    cp -r "$CACHE/anatase/cards/gaming/$name/." "$top/SOURCES/"
    spec="$top/SOURCES/$name.spec"
    # our patches after Anatase's own, in order
    local p names=()
    for p in "$@"; do
        cp "$p" "$top/SOURCES/"; names+=( "$(basename "$p")" )
    done
    spec_add_patches "$spec" "${names[@]}" || die "$name: could not add our patches to $name.spec"
    local inst; inst=$(rpm -q "$name" --qf '%{VERSION}\n' 2>/dev/null | head -1 || true)
    [[ -z $inst || $inst == "$ver" ]] || echo "note: installed $name $inst, Anatase packaging has $ver"

    say "$name $ver: sources"
    ctr spectool -g -C "$top/SOURCES" "$spec" >/dev/null
    ctr_root dnf -y -q builddep "$spec" >/dev/null 2>&1 || ctr_root dnf -y builddep --skip-unavailable "$spec" | tail -3
    say "$name $ver: x86_64"
    progress_run "$name $ver: x86_64 build" "$top/build.log" \
        ctr rpmbuild --define "_topdir $top" --define "dist .fc$FEDORA.oxp3.$STAMP" -bb "$spec" ||
        { tail -30 "$top/build.log"; die "$name x86_64 build failed (full log: $top/build.log)"; }
    say "$name $ver: i686"
    progress_run "$name $ver: i686 build" "$top/build-i686.log" \
        ctr bash -c "export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig; setarch i686 rpmbuild --target i686 --nodeps --define '_topdir $top' --define 'dist .fc$FEDORA.oxp3.$STAMP' -bb '$spec'" ||
        { tail -30 "$top/build-i686.log"; die "$name i686 build failed (full log: $top/build-i686.log)"; }
    rm -f "$OUT/$name"-*.rpm
    find "$top/RPMS" -name "$name-$ver-*.rpm" ! -name '*debug*' -exec cp {} "$OUT/" \;
    if ! rpms_fit_host "$OUT/$name"-*.rpm; then
        rm -f "$OUT/$name"-*.rpm
        die "$name $ver: the built RPMs would not install on this Anatase image (see rpm's message above), so they were discarded"
    fi
    cache_store "$name" "$key"
    ls "$OUT/$name"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# XeSS on XMX (xess-xmx-linux, our fork with Anatase support). The XESS_* pins are set by the
# caller; these helpers build the tree, install it under $XESS_PKG and report its state.
# ---------------------------------------------------------------------------------------------

build_xess() {
    local dl="$CACHE/xess" stage="$OUT/xess-xmx" zip src entry name sha key
    key=$(printf '%s\n' "$XESS_COMMIT" "$XESS_ZIP_SHA" "$XESS_DLLS" | sha256sum | cut -c1-32)
    if cache_valid xess "$key" && [[ -d $stage ]]; then
        echo "  XeSS XMX: build cached (${key:0:8})"
    else
    mkdir -p "$dl"
    say "XeSS XMX: fork ${XESS_COMMIT:0:8} + release DLLs"
    zip="$dl/${XESS_ZIP_URL##*/}"
    if [[ $(sha256sum "$zip" 2>/dev/null | cut -d' ' -f1) != "$XESS_ZIP_SHA" ]]; then
        curl -sfL --retry 3 -o "$zip.part" "$XESS_ZIP_URL" || die "download failed: $XESS_ZIP_URL"
        [[ $(sha256sum "$zip.part" | cut -d' ' -f1) == "$XESS_ZIP_SHA" ]] || die "checksum mismatch: $XESS_ZIP_URL"
        mv "$zip.part" "$zip"
    fi
    src="$dl/xess-xmx-linux-$XESS_COMMIT.tar.gz"
    [[ -s $src ]] || curl -sfL --retry 3 -o "$src" "$XESS_SRC_URL" || die "download failed: $XESS_SRC_URL"
    rm -rf "$stage"; mkdir -p "$stage"
    tar -xzf "$src" --strip-components=1 -C "$stage"
    for entry in $XESS_DLLS; do
        name=${entry%%|*}; sha=${entry#*|}
        unzip -p "$zip" "*/$name" > "$stage/$name"
        [[ $(sha256sum "$stage/$name" | cut -d' ' -f1) == "$sha" ]] || die "$name in the release zip has an unexpected checksum"
    done
    echo "  $stage"
    cache_store xess "$key"
    fi
    if [[ -n ${OXP3_XMX_DRIVER_HOOK:-} ]]; then
        "$OXP3_XMX_DRIVER_HOOK"
    fi
}

# Intel's compiler as get-igc.sh would fetch it (IGC 2.10.10, ocloc 25.18.1), from an earlier package
# download, instead of 230 MB again
seed_xess_compiler() {
    [[ -x $(ls "$XESS_PKG"/igc/neo/bin/ocloc* 2>/dev/null | head -1) ]] && return 0
    local d
    for d in "$HOME"/Downloads/xess-xmx-linux-*/igc; do
        [[ -x $d/neo/bin/ocloc-25.18.1 ]] && ls "$d"/igc/local/lib/libigc.so.2.10.10* >/dev/null 2>&1 || continue
        mkdir -p "$XESS_PKG/igc"; cp -a "$d"/. "$XESS_PKG/igc/"
        echo "  compiler bundle copied from $d"
        return 0
    done
}

# The host Mesa now carries the CM kernel injection (patches/xess-main in the RPM build),
# so a second Vulkan ICD only splits the stack: VK_DRIVER_FILES pointed the whole session
# at the old Mesa 26.3 build, and processes ended up on different Mesa versions. Once the
# host driver has CM, drop those overrides; the ANV_CM_*/shim variables stay because the CM
# path lives in the host driver now.
xmx_retire_split_driver() {
    local f tmp n=0 d
    grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so 2>/dev/null || return 0
    # 1. environment drop-ins that point the whole session at the old driver
    for f in "$HOME"/.config/environment.d/*xmx*.conf; do
        [[ -f $f ]] || continue
        grep -Eq '^[[:space:]]*(VK_DRIVER_FILES|VK_ICD_FILENAMES)=' "$f" || continue
        tmp=$(mktemp) || return 0
        grep -Ev '^[[:space:]]*(VK_DRIVER_FILES|VK_ICD_FILENAMES)=' "$f" > "$tmp" && mv "$tmp" "$f"
        echo "  dropped the separate-ICD override from ${f/#$HOME/~} (host Mesa has the CM injection)"
        n=$((n + 1))
    done
    # 2. user-scope ICD manifests: the loader reads ~/.config/vulkan/icd.d and
    # ~/.local/share/vulkan/icd.d too, so one left there adds a second Intel device at the
    # old Mesa version even with no VK_DRIVER_FILES. Keep the file, disabled, for rollback.
    for d in "$HOME"/.config/vulkan/icd.d "$HOME"/.local/share/vulkan/icd.d; do
        [[ -d $d ]] || continue
        for f in "$d"/*xmx*.json; do
            [[ -f $f ]] || continue
            mv "$f" "$f.disabled" && echo "  disabled the extra ICD ${f/#$HOME/~} (one Mesa now)"
            n=$((n + 1))
        done
    done
    # 3. the user manager keeps its own copy of the environment from the last login: a game
    # started by it would still see the old VK_DRIVER_FILES even with the file clean.
    systemctl --user unset-environment VK_DRIVER_FILES VK_ICD_FILENAMES 2>/dev/null || true
    if (( n > 0 )); then
        echo "  one Mesa now: restart Steam (leave and re-enter Gamemode) to drop the old ICD"
    else
        echo "  one Mesa already: no separate Vulkan ICD selected"
    fi
    return 0
}

install_xess() {
    [[ -d $OUT/xess-xmx ]] || die "missing $OUT/xess-xmx - run '$0 build' first"
    if ! grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so; then
        echo "  the installed Mesa has no CM kernel injection (built before patches/xess-main?) - XeSS XMX skipped"
        return 0
    fi
    mkdir -p "$XESS_PKG"
    # the package's own state (compiler, XeSS SDK, kernels, ICD file) stays
    rsync -a --delete --exclude /igc/ --exclude /xess/ --exclude /kernels/ --exclude /run/ --exclude /lib/ \
        "$OUT/xess-xmx/" "$XESS_PKG/"
    seed_xess_compiler
    bash "$XESS_PKG/install.sh" | sed 's/^/  /' || echo "  WARNING: xess-xmx-linux's install.sh failed"
    xmx_retire_split_driver
    echo "  Restart Steam (leave and re-enter Gamemode) so games get the new environment."
}

uninstall_xess() {
    [[ -x $XESS_PKG/uninstall.sh ]] || return 0
    bash "$XESS_PKG/uninstall.sh" | sed 's/^/  /' || true
    echo "  $XESS_PKG (compiler, kernel cache) left in place; delete it to free about 300 MB"
}

status_xess() {
    local f; f=$(xmx_override_file)
    if grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so 2>/dev/null; then
        echo "    Host Mesa carries the CM injection (single driver, no separate XMX ICD)"
    else
        echo "    Host Mesa has no CM injection - XeSS XMX needs a Mesa built with patches/xess-main"
    fi
    local g
    for g in "$HOME"/.config/environment.d/*xmx*.conf; do
        [[ -f $g ]] && grep -Eq '^[[:space:]]*(VK_DRIVER_FILES|VK_ICD_FILENAMES)=' "$g" &&
            echo "    WARNING: ${g/#$HOME/~} still points at a separate Vulkan ICD (run install again)"
    done
    echo "  XeSS XMX: $(head -c 200 "${XDG_CACHE_HOME:-$HOME/.cache}/xess-xmx/driver-status" 2>/dev/null || echo 'no driver check yet')"
    grep -q '^ANV_CM_HELPER=' "$f" 2>/dev/null && echo "    Steam Flatpak override: $(grep -E '^(ANV_CM_HELPER|VKD3D_DISABLE_EXTENSIONS|force_vk_vendor)=' "$f" | tr '\n' ' ')"
    local p n=0 no=""
    for p in "$HOME/.local/share/Steam/steamapps/common/Proton"* "$HOME/.local/share/Steam/compatibilitytools.d/"*; do
        [[ -f $p/proton ]] || continue
        if grep -aq IGDEXT_ "$p/files/lib/wine/igdext/x86_64-windows/igdext64.dll" 2>/dev/null; then n=$((n + 1)); else no+=" ${p##*/};"; fi
    done
    echo "    Protons with the shim: $n${no:+; without:${no%;}}"
}

xmx_override_file() { echo "${XDG_DATA_HOME:-$HOME/.local/share}/flatpak/overrides/org.anatase.Steam"; }

# ---------------------------------------------------------------------------------------------
# kernel modules and firmware: both are built from pinned sources with the caller's container
# ---------------------------------------------------------------------------------------------

# Apply patches/kernel/*.patch in order. A patch Anatase already carries is skipped, one that
# neither applies nor is already there stops the build. Prints the number applied.
# A backport can already be in the base kernel while changes around it make `git apply -R`
# fail (7.3.4 carries several 7.2.7 backports). Treat the patch as present when the first
# distinctive added line is already in its target file; these are our own, uniquely worded
# patches, so this is not ambiguous.
patch_body_present() {
    local src=$1 p=$2 file line
    file=$(sed -n 's|^+++ b/||p' "$p" | head -1)
    [[ -n $file && -r $src/$file ]] || return 1
    # Check several distinctive added lines: later backports (e.g. the shrinker series) often
    # change a signature the first line happens to be, while the body is unmistakably present.
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        grep -Fq -- "$line" "$src/$file" 2>/dev/null && return 0
    done < <(awk '/^\+[^+]/ {
        s = substr($0, 2)
        if (length(s) > 18 && (s ~ /\(/ || s ~ /=/)) { print s; n++; if (n == 8) exit }
    }' "$p")
    return 1
}

apply_kernel_patches() {
    local src=$1 p n=0 skipped=()
    for p in "$here"/patches/kernel/*.patch; do
        # A rerun against a cached source tree must recognise commits it already made even
        # when later patches changed the reverse-apply context. Subject match is sufficient
        # here: these are our own, uniquely named commits.
        local subj
        subj=$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//p' "$p" | head -1)
        if git -C "$src" log --all --format=%s 2>/dev/null | grep -Fqx -- "$subj"; then
            skipped+=( "$(basename "$p")" )
            continue
        fi
        if git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am -q -3 "$p" >/dev/null 2>&1; then
            # git am succeeds without a commit when the change is already there
            if [[ $(git -C "$src" log -1 --format=%s) == "$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//p' "$p" | head -1)"* ]]; then
                n=$((n + 1))
            else
                skipped+=( "$(basename "$p")" )
            fi
        else
            git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am --abort >/dev/null 2>&1 || true
            rm -rf "$src/.git/rebase-apply"
            if git -C "$src" apply --check -R "$p" >/dev/null 2>&1; then
                skipped+=( "$(basename "$p")" )
            elif patch_body_present "$src" "$p"; then
                skipped+=( "$(basename "$p")" )
            # --3way needs the patch's original blobs; a shallow Anatase clone often lacks
            # them even when the plain context applies (e.g. 0107 on 7.3.4). Try the plain
            # mailbox application before declaring the patch stale.
            elif git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am -q "$p" >/dev/null 2>&1; then
                if [[ $(git -C "$src" log -1 --format=%s) == "$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//p' "$p" | head -1)"* ]]; then
                    n=$((n + 1))
                else
                    skipped+=( "$(basename "$p")" )
                fi
            else
                git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am --abort >/dev/null 2>&1 || true
                rm -rf "$src/.git/rebase-apply"
                die "kernel patch $(basename "$p") doesn't apply to this kernel - it needs updating"
            fi
        fi
    done
    (( ${#skipped[@]} )) && printf '  already in Anatase, skipped: %s\n' "${skipped[@]}" >&2
    echo "$n"
}

build_kmods() {
    local kver tag src kb cfg hash
    kver=$(anatase_kver)
    tag=${kver%%.fc*}                              # 7.2.7-an19.fc45.x86_64 -> 7.2.7-an19
    cfg=/usr/lib/modules/$kver/config
    [[ -r $cfg ]] || die "no kernel config at $cfg"
    [[ -r $MOK_DIR/MOK.key && -r $MOK_DIR/MOK.der ]] ||
        die "MOK key not found in $MOK_DIR (see README: Secure Boot key), or use --skip-kernel"
    src="$CACHE/kernel/src-$tag"; kb="$CACHE/kernel/build-$tag"

    local key
    key=$( { echo "$tag $kver"
             sha256sum "$here"/patches/kernel/*.patch 2>/dev/null || true
             printf '%s\n' "${KMODS[@]}"
             sha256sum "$cfg" "$MOK_DIR/MOK.der" "$here/container/Containerfile" 2>/dev/null || true
           } | sha256sum | cut -c1-32 )
    if cache_valid "$KMOD_NAME" "$key" && compgen -G "$OUT/$KMOD_NAME-*.rpm" >/dev/null; then
        echo "  kernel $kver: build cached (${key:0:8})"; return 0
    fi

    say "kernel $kver: source (anatase-org/patchwork tag $tag)"
    if [[ ! -d $src/.git ]]; then
        git ls-remote --exit-code --tags "$PATCHWORK_REPO" "refs/tags/$tag" >/dev/null ||
            die "no tag $tag in anatase-org/patchwork (or GitHub unreachable)"
        local try
        for try in 1 2 3; do
            git clone -q --depth 1 --branch "$tag" "$PATCHWORK_REPO" "$src" && break
            rm -rf "$src"
            (( try < 3 )) || die "cloning anatase-org/patchwork $tag failed 3 times (network?)"
            echo "clone failed, retrying ($try/3)"; sleep 10
        done
    fi
    git -C "$src" reset -q --hard "$tag" 2>/dev/null || git -C "$src" reset -q --hard HEAD
    git -C "$src" clean -qfdx
    local applied; applied=$(apply_kernel_patches "$src")
    if (( applied == 0 )); then
        echo "kernel $tag already contains every patch - no modules needed"
        rm -f "$OUT/$KMOD_NAME"-*.rpm; touch "$OUT/.kernel-not-needed"; return 0
    fi
    echo "  $applied kernel patches applied"
    rm -f "$OUT/.kernel-not-needed"
    # out-of-tree drivers Anatase adds as submodules: stub their Kconfig, our modules don't use them
    local k
    for k in $(sed -n 's/^source "\(drivers\/custom\/[^"]*\)"/\1/p' "$src/drivers/custom/Kconfig" 2>/dev/null); do
        [[ -e $src/$k ]] || { mkdir -p "$(dirname "$src/$k")"; : > "$src/$k"; }
    done

    say "kernel $kver: building modules (15-30 min)"
    rm -rf "$kb"; mkdir -p "$kb"; cp "$cfg" "$kb/.config"
    local entry dir mods m make_m=""
    for entry in "${KMODS[@]}"; do
        make_m+=" && make -s O='$kb' KERNELRELEASE='$kver' KBUILD_MODPOST_WARN=1 -j\$(nproc) M=${entry%%|*} modules"
    done
    # Anatase's symvers lets modpost resolve the kernel's exports, so a symbol our modules need
    # but the running kernel lacks shows up here instead of as a module that won't load.
    [[ -r /usr/lib/modules/$kver/symvers.xz ]] || die "no /usr/lib/modules/$kver/symvers.xz"
    xz -dc "/usr/lib/modules/$kver/symvers.xz" > "$kb/Module.symvers"
    progress_run "kernel $kver: modules" "$kb/build.log" \
        ctr bash -c "cd '$src' && make -s O='$kb' KERNELRELEASE='$kver' olddefconfig &&
        make -s O='$kb' KERNELRELEASE='$kver' -j\$(nproc) modules_prepare $make_m" \
        || { tail -30 "$kb/build.log"; die "module build failed (full log: $kb/build.log)"; }
    local undef; undef=$(grep -E 'modpost: ".*" \[.*\] undefined!' "$kb/build.log" || true)
    [[ -z $undef ]] || { echo "$undef" | head -20; die "modules need kernel symbols $kver doesn't export"; }

    say "kernel $kver: signing with $MOK_DIR/MOK.der"
    hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"/\1/p' "$cfg"); hash=${hash:-sha512}
    local stage="$CACHE/kernel/stage-$tag" ko rel_path files=()
    rm -rf "$stage"; mkdir -p "$stage"
    local autoload=()
    for entry in "${KMODS[@]}"; do
        # <dir>|<modules>[|autoload] - autoload: load at boot through modules-load.d
        dir=${entry%%|*}; mods=${entry#*|}; mods=${mods%%|*}
        [[ $entry == *'|autoload' ]] && autoload+=( $mods )
        for m in $mods; do
            ko="$src/$dir/$m.ko"
            [[ -f $ko ]] || die "$dir/$m.ko not produced"
            [[ $(ctr modinfo -F vermagic "$ko") == "$kver "* ]] || die "$m.ko vermagic doesn't match $kver"
            rel_path="$dir/$m.ko"
            mkdir -p "$stage/$(dirname "$rel_path")"
            ctr strip --strip-debug -o "$stage/$rel_path" "$ko"
            ctr "$kb/scripts/sign-file" "$hash" "$MOK_DIR/MOK.key" "$MOK_DIR/MOK.der" "$stage/$rel_path"
            ctr xz -f --check=crc32 --lzma2=dict=1MiB "$stage/$rel_path"
            files+=( "$rel_path.xz" )
        done
    done
    echo "  signed by: $(ctr modinfo -F signer "$stage/drivers/gpu/drm/xe/xe.ko.xz")"

    say "kernel $kver: packaging $KMOD_NAME"
    local top="$CACHE/rpmbuild/$KMOD_NAME" rel f; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    tar -C "$stage" -cf "$top/SOURCES/modules.tar" "${files[@]}"
    {
        cat <<EOF
Name:           $KMOD_NAME
Version:        ${kver//-/_}
Release:        $rel
Summary:        xe, iwlwifi and btintel with the OneXPlayer 3 fixes
License:        GPL-2.0-only
Source0:        modules.tar
Requires:       kernel-core = ${kver%.*}
Obsoletes:      kmod-oxp3-xe < 999
%global debug_package %{nil}
%global __os_install_post %{nil}
%description
xe (VBT VS/PE-O backport, VRR/DSB timing, resume ENOMEM fix, 7.2.13 stable fixes, eDP HDR
switches), iwlwifi/iwlmld and btintel/btintel_pcie (7.2.13 stable and 7.3/7.4 fixes) for
kernel $kver. Signed with a local MOK. Installed in updates/, so they take priority over the
stock modules.
%prep
%setup -c -T
tar -xf %{SOURCE0}
%install
mkdir -p %{buildroot}/usr/lib/modules/$kver/updates
cp -a drivers %{buildroot}/usr/lib/modules/$kver/updates/
mkdir -p %{buildroot}/usr/lib/modules-load.d
printf '%s\n' ${autoload[*]} > %{buildroot}/usr/lib/modules-load.d/oxp3-kmods.conf
%files
/usr/lib/modules-load.d/oxp3-kmods.conf
EOF
        for f in "${files[@]}"; do echo "/usr/lib/modules/$kver/updates/$f"; done
        cat <<EOF
%post
/usr/sbin/depmod -a $kver || :
%postun
/usr/sbin/depmod -a $kver || :
EOF
    } > "$top/SPECS/$KMOD_NAME.spec"
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$KMOD_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "kmod packaging failed"; }
    rm -f "$OUT"/kmod-oxp3-*.rpm
    find "$top/RPMS" -name "$KMOD_NAME-*.rpm" -exec cp {} "$OUT/" \;
    cache_store "$KMOD_NAME" "$key"
    ls "$OUT/$KMOD_NAME"-*.rpm
}

# revision of the first microcode header in a file; empty when the file is absent (callers test
# for that: build_firmware compares an installed revision only when sys_fw found the file)
ucode_rev() { [[ -f $1 ]] || return 0; od -An -t x4 -j 4 -N 4 "$1" 2>/dev/null | tr -d ' '; }

# installed (Anatase) copy of a firmware file, compressed or not
sys_fw() { local f; for f in /usr/lib/firmware/$1{,.xz,.zst}; do [[ -e $f ]] && { echo "$f"; return; }; done; }

sys_fw_sha() {
    local f; f=$(sys_fw "$1"); [[ -n $f ]] || return 0
    case $f in *.xz) xz -dc "$f" ;; *.zst) zstd -dcq "$f" ;; *) cat "$f" ;; esac | sha256sum | cut -d' ' -f1
}

build_firmware() {
    local stage="$CACHE/firmware/stage" entry path url sha kind f files=() key
    key=$( { printf '%s\n' "${FIRMWARE[@]}"; echo "skip_dmc=$SKIP_DMC"
             sha256sum "$here/container/Containerfile" 2>/dev/null || true
           } | sha256sum | cut -c1-32 )
    if cache_valid "$FW_NAME" "$key"; then
        echo "  firmware: build cached (${key:0:8})"; return 0
    fi
    rm -rf "$stage"; mkdir -p "$stage" "$CACHE/firmware/dl"
    say "firmware: pinned files"
    for entry in "${FIRMWARE[@]}"; do
        IFS='|' read -r path url sha kind <<<"$entry"
        (( SKIP_DMC )) && [[ $kind == dmc ]] && { echo "  $path: --stock-dmc, keeping Anatase's"; continue; }
        f="$CACHE/firmware/dl/$sha"
        if [[ $(sha256sum "$f" 2>/dev/null | cut -d' ' -f1) != "$sha" ]]; then
            curl -sfL --retry 3 -o "$f.part" "$url" || die "download failed: $url"
            [[ $(sha256sum "$f.part" | cut -d' ' -f1) == "$sha" ]] || die "checksum mismatch: $url"
            mv "$f.part" "$f"
        fi
        # skip what Anatase already ships in the same or a newer version
        if [[ $(sys_fw_sha "$path") == "$sha" ]]; then
            echo "  $path: Anatase ships the same file"; continue
        fi
        if [[ $kind == ucode ]]; then
            local have want
            have=$(ucode_rev "$(sys_fw "$path")"); want=$(ucode_rev "$f")
            if [[ -n $have ]] && (( 16#$have >= 16#$want )); then
                echo "  $path: Anatase ships revision 0x$have (>= 0x$want)"; continue
            fi
        fi
        install -D -m 0644 "$f" "$stage/$path"
        files+=( "$path" ); echo "  $path"
    done
    rm -f "$OUT/$FW_NAME"-*.rpm
    if (( ${#files[@]} == 0 )); then echo "  nothing newer than Anatase's firmware"; cache_store "$FW_NAME" "$key"; return 0; fi

    local top="$CACHE/rpmbuild/$FW_NAME" rel; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    tar -C "$stage" -cf "$top/SOURCES/firmware.tar" "${files[@]}"
    {
        cat <<EOF
Name:           $FW_NAME
Version:        1
Release:        $rel
Summary:        Pinned firmware for the OneXPlayer 3 (display DMC, Bluetooth, CPU microcode)
License:        Redistributable, no modification permitted
BuildArch:      noarch
Source0:        firmware.tar
%global debug_package %{nil}
%description
Firmware files that take priority over linux-firmware / microcode_ctl through
/usr/lib/firmware/updates: display DMC v2.20 (VRR cap with v2.36), Bluetooth 24.80,
CPU microcode 06-cc-02 0x11c.
%prep
%setup -c -T
tar -xf %{SOURCE0}
%install
mkdir -p %{buildroot}/usr/lib/firmware/updates
cp -a * %{buildroot}/usr/lib/firmware/updates/
%files
EOF
        for f in "${files[@]}"; do echo "/usr/lib/firmware/updates/$f"; done
    } > "$top/SPECS/$FW_NAME.spec"
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$FW_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "firmware packaging failed"; }
    find "$top/RPMS" -name "$FW_NAME-*.rpm" -exec cp {} "$OUT/" \;
    cache_store "$FW_NAME" "$key"
    ls "$OUT/$FW_NAME"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# build everything for the installed Anatase: the one caller hook is build_mesa()
# (RPM packaging in oxp3-fixes.sh, podman/meson in oxp3-fixes-fast.sh)
# ---------------------------------------------------------------------------------------------

do_build() {
    command -v podman >/dev/null || die "podman is required"
    ensure_container
    if (( !SKIP_GAMESCOPE || !SKIP_MANGOHUD || !SKIP_MESA )); then fetch_anatase; fi
    (( SKIP_GAMESCOPE )) || build_card gamescope "$here/patches/gamescope"
    (( SKIP_MANGOHUD )) || build_card mangohud "$here/patches/mangohud"
    (( SKIP_MESA )) || build_mesa
    (( SKIP_MESA || SKIP_FLATPAK_GL )) || build_flatpak_gl
    (( SKIP_XESS )) || build_xess
    (( SKIP_KERNEL )) || build_kmods
    (( SKIP_FIRMWARE )) || build_firmware
    say "build done: $OUT"
    ls "$OUT"
}

# ---------------------------------------------------------------------------------------------
# module signing (MOK) and the root-owned RAPL power service
# ---------------------------------------------------------------------------------------------

mok_cn() {
    openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -subject -nameopt multiline |
        sed -n 's/^ *commonName *= *//p'
}

# Anatase's kernel only loads MOK keys into the machine keyring (the one that verifies
# modules) if they are CA certs with keyCertSign and without digitalSignature
# (CONFIG_INTEGRITY_CA_MACHINE_KEYRING_MAX); anything else lands in the platform keyring,
# which can verify kernel images but not modules.
mok_usage_ok() {
    local ext
    ext=$(openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -ext basicConstraints,keyUsage 2>/dev/null)
    grep -q 'CA:TRUE' <<<"$ext" && grep -q 'Certificate Sign' <<<"$ext" && ! grep -q 'Digital Signature' <<<"$ext"
}

# Is the MOK in the kernel's .machine keyring right now? keyctl can't find that keyring by
# name from a normal process, so look its id up in /proc/keys and list it by id.
mok_trusted() {
    local cn kr; cn=$(mok_cn)
    kr=$(sudo awk '$8 == "keyring" && $9 == ".machine:" { print $1; exit }' /proc/keys)
    [[ -n $cn && -n $kr ]] && sudo keyctl list "0x$kr" 2>/dev/null | grep -qF "$cn"
}

# Create a module signing key the machine keyring accepts and queue it for enrolment.
do_mok_setup() {
    if [[ -e $MOK_DIR/MOK.key ]] && mok_usage_ok; then
        echo "$MOK_DIR already has a suitable key."
    else
        if [[ -e $MOK_DIR/MOK.key ]]; then
            local old; old="$MOK_DIR.old-$(date +%Y%m%d%H%M%S)"
            echo "The key in $MOK_DIR isn't accepted for modules (needs CA + keyCertSign only); keeping it as $old"
            mv "$MOK_DIR" "$old"
        fi
        install -d -m 700 "$MOK_DIR"
        local cfg; cfg=$(mktemp)
        printf '%s\n' '[req]' 'distinguished_name = dn' 'x509_extensions = mok' 'prompt = no' \
            '[dn]' "CN = OXP3 module signing CA ($(id -un))" \
            '[mok]' 'basicConstraints = critical, CA:TRUE' 'keyUsage = critical, keyCertSign' \
            'subjectKeyIdentifier = hash' > "$cfg"
        (umask 077; openssl req -new -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 -config "$cfg" \
            -keyout "$MOK_DIR/MOK.key" -out "$MOK_DIR/MOK.crt" 2>/dev/null)
        rm -f "$cfg"
        openssl x509 -in "$MOK_DIR/MOK.crt" -outform DER -out "$MOK_DIR/MOK.der"
        echo "Created $MOK_DIR/MOK.key (keep it private) and MOK.der."
    fi
    if mokutil --test-key "$MOK_DIR/MOK.der" 2>&1 | grep -q 'already enrolled'; then
        echo "MOK already enrolled."
    else
        echo "Enrolling: choose a one-time password, then reboot and in MokManager pick"
        echo "Enroll MOK -> Continue -> Yes -> enter the password."
        sudo mokutil --import "$MOK_DIR/MOK.der"
    fi
}

# MangoHud's CPU/GPU power: a root service publishes 1 s / 0.1 W averages in /run/rapl-power;
# the raw RAPL counters stay root-only (CVE-2020-8694).
install_rapl_power() {
    sudo install -D -m 0755 "$here/files/rapl-power" /usr/local/libexec/rapl-power
    sudo install -m 0644 "$here/files/rapl-power.service" /etc/systemd/system/rapl-power.service
    sudo systemctl daemon-reload
    sudo systemctl enable rapl-power.service >/dev/null
    sudo systemctl restart rapl-power.service
    remove_rapl_udev_rules
}

# Earlier versions opened the counters to all users with a udev rule: remove it and make
# the counters root-only again right away (they are re-created root-only at boot anyway).
remove_rapl_udev_rules() {
    local r changed=0
    for r in /etc/udev/rules.d/70-oxp3-rapl.rules /etc/udev/rules.d/70-anatase-rapl.rules; do
        [[ -e $r ]] && { sudo rm -f "$r"; changed=1; }
    done
    if (( changed )); then
        sudo udevadm control --reload
        sudo chmod 0400 /sys/class/powercap/intel-rapl*/energy_uj 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------------------------
# volume rocker: hhd patches + atkbd force-release hwdb (both live in the /usr overlay)
# ---------------------------------------------------------------------------------------------

hhd_site() { python3 -c 'import hhd, os; print(os.path.dirname(os.path.dirname(hhd.__file__)))' 2>/dev/null; }

# Record a copy of every file a patch is about to change. An hhd update or a failed reverse
# patch then still has the exact previous bytes in the undo state (docs/ROLLBACK.md), which the
# reverse patch alone cannot guarantee.
hhd_backup_patch_files() { # SITE PATCH -> prints "<stamp>	<key>	<pre_sha>" lines, one per file
    local site=$1 patch=$2 f
    git -C "$site" apply --numstat -p1 "$patch" 2>/dev/null | awk '{ $1=""; $2=""; sub(/^  /, ""); print }' |
        while IFS= read -r f; do
            [[ -n $f && -f $site/$f ]] || continue
            oxp3_backup_image "$site/$f" "install-common/hhd"
        done
}

# Our hhd patches (patches/hhd): OneXPlayer 3 power limits and volume rocker taps. Each patch is
# applied on its own - one that no longer fits Anatase's hhd is reported and skipped, the others
# still apply (this used to undo all of them, so a stale volume patch silently cost the rest).
# hhd lives in /usr, whose overlay is discarded at reboot, so oxp3-hhd-keep re-applies the
# deployed copies before hhd starts on every boot.
install_hhd_patches() {
    local site p changed=0 rows row rk vol_total=0 vol_ok=0 keep_dir=/usr/local/share/oxp3-fixes/hhd-patches
    site=$(hhd_site)
    [[ -n $site ]] || { echo "  hhd not found, hhd patches skipped"; return 0; }
    for p in "${HHD_PATCHES[@]}"; do
        [[ ${p##*/} == *volume-rocker* ]] && vol_total=$((vol_total + 1))
        if git -C "$site" apply --check -R -p1 "$p" >/dev/null 2>&1; then
            echo "  ${p##*/}: already applied"
        elif git -C "$site" apply --check -p1 "$p" >/dev/null 2>&1; then
            rows=$(hhd_backup_patch_files "$site" "$p")
            sudo git -C "$site" apply -p1 "$p"; changed=1
            while IFS= read -r row; do
                [[ -n $row ]] || continue
                rk=$(printf '%s' "$row" | cut -f2)
                oxp3_manifest_record "/${rk#abs/}" "install-common/hhd" \
                    "${row%%$'\t'*}" backup "${p##*/}" 2>/dev/null || true
            done <<<"$rows"
            echo "  ${p##*/}: applied"
        else
            echo "  WARNING: ${p##*/} does not fit this hhd ($(rpm -q hhd 2>/dev/null | head -1)) - skipped" >&2
            continue
        fi
        [[ ${p##*/} == *volume-rocker* ]] && vol_ok=$((vol_ok + 1))
    done
    sudo find "$site/hhd" "$site/adjustor" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    # volume rocker: the atkbd force-release only belongs next to hhd's tap handling
    if (( vol_total && vol_ok == vol_total )); then
        if ! cmp -s "$here/files/84-oxp3-volume-keys.hwdb" "$VOLUME_HWDB"; then
            sudo install -m 0644 "$here/files/84-oxp3-volume-keys.hwdb" "$VOLUME_HWDB"
            sudo systemd-hwdb update
            sudo udevadm trigger --subsystem-match=input --action=change
        fi
        echo "  hhd: volume rocker presses become taps"
    elif [[ -e $VOLUME_HWDB ]]; then
        sudo rm -f "$VOLUME_HWDB"; sudo systemd-hwdb update
    fi
    # re-apply at every boot, before hhd starts
    sudo rm -rf "$keep_dir"; sudo install -d -m 0755 "$keep_dir"
    sudo install -m 0644 "${HHD_PATCHES[@]}" "$keep_dir/"
    sudo install -D -m 0755 "$here/files/oxp3-hhd-keep.sh" /usr/local/libexec/oxp3-hhd-keep
    sudo install -m 0644 "$here/files/oxp3-hhd-keep.service" /etc/systemd/system/oxp3-hhd-keep.service
    sudo systemctl daemon-reload
    sudo systemctl enable oxp3-hhd-keep.service >/dev/null 2>&1
    sudo systemctl restart hhd.service
    (( changed )) || echo "  hhd patches: nothing new to apply"
}

# take our hhd patches out again, newest first (only those that are fully applied)
hhd_volume_unpatch() { # <site>   (undo install_hhd_patches: patches, keep unit, patch copies)
    local i p k real pending=0
    sudo systemctl disable oxp3-hhd-keep.service >/dev/null 2>&1 || true
    sudo rm -f /etc/systemd/system/oxp3-hhd-keep.service /usr/local/libexec/oxp3-hhd-keep
    sudo rm -rf /usr/local/share/oxp3-fixes/hhd-patches
    sudo systemctl daemon-reload 2>/dev/null || true
    for (( i=${#HHD_PATCHES[@]} - 1; i >= 0; i-- )); do
        p=${HHD_PATCHES[$i]}
        git -C "$1" apply --check -R -p1 "$p" >/dev/null 2>&1 && sudo git -C "$1" apply -R -p1 "$p"
    done
    sudo find "$1/hhd" "$1/adjustor" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    # The reverse patch is the normal undo. If it did not apply (a newer hhd, a rejected hunk),
    # the pre-patch bytes recorded by install_hhd_patches are the fallback; restoring needs root
    # for /usr, so this only tries and otherwise points at the rollback tool.
    for k in $(oxp3_manifest_keys 2>/dev/null); do
        case $k in abs/*) real=/${k#abs/} ;; *) continue ;; esac
        [[ $real == "$1"/* ]] || continue
        oxp3_restore_file "$real" >/dev/null 2>&1 || pending=$((pending + 1))
    done
    (( pending )) && echo "  hhd patch files still differ from their backups: sudo $here/tools/oxp3-rollback.sh restore"
}

# ---------------------------------------------------------------------------------------------
# Mesa runtime state and the Steam Flatpak runtime overrides
# ---------------------------------------------------------------------------------------------

# Mesa version of the host (from its libgallium-<version>.so) and whether the Steam Flatpak runtime
# runs the same Intel Vulkan driver
# The image and the /usr overlay can both carry a libgallium (the image's 26.2.6 under ours), and
# `rpm -q` reports the image's package because the overlay's rpmdb is not what it reads - so report
# the highest version present, which is the one the driver in use links against.
host_mesa() {
    local g
    g=$(ls -1 /usr/lib64/libgallium-*.so 2>/dev/null | sed 's,.*/libgallium-,,; s,\.so$,,')
    g=$(printf '%s\n' $g | sort -V | tail -1)
    printf '%s\n' "${g:-unknown}"
}

flatpak_mesa() {
    # The overrides directory is what the runtime merges; /run holds the copy made at boot, so a
    # restore done after boot would still read as the image's Mesa through it.
    local ref=$FLATPAK_RT/overrides/lib64/libvulkan_intel.so
    [[ -e $ref ]] || ref=$FLATPAK_RT_FILES/lib64/libvulkan_intel.so
    if [[ -e $ref ]] && cmp -s /usr/lib64/libvulkan_intel.so "$ref"; then
        echo "same as the host"
    else
        echo "Anatase's (not ours)"
    fi
}

refresh_flatpak_runtime() {
    if [[ -n $(flatpak ps --columns=application 2>/dev/null) ]]; then
        echo "  Flatpak apps are running (Steam?): the Steam runtime picks this up after a reboot,"
        echo "  or close them and run: sudo systemctl restart anatase-flatpak-extensions.service"
        return 0
    fi
    sudo systemctl restart anatase-flatpak-extensions.service
    echo "  Steam Flatpak runtime Mesa: $(flatpak_mesa)"
}

remove_flatpak_mesa() {
    [[ -r $FLATPAK_MESA_LIST ]] || return 0
    # `rpm -ql` lists directories as well as files, and `rm -f` refuses a directory: that made
    # this pipeline return non-zero, which under `set -e` aborted the install before the XMX
    # and ICD-retirement steps. Remove the listed entries with -rf (they are paths relative to
    # the overrides dir and the current tree is re-extracted right after), never follow an
    # absolute or parent-relative entry, and never fail an install over a stale one.
    (cd "$FLATPAK_RT/overrides" &&
        grep -vE '^/|(^|/)\.\.(/|$)' "$FLATPAK_MESA_LIST" |
        sudo xargs -r -d '\n' rm -rf --) || true
    sudo rm -f "$FLATPAK_MESA_LIST" || true
}

# ---------------------------------------------------------------------------------------------
# Our Mesa for Flathub apps: the GL driver extension org.freedesktop.Platform.GL.oxp3
# ---------------------------------------------------------------------------------------------
# Flathub apps (Horizon, Heroic, emulators...) run on org.freedesktop.Platform and take Mesa from
# its GL driver extension, not from the host or from the Anatase Steam runtime above, so they ran
# Flathub's Mesa 26.2.x. A host-built driver cannot simply be pointed at either: it needs host
# libraries the runtime lacks (libdisplay-info, libSPIRV-Tools, a newer libexpat).
#
# So the same Mesa (MESA_COMMIT + Anatase's overrides.patch + patches/mesa) is built inside
# org.freedesktop.Sdk//<branch> (tools/flatpak-gl/build-in-sdk.sh) and exported as the extension
# org.freedesktop.Platform.GL.oxp3//<branch> with priority=100. It is enabled next to Flathub's
# driver with FLATPAK_GL_DRIVERS=oxp3:default (/etc/environment.d):
#   - priority=100 makes our files win where both provide one (vulkan/icd.d/intel_icd.*.json,
#     libEGL_mesa/libGLX_mesa/libgbm, lib/dri) - measured: without it GL.default wins;
#   - "default" stays active for everything not built here: 32-bit GL (GL32.default), apps on
#     other runtime branches, other vendors' drivers, lavapipe, OpenCL, Vulkan layers.
# The extension is installed system-wide from a local no-GPG remote and pinned, so
# `flatpak uninstall --unused` run without the variable does not prune it.
# Build: user, needs org.freedesktop.Sdk + Sdk.Extension.llvm22 for the branch (installed --user
# from Flathub when missing). Install/uninstall: root.

flatpak_gl_name() { echo oxp3; }
flatpak_gl_branches() { echo "${OXP3_FLATPAK_GL_BRANCHES:-26.08}"; }
flatpak_gl_work() { echo "$CACHE/flatpak-gl"; }
flatpak_gl_sysrepo() { echo /var/lib/oxp3-fixes/flatpak-gl-repo; }
flatpak_gl_env_file() { echo /etc/environment.d/60-oxp3-flatpak-gl.conf; }
flatpak_gl_ref() { echo "runtime/org.freedesktop.Platform.GL.$(flatpak_gl_name)/x86_64/$1"; }
# "Mesa 27.0.0-devel" from the libgallium-<version>.so in an extension's files/ (the version
# string inside the driver binaries is not contiguous, so grep on them truncates it)
flatpak_gl_mesa_version() { # <files dir>
    local g; g=$(ls "$1"/lib/libgallium-*.so 2>/dev/null | head -1)
    [[ -n $g ]] || { echo "Mesa ?"; return 0; }
    g=${g##*/libgallium-}; echo "Mesa ${g%.so}"
}

# Pinned build dependencies the SDK does not have: <file>|<url>|<sha256>. The translator release
# must match the SDK extension's LLVM (22.1.x); each project gets the SPIRV-Headers commit it
# names (SPIRV-Tools DEPS, SPIRV-LLVM-Translator spirv-headers-tag.conf).
flatpak_gl_sources() {
    cat <<'EOF'
SPIRV-Tools-vulkan-sdk-1.4.363.0.tar.gz|https://codeload.github.com/KhronosGroup/SPIRV-Tools/tar.gz/refs/tags/vulkan-sdk-1.4.363.0|e6c83a215538fbfd265ccf398d3877985055df1273dbed73912bd05307664688
SPIRV-Headers-496543121ce6419f23d6fa5d7194ba66c36212d2.tar.gz|https://codeload.github.com/KhronosGroup/SPIRV-Headers/tar.gz/496543121ce6419f23d6fa5d7194ba66c36212d2|a9bb9c48713245eacf97cc539b6f1d45405a92d8813f8b82e635f0a085ee9898
SPIRV-LLVM-Translator-v22.1.8.tar.gz|https://codeload.github.com/KhronosGroup/SPIRV-LLVM-Translator/tar.gz/refs/tags/v22.1.8|895bca0285fd4118800912a54527c0ae0d0c6e62591e807c3c96c6b342c4c5e2
SPIRV-Headers-cb42dec3830d3ac67fa449ecdc0c0f73d5e74498.tar.gz|https://codeload.github.com/KhronosGroup/SPIRV-Headers/tar.gz/cb42dec3830d3ac67fa449ecdc0c0f73d5e74498|117010e525fe36b1ce006e0e58a6e0a3b415aabc9179d912b92737f41f3f994f
pyyaml-6.0.4.tar.gz|https://files.pythonhosted.org/packages/76/b8/c0b780928e2fb1c87e9e375818c6f121da2398b071012d1747be300b83f4/pyyaml-6.0.4.tar.gz|e24bf71af8a432d7808fabff134d38dfe16d3cbb41f2873bb4b1e42422de0a1a
EOF
}

fetch_flatpak_gl_sources() { # <dl dir>
    local dl=$1 file url sha
    mkdir -p "$dl"
    while IFS='|' read -r file url sha; do
        if [[ ! -s $dl/$file ]] || ! sha256sum -c --status <<<"$sha  $dl/$file"; then
            curl -sfL --retry 3 -o "$dl/$file.part" "$url" || die "download failed: $url"
            sha256sum -c --status <<<"$sha  $dl/$file.part" ||
                { rm -f "$dl/$file.part"; die "checksum mismatch: $file ($url)"; }
            mv "$dl/$file.part" "$dl/$file"
        fi
    done < <(flatpak_gl_sources)
    # remove tarballs from older pins so build-in-sdk.sh's globs stay unambiguous
    local f; for f in "$dl"/*.tar.gz; do
        flatpak_gl_sources | cut -d'|' -f1 | grep -qxF "${f##*/}" || rm -f "$f"
    done
    echo 496543121ce6419f23d6fa5d7194ba66c36212d2 > "$dl/spirv-tools-headers.rev"
    echo cb42dec3830d3ac67fa449ecdc0c0f73d5e74498 > "$dl/translator-headers.rev"
}

# Ordered patch series for the 26.3.x tree, the same for the host RPM build and the Flatpak
# extension (the working XMX recipe: the 26.3 backports, the perf patches that apply to 26.3,
# then the CM/XMX kernel-injection series). Backports already upstream in the point release are
# skipped, so a new 26.3.x only needs MESA_COMMIT bumped. Prints paths, applies nothing.
mesa_patch_list() { # <extracted mesa tree>
    local tree=$1 f
    for f in "$here"/patches/mesa-26.3-backports/*.patch; do
        git -C "$tree" apply --check -R -p1 "$f" 2>/dev/null && continue
        echo "$f"
    done
    for f in "$here"/patches/mesa/0200*.patch "$here"/patches/mesa/0201*.patch \
             "$here"/patches/mesa/0206*.patch "$here"/patches/mesa-26.3/*.patch; do
        echo "$f"
    done
}

# Patched Mesa tree: the same inputs as the host build (MESA_COMMIT, Anatase's overrides.patch
# without its AMD-only RADV hunk, patches/mesa). Needs fetch_anatase.
prepare_mesa_src() { # <dest>
    local dest=$1 tarball="$CACHE/mesa/mesa-$MESA_COMMIT.tar.gz" f
    local ov="$CACHE/anatase/cards/drivers/mesa/overrides.patch"
    [[ -r $ov ]] || die "no $ov (fetch_anatase first)"
    mkdir -p "$CACHE/mesa"
    [[ -s $tarball ]] || curl -sfL --retry 3 -o "$tarball" "$MESA_URL" || die "download failed: $MESA_URL"
    rm -rf "$dest"; mkdir -p "$dest"
    tar -xzf "$tarball" -C "$dest" --strip-components=1
    python3 - "$ov" "$dest/.oxp3-overrides.patch" <<'PYEOF'
import re, sys
s = open(sys.argv[1]).read()
parts = re.split(r'(?m)^(?=diff --git )', s)
keep = [x for x in parts if not x.startswith('diff --git a/src/amd/vulkan/00-radv-defaults.conf')]
open(sys.argv[2], 'w').write(''.join(keep))
PYEOF
    git -C "$dest" apply -p1 "$dest/.oxp3-overrides.patch" || die "overrides.patch does not apply to $MESA_VERSION"
    # Same series as the host RPM build, CM injection included: Flatpak apps then run the
    # same Mesa, with XeSS XMX working there too.
    git -C "$dest" init -q 2>/dev/null || true
    while IFS= read -r f; do
        git -C "$dest" apply -p1 "$f" || die "$(basename "$f") does not apply to $MESA_VERSION"
    done < <(mesa_patch_list "$dest")
}

# org.freedesktop.Sdk//<branch> and its LLVM 22 extension, from any installation; installs
# whatever is missing into the user installation (no root needed for the build).
flatpak_gl_sdk() { # <branch>
    local branch=$1 ref
    for ref in "org.freedesktop.Sdk//$branch" "org.freedesktop.Sdk.Extension.llvm22//$branch"; do
        flatpak info "$ref" >/dev/null 2>&1 && continue
        echo "  installing $ref (user, from Flathub)"
        flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
        flatpak install --user -y --noninteractive flathub "$ref" >/dev/null ||
            die "could not install $ref from Flathub"
    done
}

build_flatpak_gl() {
    local work branch key name log ext ver
    work=$(flatpak_gl_work); name=$(flatpak_gl_name)
    command -v flatpak >/dev/null || { echo "  no flatpak - Flatpak GL driver skipped"; return 0; }
    mkdir -p "$work"
    for branch in $(flatpak_gl_branches); do
        flatpak_gl_sdk "$branch"
        key=$( { echo "$branch $MESA_COMMIT"
                 sha256sum "$CACHE/anatase/cards/drivers/mesa/overrides.patch" 2>/dev/null || true
                 sha256sum "$here"/patches/mesa/0200*.patch "$here"/patches/mesa/0201*.patch \
                     "$here"/patches/mesa/0206*.patch "$here"/patches/mesa-26.3/*.patch \
                     "$here"/patches/mesa-26.3-backports/*.patch \
                     "$here/tools/flatpak-gl/build-in-sdk.sh" 2>/dev/null || true
                 flatpak_gl_sources
                 flatpak info --show-commit "org.freedesktop.Sdk//$branch"
                 flatpak info --show-commit "org.freedesktop.Sdk.Extension.llvm22//$branch"
               } | sha256sum | cut -c1-32 )
        if cache_valid "flatpak-gl-$branch" "$key" &&
           ostree --repo="$work/repo" rev-parse "$(flatpak_gl_ref "$branch")" >/dev/null 2>&1; then
            echo "  Flatpak GL driver $branch: build cached (${key:0:8})"; continue
        fi
        say "Flatpak GL driver: Mesa for org.freedesktop.Platform//$branch (SDK build, 20-40 min)"
        fetch_flatpak_gl_sources "$work/dl"
        prepare_mesa_src "$work/mesa-src"
        install -m 0755 "$here/tools/flatpak-gl/build-in-sdk.sh" "$work/build-in-sdk.sh"
        log="$work/build-$branch.log"
        progress_run "Flatpak GL $branch: SDK build" "$log" \
            flatpak run --filesystem="$work" --unshare=network --command=bash \
                "org.freedesktop.Sdk//$branch" "$work/build-in-sdk.sh" "$work" "$branch" "$name" "$work/mesa-src" ||
            { tail -30 "$log"; die "Flatpak GL build failed (full log: $log)"; }
        grep -q FLATPAK_GL_BUILD_DONE "$log" || { tail -30 "$log"; die "Flatpak GL build incomplete (log: $log)"; }

        # package: <ext>/metadata + <ext>/files, committed to a local OSTree repo
        ext="$work/ext-$branch"; rm -rf "$ext"; mkdir -p "$ext"
        mv "$work/stage-$branch/usr/lib/x86_64-linux-gnu/GL/$name" "$ext/files"
        cat > "$ext/metadata" <<EOF
[Runtime]
name=org.freedesktop.Platform.GL.$name
runtime=org.freedesktop.Platform/x86_64/$branch
sdk=org.freedesktop.Sdk/x86_64/$branch

[ExtensionOf]
ref=runtime/org.freedesktop.Platform/x86_64/$branch
priority=100
EOF
        ver=$(flatpak_gl_mesa_version "$ext/files")
        flatpak build-export --runtime --files=files \
            --subject="$ver ${MESA_COMMIT:0:12} + patches/mesa" "$work/repo" "$ext" "$branch" >/dev/null ||
            die "flatpak build-export failed"
        cache_store "flatpak-gl-$branch" "$key"
        echo "  $(flatpak_gl_ref "$branch"): $ver ($(du -sh "$ext/files" | cut -f1))"
    done
}

install_flatpak_gl() {
    local work repo sys branch ref env_file
    work=$(flatpak_gl_work); repo="$work/repo"; sys=$(flatpak_gl_sysrepo); env_file=$(flatpak_gl_env_file)
    command -v flatpak >/dev/null || return 0
    [[ -d $repo/refs ]] || { echo "  no Flatpak GL build in $repo - skipped (run build)"; return 0; }
    sudo mkdir -p "$sys"
    sudo rsync -a --delete "$repo/" "$sys/"
    sudo flatpak remote-add --system --if-not-exists --no-gpg-verify --prio=1 \
        --title="OneXPlayer 3 Mesa (local)" oxp3-gl "file://$sys"
    for branch in $(flatpak_gl_branches); do
        ref=$(flatpak_gl_ref "$branch")
        ostree --repo="$sys" rev-parse "$ref" >/dev/null 2>&1 || { echo "  $ref not built - skipped"; continue; }
        if flatpak info --system "$ref" >/dev/null 2>&1; then
            sudo flatpak update --system -y --noninteractive "$ref" >/dev/null
        else
            sudo flatpak install --system -y --noninteractive oxp3-gl "$ref" >/dev/null
        fi
        flatpak pin --system 2>/dev/null | grep -qxF "$ref" || sudo flatpak pin --system "$ref" >/dev/null
        echo "  $ref: $(flatpak info --system --show-commit "$ref" | cut -c1-12) installed and pinned"
    done
    sudo mkdir -p "$(dirname "$env_file")"
    printf '# oxp3-fixes: our Mesa (org.freedesktop.Platform.GL.%s) for Flatpak apps, Flathub'"'"'s as fallback\nFLATPAK_GL_DRIVERS=%s:default\n' \
        "$(flatpak_gl_name)" "$(flatpak_gl_name)" | sudo tee "$env_file" >/dev/null
    echo "  $env_file: FLATPAK_GL_DRIVERS=$(flatpak_gl_name):default (log out and back in to apply)"
}

uninstall_flatpak_gl() {
    local ref
    command -v flatpak >/dev/null || return 0
    for ref in $(flatpak list --system --runtime --columns=ref 2>/dev/null | grep "^org.freedesktop.Platform.GL.$(flatpak_gl_name)/"); do
        ref="runtime/$ref"
        flatpak pin --system 2>/dev/null | grep -qxF "$ref" && sudo flatpak pin --system --remove "$ref" >/dev/null
        sudo flatpak uninstall --system -y --noninteractive "$ref" >/dev/null && echo "  removed $ref"
    done
    flatpak remotes --system --columns=name 2>/dev/null | grep -qx oxp3-gl && sudo flatpak remote-delete --system --force oxp3-gl
    sudo rm -rf "$(flatpak_gl_sysrepo)" "$(flatpak_gl_env_file)"
}

status_flatpak_gl() {
    local branch ref lib env_file
    env_file=$(flatpak_gl_env_file)
    command -v flatpak >/dev/null || return 0
    for branch in $(flatpak_gl_branches); do
        ref=$(flatpak_gl_ref "$branch")
        if lib=$(flatpak info --system --show-location "$ref" 2>/dev/null); then
            echo "  Flatpak GL driver $branch: $(flatpak_gl_mesa_version "$lib/files"), commit $(flatpak info --system --show-commit "$ref" | cut -c1-12)"
        else
            echo "  Flatpak GL driver $branch: not installed (Flathub apps use Flathub's Mesa)"
        fi
    done
    if [[ -r $env_file ]]; then
        echo "  $(grep -h '^FLATPAK_GL_DRIVERS=' "$env_file") ($env_file); this session: ${FLATPAK_GL_DRIVERS:-<unset>}"
    fi
}

# ---------------------------------------------------------------------------------------------
# deployment state and the read-only status report
# ---------------------------------------------------------------------------------------------

# State of the deployments: is one staged, and which layered local packages does the booted
# one have?
deploy_info() {
    rpm-ostree status --json | python3 -c 'import json,sys
ds=json.load(sys.stdin)["deployments"]
b=[x for x in ds if x.get("booted")][0]
print("staged" if any(x.get("staged") for x in ds) else "none")
print(" ".join(b.get("requested-local-packages",[])))'
}

hotfix_active() { rpm-ostree status --booted | grep -q 'Unlocked: hotfix'; }

# DXVK version behind each installed Proton, and what Steam will actually use.
#
# Measured on this machine (docs/DX11-PERFORMANCE.md, GravityMark, D3D11, headless, 1280x720):
# DXVK 3.1.x runs the D3D11 path at ~88 fps where 3.0.2 gives ~82 and 2.7.1 ~81, against native
# Vulkan's ~95. DXVK is what translates D3D11, so for a DX11-heavy library the Proton that ships
# DXVK >= 3.1 is worth ~7-8% on its own - no patch, just picking the right runtime.
dxvk_version() { # <proton dir>
    local d=$1 f
    for f in "$d"/files/lib64/wine/dxvk/version "$d"/files/lib/wine/dxvk/version; do
        [[ -r $f ]] || continue
        awk '{ v = $NF; gsub(/[()]/, "", v); print v }' "$f"
        return 0
    done
}

proton_dir_for() { # <compat tool name>
    local name=$1 d steam=${STEAM_DIR:-$HOME/.local/share/Steam}
    for d in "$steam/compatibilitytools.d/$name" "$steam/steamapps/common/$name"; do
        [[ -d $d ]] && { printf '%s' "$d"; return 0; }
    done
    case ${name,,} in
        *experimental*) d="$steam/steamapps/common/Proton - Experimental" ;;
        *hotfix*)       d="$steam/steamapps/common/Proton Hotfix" ;;
        *stable*|proton_9*) d="$steam/steamapps/common/Proton 11.0" ;;
        *) return 1 ;;
    esac
    [[ -d $d ]] && printf '%s' "$d"
}

dxvk_is_fast() { # <vX.Y.Z...>: fast when >= 3.1
    awk -v v="${1#v}" 'BEGIN { split(v, a, "[.]"); exit !(a[1] > 3 || (a[1] == 3 && a[2] >= 1)) }'
}

# One-line verdict for the install output: is the runtime Steam would launch with on the fast
# DX11 path? (measured: DXVK 3.1.x ~88 fps vs 3.0.2 ~82 on the same D3D11 workload)
dxvk_advice() {
    local steam=${STEAM_DIR:-$HOME/.local/share/Steam} cfg tool d v
    cfg=$steam/config/config.vdf
    [[ -r $cfg ]] || return 0
    tool=$(python3 "$here/tools/lib/dxvk-steam-default.py" "$cfg" 2>/dev/null) || return 0
    [[ -n $tool ]] || return 0
    if ! d=$(proton_dir_for "$tool"); then
        echo "  Steam default Proton: $tool (not installed here)"
        return 0
    fi
    v=$(dxvk_version "$d")
    if dxvk_is_fast "${v:-3.0}"; then
        echo "  Steam default Proton: $tool (DXVK ${v:-?}) - on the DX11 fast path"
    else
        echo "  Steam default Proton: $tool (DXVK ${v:-?}) - DX11 titles run ~8% slower than on DXVK 3.1.x"
        echo "    pick a 3.1.x runtime for DX11 titles: tools/steam-compat-tool.sh list (docs/DX11-PERFORMANCE.md)"
    fi
    return 0
}

status_dxvk() {
    local steam=${STEAM_DIR:-$HOME/.local/share/Steam}
    [[ -d $steam ]] || { echo "  Steam not found - DXVK versions not checked"; return 0; }
    local d v low=0
    for d in "$steam"/compatibilitytools.d/*/ "$steam"/steamapps/common/Proton*/; do
        [[ -d $d ]] || continue
        v=$(dxvk_version "${d%/}")
        [[ -n $v ]] || continue
        printf '  %-30s DXVK %s' "$(basename "${d%/}")" "$v"
        if dxvk_is_fast "$v"; then echo "  (dx11 fast path)"
        else echo "  (dx11 ~7-8% slower than 3.1.x)"; low=1; fi
    done
    local cfg=$steam/config/config.vdf tool=""
    if [[ -r $cfg ]]; then
        tool=$(python3 "$here/tools/lib/dxvk-steam-default.py" "$cfg" 2>/dev/null || true)
        if [[ -n $tool ]]; then
            printf '  Steam default compat tool: %s' "$tool"
            if d=$(proton_dir_for "$tool"); then
                v=$(dxvk_version "$d")
                printf ' (DXVK %s)' "${v:-?}"
                dxvk_is_fast "${v:-3.0}" || low=1
                echo
            else
                echo " (not installed)"
            fi
        fi
    fi
    (( low )) && echo "  for DX11-heavy titles pick a Proton with DXVK >= 3.1 (docs/DX11-PERFORMANCE.md)"
    return 0
}

do_status() {
    echo "kernel (deployment): $(anatase_kver)   booted: $(uname -r)"
    rpm -q gamescope mangohud mesa-vulkan-drivers kmod-oxp3-xe "$KMOD_NAME" "$FW_NAME" 2>&1 | grep -v 'is not installed' | sed 's/^/  /' || true
    rpm-ostree status --booted | grep -E 'Version|LocalPackages|Initramfs|Unlocked' | sed 's/^/  /' || true
    echo "  rapl-power service: $(systemctl is-active rapl-power.service 2>/dev/null)"
    local f; for f in /run/rapl-power/*; do [[ -e $f ]] && echo "    $(basename "$f"): $(cat "$f") W"; done
    ls /etc/udev/rules.d/70-*rapl.rules >/dev/null 2>&1 && echo "  WARNING: old RAPL udev rule still present (run install)"
    stat -c '  RAPL counters: %A %n' /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null
    echo "  Mesa: host $(host_mesa)$(grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so && echo ' with the XeSS XMX patches'), Steam Flatpak: $(flatpak_mesa)"
    local m; for m in xe iwlwifi iwlmld btintel btintel_pcie; do
        printf '  %-13s %s\n' "$m:" "$(modinfo -F filename "$m" 2>/dev/null)"
    done
    journalctl -k -b 0 --no-pager 2>/dev/null | sed -n \
        -e 's/.*\(Finished loading DMC firmware .*\)/  \1/p' \
        -e 's/.*microcode: \(Current revision: .*\)/  microcode \1/p' \
        -e 's/.*Bluetooth: hci0: \(Firmware timestamp .*\)/  Bluetooth \1/p' | sort -u
    local u st; for u in "${EXTRA_UNITS[@]}" "oxp3-audio-resume@$(id -un).service" user:oxp3-audio-route.service; do
        if [[ $u == user:* ]]; then st=$(systemctl --user is-enabled "${u#user:}" 2>/dev/null || true)
        else st=$(systemctl is-enabled "$u" 2>/dev/null || true); fi
        printf '  %-34s %s\n' "${u#user:}:" "${st:-missing}"
    done
    echo "  package PL1: MSR $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 )) W," \
         "MMIO $(( $(cat /sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 )) W"
    local site; site=$(hhd_site)
    local hp st; for hp in "${HHD_PATCHES[@]}"; do
        if [[ -z $site ]]; then st="hhd not found"
        elif git -C "$site" apply --check -R -p1 "$hp" >/dev/null 2>&1; then st=applied
        elif git -C "$site" apply --check -p1 "$hp" >/dev/null 2>&1; then st="not applied (run ./oxp3-install.sh)"
        else st="does not fit this hhd"; fi
        echo "  hhd ${hp##*/}: $st"
    done
    echo "  oxp3-hhd-keep.service: $(systemctl is-enabled oxp3-hhd-keep.service 2>/dev/null || echo missing)$( [[ -e $VOLUME_HWDB ]] && echo ', volume force-release on')"
    if [[ -x $here/tools/oxp3-hw-install.sh ]]; then
        "$here/tools/oxp3-hw-install.sh" --status 2>/dev/null | tail -n +2 | sed 's/^/  /' || true
    fi
    status_flatpak_gl
    status_xess
    status_dxvk
    [[ -d $OUT ]] && { echo "built packages in $OUT:"; ls "$OUT" | sed 's/^/  /'; }
}
