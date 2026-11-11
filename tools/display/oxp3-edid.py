#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""oxp3-edid - decode the OneXPlayer 3 panel EDID and report its HDR/colour capability.

Why this exists
---------------
`docs/hw/gap-audit.md` (row 9) could only say "eDP advertises a single
1920x1200 mode" because `edid-decode`/`drm_info` are not installed on Anatase
(immutable host, no rpm-ostree layering wanted for a read-only check).  The
EDID blob itself is readable by any user, so this tool decodes it with the
Python standard library only: base block, chroma coordinates, detailed
timings, CTA-861 extension blocks (colorimetry, HDR static metadata,
YCbCr 4:2:0) and DisplayID extension detection.

Everything printed here is a *claim by the panel*.  Whether the kernel, KWin,
gamescope or a game actually use it is checked separately - see
`docs/hw/hdr-vibrance.md`.

Usage
-----
    tools/display/oxp3-edid.py                  # the connected eDP panel
    tools/display/oxp3-edid.py --json           # machine readable
    tools/display/oxp3-edid.py /path/to/edid.bin
    tools/display/oxp3-edid.py --all            # every connector with an EDID

Exit codes: 0 = decoded, 1 = no EDID / not decodable.
"""

from __future__ import annotations

import argparse
import glob
import json

import sys

# CTA-861 extended tag codes we care about (CTA-861-G / H)
EXT_VIDEO_CAP = 0x00
EXT_COLORIMETRY = 0x05
EXT_HDR_STATIC = 0x06
EXT_HDR_DYNAMIC = 0x07
EXT_YCBCR420 = 0x0E
EXT_YCBCR420_MAP = 0x0F

COLORIMETRY_BITS = [
    "xvYCC601",
    "xvYCC709",
    "sYCC601",
    "AdobeYCC601",
    "AdobeRGB",
    "BT2020cYCC",
    "BT2020YCC",
    "BT2020RGB",
]
COLORIMETRY_BITS2 = [
    "DCI-P3",
    "DisplayNative",
    None,
    None,
    None,
    None,
    None,
    None,
]

EOTF_BITS = [
    "traditional-gamma-SDR",
    "traditional-gamma-HDR",
    "SMPTE-ST-2084-PQ",
    "HLG",
]

# Digital display colour depth, EDID 1.4 (byte 20 bits 6..4)
DEPTH_BITS = {0: None, 1: 6, 2: 8, 3: 10, 4: 12, 5: 14, 6: 16}
# Digital interface, EDID 1.4 (byte 20 bits 3..0)
IFACE = {
    0: "undefined",
    1: "DVI",
    2: "HDMI-a",
    3: "HDMI-b",
    4: "MDDI",
    5: "DisplayPort",
    6: "undefined",
}


def _checksum_ok(block: bytes) -> bool:
    return len(block) == 128 and sum(block) % 256 == 0


def _mfg_id(raw: int) -> str:
    # 3 x 5-bit letters, 'A' = 1
    return "".join(chr(ord("A") - 1 + ((raw >> s) & 0x1F)) for s in (10, 5, 0))


def _lum(cv: int) -> float:
    """HDR static metadata luminance code -> cd/m^2 (CTA-861 3.6.6)."""
    return 50.0 * (2.0 ** (cv / 32.0))


def _xy(hi: int, lo: int, shift: int) -> float:
    v = (hi << 2) | ((lo >> shift) & 0x03)
    return v / 1024.0


def parse_chroma(ch: bytes) -> dict:
    rl, bl = ch[0], ch[1]
    return {
        "red": (_xy(ch[2], rl, 6), _xy(ch[3], rl, 4)),
        "green": (_xy(ch[4], rl, 2), _xy(ch[5], rl, 0)),
        "blue": (_xy(ch[6], bl, 6), _xy(ch[7], bl, 4)),
        "white": (_xy(ch[8], bl, 2), _xy(ch[9], bl, 0)),
    }


def parse_detail_timing(dt: bytes) -> dict:
    pclk = int.from_bytes(dt[0:2], "little") * 10  # kHz
    ha = dt[2] | ((dt[4] >> 4) << 8)
    hb = dt[3] | ((dt[4] & 0x0F) << 8)
    va = dt[5] | ((dt[7] >> 4) << 8)
    vb = dt[6] | ((dt[7] & 0x0F) << 8)
    total = (ha + hb) * (va + vb)
    return {
        "pixel_clock_khz": pclk,
        "width": ha,
        "height": va,
        "refresh_hz": round(pclk * 1000.0 / total, 3) if total else None,
        "h_total": ha + hb,
        "v_total": va + vb,
    }


def parse_descriptors(block: bytes) -> dict:
    out = {"detailed_timings": [], "range_limits": None, "name": None, "serial": None}
    for off in (0x36, 0x48, 0x5A, 0x6C):
        d = block[off:off + 18]
        if len(d) < 18:
            continue
        if d[0] == 0 and d[1] == 0 and d[2] == 0:
            tag = d[3]
            if tag == 0xFD and d[4] == 0:  # display range limits
                out["range_limits"] = {
                    "min_vrate_hz": d[5],
                    "max_vrate_hz": d[6],
                    "min_hrate_khz": d[7],
                    "max_hrate_khz": d[8],
                    "max_pclk_mhz": d[9] * 10,
                }
            elif tag in (0xFC, 0xFE):
                txt = d[5:18].split(b"\n")[0].split(b"\x00")[0].decode("latin-1").strip()
                out["name" if tag == 0xFC else "serial"] = txt
        else:
            out["detailed_timings"].append(parse_detail_timing(d))
    return out


def parse_cta(block: bytes) -> dict:
    res = {
        "revision": block[1],
        "data_block_offset": block[2],
        "colorimetry": [],
        "hdr_static": None,
        "hdr_dynamic": False,
        "video_capability": None,
        "ycbrcr420": False,
        "blocks": [],
    }
    end = min(block[2], 127) or 127
    p = 4
    while p < end:
        hdr = block[p]
        tag, ln = hdr >> 5, hdr & 0x1F
        data = block[p + 1:p + 1 + ln]
        if len(data) != ln:
            break
        res["blocks"].append({"tag": tag, "length": ln, "data": data.hex()})
        if tag == 7 and ln >= 1:
            et = data[0]
            if et == EXT_COLORIMETRY and ln >= 3:
                b1, b2 = data[1], data[2]
                res["colorimetry"] += [
                    name for bit, name in enumerate(COLORIMETRY_BITS) if b1 >> bit & 1
                ]
                res["colorimetry"] += [
                    name
                    for bit, name in enumerate(COLORIMETRY_BITS2)
                    if name and b2 >> bit & 1
                ]
            elif et == EXT_HDR_STATIC and ln >= 2:
                eotf = data[1]
                meta = data[2] if ln >= 3 else 0
                h = {
                    "eotf": [n for bit, n in enumerate(EOTF_BITS) if eotf >> bit & 1],
                    "supported_eotf_mask": eotf,
                    "static_metadata_type1": bool(meta & 0x01),
                    "max_luminance_nits": None,
                    "max_fall_nits": None,
                    "min_luminance_nits": None,
                }
                if ln >= 6:
                    h["max_luminance_nits"] = round(_lum(data[3]), 1)
                    h["max_fall_nits"] = round(_lum(data[4]), 1)
                    mn_cv = data[5]
                    h["min_luminance_nits"] = round(
                        _lum(data[3]) * (mn_cv / 255.0) ** 2 / 100.0, 5
                    )
                res["hdr_static"] = h
            elif et == EXT_HDR_DYNAMIC:
                res["hdr_dynamic"] = True
            elif et == EXT_VIDEO_CAP and ln >= 2:
                res["video_capability"] = {"raw": data[1:].hex()}
            elif et in (EXT_YCBCR420, EXT_YCBCR420_MAP):
                res["ycbrcr420"] = True
        p += 1 + ln
    return res


def parse_displayid(block: bytes) -> dict:
    # 0x70 = DisplayID extension (EDID 1.4 = DisplayID 2.0)
    version = block[1] if len(block) > 1 else 0
    return {"tag": block[0], "version_byte": version, "raw": block[:32].hex()}


def decode(data: bytes) -> dict:
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        raise ValueError("not an EDID base block")
    vi = data[20]
    digital = bool(vi >> 7)
    info = {
        "bytes": len(data),
        "base_checksum_ok": _checksum_ok(data[:128]),
        "mfg": _mfg_id(int.from_bytes(data[8:10], "big")),
        "product_code": f"0x{int.from_bytes(data[10:12], 'little'):04x}",
        "week": data[16],
        "year": 1990 + data[17],
        "edid_version": f"{data[18]}.{data[19]}",
        "video_input": {
            "digital": digital,
            "bit_depth": DEPTH_BITS.get((vi >> 4) & 0x07) if digital else None,
            "interface": IFACE.get(vi & 0x0F) if digital else None,
            "raw": f"0x{vi:02x}",
        },
        "size_cm": (data[21], data[22]),
        "gamma": (data[23] + 100) / 100.0,
        "features": f"0x{data[24]:02x}",
        "chromaticity": parse_chroma(data[0x19:0x23]),
        "extensions": data[126],
    }
    info.update(parse_descriptors(data))
    info["cta"] = []
    info["displayid"] = []
    for i in range(info["extensions"]):
        b = data[128 * (i + 1):128 * (i + 2)]
        if len(b) < 128:
            break
        ok = _checksum_ok(b)
        if b[0] == 0x02:
            c = parse_cta(b)
            c["checksum_ok"] = ok
            c["index"] = i + 1
            info["cta"].append(c)
        elif b[0] == 0x70:
            info["displayid"].append(parse_displayid(b))
        else:
            info.setdefault("other_extensions", []).append(
                {"index": i + 1, "tag": f"0x{b[0]:02x}", "checksum_ok": ok}
            )
    info["capability"] = capability(info)
    return info


def _gamut_class(chroma: dict) -> str:
    """Classify the panel's own primaries against sRGB and DCI-P3 areas."""
    rx, ry = chroma["red"]
    gx, gy = chroma["green"]
    bx, by = chroma["blue"]

    def area(x1, y1, x2, y2, x3, y3):
        return abs((x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)) / 2.0)

    panel = area(rx, ry, gx, gy, bx, by)
    srgb = area(0.640, 0.330, 0.300, 0.600, 0.150, 0.060)
    p3 = area(0.680, 0.320, 0.265, 0.690, 0.150, 0.060)
    if abs(panel - srgb) / srgb < 0.06:
        return "sRGB-class"
    return (
        f"wide gamut: {panel / srgb:.2f}x sRGB area, "
        f"{panel / p3 - 1:+.0%} vs DCI-P3"
    )


