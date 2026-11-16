# GPU benchmark 2026-11-16T19:26:58.762003+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 51728680000000.00 
- fp16 f16acc: 52215610000000.00 
- bf16: 52461000000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 105560100000000.00 OPS

## OpenCL (rusticl)

- float: 5869346000000.00
- float2: 5881696000000.00
- float4: 5003038000000.00
- float8: 5329997000000.00
- float16: 5256592000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## OpenVINO

| model-device | throughput (FPS) | median (ms) |
|---|---:|---:|
| add-NPU | 11801.78 | 0.07704000000000001 |
| add-CPU | 170867.15 | 0.00285 |
| add-GPU | 26311.41 | 0.03014 |
| conv-NPU | 3326.68 | 0.21142 |
| conv-CPU | 6164.19 | 0.09484999999999999 |
| conv-GPU | 3347.42 | 0.15358000000000002 |

## vkmark

score: **29605**

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

