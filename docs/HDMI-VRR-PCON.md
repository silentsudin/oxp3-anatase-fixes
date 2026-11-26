# External display VRR through the USB-C dock (DP → HDMI PCON)

## Status

One kernel patch carries all external-display VRR support:
[`patches/kernel/0200-drm-i915-vrr-dp-to-hdmi-pcon.patch`](../patches/kernel/0200-drm-i915-vrr-dp-to-hdmi-pcon.patch).
`./oxp3-install.sh` builds and installs it with the other kernel modules. Its only
switch is `xe.pcon_vrr` (default on; `xe.pcon_vrr=0` on the kernel command line turns
VRR off for the dock output).

Gamescope carries two VRR-related patches (`patches/gamescope/3.16.23.3/`):
0002 backports upstream's VRR frame pacing, 0003 ports upstream 8e0a5bee (the Steam UI
presents on every vblank while it is focused, `adaptive_sync_idle_hold=0` turns that
off). 0001 is the HDR PQ fix.

Hardware: OneXPlayer 3 (Panther Lake, xe), JSAUX HB1201B dock (HB1201BWSXS01) with a
**Synaptics VMM6210** converter (DPCD 0x507-0x508 = `62 10`, fw 6.5, OUI 90-cc-24,
DPCD 0x007 = `0xc2`, 0x2214 = `0x01`) behind an LTTPR retimer (OUI 98-4f-ee), 2-lane
HBR3 link; LG TV "LG TV SSCR2" (HF-VSDB VRR 40-120 Hz, AMD VSDB v2 40-120 Hz).

## What the patch does

