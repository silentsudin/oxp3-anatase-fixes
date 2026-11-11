#!/bin/bash
# FAST variant of oxp3-fixes.sh. Everything is the same code (kernel modules, firmware, gamescope,
# MangoHud, XeSS, extras, rapl-power); only Mesa differs: it is built with podman/meson from the
# pinned main tree + patches/mesa instead of the RPM pipeline, and installed into the /usr overlay
# (plus the Steam Flatpak runtime overrides) with a backup manifest.
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
#                              --skip-mesa --skip-xess --skip-extras --skip-sched --stock-dmc
#                              (--skip-sched leaves the gaming CPU scheduler alone; --stock-dmc keeps
#                               Anatase's display DMC firmware; --skip-hw leaves the hardware fixes
#                               (home button/LEDs, power units) from tools/oxp3-hw-install.sh alone)
# Environment: OXP3_MOK_DIR (default ~/.local/share/oxp3-fixes/mok, needs MOK.key + MOK.der)
#              OXP3_CACHE   (default ~/.cache/oxp3-fixes)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# `sudo -i`-style root shells and plain `sudo ./script` both give HOME=/root while the
# build artefacts live in the invoking user's home. Resolve the human's home first, so
# `sudo ./oxp3-fixes-fast.sh install` finds the build it just made.
if [[ ${EUID:-$(id -u)} -eq 0 && -n ${SUDO_USER:-} ]]; then
    _human_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    [[ -n $_human_home ]] || _human_home=$HOME
else
    _human_home=$HOME
fi
CACHE="${OXP3_CACHE:-$_human_home/.cache/oxp3-fixes}"
MOK_DIR="${OXP3_MOK_DIR:-$_human_home/.local/share/oxp3-fixes/mok}"
# User-scoped installs (XMX package, env files, Decky) must land in the human's home.
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

SKIP_KERNEL=0 SKIP_GAMESCOPE=0 SKIP_MANGOHUD=0 SKIP_FIRMWARE=0 SKIP_DMC=0 SKIP_EXTRAS=0 SKIP_MESA=0 SKIP_XESS=0 SKIP_FLATPAK=0 SKIP_SCHED=0 SKIP_HW=0
[[ ${OXP3_SKIP_HW:-0} == 1 ]] && SKIP_HW=1
cmd="${1:-}"; shift || true
for a in "$@"; do
    case $a in
        --skip-kernel) SKIP_KERNEL=1 ;;
        --skip-gamescope) SKIP_GAMESCOPE=1 ;;
        --skip-mangohud) SKIP_MANGOHUD=1 ;;
        --skip-firmware) SKIP_FIRMWARE=1 ;;
        --stock-dmc) SKIP_DMC=1 ;;
        --skip-extras) SKIP_EXTRAS=1 ;;
        --skip-mesa) SKIP_MESA=1 ;;
        --skip-flatpak) SKIP_FLATPAK=1 ;;
        --skip-sched) SKIP_SCHED=1 ;;
        --skip-hw) SKIP_HW=1 ;;
        --skip-xess) SKIP_XESS=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------------------------
# shared build/install helpers
# ---------------------------------------------------------------------------------------------
# Everything that was byte-identical in this installer and oxp3-fixes.sh lives in
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
# the 26.3 branch at MESA_COMMIT plus patches/mesa (built with podman/meson below).
# ---------------------------------------------------------------------------------------------
# Kept local: _mpick/build_mesa/install_mesa_fast/remove_mesa_fast are the podman/meson
# pipeline that makes this script different from oxp3-fixes.sh (RPM packaging), so they are
# not part of tools/lib/install-common.sh.
MESA_COMMIT=62b0e52f875e5cb37b38776908f1b8f85588b385     # Mesa main, 2026-11-10 15:36 UTC (bumped from ea40598a)
MESA_VERSION="27.0.0~devel.20261110git${MESA_COMMIT:0:8}"  # no "^": Cargo.toml needs semver
MESA_URL=https://gitlab.freedesktop.org/mesa/mesa/-/archive/$MESA_COMMIT/mesa-$MESA_COMMIT.tar.gz
# i686 -devel packages the 32-bit build needs beyond the container's (dnf builddep is x86_64 only)
MESA_I686_DEVEL="llvm-devel.i686 clang-devel.i686 spirv-llvm-translator-devel.i686 spirv-tools-devel.i686
    libdisplay-info-devel.i686 elfutils-libelf-devel.i686 libva-devel.i686 lm_sensors-devel.i686
    libzstd-devel.i686 libXdamage-devel.i686 libunwind-devel.i686 valgrind-devel.i686
    libselinux-devel.i686 zlib-ng-compat-devel.i686 expat-devel.i686 libglvnd-devel.i686
    DirectX-Headers-devel.i686 libedit-devel.i686 libpciaccess-devel.i686 libxml2-devel.i686
    xz-devel.i686 ncurses-devel.i686 libuuid-devel.i686 libclc22-devel.i686"