def capability(info: dict) -> dict:
    hdr = None
    colorimetry = []
    for cta in info["cta"]:
        if cta["hdr_static"]:
            hdr = cta["hdr_static"]
        colorimetry += cta["colorimetry"]
    depth = info["video_input"]["bit_depth"]
    pq = bool(hdr and "SMPTE-ST-2084-PQ" in hdr["eotf"])
    hlq = bool(hdr and "HLG" in hdr["eotf"])
    return {
        "panel_name": info.get("name"),
        "max_refresh_hz": max(
            (t["refresh_hz"] for t in info["detailed_timings"] if t["refresh_hz"]),
            default=None,
        ),
        "native_mode": (
            f"{info['detailed_timings'][0]['width']}x"
            f"{info['detailed_timings'][0]['height']}"
            if info["detailed_timings"]
            else None
        ),
        "bit_depth": depth,
        "gamut_class": _gamut_class(info["chromaticity"]),
        "white_point": info["chromaticity"]["white"],
        "colorimetry": colorimetry,
        "hdr_static_metadata": hdr,
        "pq": pq,
        "hlg": hlq,
        "hdr_claim": (
            "HDR (PQ)"
            if pq
            else ("HDR (HLG only)" if hlq else "SDR only as described by EDID")
        ),
        "verdict": _verdict(depth, pq, colorimetry),
    }


