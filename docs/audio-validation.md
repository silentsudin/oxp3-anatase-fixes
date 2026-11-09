# Harman EQ: validation in a real PipeWire graph (isolated)

Status: **the shipped filter chain now has an end-to-end, hardware-free proof.**
The chain parses, loads into a real PipeWire graph, and its measured response
matches the model in [`tools/audio/eq_response.py`](../tools/audio/eq_response.py)
to **0.029 dB mean / 0.111 dB max** (1/3 octave, 100 Hz - 10 kHz). The limiter
replacement and the 10.12 kHz crossover corner are characterised too. Nothing
on the live session or the host was touched.

Owner: silentsudin. Host: Anatase `20261026.3`, kernel `7.2.7-an28.fc45`,
PipeWire 1.6.9, WirePlumber 0.5.18.

- Harness: [`tools/audio/test-isolation.sh`](../tools/audio/test-isolation.sh)
- Measurement: [`tools/audio/measure-in-isolation.py`](../tools/audio/measure-in-isolation.py)
- Crossover graph: [`tools/audio/test-xover.conf`](../tools/audio/test-xover.conf)
- Container: [`container/audio-test.Containerfile`](../container/audio-test.Containerfile),
  [`tools/audio/test-in-container.sh`](../tools/audio/test-in-container.sh)
- Repo suite: `audio-eq-isolation` in [`tools/repo-test.sh`](../tools/repo-test.sh)
- Unit tests: [`tests/unit/test_eq_measure.py`](../tests/unit/test_eq_measure.py)

## 1. Why an isolated instance is safe

The 2026-11-09 incident (a second PipeWire **plus WirePlumber** grabbed the
ALSA speaker PCM and wedged `snd_sof`) is designed out:

| risk | what the harness does |
|---|---|
| socket collision / attaching to the live session | `mktemp -d .../audiotest.XXXXXX`; `XDG_RUNTIME_DIR` **and** `PIPEWIRE_RUNTIME_DIR` point into it, `PIPEWIRE_REMOTE` and `PIPEWIRE_CONFIG_DIR` are removed from every client's environment |
| hardware probing by the daemon | private `pipewire.conf` loads only protocol-native, client-node, adapter, spa-node-factory, link-factory, metadata, access, rt. No `api.alsa.*` there. The only sink is `support.null-audio-sink` (`iso_null`) plus a `Dummy-Driver` |
| a second session manager grabbing the card | private WirePlumber with **every hardware monitor disabled** (`hardware.audio`, `hardware.bluetooth`, `hardware.video-capture = disabled`) and, on a host that has `/dev/snd`, started under `bwrap --dev-bind / / --tmpfs /dev/snd` so the ALSA nodes do not exist in its namespace. The harness also fails if any `/proc/<wp>/fd` points into `/dev/snd` |
| leaked state | everything lives in the one `mktemp` dir; `trap` kills the daemon and WirePlumber, then removes the tree. `--keep-runtime` keeps it for debugging |
| live-session side effects | the test never runs `pactl`/`wpctl` against the user server, never restarts a unit, never writes outside the private tree |

The graph is nevertheless a **real** one: WirePlumber does the linking exactly
as it does for the real sink, the filter chain is the shipped
`tools/audio/oxp3-speaker-eq.conf` (only its playback `node.target` is rewritten
from the ALSA sink to `iso_null` - the same rewrite `install-user.sh` does for
the real sink).

## 2. Tool availability on this host

| tool | state |
|---|---|
| `pipewire` 1.6.9, `pw-cli`, `pw-dump`, `pw-play`, `pw-record`, `pw-link` | **present** (`/usr/bin`) |
| `wireplumber` 0.5.18 | present |
| `bubblewrap` 0.12.0 | present (hardware sandbox) |
| `python3` 3.15 (wave/struct/math only) | present |
| `sox`, `ffmpeg` | **missing** - not needed: the sweep and hot tones are generated with `wave`/`struct`, no shell audio tool is required |
| `numpy` | **missing** on the host - the harness falls back to a built-in radix-2 FFT (0.3 s for 2^18 points). The container image installs `python3-numpy` and uses it automatically |
| `podman` 6.1.3 | present; container path verified (§7) |

