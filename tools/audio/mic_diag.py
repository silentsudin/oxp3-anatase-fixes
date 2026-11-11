#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""mic_diag - read-only diagnosis of the OneXPlayer 3 internal microphone path.

The internal microphone is *not* a PCH DMIC on this machine: the ALC245 pin
complex 0x12 is configured by the BIOS as ``[Fixed] Mic at Int N/A, Conn =
Digital`` and the codec mixes that pin into the capture ADC.  The kernel log
line ``DMICs detected in NHLT tables: 0`` therefore says nothing about this
microphone; it only says the PCH DMIC links are absent.

What can still go wrong is the *exposure*: the shipped UCM2 profile creates a
single capture device gated on the external ``Mic Jack`` control, so PipeWire
marks the only source ``not available`` while nothing is plugged into the
3.5 mm combo jack.  This tool reads the evidence and classifies the state:

    exposed     an always-available capture device for the internal pin exists
                (the opt-in UCM override is installed, or the profile already
                uses a fixed/phantom jack)
    gated       the codec path exists but no always-available capture device
                does; the shipped profile gates it on the external mic jack
    pch-dmic    a PCH DMIC capture PCM exists (device 6); a different path
    absent      the codec pin configuration declares no internal microphone
    unknown     no card / unreadable codec dump

Nothing here writes, plays, or captures audio.  It only reads /proc, /sys, the
installed UCM2 tree and (optionally) ``amixer``/``pactl`` output.

    tools/audio/mic_diag.py                human summary
    tools/audio/mic_diag.py --json         machine report (verifier input)
    tools/audio/mic_diag.py --proc DIR --ucm2 DIR --amixer-file FILE
                                           read a synthetic tree (tests)
    tools/audio/mic_diag.py --help

Exit codes: 0 = a diagnosis was produced (whatever the verdict), 2 = usage
error.  The verdict never changes the exit code: tools/oxp3-verify.sh decides
what PASS/FAIL/SKIP means for the harness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

PROG = "mic_diag"
VERSION = "1.0.0"

DEFAULT_PROC = "/proc/asound"
DEFAULT_UCM2 = "/usr/share/alsa/ucm2"

# HDA pin-default decoding (HDA Specification 1.0a, 7.1.3 "Default Pin
# Configuration").
CONNECTIVITY = {0: "jack", 1: "none", 2: "fixed", 3: "both"}
LOCATIONS = {
    0x00: "n/a", 0x01: "rear", 0x02: "front", 0x03: "left", 0x04: "right",
    0x05: "top", 0x06: "bottom", 0x07: "rear-panel", 0x08: "drive-bar",
    0x09: "hdmi", 0x0A: "atapi", 0x10: "internal", 0x11: "internal",
    0x12: "internal", 0x13: "internal", 0x14: "internal", 0x15: "internal",
    0x16: "internal", 0x17: "internal", 0x18: "internal", 0x19: "internal",
    0x1A: "internal", 0x1B: "internal", 0x1C: "internal", 0x1D: "internal",
    0x1E: "internal", 0x1F: "internal",
}
DEVICES = {
    0x0: "line-out", 0x1: "speaker", 0x2: "hp-out", 0x3: "cd", 0x4: "spdif-out",
    0x5: "digital-out", 0x6: "modem-line", 0x7: "modem-handset", 0x8: "line-in",
    0x9: "aux", 0xA: "mic-in", 0xB: "telephony", 0xC: "spdif-in", 0xD: "digital-in",
    0xE: "reserved", 0xF: "other",
}
CONNECTION_TYPES = {
    0x0: "unknown", 0x1: "1/8\"", 0x2: "1/4\"", 0x3: "atapi", 0x4: "rca",
    0x5: "optical", 0x6: "digital", 0x7: "analog", 0x8: "din", 0x9: "other",
}
PIN_MISC = ("no-presence", "trigger-required", "jack-detect-override",
            "no-jack-detect")

NODE_RE = re.compile(r"^Node 0x([0-9a-fA-F]+) \[([^\]]+)\]")
SECTION_DEVICE_RE = re.compile(r'SectionDevice\."([^"]+)"')
UCM_USE_CASE_FILE_RE = re.compile(r'SectionUseCase\."HiFi"\s*\{[^}]*?File\s+"([^"]+)"', re.S)
# Both files installed by tools/audio/internal-mic.sh carry this marker, so the
# installer can prove the files are ours before it removes anything.
OVERRIDE_MARKER = "OXP3-ONEXPLAYER3-MIC"


