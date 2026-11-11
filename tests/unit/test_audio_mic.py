# SPDX-License-Identifier: MIT
"""Unit tests for the internal-microphone diagnosis (tools/audio/mic_diag.py).

Everything here is synthetic: a fake /proc/asound tree (cards, devices and the
two ALC245 codec nodes that matter), a fake UCM2 tree with the shipped HiFi
mic file and the repo's own machine profile, and a recorded `amixer contents`
fixture.  No card, no capture, no root, no writes outside the temporary tree.

The cases pin the decision the verifier depends on:

* codec DMIC pin 0x12 present + shipped profile gates the mic on "Mic Jack"
  -> ``gated`` (the opt-in override is not installed yet)
* the same, with the repo's two UCM files installed
  -> ``exposed``
* a PCH DMIC capture PCM instead of a codec pin -> ``pch-dmic``
* no internal mic in the pin configuration -> ``absent``
* no card -> ``unknown``
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import unittest
from contextlib import redirect_stdout

import support

mic = support.load("audio_mic", "audio/mic_diag.py")

REPO = support.REPO

CARDS = """\
 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp
                      ONE_NETBOOK-ONEXPLAYER3-Defaultstring
"""

DEVICES = """\
  3: [ 0- 0]: digital audio capture
  7: [ 0- 3]: digital audio playback
  8: [ 0- 2]: hardware dependent
 33:        : timer
"""

# The ALC245 widgets this diagnosis uses: ADCs 0x07..0x0a, the DMIC pin 0x12,
# the external mic jack 0x19 and the mixers/selectors between them.  Values are
# copied from the live codec dump (the parts that matter here).
CODEC_ALC245 = """\
Codec: Realtek ALC245
Address: 0
Vendor Id: 0x10ec0245
Subsystem Id: 0x1f751602

Node 0x07 [Audio Input] wcaps 0x10051b: Stereo Amp-In
  Converter: stream=0, channel=0
  Connection: 1
     0x24
Node 0x08 [Audio Input] wcaps 0x10051b: Stereo Amp-In
  Control: name="Capture Volume", index=0, device=0
  Control: name="Capture Switch", index=0, device=0
  Converter: stream=0, channel=0
  Connection: 1
     0x23
Node 0x09 [Audio Input] wcaps 0x10051b: Stereo Amp-In
  Connection: 1
     0x22
Node 0x0a [Audio Input] wcaps 0x10051b: Stereo Amp-In
  Connection: 1
     0x25
Node 0x12 [Pin Complex] wcaps 0x40040b: Stereo Amp-In
  Control: name="Internal Mic Boost Volume", index=0, device=0
  Pincap 0x00000020: IN
  Pin Default 0x90a60130: [Fixed] Mic at Int N/A
  Pin-ctls: 0x20: IN
Node 0x13 [Pin Complex] wcaps 0x40040b: Stereo Amp-In
  Pincap 0x00000020: IN
  Pin Default 0x40000000: [N/A] Line Out at Ext N/A
  Pin-ctls: 0x00:
Node 0x19 [Pin Complex] wcaps 0x40048b: Stereo Amp-In
  Control: name="Mic Boost Volume", index=0, device=0
  Pincap 0x00003724: IN Detect
  Pin Default 0x04a19040: [Jack] Mic at Ext Right
  Pin-ctls: 0x24: IN VREF_80
Node 0x22 [Audio Mixer] wcaps 0x20010b: Stereo Amp-In
  Amp-In vals:  [0x80 0x80] [0x80 0x80] [0x80 0x80] [0x80 0x80] [0x80 0x80]
  Connection: 5
     0x19 0x1a 0x1b 0x1d 0x13
Node 0x23 [Audio Mixer] wcaps 0x20010b: Stereo Amp-In
  Amp-In vals:  [0x80 0x80] [0x80 0x80] [0x80 0x80] [0x80 0x80] [0x00 0x00]
  Connection: 5
     0x19 0x1a 0x1b 0x1d 0x12
Node 0x24 [Audio Selector] wcaps 0x300101: Stereo
  Connection: 3
     0x12* 0x13 0x18
Node 0x25 [Audio Selector] wcaps 0x300101: Stereo
  Connection: 2
     0x12* 0x13
"""

# Only jack microphones: no fixed internal pin, so there is nothing to expose.
CODEC_NO_INT_MIC = """\
Codec: Realtek ALC256
Address: 0
Node 0x19 [Pin Complex] wcaps 0x40048b: Stereo Amp-In
  Pincap 0x00003724: IN Detect
  Pin Default 0x04a19040: [Jack] Mic at Ext Right
  Pin-ctls: 0x24: IN VREF_80
