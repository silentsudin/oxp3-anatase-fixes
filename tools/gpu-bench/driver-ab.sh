#!/bin/bash
# SPDX-License-Identifier: MIT
# driver-ab.sh - A/B a Mesa build from a freshly built RPM against the installed driver, with no
# root and no install: extract the RPMs into a side tree, point the Vulkan loader at it with a
# private ICD, and run the GravityMark harness interleaved (A B B A) so drift cannot be mistaken
# for a gain.
#
# Why the ordering and the unset: a first version of this measurement exported VK_DRIVER_FILES once
# and ran both variants in one shell, so the "baseline" repeat still pointed at the patched driver
# and the two results looked identical (+0.3%). Interleaved runs in separate scopes, with the
# environment explicitly unset for the installed variant, showed the real +12.7%.
#
#   tools/gpu-bench/driver-ab.sh --rpm-glob 'mesa-vulkan-drivers-*.x86_64.rpm' --workloads rt
#   tools/gpu-bench/driver-ab.sh --tag-prefix rt --rpm-glob "$OUT"/mesa-vulkan-drivers-26.3.1-*.x86_64.rpm
set -uo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
rpm_glob=""; workloads="rt"; tag_prefix="ab"; repeats=2
while (( $# )); do
    case $1 in
        --rpm-glob) rpm_glob=${2:-}; shift 2 ;;
        --workloads) workloads=${2:-}; shift 2 ;;   # rt | raster | both
        --tag-prefix) tag_prefix=${2:-}; shift 2 ;;
        --repeats) repeats=${2:-}; shift 2 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n $rpm_glob ]] || { echo "usage: $0 --rpm-glob '<glob of built mesa RPMS>'" >&2; exit 2; }

tree=/tmp/oxp3-driver-ab
rm -rf "$tree"; mkdir -p "$tree"
n=0
for r in $rpm_glob; do
    [ -r "$r" ] || continue
    ( cd "$tree" && rpm2cpio "$r" | cpio -idm --quiet ) 2>/dev/null && n=$((n + 1))
done
(( n )) || { echo "no RPMs matched: $rpm_glob" >&2; exit 1; }
[[ -r $tree/usr/lib64/libvulkan_intel.so ]] || { echo "the RPMs carry no libvulkan_intel.so" >&2; exit 1; }
printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s/usr/lib64/libvulkan_intel.so","api_version":"1.4.0"}}\n' \
    "$tree" > "$tree/icd.json"
echo "side driver: $tree/usr/lib64/libvulkan_intel.so"
printf '  sha256 %s\n' "$(sha256sum "$tree/usr/lib64/libvulkan_intel.so" | cut -c1-16)"
printf '  installed %s\n' "$(sha256sum /usr/lib64/libvulkan_intel.so 2>/dev/null | cut -c1-16)"

flags=()
case $workloads in
    rt) flags=(--gravitymark-rt) ;;
    raster) flags=(--gravitymark) ;;
    both|*) flags=(--gravitymark --gravitymark-rt) ;;
esac

bench() { # <tag> <installed|side>
    local tag=$1 which=$2
    if [[ $which == side ]]; then
        VK_DRIVER_FILES="$tree/icd.json" LD_LIBRARY_PATH="$tree/usr/lib64" \
            python3 "$here/tools/gpu-bench/gpu-bench.py" --quick "${flags[@]}" --skip-vkmark --tag "$tag" \
            >"/tmp/driver-ab-$tag.log" 2>&1
    else
        env -u VK_DRIVER_FILES -u LD_LIBRARY_PATH \
            python3 "$here/tools/gpu-bench/gpu-bench.py" --quick "${flags[@]}" --skip-vkmark --tag "$tag" \
            >"/tmp/driver-ab-$tag.log" 2>&1
    fi
    echo "  $tag done"
}

# warm both variants first: the first run after a driver change rebuilds the shader cache
bench "$tag_prefix-warm-installed" installed
bench "$tag_prefix-warm-side" side
for i in $(seq 1 "$repeats"); do
    bench "$tag_prefix-A$i-installed" installed
    bench "$tag_prefix-B$i-side" side
done
bench "$tag_prefix-A-end-installed" installed

python3 - "$tag_prefix" "$workloads" <<'PY'
import glob, json, os, sys
prefix, workloads = sys.argv[1], sys.argv[2]
names = ["gravitymark-rt.json"] if workloads == "rt" else (
    ["gravitymark.json"] if workloads == "raster" else ["gravitymark.json", "gravitymark-rt.json"])
def gather(kind):
    out = []
    for d in glob.glob(os.path.expanduser(f"~/.cache/oxp3-fixes/gpu-bench/results/*-{prefix}-{kind}")):
        for name in names:
            f = os.path.join(d, name)
            if os.path.exists(f):
                j = json.load(open(f))
                out.append((name.split(".")[0], j.get("fps")))
    return out
a = gather("A1-installed") + gather("A-end-installed")
b = [x for i in (1, 2, 3) for x in gather(f"B{i}-side")]
for label, data in (("installed", a), ("side (built)", b)):
    for name in sorted({n for n, _ in data}):
        vals = [v for n, v in data if n == name and v]
        if vals:
            print(f"  {label:14s} {name:16s} {sum(vals)/len(vals):7.2f} fps  (n={len(vals)}, {min(vals):.1f}-{max(vals):.1f})")
for name in sorted({n for n, _ in a}):
    av = [v for n, v in a if n == name and v]
    bv = [v for n, v in b if n == name and v]
    if av and bv:
        am, bm = sum(av)/len(av), sum(bv)/len(bv)
        print(f"  {name:16s} side vs installed: {100*(bm-am)/am:+.2f}%")
PY
