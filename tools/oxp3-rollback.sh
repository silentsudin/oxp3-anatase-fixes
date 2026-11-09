#!/usr/bin/env bash
# oxp3-rollback - the undo half of the host edits this repository makes.
#
# The installers create files (units, rules, binaries) and remove them again on uninstall.
# A few files are *modified in place* instead: the Heroic launcher in the user's home, the
# Heroic GamesConfig JSON, the Bluetooth dracut drop-in and the hhd Python files we patch.
# Those are the ones a plain uninstall cannot reconstruct, so every change to them is backed
# up first under a state directory and recorded in a manifest. This tool performs those
# edits, adopts edits that were made by hand before the mechanism existed, and puts the
# previous content back.
#
#   tools/oxp3-rollback.sh status                 what is recorded and what the files hold now
#   tools/oxp3-rollback.sh list                   backups on disk (one per edit)
#   tools/oxp3-rollback.sh apply                  (re)install the Heroic launcher wrapper
#   tools/oxp3-rollback.sh adopt                  retroactively record edits made by hand
#   tools/oxp3-rollback.sh restore [--force]      undo every recorded edit (newest backup wins)
#   tools/oxp3-rollback.sh restore-file PATH      undo one file (the Bluetooth unit uses this)
#   tools/oxp3-rollback.sh verify [--json]        read-only: which of our edits have no backup
#
# Internal helpers the installers call (not for interactive use):
#   backup PATH                    print a stamp after saving PATH's pre-image, even if absent
#   record --ts STAMP PATH         append the manifest row for that stamp
#   has-record PATH                exit 0 when PATH already has a manifest record
#
# A file we did not create is copied to
#   <state>/backups/<UTC stamp>/<key>        (cp -a: byte-identical, mode and mtime preserved)
# and every edit appends one row to <state>/manifest.tsv:
#   ts  tool  event  key  logical  real  post_sha  note
# <state> is ${XDG_STATE_HOME:-$HOME/.local/state}/oxp3-fixes for the user, the invoking
# user's state when run with sudo, or /var/lib/oxp3-fixes for root without a sudo session.
# See docs/ROLLBACK.md for the full contract and the manual steps that remain.
#
# Restore never destroys a user edit: when the file no longer matches what we wrote, it is
# left alone and reported (exit 3); --force first saves the user's version as a new backup,
# then restores the original. Files we created are removed only while they still match.
#
#   --user NAME     whose home is edited (default: $SUDO_USER, else the current user)
#   --root DIR      simulate every path under DIR (offline test; nothing outside DIR)
#   --dry-run       print what would happen, write nothing
#   --json          machine-readable status/verify output
#
# Exit codes: 0 ok (nothing left to undo counts as ok), 1 failure, 2 usage, 3 something was
# skipped because the user edited it after us.
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
PROG=oxp3-rollback
VERSION=1.0.0

# The undo primitives live in the shared install library (sourcing it defines functions only).
# shellcheck source=lib/install-common.sh
source "$repo/tools/lib/install-common.sh"

say() { printf '\n== %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }

usage() {
    sed -n '2,43p' "$0" | sed 's/^# \{0,1\}//'
}

CMD=""
POS=()
ROOT=""
USER_ARG=""
FORCE=0
DRY=0
JSON=0
ADOPT_ONLY=0
ONLY=""
HEROIC_ENV_CLEAN=""

while (( $# )); do
    case $1 in
        status|list|apply|adopt|restore|restore-file|verify|has-record)
            CMD=$1; shift; continue ;;
        --user) USER_ARG=${2:-}; shift 2 || shift; continue ;;
        --user=*) USER_ARG=${1#*=}; shift; continue ;;
        --root) ROOT=${2:-}; shift 2 || shift; continue ;;
        --root=*) ROOT=${1#*=}; shift; continue ;;
        --force|-f) FORCE=1; shift; continue ;;
        --dry-run|-n) DRY=1; shift; continue ;;
        --json) JSON=1; shift; continue ;;
        --adopt-only) ADOPT_ONLY=1; shift; continue ;;
        --only) ONLY=${2:-}; shift 2 || shift; continue ;;
        --only=*) ONLY=${1#*=}; shift; continue ;;
        --heroic-env-clean) HEROIC_ENV_CLEAN=${2:-all}; shift 2 || shift; continue ;;
        --heroic-env-clean=*) HEROIC_ENV_CLEAN=${1#*=}; shift; continue ;;
        -h|--help|help) usage; exit 0 ;;
        --) shift; POS+=("$@"); break ;;
        -*) die "unknown option: $1 (try --help)" ;;
        *) POS+=("$1"); shift ;;
    esac
