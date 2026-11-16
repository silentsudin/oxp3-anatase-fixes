# Mesa/ANV cooperative-matrix investigation on Panther Lake (Xe2) — evidence

Status: **blocked upstream**. Longshot requires a 16x8x16 f16 cooperative matrix with a
32-lane subgroup and NVIDIA PTX `mma.m16n8k16` fragment order. Mesa/ANV on Xe2 cannot
deliver a correct multiply for that shape today. This file records what was proven, so the
work does not have to be repeated.

## What was built and tested

- Property patch advertising `MSize=16, NSize=8, KSize=16, f16` in
  `anv_physical_device.c` (Longshot probes for exactly this shape).
- A PTX/console load/store layout in `brw_nir_lower_cooperative_matrix.c`, gated behind
  `INTEL_COOPMAT_PTX=1`, derived from measured lane dumps. With it, Longshot's A/B/C
  layout probe **passes** — the load/store fragment order matches the console.
- An instrumented Longshot built from the installed `.longshot_exefs`, dumping per-lane
  fragment layouts and constructed matrix contents.

## Measured layouts (32-lane subgroup)

Mesa native load/store layout (probe-pass formulas):

- A `[t][k]` = `A[t/8 + 4k][2*(t%8) + {0,1}]` (lane `t`, dword `k`)
- B `[t][j]` = `B[2*(t/8) + 8j + {0,1}][t%8]`
- C `[t][j]` = `C[t/4 + 8j][2*(t%4) + {0,1}]`

Console/PTX layout:

- A `(g,i)[j]` = `A[g + 8*(j&1)][2i + 8*(j>>1) + {0,1}]`, lane = `4g+i`
- B `(g,i)[j]` = `B[2i + 8j + {0,1}][g]`
- C `(g,i)[j]` = `C[g + 8j][2i + {0,1}]`, lane = `4g+i`

Notably the native C layout **equals** the console C layout; A and B differ.

## Multiply results (every variant)

| Variant | Result |
|---|---|
| Native layout (no conversion) | 128/128 wrong |
| A-only console→native conversion | 124/128 wrong |
| B-only console→native conversion | 124/128 wrong |
| A+B console→native conversion (measured-exact) | 125/128 wrong |
| A+B conversion (earlier inferred formulas) | dispatch never completes, `VK_ERROR_DEVICE_LOST` |
| Swapped DPAS operands | 123/128 wrong |
| Swap + result transpose | 120/128 wrong |
| `repeat_count=4` | 124/128 wrong |
| Force SIMD16 for cmat shaders | pipeline creation fails (`requiredSubgroupSize=32` conflict) |

## Decisive diagnostic: Mesa's own 16x16x16 shape is broken at 32 lanes

A row-major load/muladd/store test of Mesa's **natively advertised** 16x16x16 f16 shape
(no fragment assumptions, stock shapes) on this driver:

```text
vk: 16x16 test: 126 of 256 elements differ
```

126 ≈ exactly rows 8–15. DPAS `RCOUNT` is 3 bits (max 8 rows per instruction), and the
Jay backend's handling of a 16-row matrix at SIMD32 computes only the first 8 rows; the
bottom half is garbage. Every failed fragment permutation above is explained by this:
the multiply itself only ever computes half the rows correctly.

Cross-reference: llama.cpp tunes Xe2 coopmat warptiles to subgroup sizes 8/16, and
reports wrong results/device-lost when apps assume larger warps
(https://github.com/ggml-org/llama.cpp/pull/10721).

## Why there is no commit

The blocking defect is in Mesa's Jay backend DPAS handling at SIMD32 (row split for
M=16), which is upstream compiler work, not a layout patch. Options going forward:

1. **Upstream**: report to Mesa with the 16x16 evidence above (reproducer: the
   instrumented Longshot at `/var/home/holzr/longshot-debug`, env `RX_DIAG_16X16=1`).
2. **Longshot-side**: run cooperative-matrix shaders at subgroup 16 (llama.cpp's
   approach). Requires a Longshot change, not a Mesa patch.
3. **Keep the software fallback** (current behaviour, gameplay runs ~31–33 fps).

## Artefacts

- Instrumented Longshot build: `/var/home/holzr/longshot-debug` (diagnostics active with
  `RX_DIAG_16X16=1`).
- Test-driver builds: `~/.cache/oxp3-fixes/mesa-fast/usr/local` (last build contains
  0213 + 0214-measured + 0215; none committed).
- The host system Mesa was never modified.