MESA_BUILD_DIR="${OXP3_MESA_BUILD:-}"
MESA_BACKUP=/usr/local/share/oxp3-fixes-fast

_mpick() {
    local built="$CACHE/mesa-fast/usr/local" legacy="$HOME/git/linux_scratch/mesa-build/install-shiprt/usr/local"
    if [[ -n $MESA_BUILD_DIR && -x $MESA_BUILD_DIR/lib64/libvulkan_intel.so ]]; then return 0; fi
    if [[ -x $built/lib64/libvulkan_intel.so ]]; then MESA_BUILD_DIR=$built
    elif [[ -x $legacy/lib64/libvulkan_intel.so ]]; then MESA_BUILD_DIR=$legacy
    fi
}

# Build the XMX driver (Mesa 26.3.0 final + release CM + our patches) and write the
# per-game ICD/environment. Runs during `build` as the invoking user.
build_xmx_driver() {
    local script=$here/tools/build-xmx-driver-263.sh
    [[ -x $script ]] || { echo "  no $script; XMX driver not built"; return 0; }
    say "XMX driver: Mesa 26.3.0 final + CM + our patches (podman)"
    "$script" --install || { echo "  WARNING: XMX driver build failed; per-game XMX stays unavailable"; return 0; }
    mkdir -p "$HOME/.config/xess-xmx"
    printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s","api_version":"1.4.0"}}\n' \
        "$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so" > "$HOME/.config/xess-xmx/icd-xmx.json"
    "$here/tools/xmx-write-env.sh" >/dev/null 2>&1 || true
}

