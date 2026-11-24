#!/bin/bash
# SPDX-License-Identifier: MIT
# Restore OneXPlayer 3 support on Anatase after an update:
#   - gamescope: PQ HDR on the SDC AMS881KB01 OLED (no black screens, correct peak brightness)
#   - Mesa:      26.3 branch (Jay shader compiler on Xe3, ANV fixes) from Anatase's packaging
#   - XeSS XMX:  xess-xmx-linux (our fork): XeSS SR/FG on the XMX units in DX12 games under Proton
#   - MangoHud:  CPU power, GPU power and VRAM in the Gamemode overlay on Intel iGPUs,
#                with power from the rapl-power service (counters stay root-only)
#   - kernel:    MOK-signed xe, iwlwifi and btintel modules for the installed Anatase kernel:
#                VBT VS/PE-O backport (white dots, HDR retoggle black screen), VRR/DSB timing,
#                resume out-of-memory fix, 7.2.13 stable and 7.3/7.4 Wi-Fi/Bluetooth fixes
#   - firmware:  display DMC v2.20 (VRR cap with v2.36), Bluetooth 24.80, CPU microcode 0x11c
#   - extras:    MSR PL1 kept at hhd's TDP, speaker route check after boot/resume and speaker
#                name, volume rocker taps (hhd patch + atkbd force-release), no 5 s xpad wait on
#                suspend, GPU power zone check at boot, thermald off (no tables in this BIOS)
#
#   ./oxp3-fixes.sh build       build everything for the installed Anatase (as your user)
#   ./oxp3-fixes.sh install     install (asks for sudo). Twice after a kernel change: the first
#                               run stages the kernel modules + firmware (reboot), the second puts
#                               gamescope and MangoHud into the writable /usr overlay
#   ./oxp3-fixes.sh all         build + install
#   ./oxp3-fixes.sh status      what is installed / layered / overridden
#   ./oxp3-fixes.sh uninstall   back to stock Anatase (asks for sudo), then reboot
#   ./oxp3-fixes.sh mok-setup   create a module signing key and queue it for enrolment (once)
#
# Options (after the command): --skip-kernel --skip-gamescope --skip-mangohud --skip-firmware
#                              --skip-mesa --skip-xess --skip-extras (TDP sync, speakers, volume rocker,
#                              suspend)
#                              --stock-dmc (keep Anatase's display DMC firmware)
#                              --skip-hw (leave the hardware fixes - home button/LEDs, power units -
#                                         from tools/oxp3-hw-install.sh alone)
#                              --skip-flatpak-gl (Flathub apps keep Flathub's Mesa instead of the
#                                                 org.freedesktop.Platform.GL.oxp3 extension)
# Environment: OXP3_MOK_DIR (default ~/.local/share/oxp3-fixes/mok, needs MOK.key + MOK.der)
#              OXP3_CACHE   (default ~/.cache/oxp3-fixes)
#              OXP3_FLATPAK_GL_BRANCHES  Flatpak runtime branches to build our Mesa for
#                                        (default 26.08; e.g. "26.08 25.08")
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
if [[ ${EUID:-$(id -u)} -eq 0 && -n ${SUDO_USER:-} ]]; then
    _human_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    [[ -n $_human_home ]] || _human_home=$HOME
else
    _human_home=$HOME
fi
CACHE="${OXP3_CACHE:-$_human_home/.cache/oxp3-fixes}"
MOK_DIR="${OXP3_MOK_DIR:-$_human_home/.local/share/oxp3-fixes/mok}"
[[ ${EUID:-$(id -u)} -eq 0 && -n ${SUDO_USER:-} ]] && export HOME=$_human_home
OXP3_XMX_DRIVER_HOOK=build_xmx_driver
OUT="$CACHE/out"
ANATASE_REPO=https://github.com/anatase-org/anatase.git
PATCHWORK_REPO=https://github.com/anatase-org/patchwork.git
FEDORA="$(rpm -E %fedora)"
IMAGE="localhost/oxp3-fixes-build:f$FEDORA"
CTR="oxp3-fixes-build-f$FEDORA"
KMOD_NAME="kmod-oxp3"
STAMP=$(date +%Y%m%d%H%M)

SKIP_KERNEL=0 SKIP_GAMESCOPE=0 SKIP_MANGOHUD=0 SKIP_FIRMWARE=0 SKIP_DMC=0 SKIP_EXTRAS=0 SKIP_MESA=0 SKIP_XESS=0 SKIP_HW=0 SKIP_FLATPAK_GL=0
[[ ${OXP3_SKIP_HW:-0} == 1 ]] && SKIP_HW=1
cmd="${1:-}"; shift || true
for a in "$@"; do
    case $a in
        --skip-kernel) SKIP_KERNEL=1 ;;
        --skip-gamescope) SKIP_GAMESCOPE=1 ;;
        --skip-mangohud) SKIP_MANGOHUD=1 ;;
        --skip-firmware) SKIP_FIRMWARE=1 ;;
        --stock-dmc) SKIP_DMC=1 ;;
        --skip-hw) SKIP_HW=1 ;;
        --skip-extras) SKIP_EXTRAS=1 ;;
        --skip-mesa) SKIP_MESA=1 ;;
        --skip-flatpak-gl) SKIP_FLATPAK_GL=1 ;;
        --skip-xess) SKIP_XESS=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------------------------
# shared build/install helpers
# ---------------------------------------------------------------------------------------------
# Everything that was byte-identical in this installer and oxp3-fixes-fast.sh lives in
# tools/lib/install-common.sh (contract and function list: docs/MAINTAINABILITY.md). This file
# keeps its own configuration, its own Mesa pipeline and its own install/uninstall flow; those
# are the parts that genuinely differ between the RPM and the podman/Mesa variant.
OXP3_INSTALL_COMMON_API=1
install_common_lib="$here/tools/lib/install-common.sh"
[[ -r $install_common_lib ]] ||
    { printf 'error: %s is missing (incomplete checkout?)\n' "$install_common_lib" >&2; exit 1; }
# shellcheck source=tools/lib/install-common.sh
source "$install_common_lib"

# ---------------------------------------------------------------------------------------------
# Mesa 26.3 (Jay shader compiler on Xe2/Xe3, compression/tiling work) from Anatase's packaging:
# the 26.3 branch at MESA_COMMIT plus patches/mesa, x86_64 + i686, all installed subpackages
# ---------------------------------------------------------------------------------------------
# Kept local: build_mesa()/mesa_rpms() are the RPM pipeline that makes this script different
# from oxp3-fixes-fast.sh (podman/meson), so they are not part of tools/lib/install-common.sh.
MESA_COMMIT=fec8830e9c94b1d67f3d8469467d5529fb2978cd     # Mesa tag mesa-26.3.1, 2026-11-18
MESA_VERSION="26.3.1"                                     # the stable branch: devel is retired
MESA_URL=https://gitlab.freedesktop.org/mesa/mesa/-/archive/$MESA_COMMIT/mesa-$MESA_COMMIT.tar.gz
# i686 -devel packages the 32-bit build needs beyond the container's (dnf builddep is x86_64 only)
MESA_I686_DEVEL="llvm-devel.i686 clang-devel.i686 spirv-llvm-translator-devel.i686 spirv-tools-devel.i686
    libdisplay-info-devel.i686 elfutils-libelf-devel.i686 libva-devel.i686 lm_sensors-devel.i686
    libzstd-devel.i686 libXdamage-devel.i686 libunwind-devel.i686 valgrind-devel.i686
    libselinux-devel.i686 zlib-ng-compat-devel.i686 expat-devel.i686 libglvnd-devel.i686
    DirectX-Headers-devel.i686 libedit-devel.i686 libpciaccess-devel.i686 libxml2-devel.i686
    xz-devel.i686 ncurses-devel.i686 libuuid-devel.i686 libclc22-devel.i686"

