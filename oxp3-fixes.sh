#!/bin/bash
# Restore OneXPlayer 3 support on Anatase after an update:
#   - gamescope: PQ HDR on the SDC AMS881KB01 OLED (no black screens, correct peak brightness)
#   - MangoHud:  CPU power, GPU power and VRAM in the Gamemode overlay on Intel iGPUs,
#                with power from the rapl-power service (counters stay root-only)
#   - kernel:    the VBT VS/PE-O backport for the eDP link (white dots, HDR retoggle black
#                screen), as a MOK-signed xe module for the installed Anatase kernel
#
#   ./oxp3-fixes.sh build       build everything for the installed Anatase (as your user)
#   ./oxp3-fixes.sh install     install the built packages (asks for sudo), then reboot
#   ./oxp3-fixes.sh all         build + install
#   ./oxp3-fixes.sh status      what is installed / layered / overridden
#   ./oxp3-fixes.sh uninstall   back to stock Anatase (asks for sudo), then reboot
#
# Options (after the command): --skip-kernel --skip-gamescope --skip-mangohud
# Environment: OXP3_MOK_DIR (default ~/.local/share/oxp3-fixes/mok, needs MOK.key + MOK.der)
#              OXP3_CACHE   (default ~/.cache/oxp3-fixes)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
CACHE="${OXP3_CACHE:-$HOME/.cache/oxp3-fixes}"
MOK_DIR="${OXP3_MOK_DIR:-$HOME/.local/share/oxp3-fixes/mok}"
OUT="$CACHE/out"
ANATASE_REPO=https://github.com/anatase-org/anatase.git
PATCHWORK_REPO=https://github.com/anatase-org/patchwork.git
FEDORA="$(rpm -E %fedora)"
IMAGE="localhost/oxp3-fixes-build:f$FEDORA"
CTR="oxp3-fixes-build-f$FEDORA"
KMOD_NAME=kmod-oxp3-xe

SKIP_KERNEL=0 SKIP_GAMESCOPE=0 SKIP_MANGOHUD=0
cmd="${1:-}"; shift || true
for a in "$@"; do
    case $a in
        --skip-kernel) SKIP_KERNEL=1 ;;
        --skip-gamescope) SKIP_GAMESCOPE=1 ;;
        --skip-mangohud) SKIP_MANGOHUD=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The Anatase kernel in the current deployment (not necessarily the booted one).
