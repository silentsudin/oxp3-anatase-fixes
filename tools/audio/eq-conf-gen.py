#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate the OXP3 PipeWire speaker EQ from a user's own extracted Harman JSON.

The input is the JSON produced by tools/audio/apo_extract.py from the user's
GA18v3.reg/AudioEFXService64.exe.  Nothing vendor-derived is stored in this
repository: the generated config lives in the user's state directory.
"""
import argparse
import json

PROFILES = {"loud": 2.84, "safe": -8.0}


def band_nodes(block):
    out = []
    for b in block.get("bands", []):
        if float(b.get("enable", 0)) == 0.0:
            continue
        if float(b.get("gain_db", 0)) == 0.0:
            continue
        out.append(b)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="loud")
    args = ap.parse_args()

    data = json.load(open(args.json))
    blocks = data.get("eq_blocks", [])
    block = next((b for b in blocks if b.get("offset_1based") == 19), None)
    if block is None:
        block = next((b for b in blocks if b.get("changed_bands")), None)
    if block is None:
        raise SystemExit("no changed EQ block in %s; was the right endpoint/setting exported?" % args.json)
    bands = band_nodes(block)
    if not bands:
        raise SystemExit("the extracted EQ block has no enabled non-zero bands")

    gain = PROFILES[args.profile]
    lines = [
        "# Generated from the user's own Harman AudioEFX preset by tools/audio/Harman import.",
        "# SPDX-License-Identifier: MIT",
        "# Profile: %s (headroom %s dB).  Source JSON: %s" % (args.profile, gain, args.json),
        "context.modules = [",
        "    { name = libpipewire-module-filter-chain",
        "        args = {",
        '            node.description = "OXP3 Speaker (Harman EQ)"',
        '            media.name       = "OXP3 Speaker (Harman EQ)"',
        "            filter.graph = {",
        "                nodes = [",
        '                    { type = builtin name = headroom label = bq_lowshelf',
        '                      control = { "Freq" = 20000 "Q" = 0.7 "Gain" = %s } }' % gain,
    ]
    for i, b in enumerate(bands, 1):
        lines += [
            "                    { type = builtin name = b%d  label = bq_peaking" % i,
            '                      control = { "Freq" = %g "Q" = %g "Gain" = %g } }' % (
                float(b["freq_hz"]), float(b["q"]), float(b["gain_db"])),
        ]
    lines += ["                ]", "                links = ["]
    prev = "headroom"
    for i in range(1, len(bands) + 1):
        lines.append('                    { output = "%s:Out" input = "b%d:In" }' % (prev, i))
        prev = "b%d" % i
    lines += [
        "                ]",
        "            }",
        "            audio.channels = 2",
        "            audio.position = [ FL FR ]",
        "            capture.props = {",
        '                node.name        = "oxp3_speaker_eq.sink"',
        '                node.description = "OXP3 Speaker (Harman EQ)"',
        '                media.class      = "Audio/Sink"',
        "                priority.driver  = 10000",
        "                priority.session = 1500",
        "            }",
        "            playback.props = {",
        '                node.name    = "oxp3_speaker_eq.out"',
        "                node.passive = true",
        '                node.target  = "__SINK__"',
        "            }",
        "        }",
        "    }",
        "]",
    ]
    open(args.out, "w").write("\n".join(lines) + "\n")
    print("wrote %s (%d bands, %s profile, headroom %s dB)" % (args.out, len(bands), args.profile, gain))


if __name__ == "__main__":
    main()