build_mesa() {
    local top="$CACHE/rpmbuild/mesa" spec tarball="$CACHE/mesa/mesa-$MESA_COMMIT.tar.gz" key
    key=$( { echo "$MESA_VERSION $MESA_COMMIT"
             find "$CACHE/anatase/cards/drivers/mesa" -type f -print 2>/dev/null | sort | xargs -r sha256sum
             sha256sum "$here"/patches/mesa/0200*.patch "$here"/patches/mesa/0201*.patch \
                 "$here"/patches/mesa/0206*.patch "$here"/patches/mesa-26.3/*.patch \
                 "$here"/patches/mesa-26.3-backports/*.patch 2>/dev/null || true
             sha256sum "$here/container/Containerfile" 2>/dev/null || true
           } | sha256sum | cut -c1-32 )
    if cache_valid mesa "$key" && compgen -G "$OUT/mesa-*.rpm" >/dev/null; then
        echo "  mesa: build cached (${key:0:8})"; return 0
    fi
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$CACHE/mesa"
    cp -r "$CACHE/anatase/cards/drivers/mesa/." "$top/SOURCES/"
    spec="$top/SOURCES/mesa.spec"
    say "mesa $MESA_VERSION: sources"
    [[ -s $tarball ]] || curl -sfL --retry 3 -o "$tarball" "$MESA_URL" || die "download failed: $MESA_URL"
    cp "$tarball" "$top/SOURCES/"
    # Anatase's overrides.patch: its RADV Forza Horizon 5 hunk no longer applies to 26.3 and only
    # concerns AMD GPUs, so it is dropped; every other hunk is kept.
    python3 - "$top/SOURCES/overrides.patch" <<'EOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
parts = re.split(r'(?m)^(?=diff --git )', s)
keep = [x for x in parts if not x.startswith('diff --git a/src/amd/vulkan/00-radv-defaults.conf')]
if len(keep) != len(parts):
    print("  overrides.patch: dropped the RADV FH5 hunk (AMD only, doesn't apply to 26.3)")
open(p, 'w').write(''.join(keep))
EOF
    tl=$(tar -tzf "$tarball" 2>/dev/null)
    case $tl in *01-radv-performance.conf*) export OXP3_HAS_RADV_PERF=1;; *) export OXP3_HAS_RADV_PERF=0;; esac
    case $tl in *src/vulkan/layers/anti_lag/*) export OXP3_HAS_ANTILAG_LAYER=1;; *) export OXP3_HAS_ANTILAG_LAYER=0;; esac
    case $tl in *src/virtio/*) export OXP3_HAS_VIRTIO=1;; *) export OXP3_HAS_VIRTIO=0;; esac
    # main builds zink's VA-API driver (zink_drv_video.so), the 26.3 branch does not: the spec
    # must only list that file when the tree's VA target links zink.
    if tar -xzOf "$tarball" --wildcards '*/src/gallium/targets/va/meson.build' 2>/dev/null |
           grep -q driver_zink; then
        export OXP3_HAS_ZINK_VIDEO=1
    else
        export OXP3_HAS_ZINK_VIDEO=0
    fi
    python3 - "$spec" "$MESA_VERSION" "$MESA_COMMIT" <<'EOF'
import sys
p, version, commit = sys.argv[1:]
s = open(p).read()
def rep(old, new):
    global s
    if s.count(old) != 1:
        sys.exit(f"mesa.spec: expected one '{old.strip()}' - Anatase packaging changed")
    s = s.replace(old, new)
def opt(old, new, what):
    global s
    n = s.count(old)
    if n == 1:
        s = s.replace(old, new)
    elif n > 1:
        sys.exit(f"mesa.spec: '{what}' appears {n} times")
    else:
        print(f"  mesa.spec: '{what}' not present in this tree, skipped")
import re
s, n = re.subn(r'(?m)^Version:\s+\S+$', f'Version:        {version}', s); assert n == 1
s, n = re.subn(r'(?m)^Source0:\s+\S+$', f'Source0:        mesa-{commit}.tar.gz', s); assert n == 1
rep('%autosetup -n %{name}-%{ver} -p1', f'%autosetup -n mesa-{commit} -p1')
# 26.3 removed the anti-lag layer; its virtio (venus) Vulkan driver needs venus-protocol from the net
import os
if os.environ.get('OXP3_HAS_ANTILAG_LAYER', '1') != '1':
    opt('%global with_vulkan_layers_anti_lag 1\n', '', 'anti-lag global')
# always: the virtio (venus) driver needs venus-protocol 1.1.3, and Fedora packaging disables
# wrap downloads, so enabling it fails the build with "Subproject venus-protocol is buildable: NO"
opt('%global with_virtio    1\n', '', 'virtio global')
# new in 26.3, next to the RADV defaults
import os
if '01-radv-performance.conf' not in s and os.environ.get('OXP3_HAS_RADV_PERF', '0') == '1':
    rep('%{_datadir}/drirc.d/00-radv-defaults.conf\n', '%{_datadir}/drirc.d/00-radv-defaults.conf\n%{_datadir}/drirc.d/01-radv-performance.conf\n')
# panfrost/kraid builds a Rust compiler that pulls the bitflags-2-rs crate via a meson wrap,
# which Fedora packaging refuses to download; this machine has no Mali GPU, so undefine it.
s, n = re.subn(r'(?m)^%global with_panfrost\s+1$', '%undefine with_panfrost', s)
if n != 1:
    sys.exit("mesa.spec: expected one with_panfrost global")
# Keep with_vulkan_hw enabled: the spec's %files sections gate libvulkan_intel/amd/asahi and
# the Vulkan ICDs on it, so setting it to 0 builds those files but leaves them unpackaged.
# The panfrost/kraid Rust compiler comes from extra_platform_vulkan, which is emptied below
# (and with_panfrost is undefined); with_nvk is undefined for the same reason.
s, n = re.subn(r'(?m)^%global with_vulkan_hw\s+[01]$', '%global with_vulkan_hw 1', s)
s, n = re.subn(r'(?m)^%global with_nvk .*$', '%undefine with_nvk', s)
if n != 1:
    sys.exit("mesa.spec: expected one with_nvk global")

if n != 1:
    sys.exit("mesa.spec: expected one with_vulkan_hw global")
s, n = re.subn(r'(?m)^%global extra_platform_vulkan .*$', '%global extra_platform_vulkan %{nil}', s)
if n != 1:
    sys.exit("mesa.spec: expected one extra_platform_vulkan global")
# Dropping extra_platform_vulkan leaves the Vulkan drivers it would have built out of the
# package, but the spec lists their files unconditionally under the x86 arch block. Remove
# those file entries because this machine builds only swrast/amd/intel/intel_hasvk/asahi
# (plus dzn); the panfrost/powervr Rust kernels are deliberately not built.
for block in (
    "%{_libdir}/libvulkan_broadcom.so\n%{_datadir}/vulkan/icd.d/broadcom_icd.*.json\n%{_datadir}/drirc.d/00-v3dv-defaults.conf\n",
    "%{_libdir}/libvulkan_freedreno.so\n%{_datadir}/vulkan/icd.d/freedreno_icd.*.json\n%{_datadir}/drirc.d/00-turnip-defaults.conf\n",
    "%{_libdir}/libvulkan_panfrost.so\n%{_datadir}/vulkan/icd.d/panfrost_icd.*.json\n%{_datadir}/drirc.d/00-panvk-defaults.conf\n",
    "%{_libdir}/libvulkan_powervr_mesa.so\n%{_datadir}/vulkan/icd.d/powervr_mesa_icd.*.json\n%{_datadir}/drirc.d/00-pvr-defaults.conf\n",
):
    s, n = re.subn(re.escape(block), "", s)
    if n != 1:
        sys.exit("mesa.spec: expected one dropped-driver file block")
# On main, with_vulkan_hw also builds zink's VA-API driver and the spec only lists
# zink_dri.so; 26.3's VA target does not link zink, so the entry must not be added there.
if os.environ.get('OXP3_HAS_ZINK_VIDEO', '0') == '1':
    rep("%{_libdir}/dri/zink_dri.so\n%{_datadir}/drirc.d/00-zink-defaults.conf\n",
        "%{_libdir}/dri/zink_dri.so\n%{_libdir}/dri/zink_drv_video.so\n%{_datadir}/drirc.d/00-zink-defaults.conf\n")
# i686: no rusticl/NVK (no i686 Rust std), and meson must use the 32-bit llvm-config
# (Fedora installs it as /usr/lib/llvmNN/bin/llvm-config)
rep('%global vulkan_drivers ', '%ifarch %{ix86}\n%undefine with_opencl\n%undefine with_nvk\n%endif\n\n%global vulkan_drivers ')
rep('%meson \\\n', "%ifarch %{ix86}\nprintf \"[binaries]\\nllvm-config = '%%s'\\n\" \"$(ls /usr/lib/llvm[0-9]*/bin/llvm-config | sort -V | tail -1)\" > oxp3-llvm32.ini\n%global oxp3_native --native-file oxp3-llvm32.ini\n%endif\n%meson %{?oxp3_native} \\\n")
open(p, 'w').write(s)
EOF
    # our patches after Anatase's own (overrides.patch), in order; refuses a spec they can't go into
    # One series, validated against the real tree before a 30-minute build: the 26.3
    # backports, the perf patches that apply to 26.3, and the CM (XMX) kernel-injection
    # series, so this single driver is also the XMX driver and no second ICD is needed.
    local f names=() chk
    chk=$(mktemp -d) || die "mesa: cannot create a check tree"
    tar xzf "$tarball" -C "$chk" --strip-components=1
    git -C "$chk" init -q 2>/dev/null || true
    while IFS= read -r f; do
        git -C "$chk" apply --check -p1 "$f" 2>/dev/null ||
            { rm -rf "$chk"; die "mesa: $(basename "$f") does not apply to $MESA_VERSION"; }
        git -C "$chk" apply -p1 "$f"
        cp "$f" "$top/SOURCES/"; names+=( "$(basename "$f")" )
    done < <(mesa_patch_list "$chk")
    rm -rf "$chk"
    spec_add_patches "$spec" "${names[@]}" || die "mesa: could not add the 26.3 series to mesa.spec"
    ctr spectool -g -C "$top/SOURCES" "$spec" >/dev/null
    ctr_root dnf -y -q builddep "$spec" >/dev/null 2>&1 || ctr_root dnf -y builddep --skip-unavailable "$spec" | tail -3
    # the Rust crates are dynamic build requirements (%generate_buildrequires)
    rm -rf "$top/SRPMS"
    ctr rpmbuild --define "_topdir $top" -br "$spec" >/dev/null 2>&1 || true
    local br; br=$(find "$top/SRPMS" -name '*.buildreqs.nosrc.rpm' 2>/dev/null | head -1)
    if [[ -n $br ]]; then ctr_root dnf -y -q builddep "$br" >/dev/null; fi
    # shellcheck disable=SC2086
    ctr_root dnf -y -q install --skip-unavailable $MESA_I686_DEVEL >/dev/null 2>&1 || true

    say "mesa $MESA_VERSION: x86_64 (20-30 min)"
    progress_run "mesa $MESA_VERSION: x86_64 build" "$top/build.log" \
        ctr rpmbuild --define "_topdir $top" --define "dist .fc$FEDORA.oxp3.$STAMP" -bb "$spec" ||
        { tail -30 "$top/build.log"; die "mesa x86_64 build failed (full log: $top/build.log)"; }
    say "mesa $MESA_VERSION: i686 (20-30 min)"
    progress_run "mesa $MESA_VERSION: i686 build" "$top/build-i686.log" \
        ctr bash -c "export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig; setarch i686 rpmbuild --target i686 --nodeps --define '_topdir $top' --define 'dist .fc$FEDORA.oxp3.$STAMP' -bb '$spec'" ||
        { tail -30 "$top/build-i686.log"; die "mesa i686 build failed (full log: $top/build-i686.log)"; }
    rm -f "$OUT"/mesa-*.rpm
    find "$top/RPMS" -name 'mesa-*.rpm' ! -name '*debug*' ! -name '*-devel-*' -exec cp {} "$OUT/" \;
    local fit; fit=$(mesa_rpms)
    # shellcheck disable=SC2086
    if ! rpms_fit_host $fit; then
        rm -f "$OUT"/mesa-*.rpm
        die "mesa: the built RPMs would not install on this Anatase image (see rpm's message above), so they were discarded"
    fi
    cache_store mesa "$key"
    ls "$OUT"/mesa-*.rpm
}

# the built mesa RPMs for the mesa packages installed on this system (they must all change together)
# first existing file among shell globs; no `head`, so pipefail cannot trip on SIGPIPE
first_glob() {
    local g f
    for g in "$@"; do
        for f in $g; do
            [[ -f $f ]] && { printf '%s\n' "$f"; return 0; }
        done
    done
    return 1
}

mesa_rpms() {
    local pkg arch f
    while read -r pkg arch; do
        # the fresh build first, then the durable copy install keeps for the boot restore: a
        # cleaned ~/.cache must not leave install unable to reinstall the stack it already has
        f=$(first_glob "$OUT/$pkg-$MESA_VERSION-"*".$arch.rpm" \
                        "$MESA_STATE_DIR/rpms/$pkg-$MESA_VERSION-"*".$arch.rpm" || true)
        [[ -n $f ]] || die "no built $pkg.$arch - run '$0 build' first"
        echo "$f"
    done < <(mesa_installed_pkgs)
}

# <component>: the freshly built RPMs, or the durable copies recorded by the last install.
component_rpms() {
    local comp=$1 glob f
    # no `head` here: closing the pipe early raises SIGPIPE, and this script runs under
    # `set -euo pipefail`, so the command substitution would take the whole install down
    # silently (which is exactly how a plain install appeared to do nothing).
    for glob in "$OUT/$comp"-*.rpm "$MESA_STATE_DIR/rpms/$comp"-*.rpm; do
        for f in $glob; do
            [[ -f $f ]] && printf '%s\n' "$f"
        done
    done | awk '!seen[$0]++' || true
}

# The /usr overlay ("usroverlay") does not survive a reboot, and the image's Mesa comes back
# with it. So `install` records what to restore - the version and, for the Flatpak runtime
# overrides, the file list - and files/oxp3-mesa-keep.sh reads both at boot and extracts the
# cached RPMs with cpio, the same model as the gamescope and MangoHud keep units.
# `rpm-ostree override replace` is not an option: Mesa is a multilib package set and rpm-ostree
# resolves an override by package name alone, so it stops with "Multiple packages match".
MESA_STATE_DIR=/var/lib/oxp3-fixes

# Deploy a component's boot-time restore unit (idempotent). Mesa's own deployment runs it
# immediately as well; gamescope/mangohud are extracted by install itself, so they only need the
# unit enabled for the next boot's cleared overlay.
install_overlay_keep() { # <component>
    local n=$1 svc="oxp3-$1-keep"
    [[ -r $here/files/$svc.sh && -r $here/files/$svc.service ]] || return 0
    sudo install -D -m0755 "$here/files/$svc.sh" "/usr/local/libexec/$svc"
    sudo install -D -m0644 "$here/files/$svc.service" "/etc/systemd/system/$svc.service"
    sudo systemctl daemon-reload
    sudo systemctl enable "$svc.service" >/dev/null 2>&1 || true
    echo "  $svc.service: enabled, restores the patched $n from the durable RPMs on every boot"
}

install_mesa_keep() {
    (( SKIP_MESA )) && return 0
    sudo install -D -m0755 "$here/files/oxp3-mesa-keep.sh" /usr/local/libexec/oxp3-mesa-keep
    sudo install -D -m0644 "$here/files/oxp3-mesa-keep.service" \
        /etc/systemd/system/oxp3-mesa-keep.service
    sudo systemctl daemon-reload
    sudo systemctl enable oxp3-mesa-keep.service >/dev/null 2>&1 || true
    # Run it now as well: the unit is idempotent and this is what makes the tree consistent
    # (gallium/GL/DRI from the same build as the Vulkan driver) before the next boot, instead
    # of relying on the next boot to do it.
    sudo systemctl start oxp3-mesa-keep.service >/dev/null 2>&1 || true
    echo "  oxp3-mesa-keep.service: enabled and applied, restores the full Mesa $MESA_VERSION"
    echo "  tree on every boot (host + Flatpak runtime overrides)"
}

record_rpm_state() {
    # Gamescope and MangoHud are restored into the /usr overlay at boot from their RPMs. Keeping
    # only a copy in ~/.cache means a cleaned cache leaves the stock package in place, which is how
    # the patched MangoHud (Intel iGPU power/usage) quietly stopped being used. Keep one durable
    # copy of each component's RPMs next to the recorded state.
    #
    # The kernel module's package name carries the kernel version (KMOD_NAME), so it is not
    # "kmod-oxp3-xe": globbing the wrong name matches nothing, the loop ends non-zero, and because
    # this is a plain call under `set -e` the whole install stopped silently right here. The
    # explicit return 0 makes a missing component a no-op instead of a failure.
    local comp pat rpm
    sudo mkdir -p "$MESA_STATE_DIR/rpms" || return 0
    for comp in gamescope mangohud kmod; do
        case $comp in
            gamescope) if (( SKIP_GAMESCOPE )); then continue; fi; pat=gamescope ;;
            mangohud)  if (( SKIP_MANGOHUD ));  then continue; fi; pat=mangohud ;;
            kmod)      if (( SKIP_KERNEL ));    then continue; fi; pat=$KMOD_NAME ;;
        esac
        sudo rm -f "$MESA_STATE_DIR"/rpms/"$pat"-*.rpm
        for rpm in "$OUT"/"$pat"-*.rpm; do
            [[ -f $rpm ]] && sudo cp -f "$rpm" "$MESA_STATE_DIR/rpms/"
        done
    done
    return 0
}