So the whole check runs on this host with stdlib Python; the container is the
reproducible fallback, not a requirement.

## 3. Exact commands

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

# full check: parse proof + EQ transfer function + two hot tones + crossover
tools/audio/test-isolation.sh

# machine-readable result (and keep the runtime tree for inspection)
tools/audio/test-isolation.sh --keep-runtime --json /tmp/audiotest-result.json

# fast variant used by the offline repo suite (short sweep, no hot tones)
tools/audio/test-isolation.sh --quick

# through the repo-test runner (skips cleanly when PipeWire tooling is absent)
tools/repo-test.sh --only audio-eq-isolation

# unit tests for the comparison maths (no PipeWire)
python3 -m unittest discover -s tests/unit -p 'test_eq_measure.py' -q
```

Exit codes: `0` pass, `1` fail, `2` usage, `77` skip (missing PipeWire
tooling / WirePlumber / bubblewrap where `/dev/snd` exists). The skip path was
exercised with `PATH` containing no PipeWire client: the harness prints
`SKIP: missing PipeWire tooling: pipewire pw-cli ...` and exits 77, which the
repo-test runner maps to SKIP.

## 4. Config parse proof (daemon log, `--keep-runtime`)

```
isolation: runtime=/tmp/audiotest.77I69x/runtime  ...
isolation: wireplumber runs under bubblewrap with an empty /dev/snd
== phase eq: shipped filter chain against a null sink
config: libpipewire-module-filter-chain loaded, no parse errors
graph: 4 nodes, filter-chain oxp3_speaker_eq.sink + oxp3_speaker_eq.out present, no ALSA objects
```

Daemon log (`logs/eq-daemon.log`, `PIPEWIRE_DEBUG=3`):

```
[I] pw.conf | load_module()] loaded module libpipewire-module-protocol-native
[I] pw.conf | load_module()] loaded module libpipewire-module-access
[I] spa.filter-graph | plugin_load()] successfully opened 'builtin':'builtin'
[I] spa.filter-graph | parse_link()] linking headroom:Out -> b1:In
[I] spa.filter-graph | parse_link()] linking b1:Out -> b2:In
...
[I] spa.filter-graph | parse_link()] linking b7:Out -> b8:In
[I] mod.filter-chain | graph_info()] n_default_inputs 1
[I] mod.filter-chain | graph_info()] n_default_outputs 1
[I] pw.conf | load_module()] loaded module libpipewire-module-filter-chain
[I] spa.filter-graph | setup_graph()] using 2 instances 1 1
[I] spa.null-audio-sink | impl_init()] initialized
[I] pw.node | node_update_state()] (iso_null-0) creating -> suspended
[I] pw.link | pw_context_create_link()] (oxp3_speaker_eq.out) -> (iso_null)
```

`using 2 instances 1 1` is PipeWire instantiating the single-in/single-out
graph once per channel; the last line is WirePlumber linking the chain's
playback stream to the private null sink. `pw-dump` sees exactly four nodes:

```
Dummy-Driver | None
iso_null | Audio/Sink
oxp3_speaker_eq.sink | Audio/Sink
oxp3_speaker_eq.out | Stream/Output/Audio
```

No node/device carries an `alsa` property (asserted by the harness).

## 5. Measured response vs the model

Signal: 50 Hz - 16 kHz exponential sine sweep (3.5 s, -10.5 dBFS, 1 s lead /
0.5 s tail), played twice into `iso_null`: once through `oxp3_speaker_eq.sink`
and once direct (reference). 2^19-point FFT of each recording; the ratio is
`|H(f)|` (a constant latency only adds linear phase). Per band: energy average
over the same bins for measurement and model.

```
        Hz   measured      model     diff
        50     -9.60     -9.51    -0.08
        63     -9.72     -9.71    -0.00
        79     -9.11     -9.08    -0.03
       100     -6.65     -6.57    -0.07
       126     -1.88     -1.79    -0.09
       159     -1.43     -1.50    +0.06
       200     -4.43     -4.46    +0.04
       252     -5.31     -5.31    +0.00
       317     -5.05     -5.04    -0.01
       400     -4.85     -4.85    +0.00
       504     -5.37     -5.39    +0.02
       635     -6.23     -6.25    +0.02
       800     -6.93     -6.94    +0.01
      1008     -7.39     -7.39    +0.01
      1270     -7.69     -7.70    +0.00
      1600     -7.92     -7.92    +0.00
      2016     -8.15     -8.15    +0.00
      2540     -8.47     -8.48    +0.01
      3200     -9.14     -9.16    +0.02
      4032    -10.66    -10.70    +0.04
      5080    -12.28    -12.31    +0.03
      6400    -14.59    -14.63    +0.04
      8063    -12.40    -12.29    -0.11
     10159     -7.56     -7.54    -0.02
     12800     -7.99     -8.00    +0.00
     16127     -7.88     -7.50    -0.38
