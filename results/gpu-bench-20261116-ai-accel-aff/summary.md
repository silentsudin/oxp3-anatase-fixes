# GPU benchmark 2026-11-16T19:37:54.845938+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 52956400000000.00 
- fp16 f16acc: 53116240000000.00 
- bf16: 53129310000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106495900000000.00 OPS

## OpenCL (rusticl)

- float: 5937165000000.00
- float2: 5934239000000.00
- float4: 5051486000000.00
- float8: 5407657000000.00
- float16: 5338049000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## OpenVINO

| model-device | throughput (FPS) | median (ms) |
|---|---:|---:|
| add-NPU | 19770.19 | 0.046439999999999995 |
| add-CPU | 166364.99 | 0.0030600000000000002 |
| add-GPU | 31443.25 | 0.01762 |
| conv-NPU | 5570.14 | 0.11856 |
| conv-CPU | 6555.78 | 0.09075 |
| conv-GPU | 4659.68 | 0.11782 |
| convbig-CPU | 65.68 | 8.83 |

## vkmark

score: **29938**

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