build_mesa() {
    local out="$CACHE/mesa-fast/usr/local" src="$CACHE/mesa-fast/src" tarball="$CACHE/mesa/mesa-$MESA_COMMIT.tar.gz" f
    if [[ -x $out/lib64/libvulkan_intel.so && -z ${OXP3_MESA_REBUILD:-} ]]; then
        say "mesa: reusing the podman build in $out"
        MESA_BUILD_DIR=$out; return 0
    fi
    command -v podman >/dev/null || die "podman is required to build Mesa"
    mkdir -p "$CACHE/mesa" "$CACHE/mesa-fast"
    [[ -s $tarball ]] || curl -sfL --retry 3 -o "$tarball" "$MESA_URL" || die "download failed: $MESA_URL"
    say "mesa: source $MESA_COMMIT + patches/mesa"
    rm -rf "$src"; mkdir -p "$src"
    tar xzf "$tarball" -C "$src" --strip-components=1
    python3 - "$CACHE/anatase/cards/drivers/mesa/overrides.patch" "$CACHE/mesa-fast/overrides.patch" <<'PYEOF'
import re, sys
s = open(sys.argv[1]).read()
parts = re.split(r'(?m)^(?=diff --git )', s)
keep = [x for x in parts if not x.startswith('diff --git a/src/amd/vulkan/00-radv-defaults.conf')]
open(sys.argv[2], 'w').write(''.join(keep))
PYEOF
    git -C "$src" apply -p1 "$CACHE/mesa-fast/overrides.patch" || die "overrides.patch does not apply"
    for f in "$here"/patches/mesa/*.patch; do
        git -C "$src" apply -p1 "$f" || die "$(basename "$f") does not apply to $MESA_COMMIT"
    done
    say "mesa: podman meson build (fedora:45)"
    podman run --rm -v "$src":/src:ro,z -v "$CACHE/mesa-fast":/out:z registry.fedoraproject.org/fedora:45 bash -lc '
set -e
dnf -y -q install meson ninja-build gcc gcc-c++ bison flex pkgconf-pkg-config python3-mako \
  python3-yaml python3-packaging python3-setuptools llvm22-devel llvm22-libs clang22 clang22-devel \
  zlib-devel expat-devel expat-static libdrm-devel libxcb-devel libX11-devel xorg-x11-proto-devel \
  libXext-devel libXrandr-devel libXfixes-devel libXxf86vm-devel libxshmfence-devel wayland-devel \
  wayland-protocols-devel libzstd-devel glslang-devel spirv-llvm-translator22-devel spirv-tools-devel \
  > /tmp/dnf.log 2>&1
ln -sf /usr/lib64/pkgconfig/LLVMSPIRVLib.pc /usr/lib64/pkgconfig/llvmspirvlib.pc 2>/dev/null || true
rm -rf /b; mkdir -p /out/usr/local
meson setup /b /src --prefix=/usr/local --buildtype=release -Dprefer_static=true \
  -Dvulkan-drivers=intel -Dgallium-drivers=iris,llvmpipe -Dplatforms=x11,wayland -Dglx=dri -Dllvm=enabled \
  > /tmp/cfg.log 2>&1 || { tail -25 /tmp/cfg.log; exit 1; }
ninja -C /b -j"$(nproc)" > /tmp/build.log 2>&1 || { grep -m15 "error:" /tmp/build.log; exit 2; }
DESTDIR=/out ninja -C /b install > /tmp/inst.log 2>&1
cp /usr/lib64/libexpat.so.1 /out/usr/local/lib64/ 2>/dev/null || true
echo MESA_PODMAN_BUILD_DONE
' || die "Mesa build failed (see the podman output above)"
    [[ -x $out/lib64/libvulkan_intel.so ]] || die "build produced no libvulkan_intel.so"
    MESA_BUILD_DIR=$out
    say "mesa: built $out"
}

install_mesa_fast() {
    _mpick
    [[ -n $MESA_BUILD_DIR ]] || die "no Mesa build - run '$0 build' first"
    local src="$MESA_BUILD_DIR/lib64" manifest="$MESA_BACKUP/mesa.manifest" f
    sudo mkdir -p "$MESA_BACKUP"; : | sudo tee "$manifest" >/dev/null
    local need_expat=0
    readelf -d "$src/libvulkan_intel.so" 2>/dev/null | grep -q libexpat && need_expat=1
    for f in libvulkan_intel.so libexpat.so.1; do
        [[ -e $src/$f ]] || continue
        [[ $f == libexpat.so.1 && $need_expat == 0 ]] && continue
        # -e is false for a dangling symlink, which is how earlier versions left the stock
        # backup (cp -a of /usr/lib64/libexpat.so.1, itself a symlink to the versioned file).
        # Recreate it from the real file so a restore does not depend on a broken link.
        if [[ ! -e $MESA_BACKUP/stock/$f ]]; then
            local cand="/usr/lib64/$f"
            [[ $f == libexpat.so.1 && -e /usr/lib64/libexpat.so.1.12.3 ]] && cand="/usr/lib64/libexpat.so.1.12.3"
            if [[ -e $cand ]]; then
                sudo mkdir -p "$MESA_BACKUP/stock"
                sudo rm -f "$MESA_BACKUP/stock/$f"
                sudo cp -L "$cand" "$MESA_BACKUP/stock/$f"
            fi
        fi
        sudo install -m 0755 "$src/$f" "/usr/lib64/$f"
        echo "/usr/lib64/$f" | sudo tee -a "$manifest" >/dev/null
    done
    echo "  $(grep -c . "$manifest") Mesa file(s) into /usr/lib64 (stock in $MESA_BACKUP/stock)"
    # The boot keep unit can only restore what is recorded here; a fresh install used
    # to have no known-good copy and silently fell back to stock after the first reboot.
    local h hash kg="$CACHE/known-good"
    h=$(dirname "$src") # $CACHE/mesa-fast/usr/local/lib64/libvulkan_intel.so -> lib64
    hash=$(sha256sum "$src/libvulkan_intel.so" | cut -c1-16)
    sudo mkdir -p "$kg"
    sudo install -m 0644 "$src/libvulkan_intel.so" "$kg/host-$hash-libvulkan_intel.so"
    sudo ln -sfn "host-$hash-libvulkan_intel.so" "$kg/host-current"
    echo "  known-good host Mesa recorded: host-$hash-libvulkan_intel.so"
}

remove_mesa_fast() {
    local manifest="$MESA_BACKUP/mesa.manifest" f base
    [[ -r $manifest ]] || return 0
    while read -r f; do
        [[ -z $f ]] && continue
        base=${f##*/}
        if [[ -e $MESA_BACKUP/stock/$base ]]; then sudo cp -a "$MESA_BACKUP/stock/$base" "$f"; else sudo rm -f "$f"; fi
    done < <(sudo cat "$manifest")
    sudo rm -f "$manifest"
    echo "  stock driver restored"
}

