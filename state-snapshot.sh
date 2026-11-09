#!/bin/bash
# Dump the environment that affects game performance, so a quoted benchmark can be reproduced.
# Also reports the state of every hardware fix (units, udev rules, firmware, power/battery, RGB),
# so a "works on my machine" report carries the exact install state with it.
repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
echo "== time: $(date -Is)"
echo "kernel: $(uname -r)"; modinfo -F filename xe
echo "profile: $(cat /sys/firmware/acpi/platform_profile)  temp: $(( $(cat /sys/class/thermal/thermal_zone5/temp)/1000 ))C"
echo "PL1: $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0)/1000000 ))W"
echo "PL2/PL4: MSR $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null || echo 0)/1000000 ))/$(( $(cat /sys/class/powercap/intel-rapl:0/constraint_2_power_limit_uw 2>/dev/null || echo 0)/1000000 ))W" \
     "MMIO $(( $(cat /sys/class/powercap/intel-rapl-mmio:0/constraint_1_power_limit_uw 2>/dev/null || echo 0)/1000000 ))/$(( $(cat /sys/class/powercap/intel-rapl-mmio:0/constraint_2_power_limit_uw 2>/dev/null || echo 0)/1000000 ))W"
echo "gpu freq: cur=$(cat /sys/class/drm/card1/device/tile0/gt0/freq0/cur_freq) max=$(cat /sys/class/drm/card1/device/tile0/gt0/freq0/max_freq)"
echo "== host driver:"; sha256sum /usr/lib64/libvulkan_intel.so 2>/dev/null; strings -a /usr/lib64/libvulkan_intel.so | grep -m1 -oE "Mesa [0-9][^ ]*"
echo "== flatpak runtime driver:"; sha256sum /run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files/lib64/libvulkan_intel.so 2>/dev/null
echo "== listed drivers:"; for d in ~/git/linux_scratch/mesa-build/install-*/usr/local/lib64/libvulkan_intel.so ~/.cache/oxp3-fixes/*/usr/local/lib64/libvulkan_intel.so; do [ -e "$d" ] && sha256sum "$d"; done 2>/dev/null
echo "== sched_ext: state=$(cat /sys/kernel/sched_ext/state 2>/dev/null) running=$(pgrep -a -x scx_cosmos 2>/dev/null | head -1 | cut -d" " -f2-)"
echo "== packages:"; rpm -q gamescope mangohud 2>/dev/null
echo "== steam flatpak overrides:"; cat ~/.local/share/flatpak/overrides/org.anatase.Steam 2>/dev/null
echo "== hardware fixes (docs/INSTALL-HW.md):"
if [ -x "$repo/tools/oxp3-hw-install.sh" ]; then
    "$repo/tools/oxp3-hw-install.sh" --status 2>/dev/null | tail -n +2 | sed 's/^/  /'
else
    echo "  tools/oxp3-hw-install.sh missing"
fi
echo "  firmware in updates/: $(ls /usr/lib/firmware/updates/intel/ibt-00a0-0291* 2>/dev/null | wc -l) file(s)"
for f in /usr/lib/firmware/updates/intel/ibt-00a0-0291-iml.sfi /usr/lib/firmware/updates/intel/ibt-00a0-0291-pci.sfi; do
    [ -e "$f" ] && echo "    $(sha256sum "$f" | cut -c1-16)  ${f##*/}"
done
echo "  battery: limit=$(cat /sys/class/power_supply/BAT0/charge_control_end_threshold 2>/dev/null || echo n/a)%" \
     "behaviour=$(cat /sys/class/power_supply/BAT0/charge_behaviour 2>/dev/null || echo n/a)"
echo "  suspend: $(cat /sys/power/mem_sleep 2>/dev/null)  hci0=$([ -e /sys/class/bluetooth/hci0 ] && echo yes || echo no)" \
     "rgbd-config=$([ -e "$HOME/.config/oxp3-rgb.toml" ] && echo yes || echo no)"
