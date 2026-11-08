#!/bin/bash
# Dump the environment that affects game performance, so a quoted benchmark can be reproduced.
echo "== time: $(date -Is)"
echo "kernel: $(uname -r)"; modinfo -F filename xe
echo "profile: $(cat /sys/firmware/acpi/platform_profile)  temp: $(( $(cat /sys/class/thermal/thermal_zone5/temp)/1000 ))C"
echo "PL1: $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw)/1000000 ))W"
echo "gpu freq: cur=$(cat /sys/class/drm/card1/device/tile0/gt0/freq0/cur_freq) max=$(cat /sys/class/drm/card1/device/tile0/gt0/freq0/max_freq)"
echo "== host driver:"; sha256sum /usr/lib64/libvulkan_intel.so 2>/dev/null; strings -a /usr/lib64/libvulkan_intel.so | grep -m1 -oE "Mesa [0-9][^ ]*"
echo "== flatpak runtime driver:"; sha256sum /run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files/lib64/libvulkan_intel.so 2>/dev/null
echo "== listed drivers:"; for d in ~/git/linux_scratch/mesa-build/install-*/usr/local/lib64/libvulkan_intel.so ~/.cache/oxp3-fixes/*/usr/local/lib64/libvulkan_intel.so; do [ -e "$d" ] && sha256sum "$d"; done 2>/dev/null
echo "== sched_ext: state=$(cat /sys/kernel/sched_ext/state 2>/dev/null) running=$(pgrep -a -x scx_cosmos 2>/dev/null | head -1 | cut -d" " -f2-)"
echo "== packages:"; rpm -q gamescope mangohud 2>/dev/null
echo "== steam flatpak overrides:"; cat ~/.local/share/flatpak/overrides/org.anatase.Steam 2>/dev/null