# Install the XMX stack per game instead of the package's session-wide install.sh:
# a session VK_DRIVER_FILES would put gamescope/desktop on the 26.3 CM driver and
# break PQ HDR. The shim/IGC live in the user's package; the driver and ICD are
# already in place from the build phase.
install_xess() {
    [[ -d $OUT/xess-xmx ]] || die "missing $OUT/xess-xmx - run '$0 build' first"
    rm -rf "$XESS_PKG"; mkdir -p "$XESS_PKG"
    rsync -a --exclude /lib/ "$OUT/xess-xmx/" "$XESS_PKG/"
    seed_xess_compiler
    # Fresh installs have no seed in ~/Downloads: fetch IGC/ocloc the same way the
    # upstream install.sh would (the CM helper cannot work without it).
    if ! ls "$XESS_PKG"/igc/neo/bin/ocloc* >/dev/null 2>&1; then
        if [[ -x $XESS_PKG/tools/get-igc.sh ]]; then
            ( cd "$XESS_PKG" && bash tools/get-igc.sh ) | sed 's/^/  /' || warn "IGC fetch failed; XMX kernels cannot compile until tools/get-igc.sh runs"
        else
            warn "no tools/get-igc.sh in $XESS_PKG; XMX kernels cannot compile without IGC"
        fi
    fi
    mkdir -p "$HOME/.config/xess-xmx"
    printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s","api_version":"1.4.0"}}\n' \
        "$HOME/.cache/oxp3-fixes/xmx-driver/libvulkan_intel.so" > "$HOME/.config/xess-xmx/icd-xmx.json"
    "$here/tools/xmx-write-env.sh" >/dev/null 2>&1 || true
    if [[ ${EUID:-$(id -u)} -eq 0 && -n ${SUDO_USER:-} ]]; then
        chown -R "$SUDO_USER": "$XESS_PKG" "$HOME/.config/xess-xmx" 2>/dev/null || true
    fi
    echo "  per-game XMX env written; no session-wide VK_DRIVER_FILES (gamescope keeps the host driver)"
    echo "  run tools/xmx-steam-enable.sh --all (or --appid <id>) to put it into a game's LaunchOptions"
}

# ---------------------------------------------------------------------------------------------
# XeSS on XMX (xess-xmx-linux, our fork with Anatase support): the Mesa patches are in
# patches/mesa (0101-0105). The XESS_* pins below feed build_xess/install_xess/status_xess,
# which live in tools/lib/install-common.sh.
# ---------------------------------------------------------------------------------------------
XESS_COMMIT=9532c79cebd55b8e3bac6f6498a49575a63d249b      # silentsudin/xess-xmx-linux, branch anatase
XESS_SRC_URL=https://codeload.github.com/silentsudin/xess-xmx-linux/tar.gz/$XESS_COMMIT
# the Windows DLLs from upstream's release (built there with MSVC)
XESS_ZIP_URL=https://github.com/HHHHanasak1/xess-xmx-linux/releases/download/20261029/xess-xmx-linux-20261029.zip
XESS_ZIP_SHA=b195b7a9247e429ae2583528110b033de26f97e99d561b6a8b586b50497843d2
XESS_DLLS="igdext64.dll|580ddcce0929fb8942062cd2a7413d9c6ec44ebfb2eacd40ff2f2a3ab16b39ec ControlLib.dll|b57c70cf7cbfc38d2f929a552dab69195c9ca04d249f1201960a752c5c53598c"
# under home: the Steam Flatpak and the game's container both see it
XESS_PKG=${OXP3_XESS_DIR:-$HOME/.local/share/xess-xmx}

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
EXTRA_UNITS=(oxp3-tdp-sync.service oxp3-xpad-sleep.service oxp3-rapl-pp1.service)
EXTRA_STATE=/etc/oxp3-fixes

