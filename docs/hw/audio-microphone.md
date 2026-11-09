# Internal microphone (ALC245 digital-mic pin)

**State: diagnosis complete, opt-in software fix shipped, audio not yet proven on the
device.** The microphone is *not* missing because the firmware omits an NHLT DMIC entry:
it is the Realtek ALC245's fixed digital-microphone pin `0x12`, which Linux already parses
and routes to the capture ADC. What is missing is an *exposure*: the shipped UCM2 profile
creates one capture device, gated on the external `Mic Jack` control, so PipeWire marks the
only source `not available` while nothing is plugged into the 3.5 mm combo jack and no
source is labelled internal.

## 1. Evidence (all read-only, all reproducible)

| what | command | observed on this machine |
|---|---|---|
| card | `cat /proc/asound/cards` | `0 [sofhdadsp]: sof-hda-dsp - sof-hda-dsp`, long name `ONE_NETBOOK-ONEXPLAYER3-Defaultstring` |
| kernel DMIC line | `dmesg \| grep 'DMICs detected'` | `sof-audio-pci-intel-ptl ... DMICs detected in NHLT tables: 0` |
| codec | `cat /proc/asound/card0/codec#0` | `Codec: Realtek ALC245`, `Subsystem Id: 0x1f751602` |
| internal mic pin | same file, node `0x12` | `Pin Default 0x90a60130: [Fixed] Mic at Int N/A`, `Conn = Digital`, `Misc = NO_PRESENCE`, `Pin-ctls: 0x20: IN` |
| external mic pin | same file, node `0x19` | `Pin Default 0x04a19040: [Jack] Mic at Ext Right`, `Unsolicited: tag=02, enabled=1` |
| codec route | same file | ADC `0x08` -> mixer `0x23` input 4 = `0x12`, `Amp-In vals: ... [0x00 0x00]` (un-muted); the `0x19` input is muted (`0x80`) |
| capture controls | `amixer -c0 contents` | `Capture Volume` (numid 6), `Capture Switch` (numid 7, **off,off**), `Internal Mic Boost Volume` (numid 8), `Internal Mic Phantom Jack` (numid 12, **on**), `Mic Jack` (numid 13, **off**) |
| no source selector | `amixer -c0 contents \| grep -i source` | nothing: ALC245 uses a *mixer* (0x23) plus auto-mic, not a selectable `Input Source`/`Capture Source` enum |
| capture PCMs | `cat /proc/asound/devices` | only `[0-0]: digital audio capture` (no `[0-6]` DMIC PCM) |
| PipeWire | `pactl list sources` | one source, `...HiFi__Mic1__source` "Stereo Microphone"; port `[In] Mic1 ... availability group: Mic, **not available**` |
| card components | `pactl list sources` properties | `alsa.components = "HDA:80862822,... HDA:10ec0245,1f751602,00100001 iec61937-pcm:5,4,3"` (no `cfg-dmics:`) |
| UCM profile in use | `ls /usr/share/alsa/ucm2/conf.d/sof-hda-dsp/` | only the symlink `sof-hda-dsp.conf`; no `ONE_NETBOOK-ONEXPLAYER3-Defaultstring.conf` |
| UCM gate | `grep DeviceMicJack /usr/share/alsa/ucm2/HDA/HiFi-mic.conf` | `DeviceMicJack "${var:MicJackControl}"` -> the mic device carries `Value.JackControl "Mic Jack"` |
| repo tool | `tools/audio/mic_diag.py` | `verdict: gated` — pin `0x12` routed to ADC `0x08`, no always-available source |

`tools/audio/mic_diag.py --json` in `tools/oxp3-verify.sh` (`aud.mic`) records the same
evidence so a later kernel/UCM change cannot silently remove the path.

## 2. Diagnosis

**(b) the codec's digital-mic pin, not exposed by the current UCM.** Supporting evidence:

1. The BIOS pin configuration declares a *fixed internal* microphone on the codec
   (`0x90a60130` = Fixed/Internal/Mic-In/Digital), and the pin is live (`Pin-ctls 0x20 IN`).
   A PCH DMIC would not appear as a codec pin at all.
2. The codec widget tree already routes that pin to the capture ADC: `0x08` <- `0x23`
   input 4 (`0x12`, un-muted) <- pin `0x12`, and `0x08` is the ADC that owns
   `Capture Volume`/`Capture Switch`. The route is *already selected* because the
   external mic jack (`0x19`) reports no plug, so the generic parser's auto-mic picked
   the internal input.
3. `Internal Mic Phantom Jack` is **on** and `Internal Mic Boost Volume` exists: the
   kernel's generic parser did classify `0x12` as an internal microphone.
4. The `DMICs detected in NHLT tables: 0` line only counts *PCH* DMIC endpoints from the
   ACPI NHLT table (see `check_dmic_num()` in `sound/soc/sof/intel/hda.c`). It is
   consistent with (b) and does not say the microphone is absent: this microphone is
   behind the codec, not the PCH DMIC links.