def _verdict(depth, pq, colorimetry) -> str:
    parts = []
    if depth and depth >= 10:
        parts.append(f"{depth}-bit capable")
    if pq:
        parts.append("PQ/HDR10 claim present")
    if "BT2020RGB" in colorimetry:
        parts.append("BT.2020 RGB colorimetry claim")
    if not parts:
        return "no HDR capability advertised"
    return "; ".join(parts) + " -> compositor must drive it (no vendor code path involved)"


def find_edids() -> list:
    """Connector EDID paths that actually return bytes.

    sysfs reports st_size 0 for these attribute files, so the size must be
    probed by reading (the kernel materialises the blob on read).
    """
    out = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*/edid")):
        try:
            with open(path, "rb") as fh:
                if fh.read(1):
                    out.append(path)
        except OSError:
            continue
    return out


def render(info: dict) -> str:
    cap = info["capability"]
    ch = info["chromaticity"]
    li = []
    li.append(
        f"{info['mfg']} {cap['panel_name'] or ''} "
        f"(product {info['product_code']}, {info['week']}/{info['year']}, "
        f"EDID {info['edid_version']}, {info['bytes']} bytes)"
    )
    li.append(
        "interface      : {iface}{depth}".format(
            iface=info["video_input"]["interface"],
            depth=(
                f", {info['video_input']['bit_depth']} bit/colour"
                if info["video_input"]["bit_depth"]
                else ", depth undefined"
            ),
        )
    )
    dt = info["detailed_timings"][0] if info["detailed_timings"] else None
    if dt:
        li.append(
            f"detail timing  : {dt['width']}x{dt['height']}@{dt['refresh_hz']} Hz "
            f"(pclk {dt['pixel_clock_khz'] / 1000:.3f} MHz, "
            f"h {dt['h_total']}, v {dt['v_total']})"
        )
    rl = info.get("range_limits")
    if rl:
        li.append(
            f"range limits   : {rl['min_vrate_hz']}-{rl['max_vrate_hz']} Hz vertical, "
            f"{rl['min_hrate_khz']}-{rl['max_hrate_khz']} kHz horizontal, "
            f"{rl['max_pclk_mhz']} MHz max pclk"
        )
    li.append(f"gamma          : {info['gamma']:.2f}")
    li.append(
        "primaries      : R({0[0]:.4f},{0[1]:.4f}) G({1[0]:.4f},{1[1]:.4f}) "
        "B({2[0]:.4f},{2[1]:.4f})".format(ch["red"], ch["green"], ch["blue"])
    )
    li.append(
        f"white point    : ({ch['white'][0]:.4f},{ch['white'][1]:.4f})  "
        f"gamut: {cap['gamut_class']}"
    )
    if cap["colorimetry"]:
        li.append(f"colorimetry    : {', '.join(cap['colorimetry'])}")
    h = cap["hdr_static_metadata"]
    if h:
        li.append(
            "HDR static     : EOTF {eotf}; static metadata type 1 {sm}; "
            "max {mx} cd/m2, maxFALL {fa} cd/m2, min {mn} cd/m2".format(
                eotf="+".join(h["eotf"]),
                sm="supported" if h["static_metadata_type1"] else "not supported",
                mx=h["max_luminance_nits"],
                fa=h["max_fall_nits"],
                mn=h["min_luminance_nits"],
            )
        )
    else:
        li.append("HDR static     : no CTA-861 HDR static metadata block")
    for d in info["displayid"]:
        li.append(f"DisplayID      : extension present (version byte {d['version_byte']})")
    li.append(f"VERDICT        : {cap['verdict']}")
    return "\n".join(li)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("edid", nargs="?", help="EDID file (default: the connected panel)")
    ap.add_argument("--all", action="store_true", help="decode every connector EDID")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    args = ap.parse_args(argv)

    if args.all:
        paths = find_edids()
    elif args.edid:
        paths = [args.edid]
    else:
        conn = [p for p in find_edids() if "eDP" in p]
        paths = (conn or find_edids())[:1]

    if not paths:
        print("no EDID found (connector disconnected?)", file=sys.stderr)
        return 1

    out = {}
    for p in paths:
        with open(p, "rb") as fh:
            data = fh.read()
        try:
            info = decode(data)
        except ValueError as exc:
            print(f"{p}: {exc}", file=sys.stderr)
            continue
        info["path"] = p
        out[p] = info
        if not args.json:
            print(f"== {p}")
            print(render(info))
    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
