# Internal speakers: reproducing the Harman AudioEFX tuning on PipeWire

Status: tuning recovered from the vendor package, reimplemented as a user-scope
PipeWire filter-chain, and measured end-to-end in an isolated PipeWire graph:
0.029 dB mean / 0.111 dB max against the model, plus limiter-replacement and
crossover checks. Nothing on the host was changed (no root, no config in
`~/.config`); see [`docs/audio-validation.md`](../audio-validation.md).

Target: ONEXPLAYER 3 (`ONE_NETBOOK-ONEXPLAYER3`), Anatase `20261026.3`, kernel
`7.2.7-an28.fc45.x86_64`, PipeWire 1.6.9, Realtek ALC245 behind Intel SOF
(`skl_hda_dsp_generic`). Windows side: the vendor `Harmon APO` package in
`~/Downloads/oxp3/`.

On Windows the internal speakers go through **Harman AudioEFX** (an ICEpower DSP
APO): a 10-band voicing EQ, a second all-cut resonance-control EQ and a limiter.
Linux applies none of it, which is exactly the reported difference - Linux output
is brighter/harsher and has no speaker protection.

- Extractor: [`tools/audio/apo_extract.py`](../../tools/audio/apo_extract.py)
- Filter chain: [`tools/audio/oxp3-speaker-eq.conf`](../../tools/audio/oxp3-speaker-eq.conf)
- Model + config generator: [`tools/audio/eq_response.py`](../../tools/audio/eq_response.py)
- Measurement: [`tools/audio/measure-speaker.py`](../../tools/audio/measure-speaker.py)
- Isolated-graph harness / validation: [`tools/audio/test-isolation.sh`](../../tools/audio/test-isolation.sh),
  [`tools/audio/measure-in-isolation.py`](../../tools/audio/measure-in-isolation.py),
  [`docs/audio-validation.md`](../audio-validation.md)
- Install / on / off: `install-user.sh`, `eq-on.sh`, `eq-off.sh` (same directory)

## TL;DR

1. The vendor preset is a **1580-float parameter blob** (`REG_BINARY`, little-endian
   float32) per endpoint and preset, `HKLM\SOFTWARE\Harman\AudioEFX\Parameters\
   <endpoint-GUID>\Settings\<1..6>` with `ActiveSetting="2"`.
2. **Only the speaker endpoint is tuned.** Against the factory table baked into
   `AudioEFXService64.exe` (found at file offset `0xcf6a0`), the speaker preset
   changes **177 of 1580** floats; the headphone, S/PDIF and the fourth endpoint
   change only three flag words.
3. The audible part is two cascaded 10-band blocks, written as 5 floats per band:
   `(freq_Hz, gain_linear, Q, selector, enable)` - stage 1 voicing (stored twice,
   bit-identical: left and right) and stage 2, an all-cut narrow-Q block.
4. A limiter block sits next to them: **threshold 0.607 (-4.3 dBFS), attack 0.200 s,
   release 0.360 s** (factory: 1.0 / 0.060 s / 0.500 s), i.e. the vendor deliberately
   trades 4.3 dB of headroom for protection of the small drivers.
5. [`tools/audio/oxp3-speaker-eq.conf`](../../tools/audio/oxp3-speaker-eq.conf)
   reproduces stage 1 as eight `bq_peaking` filters plus a -8 dB headroom stage
   (the peak boost of stage 1 is +7.74 dB @ 142 Hz). Measured in a real,
   hardware-free PipeWire 1.6.9 graph it matches the model to **0.029 dB mean /
   0.111 dB max** (20 bands, 100 Hz - 10 kHz); stage 2 is included commented out
   because its role is not proven (see open points). See
   [`docs/audio-validation.md`](../audio-validation.md) for the reproducible
   end-to-end run.

## 1. What Linux does today (measured)