"""

AMIXER = """\
numid=6,iface=MIXER,name='Capture Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=63,step=0
  : values=38,38
numid=7,iface=MIXER,name='Capture Switch'
  ; type=BOOLEAN,access=rw------,values=2
  : values=off,off
numid=8,iface=MIXER,name='Internal Mic Boost Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=3,step=0
  : values=0,0
numid=12,iface=CARD,name='Internal Mic Phantom Jack'
  ; type=BOOLEAN,access=r-------,values=1
  : values=on
numid=13,iface=CARD,name='Mic Jack'
  ; type=BOOLEAN,access=r-------,values=1
  : values=off
"""

SHIPPED_CARD = """\
Syntax 7

SectionUseCase."HiFi" {
	File "/Intel/sof-hda-dsp/HiFi.conf"
	Comment "Play HiFi quality Music"
}
"""

SHIPPED_MIC = """\
# Generic handling of analog microphones on HDA devices
Define.DeviceMicName ""
If.micsetup {
	Condition {
		Type String
		Empty "${var:SourceControl}"
	}
	False.EnableSequence [
		cset "name='${var:SourceControl}' '${var:__MicName}'"
	]
}
If.jack {
	False.Value.JackControl "${var:__JackName}"
}
If.mic1 {
	False.Macro.0.HDACaptureDevice {
		DevName "${var:DeviceMic}"
		MicName "${var:DeviceMicName}"
		JackName "${var:DeviceMicJack}"
	}
}
"""


class TreeCase(unittest.TestCase):
    """Common synthetic /proc + UCM tree."""

    def setUp(self):
        self.proc = support.TmpTree("oxp3-mic-proc-")
        self.ucm = support.TmpTree("oxp3-mic-ucm-")
        self.addCleanup(self.proc.cleanup)
        self.addCleanup(self.ucm.cleanup)
        self.proc.write("cards", CARDS)
        self.proc.write("devices", DEVICES)
        self.proc.write("card0/codec#0", CODEC_ALC245)
        self.ucm.write("conf.d/sof-hda-dsp/sof-hda-dsp.conf", SHIPPED_CARD)
        self.ucm.write("HDA/HiFi-mic.conf", SHIPPED_MIC)
        self.ucm.write("Intel/sof-hda-dsp/HiFi.conf", 'SectionVerb {\n}\n')

    def install_override(self):
        """Copy the repo's real UCM files into the synthetic tree (4 files)."""
        self.ucm.write("conf.d/sof-hda-dsp/ONE_NETBOOK-ONEXPLAYER3-Defaultstring.conf",
                       (REPO / "files/ucm2/oxp3-onEXPLAYER3.conf").read_text())
        for name in ("oxp3-onEXPLAYER3-usecase.conf", "oxp3-onEXPLAYER3-HiFi.conf",
                     "oxp3-onEXPLAYER3-mic.conf"):
            self.ucm.write("oxp3/" + name, (REPO / "files/ucm2" / name).read_text())

    def evaluate(self, **kwargs):
        return mic.evaluate(proc=str(self.proc.root), ucm2=str(self.ucm.root),
                            amixer_text=kwargs.pop("amixer_text", AMIXER), **kwargs)


class PinDefaultTest(unittest.TestCase):
    def test_fixed_internal_digital_mic(self):
        d = mic.decode_pin_default(0x90A60130)
        self.assertEqual(d["connectivity"], "fixed")
        self.assertEqual(d["location"], "internal")
        self.assertEqual(d["device"], "mic-in")
        self.assertEqual(d["connection"], "digital")
        self.assertTrue(d["no_presence"])
        self.assertEqual(d["value"], "0x90a60130")

    def test_external_jack_mic(self):
        d = mic.decode_pin_default(0x04A19040)
        self.assertEqual(d["connectivity"], "jack")
        self.assertEqual(d["location"], "right")
        self.assertEqual(d["device"], "mic-in")
        self.assertEqual(d["connection"], "1/8\"")

    def test_disabled_pin(self):
        d = mic.decode_pin_default(0x411111F0)
        self.assertEqual(d["connectivity"], "none")
        self.assertEqual(d["device"], "speaker")

    def test_accepts_string(self):
        self.assertEqual(mic.decode_pin_default("0x90a60130")["device"], "mic-in")

    def test_location_nibble_masks_to_internal(self):
        # 0x10..0x1f are all the "internal" location field in the parser
        self.assertEqual(mic.decode_pin_default(0x90130130)["location"], "internal")
        self.assertEqual(mic.decode_pin_default(0x901F0130)["location"], "internal")