# --------------------------------------------------------------------------
# text parsers (pure functions; the unit tests pin every branch)


def decode_pin_default(value):
    """Decode a 32-bit HDA pin-default word into its named fields."""
    value = int(value, 0) if isinstance(value, str) else int(value)
    loc = (value >> 24) & 0x3F
    misc = (value >> 8) & 0xF
    return {
        "value": "0x%08x" % (value & 0xFFFFFFFF),
        "connectivity": CONNECTIVITY[(value >> 30) & 3],
        "location": LOCATIONS.get(loc, "other-0x%02x" % loc),
        "location_raw": loc,
        "device": DEVICES.get((value >> 20) & 0xF, "reserved"),
        "device_raw": (value >> 20) & 0xF,
        "connection": CONNECTION_TYPES.get((value >> 16) & 0xF, "unknown"),
        "connection_raw": (value >> 16) & 0xF,
        "color": (value >> 12) & 0xF,
        "misc": misc,
        "no_presence": bool(misc & 0x1),
        "assoc": (value >> 4) & 0xF,
        "sequence": value & 0xF,
    }


def parse_widgets(codec_text):
    """Parse the widget tree out of a /proc/asound/cardN/codec#M dump.

    Returns {nid: {...}} with the fields the diagnosis needs: kind, controls,
    connection list, Amp-In values, pin default and pin capabilities.
    """
    widgets = {}
    cur = None
    for raw in codec_text.splitlines():
        line = raw.rstrip("\n")
        match = NODE_RE.match(line)
        if match:
            nid = int(match.group(1), 16)
            cur = {
                "nid": nid,
                "kind": match.group(2).strip(),
                "controls": [],
                "connections": [],
                "selected": None,
                "amp_in": [],
                "pin_default": None,
                "pin_ctls": None,
                "pincap": None,
                "n_connections": 0,
            }
            widgets[nid] = cur
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Control: name="):
            found = re.search(r'name="([^"]*)"', s)
            if found:
                cur["controls"].append(found.group(1))
        elif s.startswith("Connection:"):
            found = re.search(r"Connection:\s*(\d+)", s)
            if found:
                cur["n_connections"] = int(found.group(1))
        elif cur["n_connections"] and re.fullmatch(r"(?:0x[0-9a-fA-F]+\*?\s*)+", s):
            for token in s.split():
                starred = token.endswith("*")
                nid = int(token.rstrip("*"), 16)
                cur["connections"].append(nid)
                if starred:
                    cur["selected"] = nid
        elif s.startswith("Amp-In vals:"):
            cur["amp_in"] = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{2})", s)]
        elif s.startswith("Pin Default"):
            found = re.search(r"Pin Default\s+(0x[0-9a-fA-F]+)", s)
            if found:
                cur["pin_default"] = found.group(1)
        elif s.startswith("Pin-ctls:"):
            found = re.search(r"Pin-ctls:\s*(0x[0-9a-fA-F]+)", s)
            if found:
                cur["pin_ctls"] = found.group(1)
        elif s.startswith("Pincap"):
            cur["pincap"] = s.split(":", 1)[1].strip() if ":" in s else s
    return widgets


def pin_attr(pin):
    """Return the pin-default dict of one parsed pin widget."""
    if pin.get("pin_default") is None:
        return None
    return decode_pin_default(pin["pin_default"])


def internal_mic_pins(widgets):
    """All codec pins that describe a fixed internal microphone."""
    out = []
    for nid in sorted(widgets):
        widget = widgets[nid]
        if "Pin Complex" not in widget["kind"]:
            continue
        attr = pin_attr(widget)
        if not attr:
            continue
        if attr["device"] != "mic-in":
            continue
        if attr["connectivity"] not in ("fixed", "both"):
            continue
        if (attr["location_raw"] & 0x30) != 0x10:
            continue
        attr["nid"] = "0x%02x" % nid
        attr["controls"] = list(widget["controls"])
        attr["pin_ctls"] = widget["pin_ctls"]
        attr["pincap"] = widget["pincap"]
        attr["digital"] = attr["connection"] == "digital"
        out.append(attr)
    return out


