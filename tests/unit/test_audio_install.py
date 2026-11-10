"""Unit tests for tools/audio/install-user.sh and tools/audio/gain-stage.sh.

The installer is user-scope, so it can be exercised end-to-end in a throw-away
HOME: stub `pactl`/`systemctl`/`amixer` on PATH, and every path the script
writes (XDG_CONFIG_HOME, XDG_STATE_HOME, ~/.local) lands in a temporary tree.
Nothing outside the tree is touched; the stub `amixer` is a small state file.

The cases pin the contract the owner relies on:
  * the default install generates the loud (-4 dB) chain and the boot unit;
  * --safe generates the -8 dB chain; both are selectable;
  * re-running is idempotent and keeps the first backup;
  * --undo restores a pre-existing drop-in byte-identically (or removes one it
    created) and restores the ALSA values;
  * the boot unit's contents are what systemd will run.
"""

from __future__ import annotations

import os
import stat
import subprocess
import unittest

import support

AUDIO = support.REPO / "tools" / "audio"
INSTALL = AUDIO / "install-user.sh"
GAIN = AUDIO / "gain-stage.sh"
UNIT = AUDIO / "oxp3-speaker-gain.service"
SINK = ("alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic."
        "HiFi__Speaker__sink")

STUB_AMIXER = r"""#!/bin/bash
# Minimal speaker control: `-c N sget Speaker` / `-c N sset Speaker VALUE`.
state=${STUB_AMIXER_STATE:?}
value=$(cat "$state" 2>/dev/null || echo 82)
printf '%s\n' "$*" >>"$STUB_AMIXER_LOG"
if [ "${STUB_NO_CONTROLS:-0}" = 1 ]; then exit 1; fi
case "$3" in
    sget)
        [ "$4" = Speaker ] || exit 1
        if [ "$value" = 87 ]; then pct=100; db=0.00; else pct=94; db=-3.75; fi
        cat <<EOF
Simple mixer control 'Speaker',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 87
  Mono:
  Front Left: Playback $value [$pct%] [$db' 'dB] [on]
  Front Right: Playback $value [$pct%] [$db' 'dB] [on]
EOF
        ;;
    sset)
        [ "$4" = Speaker ] || exit 1
        # amixer accepts both "87" and "87,87"; this control takes either
        printf '%s' "${5%%,*}" >"$state"
        ;;
    *) exit 1 ;;
esac
"""

STUB_PACTL = r"""#!/bin/bash
if [ "$1" = list ] && [ "$2" = sinks ] && [ "$3" = short ]; then
    printf '37\toxp3_speaker_eq.sink\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED\n'
    printf '114\t%s\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n' "$STUB_SINK"
    exit 0
fi
exit 0
"""

