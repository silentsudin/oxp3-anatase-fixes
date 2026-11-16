# Automated GPU benchmark suite (OXP3 / Intel Arc B390, Xe3)

Goal: find driver/compiler gaps where a feature silently falls back to software, in
the same way Longshot's 16x8x16 cooperative matrix did.  **No GUI automation**: every
tool below is selected because it has a CLI, runs headless, or can run under a
headless compositor, and emits machine-readable output.

## Constraints and rules

- Pin the Intel device explicitly (`vkpeak 0`, `clpeak --devices vulkan:0`); fail the
  run if the selected device is `llvmpipe`/`swiftshader`/CPU.
- Prefer JSON/text output files; one result directory per run with the command line,
  driver versions, kernel, and CPU/GPU state.
- Graphics tests that need a swapchain run under `gamescope --backend headless`
  (available on this system) or a headless Weston; compute tests run directly.
- OpenCL is currently degraded (see gaps), so Vulkan/Level Zero/SYCL paths are the
  primary coverage.

## Tool matrix

| Tool | Area | Automation | Notes |
|---|---|---|---|
| `vulkaninfo` | capability inventory | CLI, text/JSON | Extensions, features, coopmat/RT/mesh-shader properties, device selection |
| `vkpeak` | Vulkan compute/matrix | headless CLI | Scenarios incl. `fp16-matrix`, `int8-matrix`, `bf16-matrix`, `fp8-matrix`, `fp6/bf6`, `fp4`, copies. Perfect matrix-engine gap detector |
| `clpeak` | Vulkan/OpenCL/SYCL compute | headless CLI, `-o json` | Coopmat fp16/bf16/int8/fp8, numeric error, GEMM, latency; explicit `--devices` |
| `BabelStream` | memory bandwidth | headless build | OpenCL/SYCL/Vulkan-style models; bandwididth ceiling check |
| `vkmark` | Vulkan graphics | headless via Weston/gamescope | Scene-suite; needs a compositor but no user input |
| `GravityMark` | Vulkan/DX12 raster + RT | CLI, needs display | `-benchmark`/scene options; run under gamescope headless |
| `Vulkan-Samples` | feature tests/bench | CLI (`--log-fps`, sample name) | RT, mesh shaders, workgraphs, coopmat samples; good fallback probes |
| `VK-GL-CTS` (`deqp-vk`) | conformance | CLI, `.qpa` results | Targeted cases: `dEQP-VK.*cooperative_matrix*`, ray tracing, mesh shader, float8 |
| `OpenCL CTS` | OpenCL conformance | CLI | Blocked until rusticl has a device |
| `OpenVINO benchmark_app` | AI inference GPU/NPU | CLI, JSON-ish | Device/precision matrix; `-d GPU`, `-d NPU`; fallback to CPU visible |
| `llama.cpp` / `whisper.cpp` | real AI workloads | CLI | Vulkan backend exercises coopmat/XMX; compare `GGML_VK_DISABLE_COOPMAT` path |
| `clpeak` ONNX/LiteRT | AI graph runtimes | CLI | Provider/device inventory; currently no ONNX runtime installed |
| `FFmpeg` | media encode/decode | CLI | QSV/VAAPI vs software; `-hwaccels`, `av1_qsv`/`hevc_qsv` checks |
| `Blender` | Cycles render | CLI, `-j` result | oneAPI/GPU vs CPU; official benchmark bundle has `--json` |
| `hashcat -b` | OpenCL kernels | CLI | Blocked by rusticl/libclc |
| `Phoronix Test Suite` | orchestration | `batch-benchmark` | Wraps many of the above; XML results; requires PHP |
| `intel_gpu_top`, `nvtop`, `/proc/<pid>/fdinfo`, RAPL | profiling | CLI | GPU busy/freq/power, per-engine time, package power |