record_mesa_state() { # <flatpak list file, may be empty>
    local list=${1:-} rpm
    (( SKIP_MESA )) && return 0
    sudo mkdir -p "$MESA_STATE_DIR/rpms"
    # The boot restore needs the RPMs themselves, and ~/.cache can be cleaned: keep a copy of
    # this build next to the recorded version (old versions are dropped).
    sudo rm -f "$MESA_STATE_DIR"/rpms/mesa-*.rpm
    for rpm in "$OUT"/mesa-*"$MESA_VERSION"*.rpm; do
        [[ -f $rpm ]] && sudo cp -f "$rpm" "$MESA_STATE_DIR/rpms/"
    done
    printf '%s\n' "$MESA_VERSION" | sudo tee "$MESA_STATE_DIR/mesa.version" >/dev/null
    if [[ -n $list && -r $list ]]; then
        sudo cp -f "$list" "$MESA_STATE_DIR/mesa-flatpak.list"
    fi
    echo "  recorded Mesa $MESA_VERSION for the boot-time restore ($MESA_STATE_DIR)"
}

# Installed subpackages of the Mesa source package. Selected by SOURCERPM, not by the "mesa-"
# name prefix: mesa-libGLU(-devel) is a different project (GLU, its own source RPM) that this
# build never produces, and matching it by name made `install` die on systems that have GLU.
mesa_installed_pkgs() {
    # 2>/dev/null: the rpmdb inside the /usr overlay can lack .keyring.lock and then every
    # query prints "can't create keyring lock" twice before answering
    rpm -qa 'mesa-*' --qf '%{NAME} %{ARCH} %{SOURCERPM}\n' 2>/dev/null |
        awk '{ src = $3; sub(/-[^-]+-[^-]+\.src\.rpm$/, "", src) } src == "mesa" { print $1, $2 }' |
        grep -vE -- '-devel |-debug' | sort -u
}