anatase_kver() { rpm -q kernel-core --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' | head -1; }

# ---------------------------------------------------------------------------------------------
# build container
# ---------------------------------------------------------------------------------------------
ctr() { podman exec -w "$CACHE" "$CTR" "$@"; }
ctr_root() { podman exec -u 0 -w "$CACHE" "$CTR" "$@"; }

ensure_container() {
    mkdir -p "$CACHE" "$OUT"
    # rebuild the image when the Containerfile changes
    local sum; sum=$(sha256sum "$here/container/Containerfile" | cut -c1-12)
    if [[ $(podman image inspect "$IMAGE" --format '{{index .Labels "oxp3.containerfile"}}' 2>/dev/null) != "$sum" ]]; then
        podman rm -f "$CTR" >/dev/null 2>&1 || true
        podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    fi
    if ! podman image exists "$IMAGE"; then
        say "creating build image $IMAGE (once, a few minutes)"
        podman build --build-arg FEDORA="$FEDORA" --label oxp3.containerfile="$sum" -t "$IMAGE" \
            -f "$here/container/Containerfile" "$here/container"
    fi
    if ! podman container exists "$CTR"; then
        podman run -d --name "$CTR" --userns=keep-id --security-opt label=disable \
            -v "$CACHE:$CACHE" -v "$here:$here:ro" -v "$MOK_DIR:$MOK_DIR:ro" \
            "$IMAGE" sleep infinity >/dev/null 2>&1 || {
            # MOK dir may not exist when only userspace parts are built
            podman run -d --name "$CTR" --userns=keep-id --security-opt label=disable \
                -v "$CACHE:$CACHE" -v "$here:$here:ro" "$IMAGE" sleep infinity >/dev/null; }
    fi
    podman start "$CTR" >/dev/null
}

# ---------------------------------------------------------------------------------------------
# gamescope / MangoHud: Anatase's packaging + our patches, rebuilt as RPMs
# ---------------------------------------------------------------------------------------------
fetch_anatase() {
    if [[ -d $CACHE/anatase/.git ]]; then
        git -C "$CACHE/anatase" fetch -q --depth 1 origin HEAD
        git -C "$CACHE/anatase" reset -q --hard FETCH_HEAD
    else
        git clone -q --depth 1 "$ANATASE_REPO" "$CACHE/anatase"
    fi
    echo "anatase packaging: $(git -C "$CACHE/anatase" log -1 --format='%h %cd %s' --date=short)"
}

# build_card <name> <patch-line-regex> <patch files...>
build_card() {
    local name=$1 anchor=$2; shift 2
    local top="$CACHE/rpmbuild/$name" spec
    rm -rf "$top"; mkdir -p "$top/SOURCES"
    cp -r "$CACHE/anatase/cards/gaming/$name/." "$top/SOURCES/"
    spec="$top/SOURCES/$name.spec"
    # add our patches right after Anatase's overrides.patch, in order
    local n=2000 lines="" p
    for p in "$@"; do
        cp "$p" "$top/SOURCES/"
        lines+="Patch$n: $(basename "$p")\n"; n=$((n + 1))
    done
    grep -qE "$anchor" "$spec" || die "$name.spec: no '$anchor' line - Anatase packaging changed"
    sed -i -E "0,/$anchor/s//&\n${lines%\\n}/" "$spec"
    local ver; ver=$(sed -n 's/^Version:[[:space:]]*//p' "$spec")
    local inst; inst=$(rpm -q "$name" --qf '%{VERSION}\n' 2>/dev/null | head -1 || true)
    [[ -z $inst || $inst == "$ver" ]] || echo "note: installed $name $inst, Anatase packaging has $ver"

    say "$name $ver: sources"
    ctr spectool -g -C "$top/SOURCES" "$spec" >/dev/null
    ctr_root dnf -y -q builddep "$spec" >/dev/null 2>&1 || ctr_root dnf -y builddep --skip-unavailable "$spec" | tail -3
    say "$name $ver: x86_64"
    ctr rpmbuild --define "_topdir $top" --define "dist .fc$FEDORA.oxp3" -bb "$spec" > "$top/build.log" 2>&1 ||
        { tail -30 "$top/build.log"; die "$name x86_64 build failed (full log: $top/build.log)"; }
    say "$name $ver: i686"
    ctr bash -c "export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig; setarch i686 rpmbuild --target i686 --nodeps --define '_topdir $top' --define 'dist .fc$FEDORA.oxp3' -bb '$spec'" \
        > "$top/build-i686.log" 2>&1 ||
        { tail -30 "$top/build-i686.log"; die "$name i686 build failed (full log: $top/build-i686.log)"; }
    rm -f "$OUT/$name"-*.rpm
    find "$top/RPMS" -name "$name-$ver-*.rpm" ! -name '*debug*' -exec cp {} "$OUT/" \;
    ls "$OUT/$name"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# kernel: xe module with the VS/PE-O backport for the installed Anatase kernel
# ---------------------------------------------------------------------------------------------
build_xe() {
    local kver tag src kb cfg hash
    kver=$(anatase_kver)
    tag=${kver%%.fc*}                              # 7.2.7-an19.fc45.x86_64 -> 7.2.7-an19
    cfg=/usr/lib/modules/$kver/config
    [[ -r $cfg ]] || die "no kernel config at $cfg"
    [[ -r $MOK_DIR/MOK.key && -r $MOK_DIR/MOK.der ]] ||
        die "MOK key not found in $MOK_DIR (see README: Secure Boot key), or use --skip-kernel"
    src="$CACHE/kernel/src-$tag"; kb="$CACHE/kernel/build-$tag"

    say "kernel $kver: source (anatase-org/patchwork tag $tag)"
    if [[ ! -d $src/.git ]]; then
        git clone -q --depth 1 --branch "$tag" "$PATCHWORK_REPO" "$src" ||
            die "no tag $tag in anatase-org/patchwork"
    fi
    git -C "$src" reset -q --hard "$tag" 2>/dev/null || git -C "$src" reset -q --hard HEAD
    git -C "$src" clean -qfdx
    # Already in Anatase? Then there is nothing to build.
    local first; first=$(ls "$here"/patches/kernel/*.patch | head -1)
    if ! git -C "$src" apply --check "$first" 2>/dev/null; then
        if git -C "$src" apply --check -R "$first" 2>/dev/null; then
            echo "kernel $tag already contains the VS/PE-O backport - no xe module needed"
            rm -f "$OUT/$KMOD_NAME"-*.rpm; touch "$OUT/.kernel-not-needed"; return 0
        fi
        die "kernel patches don't apply to $tag - they need updating"
    fi
    rm -f "$OUT/.kernel-not-needed"
    git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am -q "$here"/patches/kernel/*.patch
    # out-of-tree drivers Anatase adds as submodules: stub their Kconfig, xe doesn't use them
    local k
    for k in $(sed -n 's/^source "\(drivers\/custom\/[^"]*\)"/\1/p' "$src/drivers/custom/Kconfig" 2>/dev/null); do
        [[ -e $src/$k ]] || { mkdir -p "$(dirname "$src/$k")"; : > "$src/$k"; }
    done

    say "kernel $kver: building xe.ko (10-20 min)"
    rm -rf "$kb"; mkdir -p "$kb"; cp "$cfg" "$kb/.config"
    ctr bash -c "cd '$src' && make -s O='$kb' KERNELRELEASE='$kver' olddefconfig &&
        make -s O='$kb' KERNELRELEASE='$kver' -j\$(nproc) modules_prepare &&
        make -s O='$kb' KERNELRELEASE='$kver' KBUILD_MODPOST_WARN=1 -j\$(nproc) M=drivers/gpu/drm/xe modules" \
        > "$kb/build.log" 2>&1 || { tail -30 "$kb/build.log"; die "xe build failed (full log: $kb/build.log)"; }
    local ko="$src/drivers/gpu/drm/xe/xe.ko"
    [[ -f $ko ]] || die "xe.ko not produced"
    [[ $(ctr modinfo -F vermagic "$ko") == "$kver "* ]] || die "xe.ko vermagic doesn't match $kver"

    say "kernel $kver: signing with $MOK_DIR/MOK.der"
    hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"/\1/p' "$cfg"); hash=${hash:-sha512}
    local stage="$CACHE/kernel/stage-$tag"
    rm -rf "$stage"; mkdir -p "$stage"
    ctr strip --strip-debug -o "$stage/xe.ko" "$ko"
    ctr "$kb/scripts/sign-file" "$hash" "$MOK_DIR/MOK.key" "$MOK_DIR/MOK.der" "$stage/xe.ko"
    ctr modinfo -F signer "$stage/xe.ko"
    ctr xz -f --check=crc32 --lzma2=dict=1MiB "$stage/xe.ko"

    say "kernel $kver: packaging $KMOD_NAME"
    local top="$CACHE/rpmbuild/$KMOD_NAME" rel; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    cp "$stage/xe.ko.xz" "$top/SOURCES/"
    cat > "$top/SPECS/$KMOD_NAME.spec" <<EOF
Name:           $KMOD_NAME
Version:        ${kver//-/_}
Release:        $rel
Summary:        xe with the VBT VS/PE-O backport for the OneXPlayer 3 eDP panel
License:        GPL-2.0-only
Source0:        xe.ko.xz
Requires:       kernel-core = ${kver%.*}
%global debug_package %{nil}
%global __os_install_post %{nil}
%description
xe display driver for kernel $kver with the VBT #57 VS/PE-O override backport
(white dots, HDR retoggle black screen on the OneXPlayer 3). Signed with a local MOK.
Installed in updates/, so it takes priority over the stock xe module.
%install
install -D -m 0644 %{SOURCE0} %{buildroot}/usr/lib/modules/$kver/updates/xe.ko.xz
%files
/usr/lib/modules/$kver/updates/xe.ko.xz
%post
/usr/sbin/depmod -a $kver || :
%postun
/usr/sbin/depmod -a $kver || :
EOF
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$KMOD_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "kmod packaging failed"; }
    rm -f "$OUT/$KMOD_NAME"-*.rpm
    find "$top/RPMS" -name "$KMOD_NAME-*.rpm" -exec cp {} "$OUT/" \;
    ls "$OUT/$KMOD_NAME"-*.rpm
}

do_build() {
    command -v podman >/dev/null || die "podman is required"
    ensure_container
    if (( !SKIP_GAMESCOPE || !SKIP_MANGOHUD )); then fetch_anatase; fi
    (( SKIP_GAMESCOPE )) || build_card gamescope '^Patch:[[:space:]]+overrides\.patch' "$here"/patches/gamescope/*.patch
    (( SKIP_MANGOHUD )) || build_card mangohud '^Patch1000:[[:space:]]+overrides\.patch' "$here"/patches/mangohud/*.patch
    (( SKIP_KERNEL )) || build_xe
    say "build done: $OUT"
    ls "$OUT"
}

# ---------------------------------------------------------------------------------------------
# install / uninstall (root)
# ---------------------------------------------------------------------------------------------
mok_trusted() {
    local cn; cn=$(openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -subject -nameopt multiline |
                   sed -n 's/^ *commonName *= *//p')
    [[ -n $cn ]] && sudo keyctl list %:.machine 2>/dev/null | grep -qF "$cn"
}

# MangoHud's CPU/GPU power: a root service publishes 1 s / 0.1 W averages in /run/rapl-power;
# the raw RAPL counters stay root-only (CVE-2020-8694).
install_rapl_power() {
    sudo install -D -m 0755 "$here/files/rapl-power" /usr/local/libexec/rapl-power
    sudo install -m 0644 "$here/files/rapl-power.service" /etc/systemd/system/rapl-power.service
    sudo systemctl daemon-reload
    sudo systemctl enable rapl-power.service >/dev/null
    sudo systemctl restart rapl-power.service
    remove_rapl_udev_rules
}

# Earlier versions opened the counters to all users with a udev rule: remove it and make
# the counters root-only again right away (they are re-created root-only at boot anyway).
remove_rapl_udev_rules() {
    local r changed=0
    for r in /etc/udev/rules.d/70-oxp3-rapl.rules /etc/udev/rules.d/70-anatase-rapl.rules; do
        [[ -e $r ]] && { sudo rm -f "$r"; changed=1; }
    done
    if (( changed )); then
        sudo udevadm control --reload
        sudo chmod 0400 /sys/class/powercap/intel-rapl*/energy_uj 2>/dev/null || true
    fi
}

do_install() {
    local rpms=() kmod=() old
    (( SKIP_GAMESCOPE )) || rpms+=( "$OUT"/gamescope-*.x86_64.rpm "$OUT"/gamescope-*.i686.rpm )
    (( SKIP_MANGOHUD )) || rpms+=( "$OUT"/mangohud-*.x86_64.rpm "$OUT"/mangohud-*.i686.rpm )
    local f; for f in "${rpms[@]}"; do [[ -f $f ]] || die "missing $f - run '$0 build' first"; done

    if (( !SKIP_KERNEL )) && [[ ! -e $OUT/.kernel-not-needed ]]; then
        kmod=( "$OUT/$KMOD_NAME"-*.rpm )
        [[ -f ${kmod[0]} ]] || die "no $KMOD_NAME package - run '$0 build' first"
        rpm -qp --qf '%{VERSION}\n' "${kmod[0]}" | grep -qF "$(anatase_kver | sed 's/-/_/g')" ||
            die "$KMOD_NAME was built for another kernel than $(anatase_kver) - rebuild"
        if ! mok_trusted; then
            echo "WARNING: the MOK in $MOK_DIR is not in the kernel's machine keyring."
            echo "         The signed xe module would be rejected and the screen stay dark at boot."
            echo "         Enrol it first (README: Secure Boot key). Skipping the kernel part."
            kmod=()
        fi
    fi

    say "installing (sudo)"
    if (( ${#rpms[@]} )); then
        sudo rpm-ostree override replace "${rpms[@]}"
    fi
    if (( ${#kmod[@]} )); then
        old=$(rpm-ostree status --json | python3 -c 'import json,sys
d=json.load(sys.stdin)["deployments"]; d=[x for x in d if x.get("staged")] or [x for x in d if x.get("booted")]
print(" ".join(p for p in d[0].get("requested-local-packages",[]) if p.startswith("'"$KMOD_NAME"'")))')
        if [[ -n $old ]]; then
            # shellcheck disable=SC2086
            sudo rpm-ostree install "${kmod[@]}" $(printf -- '--uninstall=%s ' $old)
        else
            sudo rpm-ostree install "${kmod[@]}"
        fi
        rpm-ostree initramfs 2>/dev/null | grep -q 'Initramfs regeneration: enabled' ||
            sudo rpm-ostree initramfs --enable   # rebuild the initramfs so it has the new xe
    fi
    install_rapl_power
    say "done - reboot to use the new deployment"
    echo "If anything is wrong, pick the previous deployment in the boot menu or: sudo rpm-ostree rollback"
}

do_uninstall() {
    say "back to stock Anatase (sudo)"
    sudo rpm-ostree override reset gamescope gamescope.i686 mangohud mangohud.i686 2>/dev/null ||
        sudo rpm-ostree override reset gamescope mangohud || true
    local old
    old=$(rpm -qa "$KMOD_NAME*" | tr '\n' ' ')
    [[ -n $old ]] && sudo rpm-ostree uninstall $old || true
    if rpm-ostree initramfs 2>/dev/null | grep -q 'Initramfs regeneration: enabled'; then
        sudo rpm-ostree initramfs --disable
    fi
    sudo systemctl disable --now rapl-power.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/rapl-power.service /usr/local/libexec/rapl-power
    sudo systemctl daemon-reload
    remove_rapl_udev_rules
    echo "Reboot to finish."
}

do_status() {
    echo "kernel (deployment): $(anatase_kver)   booted: $(uname -r)"
    rpm -q gamescope mangohud "$KMOD_NAME" 2>&1 | sed 's/^/  /'
    rpm-ostree status --booted | grep -E 'Version|ReplacedBasePackages|LocalPackages|Initramfs|Unlocked' -A2 | sed 's/^/  /' || true
    echo "  rapl-power service: $(systemctl is-active rapl-power.service 2>/dev/null)"
    local f; for f in /run/rapl-power/*; do [[ -e $f ]] && echo "    $(basename "$f"): $(cat "$f") W"; done
    ls /etc/udev/rules.d/70-*rapl.rules >/dev/null 2>&1 && echo "  WARNING: old RAPL udev rule still present (run install)"
    stat -c '  RAPL counters: %A %n' /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null
    modinfo -F filename xe 2>/dev/null | sed 's/^/  xe module: /'
    [[ -d $OUT ]] && { echo "built packages in $OUT:"; ls "$OUT" | sed 's/^/  /'; }
}

case $cmd in
    build) do_build ;;
    install) do_install ;;
    all) do_build; do_install ;;
    status) do_status ;;
    uninstall) do_uninstall ;;
    *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