STUB_SYSTEMCTL = r"""#!/bin/bash
printf '%s\n' "$*" >>"$STUB_SYSTEMCTL_LOG"
exit 0
"""


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TmpTree("oxp3-audio-install-")
        self.home = self.tmp.path("home")
        self.config = self.tmp.path("config")
        self.state = self.tmp.path("state")
        self.bin = self.tmp.path("bin")
        for p in (self.home, self.config, self.state, self.bin):
            p.mkdir(parents=True, exist_ok=True)
        self.amixer_state = self.tmp.path("amixer.state")
        self.amixer_state.write_text("82\n")
        self.amixer_log = self.tmp.path("amixer.log")
        self.systemctl_log = self.tmp.path("systemctl.log")
        self._stub("amixer", STUB_AMIXER)
        self._stub("pactl", STUB_PACTL)
        self._stub("systemctl", STUB_SYSTEMCTL)
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.state),
            "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            "STUB_AMIXER_STATE": str(self.amixer_state),
            "STUB_AMIXER_LOG": str(self.amixer_log),
            "STUB_SYSTEMCTL_LOG": str(self.systemctl_log),
            "STUB_SINK": SINK,
            "OXP3_AUDIO_CARD": "0",
            "OXP3_GAIN_WAIT": "0",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _stub(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    def run_install(self, *args):
        return subprocess.run(["bash", str(INSTALL), *args], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, check=False, timeout=60)

    def run_gain(self, *args):
        return subprocess.run(["bash", str(GAIN), *args], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, check=False, timeout=60)

    @property
    def conf(self):
        return self.config / "pipewire" / "pipewire.conf.d" / "51-oxp3-speaker-eq.conf"

    @property
    def unit(self):
        return self.config / "systemd" / "user" / "oxp3-speaker-gain.service"

    @property
    def applier(self):
        return self.home / ".local" / "libexec" / "oxp3-speaker-gain"

    @property
    def meta(self):
        return self.state / "oxp3" / "audio" / "install.meta"

    @property
    def backup(self):
        return self.state / "oxp3" / "audio" / "51-oxp3-speaker-eq.conf.bak"

    @property
    def gain_state(self):
        return self.state / "oxp3" / "audio-gain" / "controls.tsv"

    # ------------------------------------------------------------------ cases
    def test_print_profile(self):
        out = self.run_install("--print-profile").stdout.strip()
        self.assertEqual(out, "loud 2.84")
        out = self.run_install("--print-profile", "--safe").stdout.strip()
        self.assertEqual(out, "safe -8")

    def test_default_installs_loud_chain_unit_and_applier(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout)
        text = self.conf.read_text()
        self.assertIn("# oxp3-profile: loud", text)
        self.assertIn('"Freq" = 20000 "Q" = 0.7 "Gain" = 2.84', text)
        self.assertIn(SINK, text)                  # node.target rewritten
        self.assertNotIn("HiFi__Speaker__sink\"\n", text.split("node.target")[0])
        self.assertTrue(self.unit.is_file())
        self.assertTrue(self.applier.is_file())
        self.assertTrue(os.access(self.applier, os.X_OK))
        self.assertIn("conf=created", self.meta.read_text())
        # the ALSA value was raised and saved exactly once
        self.assertEqual(self.amixer_state.read_text().strip(), "87")
        self.assertIn("Speaker\t0\t82,82", self.gain_state.read_text())
        log = self.systemctl_log.read_text()
        self.assertIn("--user daemon-reload", log)
        self.assertIn("--user enable oxp3-speaker-gain.service", log)

    def test_safe_profile(self):
        r = self.run_install("--safe")
        self.assertEqual(r.returncode, 0, r.stdout)
        text = self.conf.read_text()
        self.assertIn("# oxp3-profile: safe", text)
        self.assertIn('"Freq" = 20000 "Q" = 0.7 "Gain" = -8', text)
        self.assertNotIn('"Freq" = 20000 "Q" = 0.7 "Gain" = 2.84', text)

    def test_reinstall_is_idempotent_and_keeps_one_backup_row(self):
        self.assertEqual(self.run_install().returncode, 0)
        first = self.conf.read_text()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.conf.read_text(), first)
        meta = self.meta.read_text()
        self.assertEqual(meta.count("conf="), 1)
        self.assertEqual(len(self.gain_state.read_text().splitlines()), 1)
        self.assertIn("already at maximum", r.stdout)
        # no second sset: one control, already max
        sset = [ln for ln in self.amixer_log.read_text().splitlines()
                if " sset " in " " + ln]
        self.assertEqual(len(sset), 1, sset)

    def test_undo_restores_a_previous_dropin_byte_identically(self):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        custom = "# hand-written chain\ncontext.modules = [ ]\n"
        self.conf.write_text(custom)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertNotEqual(self.conf.read_text(), custom)
        self.assertEqual(self.backup.read_text(), custom)
        r = self.run_install("--undo")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.conf.read_text(), custom)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.applier.exists())
        self.assertFalse(self.meta.exists())
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.amixer_state.read_text().strip(), "82")
        self.assertIn("drop-in previous", r.stdout)

    def test_undo_removes_a_dropin_it_created(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.assertTrue(self.conf.exists())
        r = self.run_install("--undo")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.gain_state.exists())
        self.assertEqual(self.amixer_state.read_text().strip(), "82")
        self.assertIn("drop-in removed", r.stdout)

    def test_service_unit_contents(self):
        text = UNIT.read_text()
        self.assertIn("[Unit]", text)
        self.assertIn("[Service]", text)
        self.assertIn("[Install]", text)
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=always", text)
        self.assertIn("while :; do", text)
        self.assertNotIn("RemainAfterExit=yes", text)
        self.assertIn("%h/.local/libexec/oxp3-speaker-gain apply", text)
        self.assertIn("WantedBy=default.target", text)

    def test_gain_apply_and_undo_round_trip(self):
        r = self.run_gain("apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.amixer_state.read_text().strip(), "87")
        self.assertEqual(self.gain_state.read_text().strip(), "Speaker\t0\t82,82")
        r = self.run_gain("apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("already at maximum", r.stdout)
        self.assertFalse(self.gain_state.exists() is False)  # still exactly one row
        self.assertEqual(len(self.gain_state.read_text().splitlines()), 1)
        r = self.run_gain("undo")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.amixer_state.read_text().strip(), "82")
        self.assertFalse(self.gain_state.exists())
        self.assertIn("restored 1 control", r.stdout)

    def test_gain_guard_when_control_is_absent(self):
        self.env["STUB_NO_CONTROLS"] = "1"
        r = self.run_gain("apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no Front/Speaker/Mono playback control", r.stdout)
        self.assertFalse(self.gain_state.exists())

    def test_unknown_profile_is_refused(self):
        r = self.run_install("--profile=bogus")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.conf.exists())


if __name__ == "__main__":
    unittest.main()