# ---------------------------------------------------------------------------------------------
# XeSS on XMX (xess-xmx-linux, our fork with Anatase support): the Mesa patches are in
# patches/mesa (0101-0105). The XESS_* pins below feed build_xess/install_xess/status_xess,
# which live in tools/lib/install-common.sh.
# ---------------------------------------------------------------------------------------------
XESS_COMMIT=f06dc397375332d1b34e35b19227b86eceeddaab   # HHHHanasak1/xess-xmx-linux, release 20261120
XESS_SRC_URL=https://codeload.github.com/HHHHanasak1/xess-xmx-linux/tar.gz/$XESS_COMMIT
# the Windows DLLs from upstream's release (built there with MSVC)
XESS_ZIP_URL=https://github.com/HHHHanasak1/xess-xmx-linux/releases/download/20261120/xess-xmx-linux-20261120.zip
XESS_ZIP_SHA=2c0daa358a7b5d9a55aff35a3e65cbd6a6c32ebe59bd3e2d0021b020598cac51
XESS_DLLS="igdext64.dll|ce289bc46f737c25540ff553114c8f99f8335e82cc79a9521a0d02fcf2084e5d ControlLib.dll|80faffd2a0e5e6fc8da74147068026713c1491a9a1bdba977cc40e980ec8e50e"
# under home: the Steam Flatpak and the game's container both see it
XESS_PKG=${OXP3_XESS_DIR:-$HOME/.local/share/xess-xmx}

# Build the XMX driver (Mesa 26.3.0 final + vendored CM + our patches) and write the
# per-game ICD/environment; the fast variant has the same hook.
build_xmx_driver() {
    # The consolidated Mesa carries the CM kernel injection itself (patches/mesa 0200/0201/0206
    # plus patches/mesa-26.3), so XeSS XMX runs on the shipping driver and a second CM-enabled
    # Mesa tree is not needed. It used to be built here and then immediately retired by
    # xmx_retire_split_driver in the same run - and worse, building it reinstates exactly what the
    # consolidation removed: two Mesa trees, with an ICD and VK_DRIVER_FILES pointing games at the
    # older one. Only build when the host driver cannot act as the XMX driver.
    if grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so 2>/dev/null; then
        echo "  XMX driver: the shipping Mesa carries the CM injection - no second driver needed"
        return 0
    fi
    local script=$here/tools/build-xmx-driver-263.sh drv key
    [[ -x $script ]] || { echo "  no $script; XMX driver not built"; return 0; }
    drv="$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so"
    key=$( { sha256sum "$script" 2>/dev/null || true
             find "$here/patches/mesa-26.3" "$here/patches/mesa-26.3-backports" -type f -print 2>/dev/null | sort | xargs -r sha256sum
             sha256sum "$here"/patches/mesa/0200*.patch "$here"/patches/mesa/0201*.patch "$here"/patches/mesa/0206*.patch 2>/dev/null || true
           } | sha256sum | cut -c1-32 )
    if cache_valid xmx-driver "$key" && [[ -f $drv ]]; then
        echo "  XMX driver: build cached (${key:0:8})"
    else
        say "XMX driver: Mesa 26.3.0 final + CM + our patches (podman)"
        "$script" --install || { echo "  WARNING: XMX driver build failed; per-game XMX stays unavailable"; return 0; }
        cache_store xmx-driver "$key"
    fi
    mkdir -p "$HOME/.config/xess-xmx"
    printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s","api_version":"1.4.0"}}\n' \
        "$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so" > "$HOME/.config/xess-xmx/icd-xmx.json"
    "$here/tools/xmx-write-env.sh" >/dev/null 2>&1 || true
}