| fact | value |
|---|---|
| card | `sof-hda-dsp`, `ONE_NETBOOK-ONEXPLAYER3-Defaultstring`, `snd_soc_skl_hda_dsp` |
| codec | Realtek **ALC245** (`0x10ec0245`, subsystem `0x1f751602`), 1 HDA codec only |
| controllers | `00:1f.3` Intel `8086:e428` (Core Ultra Series 3 HD Audio) |
| smart amps | **none** - no TAS/CS35L41/RT-class amp in `/sys/bus/i2c`, so the speakers hang off the codec DACs and all vendor "tuning" is DSP, not hardware |
| PipeWire | 1.6.9 (`pactl info`: "PulseAudio (on PipeWire 1.6.9)"), server 15.0.0, `float32le 2ch 48000Hz` |
| sinks | `HiFi__Speaker__sink`, `HiFi__HDMI1..3__sink`; source `HiFi__Mic1__source` |
| active profile | `HiFi (HDMI1, HDMI2, HDMI3, Mic1, Speaker)`; headphones profile exists but the jack is `off` |
| mixer | `Master Playback Volume` 87/87, `Speaker Playback Switch` on, `Speaker Playback Volume` 0x52; `Speaker Phantom Jack` on |
| journal | `snd_sof_pci_intel_ptl`, `snd_hda_codec_alc269`, `snd_soc_skl_hda_dsp` - stock SOF IPC4 path, no quirk for this board |

So there is no linux-side tuning to "fix"; the whole gap is the missing APO.

## 2. How Windows composes the audio path

* `AudioEFXAPO64.dll` exports six render APOs (EFX/MFX/SFX plus Offload variants):

  | role | CLSID |
  |---|---|
  | MFX (**the one this package installs**) | `{23A98C03-FA1E-4137-ADC7-9CFA50A833E4}` |
  | EFX | `{649B02ED-D042-44A5-A155-90276859DE78}` |
  | SFX | `{9D643512-8584-4D97-8FCC-179452ACCF97}` |
  | MFX/EFX/SFX Offload | `{CD29595B-...}`, `{EA21698A-...}`, `{1D345243-...}` |

* `install.cmd` does: stop `Audiosrv` -> copy DLL + `AudioEFXService64.exe` ->
  register the service -> `regsvr32` -> run `APORegAdd.exe 0 ACTIVE_DEVICE 7
  {23A98C03-...}` -> import `GA18v3.reg` -> start Audiosrv.
  `APORegAdd` writes the CLSIDs into
  `HKLM\...\MMDevices\Audio\Render\<endpoint>\FxProperties`, i.e. the APO is attached
  to the **active endpoint only**, as **MFX** (mode `7`), not as a driver INF APO.
* There is **no INF in the driver package that references AudioEFX**: the APO is a
  registry bolt-on, so on this machine it applies to whichever endpoint was default
  when `install.cmd` ran (the speakers).
* The endpoint identity used by the APO is `PKEY_AudioEndpoint_GUID`
  (`...\Render\<endpoint>\Properties`). The shipped `.reg` carries four such GUIDs,
  and they are the standard `KSNODETYPE` endpoint GUIDs from the Intel SST INF
  (`intcsst.inf`: `KSNODETYPE_SPEAKER = {DFF21CE1-F70F-11D0-B917-00A0C9223196}`), so
  they are stable across machines, not per-install:

  | GUID | endpoint | tuned? |
  |---|---|---|
  | `{DFF21CE1-F70F-11D0-B917-00A0C9223196}` | SPRK / internal speakers | **yes, 177 floats** |
  | `{DFF21CE2-F70F-11D0-B917-00A0C9223196}` | headphones | no (3 flag words) |
  | `{DFF21FE3-F70F-11D0-B917-00A0C9223196}` | S/PDIF family | no |
  | `{C1A3088B-E1BB-425B-8ABA-65DC3B3675A7}` | fourth endpoint | no |