References: [vkpeak](https://github.com/nihui/vkpeak), [clpeak](https://github.com/krrishnarraj/clpeak), [BabelStream](https://github.com/UoB-HPC/BabelStream), [VK-GL-CTS tips](https://indico.freedesktop.org/event/10/contributions/416/attachments/252/339/cts-slides.pdf), [PTS batch mode](https://man.sourcentral.org/MGA9/1+phoronix-test-suite?pdf), [GravityMark](https://gravitymark.tellusim.com/), [Blender benchmark JSON](https://projects.blender.org/archive/blender-benchmark-bundle/blame/commit/ebdbaf313e449de5a677f242892d9b990a1d7098/benchmark/main.py), [OpenVINO benchmark_app](https://docs.openvino.ai/cn/2022.3/documentation_zh_CN/Getting_performance_numbers_zh_CN.html).

## First measurements (2026-11-16, host Mesa 27.0.0-devel)

`vkpeak 0 all` (headless, Intel Arc B390):

```text
fp16-matrix  = 50.7 TFLOPS      int8-matrix  = 96.7 TOPS
bf16-matrix  = 49.9 TFLOPS      fp8-matrix   = 0.00
bf8-matrix   = 0.00             fp6/fp4      not reported
copy-d2d     = 55.6 GB/s
```

`clpeak --vulkan --devices vulkan:0 --compute --matrix-compute --numeric-error -o`:

```text
fp16 = 51.5 TFLOPS, fp16 f16acc = 51.9, bf16 = 52.0, int8 = 105 TOPS
fp32 coopmat: unsupported ("No fp32xfp32+fp32 coopmat property")
fp8 E4M3/E5M2: unsupported ("No fp8 coopmat support / VK_EXT_shader_float8")
```

## Gaps found already

1. **FP8 cooperative matrix is not exposed.**
   - `VK_EXT_shader_float8` is absent and `VK_KHR_cooperative_matrix` advertises no
     FP8 component types; both vkpeak and clpeak report 0/unsupported.
   - Xe3 has FP8 dequantization and XMX per-clock figures list TF32/FP16/BF16/
     INT8/INT4/INT2, so this may be a hardware limit rather than a driver gap — but
     it needs confirmation against Intel's ISA docs before treating it as a bug.
2. **OpenCL is unusable: rusticl exposes 0 devices.**
   - `clinfo`: platform `rusticl`, `Number of devices 0`;
     `RUSTICL_ENABLE=iris` fails with `Libclc failed to load ... spirv-mesa3d-.spv`.
   - `libclc` is not installed. This blocks hashcat, clpeak OpenCL, BabelStream
     OpenCL and OpenCL CTS entirely; install `libclc` (or use Vulkan/SYCL).
3. **No AI/NPU stack installed**: `/dev/accel0` exists, but no Level Zero, OpenVINO,
   ONNX Runtime or LiteRT, so the NPU and XMX inference paths are unmeasured.
4. **No graphics benchmark tool installed**: `vkmark`, `GravityMark`,
   `Vulkan-Samples`, FFmpeg and Blender are all missing; cmake/meson/PHP are also
   missing, so the next step is a container-built tool cache.

## Proposed run layout

```text
tools/gpu-bench/
  inventory     vulkaninfo --summary + extension/feature diff
  compute       vkpeak all; clpeak Vulkan matrix/numeric/bandwidth -o json
  graphics      gamescope --backend headless -- vkmark / GravityMark
  conformance   deqp-vk targeted cases
  ai            OpenVINO / llama.cpp (GPU vs CPU)
  media         ffmpeg QSV/VAAPI transcode
  profile       per-run fdinfo engines, GPU freq/util, package power, thread placement
```

Each run writes `results/<timestamp>/<test>.json|txt`, a `meta.json` (kernel, Mesa,
driver, CPU governor/EPP, scheduler, lpmd state), and an `unsupported.json` assembled
from tool statuses (zero metrics, "unsupported", "fallback", "llvmpipe").

## Harness (implemented)

```sh
tools/gpu-bench/setup.sh                 # vkpeak + clpeak releases, vkmark container build
tools/gpu-bench/gpu-bench.py --tag baseline
tools/gpu-bench/gpu-bench.py --quick     # short vkpeak scenario list + 1 s vkmark scenes
```

Output: `~/.cache/oxp3-fixes/gpu-bench/results/<timestamp>-<tag>/` with
`meta.json`, `inventory.json` (full `vulkaninfo`), `vkpeak.json`,
`clpeak.json`, `vkmark.json`, per-run `profile` blocks (GPU clock/util sampled from
nvtop, CPU busy from `/proc/stat`), `summary.json`/`summary.md`.

The run aborts with exit code 2 if the selected Vulkan device is
`llvmpipe`/SwiftShader/LVP rather than the Arc B390.

## First full baseline (2026-11-16 16:08)

Artifacts committed under `results/gpu-bench-20261116/`.  Highlights:

- vkpeak: fp16-matrix **51.97 TFLOPS**, int8-matrix **97.13 TOPS**, bf16-matrix
  **50.27 TFLOPS**, fp8/bf8 matrix **0**, copy-d2d **54.65 GB/s**.
- clpeak: coopmat fp16 **52.35 TFLOPS**, bf16 **53.03 TFLOPS**, int8 **106.9 TOPS**;
  fp32 and FP8 coopmat reported unsupported (FP8 reason: no `VK_EXT_shader_float8`).
- vkmark (headless): score **29344**, GPU clock pinned at 2300 MHz.
- Profiling: vkpeak 92.8 % GPU util / 2241 MHz avg, clpeak 98.0 % / 2300 MHz,
  vkmark 79.8 % / 2298 MHz; CPU busy 83-88 %.
- OpenCL: clinfo sees the rusticl platform but **0 devices**; `libclc` is missing.

The compute tests are GPU-bound at the maximum clock, so there is no CPU scheduling
headroom in those workloads; the gaps to chase are feature exposure (FP8/coopmat
properties, OpenCL device bring-up, NPU stack) and the vkmark CPU overhead
(80 % GPU util at 83 % CPU busy is worth profiling next).
