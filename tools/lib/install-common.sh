#!/usr/bin/env bash
# tools/lib/install-common.sh - the build/install helpers shared by oxp3-fixes.sh and
# oxp3-fixes-fast.sh. Sourced by both; it holds every helper that was byte-identical in the two
# installers, so a fix to the shared path is made once. The two scripts keep their own
# configuration, their own Mesa pipeline and their own install/uninstall flow; the reasoning and
# the classification of every function are in docs/MAINTAINABILITY.md.
#
# CONTRACT (interface version 1)
# ------------------------------
# The caller MUST:
#   1. run under bash with `set -euo pipefail` before sourcing this file. This file does not
#      change shell options, so a caller that forgets them gets different failure behaviour;
#   2. set OXP3_INSTALL_COMMON_API=1 (the version below). A mismatch is refused by the guard;
#   3. set the configuration variables listed in install_common_guard() before it calls a
#      function. The values are read when a function runs, not when this file is sourced, so the
#      definitions may appear after the `source` line;
#   4. define build_mesa() itself - the one genuinely different build step (RPM packaging in
#      oxp3-fixes.sh, podman/meson in oxp3-fixes-fast.sh). It is the library's only caller hook;
#   5. call install_common_guard() once, after its configuration and local functions are defined
#      and before it dispatches a command. The guard refuses a caller with a wrong API version, a
#      missing configuration variable or a missing build_mesa().
#
# SOURCING SIDE EFFECTS: none. This file defines functions only: it sets no shell options, no
# variables, no traps, no umask and no locale, it runs no command, reads no file and prints
# nothing. Paths are always taken from the caller's variables (its `here`, $CACHE, $OUT, ...), so
# it can be sourced from any working directory.
#
# SIDE EFFECTS OF THE FUNCTIONS (grouped; each function documents anything unusual):
#   build_*, fetch_*, seed_*   network downloads into $CACHE (verified against pinned hashes),
#                              build trees in $CACHE/rpmbuild, packages in $OUT, podman
#                              images/containers named by the caller. No root, no host change.
#   install_*, do_mok_setup    root writes through sudo: /etc, /usr, /usr/local, /run and
#                              rpm-ostree layering; /usr overlay via rpm-ostree usroverlay.
#   status_*, do_status        read-only: rpm, rpm-ostree --booted, systemctl is-*, /sys, /run.
#   remove_*, uninstall_*      undo the corresponding install_*; idempotent, tolerate absence.
#
# IDEMPOTENCY: every install path checks the installed state first, so re-running an install is
# safe and a second run changes nothing it already applied (that is what the QA --root cycle
# relies on). Builds overwrite their own staging directories. do_mok_setup keeps an existing
# key that the machine keyring accepts and only replaces one it would reject. Uninstall paths
# tolerate missing files and units.
#
# LOCKING: this library takes no lock, and neither installer did before the extraction.
# Concurrent runs would share $CACHE/$OUT and the caller's podman container names; run one
# build/install at a time. A caller that needs more may take its own lock before calling in -
# no function here waits for, creates or removes one.
#
# LOGGING: say() prints "\n== <message>" to stdout as a phase heading; die() prints
# "error: <message>" to stderr and exits 1; the helpers print indented detail lines to stdout
# and warnings to stderr. Nothing is written to a log file or to syslog from here.
#
# COMPATIBILITY: bash, because the two installers already require bash features ([[ ]], arrays,
# process substitution, local). It is not POSIX sh and is not sourced by anything else in the
# repo; tools/oxp3-hw-install.sh intentionally does not use it (it orchestrates the component
# installers instead of reusing their helpers).

# Fail fast if the caller does not satisfy the contract above. Called by the caller once,
# immediately before its command dispatch; never from this file at source time.
install_common_guard() {
    local want=1 v n
    if [[ ${OXP3_INSTALL_COMMON_API:-} != "$want" ]]; then
        die "install-common API mismatch: ${0##*/} declares OXP3_INSTALL_COMMON_API=${OXP3_INSTALL_COMMON_API:-<unset>}, this library is version $want (update the caller or the library)"
    fi
    for v in here CACHE OUT MOK_DIR ANATASE_REPO PATCHWORK_REPO FEDORA IMAGE CTR KMOD_NAME STAMP \
             SKIP_KERNEL SKIP_GAMESCOPE SKIP_MANGOHUD SKIP_FIRMWARE SKIP_DMC SKIP_MESA SKIP_XESS \
             XESS_COMMIT XESS_SRC_URL XESS_ZIP_URL XESS_ZIP_SHA XESS_DLLS XESS_PKG \
             FIRMWARE FW_NAME HHD_PATCHES VOLUME_HWDB FLATPAK_RT FLATPAK_MESA_LIST \
             FLATPAK_RT_FILES EXTRA_UNITS; do
        [[ -v $v ]] || die "install-common contract: ${0##*/} must set \$$v before using the shared helpers"
    done
    for v in KMODS FIRMWARE HHD_PATCHES EXTRA_UNITS; do
        eval "n=\${#$v[@]}"
        (( n > 0 )) || die "install-common contract: ${0##*/} must set a non-empty \$$v array"
    done
    declare -F build_mesa >/dev/null ||
        die "install-common contract: ${0##*/} must define build_mesa() (the caller-specific Mesa build)"
}

