# GPU benchmark 2026-11-16T19:39:12.215494+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6226.85 | GFLOPS |
| fp32-vec4 | 4338.17 | GFLOPS |
| fp16-scalar | 6546.70 | GFLOPS |
| fp16-vec4 | 6735.10 | GFLOPS |
| fp16-matrix | 52039.88 | GFLOPS |
| fp64-scalar | 341.61 | GFLOPS |
| fp64-vec4 | 350.26 | GFLOPS |
| int32-scalar | 1770.72 | GIOPS |
| int32-vec4 | 1970.43 | GIOPS |
| int16-scalar | 2808.51 | GIOPS |
| int16-vec4 | 5335.13 | GIOPS |
| int64-scalar | 377.87 | GIOPS |
| int64-vec4 | 293.87 | GIOPS |
| int8-dotprod | 12640.02 | GIOPS |
| int8-matrix | 97109.06 | GIOPS |
| bf16-dotprod | 1355.60 | GFLOPS |
| bf16-matrix | 50166.21 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 16.48 | GBPS |
| copy-h2d | 15.84 | GBPS |
| copy-d2h | 19.00 | GBPS |
| copy-d2d | 54.54 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52692120000000.00 
- fp16 f16acc: 53104700000000.00 
- bf16: 52902580000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106857700000000.00 OPS

## OpenCL (NEO)

- float: 6355748000000.00
- float2: 6354599000000.00
- float4: 6341086000000.00
- float8: 6317458000000.00
- float16: 6247048000000.00
- half: 12568360000000.00
- half2: 12533720000000.00
- half4: 12537310000000.00
- half8: 12408200000000.00
- half16: 12327840000000.00

## OpenCL (rusticl)

- float: 5978380000000.00
- float2: 5937551000000.00
- float4: 5032806000000.00
- float8: 5392021000000.00
- float16: 5315246000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## media (FFmpeg, 1920x1200 testsrc2, 5 s)

| encoder | speed |
|---|---:|
| hevc_qsv | 5.03x (ok) |
| hevc_vaapi | 6.53x (ok) |
| libx265 | 3.76x (ok) |

## GravityMark Vulkan

score: **38798.0** fps: **232.3** (scene 167.0 s)

## GravityMark Vulkan RT

score: **33391.0** fps: **199.9** (scene 167.0 s)

## VK-GL-CTS (targeted)

| suite | passed | failed | unsupported |
|---|---:|---:|---:|
| cooperative_matrix | 3682/54283 | 0 | 50601 |
| ray_query | 22581/49360 | 0 | 26779 |
| mesh_shader | 18012/28067 | 0 | 10055 |

## OpenVINO

| model-device | throughput (FPS) | median (ms) |
|---|---:|---:|
| add-NPU | 19876.98 | 0.04646 |
| add-CPU | 157969.9 | 0.00302 |
| add-GPU | 41338.54 | 0.02112 |
| conv-NPU | 5362.69 | 0.12143000000000001 |
| conv-CPU | 6562.63 | 0.09085 |
| conv-GPU | 4641.38 | 0.1259 |
| convbig-CPU | 64.61 | 9.0 |

## Blender Cycles

CPU render: **00:16.98**

## Blender Cycles (oneAPI)

GPU render: **00:15.07**

## vkmark

score: **29785**

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