bands 20   mean|err| 0.029 dB   max|err| 0.111 dB @ 8063 Hz
```

* The **0.03 dB** figure in `docs/hw/audio-harman-apo.md` is reproduced on a
  real graph: mean 0.029 dB over the pass band 100 Hz - 10 kHz, max 0.111 dB.
* The only larger deviation is the top 1/3-octave (16.1 kHz): -0.38 dB. It is
  outside the pass band and is expected - the sweep is fading out, the biquads
  are close to Nyquist, and the analytic model evaluates the low shelf at
  exactly 20 kHz. It is reported, not hidden.
* Error sources that are *absent*: no resampling (48000 Hz throughout), s16 I/O
  is linear so float vs int cancels in the ratio, and the chain is bit-exact
  biquads (no dynamics). This is why the error is at the 0.01 dB level rather
  than the "couple of dB" of an acoustic measurement.

## 6. Limiter and crossover

### 6.1 Limiter: what PipeWire can and cannot do

`module-filter-chain` has **no dynamics builtin**. The labels shipped in
`libspa-filter-graph-plugin-builtin.so` on this build are
`bq_allpass bq_bandpass bq_highpass bq_highshelf bq_lowpass bq_lowshelf
bq_notch bq_peaking bq_raw copy dcblock delay gain invert mixer` (checked with
`strings`); there is no compressor/limiter and no LADSPA/LV2 limiter is
installed. The shipped chain therefore replaces the vendor limiter with the
documented **static -8 dB headroom**, and that is what is measured - not
claimed as a limiter:

```
1khz   0 dBFS in -> RMS -10.39 dBFS, peak -7.38 dBFS (model -7.38, peak diff +0.00), clipped 0, tail -180.0 dBFS
142hz  0 dBFS in -> RMS -3.28 dBFS, peak -0.26 dBFS (model -0.26, peak diff +0.00), clipped 0, tail -180.0 dBFS
```

* A 0 dBFS tone is attenuated by the chain gain: -7.38 dB at 1 kHz and, the
  worst case, **-0.26 dB at 142 Hz** (where stage 1 peaks at +7.74 dB) - still
  below full scale, zero clipped samples. That is the whole safety argument.
* Recovery is instantaneous because there is no detector: after the tone the
  output is digital silence (`-180 dBFS`) with no pumping or release tail.
* The vendor limiter itself can only be **modelled** offline. The same 0 dBFS
  1 kHz tone through a feed-forward limiter with threshold 0.607, attack
  200 ms, release 360 ms, ratio 2:1 gives **1.90 dB attenuation** and a
  residual gain reduction of **-0.08 dB** after release - i.e. it attenuates a
  hot tone and recovers, as specified. The model lives in
  `measure-in-isolation.py:limiter_model()` and is unit-tested; it is not a
  graph element. If a real limiter is wanted on the host, use EasyEffects'
  Limiter with the vendor numbers (see `docs/hw/audio-harman-apo.md`).

### 6.2 Crossover at 10.12 kHz

The vendor preset moves a crossover/HPF corner to 10.12 kHz (params 259-264);
the shipped chain does not implement it. `test-xover.conf` builds an LR4 pair
(two cascaded 2nd-order Butterworth sections, Q = 0.7071) at 10.12 kHz from
`bq_lowpass`/`bq_highpass` and sums the branches with the builtin `mixer`, then
measures each branch and the sum in the same isolated graph:

```
        Hz     low dB    high dB     sum dB
       100     -0.00   -106.67     -0.00
      1000     -0.00    -85.58     -0.00
     10159     -6.00     -5.76     -0.00
     16000    -22.70     -0.64     -0.00