- **VRR capability through the PCON.** Detects the converter's header-only
  Adaptive-Sync SDP v1 support and offers VRR on its HDMI sink, with the range from
  the HDMI Forum VSDB (the LG's EDID 1.3 has no range descriptor).
- **amdgpu's packet set.** The header-only AS SDP v1 plus the AMD FreeSync SPD
  (DP 1.3 InfoFrame SDP, range from the AMD VSDB) whenever the mode is in the VRR
  range, as amdgpu's `FREESYNC_TYPE_PCON_IN_WHITELIST` path does. The converter kept
  the picture with variable timing only when the FreeSync SPD was present (measured
  before the MSA hold below existed).
- **HDR + VRR drops the dock (open, 2026-11-25).** At 4K120 with VRR on, HDR10 games
  take the whole dock down (HDMI output and USB hub) 8-26 s after Gamescope's HDR
  modeset: Pokopia (6 times, every run), Legends Z-A, Donkey Kong Bananza (3 times after
  midnight on 11-24), Horizon (11-22). SDR games ran VRR at 4K120 for hours. DKB is not
  consistent: it also ran 8 min (11-24 20:52) and 60 s (11-25 20:43) in HDR + VRR. DKB
  runs at ~100 fps, while Pokopia is locked to 60 fps, so under VRR every Pokopia frame
  is stretched to ~2x vtotal. The suspected trigger is HDR (VSC colorimetry + HDR
  metadata) combined with long stretched frames. That is not proven.
  **Not the FreeSync SPD.** A build that dropped the SPD in PQ (AS SDP, VSC and HDR
  metadata only, the Windows driver's packet set) still dropped in Pokopia, 16 s and 8 s
  after the HDR modeset (11-25 21:01 and 21:02). The change was reverted.
  The converter side (fw 6.05.106, decompiled): in its video-running state it compares a
  TX output measurement (`0x20330004`) with the expected DSC picture width
  (`0x2022084c`) on every pass. Each miss logs "Something is wrong. If it persists, will
  have to reset in 10" and increments `0x90003df7`; the tenth logs "No display detected,
  going back" and re-initialises, which is the dock reset. With VSC colorimetry on (HDR
  only), its watchdog (`0x8002cd5a`) also compares the VSC bpc and colour format with
  the values saved at stream start, and re-programs the output after three differing
  reads. Which of the two fires has not been captured.
  **Recorder.** `oxp3-dock-watch.service` (installed by the `hdr` component) samples
  these values read-only over RC every 0.5 s while the USB-C display is on. It writes to
  the journal for each strike (with the compared TX/RX values, the live and saved VSC
  and the port flags), for each line the firmware writes to its log ring, and when the
  dock attaches or is lost. `/var/log/oxp3-dock-watch/` holds the last 60 s of samples
  before the first and ninth strike and before a loss, plus the 32 KB firmware log ring
  as it reads on attach. Follow it with `journalctl -u oxp3-dock-watch`. A strike line
  means the output check fired; a drop with no strikes points at the VSC re-program
  path or something not yet decoded.
  **First captures (2026-11-25 22:23-22:51): not the output check.** The reads are live
  (MSA decodes as htotal 4400, vtotal 2250, 3840x2160). The block at `0x90003de0` (strike
  counter, pass counter, firmware log index) stays all zeros through three dock resets, so
  the state-8 output check never runs and the firmware log is never written. Port-0 flags
  read `0x02601448`, which has no `0x100000`, so the `& 0x500000 == 0x500000` gate is closed
  anyway. Reset 1 (Steam menu, 20 s after the mode set) came with no change in any sampled
  value. Reset 2 came 12 s after Pokopia's HDR switch: the VSC colorimetry went from 1 to
  6 (BT.2020) and the TX output measurement (`0x20330004`) went from 1920 to 0 at the
  switch. The whole dock re-enumerating (USB hub included) points past the video
  converter: the dock's own controller or its USB-C power contract. The recorder now logs
  the AC adapter (ACPI `ADP1`, no UCSI on this machine) and the battery power next to each
  sample, so the next reset shows whether power goes first.
  **The HDR switch (2026-11-25 22:56-22:58, with power logging).** The AC adapter stayed
  online through the reset, so it is not the USB-C power contract. Two Pokopia launches,
  same game, same HDR switch (VSC colorimetry 1 -> 6): at 22:56:48 the converter's HDMI
  output measurement (`0x20330004`) stayed at 1920 and the game ran 70 s. At 22:58:22 it
  dropped to 0 at the switch, stayed there, and the dock reset at 22:58:36 (and the same
  at 22:50:11 -> 22:50:23). Every HDR on/off is a colorimetry change. Gamescope commits
  that as a full modeset (`DRMBackend.cpp`: Colorspace change -> `needs_modeset`), and
  i915 could not fastset it anyway (`PIPE_CONF_CHECK_DP_VSC_SDP` also runs for fastsets).
  So each one retrains the link, and the converter restarts its HDMI output; some
  restarts don't come back. Windows keeps an HDR display in HDR while HDR is on, so the
  converter sees one switch per session. gamescope patch 0004
  (`hdr_output_always_external`, default on) does the same: on an external HDR display
  the output stays PQ/BT.2020 and SDR content (the Steam UI, SDR games) is mapped into
  it, so starting or leaving an HDR game needs no modeset. Not explained by this: one
  reset in the Steam menu (22:49:42) 20 s after Gamemode started, with the output
  measurement at 1920 throughout.
  **Host side after a drop.** `intel_tc_port_link_reset_work` failed its reset commit
  (`drm_WARN_ON(ret)` at `intel_tc.c:1837`, 11-25 only). `intel_dp_detect()` had already
  cleared the sink's DSC caps on the disconnect, and upstream's own FIXME there says this
  makes "state computation fail if the connector need to be modeset after sink
  disconnect". 4K120 needs DSC, so the pipe kept driving the dead link. A suspend after
  the first drop resumed; the one after the second never came back, and the machine
  needed a hard reset. Patch 0201 keeps the DSC caps on disconnect (they are cleared and
  re-read on every connect). It also logs the reset (`TypeC DP-alt sink disconnected,
  resetting link`) and, on failure, its error code.
- **MSA timing ignore after the stream starts.** Link training leaves
  `DP_DOWNSPREAD_CTRL` bit 7 clear; `intel_ddi_enable_dp()` sets it once video is
  running. amdgpu (`enable_stream_features()` after unblank) and the Windows driver
  (after its link-training loop) do the same.
- **MSA hold (2026-11-22, confirmed working on the TV in Gamemode at 4K120).** With DC balance adjust
  enabled (`VRR_CTL_DCB_ADJ_ENABLE`), Panther Lake's timing generator takes its VMAX and
  FLIPLINE from `TRANS_VRR_DCB_VMAX`/`DCB_FLIPLINE` (i915's own DC-balance vblank code
  reads those "final" values). On the PCON output 0200 keeps `TRANS_VRR_VMAX`, the
  register the MSA VTotal is taken from, at the mode's vtotal (2250 at 4K120), and puts
  the real stretch limit (6750 for 40 Hz) in the DCB registers, with the DMC balancing
  loop off. The kernel log shows `oxp3-vrr: PCON MSA hold: MSA vtotal 2250, VRR
  2250-6750 lines` each time VRR turns on. This is the fix for the black screen and the
  dock resets: the converter sees a constant MSA VTotal while the frames stretch.
- **YCbCr 4:2:0 pass-through** to a PCON that can pass it, instead of 4:4:4 for the
  PCON to convert. 4K120 on the 2-lane link still needs DSC either way (DP carries
  4:2:0 at the full pixel rate: 1188 MHz x 12 bpp = 14.3 Gbit/s > 12.96).
- **Diagnostics.** Reads the header-only AS SDP back correctly (the state checker
  failed on it) and logs the SPD bytes the hardware holds (`DP SPD SDP readback:`,
  drm.debug KMS): `03 78 78` at fixed 120 Hz, `07 28 78` with VRR active.

## Root cause, measured inside the converter

`tools/display/vmm-rc-peek.py` reads converter memory over DP AUX with the Synaptics RC
protocol (read-only, as fwupd does). With VRR switched on and off at 4K120:

| time | live MSA dword 0 | vtotal |
|---|---|---|
| 0.00 s | `30 11 ca 08` | 2250 (4400 x 2250, 4K at 120 Hz) |
| 6.36 s, VRR on | `30 11 5e 1a` | **6750** = VRR VMAX |
| 6.71 s | firmware's saved copy updated to 6750 | |
| 21.36 s, VRR off | back to 2250 | |

- **Intel hardware:** with VRR enabled, Panther Lake sends the VRR timing generator's
  VMAX as the MSA VTotal. i915 writes the nominal value to TRANS_VTOTAL; the MSA
  follows VMAX anyway.
- **Converter:** the VMM6210 firmware's MSA watchdog (0x8002cd5a) compares the live
  MSA with a saved copy and, after two differing reads, re-parses and re-times its HDMI
  output (details in [VMM6210-FIRMWARE-RE.md](VMM6210-FIRMWARE-RE.md)). A change in a
  running stream gives the vertically multiplied picture, black, or a lost signal.
- **Keeping VRR on does not help (2026-11-22, tested and removed).** A build that kept VRR
  on for the whole mode, as the Windows driver's `AlwaysInVrrMode` suggests, announced
  the 40 Hz VMAX from the first frame at 4K120. The whole dock (converter and USB hub)
  reset 11.5 s after Gamescope's modeset and again 8 s after it came back. So at 4K120
  the announced VMAX itself takes the dock down, not only a change of it mid-stream.

## What the Windows driver does (static analysis, Intel 32.0.101.9175)

The Intel Windows display driver (`igdkmdn64.sys`) was decompiled to find what it does
differently on this dock. The decompiled functions and the Ghidra script are in
`~/ghidra-work/winkmd/`.

- **No dock-specific handling.** Its only Synaptics branch entry is a different chip
  (device ID `SYNAS1`, old firmware) and concerns UHBR link options.
- **Same registers.** Its per-platform register maps hold the same transcoder VRR and
  MSA registers Linux programs; there is no register that selects the MSA VTotal.
- **Same capability checks.** MSA-ignore support (DPCD 0x0007/0x2207 bit 6), then AS SDP
  support (0x2214 bit 0). The range only gets a 24 Hz floor.
- **Same MSA-ignore sequencing.** DPCD 0x107 bit 7 after link training. Alongside it,
  Windows writes DPCD 0x11B bit 7 = 0x2214 bit 1; this converter reports 0x2214 =
  `0x01`, so that write is 0, the register default.
- **VRR fixed with the mode.** Windows takes the VRR enable from the mode at modeset,
  and on Panther Lake an "always in VRR mode" setting (`AlwaysInVrrMode`) keeps it on
  instead of switching it per application. Copying that made the dock reset at 4K120
  (see above), so it is not how Windows keeps this dock working.
- **HDMI 2.1 FRL.** Windows trains the converter's FRL link itself (DPCD 0x305A/0x305B,
  rate from the TV's and the converter's limits; TMDS set explicitly otherwise). i915
  has the same code but runs it only when the converter advertises source control
  mode, which this one doesn't; the converter picks its link itself and the desktop's
  4K120 4:2:0 runs on FRL that way.

