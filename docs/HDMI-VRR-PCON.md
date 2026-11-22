# LG TV VRR over USB4 DP→HDMI PCON

## Current status on the external LG, 22 November

**The earlier idle-repeat change also failed in Game Mode.** The 01:16
session selected the LG at 1080p120 with build `202611220112`, and the
user saw the same corruption.

During the subsequent direct KMS test, the GPU accepted `VRR_ENABLED=1`
on pipe B while the external receiver repeatedly reported `0x00` at
DPCD `0x107`. The earlier link-training trace recorded a write of
`0x80` there. The direct test disabled both display pipes during setup.
The TV lost picture, the internal display stayed blank and the user had
to reboot. The comparison never reached pipe A.

[Kernel patch 0023](../patches/kernel/0023-drm-i915-restore-pcon-msa-ignore-on-vrr-enable.patch)
checks the receiver when enabling VRR on the Synaptics HDMI Forum path,
including transitions that reuse an existing link. If bit 7 is clear,
the driver preserves the other bits, sets bit 7, and reads it back before
programming variable timing. The prior driver relied on a link-training
write. The readback discrepancy was observed directly. Its role in the
visible corruption and signal loss has yet to be established.

### Earlier HDMI Forum and idle timing trials

**0022 reproduced the corruption and signal loss.** On the 01:05 boot the
loaded xe driver logged `signaling HDMI Forum VRR` for the Synaptics
bridge. The installed Gamescope binary matched the 0053 RPM. At boot
+33.61 s Gamescope selected 1080p120. Steam applied the VRR preference
and reported a 40–120 Hz range. The TV showed the same repeated image,
then lost signal. The USB detach at +63.22 s was the user's recovery
unplug. That leaves the actual time of HDMI signal loss unknown.

[Gamescope patch 0005](../patches/gamescope/3.16.23.3/0005-oxp3-lg-vrr-idle-repeat.patch)
changes one difference between Game Mode and the continuously presenting
Plasma test. Anatase allows 100 ms between idle frame submissions. The
Intel timing generator can repeat scanout at the LG's 40 Hz floor after
25 ms. The earlier test sustained a picture with new frames 11–18 ms
apart, although HDR flickered. On this LG at 1080p120 the patch asks for
a repeat of the last frame on the second nominal refresh cycle, about
16.7 ms after the last completed flip. New content can present earlier.
The timer uses the scheduled presentation time because it wakes early to
prepare the frame. A pending flip still takes precedence. Low-rate
content can receive extra repeated frames.

This is a test of sensitivity to long or uneven frame intervals. The
01:05 journal records neither actual frame intervals during corruption
nor the HDMI signal at the TV. Build `202611220112` contains the change.
Visible success means a single steady Steam image past the observed
30-second failure window, then HDR gameplay and a clean return to Steam.

### Earlier Gamescope focus test and kernel protocol selection

**Steam showed VRR enabled, but the TV displayed four vertically compressed
copies drifting sideways.** Gamescope build `202611220053` selected
DP-1 at 1920×1080, 120 Hz in the 00:56 session. Both advertised 1080p
timings that round to 120 Hz are progressive, with 2200×1125 total
pixels per frame. The UI symptom reproduces visible corruption before
the HDR game launch. A source-side VRR indicator and measured vblank
intervals cannot establish that the converter produced a usable HDMI signal.

