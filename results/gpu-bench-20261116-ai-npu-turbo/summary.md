# GPU benchmark 2026-11-16T19:32:03.240633+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52570050000000.00 
- fp16 f16acc: 52978270000000.00 
- bf16: 53155460000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 107049900000000.00 OPS

## OpenCL (rusticl)

- float: 5876933000000.00
- float2: 5977745000000.00
- float4: 5071422000000.00
- float8: 5394899000000.00
- float16: 5335081000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## OpenVINO

| model-device | throughput (FPS) | median (ms) |
|---|---:|---:|
| add-NPU | 18384.83 | 0.04784 |
| add-CPU | 169512.21 | 0.0028799999999999997 |
| add-GPU | 24221.97 | 0.02871 |
| conv-NPU | 4785.84 | 0.12736 |
| conv-CPU | 6164.29 | 0.09261 |
| conv-GPU | 3544.19 | 0.14612 |
| convbig-CPU | 69.8 | 7.68 |

## vkmark

score: **29783**

## Gaps

- **vulkan-compute / coopmat 16x8x16 f16**: unadvertised - only M=8/N=16 properties are exposed (Longshot needs M=16)
- **vulkan-compute / fp32**: unsupported - No fp32xfp32+fp32 coopmat property! Skipped
- **vulkan-compute / fp8_e4m3**: unsupported - No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **vulkan-compute / fp8_e5m2**: unsupported - No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **opencl / half**: unsupported - No half precision support
- **opencl / half2**: unsupported - No half precision support
- **opencl / half4**: unsupported - No half precision support
- **opencl / half8**: unsupported - No half precision support
- **opencl / half16**: unsupported - No half precision support