# Per-game XMX: do not run the package's session-wide install.sh (gamescope must stay on
# the host Mesa for PQ HDR).
install_xess() {
    [[ -d $OUT/xess-xmx ]] || die "missing $OUT/xess-xmx - run '$0 build' first"
    rm -rf "$XESS_PKG"; mkdir -p "$XESS_PKG"
    rsync -a --exclude /lib/ "$OUT/xess-xmx/" "$XESS_PKG/"
    seed_xess_compiler
    if ! ls "$XESS_PKG"/igc/neo/bin/ocloc* >/dev/null 2>&1; then
        if [[ -x $XESS_PKG/tools/get-igc.sh ]]; then
            ( cd "$XESS_PKG" && bash tools/get-igc.sh ) | sed 's/^/  /' || warn "IGC fetch failed; run tools/get-igc.sh"
        else
            warn "no tools/get-igc.sh in $XESS_PKG; IGC is required for XMX kernels"
        fi
    fi
    mkdir -p "$HOME/.config/xess-xmx"
    printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s","api_version":"1.4.0"}}\n' \
        "$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so" > "$HOME/.config/xess-xmx/icd-xmx.json"
    "$here/tools/xmx-write-env.sh" >/dev/null 2>&1 || true
    if [[ ${EUID:-$(id -u)} -eq 0 && -n ${SUDO_USER:-} ]]; then
        chown -R "$SUDO_USER": "$XESS_PKG" "$HOME/.config/xess-xmx" 2>/dev/null || true
    fi
    echo "  per-game XMX env written; no session-wide VK_DRIVER_FILES"
    echo "  run tools/xmx-steam-enable.sh --all (or --appid <id>) to put it into a game's LaunchOptions"
}

# ---------------------------------------------------------------------------------------------
# kernel: xe, iwlwifi and btintel modules with our patches, for the installed Anatase kernel
# ---------------------------------------------------------------------------------------------
# Configuration for build_kmods(); the build itself is in tools/lib/install-common.sh.
# <directory in the kernel tree>|<modules built there that we ship>. iwlwifi ships with all its
# op modes, since the patches touch its shared core; btintel's patches only change function
# bodies, so the stock btusb/hci_uart keep working with ours.
KMODS=(
    "drivers/gpu/drm/xe|xe"
    "drivers/net/wireless/intel/iwlwifi|iwlwifi dvm/iwldvm mvm/iwlmvm mld/iwlmld"
    "drivers/bluetooth|btintel btintel_pcie"
    # livepatch: relax interrupt intervals behind a TT when the PTL xHCI rejects them (Steam
    # Controller puck on a dock); usbcore/xhci are built in, hence a livepatch, loaded at boot
    "drivers/usb/oxp3|oxp3_usb_tt_quirk|autoload"
)

# ---------------------------------------------------------------------------------------------
# firmware: pinned files from upstream, packaged under /usr/lib/firmware/updates (searched first
# by the kernel and by dracut, so the initramfs gets them too)
# ---------------------------------------------------------------------------------------------
# Configuration for build_firmware(); the build itself is in tools/lib/install-common.sh.
LF=https://gitlab.com/kernel-firmware/linux-firmware/-/raw
# <installed path under /usr/lib/firmware/updates>|<url>|<sha256>|<what>
FIRMWARE=(
    # Display DMC v2.20: v2.36 (Anatase) ends a late VRR frame only at the 30 Hz maximum, which
    # caps VRR at about 74 fps (onexplayer3-steamos-setup issue #1). --stock-dmc keeps v2.36.
    "i915/xe3lpd_dmc.bin|$LF/d2e1085e36fb/i915/xe3lpd_dmc.bin|fc585fc5758fd68e1d4ba8397bfb5311ae9fa1f287b6a23b96369e5bad06584f|dmc"
    # Bluetooth BE201 24.80.0.2 (Anatase: 24.70)
    "intel/ibt-00a0-0291-pci.sfi|$LF/8a2941fae0d02364d8ae6542ba890fd496681b55/intel/ibt-00a0-0291-pci.sfi|6ea03de60bdd6231387f561fbae53b1aaedfd906437b3c2216b45df1d4f01e45|bt"
    "intel/ibt-00a0-0291-iml.sfi|$LF/8a2941fae0d02364d8ae6542ba890fd496681b55/intel/ibt-00a0-0291-iml.sfi|69beb77387a79a75ee741ff6f88f782a0fc44c8b2efcc7bcb41c413122928026|bt"
    # CPU microcode 06-cc-02 0x11c (Intel microcode-20260811; Anatase: 0x11b)
    "intel-ucode/06-cc-02|https://raw.githubusercontent.com/intel/Intel-Linux-Processor-Microcode-Data-Files/microcode-20260811/intel-ucode/06-cc-02|4e43bb4d23c3638f8c16967d61e8f6456ae0da2ddd1da82ab579a50715147bb1|ucode"
)
FW_NAME=oxp3-firmware

# ---------------------------------------------------------------------------------------------
# OneXPlayer 3 extras: services and settings in /etc and /usr/local (they survive updates)
# ---------------------------------------------------------------------------------------------
EXTRA_UNITS=(oxp3-tdp-sync.service oxp3-xpad-sleep.service oxp3-rapl-pp1.service oxp3-sleep-wakeup.service
             oxp3-sleep-pre.service oxp3-sleep-log.service oxp3-lowpower.service
             oxp3-sleep-lowpower-pre.service oxp3-sleep-lowpower-post.service)
EXTRA_STATE=/etc/oxp3-fixes