[Kernel patch 0022](../patches/kernel/0022-drm-i915-prefer-hdmi-forum-vrr-on-synaptics-pcon.patch)
selects HDMI Forum VRR on the Synaptics bridge when the LG advertises
the same range in both its HDMI Forum and FreeSync blocks (40–120 Hz on
the attached TV). Intel sends its preprogrammed v1 Adaptive-Sync header
at fixed and variable refresh and leaves HDMI VRR translation to the
bridge. The earlier AS-only trials predated fixed-refresh priming and
the guardband correction. This follows an AMD display patch whose author
tested VMM7100 adapters and reported reduced flicker with Forum signaling.
[AMD hardware test and implementation](https://www.mail-archive.com/dri-devel@lists.freedesktop.org/msg587673.html)

The signed xe module is in build `202611220102`. The patched display
source compiled with `W=1`. Packet-selection checks using this LG's EDID
selected the v1 header alone at fixed and variable refresh and exercised
the FreeSync fallback. The detection journal identifies the new selection
with `signaling HDMI Forum VRR`. End-to-end validation needs one steady
Steam image with VRR enabled, followed by a sustained HDR picture in
Savage Wind and a clean return to Steam.

### Earlier HDR game launch and Gamescope focus change

**HDR game launch at 1080p120.** Build `202611220042` was running.
Steam opened Savage Wind at 00:46:47. The game created a 10-bit HDR10
PQ swapchain, and the LG showed its HDR banner before losing picture.
The game continued presenting near 120 fps. At 00:47:13 both USB hubs
disappeared and Gamescope switched to eDP. The user had unplugged the
dock to recover the picture. That timestamp records the unplug. The
journal does not give the time the TV first went black or establish a
spontaneous dock reset in this run.

In a subsequent 1080p120 Plasma test, the LG showed a moving yellow bar
with visible flicker. The LG CRTC first ran fixed at 8.33 ms in HDR
(PQ), then reported `VRR_ENABLED=1` for about 20 seconds. Median
vblank intervals tracked 55, 90 and 72 fps (18.2, 11.1 and 13.9 ms).
Plasma's automatic policy returned to fixed refresh for the rest of the
run. The original mirrored desktop layout was restored. The timings
verify source scanout. The visible flicker rules out calling this a
clean end-to-end HDR + VRR result.

The revised [Gamescope focus patch](../patches/gamescope/3.16.23.3/0003-oxp3-external-vrr-startup.patch)
applies the Steam VRR preference to the focused Steam UI as well as the
game on the DisplayPort-connected GSM LG TV SSCR2. With VRR already
active it holds that preference through brief gaps in focus. Turning
the toggle off takes effect immediately on the next presentation.
Previously Game Mode requested fixed refresh in Steam and switched to
VRR on game focus, precisely when Savage Wind also requested HDR10.
Removing that extra timing change is a candidate fix for game launch.
The panel-side cause of the flicker and signal loss remains open.

The installed Gamescope build is `202611220053`. Both RPMs passed
verification and the installed binary matched the x86_64 package.
The same packages are in the boot service cache. The LG's Game Mode
profile is 1080p120. Check the enabled VRR toggle in
the focused Steam UI, then open Savage Wind with its HDR setting on.
The expected result is a usable Steam menu, an HDR picture throughout
gameplay, and a working return to Steam.

### Earlier SDR tests and Gamescope build 202611220042

**0021 still lost the external display in Game Mode.** The loaded xe build
matched the installed module. Steam accepted VRR at 00:29:52 and opened the
game at 3840×2160 at 00:29:58. The dock hubs disappeared at 00:30:18 and
Steam reported the internal panel at 00:30:19. The user confirmed that
audio now works.

Two fullscreen SDR tests in Plasma used the same kernel, dock and TV.
Each alternated 72, 90 and 55 fps in eight-second intervals. DRM connector
286 (DP-1) remained on CRTC 273 with `VRR_ENABLED=1` throughout both
runs. Consecutive vblank timestamps followed the frame rate.

| LG output | Observed time | Typical vblank intervals | Dock USB disconnects |
| --- | ---: | --- | ---: |
| 1920×1080 at 120 Hz | 54.4 s | 13.9, 11.1, 18.2 ms | 0 |
| 3840×2160 at 120 Hz | 54.5 s | 13.9, 11.1, 18.2 ms | 0 |

These are GPU scanout measurements. The TV's own refresh readout was
not recorded. The original mirrored Plasma layout was restored and
checked after each run.

**Gamescope SDR metadata change.** At 4K120 KWin used default colorspace
and a null `HDR_OUTPUT_METADATA` property. Gamescope instead creates an
all-zero type-1 blob for SDR on this HDR-capable TV. Intel's
`intel_dp_compute_hdr_metadata_infoframe_sdp()` enables the gamut
metadata packet for that blob. The old Game Mode trace identifies the
connector as `DP-1 -> GSM - LG TV SSCR2`.
[Gamescope patch 0004](../patches/gamescope/3.16.23.3/0004-oxp3-lg-sdr-metadata.patch)
clears the property during SDR presentation and teardown for that model
on DisplayPort. HDR content still selects the HDR10 metadata path.
Build `3.16.23.3-1.fc45.oxp3.202611220042` carried this change. It
passed the host transaction check and the boot service installed it.
The next 1080p120 run still lost picture on entering HDR.

### Earlier 0020 run and 0021 backport

Steam accepted the VRR toggle at 00:05:26. Its display log still showed
3840×2160 at 120 Hz as the game opened. Both dock hubs disconnected at
00:05:58 and Gamescope switched to eDP. This happened with 0020 loaded.
Gamescope deliberately uses fixed refresh while Steam owns focus, which
explains the inactive indication in the menu. The latest boot has no DRM
fastset trace. The cause of the subsequent dock reset is still open.

**4K120 format selection.** The saved Intel trace at 21:49:09 shows an
1188 MHz pixel clock, DSC on the DP leg, YCbCr 4:4:4 from the GPU and
4:2:0 to the TV. DPCD 0x80 reads `0b f0 79 1e`. The bridge advertises
source-controlled FRL up to 48 Gbit/s and a separate 600 MHz TMDS limit.
The TV's HDMI Forum block advertises 40 Gbit/s FRL. The Anatase driver
uses the TMDS ceiling during RGB selection even for this FRL-capable
pair. At 8 bpc it rejects RGB at 1188 MHz and accepts 4:2:0 at 594 MHz.
This explains the conversion recorded in the failed run.

[Patch 0021](../patches/kernel/0021-drm-i915-pcon-frl-format-selection.patch)
backports Xiao Lu's v2 fix in the normal kernel patch series. It checks the shared HDMI FRL bandwidth during
both mode validation and color-depth selection. The two-lane DP link can
still use DSC. A standalone check of the patched selection function using
the recorded clock and advertised link limits accepted RGB at 10 bpc and
retained the 8-bpc 4:2:0 fallback for a 600 MHz TMDS link. Upstream
confirmed 4K120 RGB at 10 bpc on another PCON.
Whether removing chroma conversion also fixes VRR on the JSAUX is unverified.
[Upstream hardware result](https://www.mail-archive.com/intel-gfx@lists.freedesktop.org/msg387551.html)

For an end-to-end check, use the normal Game Mode UI at **3840×2160,
120 Hz**, enable VRR, and open the same game. The LG Game Optimizer should
show VRR with a changing refresh rate during variable-rate gameplay.
Keeping picture and audio past the previous disconnect window and through
a return to Steam distinguishes a usable result from a successful toggle.
This check requires no terminal in Game Mode.

**Dolby Digital 5.1.** PipeWire first logged `lgac3p: snd_pcm_start:
Broken pipe` at 00:04:58, a full minute before the USB disconnect.
At short graph quanta the a52 encoder can receive less than the 1536
samples it needs for its first AC-3 frame before ALSA starts HDMI1.
A codec-prefixed PCM name sets PipeWire's running delay target. Its
startup write instead uses the graph threshold plus headroom and start
delay. Renaming the PCM alone still produced the error at 128 frames.
[PipeWire 1.6.11 playback preparation](https://github.com/PipeWire/pipewire/blob/1.6.11/spa/plugins/alsa/alsa-pcm.c#L2584-L2589)
and [ALSA a52 start callback](https://github.com/alsa-project/alsa-plugins/blob/master/a52/pcm_a52.c#L575-L586)

The installed TV sink now has `api.alsa.headroom=3072` (two AC-3 frames
at 48 kHz). HDMI1 uses `AES0=0x06` for encoded audio. The sink service
unmutes the named SOF card after seeing its ELD, when the card is
accessible. The old login-only unmute unit was disabled. On this dock,
the HDMI hardware reached RUNNING with an advancing playback pointer at
requested quanta of 128, 256 and 1024 frames, with no new startup error.
An untargeted six-channel PulseAudio client also reached `oxp3_tv`.
During playback HDMI1 reported `AES0=0x06` and its hardware pointer
advanced from 87744 to 184272 in two seconds.
The user subsequently confirmed audible playback. The
audio patch is a migration for the existing per-user TV sink and ALSA
configuration. It is already applied on this machine.
[User configuration patch for the existing LG setup](../patches/audio/0001-oxp3-tv-dolby-startup.patch)

## Original evidence before the PCON patches

- Connector `DP-1` is a DP branch device with `subconnector=HDMI`.
- EDID: LG TV SSCR2, **EDID 1.3**, HDMI Forum VSDB VRR 40–120 Hz. The range-limits
  descriptor (GTF 24–120 Hz) is ignored by the DRM core on EDID < 1.4.
- DRM: `vrr_capable = 0`; KDE reports `Vrr: incapable`.
- Intel `intel_vrr_is_capable()` explicitly rejects `drm_dp_is_branch()` and its
  comment says PCON VRR is not implemented.
- 4K120 + HDR work, so the HDMI 2.1 link/PCON conversion is functional.

## Why 0010 + 0011 still gave `vrr_capable = 0`

With 0010/0011 loaded, the debug line for the dock was:

```
oxp3-vrr: type=10 mst=0 branch=1 hdmi=1 as_sdp=1 v1=1 v2=0 range=0-0
```

The PCON gate passed. The **range** check failed. The DRM core parses the
HF-VSDB VRR range into `display_info.hdmi.vrr_cap`, not into
`display_info.monitor_range`. `monitor_range` is filled only from an EDID 1.4
range descriptor with the continuous-frequency bit. The 0010 comment assumed
otherwise. `intel_vrr_is_capable()` and the vmin/vmax maths read
`monitor_range`, so they saw 0–0.

A second, latent bug: `intel_dp_as_sdp_unpack()` rejects any AS SDP whose length
is not 9. The header-only v1 packet from 0010 (length 0) therefore always read
back as zero, so state verification fails ("pipe state mismatch") as soon as VRR
is really on. 0011 avoided this only for fixed refresh.

## Patches

- `0010-drm-i915-vrr-pcon.patch`: adds `as_sdp_v1_supported`, allows VRR on a
  branch/PCON and sends a header-only AS SDP v1.
- `0011-drm-i915-vrr-pcon-fix.patch`: drops the `is_hdmi` requirement, sends AS SDP
  to the PCON only while VRR is enabled, and adds the `oxp3-vrr` debug line.
- `0012-drm-i915-vrr-pcon-hf-vsdb-range.patch`:
  - fills `monitor_range` from `hdmi.vrr_cap` for a branch device with an HDMI
    sink, as amdgpu does for HDMI. It logs
    `oxp3-vrr: PCON HDMI sink, VRR range 40-120 Hz from HF-VSDB` once per EDID read;
  - accepts the header-only v1 packet on readout;
  - resets `as_sdp_v1/v2_supported` on each detect, so the state does not carry
    over to the next sink on the port;
  - adds `xe.pcon_as_sdp` (0 = v1 header-only, the default; 1 = v1 header + AVT
    payload; 2 = v2 payload) to choose the AS SDP layout sent to the PCON;
  - demotes the per-call debug line to `drm_dbg_kms` and fixes its `%d` of a
    pointer, which was a W=1 error.

Build check: 0012 compiles cleanly with `W=1`, and `xe.ko` links with
`pcon_as_sdp` in `modinfo -p`. The installer's own `apply_kernel_patches` applies
0009/0011/0012 cleanly to a clean clone of the `7.3.4-an02` tag.

## Hardware test (after `./oxp3-install.sh` + reboot)

1. `journalctl -k -b | grep 'oxp3-vrr: PCON'` should show the 40–120 Hz range.
2. `drm_info | grep -A1 vrr_capable` (or KDE display settings) should show VRR
   capable on DP-1.
3. Enable Adaptive Sync in KDE. Run a game with a varying frame rate and open the
   TV's Game Optimizer / info overlay (green button 7× on LG), which shows VRR
   and the live refresh rate.
4. If the TV does not report VRR, try the other AS SDP layouts without
   rebuilding. Then toggle VRR off and on in KDE to force a modeset:
   `echo 1 | sudo tee /sys/module/xe/parameters/pcon_as_sdp` (then `2`).
5. Watch `journalctl -k -f` for `pipe state doesn't match` while you toggle.

Status: **experimental**. Whether this dock's PCON turns AS SDP into HDMI VTEM
(or needs extra PCON DPCD setup) can only be shown on hardware.

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

# Gamemode black screen / dock reset (2026-11-21): VRR off on the PCON by default

Symptom: in Gamemode the TV went black, then the whole JSAUX dock disappeared (no display,
Ethernet, USB or charging until replugged). The desktop (KWin) was stable.

Chain, from `drm.debug=0x106` captures and the logs:
- The dock's converter is a **Synaptics VMM7100-family PCON** (OUI `90-cc-24`, `SYNA`, fw 6.5),
  behind an LTTPR retimer. The OXP3 gives the dock **2 DP lanes** (HBR3), so 4K@120 needs **DSC**
  (8 bpc → 10 bpp, 4 slices). The PCON decompresses it and outputs YCbCr 4:2:0 to the TV.
- KWin and gamescope program the PCON identically: the same DPCD writes (`0x100-0x107`, DSC `0x160`,
  FEC `0x120`, PCON control `0x3050-0x3052`), the same link, the same timing.
- The deciding difference is **VRR**. With 0012 the TV became VRR-capable and gamescope turned VRR
  on by itself (Steam's `STEAM_GAMESCOPE_VRR_SUPPORTED` makes no difference). Every `vrr: yes` state
  on the PCON pipe was followed by the dock disconnecting after 6-18 s, including one with a single
  modeset straight after a reconnect. Every `vrr: no` state stayed up. The AS SDP layout doesn't
  matter: `xe.pcon_as_sdp` 0, 1 and 2 all fail.
- Ruled out with data: power limits, HDR, plane scaling (`--force-composition`), FIFO underruns
  (none), memory bandwidth (5-10 GB/s needed, 20+ available), display DC states (only after the
  dock was already gone).

Fix: `patches/kernel/0013-drm-i915-vrr-pcon-vrr-off-by-default.patch` adds `xe.pcon_vrr`
(default 0). PCON outputs are reported as not VRR-capable, so no compositor can enable VRR there.
The experimental path stays behind `xe.pcon_vrr=1`.

# VRR as FreeSync through the PCON, like amdgpu (2026-11-21, patch 0015)

The earlier "still open" note was wrong about what amdgpu sends. For this dock, amdgpu (Legion Go 2)
takes its **FreeSync PCON** path (`dm_get_adaptive_sync_support_type()` →
`FREESYNC_TYPE_PCON_IN_WHITELIST`), because all of its conditions hold:

| amdgpu condition | this dock / TV |
|---|---|
| branch OUI on `dm_freesync_pcon_whitelist` | `90-cc-24` (Synaptics), on the list |
| PCON supports the AS SDP | yes (`as_sdp_v1`) |
| `allow_invalid_MSA_timing_param` (DPCD `0x007` bit 6) | DPCD `0x007` = `0xc2`, set |
| AMD FreeSync VSDB in the sink's EDID | `1a 00 00 02 01 28 78…`: **v2, 40-120 Hz** |

amdgpu then takes the range from the AMD VSDB and sends **two** packets: the header-only AS SDP v1
*and* the **AMD FreeSync SPD InfoFrame** (v2, packed as a DP 1.3 InfoFrame SDP:
header `00 83 1d 4c`, DB0 version, DB1 length, OUI `1a 00 00`, DB7 flags
supported/enabled/active, DB8/DB9 min/max Hz). The PCON forwards the SPD and the TV runs FreeSync
over HDMI. i915 only ever sent the AS SDP, so the VMM7100 did its own HDMI-Forum VRR conversion,
and that is what reset the dock.

`patches/kernel/0015-drm-i915-vrr-pcon-freesync-spd.patch` does the same as amdgpu:
- detects a FreeSync PCON (amdgpu's OUI list, the MSA-ignore bit, an AMD VSDB v1/v2; the EDID core
  parses only v3) and logs `oxp3-vrr: FreeSync PCON 90cc24, AMD VSDB v2, range 40-120 Hz`;
- sends the FreeSync SPD through the transcoder's SPD DIP. `intel_dp_set_infoframes()` rewrites it
  on the VRR fastset, so the *active* flag follows VRR. The packet is kept out of
  `infoframes.spd`, so a VRR toggle stays a fastset;
- offers VRR on a PCON **only** in that case, and turns `xe.pcon_vrr` back on by default. A PCON
  without FreeSync stays non-VRR, as 0013 made it.

**Result on hardware (2026-11-21 22:48):** detection works (`oxp3-vrr: FreeSync PCON 90cc24, AMD
VSDB v2, range 40-120 Hz`; KDE and Steam offer VRR). In the desktop, with the SPD sent and VRR
inactive, the dock was stable for 15+ minutes. But **Gamemode dropped the dock 18 s after gamescope
started** (`usb 3-1: USB disconnect` at 22:50:19), the same pattern as without the SPD. So amdgpu's
packets alone do not make this PCON survive VRR on Panther Lake. A desktop test with KWin's VRR
policy *Always* was inconclusive: KWin never set `VRR_ENABLED` on the idle desktop (checked with
`drm_info`).

`patches/kernel/0017-drm-i915-vrr-pcon-freesync-off-by-default.patch` turns `xe.pcon_vrr` off by
default again, so Gamemode is stable. The FreeSync path stays available with `xe.pcon_vrr=1`.

Next steps for VRR (not done): a `drm.debug` capture of the moment VRR goes active in a VRR
application on the desktop, to compare what PTL programs (vmin/vmax/flipline, AS SDP timing, the
PCON's DPCD `0x3050-0x305f` status) against the Legion Go 2 on the same dock.

## 4K120 fastset fix from the saved DRM capture

The archived `b2.log` records a fixed 4K120 modeset at 21:49:08 with a
60-line guardband. At 21:49:09.702 and again at 21:49:09.717, the
fixed-to-VRR request differed in only one fastset comparison field:
`vrr.guardband (expected 60, found 83)`. Intel forced a full modeset,
reset the Type-C display port and trained the DP link again. DPCD
`0x107` contained `80 01` on both trainings, so MSA timing ignore was
already set. The USB hubs disappeared at 21:49:22.998. The capture
cannot tell whether the link retrain or variable timing caused the reset.

Intel calculates the 83-line minimum as `vrr.vsync_start + 1` to fit the
Adaptive-Sync SDP. Previously it reserved this space only while sending
the packet. Patch [0018](../patches/kernel/0018-drm-i915-pcon-vrr-fastset.patch)
reserves the space in fixed refresh as well for enabled FreeSync PCONs,
using the SPD enable bit that is already set in both states. The
driver's packet validation still counts only packets actually sent. This
keeps the existing packet transmission behavior in fixed refresh.

The initial Gamescope patch `0003-oxp3-external-vrr-startup.patch` kept the external
output at fixed refresh when no window is focused. On 3.16.23.3 it also
disabled VRR while the Steam UI is focused, matching an0.8's existing
policy. Together these changes give the link a chance to train at fixed
refresh before VRR is enabled in-game.

Patch [0019](../patches/kernel/0019-drm-i915-enable-pcon-vrr.patch) enables
VRR by default for eligible FreeSync PCONs so the next standard install can
exercise the fastset. An explicit `xe.pcon_vrr=0` still overrides the
default. Use that boot argument if the dock continues to reset.

The standard installer picks up the patches. To validate on the
patched boot, enter Game Mode at 4K120 with VRR enabled and launch a
game with a varying frame rate. The expected sequence is fixed refresh
in the Steam UI, then `VRR_ENABLED=1` with the same guardband and no
new link-training sequence when the game takes focus. The dock should
stay present and the LG live refresh should track the game's frame
rate. If it still resets following a confirmed fastset, the guardband
mismatch was a separate bug and variable timing over the PCON remains
the next issue.

## Game launch still disconnects the dock

With 0018 and 0019 installed, Game Mode exposes the VRR switch and shows
VRR inactive in the UI. Launching a game still caused the screen to
lose signal. At 23:50:12 the journal records both dock USB hubs
disconnecting. This run has no DRM fastset trace, so it does not show
whether that transition retrained the link.

The guardband fix and the test default remain in 0018 and 0019. A
boot with `xe.pcon_vrr=0` can be used to recover fixed refresh if needed.

### Dolby Digital playback recovery

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

### Pre-program the Adaptive-Sync packet

Steam's `systemperfmanager.txt` recorded `VRR support: 1` and
`Set VRR enabled: 1` in the failed session. The toggle changed the stored
preference. Gamescope reports whether the CRTC is currently using VRR
independently. Its Steam UI focus policy keeps the CRTC at fixed refresh
until the game takes focus. Steam's display log still showed 3840×2160
at 120 Hz before losing DP-1 and falling back to eDP-1.

Patch [0020](../patches/kernel/0020-drm-i915-prime-freesync-pcon-as-sdp.patch)
programs the header-only v1 Adaptive-Sync packet on an eligible PCON
while the already-selected mode is within the VRR range, as amdgpu
does. It leaves the identical header enabled during subsequent
updates and uses the FreeSync SPD to signal whether VRR is active.
The alternate packet layouts retain their existing activation policy.
This tests whether introducing the AS header at game launch was part
of the dock failure. The PCON remains VRR-capable by default.

# VRR enable must retrain: the MSA-ignore latching window (2026-11-22, patch 0024)

The bridge accepts an AUX write to `DP_DOWNSPREAD_CTRL` while the stream is up, but a
readback over repeated attempts (patch 0023 at commit time) shows the
`MSA_TIMING_PAR_IGNORE_EN` bit never sticks. The only window where the VMM7100 latches the
bit is link training with the stream down — `intel_dp_link_training_set_mode()` writes it
when `vrr.in_range`, on every retrain. amdgpu programs it the same way, in its stream-enable
sequence.

Gamemode enables VRR as a fixed→variable **fastset** (`vrr.enable` is not part of the
fastset comparison in this kernel), so no retrain runs and the bit stays clear when the
first stretched frames arrive. Patch 0023 (commit-time write) is dropped: it cannot work.

Patch `0024-drm-i915-force-pcon-vrr-modeset-on-freesync-pcon.patch` forces a **full modeset**
when VRR enables or disables on a FreeSync PCON (`intel_crtc_check_fastset()`), so the
retrain runs with `vrr.in_range` and the training write leaves the bit set. Observable:
`oxp3-vrr: VRR transition on a FreeSync PCON forces a full modeset` in the kernel log, and
no `PCON MSA-ignore unavailable` messages.

If VRR still breaks the external output with the bit latched during training, the remaining
explanation is the VMM7100 firmware itself; that would need a dock firmware update or the
amdgpu-side comparison on real hardware, not more packet work.