5. Only one capture PCM exists (`hw:0,0`) and the PipeWire source opens it. The source is
   `not available` purely because the UCM device's `JackControl` is the *external*
   `Mic Jack`, which reads `off`; nothing is wrong with the PCM itself.

**Not (a) PCH DMIC:** the NHLT table (v00 ALASKA A M I) has no DMIC endpoint, there is no
`hw:0,6` capture PCM, the card components carry no `cfg-dmics:`, and the BIOS instead
configured a codec digital-mic pin. Forcing `dmic_num` would only make SOF advertise a
DMIC link that the firmware does not describe.

**Not (c) USB/EC:** the path is on the HDA codec (`10ec0245`), visible in `codec#0`; there
is no USB audio device and no EC microphone in the card list.

## 3. What remains unknown

* Whether the array actually *produces signal* on this board. The route and the exposure
  are proven; audio capture is deliberately not run by the harness (`arecord`/`pw-record`
  against the hardware is an owner action, see §4.1). An ALC245 DMIC pin does record once
  the ADC is routed — the Minisforum V3/V3 SE fix relies on exactly that codec path
  ([patch](https://lists.openwall.net/linux-kernel/2026/09/27/807),
  [thread](https://lkml.org/lkml/2026/9/28/1725)).
* Whether this BIOS's DMIC wiring matches the generic ALC245 expectation (clock/PDM
  polarity). If the owner's capture is silent even with `Capture Switch` on, the next step
  is the same kind of `alc269.c` quirk the Minisforum V3 needed, not an NHLT change.
* Nothing about `dmic_num`/`snd_hda_intel.dmic_detect`: both address the PCH DMIC path,
  which this machine does not use.

## 4. Fixes, ordered by risk

### 4.1 No change: prove the hardware path with a raw capture (do this first)

```bash
amixer -c0 cget numid=7                       # Capture Switch; may be off
amixer -c0 cset numid=7 on                    # runtime only, resets at reboot/replug
arecord -D hw:0,0 -f S16_LE -c1 -r48000 -d3 /tmp/oxp3-mic.wav
python3 - <<'EOF'
import wave, struct, math
w = wave.open('/tmp/oxp3-mic.wav'); d = w.readframes(w.getnframes())
s = struct.unpack('<%dh' % (len(d)//2), d)
print('peak', max(map(abs, s)), 'rms', int(math.sqrt(sum(x*x for x in s)/len(s))))
EOF
```

* Risk: none (runtime mixer value only; the next UCM device activation/boot restores it).
* Pass: non-trivial peak/RMS while talking near the device; `Internal Mic Phantom Jack`
  stays `on`. Fail (all-zero/peak < a few hundred): stop here, the codec DMIC unit or its
  wiring is the problem — open §4.5 instead of installing the UCM profile.

### 4.2 Recommended: opt-in UCM2 machine profile (exposes the source persistently)

```bash
sudo tools/audio/internal-mic.sh install
systemctl --user restart pipewire wireplumber   # as the desktop user
tools/audio/mic_diag.py                         # verdict: exposed
tools/oxp3-verify.sh                            # aud.mic PASS
pactl list sources | grep -A3 'Internal Microphone'
```

What it installs (four files, nothing else):

* `/usr/share/alsa/ucm2/conf.d/sof-hda-dsp/ONE_NETBOOK-ONEXPLAYER3-Defaultstring.conf` —
  the UCM2 machine profile (`conf.d/<driver>/<CardLongName>.conf` is looked up *before*
  `conf.d/<driver>/<driver>.conf`, see `ucm2/ucm.conf`). It includes the shipped
  `sof-hda-dsp.conf` and then the card override below.
* `/usr/share/alsa/ucm2/oxp3/oxp3-onEXPLAYER3-usecase.conf` — points `SectionUseCase."HiFi"`
  at the wrapper.
* `/usr/share/alsa/ucm2/oxp3/oxp3-onEXPLAYER3-HiFi.conf` — the use-case wrapper: includes
  the shipped `/Intel/sof-hda-dsp/HiFi.conf` (verb, Headphones, Speaker, HDMI, SOF
  EQ/DRC), then the microphone override.
* `/usr/share/alsa/ucm2/oxp3/oxp3-onEXPLAYER3-mic.conf` — re-declares **only**
  `SectionDevice."Mic"` with `JackControl "Internal Mic Phantom Jack"` and
  `CaptureMasterElem "Internal Mic Boost"`; every other value is the shipped one, and the
  device is written in full so it stays valid if a parser ever replaces the subtree.

Why overrides are separate include files: the UCM include pass merges an included file
with `snd_config_merge(parent, included, 0)`, which *substitutes* duplicate scalar values
(`src/conf.c`), i.e. the **later include wins**. A `SectionUseCase` or `SectionDevice`
declared before the include would be overwritten by the shipped value. Listing each
override as the second include is what makes it effective; the shipped `HiFi.conf` uses the
same ordering when it includes `HiFi-sof.conf` after `HiFi-analog.conf`. If the override
were ignored, the result is stock behaviour (mic still gated), never a missing speaker.

* Risk: low-to-medium. No shipped file is edited and playback devices still come from the
  shipped use case, so a wrong wrapper cannot remove the speaker. A UCM syntax error would
  make PipeWire fall back for this card, which is why the installer refuses foreign files,
  backs up anything it overwrites, validates with `alsaucm` when available, and rollback is
  one command.
* Rollback: `sudo tools/audio/internal-mic.sh undo` +
  `systemctl --user restart pipewire wireplumber`.
* The codec keeps its auto-mic behaviour: plugging a headset still selects the `0x19`
  headset mic on the same PCM, so the "Internal Microphone" source then carries the
  headset mic (the port label is the only imprecision).

### 4.3 Not recommended: `dmic_num` / `dmic_detect` module options

`options snd_sof_intel_hda_generic dmic_num=2` (the parameter moved from
`snd_sof_intel_hda_common` to `snd_sof_intel_hda_generic`; see the Fedora thread
[Fujitsu FMV UH-X](https://discussion.fedoraproject.org/t/fujitsu-fmv-uh-x-inbuilt-microhone-not-working-in-fedora-39-intel-raptor-lake-p-u-h/95618/6))
forces SOF to *claim* PCH DMICs. On this machine the NHLT has no DMIC endpoint and the
microphone is on the codec, so at best this adds an empty `hw:0,6` and confuses UCM's
`cfg-dmics` device selection, at worst it advertises a broken DMIC link. Do not use it
unless `mic_diag.py` says `pch-dmic`.

### 4.4 Not needed unless §4.1 fails: kernel quirk

The Minisforum V3/V3 SE (`ALC245`, PCI SSID `1f4c:e001`) needed a `patch_realtek` quirk
because pin `0x19` had no presence detect and auto-mic stayed on the empty jack. Here
`Mic Jack` correctly reads `off` and the ADC is already on `0x12`, so that quirk is not
applicable; a quirk would only be considered if the raw capture in §4.1 is silent with
`Capture Switch` on.

### 4.5 Firmware / vendor only if §4.1 is silent

Asking for an NHLT DMIC entry only makes sense if the microphone is a PCH DMIC. Before
contacting the vendor, check Windows: Device Manager -> Sound, video and game controllers;
if the microphone is enumerated with the *Realtek* codec (not "Intel Smart Sound
Technology ... DMIC"), the array is on the codec and no NHLT change can help. What to ask
for either way: the ALC245 pin-configuration/verb table the Windows driver uses for the
internal DMIC (clock, PDM polarity) and, if Windows shows an SST DMIC endpoint, the NHLT
DMIC device entry.

## 5. How to tell it worked

* `tools/audio/mic_diag.py` -> `verdict: exposed`, and the UCM line names
  `'Internal Microphone'` with `jack 'Internal Mic Phantom Jack'`.
* `tools/oxp3-verify.sh` -> `[PASS ] audio aud.mic`; `pactl list sources` shows a source
  whose description contains `Internal Microphone` and whose port is *not*
  `not available`.
* Talking into the device with the record command from §4.1 produces a non-zero waveform.

## 6. Files

| path | role |
|---|---|
| `tools/audio/mic_diag.py` | read-only classifier (`--json` for the verifier) |
| `tools/audio/internal-mic.sh` | opt-in `install` / `undo` / `status` / `--dry-run` (four files, manifest, marker-checked undo) |
| `files/ucm2/oxp3-onEXPLAYER3.conf` | machine card profile (includes the shipped profile, then the card override) |
| `files/ucm2/oxp3-onEXPLAYER3-usecase.conf` | card-level `SectionUseCase."HiFi"` override |
| `files/ucm2/oxp3-onEXPLAYER3-HiFi.conf` | use-case wrapper (shipped HiFi + microphone override) |
| `files/ucm2/oxp3-onEXPLAYER3-mic.conf` | the `SectionDevice."Mic"` override itself |
| `tests/unit/test_audio_mic.py` | synthetic proc/UCM/amixer fixtures pinning every verdict |

## 7. References

* Minisforum V3 / V3 SE ALC245 internal-mic fix (codec pin routing, `GET_PIN_SENSE`):
  <https://lists.openwall.net/linux-kernel/2026/09/27/807>,
  <https://lkml.org/lkml/2026/9/28/1725>,
  <https://patchew.org/linux/20260928152925.69032-1-jwuerz@gmail.com/>
* `dmic_num` (PCH DMIC path) reports:
  <https://discussion.fedoraproject.org/t/fujitsu-fmv-uh-x-inbuilt-microhone-not-working-in-fedora-39-intel-raptor-lake-p-u-h/95618/6>
* SOF source used for the NHLT line: `sound/soc/sof/intel/hda.c` `check_dmic_num()` in the
  installed kernel tree (`kbuild-an19-src`, 7.2.7-an28); UCM2 lookup order:
  `/usr/share/alsa/ucm2/ucm.conf`.
* Web sources are cited as data; the on-device evidence in §1 is what the diagnosis rests
  on.