## Removed

Patches 0010-0029 (one change per test, several undoing each other), 0201 (a VRR
stretch cap), 0202-0204 (4:2:0 and MSA-ignore bisection switches), three local
gamescope workarounds and `tools/display/pcon-vrr-matrix.sh` (it drove switches that no
longer exist) were removed on 2026-11-22. They, and the full test log this file used to
hold, are archived in `../oxp3-external-vrr-history-20261122.tar.gz` (next to the repo).

# HDMI eARC surround (same dock/TV)

The TV's ELD (`/proc/asound/card0/eld#2.*`, `connection_type DisplayPort`):
LPCM **2 ch**, AC-3 6 ch, E-AC-3 8 ch, TrueHD 8 ch. The hardware PCM accepts
2–8 channels. However, the TV accepts only stereo LPCM, so 5.1/7.1 LPCM is not
possible as configured. Surround has to be sent as a bitstream that the TV
passes through over eARC.

Problems found in the earlier AC-3 attempt:

- `~/.asoundrc` used `hdmi:CARD=sofhdadsp,DEV=2`, which is HDMI3 = `hw:0,5`, an
  empty port. The TV is on HDMI1 = `hw:0,3` = `DEV=0`. The encoder worked but its
  output went to the wrong port.
- `~/.config/environment.d/90-alsa-a52.conf` set `ALSA_PLUGIN_DIR`. That
  *replaces* the plugin path, so the ALSA `default`/`pipewire` device failed for
  every ALSA client ("Cannot open shared library libasound_module_pcm_pipewire.so").

