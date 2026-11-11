#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only loudness report for the OneXPlayer 3 speaker chain (our own code).

It answers the owner's question "how much level is the chain throwing away?"
without playing audio and without changing anything:

  ALSA speaker volume   amixer -c0 sget <Front|Speaker|Mono>
                        raw value, percent, dB and whether it is at maximum
  PipeWire sink volume  pactl get-sink-volume / get-sink-mute
  EQ headroom           the headroom node's Gain in the installed drop-in
  net gain vs flat      ALSA attenuation + EQ headroom (the static gain staging)
  net vs the old default  the same number against the shipped -8 dB/82-of-87 chain
  clipping margin       -dense peak of the modelled chain; negative means a
                        0 dBFS tone clips by that much (the loud profile does,
                        the safe profile does not)

The model comes from eq_response.py, so the profile numbers cannot drift from
the filter chain.  Everything here is read-only; --json is for the verifier.

Usage:
    tools/audio/level-report.py                 human report
    tools/audio/level-report.py --json          machine report
    tools/audio/level-report.py --help
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import eq_response  # noqa: E402  (same-directory model)

# The speaker path may be called "Front" (UCM/older kernels), "Speaker" (this
# ALC245 HDA codec) or "Mono" (the joined mono control some UCMs expose).  All
# present controls are raised to maximum; an absent one is skipped.
CANDIDATE_CONTROLS = ("Front", "Speaker", "Mono")

# amixer prints the exact dB from the control's TLV.  It is the authoritative
# number; DB_PER_STEP is the fallback for a control without a dB range and the
# value the unit tests pin (ALC245 speaker volume: 0.75 dB/step, 87 steps).
DB_PER_STEP = 0.75

# The chain as shipped before this change: ALSA at 82/87 (-3.75 dB) + the
# -8 dB headroom.  Used only for the "how much did we gain?" comparison.
OLD_DEFAULT_ALSA_DB = -3.75
OLD_DEFAULT_HEADROOM_DB = -8.0
OLD_DEFAULT_NET_DB = OLD_DEFAULT_ALSA_DB + OLD_DEFAULT_HEADROOM_DB


def alsa_db(value, maximum, step_db=DB_PER_STEP):
    """Static attenuation of a raw ALSA control value (maximum == 0 dB)."""
    if maximum <= 0:
        raise ValueError("maximum must be positive")
    if not 0 <= value <= maximum:
        raise ValueError("value %r outside 0..%r" % (value, maximum))
    return -(maximum - value) * step_db


def parse_amixer_sget(text):
    """Parse one `amixer sget` block into name/max/values/percent/db/at_max."""
    name_m = re.search(r"Simple mixer control '([^']+)'", text)
    max_m = re.search(r"Limits:\s*Playback\s+\d+\s*-\s*(\d+)", text)
    if not name_m or not max_m:
        return None
    maximum = int(max_m.group(1))
    values = [int(v) for v in re.findall(r"Playback\s+(\d+)\s+\[", text)]
    if not values:
        return None
    percent_m = re.search(r"Playback\s+\d+\s+\[(\d+)%\]", text)
    db_m = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", text)
    db = float(db_m.group(1)) if db_m else alsa_db(min(values), maximum)
    return {
        "name": name_m.group(1),
        "max": maximum,
        "values": values,
        "value": min(values),
        "balanced": len(set(values)) == 1,
        "percent": int(percent_m.group(1)) if percent_m else None,
        "db": round(db, 3),
        "at_max": all(v == maximum for v in values),
    }