# ---------------------------------------------------------------------------------------------
# shell primitives
# ---------------------------------------------------------------------------------------------

say() { printf '\n== %s\n' "$*"; }

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The Anatase kernel in the current deployment (not necessarily the booted one).
anatase_kver() { rpm -q kernel-core --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' | head -1; }

# ---------------------------------------------------------------------------------------------
# build container: every build step runs inside podman, so the host needs no build toolchain
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
    # recreate the container if it was made with another MOK directory
    if podman container exists "$CTR" &&
       ! podman inspect "$CTR" --format '{{range .Mounts}}{{.Destination}} {{end}}' | grep -qF "$MOK_DIR"; then
        podman rm -f "$CTR" >/dev/null
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
    ctr rpmbuild --define "_topdir $top" --define "dist .fc$FEDORA.oxp3.$STAMP" -bb "$spec" > "$top/build.log" 2>&1 ||
        { tail -30 "$top/build.log"; die "$name x86_64 build failed (full log: $top/build.log)"; }
    say "$name $ver: i686"
    ctr bash -c "export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig; setarch i686 rpmbuild --target i686 --nodeps --define '_topdir $top' --define 'dist .fc$FEDORA.oxp3.$STAMP' -bb '$spec'" \
        > "$top/build-i686.log" 2>&1 ||
        { tail -30 "$top/build-i686.log"; die "$name i686 build failed (full log: $top/build-i686.log)"; }
    rm -f "$OUT/$name"-*.rpm
    find "$top/RPMS" -name "$name-$ver-*.rpm" ! -name '*debug*' -exec cp {} "$OUT/" \;
    ls "$OUT/$name"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# XeSS on XMX (xess-xmx-linux, our fork with Anatase support). The XESS_* pins are set by the
# caller; these helpers build the tree, install it under $XESS_PKG and report its state.
# ---------------------------------------------------------------------------------------------

build_xess() {
    local dl="$CACHE/xess" stage="$OUT/xess-xmx" zip src entry name sha
    mkdir -p "$dl"
    say "XeSS XMX: fork ${XESS_COMMIT:0:8} + release DLLs"
    zip="$dl/${XESS_ZIP_URL##*/}"
    if [[ $(sha256sum "$zip" 2>/dev/null | cut -d' ' -f1) != "$XESS_ZIP_SHA" ]]; then
        curl -sfL --retry 3 -o "$zip.part" "$XESS_ZIP_URL" || die "download failed: $XESS_ZIP_URL"
        [[ $(sha256sum "$zip.part" | cut -d' ' -f1) == "$XESS_ZIP_SHA" ]] || die "checksum mismatch: $XESS_ZIP_URL"
        mv "$zip.part" "$zip"
    fi
    src="$dl/xess-xmx-linux-$XESS_COMMIT.tar.gz"
    [[ -s $src ]] || curl -sfL --retry 3 -o "$src" "$XESS_SRC_URL" || die "download failed: $XESS_SRC_URL"
    rm -rf "$stage"; mkdir -p "$stage"
    tar -xzf "$src" --strip-components=1 -C "$stage"
    for entry in $XESS_DLLS; do
        name=${entry%%|*}; sha=${entry#*|}
        unzip -p "$zip" "*/$name" > "$stage/$name"
        [[ $(sha256sum "$stage/$name" | cut -d' ' -f1) == "$sha" ]] || die "$name in the release zip has an unexpected checksum"
    done
    echo "  $stage"
}

# Intel's compiler as get-igc.sh would fetch it (IGC 2.10.10, ocloc 25.18.1), from an earlier package
# download, instead of 230 MB again
seed_xess_compiler() {
    [[ -x $(ls "$XESS_PKG"/igc/neo/bin/ocloc* 2>/dev/null | head -1) ]] && return 0
    local d
    for d in "$HOME"/Downloads/xess-xmx-linux-*/igc; do
        [[ -x $d/neo/bin/ocloc-25.18.1 ]] && ls "$d"/igc/local/lib/libigc.so.2.10.10* >/dev/null 2>&1 || continue
        mkdir -p "$XESS_PKG/igc"; cp -a "$d"/. "$XESS_PKG/igc/"
        echo "  compiler bundle copied from $d"
        return 0
    done
}

install_xess() {
    [[ -d $OUT/xess-xmx ]] || die "missing $OUT/xess-xmx - run '$0 build' first"
    if ! grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so; then
        echo "  the installed Mesa has no CM kernel injection (built before patches/mesa 0101?) - XeSS XMX skipped"
        return 0
    fi
    mkdir -p "$XESS_PKG"
    # the package's own state (compiler, XeSS SDK, kernels, ICD file) stays
    rsync -a --delete --exclude /igc/ --exclude /xess/ --exclude /kernels/ --exclude /run/ --exclude /lib/ \
        "$OUT/xess-xmx/" "$XESS_PKG/"
    seed_xess_compiler
    bash "$XESS_PKG/install.sh" | sed 's/^/  /' || echo "  WARNING: xess-xmx-linux's install.sh failed"
    echo "  Restart Steam (leave and re-enter Gamemode) so games get the new environment."
}

uninstall_xess() {
    [[ -x $XESS_PKG/uninstall.sh ]] || return 0
    bash "$XESS_PKG/uninstall.sh" | sed 's/^/  /' || true
    echo "  $XESS_PKG (compiler, kernel cache) left in place; delete it to free about 300 MB"
}

status_xess() {
    local f; f=$(xmx_override_file)
    echo "  XeSS XMX: $(head -c 200 "${XDG_CACHE_HOME:-$HOME/.cache}/xess-xmx/driver-status" 2>/dev/null || echo 'no driver check yet')"
    grep -q '^ANV_CM_HELPER=' "$f" 2>/dev/null && echo "    Steam Flatpak override: $(grep -E '^(ANV_CM_HELPER|VKD3D_DISABLE_EXTENSIONS|force_vk_vendor)=' "$f" | tr '\n' ' ')"
    local p n=0 no=""
    for p in "$HOME/.local/share/Steam/steamapps/common/Proton"* "$HOME/.local/share/Steam/compatibilitytools.d/"*; do
        [[ -f $p/proton ]] || continue
        if grep -aq IGDEXT_ "$p/files/lib/wine/igdext/x86_64-windows/igdext64.dll" 2>/dev/null; then n=$((n + 1)); else no+=" ${p##*/};"; fi
    done
    echo "    Protons with the shim: $n${no:+; without:${no%;}}"
}

xmx_override_file() { echo "${XDG_DATA_HOME:-$HOME/.local/share}/flatpak/overrides/org.anatase.Steam"; }

# ---------------------------------------------------------------------------------------------
# kernel modules and firmware: both are built from pinned sources with the caller's container
# ---------------------------------------------------------------------------------------------

# Apply patches/kernel/*.patch in order. A patch Anatase already carries is skipped, one that
# neither applies nor is already there stops the build. Prints the number applied.
apply_kernel_patches() {
    local src=$1 p n=0 skipped=()
    for p in "$here"/patches/kernel/*.patch; do
        if git -C "$src" -c user.name=oxp3-fixes -c user.email=oxp3-fixes@localhost am -q -3 "$p" >/dev/null 2>&1; then
            # git am succeeds without a commit when the change is already there
            if [[ $(git -C "$src" log -1 --format=%s) == "$(sed -n 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//p' "$p" | head -1)"* ]]; then
                n=$((n + 1))
            else
                skipped+=( "$(basename "$p")" )
            fi
        else
            git -C "$src" am --abort >/dev/null 2>&1 || true
            if git -C "$src" apply --check -R "$p" >/dev/null 2>&1; then
                skipped+=( "$(basename "$p")" )
            else
                die "kernel patch $(basename "$p") doesn't apply to this kernel - it needs updating"
            fi
        fi
    done
    (( ${#skipped[@]} )) && printf '  already in Anatase, skipped: %s\n' "${skipped[@]}" >&2
    echo "$n"
}

build_kmods() {
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
        git ls-remote --exit-code --tags "$PATCHWORK_REPO" "refs/tags/$tag" >/dev/null ||
            die "no tag $tag in anatase-org/patchwork (or GitHub unreachable)"
        local try
        for try in 1 2 3; do
            git clone -q --depth 1 --branch "$tag" "$PATCHWORK_REPO" "$src" && break
            rm -rf "$src"
            (( try < 3 )) || die "cloning anatase-org/patchwork $tag failed 3 times (network?)"
            echo "clone failed, retrying ($try/3)"; sleep 10
        done
    fi
    git -C "$src" reset -q --hard "$tag" 2>/dev/null || git -C "$src" reset -q --hard HEAD
    git -C "$src" clean -qfdx
    local applied; applied=$(apply_kernel_patches "$src")
    if (( applied == 0 )); then
        echo "kernel $tag already contains every patch - no modules needed"
        rm -f "$OUT/$KMOD_NAME"-*.rpm; touch "$OUT/.kernel-not-needed"; return 0
    fi
    echo "  $applied kernel patches applied"
    rm -f "$OUT/.kernel-not-needed"
    # out-of-tree drivers Anatase adds as submodules: stub their Kconfig, our modules don't use them
    local k
    for k in $(sed -n 's/^source "\(drivers\/custom\/[^"]*\)"/\1/p' "$src/drivers/custom/Kconfig" 2>/dev/null); do
        [[ -e $src/$k ]] || { mkdir -p "$(dirname "$src/$k")"; : > "$src/$k"; }
    done

    say "kernel $kver: building modules (15-30 min)"
    rm -rf "$kb"; mkdir -p "$kb"; cp "$cfg" "$kb/.config"
    local entry dir mods m make_m=""
    for entry in "${KMODS[@]}"; do
        make_m+=" && make -s O='$kb' KERNELRELEASE='$kver' KBUILD_MODPOST_WARN=1 -j\$(nproc) M=${entry%%|*} modules"
    done
    # Anatase's symvers lets modpost resolve the kernel's exports, so a symbol our modules need
    # but the running kernel lacks shows up here instead of as a module that won't load.
    [[ -r /usr/lib/modules/$kver/symvers.xz ]] || die "no /usr/lib/modules/$kver/symvers.xz"
    xz -dc "/usr/lib/modules/$kver/symvers.xz" > "$kb/Module.symvers"
    ctr bash -c "cd '$src' && make -s O='$kb' KERNELRELEASE='$kver' olddefconfig &&
        make -s O='$kb' KERNELRELEASE='$kver' -j\$(nproc) modules_prepare $make_m" \
        > "$kb/build.log" 2>&1 || { tail -30 "$kb/build.log"; die "module build failed (full log: $kb/build.log)"; }
    local undef; undef=$(grep -E 'modpost: ".*" \[.*\] undefined!' "$kb/build.log" || true)
    [[ -z $undef ]] || { echo "$undef" | head -20; die "modules need kernel symbols $kver doesn't export"; }

    say "kernel $kver: signing with $MOK_DIR/MOK.der"
    hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"/\1/p' "$cfg"); hash=${hash:-sha512}
    local stage="$CACHE/kernel/stage-$tag" ko rel_path files=()
    rm -rf "$stage"; mkdir -p "$stage"
    for entry in "${KMODS[@]}"; do
        dir=${entry%%|*}; mods=${entry#*|}
        for m in $mods; do
            ko="$src/$dir/$m.ko"
            [[ -f $ko ]] || die "$dir/$m.ko not produced"
            [[ $(ctr modinfo -F vermagic "$ko") == "$kver "* ]] || die "$m.ko vermagic doesn't match $kver"
            rel_path="$dir/$m.ko"
            mkdir -p "$stage/$(dirname "$rel_path")"
            ctr strip --strip-debug -o "$stage/$rel_path" "$ko"
            ctr "$kb/scripts/sign-file" "$hash" "$MOK_DIR/MOK.key" "$MOK_DIR/MOK.der" "$stage/$rel_path"
            ctr xz -f --check=crc32 --lzma2=dict=1MiB "$stage/$rel_path"
            files+=( "$rel_path.xz" )
        done
    done
    echo "  signed by: $(ctr modinfo -F signer "$stage/drivers/gpu/drm/xe/xe.ko.xz")"

    say "kernel $kver: packaging $KMOD_NAME"
    local top="$CACHE/rpmbuild/$KMOD_NAME" rel f; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    tar -C "$stage" -cf "$top/SOURCES/modules.tar" "${files[@]}"
    {
        cat <<EOF
Name:           $KMOD_NAME
Version:        ${kver//-/_}
Release:        $rel
Summary:        xe, iwlwifi and btintel with the OneXPlayer 3 fixes
License:        GPL-2.0-only
Source0:        modules.tar
Requires:       kernel-core = ${kver%.*}
Obsoletes:      kmod-oxp3-xe < 999
%global debug_package %{nil}
%global __os_install_post %{nil}
%description
xe (VBT VS/PE-O backport, VRR/DSB timing, resume ENOMEM fix, 7.2.13 stable fixes, eDP HDR
switches), iwlwifi/iwlmld and btintel/btintel_pcie (7.2.13 stable and 7.3/7.4 fixes) for
kernel $kver. Signed with a local MOK. Installed in updates/, so they take priority over the
stock modules.
%prep
%setup -c -T
tar -xf %{SOURCE0}
%install
mkdir -p %{buildroot}/usr/lib/modules/$kver/updates
cp -a drivers %{buildroot}/usr/lib/modules/$kver/updates/
%files
EOF
        for f in "${files[@]}"; do echo "/usr/lib/modules/$kver/updates/$f"; done
        cat <<EOF
%post
/usr/sbin/depmod -a $kver || :
%postun
/usr/sbin/depmod -a $kver || :
EOF
    } > "$top/SPECS/$KMOD_NAME.spec"
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$KMOD_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "kmod packaging failed"; }
    rm -f "$OUT"/kmod-oxp3-*.rpm
    find "$top/RPMS" -name "$KMOD_NAME-*.rpm" -exec cp {} "$OUT/" \;
    ls "$OUT/$KMOD_NAME"-*.rpm
}

# revision of the first microcode header in a file; empty when the file is absent (callers test
# for that: build_firmware compares an installed revision only when sys_fw found the file)
ucode_rev() { [[ -f $1 ]] || return 0; od -An -t x4 -j 4 -N 4 "$1" 2>/dev/null | tr -d ' '; }

# installed (Anatase) copy of a firmware file, compressed or not
sys_fw() { local f; for f in /usr/lib/firmware/$1{,.xz,.zst}; do [[ -e $f ]] && { echo "$f"; return; }; done; }

sys_fw_sha() {
    local f; f=$(sys_fw "$1"); [[ -n $f ]] || return 0
    case $f in *.xz) xz -dc "$f" ;; *.zst) zstd -dcq "$f" ;; *) cat "$f" ;; esac | sha256sum | cut -d' ' -f1
}

build_firmware() {
    local stage="$CACHE/firmware/stage" entry path url sha kind f files=()
    rm -rf "$stage"; mkdir -p "$stage" "$CACHE/firmware/dl"
    say "firmware: pinned files"
    for entry in "${FIRMWARE[@]}"; do
        IFS='|' read -r path url sha kind <<<"$entry"
        (( SKIP_DMC )) && [[ $kind == dmc ]] && { echo "  $path: --stock-dmc, keeping Anatase's"; continue; }
        f="$CACHE/firmware/dl/$sha"
        if [[ $(sha256sum "$f" 2>/dev/null | cut -d' ' -f1) != "$sha" ]]; then
            curl -sfL --retry 3 -o "$f.part" "$url" || die "download failed: $url"
            [[ $(sha256sum "$f.part" | cut -d' ' -f1) == "$sha" ]] || die "checksum mismatch: $url"
            mv "$f.part" "$f"
        fi
        # skip what Anatase already ships in the same or a newer version
        if [[ $(sys_fw_sha "$path") == "$sha" ]]; then
            echo "  $path: Anatase ships the same file"; continue
        fi
        if [[ $kind == ucode ]]; then
            local have want
            have=$(ucode_rev "$(sys_fw "$path")"); want=$(ucode_rev "$f")
            if [[ -n $have ]] && (( 16#$have >= 16#$want )); then
                echo "  $path: Anatase ships revision 0x$have (>= 0x$want)"; continue
            fi
        fi
        install -D -m 0644 "$f" "$stage/$path"
        files+=( "$path" ); echo "  $path"
    done
    rm -f "$OUT/$FW_NAME"-*.rpm
    if (( ${#files[@]} == 0 )); then echo "  nothing newer than Anatase's firmware"; return 0; fi

    local top="$CACHE/rpmbuild/$FW_NAME" rel; rel=$(date +%Y%m%d%H%M)
    rm -rf "$top"; mkdir -p "$top/SOURCES" "$top/SPECS"
    tar -C "$stage" -cf "$top/SOURCES/firmware.tar" "${files[@]}"
    {
        cat <<EOF
Name:           $FW_NAME
Version:        1
Release:        $rel
Summary:        Pinned firmware for the OneXPlayer 3 (display DMC, Bluetooth, CPU microcode)
License:        Redistributable, no modification permitted
BuildArch:      noarch
Source0:        firmware.tar
%global debug_package %{nil}
%description
Firmware files that take priority over linux-firmware / microcode_ctl through
/usr/lib/firmware/updates: display DMC v2.20 (VRR cap with v2.36), Bluetooth 24.80,
CPU microcode 06-cc-02 0x11c.
%prep
%setup -c -T
tar -xf %{SOURCE0}
%install
mkdir -p %{buildroot}/usr/lib/firmware/updates
cp -a * %{buildroot}/usr/lib/firmware/updates/
%files
EOF
        for f in "${files[@]}"; do echo "/usr/lib/firmware/updates/$f"; done
    } > "$top/SPECS/$FW_NAME.spec"
    ctr rpmbuild --define "_topdir $top" -bb "$top/SPECS/$FW_NAME.spec" > "$top/build.log" 2>&1 ||
        { tail -20 "$top/build.log"; die "firmware packaging failed"; }
    find "$top/RPMS" -name "$FW_NAME-*.rpm" -exec cp {} "$OUT/" \;
    ls "$OUT/$FW_NAME"-*.rpm
}

# ---------------------------------------------------------------------------------------------
# build everything for the installed Anatase: the one caller hook is build_mesa()
# (RPM packaging in oxp3-fixes.sh, podman/meson in oxp3-fixes-fast.sh)
# ---------------------------------------------------------------------------------------------

do_build() {
    command -v podman >/dev/null || die "podman is required"
    ensure_container
    if (( !SKIP_GAMESCOPE || !SKIP_MANGOHUD || !SKIP_MESA )); then fetch_anatase; fi
    (( SKIP_GAMESCOPE )) || build_card gamescope '^Patch:[[:space:]]+overrides\.patch' "$here"/patches/gamescope/*.patch
    (( SKIP_MANGOHUD )) || build_card mangohud '^Patch1000:[[:space:]]+overrides\.patch' "$here"/patches/mangohud/*.patch
    (( SKIP_MESA )) || build_mesa
    (( SKIP_XESS )) || build_xess
    (( SKIP_KERNEL )) || build_kmods
    (( SKIP_FIRMWARE )) || build_firmware
    say "build done: $OUT"
    ls "$OUT"
}

# ---------------------------------------------------------------------------------------------
# module signing (MOK) and the root-owned RAPL power service
# ---------------------------------------------------------------------------------------------

mok_cn() {
    openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -subject -nameopt multiline |
        sed -n 's/^ *commonName *= *//p'
}

# Anatase's kernel only loads MOK keys into the machine keyring (the one that verifies
# modules) if they are CA certs with keyCertSign and without digitalSignature
# (CONFIG_INTEGRITY_CA_MACHINE_KEYRING_MAX); anything else lands in the platform keyring,
# which can verify kernel images but not modules.
mok_usage_ok() {
    local ext
    ext=$(openssl x509 -in "$MOK_DIR/MOK.der" -inform der -noout -ext basicConstraints,keyUsage 2>/dev/null)
    grep -q 'CA:TRUE' <<<"$ext" && grep -q 'Certificate Sign' <<<"$ext" && ! grep -q 'Digital Signature' <<<"$ext"
}

# Is the MOK in the kernel's .machine keyring right now? keyctl can't find that keyring by
# name from a normal process, so look its id up in /proc/keys and list it by id.
mok_trusted() {
    local cn kr; cn=$(mok_cn)
    kr=$(sudo awk '$8 == "keyring" && $9 == ".machine:" { print $1; exit }' /proc/keys)
    [[ -n $cn && -n $kr ]] && sudo keyctl list "0x$kr" 2>/dev/null | grep -qF "$cn"
}

# Create a module signing key the machine keyring accepts and queue it for enrolment.
do_mok_setup() {
    if [[ -e $MOK_DIR/MOK.key ]] && mok_usage_ok; then
        echo "$MOK_DIR already has a suitable key."
    else
        if [[ -e $MOK_DIR/MOK.key ]]; then
            local old; old="$MOK_DIR.old-$(date +%Y%m%d%H%M%S)"
            echo "The key in $MOK_DIR isn't accepted for modules (needs CA + keyCertSign only); keeping it as $old"
            mv "$MOK_DIR" "$old"
        fi
        install -d -m 700 "$MOK_DIR"
        local cfg; cfg=$(mktemp)
        printf '%s\n' '[req]' 'distinguished_name = dn' 'x509_extensions = mok' 'prompt = no' \
            '[dn]' "CN = OXP3 module signing CA ($(id -un))" \
            '[mok]' 'basicConstraints = critical, CA:TRUE' 'keyUsage = critical, keyCertSign' \
            'subjectKeyIdentifier = hash' > "$cfg"
        (umask 077; openssl req -new -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 -config "$cfg" \
            -keyout "$MOK_DIR/MOK.key" -out "$MOK_DIR/MOK.crt" 2>/dev/null)
        rm -f "$cfg"
        openssl x509 -in "$MOK_DIR/MOK.crt" -outform DER -out "$MOK_DIR/MOK.der"
        echo "Created $MOK_DIR/MOK.key (keep it private) and MOK.der."
    fi
    if mokutil --test-key "$MOK_DIR/MOK.der" 2>&1 | grep -q 'already enrolled'; then
        echo "MOK already enrolled."
    else
        echo "Enrolling: choose a one-time password, then reboot and in MokManager pick"
        echo "Enroll MOK -> Continue -> Yes -> enter the password."
        sudo mokutil --import "$MOK_DIR/MOK.der"
    fi
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

# ---------------------------------------------------------------------------------------------
# volume rocker: hhd patches + atkbd force-release hwdb (both live in the /usr overlay)
# ---------------------------------------------------------------------------------------------

hhd_site() { python3 -c 'import hhd, os; print(os.path.dirname(os.path.dirname(hhd.__file__)))' 2>/dev/null; }

install_volume_fix() {
    local site p changed=0; site=$(hhd_site)
    [[ -n $site ]] || { echo "  hhd not found, volume rocker fix skipped"; return 0; }
    # each patch: already there, or applied now; one that does neither leaves the rocker stock
    for p in "${HHD_PATCHES[@]}"; do
        if git -C "$site" apply --check -R -p1 "$p" >/dev/null 2>&1; then
            continue
        elif git -C "$site" apply --check -p1 "$p" >/dev/null 2>&1; then
            sudo git -C "$site" apply -p1 "$p"; changed=1
        else
            echo "  WARNING: ${p##*/} doesn't apply to this hhd - volume rocker left stock"
            hhd_volume_unpatch "$site"
            [[ -e $VOLUME_HWDB ]] && { sudo rm -f "$VOLUME_HWDB"; sudo systemd-hwdb update; }
            sudo systemctl restart hhd.service
            return 0
        fi
    done
    sudo find "$site/hhd/device/oxp" -name '__pycache__' -prune -exec rm -rf {} +
    echo "  hhd: volume rocker presses become taps$( ((changed)) || echo ' (already patched)')"
    if ! cmp -s "$here/files/84-oxp3-volume-keys.hwdb" "$VOLUME_HWDB"; then
        sudo install -m 0644 "$here/files/84-oxp3-volume-keys.hwdb" "$VOLUME_HWDB"
        sudo systemd-hwdb update
        sudo udevadm trigger --subsystem-match=input --action=change
    fi
    sudo systemctl restart hhd.service
}

# take our hhd patches out again, newest first (only those that are fully applied)
hhd_volume_unpatch() { # <site>
    local i p
    for (( i=${#HHD_PATCHES[@]} - 1; i >= 0; i-- )); do
        p=${HHD_PATCHES[$i]}
        git -C "$1" apply --check -R -p1 "$p" >/dev/null 2>&1 && sudo git -C "$1" apply -R -p1 "$p"
    done
    sudo find "$1/hhd/device/oxp" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
}

# ---------------------------------------------------------------------------------------------
# Mesa runtime state and the Steam Flatpak runtime overrides
# ---------------------------------------------------------------------------------------------

# Mesa version of the host (from its libgallium-<version>.so) and whether the Steam Flatpak runtime
# runs the same Intel Vulkan driver
host_mesa() { local g; g=$(ls /usr/lib64/libgallium-*.so 2>/dev/null | head -1); g=${g##*/libgallium-}; echo "${g%.so}"; }

flatpak_mesa() {
    if cmp -s /usr/lib64/libvulkan_intel.so "$FLATPAK_RT_FILES/lib64/libvulkan_intel.so"; then
        echo "same as the host"
    else
        echo "Anatase's (not ours)"
    fi
}

refresh_flatpak_runtime() {
    if [[ -n $(flatpak ps --columns=application 2>/dev/null) ]]; then
        echo "  Flatpak apps are running (Steam?): the Steam runtime picks this up after a reboot,"
        echo "  or close them and run: sudo systemctl restart anatase-flatpak-extensions.service"
        return 0
    fi
    sudo systemctl restart anatase-flatpak-extensions.service
    echo "  Steam Flatpak runtime Mesa: $(flatpak_mesa)"
}

remove_flatpak_mesa() {
    [[ -r $FLATPAK_MESA_LIST ]] || return 0
    (cd "$FLATPAK_RT/overrides" && grep -v '/$' "$FLATPAK_MESA_LIST" | sudo xargs -r -d '\n' rm -f --)
    sudo rm -f "$FLATPAK_MESA_LIST"
}

# ---------------------------------------------------------------------------------------------
# deployment state and the read-only status report
# ---------------------------------------------------------------------------------------------

# State of the deployments: is one staged, and which layered local packages does the booted
# one have?
deploy_info() {
    rpm-ostree status --json | python3 -c 'import json,sys
ds=json.load(sys.stdin)["deployments"]
b=[x for x in ds if x.get("booted")][0]
print("staged" if any(x.get("staged") for x in ds) else "none")
print(" ".join(b.get("requested-local-packages",[])))'
}

hotfix_active() { rpm-ostree status --booted | grep -q 'Unlocked: hotfix'; }

do_status() {
    echo "kernel (deployment): $(anatase_kver)   booted: $(uname -r)"
    rpm -q gamescope mangohud mesa-vulkan-drivers kmod-oxp3-xe "$KMOD_NAME" "$FW_NAME" 2>&1 | grep -v 'is not installed' | sed 's/^/  /' || true
    rpm-ostree status --booted | grep -E 'Version|LocalPackages|Initramfs|Unlocked' | sed 's/^/  /' || true
    echo "  rapl-power service: $(systemctl is-active rapl-power.service 2>/dev/null)"
    local f; for f in /run/rapl-power/*; do [[ -e $f ]] && echo "    $(basename "$f"): $(cat "$f") W"; done
    ls /etc/udev/rules.d/70-*rapl.rules >/dev/null 2>&1 && echo "  WARNING: old RAPL udev rule still present (run install)"
    stat -c '  RAPL counters: %A %n' /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null
    echo "  Mesa: host $(host_mesa)$(grep -aq ANV_CM_HELPER /usr/lib64/libvulkan_intel.so && echo ' with the XeSS XMX patches'), Steam Flatpak: $(flatpak_mesa)"
    local m; for m in xe iwlwifi iwlmld btintel btintel_pcie; do
        printf '  %-13s %s\n' "$m:" "$(modinfo -F filename "$m" 2>/dev/null)"
    done
    journalctl -k -b 0 --no-pager 2>/dev/null | sed -n \
        -e 's/.*\(Finished loading DMC firmware .*\)/  \1/p' \
        -e 's/.*microcode: \(Current revision: .*\)/  microcode \1/p' \
        -e 's/.*Bluetooth: hci0: \(Firmware timestamp .*\)/  Bluetooth \1/p' | sort -u
    local u st; for u in "${EXTRA_UNITS[@]}" "oxp3-audio-resume@$(id -un).service" user:oxp3-audio-route.service; do
        if [[ $u == user:* ]]; then st=$(systemctl --user is-enabled "${u#user:}" 2>/dev/null || true)
        else st=$(systemctl is-enabled "$u" 2>/dev/null || true); fi
        printf '  %-34s %s\n' "${u#user:}:" "${st:-missing}"
    done
    echo "  package PL1: MSR $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 )) W," \
         "MMIO $(( $(cat /sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 )) W"
    local site; site=$(hhd_site)
    local hp n=0; for hp in "${HHD_PATCHES[@]}"; do
        [[ -n $site ]] && git -C "$site" apply --check -R -p1 "$hp" >/dev/null 2>&1 && n=$((n + 1))
    done
    echo "  volume rocker: $n/${#HHD_PATCHES[@]} hhd patches applied$( [[ -e $VOLUME_HWDB ]] && echo ', force-release on')"
    if [[ -x $here/tools/oxp3-hw-install.sh ]]; then
        "$here/tools/oxp3-hw-install.sh" --status 2>/dev/null | tail -n +2 | sed 's/^/  /' || true
    fi
    status_xess
    [[ -d $OUT ]] && { echo "built packages in $OUT:"; ls "$OUT" | sed 's/^/  /'; }
}