Fixes (user config, outside the repo):

- `~/.asoundrc`: `DEV=0`, and the a52 plugin is loaded by full path through
  `pcm_type.a52 { lib ... }`.
- `90-alsa-a52.conf` renamed to `.disabled`. This takes effect at the next login.
- `~/.config/pipewire/pipewire.conf.d/50-lg-hdmi-ac3.conf`: a 6-channel
  "LG TV 5.1 (AC-3)" sink.

**2026-11-21, second pass:** the AC-3 chain was checked offline. Six distinct tones in ALSA order
(FL FR RL RR FC LFE) were encoded through the same `a52` plugin into a file, extracted from
IEC 61937 and decoded with ffmpeg. Every tone came out on its own channel (AC-3 5.1, side). So the
`audio.position = [ FL FR RL RR FC LFE ]` in `50-lg-hdmi-ac3.conf` is the right order. The earlier
mapping error came from `[ FL FR FC LFE RL RR ]`. The "no sound" right after that change came from the
PipeWire restart running into the HDMI silent-stream bug (fixed by
`snd_hda_codec_intelhdmi enable_silent_stream=0`), not from the order.

**Third pass (2026-11-21 22:55), the setup that works:** a static sink in `pipewire.conf.d`
(`50-lg-hdmi-ac3.conf`) was the wrong design, for three reasons:
- **it crashed PipeWire.** When its device can't be opened (at login, before the SOF card is
  registered), the whole of PipeWire fails to start (`failed to create context`);
- **it competed for the port.** It and PipeWire's own HDMI1 sink share `hw:sofhdadsp,3`, so
  whichever opened first left the other "Device or resource busy". In Gamemode, Steam took HDMI1;
- **it couldn't follow hotplug.** It existed with or without the TV.

The working setup, all user config:
- `~/.local/libexec/oxp3-tv-audio`, run by `~/.config/systemd/user/oxp3-tv-audio.service` (bound
  to PipeWire): while the HDMI1 ELD is present it creates one sink, `oxp3_tv`, with
  `pw-cli create-node` and `object.linger`, and removes it on unplug. If the ELD lists AC-3,
  that sink is "LG TV SSCR2 5.1 (Dolby Digital)" through `lgac3`; otherwise it is plain stereo
  LPCM;
- `~/.config/wireplumber/wireplumber.conf.d/51-oxp3-tv-audio.conf` hides PipeWire's HDMI1 sink.
  Retargeting that sink at `lgac3` instead doesn't work: it is opened through UCM
  (`_ucm0002.lgac3`), which doesn't see `~/.asoundrc`;
- `session.suspend-timeout-seconds = 30`: the soundbar needs ~3 s to lock onto a new Dolby
  stream, so the sink keeps running through short pauses;
- set as the default sink; WirePlumber falls back to the speakers when it disappears.

Verified on the soundbar: six beeps, one per speaker, all in the right order through the default
output.

The ELD depends on the TV settings. With *eARC Support: On* and *Digital Sound
Output: Pass Through*, some LG firmware advertises multichannel LPCM. Re-check
`sad0_channels` after you change them and replug. If it shows 6 or 8, plain
LPCM 5.1/7.1 to the HDMI1 sink works and is lossless.


## Dolby Digital playback recovery

The custom ALSA HDMI slave in `~/.asoundrc` inherited `AES0=0x04`,
which marked the encoded IEC 61937 stream as PCM. ALSA's a52 plugin
sets the non-audio bit when it chooses its own slave. With an explicit
HDMI slave, it passes through the supplied channel-status flags. The
HDMI1 slave now uses `AES0=0x06` to identify the AC-3 bitstream. This
addresses compressed bytes being played as noise. After restoring the
5.1 service, a six-channel PipeWire playback test completed with no
new PipeWire errors. The codec reported `AES0=0x06` and WirePlumber
selected the Dolby Digital sink. Audible and Game Mode playback
remain to be confirmed.

