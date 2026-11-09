#!/usr/bin/env bash
# test-in-container.sh - run the isolated Harman-EQ validation in a container.
#
# Use this when the host lacks part of the toolchain (on Anatase: sox and
# numpy are missing, so the repo defaults to the stdlib FFT path) or when a
# future session wants a reproducible environment.  The container has no
# /dev/snd, so the private WirePlumber cannot touch the real card.
#
#   tools/audio/test-in-container.sh                # build if needed, run full
#   tools/audio/test-in-container.sh --quick        # short sweep, no hot tones
#   tools/audio/test-in-container.sh --rebuild      # force an image rebuild
#   tools/audio/test-in-container.sh --keep-runtime # keep the tree in the container (debug)
#
# Exit: 0 pass, 1 fail, 2 usage/setup, 77 skip (podman missing).
set -u

PROG=oxp3-audio-container
IMAGE=${OXP3_AUDIO_TEST_IMAGE:-localhost/oxp3-audio-test:f45}
REBUILD=0
ARGS=()

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case $1 in
        --rebuild) REBUILD=1 ;;
        --keep-runtime) ARGS+=(--keep-runtime) ;;
        --quick) ARGS+=(--quick) ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

here=$(cd "$(dirname "$0")" && pwd) || exit 2
repo=$(cd "$here/../.." && pwd) || exit 2
containerfile=$repo/container/audio-test.Containerfile
[ -f "$containerfile" ] || { echo "$PROG: missing $containerfile" >&2; exit 2; }

command -v podman >/dev/null 2>&1 || { echo "SKIP: podman is not installed"; exit 77; }

if [ "$REBUILD" -eq 1 ] || ! podman image exists "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE (dnf install inside the image; needs network once)"
    podman build -t "$IMAGE" -f "$containerfile" "$repo/container" || exit 2
fi

# Keep the JSON on the host so the owner can diff runs.
outdir=$(mktemp -d "${TMPDIR:-/tmp}/audiotest-container.XXXXXX") || exit 2

echo "running: podman run --rm $IMAGE tools/audio/test-isolation.sh ${ARGS[*]}"
set +e
podman run --rm \
    --security-opt label=disable \
    -v "$repo:/w:z" \
    -v "$outdir:/out:z" \
    -w /w \
    "$IMAGE" \
    tools/audio/test-isolation.sh --json /out/result.json "${ARGS[@]}"
rc=$?
set -e
if [ -f "$outdir/result.json" ]; then
    echo "result JSON: $outdir/result.json"
else
    rm -rf -- "$outdir"
fi
exit "$rc"
