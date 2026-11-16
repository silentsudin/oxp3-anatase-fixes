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

## OpenCL bring-up (user-space, no root)

Fedora's `libclc-spirv` installs `libclc.spv` under the LLVM 23 names, while
rusticl looks for `spirv-mesa3d-.spv` (the well-known LLVM 23/libclc packaging
breakage).  `tools/gpu-bench/opencl-setup.sh`:

- downloads `libclc-spirv`, extracts `libclc.spv` to
  `~/.cache/oxp3-fixes/gpu-bench/share/clc/spirv{64,}-mesa3d-.spv`,
- builds `libclc-shim.so`, a small `LD_PRELOAD` path shim, and writes
  `opencl-env.sh`.

Result: `clinfo -l` now sees `Device #0: Mesa Intel(R) Arc(tm) B390 (PTL)`.

The harness auto-enables the shim when those files exist.  Note the rusticl warning
that upstream (non-Mesa-patched) libclc may have bugs.

**New gap:** rusticl does **not expose `cl_khr_fp16`** on this device:
`clinfo` reports half-precision `n/a`, and clpeak's OpenCL half tests are
"unsupported: No half precision support", even though the Vulkan path has full FP16
coopmat.  This is a rusticl/iris feature gap worth investigating (it blocks OpenCL
FP16 workloads, hashcat's FP16 paths and any FP16 OpenCL AI kernels).

## Baseline 3 (Vulkan + OpenCL, 2026-11-16 16:2x)

Artifacts: `results/gpu-bench-20261116-opencl/`.

- Vulkan coopmat unchanged: fp16 ~53 TFLOPS, bf16 ~53 TFLOPS, int8 ~107 TOPS;
  fp8/bf8 unsupported, fp32 coopmat unsupported.
- vkmark headless score ~29383.
- OpenCL rusticl: FP32 ~5.9 TFLOPS, FP32 vec2 ~5.9, vec4 ~5.0, vec8 ~5.4,
  vec16 ~5.3; **half precision unsupported**.
- GPU profiling: compute runs pin at 2300 MHz / 93-98 % util; vkmark ~80 % util.
- `GravityMark` 1.89 is now integrated (`--gravitymark`, `--gravitymark-rt`) and
  runs headless under `gamescope --backend headless`; raster 50k asteroids:
  score 38608 / 231.2 fps, RT 20k asteroids: score 33317-33383 / 199.5-199.9 fps
  (both scenes 167 s).

## Validated Mesa fix: iris FP16 for rusticl

`clinfo` on stock Mesa 26.2.6 showed **no `cl_khr_fp16`** and clpeak's OpenCL half
tests were unsupported, while Vulkan had full FP16 cooperative matrix.  Root cause:
`iris_init_shader_caps()` never set `caps->fp16`, so rusticl's
`fp16_supported()` (device.rs: `self.shader_caps().fp16`) returned false and the
extension was hidden.

Patch `patches/mesa/0215-iris-enable-fp16-for-rusticl.patch` adds:

```c
caps->fp16 = true;
```

Validated with a Mesa 27.0.0-devel build (`-Dgallium-rusticl=true`, iris/llvmpipe)
and a custom rusticl ICD:

```text
cl_khr_fp16 0x400000 (1.0.0)
half        1 / 1 (cl_khr_fp16)
```

clpeak OpenCL on the patched driver:

```text
half     : 10.78 TFLOPS
half2    :  9.94 TFLOPS
half4    : 11.25 TFLOPS
half8    : 11.23 TFLOPS
half16   :  9.24 TFLOPS
```

That is ~1.8-2x the FP32 OpenCL result (~5.9 TFLOPS) and confirms the hardware path.
Evidence: `results/gpu-bench-20261116-opencl/clpeak-fp16-patched-rusticl.json`,
`clinfo-patched-rusticl.txt`.

Note: the test used the user-space libclc shim, and rusticl warns that upstream
(non-Mesa-patched) libclc may have bugs; the capability itself is fixed by the iris
cap.  This patch should go through the normal Mesa package/overlay build for a
system-wide install.

## Media (FFmpeg): QSV gap and oneVPL runtime

`media-setup.sh` downloads the static FFmpeg build and extracts Fedora's
`intel-vpl-gpu-rt` RPM into the user cache.  With only `libvpl` installed,
`hevc_qsv` fails:

```text
Use Intel(R) oneVPL to create MFX session, the required implementation version is 1.1
Error creating a MFX session: -9.
```

After extracting `intel-vpl-gpu-rt` (libmfx-gen.so.1.2), QSV works.  The harness
prepends that path to `LD_LIBRARY_PATH` automatically when present.

Baseline 4 media (1920x1200 `testsrc2`, 60 fps, 5 s):

| encoder | speed |
|---|---:|
| hevc_qsv | 5.13x (~308 fps) |
| hevc_vaapi | 6.82x (~409 fps) |
| libx265 (CPU, ultrafast) | 3.51x (~211 fps) |

So the GPU encoders are already 1.5-2x the CPU x265 path; the actionable gap was
purely the missing oneVPL GPU runtime package, not the driver.

Artifacts: `results/gpu-bench-20261116-media/`.

## AI / NPU: device enumerates, runtime stack is the blocker

OpenVINO 2026.4.1 installed in a Python 3.12 venv; stock `Core().available_devices`
was only `['CPU']`.

User-space extraction of the missing runtime packages changed that:

```text
oneapi-level-zero (libze_loader) + intel-npu-driver (libze_intel_npu)
-> available_devices = ['CPU', 'NPU']
   NPU: Intel(R) AI Boost
```

But every `benchmark_app -d NPU` compile fails, even for a trivial single `Add`
model, with:

```text
Level0 pfnCreate2 result: ZE_RESULT_ERROR_UNSUPPORTED_FEATURE (0x78000003)
```

The NPU kernel driver is loaded (`intel_vpu`, `/dev/accel/accel0`) and firmware is
installed, so this is a **user-space NPU driver/firmware/OpenVINO version mismatch**,
not a missing device.  It needs the system `intel-npu-driver` matched to the kernel
and firmware; the extracted 1.32.0 runtime is not sufficient.

The **GPU** path through Level Zero is also blocked: the system has no
`intel-level-zero` package, and the extracted 26.35 runtime aborts inside
`zello_world`/`command_stream_receiver` on this B390.  OpenVINO therefore only
offers CPU here; the Intel GPU plugin reports "no supported devices".

Status: benchmarks and probes are ready; AI GPU/NPU measurement waits on a working
Level Zero/NPU stack.  The CPU reference (tiny models) is measurable:
`benchmark_app -d CPU` gives thousands of FPS on the toy graphs.

## Render (Blender Cycles): CPU works, oneAPI GPU blocked

`render-setup.sh` installs Blender 4.5.16 LTS.  Default cube, 4096 samples, Cycles:

```text
CPU render: 00:19.5
```

`-- --cycles-device ONEAPI` reports `Found no Cycles device of the specified type`;
with the extracted Level Zero runtime it aborts in
`intel-compute-runtime/.../command_stream_receiver.cpp` - the same failure seen with
`zello_world` and the OpenVINO GPU plugin.  So Blender GPU render is blocked by the
Level Zero GPU runtime on this B390, not by Blender or the render settings.

Harness: `--blender` (CPU render).  Artifacts:
`results/gpu-bench-20261116-render/`.

## Level Zero / NEO GPU compute runtime: aborts on this B390

Testing the extracted Intel GPU compute stack (`intel-level-zero` 26.35,
`intel-opencl` 26.35, `intel-igc` 2.41) on the Arc B390 (PCI ID **8086:b080**):

```text
zello_world              -> Abort at command_stream_receiver.cpp:1205
clinfo (NEO ICD)         -> Abort at command_stream_receiver.cpp:1205
Blender --cycles-device ONEAPI -> same abort
OpenVINO GPU plugin      -> no supported devices / abort
```

So the Vulkan/rusticl compute paths work, but the **NEO/Level Zero GPU compute
runtime is unusable on this device**, which blocks Blender oneAPI, OpenVINO GPU,
SYCL/oneAPI and any Level Zero benchmark - independent of the benchmark harness.

This matches the upstream Panther Lake Level Zero breakage reports
(intel/compute-runtime issue #918), though that report used device 8086:b0a0; the
B390 here is 8086:b080.  Possible directions: a newer NEO build/device ID support,
or running the compute workload through Vulkan/rusticl instead.

The NPU path is separate: with the extracted `intel-npu-driver`, OpenVINO enumerates
`Intel(R) AI Boost`, but compile fails with `ZE_RESULT_ERROR_UNSUPPORTED_FEATURE`
(see the AI/NPU section) - likely the same driver/firmware generation mismatch.

## Packaged Mesa validates the FP16 fix

The normal Mesa RPM pipeline (`./oxp3-fixes.sh build`) was rebuilt with
`patches/mesa/0215-iris-enable-fp16-for-rusticl.patch`.  The resulting
`mesa-libOpenCL-27.0.0-devel` package was extracted and run with a custom ICD:

```text
/usr/lib64/libRusticlOpenCL.so.1   -> rusticl 27.0.0-devel
cl_khr_fp16                        0x400000 (1.0.0)
half                               1 / 1 (cl_khr_fp16)
```

clpeak OpenCL on the packaged driver:

```text
half   : 11.25 TFLOPS   half2 : 9.46   half4 : 10.95
half8  : 10.85          half16: 8.97
```

So the one-line iris cap survives the real packaging path, not just the ad-hoc
container build.  Installing these Mesa RPMs system-wide should give the same
rusticl FP16 support; the remaining caveat is the Fedora/upstream libclc mismatch
(the user-space shim was still used for this test).

## Full suite run (2026-11-16 17:33)

```sh
tools/gpu-bench/gpu-bench.py --full --tag full-suite
```

`--full` adds GravityMark raster + RT and the Blender CPU render.  One run now
captures: Vulkan inventory, vkpeak, clpeak Vulkan, clpeak OpenCL, FFmpeg
QSV/VAAPI/x265, vkmark, GravityMark raster/RT, Blender Cycles, and a `gaps.json`
assembled from every tool's unsupported/zero/error statuses.

Results (artifacts in `results/gpu-bench-20261116-full/`):

| test | result |
|---|---|
| fp16 coopmat | ~52 TFLOPS |
| bf16 coopmat | ~50-53 TFLOPS |
| int8 coopmat | ~97-107 TOPS |
| vkmark | 28912 |
| GravityMark raster | 37509 / 224.6 fps |
| GravityMark RT | 33247 / 199.1 fps |
| Blender Cycles CPU | 19.4 s |
| FFmpeg hevc_qsv / vaapi / x265 | 5.6x / 6.3x / 3.5x |
| OpenCL FP32 | ~5.9 TFLOPS |

Detected gaps in this (stock-runtime) pass: fp8/bf8 matrix 0, fp32 coopmat
unsupported, fp8 coopmat unsupported, and OpenCL half unsupported (fixed by the
iris FP16 patch when that Mesa is installed).  The Level Zero GPU compute abort
and NPU mismatch are documented separately above.

## Cooperative-matrix property audit

`inventory` now runs `vulkaninfo --show-all` and parses every
`VkCooperativeMatrixPropertiesKHR`.  On stock Mesa the B390 advertises only
`MSize=8, NSize=16, KSize=16` properties; the gap report now emits:

```text
vulkan-compute / coopmat 16x8x16 f16: unadvertised
  only M=8/N=16 properties are exposed (Longshot needs M=16)
```

That is the exact Longshot fallback condition, now detected automatically
without launching the game.  The repo's 16x8x16 lowering patch
(`0214-anv-coopmat-ptx-layout.patch` + `0213`) adds the property; the packaged
Mesa validation above covers the rusticl side.  This check will also catch a
driver regression that removes the property.

Evidence: `results/gpu-bench-20261116-coopmat/`.

### Update: NPU driver 1.38 fixes the NPU path

The 1.32 NPU driver failure was a version mismatch.  Intel's **1.38.0** release
(`linux-npu-driver` v1.38.0, Ubuntu 24.04 deb) compiles and runs the same models:

```text
add-NPU   11805 FPS   median 0.078 ms
add-CPU  107842 FPS   median 0.004 ms
conv-NPU  3495 FPS    median 0.230 ms
conv-CPU  4429 FPS    median 0.126 ms
```

`ai-setup.sh` downloads 1.38, extracts the NPU/compiler and Level Zero loader into
the user cache, creates a Python 3.12 OpenVINO venv, and generates the two tiny IR
models via `ai-models.py`.  `gpu-bench.py --ai` runs NPU and CPU and adds the table
above.  The models are deliberately tiny (latency-dominated); the important result
is that the OpenVINO NPU compile path now works.

Artifacts: `results/gpu-bench-20261116-ai/`.

### Level Zero GPU: preemption workaround gets enumeration

The `command_stream_receiver.cpp` abort is `UNRECOVERABLE_IF(preemptionSurfaceSize == 0)`.
Setting the compute-runtime debug override avoids it:

```sh
export NEOReadDebugKeys=1
export NEO_OverrideCsrAllocationSize=4096
```

With Intel compute-runtime 26.40 + IGC 2.41:

```text
clinfo (NEO):  Intel(R) Arc(TM) B390 GPU, 96 CUs, 2300 MHz
OpenVINO:      available_devices = ['CPU', 'GPU']
Blender oneAPI: device initializes (after adding
                intel-level-zero-gpu-raytracing + libtbb)
```

That is a major improvement over "device unavailable", but kernel compilation still
fails: `clBuildProgram -6 (CL_OUT_OF_HOST_MEMORY)` for NEO OpenCL, and Blender
aborts in `built_ins.cpp` when fetching the SIP kernel.  So the preemption size was
one blocker; the built-ins/IGC compiler path is another.  This is now a focused,
reproducible next fix rather than an opaque "Level Zero doesn't work".