* `AudioEFXService64.exe` is a singleton service that owns the DSP state and talks to
  the APO through `Global\ICEsoundService*` shared memory; the PDB path
  (`...\APOwithHSlimiter\...`) says the limiter ("HS" = headset/speaker) is the point
  of this build.

## 3. Parameter format (recovered)

`Settings\N` holds `Param1..Param1580` as `hex:` DWORDs; the APO reads each value as a
raw 32-bit word and reinterprets it as float32 (`RegQueryValueExW` -> copy -> float).
A preset is therefore **1580 floats**. Blocks are concatenations of per-module
parameter structs; the module we care about is an **EQ block of 10 bands x 5 floats**:

```
band = ( freq_Hz , gain_linear , Q , selector , enable )
```

* `gain_linear` is exactly `10**(dB/20)` (0.707946 = -3 dB, 1.41254 = +3 dB,
  1.99526 = +6 dB, 7.94328 = +18 dB), which is how the tables below are back-converted.
* `selector` is a small integer (`0`, `1`, `5`, `15`) that picks the biquad family in
  the designer; the designer (`FUN_18002eb54` in the DLL) implements
  `case 0 = gain only, 1 = peaking (alpha = sin/2Q), 2 = lowpass, 3 = highpass,
  4/5 = shelving (sqrt(gain)), 6/7 = notch/allpass-style`. Flat presets use
  `selector 0` with `gain 1.0`.
* The default (factory) preset in the service exe has **every band at 0 dB**, which is
  what makes the extraction self-checking: an untuned endpoint is bit-identical.

`apo_extract.py --reg ... --exe ... --csv out.csv` prints the block offsets, the
changed bands and the non-EQ deltas.

## 4. Vendor-neutral parameter table (speaker endpoint, active preset 2)

Gains are dB; Q as stored. "Stage 1" is the voicing EQ and is stored twice
(param offsets 19 and 69, bit-identical -> L and R). "Stage 2" is stored once
(offset 1435).

**Stage 1 - voicing (offset 19 / 69)**

| # | freq | gain | Q | selector | role (inferred) |
|---|---|---|---|---|---|
| 1 | 80 Hz | -3.0 | 0.7 | 1 | subsonic / port-noise cut |
| 2 | 140 Hz | +6.0 | 2.0 | 0 | bass lift |
| 3 | 140 Hz | +3.0 | 1.5 | 0 | bass lift, wide |
| 4 | 400 Hz | +3.0 | 0.9 | 1 | body / warmth |
| 5 | 4500 Hz | -2.0 | 2.0 | 0 | harshness |
| 6 | 6500 Hz | -4.0 | 2.0 | 0 | cone / port resonance |
| 7 | 7500 Hz | -4.0 | 2.0 | 0 | cone / port resonance |
| 8 | 10000 Hz | +3.0 | 3.0 | 0 | air |
| - | 8000 Hz | 0.0 | 1.0 | 0 | flat (untouched slot) |
| - | 16000 Hz | 0.0 | 1.0 | 1 | flat, selector changed only |

**Stage 2 - resonance control, all cut (offset 1435)**

| # | freq | gain | Q | selector |
|---|---|---|---|---|
| 1 | 95 Hz | -3.0 | 0.7 | 1 |
| 2 | 250 Hz | -3.0 | 4.0 | 1 |
| 3 | 400 Hz | -3.0 | 0.9 | 1 |
| 4 | 650 Hz | -5.0 | 3.0 | 1 |
| 5 | 700 Hz | -2.0 | 3.0 | 1 |
| 6 | 4500 Hz | -7.0 | 4.5 | 1 |
| 7 | 7000 Hz | -6.0 | 4.0 | 1 |
| 8 | 13000 Hz | -4.0 | 7.0 | 0 |
| 9 | 15000 Hz | -3.0 | 0.7 | 0 |

Cascaded, stage 1 + stage 2 give **+4.8 dB max @ 143 Hz** and **-14.3 dB min @
6.98 kHz** before headroom, i.e. a warm, deliberately de-harshed voicing. Stage 1
alone is +7.74 dB @ 142 Hz / -7.3 dB @ 6.85 kHz.

