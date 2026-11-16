# GPU benchmark 2026-11-16T18:27:32.460008+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6225.97 | GFLOPS |
| fp32-vec4 | 4340.07 | GFLOPS |
| fp16-scalar | 6564.80 | GFLOPS |
| fp16-vec4 | 6747.08 | GFLOPS |
| fp16-matrix | 51878.89 | GFLOPS |
| fp64-scalar | 341.00 | GFLOPS |
| fp64-vec4 | 349.51 | GFLOPS |
| int32-scalar | 1772.38 | GIOPS |
| int32-vec4 | 1967.49 | GIOPS |
| int16-scalar | 2816.16 | GIOPS |
| int16-vec4 | 5345.43 | GIOPS |
| int64-scalar | 378.77 | GIOPS |
| int64-vec4 | 294.75 | GIOPS |
| int8-dotprod | 12699.61 | GIOPS |
| int8-matrix | 97130.27 | GIOPS |
| bf16-dotprod | 1357.78 | GFLOPS |
| bf16-matrix | 50227.37 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 15.86 | GBPS |
| copy-h2d | 15.76 | GBPS |
| copy-d2h | 18.34 | GBPS |
| copy-d2d | 54.99 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52669280000000.00 
- fp16 f16acc: 53042550000000.00 
- bf16: 53102490000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106989700000000.00 OPS

## OpenCL (rusticl)

- float: 5874701000000.00
- float2: 5888718000000.00
- float4: 5037545000000.00
- float8: 5413603000000.00
- float16: 5305943000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## media (FFmpeg, 1920x1200 testsrc2, 5 s)

| encoder | speed |
|---|---:|
| hevc_qsv | 5.35x (ok) |
| hevc_vaapi | Nonex (ok) |
| libx265 | 3.03x (ok) |

## GravityMark Vulkan

score: **37617.0** fps: **225.2** (scene 167.0 s)

## GravityMark Vulkan RT

score: **33302.0** fps: **199.4** (scene 167.0 s)

## VK-GL-CTS (targeted)

| suite | passed | failed | unsupported |
|---|---:|---:|---:|
| cooperative_matrix | 3682/54283 | 0 | 50601 |
| ray_query | 22581/49360 | 0 | 26779 |
| mesh_shader | 18012/28067 | 0 | 10055 |

## OpenVINO

| model-device | throughput (FPS) | median (ms) |
|---|---:|---:|
| add-NPU | 11345.7 | 0.07737000000000001 |
| add-CPU | 102623.04 | 0.0041600000000000005 |
| add-GPU | 25874.47 | 0.030989999999999997 |
| conv-NPU | 3485.13 | 0.23001 |
| conv-CPU | 4259.05 | 0.13178 |
| conv-GPU | 3334.99 | 0.13573 |

## Blender Cycles

CPU render: **00:21.14**

## Blender Cycles (oneAPI)

GPU render: **00:17.28**

## vkmark

score: **29790**

## Gaps

- **vulkan-compute / coopmat 16x8x16 f16**: unadvertised - only M=8/N=16 properties are exposed (Longshot needs M=16)
- **vulkan-compute / fp8-matrix**: zero - vkpeak reports 0 (unexposed or hardware-unsupported)
- **vulkan-compute / bf8-matrix**: zero - vkpeak reports 0 (unexposed or hardware-unsupported)
- **vulkan-compute / fp32**: unsupported - No fp32xfp32+fp32 coopmat property! Skipped
- **vulkan-compute / fp8_e4m3**: unsupported - No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **vulkan-compute / fp8_e5m2**: unsupported - No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **opencl / half**: unsupported - No half precision support
- **opencl / half2**: unsupported - No half precision support
- **opencl / half4**: unsupported - No half precision support
- **opencl / half8**: unsupported - No half precision support
- **opencl / half16**: unsupported - No half precision support

