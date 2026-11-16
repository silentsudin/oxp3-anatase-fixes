# GPU benchmark 2026-11-16T17:00:25.633731+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6245.81 | GFLOPS |
| fp32-vec4 | 4353.43 | GFLOPS |
| fp16-scalar | 6563.27 | GFLOPS |
| fp16-vec4 | 6753.56 | GFLOPS |
| fp16-matrix | 51987.13 | GFLOPS |
| fp64-scalar | 342.64 | GFLOPS |
| fp64-vec4 | 351.55 | GFLOPS |
| int32-scalar | 1781.17 | GIOPS |
| int32-vec4 | 1990.04 | GIOPS |
| int16-scalar | 2820.05 | GIOPS |
| int16-vec4 | 5363.50 | GIOPS |
| int64-scalar | 379.33 | GIOPS |
| int64-vec4 | 295.25 | GIOPS |
| int8-dotprod | 12634.82 | GIOPS |
| int8-matrix | 97127.06 | GIOPS |
| bf16-dotprod | 1361.14 | GFLOPS |
| bf16-matrix | 50240.99 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 15.88 | GBPS |
| copy-h2d | 15.93 | GBPS |
| copy-d2h | 26.63 | GBPS |
| copy-d2d | 54.65 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 53034640000000.00 
- fp16 f16acc: 53137420000000.00 
- bf16: 53275190000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106911100000000.00 OPS

## OpenCL (rusticl)

- float: 5947379000000.00
- float2: 5907886000000.00
- float4: 5060334000000.00
- float8: 5371399000000.00
- float16: 5333109000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## media (FFmpeg, 1920x1200 testsrc2, 5 s)

| encoder | speed |
|---|---:|
| hevc_qsv | 5.27x (ok) |
| hevc_vaapi | 6.33x (ok) |
| libx265 | 3.03x (ok) |

## Blender Cycles

CPU render: **00:19.53**

## vkmark

score: **29696**