**Dynamics / limiter block (param 221-246)**

| field | vendor | factory | reading |
|---|---|---|---|
| threshold | 0.607 | 1.000 | **-4.34 dBFS** |
| attack | 0.200 | 0.060 | 200 ms |
| release | 0.360 | 0.500 | 360 ms |
| ratio (2 fields) | 1.99 / 2.5 | 4 / 3 | ≈2:1 .. 2.5:1 |
| knee | 0.5 | 0.5 | unchanged |
| ceiling / makeup-ish | 140 / 2.5 | 150 / 3 | reduced ~1 dB |

Field order and units are inferred from the numbers and the surrounding parameter
layout; the *values* are exact. Treat the semantic labels as best effort.

**Other changed words (exact, meaning partly unresolved)**

| param (1-based) | vendor | factory | comment |
|---|---|---|---|
| 5 | 0 | 1 | module bypass/mode word |
| 13 | 1.12202 | 1 | **+1 dB** static gain (gain staging) |
| 17, 18 | 1 / 3 | 0 / 1 | module flags next to stage 1 |
| 259-264 | 10121 / 3 / 1 | 10000 / 2 / 3 | crossover/HPF corner moved to **10.12 kHz** |
| 288-306 | 0.95 / 0.062 / 0.4 / 0.004 | 0.85 / 0.021 / 0.2 / 0 | time constants (detector) |
| 323, 326 | 0.988553 / 1 | 1.19 / 0 | limiter make-up / enable |
| 1074-1077, 1110, 1113-1114, 1180 | 4 / 2 / 4 / 2, 1, 0.251189, 2.23872, 2 | all 0, 0.0708, 2 | second dynamics block (gate/expander flags) |
| 1360-1364 | 1, **462, 5616, 1, 7.94328** | 0, 250, 8000, 1, 1 | **level-dependent block, +18 dB max between 462 Hz and 5616 Hz** - unresolved, see open points |
| 1420-1431 | 400, -6, 10, 100, 0, 0, 7 / -6, 20, 200 ... | 5000, -10, 1, 500, -0.848, 2.268 ... | multi-band dynamics in **dB** domain |

## 5. Implementation on PipeWire

`tools/audio/oxp3-speaker-eq.conf` is a `libpipewire-module-filter-chain` graph that
creates a sink `oxp3_speaker_eq.sink` and a passive output stream pinned to the
hardware speaker sink:

```
headroom (-8 dB low shelf @ 20 kHz) -> b1 80 Hz -3 Q0.7 -> b2 140 Hz +6 Q2.0
  -> b3 140 Hz +3 Q1.5 -> b4 400 Hz +3 Q0.9 -> b5 4500 Hz -2 Q2.0
  -> b6 6500 Hz -4 Q2.0 -> b7 7500 Hz -4 Q2.0 -> b8 10000 Hz +3 Q3.0
```

Why the odd headroom node: filter-chain has no plain gain element (`label = gain`
does not exist on this build - it fails with `cannot create label gain`; a newer
build adds one but the shelf is kept for compatibility), so a low shelf parked at
20 kHz acts as -8 dB across the audio band and approaches 0 dB at Nyquist (24 kHz).
-8 dB is the stage-1 peak (+7.74 dB), so the chain cannot clip even at 0 dBFS -
measured: a 0 dBFS tone leaves -7.38 dBFS at 1 kHz and -0.26 dBFS at the 142 Hz
worst case (no clipped samples). If you want the vendor's loudness instead of headroom,
use the EasyEffects route below, which has a real limiter.

Stage 2 is present but commented out: uncomment `s1..s9` and add the links
`b8:Out -> s1:In ... s9:Out` if you want the full vendor preset (then -5 dB headroom
is enough).

Install / switch (user scope, no root, no host changes):

