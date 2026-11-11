#!/bin/sh
# SPDX-License-Identifier: MIT
# Verify the installed system still matches the recorded working build of 2026-11-09.
fail=0
ok() { printf 'OK       %s\n' "$1"; }
bad() { printf 'MISMATCH %s\n' "$1"; fail=1; }
check() { # <label> <expected-prefix> <file>
    if [ -e "$3" ] && [ "$(sha256sum "$3" | cut -c1-16)" = "$2" ]; then ok "$1"; else bad "$1"; fi
}
RT=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable
# Host/runtime may be the same build now (install_flatpak_mesa copies the host driver
# into the runtime overrides); accept the recorded runtime-only build as well.
host16=$( [ -e /usr/lib64/libvulkan_intel.so ] && sha256sum /usr/lib64/libvulkan_intel.so | cut -c1-16 )
check "host driver 5a75b6a4" 5a75b6a44d285fd1 /usr/lib64/libvulkan_intel.so
rt16=$( [ -e "$RT/files/lib64/libvulkan_intel.so" ] && sha256sum "$RT/files/lib64/libvulkan_intel.so" | cut -c1-16 )
if [ "$rt16" = "4d043cc2851a11fd" ] || { [ -n "$host16" ] && [ "$rt16" = "$host16" ]; }; then
    ok "runtime driver ${rt16}"
else
    bad "runtime driver ${rt16:-missing} (expected 4d043cc2 or the host build)"
fi
OV=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides
[ -e "$OV/lib64/libexpat.so.1" ] && bad "runtime override ships libexpat.so.1" || ok "no expat override in runtime"
[ -e "$OV/lib64/libvulkan_intel.so" ] && ok "runtime override present" || bad "runtime override missing"
pl1_of() { # <zone> -> its long_term limit in W (constraint_0 is not always long_term)
    local c
    for c in "$1"/constraint_*_name; do
        [ "$(cat "$c" 2>/dev/null)" = long_term ] && { echo "$(($(cat "${c%name}power_limit_uw" 2>/dev/null)/1000000))"; return; }
    done
}
pl1_msr=$(pl1_of /sys/class/powercap/intel-rapl:0)
pl1_mmio=$(pl1_of /sys/class/powercap/intel-rapl-mmio:0)
# HHD owns the TDP profile (the panel can change it); the invariant that matters is
# that the MSR package limit follows the MMIO limit, not a fixed number.
[ "$pl1_msr" -gt 0 ] && [ "$pl1_msr" = "$pl1_mmio" ] \
    && ok "PL1 ${pl1_msr} W (MSR = MMIO, HHD profile)" \
    || bad "PL1 MSR ${pl1_msr} W does not follow MMIO ${pl1_mmio} W"
case "$(modinfo -F filename xe 2>/dev/null)" in */updates/*) ok "patched xe module layered";; *) bad "stock xe module (patched modules missing)";; esac
# PQ HDR: the patched gamescope build and the persistent launcher override.
if [ -r /var/lib/oxp3-fixes/gamescope.sha256 ] && \
   [ "$(sha256sum /usr/bin/gamescope 2>/dev/null | cut -d' ' -f1)" = "$(cat /var/lib/oxp3-fixes/gamescope.sha256)" ]; then
    ok "patched gamescope (PQ HDR)"
else
    bad "stock gamescope (PQ HDR patch missing)"
fi
HDRCOPY=/usr/local/libexec/anatase-gamemode-hdr
HDRDROP=/etc/systemd/user/anatase-gamemode.service.d/90-oxp3-hdr.conf
if grep -q -- '--hdr-enabled' "$HDRCOPY" 2>/dev/null && grep -q "^ExecStart=$HDRCOPY$" "$HDRDROP" 2>/dev/null; then
    ok "Gamemode HDR/PQ launcher override"
else
    bad "Gamemode HDR launcher override missing"
fi
for u in oxp3-mesa-keep oxp3-gamescope-keep oxp3-mangohud-keep; do
    if systemctl is-enabled --quiet "$u" 2>/dev/null; then ok "$u enabled"; else bad "$u not enabled"; fi
done

n=$(python3 - <<'PY'
import json,glob,os
c=0
for f in glob.glob(os.path.expanduser("~/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig/*.json")):
    try: d=json.load(open(f))
    except Exception: continue
    c+=sum(1 for v in d.values() if isinstance(v,dict) and v.get("useGameMode") is True)
print(c)
PY
)
[ "$n" = 0 ] && ok "Heroic GameMode off for all games" || bad "Heroic GameMode enabled for $n game(s)"
[ $fail = 0 ] && echo "state matches the recorded working build" || echo "state differs from the recorded working build"
exit $fail

# XeSS XMX stack (optional feature: report, do not fail the build check)
drv=$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so
helper=$repo/tools/xmx-cm-compile-nodgrf.sh
if [ -e "$drv" ]; then
  printf 'xmx driver       %s\n' "$(sha256sum "$drv" | cut -d" " -f1 | cut -c1-16)"
fi
if grep -q "xmx-cm-compile-nodgrf" "$HOME/.config/xess-xmx/oxp3-env.sh" 2>/dev/null; then
  printf 'xmx kernels     128-GRF helper wired\n'
else
  printf 'xmx kernels     NOT wired (helper override missing in oxp3-env.sh)\n'
fi
