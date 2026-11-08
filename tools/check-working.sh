#!/bin/sh
# Verify the installed system still matches the recorded working build of 2026-11-08.
fail=0
ok() { printf 'OK       %s\n' "$1"; }
bad() { printf 'MISMATCH %s\n' "$1"; fail=1; }
check() { # <label> <expected-prefix> <file>
    if [ -e "$3" ] && [ "$(sha256sum "$3" | cut -c1-16)" = "$2" ]; then ok "$1"; else bad "$1"; fi
}
RT=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable
check "host driver ec418f04" ec418f04e589965a /usr/lib64/libvulkan_intel.so
check "runtime driver 4d043cc2" 4d043cc2851a11fd "$RT/files/lib64/libvulkan_intel.so"
OV=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/overrides
[ -e "$OV/lib64/libexpat.so.1" ] && bad "runtime override ships libexpat.so.1" || ok "no expat override in runtime"
[ -e "$OV/lib64/libvulkan_intel.so" ] && ok "runtime override present" || bad "runtime override missing"
[ "$(($(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null)/1000000))" = 35 ] \
    && ok "PL1 35 W" || bad "PL1 is not 35 W"
case "$(modinfo -F filename xe 2>/dev/null)" in */updates/*) ok "patched xe module layered";; *) bad "stock xe module (patched modules missing)";; esac
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