done
[[ -n $CMD ]] || { usage; exit 2; }

# ---------------------------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------------------------
if [[ -n $ROOT ]]; then
    ROOT=${ROOT%/}
    [[ -d $ROOT ]] || die "--root directory does not exist: $ROOT"
    ROOT=$(CDPATH= cd -- "$ROOT" && pwd)
    [[ -n $ROOT ]] || die "--root=/ is not a simulation"
fi

target_user() {
    if [[ -n $USER_ARG ]]; then printf '%s' "$USER_ARG"; return 0; fi
    if [[ -n ${SUDO_USER:-} && ${SUDO_USER:-} != root ]]; then printf '%s' "$SUDO_USER"; return 0; fi
    id -un
}
TARGET_USER=$(target_user)
id -u "$TARGET_USER" >/dev/null 2>&1 || die "no such user: $TARGET_USER"
HOME_LOGICAL=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[[ -n $HOME_LOGICAL ]] || die "cannot resolve the home directory of $TARGET_USER"

# Every path this tool touches is derived from these two: the simulation prefix and the real
# home. The library maps them back to "home/..." and "abs/..." keys.
OXP3_HOME=$HOME_LOGICAL
OXP3_ROOT=$ROOT
export OXP3_HOME OXP3_ROOT
REAL_HOME="$ROOT$HOME_LOGICAL"
if [[ -n $ROOT ]]; then
    OXP3_STATE_DIR="$REAL_HOME/.local/state/oxp3-fixes"
    export OXP3_STATE_DIR
fi
STATE=$(oxp3_state_dir)

# ---------------------------------------------------------------------------------------------
# the files we edit in place, and the artifacts an earlier session created outside the repo
# ---------------------------------------------------------------------------------------------
launcher_path() { printf '%s/heroic-launch.sh' "$REAL_HOME"; }

heroic_json_paths() {
    local f
    for f in "$REAL_HOME"/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig/*.json; do
        [[ -f $f ]] && printf '%s\n' "$f"
    done
}

# Created by hand in an earlier session (docs/ROLLBACK.md, "artifacts outside the repo"). They
# are adopted as files we created, so a restore can remove them while they stay untouched.
agent_created_paths() {
    local f
    for f in "$REAL_HOME"/.config/xess-xmx/oxp3-env.sh \
             "$REAL_HOME"/.config/xess-xmx/icd-*.json \
             "$REAL_HOME"/.config/oxp3-vkbasalt.conf \
             "$REAL_HOME"/.config/systemd/user/xess-xmx-*.service \
             "$REAL_HOME"/.config/systemd/user/xess-xmx-*.path; do
        [[ -f $f ]] && printf '%s\n' "$f"
    done
}

has_record() { # KEY
    [[ -n $(oxp3_rows_for_key "$1") ]]
}

last_post() { # KEY -> the content sha the newest write recorded
    oxp3_rows_for_key "$1" | awk -F'\t' \
        '$3 == "backup" || $3 == "created" || $3 == "adopted"' | tail -n 1 | cut -f7
}

newest_backup_dir() { # KEY -> backups/<ts> of the newest write, or empty
    local row ts
    row=$(oxp3_rows_for_key "$1" | awk -F'\t' \
        '$3 == "backup" || $3 == "created" || $3 == "adopted"' | tail -n 1)
    [[ -n $row ]] || return 0
    ts=$(printf '%s' "$row" | cut -f1)
    printf '%s/backups/%s' "$STATE" "$ts"
}

render_launcher() {
    sed -e "s|/var/home/holzr/git/linux_scratch/oxp3-anatase-fixes|$repo|g" \
        -e "s|%h/git/linux_scratch/oxp3-anatase-fixes|$repo|g" \
        "$repo/tools/heroic-launch.sh"
}

wiring_present() { # PATH
    [[ -f $1 ]] && grep -qE 'cp2077-direct\.sh|oxp3-gamewrap\.sh|heroic://' "$1"
}

newest_sibling_bak() { # PATH
    ls -1t "$1".bak* 2>/dev/null | head -1 || true
}

# Record a path we edited by hand: use the newest sibling .bak* as the pre-image when it
# differs, otherwise record it with no earlier image (restore then reports "nothing to undo").
adopt_one() { # PATH KIND
    local path=$1 kind=$2 key pre bak note
    key=$(oxp3_key "$path")
    has_record "$key" && { info "already recorded: ${path#"$ROOT"}"; return 0; }
    case $kind in
        launcher) note="retroactive: the Heroic wrapper wiring was installed by hand" ;;
        json)     note="retroactive: per-game Heroic XMX envVars were written by hand" ;;
        *)        note="adopted: created outside the repo by an earlier session" ;;
    esac
    if (( DRY )); then
        info "would adopt: ${path#"$ROOT"} ($kind)"; return 0
    fi
    if [[ $kind == created ]]; then
        local row
        row=$(oxp3_backup_image "$path" "$PROG") || return 1
        oxp3_manifest_record "$path" "$PROG" "${row%%$'\t'*}" created "$note"
        info "adopted (created): ${path#"$ROOT"}"
        return 0
    fi
    bak=$(newest_sibling_bak "$path")
    if [[ -n $bak && $(oxp3_sha "$bak") != "$(oxp3_sha "$path")" ]]; then
        local row
        row=$(oxp3_adopt_image "$path" "$bak") || return 1
        oxp3_manifest_record "$path" "$PROG" "${row%%$'\t'*}" adopted \
            "$note; pre-edit image from ${bak##*/}"
        info "adopted: ${path#"$ROOT"} (pre-edit image from ${bak##*/})"
    else
        local row
        row=$(oxp3_backup_image "$path" "$PROG") || return 1
        oxp3_manifest_record "$path" "$PROG" "${row%%$'\t'*}" adopted \
            "$note; no earlier image exists, so restore leaves it alone"
        info "adopted: ${path#"$ROOT"} (no earlier image)"
    fi
}