# Local to this variant: oxp3-fixes.sh has no sched_ext step (see docs/hw/battery-tdp.md and the
# CPU scheduler measurements in docs/QA.md's follow-ups).
install_sched() {
    # scx_cosmos -c 0 -p 0 (official gaming profile) measured +4.8% Cyberpunk average and +5.7% FPS/W over EEVDF,
    # with GravityMark unchanged and no power cost. Forcing per-cluster EPP on top was 3 FPS/1 W worse, so EPP is
    # left at balance_power.
    if ! command -v scx_cosmos >/dev/null 2>&1; then
        echo "  scx_cosmos not installed - skipping the sched_ext scheduler"
        return 0
    fi
    sudo install -m 0644 "$here/files/oxp3-gaming-sched.service" /etc/systemd/system/oxp3-gaming-sched.service
    sudo systemctl daemon-reload
    sudo systemctl enable oxp3-gaming-sched.service >/dev/null 2>&1
    sudo systemctl restart oxp3-gaming-sched.service
    sleep 1
    echo "  sched_ext: scx_cosmos gaming profile loaded (state=$(cat /sys/kernel/sched_ext/state 2>/dev/null)), EEVDF fallback if it unloads"
}

# Kept local: this variant leaves the suspend units to tools/oxp3-hw-install.sh, while
# oxp3-fixes.sh installs them here, so the two bodies genuinely differ.
install_extras() {
    local s u me; me=$(id -un)
    for s in oxp3-tdp-sync oxp3-xpad-sleep oxp3-audio-route; do
        sudo install -D -m 0755 "$here/files/$s" "/usr/local/libexec/$s"
    done
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
    sudo systemctl daemon-reload
    sudo systemctl enable oxp3-xpad-sleep.service oxp3-rapl-pp1.service "oxp3-audio-resume@$me.service" >/dev/null 2>&1
    sudo systemctl enable oxp3-tdp-sync.service >/dev/null 2>&1
    sudo systemctl restart oxp3-tdp-sync.service
    sudo systemctl --global enable oxp3-audio-route.service >/dev/null 2>&1
    systemctl --user daemon-reload
    # thermald exits at boot here: the BIOS has no PSVT tables for it ("THD engine init failed")
    if systemctl is-enabled -q thermald.service 2>/dev/null; then
        sudo systemctl disable --now thermald.service >/dev/null 2>&1
        sudo install -d "$EXTRA_STATE"; sudo touch "$EXTRA_STATE/thermald-disabled"
    fi
    echo "  TDP sync, speaker route check (+ resume), speaker name, xpad sleep, GPU power zone check"
    echo "  (the speaker name shows after WirePlumber restarts: next login, or systemctl --user restart wireplumber)"
}