class ParserTest(TreeCase):
    def test_parse_cards(self):
        cards = mic.parse_cards(CARDS)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["driver"], "sof-hda-dsp")
        self.assertEqual(cards[0]["longname"], "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")

    def test_parse_devices_only_capture(self):
        captures = mic.parse_devices(DEVICES)
        self.assertEqual(captures, [{"card": 0, "device": 0}])

    def test_parse_widgets_multiple_nids_per_connection_line(self):
        widgets = mic.parse_widgets(CODEC_ALC245)
        self.assertEqual(widgets[0x23]["connections"], [0x19, 0x1A, 0x1B, 0x1D, 0x12])
        self.assertEqual(widgets[0x24]["selected"], 0x12)
        self.assertEqual(len(widgets[0x23]["amp_in"]), 10)

    def test_internal_mic_pin_is_0x12(self):
        pins = mic.internal_mic_pins(mic.parse_widgets(CODEC_ALC245))
        self.assertEqual([p["nid"] for p in pins], ["0x12"])
        self.assertTrue(pins[0]["digital"])
        self.assertIn("Internal Mic Boost Volume", pins[0]["controls"])

    def test_jack_mic_is_not_internal(self):
        pins = mic.internal_mic_pins(mic.parse_widgets(CODEC_ALC245))
        self.assertNotIn("0x19", [p["nid"] for p in pins])

    def test_capture_adc_prefers_the_named_adc(self):
        self.assertEqual(mic.capture_adc(mic.parse_widgets(CODEC_ALC245)), 0x08)

    def test_mic_route_follows_adc_to_mixer_input(self):
        widgets = mic.parse_widgets(CODEC_ALC245)
        route = mic.mic_route(widgets, 0x12)
        self.assertEqual(route["adc"], "0x08")
        self.assertEqual(route["mixer"], "0x23")
        self.assertEqual(route["index"], 4)
        self.assertFalse(route["muted"])

    def test_mic_route_missing_pin(self):
        self.assertIsNone(mic.mic_route(mic.parse_widgets(CODEC_ALC245), 0x1F))

    def test_parse_amixer_controls(self):
        controls = mic.parse_amixer_controls(AMIXER)
        self.assertEqual(controls["Mic Jack"]["value"], "off")
        self.assertEqual(controls["Internal Mic Phantom Jack"]["value"], "on")
        self.assertEqual(controls["Capture Switch"]["type"], "BOOLEAN")

    def test_ucm_section_devices(self):
        text = """\
SectionDevice."Mic" {
	Comment "Internal Microphone"
	Value {
		JackControl "Internal Mic Phantom Jack"
		CapturePCM "hw:0,0"
	}
}
SectionDevice."Speaker" {
	Comment "Speaker"
}
"""
        devices = mic.ucm_section_devices(text)
        self.assertEqual(sorted(devices), ["Mic", "Speaker"])
        self.assertEqual(devices["Mic"]["jack_control"], "Internal Mic Phantom Jack")
        self.assertEqual(devices["Mic"]["capture_pcm"], "hw:0,0")
        self.assertEqual(devices["Speaker"]["capture_pcm"], "")

    def test_shipped_gate_detected(self):
        self.assertEqual(mic.shipped_ucm_mic_gate(str(self.ucm.root)), "Mic Jack")

    def test_shipped_gate_absent_when_file_missing(self):
        self.assertEqual(mic.shipped_ucm_mic_gate(str(self.proc.root)), "")

    def test_read_module_params(self):
        tree = support.TmpTree("oxp3-mic-sys-")
        self.addCleanup(tree.cleanup)
        tree.write("snd_sof_intel_hda_generic/parameters/dmic_num", "-1")
        tree.write("snd_hda_intel/parameters/dmic_detect", "Y")
        params = mic.read_module_params(str(tree.root))
        self.assertEqual(params["snd_sof_intel_hda_generic"]["dmic_num"], "-1")
        self.assertEqual(params["snd_hda_intel"]["dmic_detect"], "Y")