sum flatness over 100 Hz..16 kHz: max deviation +0.00 dB
checks: bands_minus6_at_corner=True, high_passes_16khz=True, high_rejects_1khz=True,
        low_passes_1khz=True, low_rejects_16khz=True, sum_flat=True
```

Both branches are -6 dB at the corner, each passes its own side and rejects the
other (>20 dB at 1 kHz / 16 kHz), and the sum is flat to 0.00 dB - the
expected allpass sum of an LR4 pair. The 10.12 kHz corner is therefore
implementable today; whether it belongs in the speaker chain is still the open
question from `docs/hw/audio-harman-apo.md` §9.

## 7. Container path (verified)

The host lacks `sox`/`numpy`; they are not needed, but the image makes the
check reproducible on a machine without PipeWire tooling at all and gives the
faster numpy FFT:

```sh
podman build -t localhost/oxp3-audio-test:f45 \
    -f container/audio-test.Containerfile container/
tools/audio/test-in-container.sh --quick      # builds on first use
tools/audio/test-in-container.sh              # full check
```

The image is Fedora 45 + `pipewire`, `pipewire-utils`, `wireplumber`,
`python3-numpy`, `bubblewrap`. It has **no /dev/snd**, so the private
WirePlumber cannot reach a real card even without bubblewrap. Verified on this host: the image
build succeeded and both the quick and the full run returned `RESULT: PASS` with
the same 0.029 dB mean error and the same hot-tone/crossover checks (container
log: `/tmp/audiotest-*/logs`, JSON in `/tmp/audiotest-container.*/result.json`).

## 8. Repo wiring

* `tools/repo-test.sh` suite **`audio-eq-isolation`** (fast tier, 300 s) runs
  `tools/audio/test-isolation.sh --quick --json <logs>/audio-eq-isolation.json`
  and maps exit 77 to SKIP. `tools/repo-test.sh --only audio-eq-isolation`
  passes on this host.
* `tests/unit/test_eq_measure.py` (23 tests) covers the maths that could be
  silently wrong: FFT vs naive DFT, Parseval, WAV round trip, sweep/hot-tone
  generation, band binning, model-vs-measured comparison (zero error and a
  known +2 dB error), `summarise()`, and the limiter model (unity below
  threshold, attenuation on a hot tone, recovery after release).

## 9. What this does not prove

* No acoustic measurement: a software loopback cannot hear the drivers. The
  on-device mic procedure in `docs/hw/audio-harman-apo.md` §6 is still the only
  way to verify the voicing in the room.
* No limiter *in the graph*, for the reason in §6.1. The vendor's dynamic
  behaviour remains a model on this stack.
* The stage-2 all-cut block and the level-dependent +18 dB block are still not
  part of the default chain; this validation does not change that.