# Kept local: this variant also installs the suspend units; oxp3-fixes-fast.sh leaves them to
# tools/oxp3-hw-install.sh, so the two bodies genuinely differ.
install_extras() {
    local s u me; me=$(id -un)
    for s in oxp3-tdp-sync oxp3-xpad-sleep oxp3-audio-route oxp3-sleep-wakeup oxp3-sleep-log; do
        sudo install -D -m 0755 "$here/files/$s" "/usr/local/libexec/$s"
    done
    # the reversible s2idle low-power policy (mmc wake, ASPM, SD-host park)
    sudo install -D -m 0755 "$here/tools/oxp3_lowpower.py" /usr/local/libexec/oxp3-lowpower
    # suspend: bag-safe wake sources, and the pre/post pair that logs what woke the machine.
    # The pair lives in /etc/systemd/system (not /usr/lib/systemd/system-sleep) so it survives
    # an ostree update; both call the same multi-call script.
    sudo install -D -m 0644 "$here/files/99-oxp3-sleep-wakeup.rules" \
        /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules
    for u in "${EXTRA_UNITS[@]}" oxp3-audio-resume@.service; do
        sudo install -m 0644 "$here/files/$u" "/etc/systemd/system/$u"
    done
    sudo install -D -m 0644 "$here/files/oxp3-audio-route.service" /etc/systemd/user/oxp3-audio-route.service
    sudo install -D -m 0644 "$here/files/52-oxp3-speakers.conf" \
        /etc/wireplumber/wireplumber.conf.d/52-oxp3-speakers.conf
    # the BMI260 IMU also matches bmi160's ACPI id; bmi160 loads but never binds. The file is
    # ours by name, but whatever was there before is kept in the undo state first so the
    # uninstall can put it back instead of deleting it (docs/ROLLBACK.md).
    local bmit
    if printf 'blacklist bmi160_i2c\n' | cmp -s - /etc/modprobe.d/oxp3-bmi160.conf; then
        :
    else
        bmit=$(oxp3_backup_image /etc/modprobe.d/oxp3-bmi160.conf oxp3-fixes)
        echo 'blacklist bmi160_i2c' | sudo tee /etc/modprobe.d/oxp3-bmi160.conf >/dev/null
        oxp3_manifest_record /etc/modprobe.d/oxp3-bmi160.conf oxp3-fixes "${bmit%%$'\t'*}" auto \
            "IMU blacklist (bmi160_i2c)"
    fi
    # Panther Lake HDMI KAE: without this, the first 6/8-channel stream on the
    # DP-alt/PCON output wedges the audio pin and every later stream is silent.
    # A module option only takes effect on the next module load, i.e. reboot.
    local kaec=/etc/modprobe.d/99-oxp3-hdmi-kae.conf kaeimg
    if cmp -s "$here/files/99-oxp3-hdmi-kae.conf" "$kaec"; then
        :
    else
        kaeimg=$(oxp3_backup_image "$kaec" oxp3-fixes)
        sudo install -D -m 0644 "$here/files/99-oxp3-hdmi-kae.conf" "$kaec"
        oxp3_manifest_record "$kaec" oxp3-fixes "${kaeimg%%$'\t'*}" auto \
            "Panther Lake HDMI KAE workaround"
    fi
    sudo systemctl daemon-reload
    sudo systemctl enable oxp3-xpad-sleep.service oxp3-rapl-pp1.service "oxp3-audio-resume@$me.service" \
        oxp3-sleep-wakeup.service oxp3-sleep-pre.service oxp3-sleep-log.service \
        oxp3-lowpower.service oxp3-sleep-lowpower-pre.service oxp3-sleep-lowpower-post.service \
        >/dev/null 2>&1
    sudo systemctl enable oxp3-tdp-sync.service >/dev/null 2>&1
    sudo systemctl restart oxp3-tdp-sync.service
    sudo systemctl restart oxp3-sleep-wakeup.service
    sudo systemctl restart oxp3-lowpower.service
    sudo udevadm control --reload 2>/dev/null || true
    sudo systemctl --global enable oxp3-audio-route.service >/dev/null 2>&1
    systemctl --user daemon-reload
    # thermald exits at boot here: the BIOS has no PSVT tables for it ("THD engine init failed")
    if systemctl is-enabled -q thermald.service 2>/dev/null; then
        sudo systemctl disable --now thermald.service >/dev/null 2>&1
        sudo install -d "$EXTRA_STATE"; sudo touch "$EXTRA_STATE/thermald-disabled"
    fi
    echo "  TDP sync, speaker route check (+ resume), speaker name, xpad sleep, GPU power zone check"
    echo "  HDMI KAE workaround (takes effect after reboot)"
    echo "  suspend: only lid/power/sleep button + the RTC alarm can wake it now (tools/sleep-power-measure.sh)"
    echo "  (the speaker name shows after WirePlumber restarts: next login, or systemctl --user restart wireplumber)"
}

uninstall_extras() {
    local u me; me=$(id -un)
    # put the recorded sysfs pre-images back before the policy script is removed
    [[ -x /usr/local/libexec/oxp3-lowpower ]] && sudo /usr/local/libexec/oxp3-lowpower undo >/dev/null 2>&1 || true
    sudo systemctl disable --now "${EXTRA_UNITS[@]}" "oxp3-audio-resume@$me.service" >/dev/null 2>&1 || true
    sudo systemctl --global disable oxp3-audio-route.service >/dev/null 2>&1 || true
    for u in "${EXTRA_UNITS[@]}" oxp3-audio-resume@.service; do sudo rm -f "/etc/systemd/system/$u"; done
    local bmiconf=/etc/modprobe.d/oxp3-bmi160.conf kaec=/etc/modprobe.d/99-oxp3-hdmi-kae.conf rbt="$here/tools/oxp3-rollback.sh"
    if [[ -x $rbt ]] && "$rbt" has-record "$bmiconf" 2>/dev/null; then
        sudo "$rbt" restore-file "$bmiconf" || sudo rm -f "$bmiconf"
    else
        sudo rm -f "$bmiconf"
    fi
    if [[ -x $rbt ]] && "$rbt" has-record "$kaec" 2>/dev/null; then
        sudo "$rbt" restore-file "$kaec" || sudo rm -f "$kaec"
    else
        sudo rm -f "$kaec"
    fi
    sudo rm -f /etc/systemd/user/oxp3-audio-route.service \
        /etc/wireplumber/wireplumber.conf.d/52-oxp3-speakers.conf \
        /usr/local/libexec/oxp3-tdp-sync /usr/local/libexec/oxp3-xpad-sleep /usr/local/libexec/oxp3-audio-route \
        /usr/local/libexec/oxp3-sleep-wakeup /usr/local/libexec/oxp3-sleep-log \
        /usr/local/libexec/oxp3-lowpower \
        /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules
    # the wake-source policy is applied at boot, so a plain reboot restores the firmware defaults
    sudo udevadm control --reload 2>/dev/null || true
    if [[ -e $EXTRA_STATE/thermald-disabled ]]; then
        sudo systemctl enable thermald.service >/dev/null 2>&1 || true
        sudo rm -f "$EXTRA_STATE/thermald-disabled"
    fi
    sudo rmdir "$EXTRA_STATE" 2>/dev/null || true
    sudo systemctl daemon-reload
}