def capture_adc(widgets):
    """The Audio Input widget the analog capture PCM reads (ADC 0x08 here)."""
    candidates = []
    for nid in sorted(widgets):
        widget = widgets[nid]
        if "Audio Input" not in widget["kind"]:
            continue
        named = any(c in ("Capture Volume", "Capture Switch") for c in widget["controls"])
        candidates.append((0 if named else 1, nid))
    if not candidates:
        return None
    # the ADC with a "Capture Volume/Switch" control is the one the PCM reads
    return min(candidates)[1]


def mic_route(widgets, mic_nid):
    """Follow ADC -> mixer/selector -> pin and report the mixer amp state.

    The ALC245 routes its internal DMIC pin through the *mixer* 0x23 (one
    un-muted input), not through a selectable mux, which is why no "Input
    Source" enum exists on this card.
    """
    adc = capture_adc(widgets)
    if adc is None:
        return None
    adc_widget = widgets[adc]
    if not adc_widget["connections"]:
        return None
    mixer_nid = adc_widget["selected"] if adc_widget["selected"] is not None \
        else adc_widget["connections"][0]
    mixer = widgets.get(mixer_nid)
    if mixer is None or mic_nid not in mixer["connections"]:
        return None
    index = mixer["connections"].index(mic_nid)
    values = mixer["amp_in"]
    if index * 2 + 1 < len(values):
        left, right = values[index * 2], values[index * 2 + 1]
        muted = bool(left & 0x80) and bool(right & 0x80)
    else:
        muted = None
    return {
        "adc": "0x%02x" % adc,
        "mixer": "0x%02x" % mixer_nid,
        "index": index,
        "muted": muted,
    }


def parse_cards(text):
    """Parse /proc/asound/cards (id, driver and long name per card)."""
    cards = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^\s*(\d+)\s+\[([^\]]*)\]:\s*(\S+)\s*-\s*(.+?)\s*$", line)
        if not match:
            continue
        longname = lines[i + 1].strip() if i + 1 < len(lines) else ""
        cards.append({
            "index": int(match.group(1)),
            "id": match.group(2).strip(),
            "driver": match.group(3).strip(),
            "name": match.group(4).strip(),
            "longname": longname,
        })
    return cards


def parse_devices(text):
    """Parse /proc/asound/devices into capture PCM (card, device) pairs."""
    captures = []
    for line in text.splitlines():
        match = re.match(r"^\s*\d+:\s*\[\s*(\d+)\s*-\s*(\d+)\]:\s*(.+?)\s*$", line)
        if not match:
            continue
        desc = match.group(3).lower()
        if "capture" in desc and "playback" not in desc:
            captures.append({"card": int(match.group(1)),
                             "device": int(match.group(2))})
    return captures


def parse_amixer_controls(text):
    """Parse `amixer -c N contents` into {control name: {value,type}}."""
    controls = {}
    name = None
    for line in text.splitlines():
        match = re.match(r"numid=\d+,iface=\w+,name='([^']*)'", line.strip())
        if match:
            name = match.group(1)
            controls[name] = {"value": "", "type": ""}
            continue
        if name is None:
            continue
        s = line.strip()
        if s.startswith("; type="):
            controls[name]["type"] = s.split("type=", 1)[1].split(",")[0]
        elif s.startswith(": values="):
            controls[name]["value"] = s.split("=", 1)[1]
    return controls


def ucm_section_devices(text):
    """Extract SectionDevice blocks (name, comment, jack gate, capture PCM).

    Only literal values are resolved.  UCM variables and macros are left to
    the shipped profile; the override file this repo installs uses literals on
    purpose so the state can be verified without a UCM interpreter.
    """
    devices = {}
    for match in SECTION_DEVICE_RE.finditer(text):
        name = match.group(1)
        start = text.find("{", match.end())
        if start < 0:
            continue
        depth = 0
        end = start
        in_string = False
        while end < len(text):
            ch = text[end]
            if ch == '"' and (end == 0 or text[end - 1] != "\\"):
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            end += 1
        body = text[start:end + 1]
        devices[name] = {
            "name": name,
            "comment": _literal(body, r'Comment\s+"([^"]*)"'),
            "jack_control": _literal(body, r'JackControl\s+"([^"]*)"'),
            "capture_pcm": _literal(body, r'CapturePCM\s+"([^"]*)"'),
        }
    return devices


def _literal(text, pattern):
    found = re.search(pattern, text)
    return found.group(1) if found else ""


INCLUDE_FILE_RE = re.compile(r'Include\.[A-Za-z0-9_.\-]+\.File\s+"([^"]+)"')


