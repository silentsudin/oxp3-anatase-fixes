# GPU benchmark 2026-11-16T16:50:33.968416+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6228.88 | GFLOPS |
| fp32-vec4 | 4338.63 | GFLOPS |
| fp16-scalar | 6557.93 | GFLOPS |
| fp16-vec4 | 6739.59 | GFLOPS |
| fp16-matrix | 51484.60 | GFLOPS |
| fp64-scalar | 342.54 | GFLOPS |
| fp64-vec4 | 351.04 | GFLOPS |
| int32-scalar | 1781.81 | GIOPS |
| int32-vec4 | 1994.25 | GIOPS |
| int16-scalar | 2813.04 | GIOPS |
| int16-vec4 | 5349.20 | GIOPS |
| int64-scalar | 378.28 | GIOPS |
| int64-vec4 | 294.33 | GIOPS |
| int8-dotprod | 12686.68 | GIOPS |
| int8-matrix | 97324.29 | GIOPS |
| bf16-dotprod | 1357.17 | GFLOPS |
| bf16-matrix | 50187.84 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 15.69 | GBPS |
| copy-h2d | 15.76 | GBPS |
| copy-d2h | 18.35 | GBPS |
| copy-d2d | 55.28 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52600620000000.00 
- fp16 f16acc: 52677330000000.00 
- bf16: 53059040000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106996400000000.00 OPS

## OpenCL (rusticl)

- float: 5910563000000.00
- float2: 5921947000000.00
- float4: 5011675000000.00
- float8: 5383201000000.00
- float16: 5295550000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## media (FFmpeg, 1920x1200 testsrc2, 5 s)

| encoder | speed |
|---|---:|
| hevc_qsv | 5.84x (ok) |
| hevc_vaapi | 6.37x (ok) |
| libx265 | 3.07x (ok) |

## GravityMark Vulkan RT

score: **33383.0** fps: **199.9** (scene 167.0 s)

## vkmark

score: **29469**

