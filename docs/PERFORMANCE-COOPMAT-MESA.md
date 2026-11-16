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

## Precise root cause from the IGC DPAS specification

From Intel's IGC DPAS documentation
(https://github.com/intel/intel-graphics-compiler/blob/master/documentation/visa/instructions/DPAS.md):

```
 D = C + A x B
 D (Dst)  : MxN
 C (Src0) : MxN
 A (Src2) : MxK
 B (Src1) : KxN
 M : repeat count
 N : fixed execution size, either 8 or 16
 K : depth * OPS_PER_CHAN   (OPS_PER_CHAN = 2 for f16)
```

On Xe2 the DPAS execution size (`N`) is 16 and `repeat_count` is a small field
(`DPAS_RCOUNT`, 3 bits — at most 8). Therefore **one DPAS instruction computes at most
8 rows × 16 columns of f16 output**. A 16-row cooperative-matrix multiply cannot be one
DPAS.

Mesa's `brw_nir_lower_cooperative_matrix.c` always emits a single
`nir_dpas_intel(..., .systolic_depth = 8, .repeat_count = 8)`. Combined with the measured
`126 of 256 elements differ` on a plain 16x16x16 row-major test, this pins the defect:
Mesa implements only the M=8 shape (its advertised properties are M=8, N=exec_size,
K=16 for f16); the 16-row path computes the top eight rows and leaves the rest wrong.
That is why every fragment-layout permutation failed — the multiply itself never
computed half the matrix.

## Implementation design for 16x8x16 (not implemented)

Longshot needs `C(16x8) = A_long(16x16) * B_long(16x8)`.

### Option 1 — the DPAS swap trick (single instruction)

DPAS computes `D[m,n] = sum_k A_hw[m,k] * B_hw[k,n]` with m<8, n<16.

Set:
- `A_hw[m,k] = B_long[k,m]`  (A_hw = B_long transposed, 8x16)
- `B_hw[k,n] = A_long[n,k]`  (B_hw = A_long transposed, 16x16)

Then `D[m,n] = sum_k B_long[k,m] * A_long[n,k] = C[n,m]`, i.e. **D = C^T (8x16)**, and
C is recovered by transposing D. One DPAS covers the whole product.

The catch is operand construction: the `nir_dpas_intel` operands are slices in Mesa's
own native layout, not semantic matrices. The native layouts measured earlier are:

- A 16x16 (console-converted, from the passing probe): `A[t][k] = A[t/8 + 4k][2*(t%8)]`
- B 16x8: `B[t][j] = B[2*(t/8) + 8j][t%8]`
- C 16x8: `C[t][j] = C[t/4 + 8j][2*(t%4)]`

The hardware operands must instead be slices that `nir_dpas_intel` would produce for a
native 8x16 A and a 16x16 B; those formulas are:
`A8x16[t] = { A[t/8][2*(t%8)], A[4+t/8][2*(t%8)] }` and the corresponding 16x16 B VNNI
pairing. Constructing them from the app slices is a lane/dword gather (the same
`cmat_gather` machinery used for the PTX conversions), but with the semantic transpose
folded into the index math.

### Option 2 — two DPAS of M=8

Run DPAS with A = rows 0..7 of A_long and again with rows 8..15, with B padded from N=8
to N=16 (columns 8..15 zero). Each result gives 8 rows × 8 valid columns; concatenate
the two 8x8 halves into C. Slightly simpler indexing than option 1 (no transposes), but
needs an extra zero-padded B slice and two accumulator slices.

### Option 3

Run cooperative-matrix shaders at subgroup 16 (what llama.cpp does for Xe2). Requires an
application change, because Longshot explicitly requests `requiredSubgroupSize = 32`;
forcing SIMD16 makes pipeline creation fail (verified: VkResult -77).

## M=8 path verified correct; swap lowering is at 64/128

Two new diagnostics in the instrumented Longshot settle the question of where the
defect is:

- `RX_DIAG_M8=1` runs A(8x16) * B(16x16) + C(8x16) from plain row-major
  `OpCooperativeMatrixLoadKHR` operands and checks the result in the measured
  C 8x16 layout: **0 of 128 elements differ**.  Mesa's DPAS M=8, N=16 path is
  correct at 32 lanes.
- `RX_DIAG_IDENT=1` runs A = I(16x16), B[k][n] = 100 + 8k + n, C = 0 through the
  same cooperative-matrix types Longshot uses.  The result is exactly half
  correct: C rows 8..15 verify, rows 0..7 do not, and the raw result slice dump
  shows the DPAS destination registers do not land where the C 8x16 load layout
  would put them for that half.

The M=16, N=8 swap lowering (Ahw = B^T 8x16, Bhw = A^T 16x16, D = C^T) is
implemented and reaches 64/128 correct, up from 124-125/128 wrong.  Since the
M=8 path is proven correct, the remaining defect is confined to the operand/result
conversions of the swap lowering, not to DPAS or the layout measurements.

## RESOLVED: native matrix multiplies work on Xe2

Two patches implement the M=16, N=8, K=16 f16 cooperative matrix that Mesa never
supported:

- `patches/mesa/0213-anv-coopmat-16x8x16-f16.patch` advertises the shape.
- `patches/mesa/0214-anv-coopmat-ptx-layout.patch` lowers it.  The lowering keeps the
  PTX/console fragment order for loads/stores (so Longshot's measured layout matches)
  and implements the multiply as one DPAS M=8,N=16 with swapped, transposed operands:
  Ahw = B_long^T (8x16), Bhw = A_long^T (16x16 VNNI), D = C^T (8x16), plus the
  accumulator and result conversions back to the console 16x8 slice.

The decisive detail: the console A fragment dword index is
`j = (row >= 8 ? 1 : 0) + 2 * (col >= 8 ? 1 : 0)`.  An earlier version omitted the
row term, so rows 8..15 were never gathered and half the output duplicated — the
64/128-wrong result.

With the fix, the instrumented Longshot probe reports:

```
cooperative matrix A: the console's elements are the device's 0 1 2 3 4 5 6 7
cooperative matrix B: the console's elements are the device's 0 1 2 3
cooperative matrix C: the console's elements are the device's 0 1 2 3
native matrix multiplies on (cooperative matrices, the console's fragment layout checked)
```

Test support added to the instrumented build: `RX_DIAG_M8` (Mesa's supported M=8 path,
0/128 wrong), `RX_DIAG_IDENT` (unique-value 16x8 multiply), `RX_DIAG_LAYOUTS` (native
layout dumps).