def _resolve_ucm_path(root, base_dir, target):
    if os.path.isabs(target):
        return os.path.join(root, target.lstrip("/"))
    return os.path.normpath(os.path.join(base_dir, target))


def ucm_load_includes(path, root, depth=0, seen=None):
    """Read a UCM file and, recursively, every `Include.<id>.File` target.

    Returns [(path, text)] in evaluation order.  UCM evaluates includes in file
    order and merges each with snd_config_merge(parent, included, 0), which
    substitutes duplicate scalar values - i.e. the later include wins.
    """
    if seen is None:
        seen = set()
    if depth > 4 or path in seen or not os.path.isfile(path):
        return []
    seen.add(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    loaded = [(path, text)]
    for target in INCLUDE_FILE_RE.findall(text):
        loaded.extend(ucm_load_includes(
            _resolve_ucm_path(root, os.path.dirname(path), target), root,
            depth + 1, seen))
    return loaded


def ucm_machine_conf(ucm2_root, driver, longname):
    """The card configuration UCM2 picks first (ucm2/ucm.conf 'confd1')."""
    base = os.path.join(ucm2_root, "conf.d", driver)
    for name in (longname, driver):
        if not name:
            continue
        path = os.path.join(base, name + ".conf")
        if os.path.isfile(path):
            return path
    return None


def ucm_override_state(ucm2_root, driver, longname):
    """Is the always-available internal-mic device installed and well formed?

    The installed layout is a machine card profile plus three include files
    under <ucm2>/oxp3/ (card-level override, HiFi wrapper, microphone override).
    Every one of our files carries OVERRIDE_MARKER; the effective use-case File
    is the *last* SectionUseCase merge in evaluation order, and the microphone
    device is the SectionDevice merged last.
    """
    state = {
        "machine_conf": None,
        "card_conf": None,
        "override_installed": False,
        "mic_device": None,
        "phantom_jack": False,
        "files": [],
    }
    conf = ucm_machine_conf(ucm2_root, driver, longname)
    state["card_conf"] = conf
    machine = os.path.join(ucm2_root, "conf.d", driver, longname + ".conf") if longname else None
    if machine and os.path.isfile(machine):
        state["machine_conf"] = machine
    if not conf:
        return state
    try:
        with open(conf, encoding="utf-8", errors="replace") as fh:
            card_text = fh.read()
    except OSError:
        return state
    if OVERRIDE_MARKER not in card_text:
        return state

    card_tree = ucm_load_includes(conf, ucm2_root)
    ours = [path for path, text in card_tree if OVERRIDE_MARKER in text]
    if conf not in ours:
        return state

    # the effective use-case file: the last SectionUseCase."HiFi" File merged
    use_case = None
    for path, text in card_tree:
        for target in UCM_USE_CASE_FILE_RE.findall(text):
            use_case = _resolve_ucm_path(ucm2_root, os.path.dirname(path), target)
    if not use_case or not os.path.isfile(use_case):
        return state
    try:
        with open(use_case, encoding="utf-8", errors="replace") as fh:
            use_text = fh.read()
    except OSError:
        return state
    if OVERRIDE_MARKER not in use_text:
        return state
    # the wrapper must keep the shipped use case (playback devices) in place
    if "HiFi.conf" not in use_text or "Include" not in use_text:
        return state

    use_tree = ucm_load_includes(use_case, ucm2_root)
    ours.extend(path for path, text in use_tree if OVERRIDE_MARKER in text)
    devices = {}
    for path, text in use_tree:
        devices.update(ucm_section_devices(text))
    mic = devices.get("Mic")
    if mic is None:
        for name in sorted(devices):
            if devices[name]["capture_pcm"] or devices[name]["jack_control"]:
                mic = devices[name]
                break
    if mic is None:
        return state
    state["override_installed"] = True
    state["microphone_file"] = next(
        (path for path, text in use_tree if OVERRIDE_MARKER in text and "SectionDevice" in text),
        use_case)
    state["files"] = sorted(set(ours))
    state["mic_device"] = mic
    state["phantom_jack"] = bool(
        mic["jack_control"] == "" or "phantom" in mic["jack_control"].lower()
        or "internal" in mic["jack_control"].lower()
    )
    return state


def shipped_ucm_mic_gate(ucm2_root):
    """The shipped HDA mic device gate: the external 'Mic Jack' control.

    Returns the control name used for gating, or "" when the generic mic file
    is not installed.
    """
    path = os.path.join(ucm2_root, "HDA", "HiFi-mic.conf")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    if 'Value.JackControl' not in text:
        return ""
    if 'DeviceMicJack "${var:MicJackControl}"' not in text:
        return "Mic Jack"
    return "Mic Jack"


def read_module_params(sys_module="/sys/module"):
    """The HDA/SOF module parameters that matter for the DMIC paths."""
    wanted = {
        "snd_sof_intel_hda_generic": ("dmic_num",),
        "snd_sof_intel_hda_common": ("dmic_num",),
        "snd_hda_intel": ("dmic_detect", "model"),
        "snd_intel_dspcfg": ("dsp_driver",),
    }
    params = {}
    for module, names in wanted.items():
        params[module] = {}
        for name in names:
            path = os.path.join(sys_module, module, "parameters", name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    params[module][name] = fh.read().strip()
            except OSError:
                continue
    return params


def pipewire_sources(pactl_text):
    """Names of the capture sources PipeWire currently exposes."""
    sources = []
    for line in pactl_text.splitlines():
        match = re.match(r"Name:\s*(\S+)", line.strip())
        if match and match.group(1).endswith("source"):
            sources.append(match.group(1))
    return sorted(set(sources))


# --------------------------------------------------------------------------
# decision logic


def evaluate(proc=DEFAULT_PROC, ucm2=DEFAULT_UCM2, amixer_text=None,
             pactl_text=None, sys_module="/sys/module"):
    """Classify the internal-mic exposure from read-only evidence."""
    evidence = []
    limitations = []

    def read(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    report = {
        "schema": 1,
        "card": None,
        "codec": None,
        "mic": None,
        "route": None,
        "pch_dmic": {"capture_pcm_device": None, "dmic_num": None,
                     "nhl_nodes": "DMICs detected in NHLT tables: 0"},
        "ucm": {"machine_conf": None, "override_installed": False,
                "mic_device": None, "phantom_jack": False,
                "shipped_gate": ""},
        "module_params": read_module_params(sys_module),
        "sources": [],
        "verdict": "unknown",
        "summary": "",
        "hint": "",
        "evidence": evidence,
        "limitations": limitations,
    }

    cards_text = read(os.path.join(proc, "cards"))
    if cards_text is None:
        report["summary"] = "no %s/cards; no ALSA sound card visible" % proc
        report["hint"] = "boot the OneXPlayer 3 kernel and rerun inside the desktop session"
        limitations.append("ALSA procfs not readable")
        return report

    cards = parse_cards(cards_text)
    card = None
    for candidate in cards:
        if candidate["driver"] in ("sof-hda-dsp", "HDA-Intel", "snd_soc_skl_hda_dsp"):
            card = candidate
            break
    if card is None and cards:
        card = cards[0]
    if card is None:
        report["summary"] = "no ALSA card in %s/cards" % proc
        report["hint"] = "check dmesg for sof-audio / snd_hda_intel registration failures"
        return report
    report["card"] = card
    evidence.append("card %(index)s: driver=%(driver)s longname=%(longname)s" % card)

    devices_text = read(os.path.join(proc, "devices")) or ""
    captures = parse_devices(devices_text)
    card_captures = [c for c in captures if c["card"] == card["index"]]
    evidence.append("capture PCMs: %s" % (
        ", ".join("hw:%d,%d" % (c["card"], c["device"]) for c in card_captures) or "none"))
    for capture in card_captures:
        if capture["device"] == 6:
            report["pch_dmic"]["capture_pcm_device"] = 6
    params = report["module_params"]
    for module in ("snd_sof_intel_hda_generic", "snd_sof_intel_hda_common"):
        value = params.get(module, {}).get("dmic_num")
        if value not in (None, "-1", ""):
            report["pch_dmic"]["dmic_num"] = value

    # codec dump: prefer the codec that declares an internal mic pin
    card_dir = os.path.join(proc, "card%d" % card["index"])
    codecs = []
    try:
        for entry in sorted(os.listdir(card_dir)):
            if entry.startswith("codec#"):
                text = read(os.path.join(card_dir, entry))
                if text:
                    codecs.append((entry, text))
    except OSError:
        limitations.append("no %s directory" % card_dir)
    codec_name = ""
    widgets = {}
    mic = None
    for entry, text in codecs:
        found = re.search(r"^Codec:\s*(.+)$", text, re.M)
        if found and not codec_name:
            codec_name = found.group(1).strip()
        candidate = internal_mic_pins(parse_widgets(text))
        if candidate:
            codec_name = found.group(1).strip() if found else codec_name
            widgets = parse_widgets(text)
            mic = candidate[0]
            report["codec_file"] = entry
            break
    report["codec"] = codec_name

    if mic:
        mic["nid_int"] = int(mic["nid"], 16)
        route = mic_route(widgets, mic["nid_int"])
        report["mic"] = {k: v for k, v in mic.items() if k != "nid_int"}
        report["route"] = route
        evidence.append(
            "internal mic pin %s: default=%s device=%s connection=%s location=%s no_presence=%s pin_ctls=%s"
            % (mic["nid"], mic["value"], mic["device"], mic["connection"],
               mic["location"], mic["no_presence"], mic["pin_ctls"]))
        if route:
            evidence.append(
                "route: ADC %s <- mixer %s input #%d (Amp-In %s)"
                % (route["adc"], route["mixer"], route["index"],
                   "muted" if route["muted"] else "un-muted"))
        else:
            limitations.append("codec widget route from the mic pin to the capture ADC not resolved")
    else:
        evidence.append("no codec pin declares a fixed internal microphone")

    controls = parse_amixer_controls(amixer_text) if amixer_text else {}
    phantom = controls.get("Internal Mic Phantom Jack", {}).get("value", "")
    mic_jack = ""
    for name in ("Mic Jack", "Headset Mic Jack", "Headphone Mic Jack"):
        if name in controls:
            mic_jack = "%s=%s" % (name, controls[name]["value"])
            break
    capture_switch = controls.get("Capture Switch", {}).get("value", "")
    if controls:
        evidence.append("amixer: %s%s%s" % (
            ("Internal Mic Phantom Jack=%s " % phantom) if phantom else "",
            (mic_jack + " ") if mic_jack else "",
            ("Capture Switch=%s" % capture_switch) if capture_switch else ""))
    report["jack"] = {"internal_phantom": phantom, "external_mic": mic_jack,
                      "capture_switch": capture_switch}

    ucm = report["ucm"]
    ucm["shipped_gate"] = shipped_ucm_mic_gate(ucm2)
    state = ucm_override_state(ucm2, card["driver"], card["longname"])
    ucm.update({
        "machine_conf": state["machine_conf"],
        "card_conf": state["card_conf"],
        "override_installed": state["override_installed"],
        "mic_device": state["mic_device"],
        "phantom_jack": state["phantom_jack"],
    })
    if state.get("files"):
        ucm["files"] = state["files"]
    if state["machine_conf"]:
        evidence.append("UCM machine profile: %s" % state["machine_conf"])
    else:
        evidence.append("UCM: shipped %s profile applies (no machine override)"
                        % (state["card_conf"] or card["driver"] + ".conf"))
    if state["mic_device"]:
        evidence.append("UCM mic device '%s': comment=%r jack=%r pcm=%r" % (
            state["mic_device"]["name"], state["mic_device"]["comment"],
            state["mic_device"]["jack_control"], state["mic_device"]["capture_pcm"]))

    if pactl_text:
        report["sources"] = pipewire_sources(pactl_text)
        if report["sources"]:
            evidence.append("PipeWire sources: %s" % ", ".join(report["sources"]))

    # ---- verdict ---------------------------------------------------------
    if mic:
        if report["pch_dmic"]["capture_pcm_device"] == 6:
            evidence.append("note: a PCH DMIC PCM (hw:%d,6) also exists" % card["index"])
        if state["override_installed"] and state["phantom_jack"]:
            if phantom in ("off", "0", "false") and report["route"]:
                report["verdict"] = "gated"
                report["summary"] = (
                    "internal mic pin %s is routed, but its phantom jack control "
                    "reads %r" % (mic["nid"], phantom))
                report["hint"] = ("amixer -c %d cset name='Internal Mic Phantom Jack' on ; "
                                  "sudo tools/audio/internal-mic.sh status"
                                  % card["index"])
            else:
                report["verdict"] = "exposed"
                report["summary"] = (
                    "internal mic pin %s (%s, %s) routed to ADC %s via mixer %s; "
                    "UCM exposes an always-available capture device (%r, jack %r)"
                    % (mic["nid"], mic["device"], mic["connection"],
                       (report["route"] or {}).get("adc", "?"),
                       (report["route"] or {}).get("mixer", "?"),
                       state["mic_device"]["comment"] or state["mic_device"]["name"],
                       state["mic_device"]["jack_control"]))
                report["hint"] = ""
        else:
            report["verdict"] = "gated"
            report["summary"] = (
                "internal mic pin %s (%s, %s) exists and is %s, but no always-available "
                "capture device exists: the shipped UCM profile gates its only mic device "
                "on the external %s, which reads %s"
                % (mic["nid"], mic["device"], mic["connection"],
                   "routed to the capture ADC" if report["route"]
                   and report["route"]["muted"] is False
                   else "present but not confirmed routed",
                   ucm["shipped_gate"] or "mic jack", mic_jack or "off"))
            report["hint"] = ("sudo tools/audio/internal-mic.sh install ; "
                              "systemctl --user restart pipewire wireplumber ; "
                              "docs/hw/audio-microphone.md")
    elif report["pch_dmic"]["capture_pcm_device"] == 6 or report["pch_dmic"]["dmic_num"]:
        report["verdict"] = "pch-dmic"
        report["summary"] = (
            "no codec internal-mic pin; a PCH DMIC capture PCM exists (hw:%d,6, dmic_num=%s)"
            % (card["index"], report["pch_dmic"]["dmic_num"] or "n/a"))
        report["hint"] = "the internal mic is a PCH DMIC here; see docs/hw/audio-microphone.md"
    else:
        report["verdict"] = "absent"
        report["summary"] = (
            "the codec pin configuration declares no fixed internal microphone and no "
            "PCH DMIC capture PCM exists")
        report["hint"] = ("if the device is supposed to have an internal mic, capture a "
                          "codec dump and open docs/hw/audio-microphone.md")
    return report


# --------------------------------------------------------------------------
# CLI


def _run(cmd, timeout=10):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else proc.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Read-only diagnosis of the OneXPlayer 3 internal microphone "
                    "(ALC245 pin 0x12 codec DMIC vs PCH DMIC vs UCM gating).",
        epilog="Nothing is written, played or captured; see docs/hw/audio-microphone.md.")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--proc", default=DEFAULT_PROC, help="ALSA procfs dir (default %(default)s)")
    parser.add_argument("--ucm2", default=DEFAULT_UCM2, help="UCM2 tree (default %(default)s)")
    parser.add_argument("--amixer-file", default=None,
                        help="read `amixer -c N contents` output from this file instead of running it")
    parser.add_argument("--pactl-file", default=None,
                        help="read `pactl list sources` output from this file instead of running it")
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, VERSION))
    args = parser.parse_args(argv)

    amixer_text = None
    if args.amixer_file:
        try:
            with open(args.amixer_file, encoding="utf-8", errors="replace") as fh:
                amixer_text = fh.read()
        except OSError as exc:
            print("%s: cannot read %s: %s" % (PROG, args.amixer_file, exc), file=sys.stderr)
            return 2
    elif args.proc == DEFAULT_PROC and _which("amixer"):
        amixer_text = _run(["amixer", "-c", "0", "contents"])

    pactl_text = None
    if args.pactl_file:
        try:
            with open(args.pactl_file, encoding="utf-8", errors="replace") as fh:
                pactl_text = fh.read()
        except OSError as exc:
            print("%s: cannot read %s: %s" % (PROG, args.pactl_file, exc), file=sys.stderr)
            return 2
    elif args.proc == DEFAULT_PROC and _which("pactl"):
        pactl_text = _run(["pactl", "list", "sources"])

    report = evaluate(proc=args.proc, ucm2=args.ucm2, amixer_text=amixer_text,
                      pactl_text=pactl_text)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    card = report.get("card") or {}
    print("%s %s" % (PROG, VERSION))
    print("card:   %s%s" % (card.get("driver", "?"),
                            (" (%s)" % card.get("longname")) if card.get("longname") else ""))
    print("codec:  %s" % (report.get("codec") or "?"))
    print("verdict: %s" % report["verdict"])
    print("  %s" % report["summary"])
    if report.get("hint"):
        print("  fix: %s" % report["hint"])
    print("evidence:")
    for line in report["evidence"]:
        print("  - %s" % line)
    for line in report["limitations"]:
        print("  ? %s" % line)
    return 0


def _which(name):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = os.path.join(directory, name)
        if os.access(path, os.X_OK):
            return path
    return None


if __name__ == "__main__":
    sys.exit(main())