```bash
tools/audio/install-user.sh      # writes ~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf
tools/audio/eq-on.sh             # + restart pipewire/wireplumber, waits for the sink, makes it default
tools/audio/eq-off.sh            # removes the drop-in, restores the hardware speaker as default
```

Verify:

```bash
pactl list sinks short | grep oxp3_speaker_eq        # sink exists and is default
wpctl status | sed -n '/Filters:/,/Streams:/p'      # filter-chain with 2 nodes
tools/audio/eq_response.py                          # model: 8 bands, max -1.6 dB, min -15.0 dB
tools/audio/measure-speaker.py --gen /tmp/tones.wav # test signal (22 tones, 125 Hz..16 kHz)
```

If you would rather keep a dynamic limiter, use EasyEffects instead of the
filter-chain: one *Equalizer* (IIR) with the eight stage-1 bands above, then a
*Limiter* with **threshold -4.3 dB, attack 200 ms, release 360 ms, lookahead 5 ms**
(the vendor's numbers). That route is not shipped as a preset file because the
EasyEffects preset schema is version-specific.

## 6. Validation

Two independent checks, both reproducible:

1. **Model vs the vendor blob** - `apo_extract.py` re-reads the preset and prints the
   same eight bands; the extractor's factory-table cross-check found the table at
   `0xcf6a0` in the service exe and 1580/1580 values match for untuned endpoints.
2. **Filter chain vs model, measured in a real (isolated) PipeWire graph** - the
   shipped config is loaded unchanged into a private PipeWire 1.6.9 instance
   (private `XDG_RUNTIME_DIR`, null sink, hardware-free WirePlumber, no ALSA
   object) and measured with a 50 Hz - 16 kHz log sweep against a reference run
   with the filter bypassed (`tools/audio/test-isolation.sh`):

   ```
        Hz   measured      model     diff
       126      -1.88     -1.79    -0.09
       252      -5.31     -5.31    +0.00
       635      -6.23     -6.25    +0.02
      1008      -7.39     -7.39    +0.01
      2016      -8.15     -8.15    +0.00
      4032     -10.66    -10.70    +0.04
      6400     -14.59    -14.63    +0.04
      8063     -12.40    -12.29    -0.11
      mean abs error 0.029 dB, max 0.111 dB over 20 bands (100 Hz - 10 kHz)
   ```

   i.e. the emitted `bq_*` parameterisation and `eq_response.py` agree, and the
   numbers in the config are the numbers in the table. The full 26-band table,
   the raw daemon log and the re-run commands are in
   [`docs/audio-validation.md`](../audio-validation.md).

3. **Limiter replacement and crossover** - the chain's protection is the static
   -8 dB headroom (filter-chain has no dynamics builtin): hot 0 dBFS tones are
   measured at -7.38 dBFS (1 kHz) and -0.26 dBFS (142 Hz) with zero clipped
   samples, recovering instantly (no detector, no pumping). The vendor limiter's
   threshold/attack/release are validated only as an offline model (1.90 dB
   attenuation, -0.08 dB residual after release). An LR4 10.12 kHz crossover
   built from `bq_lowpass`/`bq_highpass` is real-graph verified: -6.0/-5.8 dB at
   the corner, each branch passing its side, and a 0.00 dB flat sum. Details in
   [`docs/audio-validation.md`](../audio-validation.md).

**Acoustic measurement of the real speakers** (what the user should do, since a
software loopback cannot hear the drivers):

```bash
# 1. play the test signal through the EQ sink, record with a mic ~10 cm on axis
tools/audio/measure-speaker.py --gen /tmp/oxp3-tones.wav
pw-play --target oxp3_speaker_eq.sink /tmp/oxp3-tones.wav &
pw-record --rate 48000 --channels 1 /tmp/oxp3-tones-rec.wav      # mic as default source
# 2. compare recorded level per band with the model
tools/audio/measure-speaker.py --analyze /tmp/oxp3-tones-rec.wav
# with EQ on vs off: do it twice (eq-off.sh / eq-on.sh) and subtract the two results
```

Expect a couple of dB of room/mic error; run the measurement twice (EQ on/off) and
compare the *difference*, which cancels mic and room colour.

## 7. Why the vendor does this (speaker-protection rationale)

* **Headroom**: the limiter threshold is 4.34 dB below full scale and the EQ boosts up
  to +7.7 dB at 142 Hz. Without the limiter, the small drivers see digital full scale
  plus 8 dB of bass boost - that is how laptop/tablet speakers get destroyed at high
  volume.
* **Speed of protection**: 200 ms attack / 360 ms release is a slow compressor, not a
  clip limiter - it is thermal/excursion protection, riding gain on sustained bass.
* **Subsonic cut** at 80-95 Hz (Q 0.7) removes content the drivers cannot reproduce,
  which otherwise only produces excursion and port noise.
* **Narrow cuts** at 4.5-7.5 kHz (Q 2-7) tame cone/port resonances and the "harsh"
  band of a tiny full-range driver; the +3 dB at 400 Hz and 10 kHz is the voicing
  that makes the result still sound forward.
* Our static -8 dB headroom replaces the limiter's protection with a fixed margin - it
  is conservative but cannot pump, and it cannot protect the driver from a DC/low
  frequency transient the way the vendor's time constants would.

## 8. Rollback

Nothing outside `~/.config` is touched, so:

```bash
tools/audio/eq-off.sh                       # normal rollback
# or manually:
rm -f ~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf
systemctl --user restart pipewire wireplumber
pactl set-default-sink alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink
```

The Windows side is not modified at all; `GA18v3.reg` was only read.

## 9. Open points / cautions

* **Stage 2 is not proven to be in the audio path.** It is stored once (stage 1 is
  stored per channel), and an all-cut block with Q up to 7 is exactly what a
  limiter's side-chain weighting filter looks like. That is why it ships commented
  out; enable it only if an A/B measurement says it matches Windows.
* **`selector` semantics** are inferred from the DLL's biquad designer, not proven
  per band. Bands with `selector 1` (80 Hz, 400 Hz in stage 1) may be shelving
  filters rather than peaking ones; the gains are small enough (<3 dB) that the
  audible difference is minor, but a shelving interpretation would change the
  80 Hz corner.
* **The 462 Hz - 5616 Hz, +18 dB block (param 1360-1364)** is enabled in the vendor
  preset and looks like volume-dependent loudness (Harman APOs have one). We do not
  reproduce it: it is level-dependent and +18 dB is not a safe static boost.
* **Per-speaker differences**: there are none to reproduce - the device has one
  stereo pair, no smart amps, and the vendor stores the same stage-1 table for both
  channels.
* **Incident during validation (2026-11-09 00:04)**: a *second* PipeWire instance with
  its own WirePlumber grabbed the ALSA speaker PCM while the session was using it.
  `snd_sof` then hit a kernel WARNING in `sof_ipc4_widget_setup` (NULL write,
  `WARNING: CPU ... RIP sof_ipc4_widget_setup+0x205`) and one WirePlumber process is
  stuck in `dpcm_fe_dai_close` (D state), so the card stays busy and the session only
  shows "Dummy Output". A reboot was required to get the speakers back.
  Lesson: never start a second PipeWire+WirePlumber against the live session.
  `tools/audio/test-isolation.sh` is the safe way to do a graph test: private
  runtime/config, only `support.null-audio-sink` and the filter chain, a private
  WirePlumber with **all hardware monitors disabled** and (on a host with
  `/dev/snd`) a bubblewrap sandbox with an empty `/dev/snd`, plus a hard check
  that the private session manager has no `/dev/snd` fd open. See
  [`docs/audio-validation.md`](../audio-validation.md).
  The kernel warning itself is worth reporting upstream: two clients driving the same
  SOF IPC4 PCM concurrently leaves a widget with a NULL data pointer.
