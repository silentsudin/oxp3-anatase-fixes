# GPU benchmark 2026-11-16T16:08:45.219247+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6220.88 | GFLOPS |
| fp32-vec4 | 4336.94 | GFLOPS |
| fp16-scalar | 6554.66 | GFLOPS |
| fp16-vec4 | 6746.30 | GFLOPS |
| fp16-matrix | 51772.32 | GFLOPS |
| fp64-scalar | 341.88 | GFLOPS |
| fp64-vec4 | 350.33 | GFLOPS |
| int32-scalar | 1778.21 | GIOPS |
| int32-vec4 | 1994.53 | GIOPS |
| int16-scalar | 2810.65 | GIOPS |
| int16-vec4 | 5351.15 | GIOPS |
| int64-scalar | 377.94 | GIOPS |
| int64-vec4 | 294.72 | GIOPS |
| int8-dotprod | 12671.48 | GIOPS |
| int8-matrix | 97242.99 | GIOPS |
| bf16-dotprod | 1360.08 | GFLOPS |
| bf16-matrix | 50211.73 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 24.51 | GBPS |
| copy-h2d | 15.58 | GBPS |
| copy-d2h | 26.90 | GBPS |
| copy-d2d | 54.65 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52348450000000.00 
- fp16 f16acc: 52758410000000.00 
- bf16: 53026170000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106937500000000.00 OPS

## vkmark

score: **29344**

