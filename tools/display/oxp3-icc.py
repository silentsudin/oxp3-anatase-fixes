#!/usr/bin/env python3
"""oxp3-icc - describe this panel to KWin colour management as an ICC v2 profile.

Why this exists
---------------
The eDP panel is a wide-gamut part (``oxp3-edid.py``: primaries ~1.5x sRGB
area, D65 white, gamma 2.2) but Plasma has no idea: ``kscreen-doctor -o``
reports ``Color profile source: sRGB`` and ``ICC profile: none``, i.e. KWin
assumes the panel *is* sRGB.  Every sRGB-tagged app is then shown with the
panel's native primaries untouched, which is exactly the "oversaturated
desktop" problem the vendor's Windows saturation slider papers over.

KWin 6.7 can apply a display profile per output
(``output.<name>.iccprofile`` + ``colorProfileSource.ICC``).  Given a profile
that describes the panel, KWin converts sRGB content into the panel's real
gamut instead of showing raw primaries.  This tool generates that profile from
the panel's own EDID chromaticities: an ICC v2 matrix/TRC RGB display profile
(D50 PCS, Bradford-adapted, gamma 2.2 TRC), which is what every colour-managed
consumer expects.

This is a *description* of the panel computed from its EDID, not a
measurement: primaries come from the EDID chromaticity bytes, and the TRC is
the EDID gamma.  If you ever colour-meter the panel, ship the measured profile
instead; the plumbing in tools/display/display-mode.sh is the same.

Usage
-----
    tools/display/oxp3-icc.py --out ~/.local/share/color/icc/oxp3-panel.icc
    tools/display/oxp3-icc.py --check ~/.local/share/color/icc/oxp3-panel.icc
    tools/display/oxp3-icc.py --from-edid /path/edid.bin --description "OXP3 panel"

Exit codes: 0 = ok, 1 = input/validation error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import struct
import sys

# sRGB primaries / D65 white, used for the reference column and for --srgb.
SRGB_PRIMARIES = {
    "red": (0.640, 0.330),
    "green": (0.300, 0.600),
    "blue": (0.150, 0.060),
    "white": (0.3127, 0.3290),
}
D50 = (0.9642, 1.0000, 0.8249)
D65 = (0.95047, 1.00000, 1.08883)


# ---------------------------------------------------------------- colour math


def xy_to_xyz(x: float, y: float) -> list:
    if y == 0:
        raise ValueError("chromaticity y must be non-zero")
    return [x / y, 1.0, (1.0 - x - y) / y]


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_vec(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def mat_inv(m):
    det = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    if abs(det) < 1e-12:
        raise ValueError("singular matrix")
    inv = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            a = [[m[r][c] for c in range(3) if c != j] for r in range(3) if r != i]
            minor = a[0][0] * a[1][1] - a[0][1] * a[1][0]
            sign = -1.0 if (i + j) % 2 else 1.0
            inv[j][i] = sign * minor / det
    return inv


def bradford(src_white, dst_white):
    """Bradford chromatic adaptation matrix between two XYZ whites."""
    m = [
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296],
    ]
    src = mat_vec(m, src_white)
    dst = mat_vec(m, dst_white)
    s = [dst[i] / src[i] for i in range(3)]
    scale = [[s[i] if i == j else 0.0 for j in range(3)] for i in range(3)]
    return mat_mul(mat_inv(m), mat_mul(scale, m))


def rgb_to_xyz(primaries: dict) -> list:
    """RGB->XYZ matrix for the given primaries, normalised to white (Y=1)."""
    cols = [xy_to_xyz(*primaries[c]) for c in ("red", "green", "blue")]
    p = [[cols[j][i] for j in range(3)] for i in range(3)]
    w = xy_to_xyz(*primaries["white"])
    s = mat_vec(mat_inv(p), w)
    return [[p[i][j] * s[j] for j in range(3)] for i in range(3)]


# ------------------------------------------------------------------- ICC writer


def _tag_data_desc(text: str) -> bytes:
    ascii_text = text.encode("ascii", "replace") + b"\x00"
    return (
        b"desc"
        + b"\x00" * 4
        + struct.pack(">I", len(ascii_text))
        + ascii_text
        + b"\x00" * 4
        + b"\x00" * 4  # unicode language code
        + b"\x00" * 4  # unicode count
        + b"\x00" * 67  # unicode string
        + b"\x00" * 3  # script code
        + b"\x00"  # mac count
        + b"\x00" * 67  # mac string
    )


def _tag_text(text: str) -> bytes:
    return b"text" + b"\x00" * 4 + text.encode("ascii", "replace") + b"\x00"


def _tag_xyz(x: float, y: float, z: float) -> bytes:
    def s15(v):
        # s15Fixed16Number: 1.0 == 0x00010000
        return int(round(v * 65536.0))

    return b"XYZ " + b"\x00" * 4 + struct.pack(">iii", s15(x), s15(y), s15(z))


def _tag_curv(gamma: float) -> bytes:
    # 'curv' with a single u8Fixed8 gamma value
    return b"curv" + b"\x00" * 4 + struct.pack(">IH", 1, int(round(gamma * 256.0)))


def build_profile(
    primaries: dict,
    gamma: float,
    description: str,
    copyright_text: str,
    white: tuple = D50,
) -> bytes:
    """Build an ICC v2 display profile (matrix/TRC) as bytes."""
    m_d65 = rgb_to_xyz(primaries)
    adapt = bradford(D65, list(white))
    m_d50 = mat_mul(adapt, m_d65)

    # sanity: the primaries matrix must be white-normalised (row sums == white)
    row_sums = [sum(m_d65[i][j] for j in range(3)) for i in range(3)]
    want = xy_to_xyz(*primaries["white"])
    if any(abs(row_sums[i] - want[i]) > 5e-3 for i in range(3)):
        raise ValueError(f"primaries matrix is not white-normalised: {row_sums} vs {want}")

    tags = [
        ("desc", _tag_data_desc(description)),
        ("cprt", _tag_text(copyright_text)),
        ("wtpt", _tag_xyz(*white)),
        ("rXYZ", _tag_xyz(*[m_d50[i][0] for i in range(3)])),
        ("gXYZ", _tag_xyz(*[m_d50[i][1] for i in range(3)])),
        ("bXYZ", _tag_xyz(*[m_d50[i][2] for i in range(3)])),
        ("rTRC", _tag_curv(gamma)),
        ("gTRC", _tag_curv(gamma)),
        ("bTRC", _tag_curv(gamma)),
    ]

    header = bytearray(128)
    tag_table_size = 4 + 12 * len(tags)
    offset = 128 + tag_table_size
    offsets = []
    body = bytearray()
    for name, data in tags:
        while (offset + len(body)) % 4:
            body += b"\x00"
        offsets.append((name, offset + len(body), len(data)))
        body += data

    total = 128 + tag_table_size + len(body)
    header[0:4] = struct.pack(">I", total)
    header[4:8] = b"none"  # preferred CMM
    header[8:12] = struct.pack(">I", 0x02100000)  # ICC v2.1
    header[12:16] = b"mntr"  # display device class
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    now = datetime.datetime.now()
    header[24:36] = struct.pack(
        ">6H", now.year, now.month, now.day, now.hour, now.minute, now.second
    )
    header[36:40] = b"acsp"
    header[40:44] = b"    "  # platform
    header[44:48] = struct.pack(">I", 0)  # flags: not embedded, independent
    header[48:52] = struct.pack(">I", 0)  # manufacturer
    header[52:56] = struct.pack(">I", 0)  # model
    header[56:64] = struct.pack(">Q", 0)  # attributes
    header[64:68] = struct.pack(">I", 0)  # rendering intent: perceptual
    header[68:80] = struct.pack(">iii", *[int(round(v * 65536)) for v in white])
    header[80:84] = b"oxp3"
    # bytes 84..99 reserved (0), 100..127 profile ID (0 for v2)
    header[128 - 4:128] = b"\x00" * 4

    # v2 requires the profile ID field to be zero; header[84:100] reserved.
    out = bytearray(header)
    out += struct.pack(">I", len(offsets))
    for name, off, size in sorted(offsets):
        out += name.encode("ascii") + struct.pack(">II", off, size)
    out += body
    return bytes(out)


# -------------------------------------------------------------------- reading


def read_profile(path: str) -> dict:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 132 or data[36:40] != b"acsp":
        raise ValueError("not an ICC profile")
    size = struct.unpack(">I", data[0:4])[0]
    if size != len(data):
        raise ValueError(f"declared size {size} != file size {len(data)}")
    count = struct.unpack(">I", data[128:132])[0]
    tags = {}
    for i in range(count):
        e = 132 + 12 * i
        name = data[e:e + 4].decode("ascii", "replace")
        off, tsize = struct.unpack(">II", data[e + 4:e + 12])
        tags[name] = data[off:off + tsize]
    return {
        "size": size,
        "class": data[12:16].decode("ascii"),
        "space": data[16:20].decode("ascii"),
        "pcs": data[20:24].decode("ascii"),
        "version": f"{data[8]}.{data[9] >> 4}",
        "tags": sorted(tags),
        "red": _read_xyz(tags.get("rXYZ", b"")),
        "green": _read_xyz(tags.get("gXYZ", b"")),
        "blue": _read_xyz(tags.get("bXYZ", b"")),
        "white": _read_xyz(tags.get("wtpt", b"")),
        "gamma": _read_gamma(tags.get("rTRC", b"")),
        "description": _read_desc(tags.get("desc", b"")),
    }


def _read_xyz(tag: bytes):
    if len(tag) < 20 or tag[:4] != b"XYZ ":
        return None
    x, y, z = struct.unpack(">iii", tag[8:20])
    return [round(v / 65536.0, 5) for v in (x, y, z)]


def _read_gamma(tag: bytes):
    if len(tag) < 14 or tag[:4] != b"curv":
        return None
    n = struct.unpack(">I", tag[8:12])[0]
    if n == 1:
        return round(struct.unpack(">H", tag[12:14])[0] / 256.0, 3)
    return f"table({n})"


def _read_desc(tag: bytes):
    if len(tag) < 12:
        return None
    n = struct.unpack(">I", tag[8:12])[0]
    return tag[12:12 + n].split(b"\x00")[0].decode("ascii", "replace")


def panel_primaries(edid_path: str) -> tuple:
    """Read primaries+gamma from an EDID using the sibling decoder."""
    import importlib.util
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("oxp3_edid", os.path.join(here, "oxp3-edid.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with open(edid_path, "rb") as fh:
        info = mod.decode(fh.read())
    ch = info["chromaticity"]
    prim = {
        "red": ch["red"],
        "green": ch["green"],
        "blue": ch["blue"],
        "white": ch["white"],
    }
    return prim, info["gamma"], info


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the generated profile here")
    ap.add_argument("--check", help="parse and sanity-check an existing profile")
    ap.add_argument("--from-edid", help="EDID file (default: connected eDP panel)")
    ap.add_argument("--srgb", action="store_true", help="use sRGB primaries instead of the panel's")
    ap.add_argument("--gamma", type=float, default=None, help="override the TRC gamma")
    ap.add_argument(
        "--description", default="OXP3 eDP panel (EDID primaries, gamma 2.20)"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.check:
        info = read_profile(args.check)
        ok = (
            info["class"] == "mntr"
            and info["space"] == "RGB "
            and info["pcs"] == "XYZ "
            and all(info[k] for k in ("red", "green", "blue", "white"))
            and bool(info["gamma"])
        )
        if args.json:
            json.dump(info, sys.stdout, indent=2)
            print()
        else:
            print(f"{args.check}: {info['description']}")
            print(
                f"  class={info['class']} space={info['space']} pcs={info['pcs']} "
                f"v{info['version']} tags={len(info['tags'])}"
            )
            print(
                f"  wtpt={info['white']} rXYZ={info['red']} gXYZ={info['green']} "
                f"bXYZ={info['blue']} gamma={info['gamma']}"
            )
            print(f"  structurally valid: {'yes' if ok else 'NO'}")
        return 0 if ok else 1

    if args.srgb:
        prim, gamma, info = SRGB_PRIMARIES, 2.2, None
    else:
        edid = args.from_edid
        if not edid:
            import glob

            cand = [p for p in glob.glob("/sys/class/drm/card*-eDP-*/edid")]
            if not cand:
                print("no eDP EDID found; pass --from-edid or --srgb", file=sys.stderr)
                return 1
            edid = cand[0]
        prim, gamma, info = panel_primaries(edid)

    if args.gamma:
        gamma = args.gamma
    blob = build_profile(
        prim,
        gamma,
        args.description,
        "Generated by oxp3-anatase-fixes tools/display/oxp3-icc.py (no copyright).",
    )
    if not args.out:
        print(f"profile bytes: {len(blob)} (use --out PATH to write it)", file=sys.stderr)
        return 0
    with open(args.out, "wb") as fh:
        fh.write(blob)
    summary = read_profile(args.out)
    print(f"wrote {args.out} ({len(blob)} bytes)")
    print(
        f"  primaries R{prim['red']} G{prim['green']} B{prim['blue']} "
        f"white {prim['white']} gamma {gamma:g}"
    )
    print(f"  wtpt(D50)={summary['white']} rXYZ={summary['red']}")
    if info:
        print(f"  source EDID: {edid} ({info['capability']['panel_name']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
