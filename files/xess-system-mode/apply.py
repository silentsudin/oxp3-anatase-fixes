#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Port "system mode" into upstream xess-xmx-linux's install.sh.

Why a script and not a patch file: upstream's install.sh is rewritten between releases (MODE selection,
the layer, the session check all moved), so a context patch would fail on every bump with no clue which
hunk mattered. Each transformation below is anchored on a literal that must exist exactly once and
fails loudly with the anchor when it does not, so a bump tells us precisely what changed.

System mode means: the host's ANV is built with the CM kernel injection (patches/mesa-26.3), so the
package's own driver, its ICD wrapper and VK_DRIVER_FILES are not used at all - one Intel driver in the
session, as this repo's Mesa consolidation requires. The kernels are compiled by the package's
tools/cm-compile.sh through ANV_CM_HELPER, and CM_MAX_GRF=128 asks for the register count upstream found
16-18% faster on Xe3 (release 20261119) inside the driver path.

Usage: apply.py <unpacked package dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

STEPS: list[tuple[str, str, str]] = []


def sub(src: str, old: str, new: str) -> str:
    n = src.count(old)
    if n != 1:
        die(f"anchor appears {n} times (expected 1):\n{old[:200]}")
    return src.replace(old, new, 1)


def die(msg: str) -> None:
    print(f"apply.py: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    if len(sys.argv) != 2:
        die("usage: apply.py <unpacked package dir>")
    pkg = Path(sys.argv[1])
    inst = pkg / "install.sh"
    s = inst.read_text()

    # 1. load the system-mode helper and resolve the host ANV
    s = sub(s, 'SHIM="$PKG/igdext64.dll"\n',
            'SHIM="$PKG/igdext64.dll"\n'
            '# system mode: the host Mesa carries the CM kernel injection (patches/mesa-26.3, Anatase)\n'
            '. "$PKG/tools/system-anv.sh"\n'
            'SYSTEM_ANV=$(xmx_system_anv || true)\n')

    # 2. the package's driver is only required when the host cannot serve as the XMX driver
    s = sub(s, 'for f in "$SHIM" "$PKG/lib/libvulkan_intel.so" "$PKG/tools/cm-compile.sh"; do',
            'for f in "$SHIM" "$PKG/tools/cm-compile.sh" "${SYSTEM_ANV:-$PKG/lib/libvulkan_intel.so}"; do')

    # 3. skip the driver selection and the ICD wrapper in system mode
    s = sub(s, 'case "${XMX_DRIVER:-}" in',
            'if [ -n "$SYSTEM_ANV" ]; then\n'
            '  # system mode: no package driver, no ICD wrapper - the host ANV is the Intel Vulkan driver\n'
            '  MODE=system\n'
            '  [ "$MANUAL" = 1 ] && echo "$MODE" > "$PKG/run/session-driver"\n'
            'else\n'
            'case "${XMX_DRIVER:-}" in')
    s = sub(s, 'grep -q "\\"library_path\\": \\"$PKG/lib/libvulkan_intel.so\\"" "$PKG/run/intel_icd.json" 2>/dev/null || \\\n'
               '  xmx_icd_json "$PKG/lib/libvulkan_intel.so" > "$PKG/run/intel_icd.json"',
            'grep -q "\\"library_path\\": \\"$PKG/lib/libvulkan_intel.so\\"" "$PKG/run/intel_icd.json" 2>/dev/null || \\\n'
            '  xmx_icd_json "$PKG/lib/libvulkan_intel.so" > "$PKG/run/intel_icd.json"\n'
            'fi')

    # 4. session environment: no VK_DRIVER_FILES, only the compiler path and the driconf/vkd3d settings
    s = sub(s, 'if [ "${XMX_NO_ENV:-0}" != 1 ]; then\n  # VK_DRIVER_FILES replaces',
            'if [ "${XMX_NO_ENV:-0}" != 1 ] && [ "$MODE" = system ]; then\n'
            '  # system mode: every Vulkan program already runs the patched host ANV; an earlier non-system\n'
            '  # install\'s VK_DRIVER_FILES / layer variables would point at a driver this package no longer ships\n'
            '  rm -f "$ENVD/50-xess-xmx.conf"\n'
            '  # CM_MAX_GRF / VK_ICD_FILENAMES: left by an earlier port or an A/B test; CM_MAX_GRF=128 makes\n'
            '  # the helper drop XeSS\'s own register-file flags\n'
            '  systemctl --user unset-environment VK_DRIVER_FILES VK_ICD_FILENAMES VK_ADD_LAYER_PATH \\\n'
            '    VK_LOADER_LAYERS_ENABLE MESA XMX_LAYER_HELPER XMX_IGC_DIR ANV_CM_KERNEL_DIR CM_MAX_GRF 2>/dev/null\n'
            '  mkdir -p "$ENVD"\n'
            '  {\n'
            '    echo "# XeSS XMX (xess-xmx-linux), system mode: the host ANV at $SYSTEM_ANV carries the CM kernel injection"\n'
            '    echo "ANV_CM_HELPER=$PKG/tools/cm-compile.sh"\n'
            '    echo "# vkd3d-proton: the classic descriptor heap (the kernel binding code needs it)"\n'
            '    echo "VKD3D_DISABLE_EXTENSIONS=$XMX_VKD3D_OFF"\n'
            '    echo "force_vk_vendor=0"\n'
            '    echo "# Protons without Intel\'s driver store never load the shim from the prefix; XeSS looks here first"\n'
            '    echo "INTC_ALT_DRIVER_EXTENSIONS_PATH=\'$XMX_ALT_EXT\'"\n'
            # No CM_MAX_GRF here. In upstream's stock mode its *layer* sets that variable because the
            # unpatched driver gives every thread 128 GRF; on the driver-injection path this repo uses,
            # the driver sizes the register file per shader, and forcing 128 made the package's helper
            # drop XeSS's own -ze-opt-large-register-file / -ze-exp-register-file-size flags, which
            # measured 10 fps slower (Trails, 2026-11-24: 75 vs 85 fps) with no visual gain.
            '  } > "$ENVD/50-xess-xmx.conf"\n'
            '  systemctl --user set-environment "ANV_CM_HELPER=$PKG/tools/cm-compile.sh" \\\n'
            '    "VKD3D_DISABLE_EXTENSIONS=$XMX_VKD3D_OFF" "force_vk_vendor=0" \\\n'
            '    "INTC_ALT_DRIVER_EXTENSIONS_PATH=$XMX_ALT_EXT" 2>/dev/null\n'
            '  echo "== environment: $ENVD/50-xess-xmx.conf (system mode, applies to Steam after a reboot / re-login)"\n'
            '  echo "   driver: the host\'s patched ANV ($SYSTEM_ANV), kernels compiled with XeSS\'s own register-file flags"\n'
            'elif [ "${XMX_NO_ENV:-0}" != 1 ]; then\n'
            '  # VK_DRIVER_FILES replaces')

    # 5. the session check verifies the package's own driver: meaningless in system mode, and its fallback
    #    would rewrite the session to the stock drivers. Skip it and remove a unit an earlier install left.
    s = sub(s, '#    stock drivers\nif [ "${XMX_NO_ENV:-0}" != 1 ]; then',
            '#    stock drivers\n'
            'if [ -n "$SYSTEM_ANV" ]; then\n'
            '  # In system mode there is no package driver to check, and the check\'s fallback PATH\n'
            '  # clears ANV_CM_HELPER / ANV_CM_KERNEL_DIR - it strips the XMX environment every session\n'
            '  # and leaves XeSS on DP4a (measured: 75 vs 85 fps in Trails). Remove it by its real name\n'
            '  # (20261120 uses xess-xmx-check.service; older packages used xess-xmx-session-check).\n'
            '  for unit in xess-xmx-check.service xess-xmx-session-check.service; do\n'
            '    systemctl --user disable --now "$unit" >/dev/null 2>&1 || true\n'
            '    rm -f "$UNITD/$unit" "$UNITD/default.target.wants/$unit" \\\n'
            '          "$UNITD/graphical-session-pre.target.wants/$unit"\n'
            '  done\n'
            '  rm -f "$CFGD/session-check.sh"\n'
            'fi\n'
            'if [ "${XMX_NO_ENV:-0}" != 1 ] && [ -z "$SYSTEM_ANV" ]; then')

    inst.write_text(s)
    print("apply.py: system mode ported into install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
