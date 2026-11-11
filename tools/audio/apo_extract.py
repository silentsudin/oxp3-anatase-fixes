#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract the OneXPlayer 3 speaker tuning from the Harman GA18v3 parameter file.

Everything here is *our* parser for data the user already owns on disk; no vendor
code is copied or redistributed.  Inputs (both optional except --reg):

  --reg     GA18v3.reg from the vendor driver package / "Harmon APO" folder
            (UTF-16LE "Windows Registry Editor Version 5.00" export).  It holds
            HKLM\\SOFTWARE\\Harman\\AudioEFX\\{Defaults,Parameters}\\<endpoint-GUID>\\
            Settings\\<1..6>, one DWORD per "Param<N>", little-endian float32.
  --exe     AudioEFXService64.exe; its read-only data section carries the same
            1580-float table as the factory "flat" defaults, which lets us print
            only what the vendor preset actually changed.

Endpoint GUIDs are KSNODETYPE endpoint GUIDs from the Intel SST INF, so they are
stable, not machine specific:

  {DFF21CE1-F70F-11D0-B917-00A0C9223196}  KSNODETYPE_SPEAKER   <- internal speakers
  {DFF21CE2-...}                          KSNODETYPE_HEADPHONES
  {DFF21FE3-...}                          S/PDIF / HDMI family
  {C1A3088B-E1BB-425B-8ABA-65DC3B3675A7}  other endpoint on this platform

"ActiveSetting" per endpoint selects the live preset (2 on this device).

EQ blocks: a block is 10 bands x 5 float32 read as
    (freq_Hz, gain_linear, Q, selector, enable)
gain_linear is 10**(dB/20).  We detect blocks by the 31/63/125 Hz octave series
in the flat defaults, then read the same offsets from the active preset.

Usage:
  ./apo_extract.py --reg ~/Downloads/oxp3/'Harmon APO'/GA18v3.reg \
                   --exe ~/Downloads/oxp3/'Harmon APO'/AudioEFXService64.exe \
                   --json /tmp/apo.json --csv tools/audio/harman-speaker-bands.csv