# Volume rocker (needs the /usr overlay): hhd patch and atkbd force-release go in together, since
# force-release without the patch makes hhd lose every press. Both live in /usr, so an Anatase
# update drops both and the rocker keeps working (just unfixed) until the next install.
HHD_PATCHES=("$here"/patches/hhd/*.patch)
VOLUME_HWDB=/usr/lib/udev/hwdb.d/84-oxp3-volume-keys.hwdb
# The patch list above is consumed by hhd_site/install_hhd_patches/hhd_volume_unpatch in
# tools/lib/install-common.sh.

# Mesa for the Steam Flatpak. Anatase builds the org.anatase.Platform runtime from the composefs /usr
# (anatase-flatpak-extensions skips the /usr overlay on purpose) plus its overrides directory, so
# games under the Flatpak Steam would keep Anatase's Mesa. Our Mesa files go into those overrides,
# inside the /usr overlay, so an Anatase update drops them together with the overlay Mesa. The
# overrides are copied to /run at boot: other vendors' Vulkan drivers and OpenCL stay stock to keep
# that small (each Vulkan driver is self-contained).
FLATPAK_RT=/usr/lib/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable
FLATPAK_MESA_LIST=$FLATPAK_RT/oxp3-mesa.list
FLATPAK_RT_FILES=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files
FLATPAK_MESA_SKIP='/\\.build-id/|libvulkan_(radeon|panfrost|nouveau|freedreno|asahi|powervr_mesa|broadcom|dzn|virtio|intel_hasvk|lvp)\.so|/icd\.d/(radeon|panfrost|nouveau|freedreno|asahi|powervr_mesa|broadcom|dzn|virtio|intel_hasvk|lvp)_icd|RusticlOpenCL|/OpenCL/|^/usr/share/(doc|licenses|man)/'

# Kept local: here the file list comes from the distro Mesa RPMs; the fast variant installs its
# own built driver and expat. Only remove_flatpak_mesa/refresh_flatpak_runtime are shared.
# The boot keep unit restores known-good copies; a fresh install must record them.
record_known_good() { # <host|runtime> <driver file>
    local kind=$1 file=$2 hash kg="$CACHE/known-good"
    [ -e "$file" ] || return 0
    hash=$(sha256sum "$file" | cut -c1-16)
    sudo mkdir -p "$kg"
    sudo install -m 0644 "$file" "$kg/$kind-$hash-libvulkan_intel.so"
    sudo ln -sfn "$kind-$hash-libvulkan_intel.so" "$kg/$kind-current"
    echo "  known-good $kind Mesa recorded: $kind-$hash-libvulkan_intel.so"
}

install_flatpak_mesa() {
    [[ -d $FLATPAK_RT/overrides ]] || { echo "  no Anatase Flatpak runtime overrides - Steam keeps Anatase's Mesa"; return 0; }
    local pkgs list p
    # The list comes from the RPMs this run installed, not from `rpm -q`: an install into the
    # /usr overlay can leave the rpmdb reporting the base image's Mesa while the overlay holds
    # ours, and then this step would copy the wrong tree (or miss files) in the runtime.
    # Every Mesa RPM, not just the alphabetically first one: mesa-dri-drivers alone has no
    # libvulkan_intel.so, and a list from it left the runtime overrides without the Vulkan driver
    local -a src=()
    src=( "$OUT"/mesa-*.x86_64.rpm )
    [[ -f ${src[0]} ]] || src=( "$MESA_STATE_DIR"/rpms/mesa-*.x86_64.rpm )
    list=$(rpm -qlp "${src[@]}" 2>/dev/null | grep '^/usr/' |
           grep -vE "$FLATPAK_MESA_SKIP" | sort -u)
    if [[ -z $list ]]; then
        pkgs=$(mesa_installed_pkgs | awk '{ print $1 "." $2 }')
        # shellcheck disable=SC2086
        list=$(rpm -ql $pkgs | grep '^/usr/' | grep -vE "$FLATPAK_MESA_SKIP" | sort -u)
    fi
    local -a present=()
    while IFS= read -r p; do [[ -e $p ]] && present+=( "$p" ); done <<<"$list"
    if (( ${#present[@]} == 0 )); then
        echo "  WARNING: no Mesa file is installed at the expected paths - runtime overrides left empty"
        return 0
    fi
    remove_flatpak_mesa
    # /usr/<path> -> overrides/<path> (the runtime is mounted at /usr in the sandbox). The list
    # is pre-filtered to existing paths, so one missing file cannot abort the extraction and
    # leave the runtime holding a partial tree.
    printf '%s\n' "${present[@]}" | sed 's,^/,,' |
        sudo tar -C / --no-recursion -cf - -T - 2>/dev/null |
        sudo tar -C "$FLATPAK_RT/overrides" -xf - --transform 's,^usr/,,'
    printf '%s\n' "${present[@]}" | sed 's,^/usr/,,' | sudo tee "$FLATPAK_MESA_LIST" >/dev/null
    echo "  ${#present[@]} Mesa files in the Steam Flatpak runtime overrides"
    if [[ ! -e $FLATPAK_RT/overrides/lib64/libvulkan_intel.so ]]; then
        echo "  WARNING: the runtime overrides have no libvulkan_intel.so - Flatpak Steam keeps Anatase's Mesa"
    else
        record_known_good runtime "$FLATPAK_RT/overrides/lib64/libvulkan_intel.so"
    fi
    refresh_flatpak_runtime
}

# ---------------------------------------------------------------------------------------------
# install / uninstall (root)
# ---------------------------------------------------------------------------------------------
# Kept local: the variants stage different things (Mesa RPMs here; podman-built Mesa, sched_ext
# and the Flatpak runtime in oxp3-fixes-fast.sh), so the flow stays with each caller. Every step
# it calls (MOK, RAPL, extras, XeSS, deployment state) is in tools/lib/install-common.sh.
# ---------------------------------------------------------------------------------------------

do_install() {
    local rpms=() kmod=() kver info staged locals p un=()
    if (( !SKIP_GAMESCOPE )); then local gr; gr=$(component_rpms gamescope || true); mapfile -t -O "${#rpms[@]}" rpms <<<"$gr"; fi
    if (( !SKIP_MANGOHUD )); then local mr2; mr2=$(component_rpms mangohud || true); mapfile -t -O "${#rpms[@]}" rpms <<<"$mr2"; fi
    if (( !SKIP_MESA )); then local mr; mr=$(mesa_rpms); mapfile -t -O "${#rpms[@]}" rpms <<<"$mr"; fi
    local f; for f in "${rpms[@]}"; do [[ -f $f ]] || die "missing $f - run '$0 build' first"; done
    kver=$(anatase_kver)
    info=$(deploy_info); staged=$(sed -n 1p <<<"$info"); locals=$(sed -n 2p <<<"$info")

    say "power readings service (sudo)"
    install_rapl_power
    if (( !SKIP_EXTRAS )); then
        say "OneXPlayer 3 services (sudo)"
        install_extras
    fi
    # Hardware fixes that live outside the /usr overlay (they survive updates in /etc and
    # /usr/local): home button + top LEDs (udev rule + user unit) and the power/fan units.
    # The volume rocker keeps the hhd patch path above; use tools/oxp3-hw-install.sh --volume
    # for the oxp3-voltap daemon instead (the two are alternatives, see docs/INSTALL-HW.md).
    if (( !SKIP_HW )) && [[ -x $here/tools/oxp3-hw-install.sh ]]; then
        # The user-scope parts belong to the person who started the install, not to root. This
        # script is already running under sudo, so calling `sudo oxp3-hw-install.sh` a second time
        # makes the inner sudo set SUDO_USER=root and the helper refuses ("refusing to install the
        # user-scope parts as root"). Pass the outer invoking user explicitly instead.
        local hwuser=${OXP3_USER:-${SUDO_USER:-}}
        if [[ -n $hwuser && $hwuser != root ]]; then
            say "hardware fixes: home button/LEDs + power units (sudo, user $hwuser)"
            sudo "$here/tools/oxp3-hw-install.sh" --leds --power --user "$hwuser" ||
                echo "WARNING: hardware fixes (leds/power units) failed - see above" >&2
        else
            echo "  hardware fixes (user-scope parts) skipped: no unprivileged user to wire them for"
            echo "  run ./oxp3-install.sh from your desktop session, or pass --user NAME"
        fi
    fi

    # Flathub apps: our Mesa as a Flatpak GL driver extension. It lives in /var/lib/flatpak and
    # /etc (not the /usr overlay), so it survives Anatase updates and needs no reboot.
    if (( !SKIP_MESA && !SKIP_FLATPAK_GL )); then
        say "Mesa for Flathub apps: Flatpak GL driver extension (sudo)"
        install_flatpak_gl
    fi

    # 1. kernel modules + firmware: xe loads from the initramfs, so both are layered with
    #    rpm-ostree and the initramfs regenerated. That makes a new deployment, which doesn't keep
    #    the writable /usr overlay, so this goes first; gamescope/MangoHud follow on the next run.
    local layer=() want=() pkg
    if (( !SKIP_KERNEL )) && [[ ! -e $OUT/.kernel-not-needed ]]; then
        kmod=( "$OUT/$KMOD_NAME"-*.rpm )
        [[ -f ${kmod[0]} ]] || kmod=( "$MESA_STATE_DIR/rpms/$KMOD_NAME"-*.rpm )
        if [[ ! -f ${kmod[0]} ]]; then
            # already layered in the deployment (and a kmod cannot be rebuilt for a kernel the
            # module was not built against anyway): skip rather than abort the whole install
            if grep -qwE "$KMOD_NAME|kmod-oxp3-.*" <<<"$locals"; then
                echo "  no fresh $KMOD_NAME RPM to layer - the installed one stays"
                SKIP_KERNEL=1
            else
                die "no $KMOD_NAME package - run '$0 build' first"
            fi
        fi
        if [[ -f ${kmod[0]} ]]; then
            rpm -qp --qf '%{VERSION}\n' "${kmod[0]}" | grep -qF "${kver//-/_}" ||
                die "$KMOD_NAME was built for another kernel than $kver - rebuild"
            if ! mok_trusted; then
                echo "WARNING: the MOK in $MOK_DIR is not in the kernel's machine keyring."
                echo "         The signed modules would be rejected and the screen stay dark at boot."
                mok_usage_ok || echo "         The key isn't a CA with keyCertSign only: run '$0 mok-setup'."
                echo "         Skipping the kernel part (README: Secure Boot key)."
            else
                layer+=( "${kmod[0]}" )
            fi
        fi
    fi
    if (( !SKIP_FIRMWARE )); then
        local fw=( "$OUT/$FW_NAME"-*.rpm )
        [[ -f ${fw[0]} ]] && layer+=( "${fw[0]}" )
    fi
    for pkg in "${layer[@]}"; do want+=( "$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' "$pkg")" ); done
    # our layered packages that are not wanted any more (older builds, kmod-oxp3-xe, or firmware
    # Anatase now ships itself)
    # A local package is only "unwanted" when there is a fresh RPM that replaces it. Without one -
    # a cleaned build cache, or --skip-kernel - the installed package must stay: dropping
    # oxp3-firmware because no new firmware RPM was staged would uninstall working firmware and,
    # worse, hit the staging branch below and return before gamescope/MangoHud are installed.
    local have_fw=0
    compgen -G "$OUT/$FW_NAME-*.rpm" >/dev/null && have_fw=1
    for p in $locals; do
        [[ $p == kmod-oxp3-* || $p == "$FW_NAME"-* ]] || continue
        if [[ $p == kmod-oxp3-* ]]; then
            (( SKIP_KERNEL )) && continue
            [[ -f ${kmod[0]} ]] || continue
        else
            (( SKIP_FIRMWARE || !have_fw )) && continue
        fi
        [[ " ${want[*]} " == *" $p "* ]] || un+=( "$p" )
    done
    local missing=() i
    for i in "${!layer[@]}"; do grep -qwF "${want[$i]}" <<<"$locals" || missing+=( "${layer[$i]}" ); done
    if (( ${#missing[@]} || ${#un[@]} )); then
        say "staging kernel modules / firmware for $kver (sudo)"
        printf '  layer:  %s\n' "${missing[@]##*/}"
        printf '  remove: %s\n' "${un[@]}"
        if (( ${#missing[@]} )); then
            sudo rpm-ostree install "${missing[@]}" "${un[@]/#/--uninstall=}"
        else
            sudo rpm-ostree uninstall "${un[@]}"
        fi
        rpm-ostree initramfs 2>/dev/null | grep -q 'Initramfs regeneration: enabled' ||
            sudo rpm-ostree initramfs --enable
        say "reboot now, then run '$0 install' again for gamescope and MangoHud"
        echo "(If the screen stays dark after the reboot, pick the previous entry in the boot menu.)"
        return 0
    elif (( ${#layer[@]} )); then
        echo "kernel modules / firmware for $kver already in this deployment"
    fi

    # 2. gamescope / MangoHud / hhd volume fix into the writable /usr overlay of the booted deployment
    if (( ${#rpms[@]} || !SKIP_EXTRAS || !SKIP_XESS )); then
        if [[ $staged == staged ]]; then
            echo "A deployment is staged (an update or a pending change): reboot into it first,"
            echo "then run '$0 install' again - changes to the current /usr would be lost."
            return 0
        fi
        hotfix_active || sudo rpm-ostree usroverlay --hotfix >/dev/null 2>&1 || true
        if ! sudo install -m 000 /dev/null "/usr/.oxp3-rw-probe.$$" 2>/dev/null; then
            die "the /usr overlay did not become writable (rpm-ostree usroverlay failed and /usr is read-only)"
        fi
        sudo rm -f "/usr/.oxp3-rw-probe.$$"
        if (( ${#rpms[@]} )); then
            say "gamescope / MangoHud / Mesa into the writable /usr overlay (sudo)"
            sudo rpm -Uvh --replacepkgs --oldpackage --nosignature "${rpms[@]}"
            rpm -q gamescope mangohud mesa-vulkan-drivers | sed 's/^/  /'
            (( SKIP_MESA )) || record_known_good host /usr/lib64/libvulkan_intel.so
            if (( !SKIP_MESA )); then
                say "Mesa for the Steam Flatpak (sudo)"
                install_flatpak_mesa
            fi
            (( SKIP_MESA )) || record_mesa_state "$FLATPAK_MESA_LIST"
            record_rpm_state
        fi
        install_mesa_keep
        (( SKIP_GAMESCOPE )) || install_overlay_keep gamescope
        (( SKIP_MANGOHUD )) || install_overlay_keep mangohud
        if (( !SKIP_EXTRAS )); then
            say "hhd patches: OneXPlayer 3 power limits, volume rocker (sudo)"
            install_hhd_patches
        fi
        if (( !SKIP_XESS )); then
            say "XeSS on XMX (xess-xmx-linux)"
            install_xess
        else
            # independent of the XMX package: keep one driver even when that step is skipped
            say "one Vulkan driver: retire any separate XMX ICD"
            xmx_retire_split_driver
        fi
        say "done - leave and re-enter Gamemode (no reboot needed)"
        dxvk_advice
        if (( !SKIP_MESA )); then
            echo "Mesa $MESA_VERSION is restored automatically on every boot (oxp3-mesa-keep);"
            echo "run '$0 all' after an Anatase update to rebuild it against the new image."
        else
            echo "This lasts until the next Anatase update; run '$0 all' after updating."
        fi
    fi
}

# Kept local: mirrors this variant's do_install() and restores only what this variant installs.
do_uninstall() {
    say "back to stock Anatase (sudo)"
    local p un=()
    for p in $(sed -n 2p <<<"$(deploy_info)"); do [[ $p == kmod-oxp3-* || $p == "$FW_NAME"-* ]] && un+=( "$p" ); done
    (( ${#un[@]} )) && sudo rpm-ostree uninstall "${un[@]}"
    if rpm-ostree initramfs 2>/dev/null | grep -q 'Initramfs regeneration: enabled'; then
        sudo rpm-ostree initramfs --disable
    fi
    sudo systemctl disable --now rapl-power.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/rapl-power.service /usr/local/libexec/rapl-power
    sudo systemctl daemon-reload
    remove_rapl_udev_rules
    # the boot-time Mesa restore is ours: disabling it and dropping the recorded version makes
    # the next boot the image's Mesa again, with no leftover state.
    sudo systemctl disable --now oxp3-mesa-keep.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/oxp3-mesa-keep.service /usr/local/libexec/oxp3-mesa-keep
    sudo rm -f /var/lib/oxp3-fixes/mesa.version /var/lib/oxp3-fixes/mesa-flatpak.list
    sudo systemctl daemon-reload 2>/dev/null || true
    uninstall_extras
    if [[ -x $here/tools/oxp3-hw-install.sh ]]; then
        sudo "$here/tools/oxp3-hw-install.sh" --uninstall --leds --power ||
            echo "WARNING: removing the hardware units failed - see above" >&2
    fi
    uninstall_xess
    uninstall_flatpak_gl
    if [[ -r $FLATPAK_MESA_LIST ]]; then remove_flatpak_mesa; refresh_flatpak_runtime; fi
    if [[ -e $VOLUME_HWDB ]]; then
        sudo rm -f "$VOLUME_HWDB"; sudo systemd-hwdb update
    fi
    local site; site=$(hhd_site)
    if [[ -n $site ]]; then hhd_volume_unpatch "$site"; sudo systemctl restart hhd.service; fi
    echo "Reboot to finish. The patched gamescope/MangoHud live in the writable /usr overlay of"
    echo "the current deployment; any new deployment (the reboot above, or the next Anatase"
    echo "update) starts from the stock packages again."
}

# Refuse to run if this caller does not satisfy the install-common contract (API version,
# required configuration variables and the build_mesa hook): see tools/lib/install-common.sh.
install_common_guard

case $cmd in
    build) do_build ;;
    install) do_install ;;
    all) do_build; do_install ;;
    status) do_status ;;
    uninstall) do_uninstall ;;
    mok-setup) do_mok_setup ;;
    *) sed -n '2,/^set -euo pipefail/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
