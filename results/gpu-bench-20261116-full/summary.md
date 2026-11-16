# GPU benchmark 2026-11-16T17:27:37.150262+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6207.22 | GFLOPS |
| fp32-vec4 | 4321.75 | GFLOPS |
| fp16-scalar | 6521.83 | GFLOPS |
| fp16-vec4 | 6693.75 | GFLOPS |
| fp16-matrix | 51207.82 | GFLOPS |
| fp64-scalar | 340.71 | GFLOPS |
| fp64-vec4 | 349.53 | GFLOPS |
| int32-scalar | 1770.93 | GIOPS |
| int32-vec4 | 1992.79 | GIOPS |
| int16-scalar | 2790.16 | GIOPS |
| int16-vec4 | 5326.51 | GIOPS |
| int64-scalar | 376.05 | GIOPS |
| int64-vec4 | 293.64 | GIOPS |
| int8-dotprod | 12611.13 | GIOPS |
| int8-matrix | 97128.89 | GIOPS |
| bf16-dotprod | 1352.40 | GFLOPS |
| bf16-matrix | 50242.71 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 15.92 | GBPS |
| copy-h2d | 15.89 | GBPS |
| copy-d2h | 18.30 | GBPS |
| copy-d2d | 55.46 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52532630000000.00 
- fp16 f16acc: 52835350000000.00 
- bf16: 52842480000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106758400000000.00 OPS

## OpenCL (rusticl)

- float: 5881744000000.00
- float2: 5947866000000.00
- float4: 5037695000000.00
- float8: 5365538000000.00
- float16: 5313049000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## media (FFmpeg, 1920x1200 testsrc2, 5 s)

| encoder | speed |
|---|---:|
| hevc_qsv | 5.63x (ok) |
| hevc_vaapi | 6.27x (ok) |
| libx265 | 3.45x (ok) |

## GravityMark Vulkan

score: **37509.0** fps: **224.6** (scene 167.0 s)

## GravityMark Vulkan RT

score: **33247.0** fps: **199.1** (scene 167.0 s)

## Blender Cycles

CPU render: **00:19.42**

## vkmark

score: **28912**

## Gaps

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