adopt_all() {
    local path n=0 pre
    pre=$(oxp3_state_dir)
    if [[ ! -f $pre/manifest.tsv ]]; then
        (( DRY )) || oxp3_state_init >/dev/null || die "cannot create the state directory $pre"
    fi
    path=$(launcher_path)
    if [[ -f $path ]]; then adopt_one "$path" launcher; n=$((n + 1)); fi
    while IFS= read -r path; do
        adopt_one "$path" json; n=$((n + 1))
    done < <(heroic_json_paths)
    while IFS= read -r path; do
        adopt_one "$path" created; n=$((n + 1))
    done < <(agent_created_paths)
    say "adopted $n known path(s) from this session's edits"
    info "state: $STATE"
    info "undo with: $PROG restore"
}

# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
cmd_list() {
    local d n
    [[ -d $STATE/backups ]] || { info "no backups yet ($STATE)"; return 0; }
    n=0
    while IFS= read -r d; do
        n=$((n + 1))
        printf '%s  (%d file%s)\n' "${d##*/}" \
            "$(find "$d" -type f | wc -l)" "$( [ "$(find "$d" -type f | wc -l)" = 1 ] && echo '' || echo 's')"
    done < <(find "$STATE/backups" -mindepth 1 -maxdepth 1 -type d | sort)
    [[ $n -gt 0 ]] || info "no backups yet ($STATE)"
}

