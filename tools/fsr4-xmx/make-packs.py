#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""make-packs.py - index the kernel packs an FSR 4 XMX upscaler DLL unpacks from data.bin.

The OptiScaler "FSR 4 and DLSS on Intel XMX" build of amd_fidelityfx_upscaler_dx12.dll carries
its XMX kernels as Windows Compression API (LZMS) packs and unpacks them with cabinet.dll's
Decompress(). Wine has no LZMS. The bundle therefore patches the DLL to import fsr4dec.dll
instead, which returns the unpacked bytes from data.bin. Its 64 KB chunk-hash lookup misses the
first pack (35 MB, "Decompress MISS csize=6107632"), so the Xe2/Xe3 XMX variants never load.

data.bin itself is complete: it is the six unpacked packs back to back, and each slice matches the
SHA-256 the DLL checks. This writes fsr4dec-packs.txt for our fsr4dec.dll
(tools/fsr4-xmx/fsr4dec.c). Each line is "<compressed size> <unpacked size> <offset in data.bin>".
A pack is listed only when its data.bin slice hashes to a SHA-256 stored in the DLL, so a data.bin
from another build is refused here instead of failing in the game.

    make-packs.py <amd_fidelityfx_upscaler_dx12.dll> <data.bin> <out fsr4dec-packs.txt>
    make-packs.py --key <amd_fidelityfx_upscaler_dx12.dll>   # the build's pack layout, names a cached data.bin
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys

MAGIC = bytes.fromhex("0a51e5c0")   # Compression API buffer header


def packs(dll: bytes) -> list[tuple[int, int]]:
    """(compressed size, unpacked size) of every Compression API buffer in the DLL, in order."""
    offs = [m.start() for m in re.finditer(re.escape(MAGIC), dll)]
    out = []
    for i, off in enumerate(offs):
        raw = struct.unpack_from("<Q", dll, off + 8)[0]
        end = offs[i + 1] if i + 1 < len(offs) else None
        if end is None:
            # the last pack runs to the end of its section; its compressed size is only needed for
            # the log, so take what is left of the file
            end = len(dll)
        out.append((end - off, raw))
    return out


def key(dll: bytes) -> str:
    """Names the data.bin a build needs: its pack headers (sizes and flags of every pack). The Cabinet ->
    fsr4dec import patch does not change it, so the original and the patched DLL share one data.bin."""
    h = hashlib.sha256()
    for m in re.finditer(re.escape(MAGIC), dll):
        h.update(dll[m.start():m.start() + 24])
    return h.hexdigest()[:16]


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--key":
        found = packs(open(sys.argv[2], "rb").read())
        if not found:
            sys.exit("no Compression API packs in this DLL - not an XMX build?")
        print(key(open(sys.argv[2], "rb").read()))
        return 0
    if len(sys.argv) != 4:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    dll = open(sys.argv[1], "rb").read()
    data = open(sys.argv[2], "rb").read()
    found = packs(dll)
    if not found:
        sys.exit("no Compression API packs in this DLL - not an XMX build?")
    total = sum(raw for _, raw in found)
    if total != len(data):
        sys.exit(f"data.bin is {len(data)} bytes but the DLL's packs unpack to {total}: another build's data.bin")
    lines, off = [], 0
    for i, (csize, raw) in enumerate(found):
        digest = hashlib.sha256(data[off:off + raw]).hexdigest()
        if digest.encode() not in dll and bytes.fromhex(digest) not in dll:
            sys.exit(f"pack {i}: data.bin slice at {off} ({raw} bytes) does not match any SHA-256 in the DLL")
        lines.append(f"{csize} {raw} {off}")
        print(f"pack {i}: {csize} -> {raw} bytes at {off}, SHA-256 {digest[:16]}... verified")
        off += raw
    with open(sys.argv[3], "w") as f:
        f.write("# fsr4dec-packs.txt: <compressed> <unpacked> <offset in data.bin> (tools/fsr4-xmx/make-packs.py)\n")
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
