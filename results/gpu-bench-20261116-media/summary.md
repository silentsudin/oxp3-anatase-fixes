# GPU benchmark 2026-11-16T16:37:21.939987+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6234.30 | GFLOPS |
| fp32-vec4 | 4348.24 | GFLOPS |
| fp16-scalar | 6565.89 | GFLOPS |
| fp16-vec4 | 6736.11 | GFLOPS |
| fp16-matrix | 52087.92 | GFLOPS |
| fp64-scalar | 341.62 | GFLOPS |
| fp64-vec4 | 349.93 | GFLOPS |
| int32-scalar | 1781.56 | GIOPS |
| int32-vec4 | 1996.38 | GIOPS |
| int16-scalar | 2816.61 | GIOPS |
| int16-vec4 | 5351.99 | GIOPS |
| int64-scalar | 378.61 | GIOPS |
| int64-vec4 | 294.78 | GIOPS |
| int8-dotprod | 12676.88 | GIOPS |
| int8-matrix | 97225.87 | GIOPS |
| bf16-dotprod | 1360.88 | GFLOPS |
| bf16-matrix | 50217.47 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 23.15 | GBPS |
| copy-h2d | 15.76 | GBPS |
| copy-d2h | 18.51 | GBPS |
| copy-d2d | 55.17 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 53167630000000.00 
- fp16 f16acc: 53005520000000.00 
- bf16: 53179960000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 107027300000000.00 OPS

## OpenCL (rusticl)

- float: 5879351000000.00
- float2: 5949703000000.00
- float4: 5029932000000.00
- float8: 5359128000000.00
- float16: 5299435000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## media (FFmpeg, 1920x1200 testsrc2, 5 s)

| encoder | speed |
|---|---:|
| hevc_qsv | 5.13x (ok) |
| hevc_vaapi | 6.82x (ok) |
| libx265 | 3.51x (ok) |

## vkmark

score: **29286**