cmd_status() {
    local keys=() i key rows last real cur post pre store mark
    if (( JSON )); then
        printf '{"schema":1,"tool":"%s","version":"%s","state_dir":"%s","manifest":"%s","edits":[' \
            "$PROG" "$VERSION" "$STATE" "$STATE/manifest.tsv"
        keys=()
        mapfile -t keys < <(oxp3_manifest_keys)
        for i in "${!keys[@]}"; do
            key=${keys[i]}
            real=$(oxp3_logical_to_real "$key")
            rows=$(oxp3_rows_for_key "$key")
            last=$(printf '%s\n' "$rows" | tail -n 1)
            cur=$(oxp3_sha "$real"); [[ -n $cur ]] || cur=-
            post=$(last_post "$key"); [[ -n $post ]] || post=-
            (( i )) && printf ','
            printf '{"path":"%s","key":"%s","last_event":"%s","current_sha":"%s","our_sha":"%s"}' \
                "${real#"$ROOT"}" "$key" "$(printf '%s' "$last" | cut -f3)" "$cur" "$post"
        done
        printf ']}\n'
        return 0
    fi
    say "$PROG $VERSION"
    info "user       : $TARGET_USER ($HOME_LOGICAL)"
    info "state dir  : $STATE"
    [[ -f $STATE/manifest.tsv ]] && info "records    : $(wc -l <"$STATE/manifest.tsv")" \
        || info "records    : 0 (nothing installed through this tool yet)"
    [[ -d $STATE/backups ]] && info "backup dirs: $(find "$STATE/backups" -mindepth 1 -maxdepth 1 -type d | wc -l)" \
        || info "backup dirs: 0"
    mapfile -t keys < <(oxp3_manifest_keys)
    if (( ${#keys[@]} == 0 )); then
        say "no recorded edits"
        return 0
    fi
    say "recorded edits"
    printf '%-9s %-9s %-12s %s\n' "event" "state" "sha" "path"
    for key in "${keys[@]}"; do
        real=$(oxp3_logical_to_real "$key")
        rows=$(oxp3_rows_for_key "$key")
        last=$(printf '%s\n' "$rows" | tail -n 1)
        cur=$(oxp3_sha "$real"); [[ -n $cur ]] || cur=-
        post=$(last_post "$key"); [[ -n $post ]] || post=-
        if [[ $cur == - ]]; then mark=absent
        elif [[ -n $post && $post != - && $cur == "$post" ]]; then mark=ours
        elif [[ $cur == "$post" ]]; then mark=ours
        else
            pre=""
            store=$(newest_backup_dir "$key")
            [[ -n $store && -f $store/$key ]] && pre=$(oxp3_sha "$store/$key")
            if [[ -n $pre && $cur == "$pre" ]]; then mark=original; else mark=edited; fi
        fi
        printf '%-9s %-9s %-12s %s\n' "$(printf '%s' "$last" | cut -f3)" "$mark" \
            "${cur:0:12}" "${real#"$ROOT"}"
    done
}

cmd_verify() {
    # Read-only: no state directory is created, no file is written.
    local unbacked=() missing=() edited=() path key rows row ts key2 ev keys=() i n
    local manifest="$STATE/manifest.tsv"
    local -a known=()
    if [[ -f $(launcher_path) ]]; then known+=("$(launcher_path)"); fi
    while IFS= read -r path; do known+=("$path"); done < <(heroic_json_paths)
    while IFS= read -r path; do known+=("$path"); done < <(agent_created_paths)
    for path in "${known[@]}"; do
        key=$(oxp3_key "$path")
        if ! has_record "$key"; then
            unbacked+=("${path#"$ROOT"}")
        fi
    done
    if [[ -f $manifest ]]; then
        while IFS=$'\t' read -r ts _tool ev key2 _logical path post _note; do
            [[ $ev == backup || $ev == adopted ]] || continue
            [[ -f "$STATE/backups/$ts/$key2" ]] || missing+=("$path (backups/$ts/$key2)")
        done <"$manifest"
        mapfile -t keys < <(oxp3_manifest_keys)
        for key in "${keys[@]}"; do
            rows=$(oxp3_rows_for_key "$key")
            row=$(printf '%s\n' "$rows" | awk -F'\t' \
                '$3 == "backup" || $3 == "created" || $3 == "adopted"' | tail -n 1)
            [[ -n $row ]] || continue
            post=$(printf '%s' "$row" | cut -f7)
            path=$(oxp3_logical_to_real "$key")
            [[ -f $path ]] || continue
            [[ $post == - ]] && continue
            local cursha store pre
            cursha=$(oxp3_sha "$path")
            [[ $cursha == "$post" ]] && continue
            # Different from what we wrote. That is only a user edit when it is also different
            # from the pre-image (a restored file is original again, not "edited").
            store=$(newest_backup_dir "$key")
            pre=""
            [[ -n $store && -f $store/$key ]] && pre=$(oxp3_sha "$store/$key")
            [[ -n $pre && $cursha == "$pre" ]] && continue
            edited+=("${path#"$ROOT"}")
        done
    fi
    if (( JSON )); then
        printf '{"schema":1,"tool":"%s","state_dir":"%s","manifest":%s,"unbacked":[' \
            "$PROG" "$STATE" "$( [[ -f $manifest ]] && echo true || echo false )"
        n=0
        for path in ${unbacked[@]+"${unbacked[@]}"}; do
            (( n++ )) && printf ','
            printf '{"path":"%s","reason":"no manifest record"}' "$path"
        done
        printf '],"missing_images":['
        n=0
        for path in ${missing[@]+"${missing[@]}"}; do
            (( n++ )) && printf ','
            printf '{"path":"%s","reason":"backup image is missing"}' "$path"
        done
        printf '],"edited_since":['
        n=0
        for path in ${edited[@]+"${edited[@]}"}; do
            (( n++ )) && printf ','
            printf '"%s"' "$path"
        done
        printf '],"ok":%s}\n' "$( (( ${#unbacked[@]} == 0 && ${#missing[@]} == 0 )) && echo true || echo false )"
    else
        say "$PROG verify"
        info "state dir: $STATE"
        if (( ${#unbacked[@]} == 0 && ${#missing[@]} == 0 )); then
            info "every known edit of ours has a backup"
        fi
        for path in ${unbacked[@]+"${unbacked[@]}"}; do
            printf 'UNBACKED      %s\n' "$path"
        done
        for path in ${missing[@]+"${missing[@]}"}; do
            printf 'MISSING-IMAGE %s\n' "$path"
        done
        for path in ${edited[@]+"${edited[@]}"}; do
            printf 'edited-since  %s\n' "$path"
        done
        if (( ${#unbacked[@]} || ${#missing[@]} )); then
            info "record them retroactively with: $PROG adopt"
        fi
    fi
    (( ${#unbacked[@]} == 0 && ${#missing[@]} == 0 ))
}

# Print the stamp of a fresh pre-image for PATH. A path that does not exist yet is recorded too
# (empty image), so the caller can record it as "created" after writing.
cmd_backup() { # PATH [--tool NAME]
    local path=${1:-} tool=$PROG row
    [[ -n $path ]] || die "backup needs a path"
    [[ -d $(dirname -- "$path") ]] || die "parent directory does not exist: $(dirname -- "$path")"
    row=$(oxp3_backup_image "$path" "$tool") || die "cannot back up $path"
    printf '%s\n' "${row%%$'\t'*}"
}

cmd_has_record() { # PATH -> exit 0 when a manifest record exists
    local path=${1:-}
    [[ -n $path ]] || die "has-record needs a path"
    has_record "$(oxp3_key "$path")"
}

cmd_record() { # --ts TS PATH
    local ts="" path="" tool="$PROG" note=""
    while (( $# )); do
        case $1 in
            --ts) ts=${2:-}; shift 2 || shift ;;
            --tool) tool=${2:-}; shift 2 || shift ;;
            --note) note=${2:-}; shift 2 || shift ;;
            *) path=$1; shift ;;
        esac
    done
    [[ -n $ts && -n $path ]] || die "record needs --ts STAMP PATH"
    oxp3_manifest_record "$path" "$tool" "$ts" auto "$note"
}

cmd_apply() {
    local path wanted cur key post rc=0
    if (( EUID == 0 )) && [[ -z $ROOT ]]; then
        die "the user-owned launcher must be edited as the user, not as root (use --root DIR to simulate)"
    fi
    if (( ADOPT_ONLY )); then adopt_all; return 0; fi
    path=$(launcher_path)
    wanted=$(mktemp)
    render_launcher >"$wanted"
    key=$(oxp3_key "$path")
    if [[ -f $path ]] && cmp -s "$wanted" "$path"; then
        if has_record "$key"; then
            info "already installed and recorded: ${path#"$ROOT"}"
        else
            info "wrapper already in place; recording it retroactively"
            adopt_one "$path" launcher
        fi
        rm -f "$wanted"
        if [[ -n $HEROIC_ENV_CLEAN ]]; then
            heroic_env_clean "$HEROIC_ENV_CLEAN" || rc=$?
        fi
        return $rc
    fi
    cur=$(oxp3_sha "$path")
    post=$(last_post "$key"); [[ -n $post ]] || post=""
    if [[ -n $cur && -n $post && $post != - && $cur != "$post" && $FORCE -eq 0 ]]; then
        warn "${path#"$ROOT"} was edited since our last install; not overwriting (use --force to back it up and replace)"
        rm -f "$wanted"; return 3
    fi
    if (( DRY )); then
        info "would install: ${path#"$ROOT"} (previous content backed up)"
        rm -f "$wanted"; return 0
    fi
    local row; row=$(oxp3_backup_image "$path" "$PROG") || { rm -f "$wanted"; die "cannot back up $path"; }
    mkdir -p -- "$(dirname -- "$path")"
    install -m 0755 -- "$wanted" "$path" || { rm -f "$wanted"; die "cannot install $path"; }
    oxp3_manifest_record "$path" "$PROG" "${row%%$'\t'*}" auto \
        "Heroic launcher wrapper (tools/heroic-launch.sh)"
    if [[ -n ${row##*$'\t'} ]]; then
        info "installed ${path#"$ROOT"} (previous content in backups/${row%%$'\t'*})"
    else
        info "installed ${path#"$ROOT"} (created; it did not exist)"
    fi
    rm -f "$wanted"
    if [[ -n $HEROIC_ENV_CLEAN ]]; then
        heroic_env_clean "$HEROIC_ENV_CLEAN" || rc=$?
    fi
    return $rc
}

# Remove the per-game XMX envVars from Heroic's GamesConfig JSON (opt-in; the wrapper owns the
# environment now). Every file is backed up byte-for-byte first, so restore puts the exact
# original JSON back.
heroic_env_clean() { # APPID|all
    local want=$1 path key row out
    command -v python3 >/dev/null || { warn "python3 missing; Heroic JSON left untouched"; return 1; }
    while IFS= read -r path; do
        if [[ $want != all && $path != *"/$want.json" ]]; then continue; fi
        key=$(oxp3_key "$path")
        if (( DRY )); then info "would clean: ${path#"$ROOT"}"; continue; fi
        row=$(oxp3_backup_image "$path" "$PROG") || { warn "cannot back up $path"; return 1; }
        out=$(python3 - "$path" <<'PY'
import json
import sys

KEYS = {
    "VK_DRIVER_FILES", "ANV_CM_KERNEL_DIR", "ANV_CM_HELPER", "XMX_IGC_DIR",
    "VKD3D_DISABLE_EXTENSIONS", "force_vk_vendor",
    "INTC_ALT_DRIVER_EXTENSIONS_PATH", "XESS_LOG",
}
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
removed = []
for app in data.values():
    env = app.get("envVars") if isinstance(app, dict) else None
    if not isinstance(env, dict):
        continue
    for key in [k for k in env if k in KEYS]:
        removed.append(key)
        del env[key]
if removed:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
print(len(removed))
PY
) || { warn "cannot rewrite $path"; return 1; }
        if [[ $out == 0 ]]; then
            # Nothing changed: record the pre-image as an adopted edit and drop the copy.
            rm -f "$STATE/backups/${row%%$'\t'*}/$key"
            info "no XMX envVars left in ${path#"$ROOT"}"
        else
            oxp3_manifest_record "$path" "$PROG" "${row%%$'\t'*}" auto \
                "removed $out per-game XMX envVars (wrapper owns the environment)"
            info "cleaned ${path#"$ROOT"} ($out envVars removed; original in backups/${row%%$'\t'*})"
        fi
    done < <(heroic_json_paths)
}

cmd_restore() {
    local rc=0
    local -a args=()
    (( FORCE )) && args+=(--force)
    (( DRY )) && args+=(--dry-run)
    [[ -n $ONLY ]] && args+=("--only=$ONLY")
    oxp3_restore_all "${args[@]}" || rc=$?
    case $rc in
        0) return 0 ;;
        3) printf '%s: some edits were left in place because they changed after ours (see above)\n' "$PROG" >&2
           return 3 ;;
        *) return 1 ;;
    esac
}

cmd_restore_file() {
    local path=${1:-} rc=0 code
    [[ -n $path ]] || die "restore-file needs a path"
    local -a args=()
    (( FORCE )) && args+=(--force)
    (( DRY )) && args+=(--dry-run)
    if oxp3_restore_file "$path" ${args[@]+"${args[@]}"}; then
        return 0
    else
        code=$?
    fi
    case $code in
        10|13) return 0 ;;
        12) return 3 ;;
        *) return 1 ;;
    esac
}

case $CMD in
    status)       cmd_status ;;
    list)         cmd_list ;;
    apply)        cmd_apply ;;
    adopt)        adopt_all ;;
    restore)      cmd_restore ;;
    restore-file) cmd_restore_file "${POS[0]:-}" ;;
    has-record)   cmd_has_record "${POS[0]:-}" ;;
    verify)       cmd_verify ;;
    backup)       cmd_backup "${POS[0]:-}" ;;
    record)       cmd_record ${POS[@]+"${POS[@]}"} ;;
    *)            die "unknown command: $CMD" ;;
esac