uninstall_extras() {
    local u me; me=$(id -un)
    sudo systemctl disable --now "${EXTRA_UNITS[@]}" "oxp3-audio-resume@$me.service" >/dev/null 2>&1 || true
    sudo systemctl --global disable oxp3-audio-route.service >/dev/null 2>&1 || true
    for u in "${EXTRA_UNITS[@]}" oxp3-audio-resume@.service; do sudo rm -f "/etc/systemd/system/$u"; done
    local bmiconf=/etc/modprobe.d/oxp3-bmi160.conf rbt="$here/tools/oxp3-rollback.sh"
    if [[ -x $rbt ]] && "$rbt" has-record "$bmiconf" 2>/dev/null; then
        sudo "$rbt" restore-file "$bmiconf" || sudo rm -f "$bmiconf"
    else
        sudo rm -f "$bmiconf"
    fi
    sudo rm -f /etc/systemd/user/oxp3-audio-route.service \
        /etc/wireplumber/wireplumber.conf.d/52-oxp3-speakers.conf \
        /usr/local/libexec/oxp3-tdp-sync /usr/local/libexec/oxp3-xpad-sleep /usr/local/libexec/oxp3-audio-route
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
# The patch list above is consumed by hhd_site/install_volume_fix/hhd_volume_unpatch in
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
FLATPAK_MESA_SKIP='libvulkan_(radeon|panfrost|nouveau|freedreno|asahi|powervr_mesa|broadcom|dzn|virtio|intel_hasvk|lvp)\.so|/icd\.d/(radeon|panfrost|nouveau|freedreno|asahi|powervr_mesa|broadcom|dzn|virtio|intel_hasvk|lvp)_icd|RusticlOpenCL|/OpenCL/|^/usr/share/(doc|licenses|man)/'

# Kept local: this variant installs its own built driver (and expat) into the runtime
# overrides; the RPM variant copies the distro Mesa files. Only remove_flatpak_mesa/
# refresh_flatpak_runtime are shared.
install_flatpak_mesa() {
    _mpick
    [[ -n $MESA_BUILD_DIR ]] || die "no Mesa build - run '$0 build' first"
    # OXP3_FLATPAK_BUILD selects a different tree for the Steam Flatpak runtime only: the
    # host can run a dynamic-expat build (faster patches) while the sandbox gets a static one.
    local root="$MESA_BUILD_DIR" src list
    if [[ -n ${OXP3_FLATPAK_BUILD:-} ]]; then
        # same convention as OXP3_MESA_BUILD: the value is the prefix that contains lib64/
        # (accept a tree root too, for convenience)
        root="${OXP3_FLATPAK_BUILD%/}"
        [[ -e $root/lib64/libvulkan_intel.so ]] || root="$root/usr/local"
        [[ -e $root/lib64/libvulkan_intel.so ]] || die "OXP3_FLATPAK_BUILD=$OXP3_FLATPAK_BUILD has no lib64/libvulkan_intel.so"
        say "Flatpak runtime driver from OXP3_FLATPAK_BUILD: $root"
    fi
    src="$root/lib64"
    [[ -d $FLATPAK_RT/overrides ]] || { echo "  no Anatase Flatpak runtime overrides - Steam keeps Anatase's Mesa"; return 0; }
    [[ -e $src/libvulkan_intel.so ]] || return 0
    sudo mkdir -p "$FLATPAK_RT/overrides/lib64" "$MESA_BACKUP/flatpak-stock"
    if [[ ! -e $MESA_BACKUP/flatpak-stock/libexpat.so.1 && -e $FLATPAK_RT/files/lib64/libexpat.so.1 ]]; then
        sudo cp -a "$FLATPAK_RT/files/lib64/libexpat.so.1" "$MESA_BACKUP/flatpak-stock/libexpat.so.1"
    fi
    sudo install -m 0755 "$src/libvulkan_intel.so" "$FLATPAK_RT/overrides/lib64/libvulkan_intel.so"
    list=$'lib64/libvulkan_intel.so\n'
    if readelf -d "$src/libvulkan_intel.so" 2>/dev/null | grep -q libexpat; then
        [[ -e $src/libexpat.so.1 ]] || die "driver needs expat but $src/libexpat.so.1 is missing"
        sudo install -m 0755 "$src/libexpat.so.1" "$FLATPAK_RT/overrides/lib64/libexpat.so.1"
        list+=$'lib64/libexpat.so.1\n'
        echo "  Intel Vulkan driver + bundled expat in the Steam Flatpak runtime overrides"
    else
        sudo rm -f "$FLATPAK_RT/overrides/lib64/libexpat.so.1"
        echo "  driver is self-contained (static expat): no runtime expat override needed"
    fi
    local hash kg="$CACHE/known-good"
    hash=$(sha256sum "$src/libvulkan_intel.so" | cut -c1-16)
    sudo mkdir -p "$kg"
    sudo install -m 0644 "$src/libvulkan_intel.so" "$kg/runtime-$hash-libvulkan_intel.so"
    sudo ln -sfn "runtime-$hash-libvulkan_intel.so" "$kg/runtime-current"
    printf '%s' "$list" | sudo tee "$FLATPAK_MESA_LIST" >/dev/null
    refresh_flatpak_runtime
}

# ---------------------------------------------------------------------------------------------
# install / uninstall (root)
# ---------------------------------------------------------------------------------------------
# Kept local: the variants stage different things (podman-built Mesa, sched_ext and the Flatpak
# runtime here; Mesa RPMs in oxp3-fixes.sh), so the flow stays with each caller. Every step it
# calls (MOK, RAPL, extras, XeSS, deployment state) is in tools/lib/install-common.sh.
# ---------------------------------------------------------------------------------------------

do_install() {
    local rpms=() kmod=() kver info staged locals p un=()
    (( SKIP_GAMESCOPE )) || rpms+=( "$OUT"/gamescope-*.x86_64.rpm "$OUT"/gamescope-*.i686.rpm )
    (( SKIP_MANGOHUD )) || rpms+=( "$OUT"/mangohud-*.x86_64.rpm "$OUT"/mangohud-*.i686.rpm )
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
        say "hardware fixes: home button/LEDs + power units (sudo)"
        sudo "$here/tools/oxp3-hw-install.sh" --leds --power ||
            echo "WARNING: hardware fixes (leds/power units) failed - see above" >&2
    fi

    # 1. kernel modules + firmware: xe loads from the initramfs, so both are layered with
    #    rpm-ostree and the initramfs regenerated. That makes a new deployment, which doesn't keep
    #    the writable /usr overlay, so this goes first; gamescope/MangoHud follow on the next run.
    local layer=() want=() pkg
    if (( !SKIP_KERNEL )) && [[ ! -e $OUT/.kernel-not-needed ]]; then
        kmod=( "$OUT/$KMOD_NAME"-*.rpm )
        [[ -f ${kmod[0]} ]] || die "no $KMOD_NAME package - run '$0 build' first"
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
    if (( !SKIP_FIRMWARE )); then
        local fw=( "$OUT/$FW_NAME"-*.rpm )
        [[ -f ${fw[0]} ]] && layer+=( "${fw[0]}" )
    fi
    for pkg in "${layer[@]}"; do want+=( "$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' "$pkg")" ); done
    # our layered packages that are not wanted any more (older builds, kmod-oxp3-xe, or firmware
    # Anatase now ships itself)
    for p in $locals; do
        [[ $p == kmod-oxp3-* || $p == "$FW_NAME"-* ]] || continue
        (( SKIP_KERNEL )) && [[ $p == kmod-oxp3-* ]] && continue
        (( SKIP_FIRMWARE )) && [[ $p == "$FW_NAME"-* ]] && continue
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
    if (( ${#rpms[@]} || !SKIP_EXTRAS || !SKIP_XESS || !SKIP_MESA )); then
        if [[ $staged == staged ]]; then
            echo "A deployment is staged (an update or a pending change): reboot into it first,"
            echo "then run '$0 install' again - changes to the current /usr would be lost."
            return 0
        fi
        # "Deployment is already in unlocked state" is a non-zero exit that still means
        # the overlay is writable (the keep units race into it at boot). Ignore the
        # status and decide by probing the filesystem.
        hotfix_active || sudo rpm-ostree usroverlay --hotfix >/dev/null 2>&1 || true
        if ! sudo install -m 000 /dev/null "/usr/.oxp3-rw-probe.$$" 2>/dev/null; then
            die "the /usr overlay did not become writable (rpm-ostree usroverlay failed and /usr is read-only)"
        fi
        sudo rm -f "/usr/.oxp3-rw-probe.$$"
        if (( ${#rpms[@]} )); then
            say "gamescope / MangoHud / Mesa into the writable /usr overlay (sudo)"
            sudo rpm -Uvh --replacepkgs --oldpackage --nosignature "${rpms[@]}"
            rpm -q gamescope mangohud | sed 's/^/  /'
            if (( !SKIP_MESA )); then
                say "Mesa for the Steam Flatpak (sudo)"
                install_flatpak_mesa
            fi

        fi
        if (( !SKIP_MESA )); then
            say "Mesa (podman build) into the writable /usr overlay (sudo)"
            install_mesa_fast
            if (( !SKIP_FLATPAK )); then
                say "Mesa for the Steam Flatpak (sudo)"
                install_flatpak_mesa
            else
                echo "  --skip-flatpak: the Flatpak Steam runtime keeps Anatase's Mesa"
            fi
        fi
        if (( !SKIP_SCHED )); then
            say "Gaming CPU scheduler (scx_cosmos gaming profile)"
            install_sched
        fi
        if (( !SKIP_EXTRAS )); then
            say "volume rocker (sudo)"
            install_volume_fix
        fi
        if (( !SKIP_XESS )); then
            say "XeSS on XMX (xess-xmx-linux)"
            install_xess
        fi
        say "done - leave and re-enter Gamemode (no reboot needed)"
        echo "This lasts until the next Anatase update; run '$0 all' after updating."
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
    sudo systemctl disable --now oxp3-gaming-sched.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/oxp3-gaming-sched.service
    sudo systemctl daemon-reload
    uninstall_extras
    if [[ -x $here/tools/oxp3-hw-install.sh ]]; then
        sudo "$here/tools/oxp3-hw-install.sh" --uninstall --leds --power ||
            echo "WARNING: removing the hardware units failed - see above" >&2
    fi
    uninstall_xess
    if [[ -r $FLATPAK_MESA_LIST ]]; then remove_flatpak_mesa; refresh_flatpak_runtime; fi
    remove_mesa_fast
    if [[ -e $VOLUME_HWDB ]]; then
        sudo rm -f "$VOLUME_HWDB"; sudo systemd-hwdb update
    fi
    local site; site=$(hhd_site)
    if [[ -n $site ]]; then hhd_volume_unpatch "$site"; sudo systemctl restart hhd.service; fi
    echo "Reboot to finish. The patched gamescope/MangoHud live in the writable /usr overlay of"
    echo "the current deployment; any new deployment (the reboot above, or the next Anatase"
    echo "update) starts from the stock packages again."
}

# Local to this variant: the RPM installer has no podman-built runtime to refresh or verify.
refresh() {
    # rebuild the Flatpak runtime image from the current overrides (driver may be newer than
    # the image built at boot). Gamemode's Steam IS the Flatpak, so this is the step that matters.
    if [[ -n $(flatpak ps --columns=application 2>/dev/null) ]]; then
        echo "Flatpak apps are running (Steam in Gamemode is one). Close them first:"
        echo "  exit Gamemode / quit Steam, then run: sudo $0 refresh"
        return 1
    fi
    sudo systemctl restart anatase-flatpak-extensions.service || return 1
    sleep 1
    verify
}

verify() {
    local rtf=/run/anatase/flatpak/runtime/org.anatase.Platform/x86_64/stable/files/lib64
    echo "host:"
    if LD_LIBRARY_PATH=/usr/lib64 python3 -c "import ctypes;l=ctypes.CDLL('/usr/lib64/libvulkan_intel.so');assert l.vk_icdGetInstanceProcAddr" 2>/dev/null; then
        echo "  /usr/lib64/libvulkan_intel.so loads (Intel ICD usable)"
    else
        echo "  /usr/lib64/libvulkan_intel.so FAILS to load -> Vulkan falls back to lavapipe"
    fi
    if [[ -d $HOME/.local/share/Steam || -d $HOME/.steam ]]; then
        echo "steam (native, Steam Linux Runtime): pressure-vessel maps the host driver;"
        echo "  the host line above decides whether games get the Intel driver or lavapipe."
    fi
    echo "steam flatpak runtime:"
    local ov=$FLATPAK_RT/overrides/lib64/libvulkan_intel.so
    if [[ ! -e $rtf/libvulkan_intel.so ]]; then
        echo "  no runtime driver present"
    elif [[ -e $ov ]] && ! cmp -s "$ov" "$rtf/libvulkan_intel.so"; then
        echo "  runtime image is STALE: close Flatpak apps and run"
        echo "    sudo systemctl restart anatase-flatpak-extensions.service   (or reboot)"
    elif LD_LIBRARY_PATH=$rtf python3 -c "import ctypes;l=ctypes.CDLL('$rtf/libvulkan_intel.so');assert l.vk_icdGetInstanceProcAddr" 2>/dev/null; then
        echo "  runtime driver loads"
    else
        echo "  runtime driver FAILS to load -> Flatpak Steam falls back to lavapipe"
    fi
}

# Refuse to run if this caller does not satisfy the install-common contract (API version,
# required configuration variables and the build_mesa hook): see tools/lib/install-common.sh.
install_common_guard

case $cmd in
    build) do_build ;;
    install) do_install ;;
    all) do_build; do_install ;;
    status) do_status ;;
    verify) verify ;;
    refresh) refresh ;;
    uninstall) do_uninstall ;;
    mok-setup) do_mok_setup ;;
    *) sed -n '2,/^set -euo pipefail/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
