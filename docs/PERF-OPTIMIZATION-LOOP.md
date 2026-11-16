# OXP3 performance optimization loop

Method: every candidate is accepted only after repeated A/B runs with the
`tools/gpu-bench` suite.  Use `tools/gpu-bench/compare.py <baseline-dir>
<candidate-dir>` to compute deltas; GPU clock/util and CPU busy are recorded per
run by the harness.

## Baseline noise (quick suite, repeated)

- vkpeak/clpeak metrics: stable within ~1-2%.
- vkmark: ~1.5% run-to-run spread.
- FFmpeg QSV/VAAPI: 5-13% spread; always repeat media runs.
- Complete GPU-bound compute: 2300 MHz, 93-98% GPU util -> no CPU headroom.

## Accepted: OpenVINO CPU thread pinning

`benchmark_app` CPU with `-hint none -pin YES`:

| model | default | pinned | delta |
|---|---:|---:|---:|
| add | ~102k FPS | 159-171k FPS | +1.2x |
| conv | ~3.5k FPS | 6.1-6.7k FPS | +1.8-1.9x |
| convbig (512x512, 64 filters) | ~27.7 FPS | 65-70 FPS | +2.4-2.5x |

The larger convbigg case confirms this is not a tiny-model artifact.  Harness now
applies pinning by default; `OXP3_AI_CPU_PIN=0` disables it.
Commit `f481b3c`.  For deployment use an OpenVINO thread-affinity policy or a
cpuset; pinning to all P+E externally was less consistent than OpenVINO's pinning.

## Rejected after A/B

- **vkmark CPU affinity** `0-13`, `0-9`, `0-3`, `0-1`: all within ~1% of default
  (the scene is GPU/driver-bound; affinity adds no gain).
- **x265 affinity** to `0-9`, `0-3`, `0-1`: slower (needs all cores including
  LP-E); default scheduling is correct.
- **Platform profile performance vs balanced** (power-profiles-daemon): no
  repeatable vkmark gain (~0.1% over 8 interleaved runs); media differences were
  noise.  Restored balanced.
- **Mesa 16x8x16 coopmat property** (repo patched build vs stock): property appears
  (`MSize=16`), but vkpeak matrix throughput is unchanged (~51.5 TFLOPS); it is a
  compatibility fix for Longshot, not a synthetic-compute speedup.
- **QSV preset/async_depth/look_ahead tuning**: 5.5-6.0x, no repeatable gain over
  the harness default.

## Blocked by privileges

Changing sched_ext scheduler, EPP/governor, GPU min/max frequency, or IRQ
affinity requires root; non-interactive `sudo` is unavailable in this session.
The existing `tools/cpu-sched-test.sh` already covers the scheduler matrix when
run with sudo.  Candidates to test next with root: EEVDF vs cosmos gaming,
`scx_lavd`, EPP performance on P-cores, and a higher GPU minimum frequency.
