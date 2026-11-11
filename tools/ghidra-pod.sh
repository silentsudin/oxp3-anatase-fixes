#!/bin/bash
# SPDX-License-Identifier: MIT
# ghidra-pod.sh <agent-name> <binary> [symbol-regex]
# Isolated headless Ghidra per agent: private project dir + decompilation text dump.
# Never touches the shared Ghidra bridge; safe to run many in parallel.
set -eu
name=${1:?usage: $0 <agent-name> <binary> [regex]}
bin=${2:?usage: $0 <agent-name> <binary> [regex]}
pat=${3:-}
repo=$(cd "$(dirname "$0")/.." && pwd)
work=${GHIDRA_AGENT_DIR:-$HOME/.cache/ghidra-agents/$name}
mkdir -p "$work/out" "$work/proj" "$work/scripts"
cp -f "$repo"/tools/ghidra-scripts/*.java "$work/scripts"/ 2>/dev/null || true
cp -f "$bin" "$work/input.bin"
base=$(basename "$bin" | tr -c 'A-Za-z0-9._-' '_')
exec podman run --rm --security-opt label=disable \
  -v "$work":/work:z \
  docker.io/blacktop/ghidra:latest \
  /ghidra/support/analyzeHeadless /work/proj "$base" \
  -import /work/input.bin -scriptPath /work/scripts \
  -postScript DumpAll.java "/work/out/decomp.txt" "$pat" \
  -deleteProject 2>&1 | grep -E "WROTE|ERROR|Exception" | tail -5
echo "dump: $work/out/decomp.txt"
