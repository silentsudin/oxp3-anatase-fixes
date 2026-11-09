#!/bin/bash
# oxp3-hhd-voltap installer - own files only, into a prefix that survives
# rpm-ostree updates (/usr/local is /var/usrlocal on Anatase).
#
#   sudo tools/hhd-oxp3-voltap/install.sh install
#   sudo tools/hhd-oxp3-voltap/install.sh uninstall
#        tools/hhd-oxp3-voltap/install.sh status     (read-only)
#        tools/hhd-oxp3-voltap/install.sh check      (read-only)
#
# The plugin is an hhd.plugins entry point; hhd imports it at startup, so a
# plugin change needs `sudo systemctl restart hhd`.  This installer never
# restarts or enables anything itself: restarting hhd drops the emulated Steam
# controller (28DE:12FF) for a few seconds and that is the owner's call.
#
# Installed files (and nothing else):
#   <site>/oxp3_hhd_voltap/*.py
#   <site>/oxp3_hhd_voltap-<version>.dist-info/{METADATA,WHEEL,entry_points.txt,
#                                               top_level.txt,INSTALLER,RECORD}
#
# Offline validation can target a scratch prefix:
#   tools/hhd-oxp3-voltap/install.sh --site /tmp/site install
#   OXP3_HHD_VOLTAP_PYTHON=python3.15 tools/hhd-oxp3-voltap/install.sh check
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
pkg=oxp3_hhd_voltap
VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$here/pyproject.toml" | head -1)
PY=${OXP3_HHD_VOLTAP_PYTHON:-python3}
ROOT=""
SITE=""
DRY_RUN=0
ACTION=""

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

restart_notice() {
    cat <<EOF

The plugin only takes effect when hhd imports it. Restart hhd yourself:
  sudo systemctl restart hhd

  Restarting hhd drops the emulated Steam controller (28DE:12FF) and the
  volume keyboard for a few seconds. Do it outside a game.
  Do NOT grab hhd's event devices; this plugin opens none.
EOF
}

usage() {
    cat <<EOF
usage: install.sh [--root DIR] [--site DIR] [--dry-run] install|uninstall|status|check
  --root DIR    prefix every path with DIR (offline simulation)
  --site DIR    Python site-packages destination (default <root>/usr/local/lib/python3.15/site-packages)
  --dry-run     print every command, change nothing
  status        read-only: installed files and entry-point resolution
  check         read-only: is hhd present, is it running, can this build be hooked
  -h, --help    this help
EOF
}

