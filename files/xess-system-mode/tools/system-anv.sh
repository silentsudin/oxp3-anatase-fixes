#!/bin/bash
# system-anv.sh - "system mode" support for upstream's xess-xmx-linux package.
#
# Upstream's install.sh assumes the package ships its own ANV driver: it requires
# $PKG/lib/libvulkan_intel.so, writes an ICD wrapper, and puts that driver into VK_DRIVER_FILES for the
# whole session. On this system the host's Mesa is built with the CM kernel injection
# (patches/mesa-26.3, so /usr/lib64/libvulkan_intel.so answers ANV_CM_HELPER), and having two Intel ANV
# drivers in one session is exactly what the oxp3-anatase-fixes consolidation removed.
#
# Sourced by install.sh (patched by apply.py): xmx_system_anv() reports the host driver when it carries
# the CM injection, and install.sh then sets only the compiler/helper variables instead of a driver.
xmx_system_anv() {
    local l
    for l in /usr/lib64/libvulkan_intel.so /usr/lib/x86_64-linux-gnu/libvulkan_intel.so \
             /usr/lib/libvulkan_intel.so; do
        if [ -f "$l" ] && grep -aq ANV_CM_HELPER "$l"; then
            printf '%s\n' "$l"
            return 0
        fi
    done
    return 1
}
