#!/bin/bash
# xmx-session-check-fix.sh - make the xess-xmx release session checker validate the
# driver this repo actually uses.
#
# The release ships ~/.local/share/xess-xmx/tools/session-check.sh and the user unit
# xess-xmx-check.service. It tests ~/.local/share/xess-xmx/lib/libvulkan_intel.so; when
# the package's lib/ directory is absent (as on this install) the check fails and the
# unit sets the *session* VK_DRIVER_FILES to the stock ICDs - XeSS then runs DP4a and the
# per-game XMX environment looks broken even though its ICD loads. A drop-in points the
# check at ~/.cache/oxp3-fixes/xmx-driver and this repo's per-game ICD instead.
set -eu
drv=${XMX_SESSION_DRIVER:-$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so}
icd=${XMX_SESSION_ICD:-$HOME/.config/xess-xmx/icd-xmx.json}
unit=xess-xmx-check.service
dir=$HOME/.config/systemd/user/$unit.d
[ -e "$drv" ] || { echo "error: no XMX driver at $drv; run tools/xmx-enable.sh first" >&2; exit 1; }
[ -e "$icd" ] || { echo "error: no per-game ICD at $icd; run tools/xmx-enable.sh first" >&2; exit 1; }
if ! systemctl --user cat $unit >/dev/null 2>&1; then
    echo "no $unit in this session; nothing to do"
    exit 0
fi
mkdir -p "$dir"
cat > "$dir/50-oxp3.conf" <<CONF
[Service]
Environment=XMX_CHECK_LIB=$drv
Environment=XMX_CHECK_ICD=$icd
CONF
systemctl --user daemon-reload
systemctl --user restart $unit 2>/dev/null || true
sleep 1
status=$(cat "${XDG_CACHE_HOME:-$HOME/.cache}/xess-xmx/driver-status" 2>/dev/null || true)
printf 'wrote %s\n' "$dir/50-oxp3.conf"
printf 'driver-status: %s\n' "${status:-<none>}"
case $status in
    ok\ *) echo "session check passes on $drv" ;;
    *) echo "session check still failing; inspect: systemctl --user status $unit" >&2; exit 1 ;;
esac