while (($#)); do
    case $1 in
        install|uninstall|status|check) ACTION=$1 ;;
        --root) (($# >= 2)) || die "--root needs a directory"; ROOT=${2%/}; shift ;;
        --root=*) ROOT=${1#*=}; ROOT=${ROOT%/} ;;
        --site) (($# >= 2)) || die "--site needs a directory"; SITE=${2%/}; shift ;;
        --site=*) SITE=${1#*=}; SITE=${SITE%/} ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done
[ -n "$ACTION" ] || { usage >&2; exit 2; }
[ -n "$VERSION" ] || die "cannot read the version from $here/pyproject.toml"
[ -f "$here/$pkg/__init__.py" ] || die "package source missing: $here/$pkg"

PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) \
    || die "cannot run $PY (set OXP3_HHD_VOLTAP_PYTHON)"
[ -n "$SITE" ] || SITE="$ROOT/usr/local/lib/python${PYVER}/site-packages"
DISTINFO="$SITE/${pkg}-${VERSION}.dist-info"
PKGDIR="$SITE/$pkg"

root_check() {
    # Only the real /usr tree needs root; a --site/--root sandbox does not.
    case $SITE in
        /usr/*)
            if [ "$(id -u)" -ne 0 ]; then
                die "installing into $SITE needs root; rerun with sudo, or use --site DIR for a sandbox"
            fi
            ;;
    esac
}

run() {
    if ((DRY_RUN)); then
        printf '  [dry-run]'; printf ' %q' "$@"; printf '\n'
        return 0
    fi
    printf '  +'; printf ' %q' "$@"; printf '\n'
    "$@"
}

probe_installed() { # exit 0 when the entry point resolves to our plugin
    PYTHONPATH="$SITE${PYTHONPATH:+:$PYTHONPATH}" "$PY" - <<'PY'
from importlib.metadata import entry_points
wanted = "oxp3_voltap"
eps = [e for e in entry_points(group="hhd.plugins") if e.name == wanted]
if len(eps) != 1:
    print(f"ENTRY=missing (found {len(eps)} entry points named {wanted})")
    raise SystemExit(1)
fn = eps[0].load()
plugins = fn([])
names = [getattr(p, "name", "?") for p in plugins]
if names != [wanted]:
    print(f"ENTRY=bad (autodetect returned {names})")
    raise SystemExit(1)
plugin = plugins[0]
print(f"ENTRY=ok NAME={plugin.name} VERSION={getattr(__import__('oxp3_hhd_voltap'), '__version__', '?')}")
describe = getattr(plugin, "describe", None)
if callable(describe):
    d = describe()
    print(f"HOOK_ENABLED={d.get('enabled')} HOLD_MS={d.get('hold_ms')} "
          f"GAP_MS={d.get('gap_ms')} MAX_HOLD_MS={d.get('max_hold_ms')}")
PY
}

hook_probe() {
    local where=$1
    PYTHONPATH="$where${PYTHONPATH:+:$PYTHONPATH}" "$PY" - <<'PY'
import json
try:
    from oxp3_hhd_voltap import hhd_hook
    print(json.dumps(hhd_hook.probe()))
except Exception as exc:
    print(json.dumps({"available": False, "reason": f"cannot probe: {exc}"}))
PY
}

hhd_state() {
    if ! command -v systemctl >/dev/null 2>&1; then
        printf 'unknown (no systemctl)'
        return 0
    fi
    local active enabled
    active=$(systemctl is-active hhd 2>/dev/null || true)
    enabled=$(systemctl is-enabled hhd 2>/dev/null || true)
    printf '%s/%s' "${enabled:-?}" "${active:-?}"
}

do_status() {
    local state="absent" ver=""
    if [ -d "$PKGDIR" ]; then
        state="present"
        [ -f "$DISTINFO/METADATA" ] && ver=$(sed -n 's/^Version: //p' "$DISTINFO/METADATA")
    fi
    say "site:   $SITE"
    say "package: $state${ver:+ (version $ver)}"
    if [ "$state" = present ]; then
        say "files:"
        find "$PKGDIR" "$DISTINFO" -maxdepth 1 -type f 2>/dev/null | sed 's/^/  /' | sort
        if probe_installed; then :; else warn "entry point does not resolve to the plugin"; fi
    else
        say "entry:  not installed"
    fi
    say "hhd:    $(hhd_state) (enabled/active)"
    if [ "$state" = present ]; then
        local probe
        probe=$(hook_probe "$SITE")
        say "hook:   $probe"
    fi
}

do_check() {
    say "site:   $SITE"
    if [ -d "$PKGDIR" ]; then
        say "package: present (installed copy is what the check uses)"
        hook_probe "$SITE"
    else
        say "package: not installed; probing the checkout instead"
        hook_probe "$here"
    fi
    say "hhd:    $(hhd_state) (enabled/active)"
    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl is-active --quiet hhd 2>/dev/null; then
            warn "hhd is not running; the plugin cannot be loaded until it is"
        fi
    fi
    cat <<'EOF'

Reminder: never let a tool grab hhd's own virtual devices. The only supported
volume path is this plugin (docs/DAEMONS.md, "never grab what HHD owns").
EOF
}

do_install() {
    root_check
    say "installing oxp3-hhd-voltap $VERSION into $SITE"
    run install -d -m 0755 "$SITE"
    run install -d -m 0755 "$PKGDIR"
    local f
    for f in "$here/$pkg"/*.py; do
        run install -m 0644 "$f" "$PKGDIR/$(basename "$f")"
    done
    if [ -d "$PKGDIR/__pycache__" ]; then
        # A stale cache from a previous version must not shadow the new code.
        run rm -rf "$PKGDIR/__pycache__"
    fi
    run install -d -m 0755 "$DISTINFO"
    if ((DRY_RUN)); then
        printf '  [dry-run] write %s\n' \
            "$DISTINFO/METADATA" "$DISTINFO/WHEEL" "$DISTINFO/entry_points.txt" \
            "$DISTINFO/top_level.txt" "$DISTINFO/INSTALLER" "$DISTINFO/RECORD"
    else
        cat >"$DISTINFO/METADATA" <<EOF
Metadata-Version: 2.1
Name: oxp3-hhd-voltap
Version: $VERSION
Summary: OneXPlayer 3 volume rocker tap normalisation as an HHD plugin
Home-page: https://github.com/silentsudin/oxp3-anatase-fixes
Author: silentsudin
Author-email: 25103587+silentsudin@users.noreply.github.com
License: GPL-2.0-or-later
Requires-Python: >=3.10
Description-Content-Type: text/markdown
EOF
        cat "$here/README.md" >>"$DISTINFO/METADATA"
        cat >"$DISTINFO/WHEEL" <<EOF
Wheel-Version: 1.0
Generator: oxp3-hhd-voltap install.sh ($VERSION)
Root-Is-Purelib: true
Tag: py3-none-any
EOF
        cat >"$DISTINFO/entry_points.txt" <<EOF
[hhd.plugins]
oxp3_voltap = oxp3_hhd_voltap:autodetect
EOF
        printf 'oxp3_hhd_voltap\n' >"$DISTINFO/top_level.txt"
        printf 'oxp3-hhd-voltap install.sh\n' >"$DISTINFO/INSTALLER"
        # RECORD: every installed file with its digest; RECORD itself has none.
        (
            cd "$SITE"
            : >"$DISTINFO/RECORD"
            for f in "$pkg"/*.py "$DISTINFO/METADATA" "$DISTINFO/WHEEL" \
                     "$DISTINFO/entry_points.txt" "$DISTINFO/top_level.txt" \
                     "$DISTINFO/INSTALLER"; do
                rel=${f#"$SITE"/}
                if command -v sha256sum >/dev/null 2>&1; then
                    digest=$(sha256sum "$f" | cut -d' ' -f1)
                    size=$(wc -c <"$f")
                    printf '%s,sha256=%s,%s\n' "$rel" "$digest" "$size"
                else
                    printf '%s,,\n' "$rel"
                fi
            done >"$DISTINFO/RECORD"
            printf '%s,,\n' "${DISTINFO#"$SITE"/}/RECORD" >>"$DISTINFO/RECORD"
        )
    fi
    if ((DRY_RUN)); then
        say "dry run: nothing written"
        restart_notice
        return 0
    fi
    say "installed files:"
    find "$PKGDIR" "$DISTINFO" -maxdepth 1 -type f | sed 's/^/  /' | sort
    if ! probe_installed; then
        die "entry-point probe failed after install; remove $PKGDIR and $DISTINFO"
    fi
    restart_notice
}

do_uninstall() {
    root_check
    say "removing oxp3-hhd-voltap from $SITE"
    local found=0
    if [ -d "$PKGDIR" ]; then
        found=1
        run rm -rf "$PKGDIR"
    fi
    if [ -d "$DISTINFO" ]; then
        found=1
        run rm -rf "$DISTINFO"
    fi
    # Only ever remove our own names; a glob cannot escape them.
    local d
    for d in "$SITE"/oxp3_hhd_voltap-*.dist-info; do
        [ -d "$d" ] || continue
        case $(basename "$d") in
            oxp3_hhd_voltap-*.dist-info) found=1; run rm -rf "$d" ;;
            *) warn "refusing to remove unexpected path: $d" ;;
        esac
    done
    ((DRY_RUN)) && { say "dry run: nothing removed"; restart_notice; return 0; }
    if ((found == 0)); then
        say "nothing was installed in $SITE"
    else
        say "removed $PKGDIR and $DISTINFO (nothing else was touched)"
    fi
    if probe_installed >/dev/null 2>&1; then
        warn "an entry point named oxp3_voltap still resolves; another install of this package exists outside $SITE"
    else
        say "entry:  not installed any more"
    fi
    restart_notice
}

case $ACTION in
    install) do_install ;;
    uninstall) do_uninstall ;;
    status) do_status ;;
    check) do_check ;;
esac