"""
import argparse
import json
import struct
import sys

SPEAKER = "{DFF21CE1-F70F-11D0-B917-00A0C9223196}"
HEADPHONES = "{DFF21CE2-F70F-11D0-B917-00A0C9223196}"
SPDIF = "{DFF21FE3-F70F-11D0-B917-00A0C9223196}"
OTHER = "{C1A3088B-E1BB-425B-8ABA-65DC3B3675A7}"
ENDPOINTS = [SPEAKER, HEADPHONES, SPDIF, OTHER]

PARAM_COUNT = 1580          # one preset (validated against the exe defaults table)
OCTAVE_SERIES = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def parse_reg(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-16-be")
    else:
        text = raw.decode("utf-8-sig")
    out, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].replace("HKEY_LOCAL_MACHINE\\SOFTWARE\\Harman\\AudioEFX\\", "")
            cur = key
            out.setdefault(cur, {})
            continue
        if cur is None or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().strip('"')
        value = value.strip()
        if value.startswith("hex:"):
            blob = bytes(int(x, 16) for x in value[4:].split(","))
            out[cur][name] = struct.unpack("<f", blob)[0]
        else:
            out[cur][name] = value.strip('"')
    return out


def floats_section(reg, key, count=PARAM_COUNT):
    d = reg.get(key)
    if not d:
        return None
    vals = [d.get("Param%d" % i) for i in range(1, count + 1)]
    return [v for v in vals if v is not None]


def find_defaults_in_exe(path, count=PARAM_COUNT):
    """Locate the factory 1580-float table by its leading values."""
    blob = open(path, "rb").read()
    want = struct.pack("<5f", 1.0, 0.0, 0.0, 0.0, 1.0)
    off = blob.find(want)
    while off >= 0:
        table = struct.unpack("<%df" % count, blob[off:off + count * 4])
        if table[4] == 1.0 and table[5] == 0.0 and abs(table[7] - 0.0004) < 1e-6:
            return off, list(table)
        off = blob.find(want, off + 1)
    return None, None


def find_eq_blocks(flat):
    """Offsets where a 10-band octave EQ block starts, in the flat table."""
    blocks = []
    for i in range(len(flat) - 50):
        if (flat[i] == 31.0 and flat[i + 5] == 63.0 and flat[i + 10] == 125.0):
            blocks.append(i)
    return blocks


def read_bands(table, start):
    bands = []
    for k in range(10):
        f, g, q, sel, en = (float(x) for x in table[start + 5 * k:start + 5 * k + 5])
        bands.append({
            "freq_hz": f,
            "gain_lin": g,
            "gain_db": 20.0 * __import__("math").log10(g) if g > 0 else float("-inf"),
            "q": q,
            "selector": sel,
            "enable": en,
        })
    return bands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reg", required=True)
    ap.add_argument("--exe")
    ap.add_argument("--endpoint", default=SPEAKER)
    ap.add_argument("--json")
    ap.add_argument("--csv")
    args = ap.parse_args()

    reg = parse_reg(args.reg)
    defaults_table = None
    exe_off = None
    if args.exe:
        exe_off, defaults_table = find_defaults_in_exe(args.exe)
        if defaults_table is None:
            print("warning: defaults table not found in %s" % args.exe, file=sys.stderr)

    report = {}
    for ep in ENDPOINTS:
        act = reg.get("Parameters\\%s\\Settings" % ep, {}).get("ActiveSetting")
        settings = {}
        for s in range(1, 7):
            vals = floats_section(reg, "Parameters\\%s\\Settings\\%d" % (ep, s))
            if vals:
                settings[str(s)] = vals
        report[ep] = {
            "active_setting": act,
            "n_params": {k: len(v) for k, v in settings.items()},
            "settings": settings,
        }

    ep = args.endpoint
    active = report[ep]["active_setting"] or "2"
    tuned = report[ep]["settings"][active]
    flat = defaults_table or report[ep]["settings"].get("1")

    deltas = []
    if flat and len(flat) == len(tuned):
        deltas = [{"index_1based": i + 1, "tuned": tuned[i], "flat": flat[i]}
                  for i in range(len(tuned)) if tuned[i] != flat[i]]

    blocks = find_eq_blocks(flat) if flat else []
    eq = []
    for start in blocks:
        bands = read_bands(tuned, start)
        flat_bands = read_bands(flat, start)
        changed = [b for b, fb in zip(bands, flat_bands) if b != fb]
        eq.append({
            "offset": start,
            "offset_1based": start + 1,
            "bands": bands,
            "changed_bands": changed,
        })

    result = {
        "endpoint": ep,
        "active_setting": active,
        "params_per_preset": PARAM_COUNT,
        "exe_defaults_offset": exe_off,
        "endpoints": {k: {"active_setting": v["active_setting"],
                          "n_params": v["n_params"]} for k, v in report.items()},
        "n_deltas": len(deltas),
        "deltas": deltas,
        "eq_blocks": eq,
        "raw_tuned": tuned,
        "raw_flat": flat,
    }

    if args.json:
        json.dump(result, open(args.json, "w"), indent=1)

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("module_offset_1based,band,freq_hz,gain_db,q,selector,enable\n")
            for blk in eq:
                for i, b in enumerate(blk["bands"], 1):
                    if b["enable"] == 0.0:
                        continue
                    f.write("%d,%d,%g,%.2f,%g,%g,%g\n" % (
                        blk["offset_1based"], i, b["freq_hz"], b["gain_db"],
                        b["q"], b["selector"], b["enable"]))
        print("wrote %s" % args.csv)

    print("endpoint          %s" % ep)
    print("active setting    %s   (%d floats per preset)" % (active, PARAM_COUNT))
    if exe_off is not None:
        print("defaults table    @0x%x in AudioEFXService64.exe" % exe_off)
    print("changed floats    %d of %d" % (len(deltas), PARAM_COUNT))
    print("EQ blocks         %s" % ", ".join(str(b["offset_1based"]) for b in eq))
    for blk in eq:
        if not blk["changed_bands"]:
            continue
        print("\nEQ block @%d (1-based), changed bands:" % blk["offset_1based"])
        for b in blk["changed_bands"]:
            print("  %7g Hz  %+5.1f dB  Q=%-5g sel=%g" % (
                b["freq_hz"], b["gain_db"], b["q"], b["selector"]))
    print("\nnon-EQ deltas (index 1-based, tuned <- flat):")
    for d in deltas:
        if any(b["offset_1based"] <= d["index_1based"] < b["offset_1based"] + 50
               for b in eq):
            continue
        print("  %4d  %10.6g  <- %10.6g" % (d["index_1based"], d["tuned"], d["flat"]))


if __name__ == "__main__":
    main()
