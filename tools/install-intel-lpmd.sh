#!/bin/bash
# SPDX-License-Identifier: MIT
# install-intel-lpmd.sh - install Intel's 0.1.1-test lpmd with per-process cpusets.
#
# Enables <UseProcessCPUSet>1</UseProcessCPUSet> and seeds game classifications in
# /etc/intel_lpmd/process_cpuset_user.xml, then runs the new binary through a
# systemd drop-in (the distro package stays installed and is restored on
# --uninstall).
#
#   sudo ./tools/build-intel-lpmd.sh
#   sudo ./tools/install-intel-lpmd.sh
#   sudo ./tools/install-intel-lpmd.sh --uninstall
#
# Options: --dry-run  --uninstall  --games "name=class ..."
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# Under sudo, $HOME is /root; use the invoking user's cache/state directories.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    user_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    user_home=$HOME
fi
cache=${OXP3_CACHE:-$user_home/.cache/oxp3-fixes}
stage=$cache/intel-lpmd/stage
state=${XDG_STATE_HOME:-$user_home/.local/state}/oxp3-fixes/intel-lpmd
root=${OXP3_LPMD_ROOT:-}
dry=0 uninstall=0
games="udpre=game_profile_cpu uDPRE.exe=game_profile_cpu Dpr.exe=game_profile_cpu eldenring.exe=game_profile_gpu"
say() { printf '== %s\n' "$*"; }
run() { if (( dry )); then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

while (($#)); do
    case $1 in
        --dry-run) dry=1 ;;
        --uninstall) uninstall=1 ;;
        --games) games=$2; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
confdir=$root/etc/intel_lpmd
unitdir=$root/etc/systemd/system
bindir=$root/usr/local/bin
libexecdir=$root/usr/local/libexec
dropin=$unitdir/intel_lpmd.service.d/90-oxp3-test.conf

if (( uninstall )); then
    say "restoring the packaged intel-lpmd"
    if [ -f "$state/conf.tar" ]; then
        run rm -rf "$confdir"
        run mkdir -p "$root/etc"
        run tar -C "$root/etc" -xf "$state/conf.tar"
    fi
    run rm -f "$dropin"
    run rm -f "$bindir/intel_lpmd" "$libexecdir/intel_lpmd_focus_helper"
    if [ -z "$root" ] && (( ! dry )); then
        systemctl daemon-reload
        systemctl restart intel_lpmd.service
    fi
    exit 0
fi

[ -x "$stage/bin/intel_lpmd" ] || { echo "error: $stage/bin/intel_lpmd missing; run tools/build-intel-lpmd.sh" >&2; exit 1; }
[ -n "$root" ] || [ "$(id -u)" = 0 ] || { echo "error: run as root (sudo $0)" >&2; exit 1; }

say "backing up /etc/intel_lpmd to $state"
run mkdir -p "$state"
if [ -z "$root" ] && (( ! dry )) && [ -d "$confdir" ]; then
    tar -C "$root/etc" -cf "$state/conf.tar" intel_lpmd
fi

say "installing binary and configs"
run mkdir -p "$confdir" "$bindir" "$libexecdir" "$(dirname "$dropin")"
run install -m0755 "$stage/bin/intel_lpmd" "$bindir/intel_lpmd"
run install -m0755 "$stage/bin/intel_lpmd_control" "$bindir/intel_lpmd_control"
[ -x "$stage/libexec/intel_lpmd_focus_helper" ] && run install -m0755 "$stage/libexec/intel_lpmd_focus_helper" "$libexecdir/intel_lpmd_focus_helper"
for f in "$stage"/data/*.xml; do
    case $(basename "$f") in
        intel_lpmd_config*.xml|process_cpuset*.xml) run install -m0644 "$f" "$confdir/" ;;
    esac
done

say "enabling UseProcessCPUSet"
for f in "$confdir"/intel_lpmd_config.xml "$confdir"/intel_lpmd_config_F6_M204.xml; do
    [ -f "$f" ] || continue
    if (( dry )); then printf '   [dry-run] set UseProcessCPUSet=1 in %s\n' "$f"; continue; fi
    python3 - "$f" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
if '<UseProcessCPUSet>' in s:
    s = re.sub(r'<UseProcessCPUSet>\s*[01]\s*</UseProcessCPUSet>',
               '<UseProcessCPUSet>1</UseProcessCPUSet>', s, count=1)
else:
    s = s.replace('</Configuration>',
                  '\t<UseProcessCPUSet>1</UseProcessCPUSet>\n</Configuration>', 1)
open(p, 'w').write(s)
PY
done

say "seeding game classifications"
if (( ! dry )); then
    python3 - "$confdir/process_cpuset_user.xml" "$games" <<'PY'
import sys
p, games = sys.argv[1], sys.argv[2]
try:
    s = open(p).read()
except FileNotFoundError:
    s = '<?xml version="1.0"?>\n<ProcessCpusetConfig>\n</ProcessCpusetConfig>\n'
entries = []
for item in games.split():
    name, _, cls = item.partition('=')
    if not cls:
        continue
    if f'<Name>{name}</Name>' in s:
        continue
    entries.append(f'''    <Process>
        <Name>{name}</Name>
        <AllowSession>1</AllowSession>
        <AffinityAllThreads>1</AffinityAllThreads>
        <Classification>{cls}</Classification>
    </Process>
''')
if entries:
    s = s.replace('</ProcessCpusetConfig>', ''.join(entries) + '</ProcessCpusetConfig>', 1)
    open(p, 'w').write(s)
    print(f'   added {len(entries)} game entries')
else:
    print('   game entries already present')
PY
fi

say "installing systemd drop-in"
if (( ! dry )); then
    cat > "$dropin" <<CONF
[Service]
ExecStart=
ExecStart=/usr/local/bin/intel_lpmd --systemd --dbus-enable
CONF
fi
if [ -z "$root" ] && (( ! dry )); then
    systemctl daemon-reload
    systemctl restart intel_lpmd.service
    sleep 1
    say "status"
    systemctl is-active intel_lpmd.service || true
    "$bindir/intel_lpmd" --version || true
fi
say "done"
