# GPU benchmark 2026-11-16T16:23:17.612718+00:00

- kernel: `7.3.4-an02.fc45.x86_64`
- scheduler: `cosmos_1.1.7_x86_64_unknown_linux_gnu`
- governor: `powersave` EPP: `balance_power`
- device: `Intel(R) Arc(tm) B390 (PTL)` driver `Mesa 27.0.0-devel`

## vkpeak

| metric | value | unit |
|---|---:|---|
| fp32-scalar | 6240.27 | GFLOPS |
| fp32-vec4 | 4344.61 | GFLOPS |
| fp16-scalar | 6581.54 | GFLOPS |
| fp16-vec4 | 6754.73 | GFLOPS |
| fp16-matrix | 52253.36 | GFLOPS |
| fp64-scalar | 342.30 | GFLOPS |
| fp64-vec4 | 350.06 | GFLOPS |
| int32-scalar | 1778.33 | GIOPS |
| int32-vec4 | 1986.41 | GIOPS |
| int16-scalar | 2815.08 | GIOPS |
| int16-vec4 | 5353.86 | GIOPS |
| int64-scalar | 378.33 | GIOPS |
| int64-vec4 | 294.66 | GIOPS |
| int8-dotprod | 12694.92 | GIOPS |
| int8-matrix | 97160.26 | GIOPS |
| bf16-dotprod | 1362.52 | GFLOPS |
| bf16-matrix | 50302.20 | GFLOPS |
| fp8-matrix | 0.00 | GFLOPS |
| bf8-matrix | 0.00 | GFLOPS |
| copy-h2h | 15.91 | GBPS |
| copy-h2d | 18.47 | GBPS |
| copy-d2h | 25.46 | GBPS |
| copy-d2d | 54.70 | GBPS |

zero metrics: fp8-matrix, bf8-matrix

## clpeak coopmat

- **unsupported** fp32: No fp32xfp32+fp32 coopmat property! Skipped
- fp16: 53085950000000.00 
- fp16 f16acc: 53253710000000.00 
- bf16: 53333410000000.00 
- **unsupported** fp8_e4m3: No fp8-E4M3 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- **unsupported** fp8_e5m2: No fp8-E5M2 coopmat support (VK_EXT_shader_float8 or property)! Skipped
- int8: 106986800000000.00 OPS

## OpenCL (rusticl)

- float: 5884107000000.00
- float2: 5946836000000.00
- float4: 5051307000000.00
- float8: 5386541000000.00
- float16: 5330427000000.00
- **unsupported** half: No half precision support
- **unsupported** half2: No half precision support
- **unsupported** half4: No half precision support
- **unsupported** half8: No half precision support
- **unsupported** half16: No half precision support

## vkmark

score: **29383**