def parse_sink_volume(text):
    """Parse `pactl get-sink-volume` -> percent/db of the first channel."""
    m = re.search(r"(\d+)%\s*/\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    if not m:
        return None
    return {"percent": int(m.group(1)), "db": round(float(m.group(2)), 3)}


def parse_sink_mute(text):
    """Parse `pactl get-sink-mute` -> True/False or None."""
    m = re.search(r"Mute:\s*(yes|no)", text, re.IGNORECASE)
    return None if not m else m.group(1).lower() == "yes"


def parse_eq_headroom(conf_text):
    """Headroom (dB) of the filter-chain drop-in, or None if there is none."""
    node = re.search(r"name\s*=\s*headroom\b", conf_text)
    if not node:
        return None
    tail = conf_text[node.end():]
    gain = re.search(r'"Gain"\s*=\s*(-?\d+(?:\.\d+)?)', tail)
    if not gain:
        return None
    return float(gain.group(1))


def parse_profile_marker(conf_text):
    """The `# oxp3-profile: <name>` marker install-user.sh writes, if any."""
    m = re.search(r"^#\s*oxp3-profile:\s*([A-Za-z0-9_-]+)", conf_text, re.MULTILINE)
    return m.group(1) if m else None


def profile_for(headroom_db):
    """Name the installed headroom: loud/safe/off/custom."""
    if headroom_db is None:
        return "off"
    for name in ("loud", "safe"):
        want = eq_response.PROFILES[name]
        if abs(headroom_db - want) < 0.05:
            return name
    return "custom"


def net_gain_db(alsa_controls, headroom_db):
    """Static gain staging: sum of the present ALSA attenuations + EQ headroom.

    On this machine only one of Front/Speaker/Mono exists, so the sum is just
    that control.  Summing is the conservative reading if a future UCM exposes
    two in series (two static attenuators in the speaker path).
    """
    alsa = sum(c["db"] for c in alsa_controls)
    return round(alsa + (headroom_db or 0.0), 3)


def build_report(card, controls, sink, eq_conf_text, profile_override=None):
    """Assemble the report from already-collected facts (pure, unit-tested)."""
    headroom = parse_eq_headroom(eq_conf_text) if eq_conf_text else None
    # The Gain value is authoritative; the marker only names a custom chain.
    profile = profile_for(headroom)
    if profile == "custom" and profile_override:
        profile = profile_override
    peak, trough, peak_hz = eq_response.curve_extremes(pre=headroom or 0.0)
    net = net_gain_db(controls, headroom)
    return {
        "schema": "oxp3-level-report/1",
        "card": card,
        "controls": controls,
        "control": controls[0]["name"] if controls else None,
        "alsa_db": round(sum(c["db"] for c in controls), 3),
        "at_max": bool(controls) and all(c["at_max"] for c in controls),
        "sink": sink,
        "eq": {
            "installed": headroom is not None,
            "profile": profile,
            "headroom_db": headroom,
            "peak_db": round(peak, 3),
            "trough_db": round(trough, 3),
            "peak_hz": round(peak_hz, 1),
            "clip_margin_db": round(-peak, 3),
        },
        "net_db": net,
        "net_vs_old_db": round(net - OLD_DEFAULT_NET_DB, 3),
        "old_default_net_db": OLD_DEFAULT_NET_DB,
    }


# --------------------------------------------------------------------------
# host reads (read-only)
# --------------------------------------------------------------------------
def run(cmd):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def detect_card():
    env = os.environ.get("OXP3_AUDIO_CARD", "").strip()
    if env:
        return int(env)
    text = ""
    try:
        with open("/proc/asound/cards", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        pass
    for line in text.splitlines():
        if "sof-hda-dsp" in line or "sofhdadsp" in line:
            m = re.match(r"\s*(\d+)\s+\[", line)
            if m:
                return int(m.group(1))
    return 0


def default_conf_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"),
                                                              ".config")
    return os.path.join(base, "pipewire", "pipewire.conf.d", "51-oxp3-speaker-eq.conf")


def read_controls(card, amixer="amixer"):
    controls = []
    for name in CANDIDATE_CONTROLS:
        text = run([amixer, "-c", str(card), "sget", name])
        if not text:
            continue
        parsed = parse_amixer_sget(text)
        if parsed:
            controls.append(parsed)
    return controls


def read_sink(pactl="pactl"):
    name = (run([pactl, "get-default-sink"]) or "").strip() or None
    sink = {"name": name, "percent": None, "db": None, "muted": None}
    if not name:
        return sink
    vol = parse_sink_volume(run([pactl, "get-sink-volume", name]) or "")
    if vol:
        sink.update(vol)
    sink["muted"] = parse_sink_mute(run([pactl, "get-sink-mute", name]) or "")
    return sink


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--card", type=int, help="ALSA card number (default: detect)")
    ap.add_argument("--conf", help="installed filter-chain drop-in to inspect")
    ap.add_argument("--amixer", default=os.environ.get("OXP3_AMIXER", "amixer"))
    ap.add_argument("--pactl", default=os.environ.get("OXP3_PACTL", "pactl"))
    args = ap.parse_args()

    card = args.card if args.card is not None else detect_card()
    conf_path = args.conf or default_conf_path()
    conf_text = None
    try:
        with open(conf_path, encoding="utf-8", errors="replace") as fh:
            conf_text = fh.read()
    except OSError:
        conf_text = None

    report = build_report(card, read_controls(card, args.amixer),
                          read_sink(args.pactl), conf_text)
    report["conf_path"] = conf_path
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print("OneXPlayer 3 speaker level report (read-only)")
    print("  ALSA card %d:" % card)
    if not report["controls"]:
        print("    no Front/Speaker/Mono playback control found")
    for c in report["controls"]:
        print("    %-8s %2d/%d  %3s%%  %+6.2f dB%s"
              % (c["name"], c["value"], c["max"],
                 c["percent"] if c["percent"] is not None else "?",
                 c["db"], "  (at maximum)" if c["at_max"] else ""))
    sink = report["sink"]
    if sink.get("name") and sink.get("percent") is not None:
        print("  PipeWire sink %s: %s%% %s dB, %s"
              % (sink["name"], sink["percent"], sink["db"],
                 "muted" if sink.get("muted") else "unmuted"))
    else:
        print("  PipeWire sink: not readable (no session)")
    eq = report["eq"]
    if not eq["installed"]:
        print("  EQ: not installed (no headroom node) -> net %+.2f dB vs flat"
              % report["net_db"])
    else:
        print("  EQ: profile %s, headroom %+.1f dB, peak %+.2f dB @ %.0f Hz"
              % (eq["profile"], eq["headroom_db"], eq["peak_db"], eq["peak_hz"]))
    print("  net gain vs flat:            %+.2f dB" % report["net_db"])
    print("  net gain vs the old default: %+.2f dB  (old %+.2f dB)"
          % (report["net_vs_old_db"], report["old_default_net_db"]))
    print("  clipping margin at 0 dBFS:   %+.2f dB%s"
          % (eq["clip_margin_db"],
             "  CLIPS (documented loud-profile trade-off)" if eq["clip_margin_db"] < 0
             else ""))
    print("  ALSA control at maximum:     %s"
          % ("yes" if report["at_max"] else "no"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