class UcmOverrideTest(TreeCase):
    def test_not_installed_with_shipped_profile(self):
        state = mic.ucm_override_state(str(self.ucm.root), "sof-hda-dsp",
                                       "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
        self.assertFalse(state["override_installed"])
        self.assertIsNone(state["machine_conf"])

    def test_installed_uses_phantom_jack(self):
        self.install_override()
        state = mic.ucm_override_state(str(self.ucm.root), "sof-hda-dsp",
                                       "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
        self.assertTrue(state["override_installed"])
        self.assertTrue(state["phantom_jack"])
        self.assertEqual(state["mic_device"]["comment"], "Internal Microphone")
        self.assertEqual(state["mic_device"]["jack_control"],
                         "Internal Mic Phantom Jack")

    def test_wrapper_without_shipped_include_is_not_valid(self):
        self.install_override()
        self.ucm.write("oxp3/oxp3-onEXPLAYER3-HiFi.conf",
                       "# OXP3-ONEXPLAYER3-MIC\n"
                       'Include.oxp3-mic.File "/oxp3/oxp3-onEXPLAYER3-mic.conf"\n')
        state = mic.ucm_override_state(str(self.ucm.root), "sof-hda-dsp",
                                       "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
        self.assertFalse(state["override_installed"])

    def test_missing_card_level_override_is_not_installed(self):
        self.install_override()
        (self.ucm.root / "oxp3/oxp3-onEXPLAYER3-usecase.conf").unlink()
        state = mic.ucm_override_state(str(self.ucm.root), "sof-hda-dsp",
                                       "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
        self.assertFalse(state["override_installed"])

    def test_installed_override_declares_a_complete_device(self):
        self.install_override()
        self.install_override()  # idempotent
        state = mic.ucm_override_state(str(self.ucm.root), "sof-hda-dsp",
                                       "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
        device = state["mic_device"]
        self.assertEqual(device["capture_pcm"], "hw:${CardId}")
        self.assertEqual(len(state["files"]), 4)

    def test_foreign_machine_file_is_not_ours(self):
        self.ucm.write("conf.d/sof-hda-dsp/ONE_NETBOOK-ONEXPLAYER3-Defaultstring.conf",
                       SHIPPED_CARD)
        state = mic.ucm_override_state(str(self.ucm.root), "sof-hda-dsp",
                                       "ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
        self.assertFalse(state["override_installed"])


def _machine_is_onexplayer3():
    try:
        text = pathlib.Path("/proc/asound/cards").read_text()
    except OSError:
        return False
    return "ONEXPLAYER3" in text or "ONE_NETBOOK" in text


class InstallerScratchTest(unittest.TestCase):
    """Run the real installer in a scratch UCM prefix (no root, no hardware).

    The script refuses unless the live card is an OneXPlayer 3 SOF card, so the
    whole class is skipped on other machines and in containers without a card.
    """

    def setUp(self):
        self.tmp = support.TmpTree("oxp3-mic-install-")
        self.addCleanup(self.tmp.cleanup)
        self.ucm = self.tmp.path("ucm")
        self.state = self.tmp.path("state")
        # the two shipped files the installer insists on, plus the gate file
        self.tmp.write("ucm/conf.d/sof-hda-dsp/sof-hda-dsp.conf", SHIPPED_CARD)
        self.tmp.write("ucm/Intel/sof-hda-dsp/HiFi.conf", "SectionVerb {\n}\n")
        self.tmp.write("ucm/HDA/HiFi-mic.conf", SHIPPED_MIC)

    def run_script(self, *args):
        return subprocess.run(
            [str(REPO / "tools/audio/internal-mic.sh"), *args,
             "--ucm2", str(self.ucm), "--state", str(self.state)],
            capture_output=True, text=True, timeout=60)

    def targets(self):
        return [
            self.ucm / "conf.d/sof-hda-dsp/ONE_NETBOOK-ONEXPLAYER3-Defaultstring.conf",
            self.ucm / "oxp3/oxp3-onEXPLAYER3-usecase.conf",
            self.ucm / "oxp3/oxp3-onEXPLAYER3-HiFi.conf",
            self.ucm / "oxp3/oxp3-onEXPLAYER3-mic.conf",
        ]

    @unittest.skipUnless(_machine_is_onexplayer3(),
                         "needs the live OneXPlayer 3 sof-hda-dsp card for card detection")
    def test_install_and_undo_roundtrip(self):
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.targets():
            self.assertTrue(path.is_file(), path)
            self.assertIn(b"OXP3-ONEXPLAYER3-MIC", path.read_bytes())
        self.assertTrue((self.state / "audio-mic.manifest").is_file())
        report = mic.evaluate(proc="/proc/asound", ucm2=str(self.ucm), amixer_text=AMIXER)
        self.assertEqual(report["verdict"], "exposed")

        # undo removes only our files and keeps a foreign file in our directory
        foreign = self.ucm / "oxp3/foreign.conf"
        foreign.write_text("not ours\n")
        result = self.run_script("undo")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.targets():
            self.assertFalse(path.exists(), path)
        self.assertTrue(foreign.is_file())
        self.assertFalse((self.state / "audio-mic.manifest").exists())
        report = mic.evaluate(proc="/proc/asound", ucm2=str(self.ucm), amixer_text=AMIXER)
        self.assertEqual(report["verdict"], "gated")

    @unittest.skipUnless(_machine_is_onexplayer3(),
                         "needs the live OneXPlayer 3 sof-hda-dsp card for card detection")
    def test_install_refuses_foreign_machine_profile(self):
        target = self.targets()[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SHIPPED_CARD)
        result = self.run_script("install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not ours", result.stderr)
        self.assertFalse(self.targets()[1].exists())

    @unittest.skipUnless(_machine_is_onexplayer3(),
                         "needs the live OneXPlayer 3 sof-hda-dsp card for card detection")
    def test_dry_run_writes_nothing(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self.targets():
            self.assertFalse(path.exists(), path)


class EvaluateTest(TreeCase):
    def test_gated_without_override(self):
        report = self.evaluate()
        self.assertEqual(report["verdict"], "gated")
        self.assertIn("Mic Jack", report["summary"])
        self.assertIn("internal-mic.sh install", report["hint"])
        self.assertEqual(report["mic"]["nid"], "0x12")
        self.assertEqual(report["route"]["adc"], "0x08")

    def test_exposed_with_override(self):
        self.install_override()
        report = self.evaluate()
        self.assertEqual(report["verdict"], "exposed")
        self.assertIn("Internal Microphone", report["summary"])

    def test_override_but_phantom_jack_off_is_gated(self):
        self.install_override()
        amixer = AMIXER.replace("  : values=on\n", "  : values=off\n")
        report = self.evaluate(amixer_text=amixer)
        self.assertEqual(report["verdict"], "gated")
        self.assertIn("phantom jack", report["summary"])

    def test_pch_dmic(self):
        self.proc.write("card0/codec#0", CODEC_NO_INT_MIC)
        self.proc.write("devices", DEVICES.replace(
            "  3: [ 0- 0]: digital audio capture",
            "  3: [ 0- 0]: digital audio capture\n  9: [ 0- 6]: digital audio capture"))
        report = self.evaluate()
        self.assertEqual(report["verdict"], "pch-dmic")
        self.assertEqual(report["pch_dmic"]["capture_pcm_device"], 6)

    def test_dmic_num_param_counted_as_pch_dmic(self):
        self.proc.write("card0/codec#0", CODEC_NO_INT_MIC)
        sysroot = support.TmpTree("oxp3-sys-")
        self.addCleanup(sysroot.cleanup)
        sysroot.write("snd_sof_intel_hda_generic/parameters/dmic_num", "2")
        report = mic.evaluate(proc=str(self.proc.root), ucm2=str(self.ucm.root),
                              amixer_text=AMIXER, sys_module=str(sysroot.root))
        self.assertEqual(report["verdict"], "pch-dmic")
        self.assertEqual(report["pch_dmic"]["dmic_num"], "2")

    def test_absent_without_internal_pin(self):
        self.proc.write("card0/codec#0", CODEC_NO_INT_MIC)
        report = self.evaluate()
        self.assertEqual(report["verdict"], "absent")
        self.assertIsNone(report["mic"])

    def test_unknown_without_card(self):
        self.proc.write("cards", "")
        report = self.evaluate()
        self.assertEqual(report["verdict"], "unknown")
        self.assertIsNone(report["card"])

    def test_json_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = mic.main(["--json", "--proc", str(self.proc.root),
                           "--ucm2", str(self.ucm.root),
                           "--amixer-file", str(self.proc.write("amixer.txt", AMIXER))])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["schema"], 1)
        self.assertIn(report["verdict"], ("gated", "exposed"))
        self.assertEqual(report["codec"], "Realtek ALC245")

    def test_human_output_names_the_verdict(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = mic.main(["--proc", str(self.proc.root), "--ucm2", str(self.ucm.root),
                           "--amixer-file", str(self.proc.write("amixer2.txt", AMIXER))])
        self.assertEqual(rc, 0)
        self.assertIn("verdict: gated", out.getvalue())

    def test_bad_amixer_file_is_usage_error(self):
        with redirect_stdout(io.StringIO()):
            rc = mic.main(["--proc", str(self.proc.root), "--ucm2", str(self.ucm.root),
                           "--amixer-file", str(self.proc.root / "nope.txt")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
