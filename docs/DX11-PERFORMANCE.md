# DX11 is the slowest of DX11 / DX12 / Vulkan on the OXP3 — measured

Question: on this machine (Panther Lake, Arc B390, Mesa 26.3.1, DXVK/VKD3D-Proton under Proton)
DirectX 11 performs worst of the three API paths. Why, and is it fixable on our side?

Short answer so far: **D3D11 runs at ~85% of native Vulkan at every resolution** — the deficit is a
fixed per-frame CPU cost on the D3D11 path (~1.6–1.8 ms), not worse GPU work — and **no DXVK
configuration knob recovers it**. VKD3D-Proton (D3D12) loses only 3–4%, so it is specifically the
D3D11 translation that costs.

## Method

A game cannot answer this: each game has its own D3D11 and D3D12 renderers, so a difference mixes
the API path with the engine's per-API content. `tools/gpu-bench/gravitymark.py` instead runs
GravityMark's *same* scene through four paths:

| path | what it measures |
|---|---|
| `d3d11` | Windows build under Proton → DXVK over ANV |
| `d3d12` | Windows build under Proton → VKD3D-Proton over ANV |
| `vk-native` | Linux build on host Mesa → the ceiling for this GPU |
| `vk-win` | Windows build under Proton → Vulkan (not usable here: it produces no report) |

Every run is headless (`gamescope --backend headless`), so measurements never touch the visible
desktop, and all APIs go through the same compositor. Metrics come from GravityMark's own
per-frame report (`-times`): mean fps, mean frametime, p99 frametime, and the 1% low as the mean
of the slowest 1% of frames. Run-to-run spread at these settings is **< 0.3%**, so anything inside
±0.5% is noise and is not reported as a result.

```sh
tools/gpu-bench/gravitymark.py setup
tools/gpu-bench/gravitymark.py matrix --apis d3d11,d3d12,vk-native --repeats 3 \
    --width 1280 --height 720
tools/gpu-bench/dx11-ab.sh          # the DXVK/scheduling candidates
tools/gpu-bench/dx11-protons.sh     # the same D3D11 workload per installed Proton (per DXVK)
```

## Result 1 — the deficit is constant in relative terms

Headless, same scene, one benchmark pass, medians of three runs:

| path | 1280×720 | 640×360 | relative to native |
|---|---:|---:|---|
| native Vulkan | 95.47 | 106.64 | — |
| D3D12 (VKD3D-Proton) | 91.86 | 103.50 | −3.8% / −2.9% |
| **D3D11 (DXVK)** | **81.34** | **91.37** | **−14.8% / −14.3%** |

Dropping the pixel count 4× leaves the ratio at 85.2% (720p) and 85.7% (360p). A GPU-side cause
would have narrowed there; a CPU-side per-frame cost keeps its share. In absolute terms:

| resolution | D3D11 frametime | native frametime | DXVK overhead |
|---|---:|---:|---:|
| 1280×720 | 12.29 ms | 10.47 ms | **1.82 ms/frame** |
| 640×360 | 10.94 ms | 9.38 ms | **1.59 ms/frame** |

A useful side observation: *all three* paths gain only ~12% from the 4× pixel reduction, i.e. this
scene is CPU/submission-limited on every API at these settings. That is why the DXVK cost shows up
as a clean constant percentage instead of hiding behind GPU work.

## Result 2 — no DXVK knob recovers it, and pinning makes it worse

1280×720, D3D11, one run per variant, baseline 81.62 fps (0.25% noise floor; earlier three-run
baseline medians agree within 0.4%):

| variant | fps | verdict |
|---|---:|---|
| baseline | 81.62 | — |
| `INTEL_JAY=all` | 81.82 | noise (+0.25%) |
| `dxvk.enableGraphicsPipelineLibrary = False` | 81.81 | noise (+0.23%) |
| `d3d11.cachedDynamicResources = a` | 81.70 | noise (+0.10%) |
| `dxvk.enableDescriptorHeap = False` | 81.87 | noise (+0.31%) |
| `taskset 0-1` (the two 4.7 GHz cores) | 80.25 | **−1.7%, worse** |
| `taskset 0-1` on D3D12 (control) | 91.77 | −0.1%, no change |

The control matters: pinning the D3D12 path changes nothing, so the D3D11 regression is not a GPU
effect — DXVK's translation is multi-threaded, and two cores starve it. `INTEL_JAY=all` is a no-op
because Jay is already the default compiler on Xe3 in this Mesa.

DXVK's own startup dump (captured per run) confirms this is not a missing-feature fallback: it
enables `VK_EXT_graphics_pipeline_library`, `VK_EXT_descriptor_heap`,
`VK_EXT_extended_dynamic_state3`, `VK_EXT_multi_draw`, `VK_EXT_transform_feedback`,
`VK_EXT_robustness2` on Mesa 26.3.1.

## Result 3 — DXVK 3.1.x is worth ~7% and is a runtime choice, not a patch

The same D3D11 workload through every installed Proton (1280x720, headless, one run per variant;
all three 3.1.1 builds agree to within 0.4%, so this is a version effect and not a build quirk):

| Proton | DXVK | fps |
|---|---|---:|
| Proton-CachyOS Latest | 3.0.2 | 82.39 |
| Proton 11.0 | 2.7.1 | 81.09 |
| Proton Experimental | 3.1.1 | **88.23** |
| Proton Hotfix | 3.1.1-200 | **88.36** |
| GE-Proton11-12 | 3.1.1-174 | **88.03** |

DXVK 3.1.x recovers about half of the D3D11 deficit: 88.2 fps against native Vulkan's 95.5 is 92.4%,
where DXVK 3.0.2 sits at 85.2%. Nothing is patched — the runtime is simply chosen.

And this machine's default is the slow one: Steam's `CompatToolMapping` "0" entry is
`Proton-CachyOS Latest` (DXVK 3.0.2). `oxp3-fixes.sh status` now prints every installed Proton with
its DXVK version, marks which are on the fast path, and names the default — so the gap is visible
without reading this document.

```sh
./oxp3-fixes.sh status      # "Steam default compat tool: Proton-CachyOS Latest (DXVK 3.0.2)"
```

Set the default in Steam (Settings → Compatibility → Steam Play: Proton Experimental), or per title
in Properties → Compatibility, for anything that runs through D3D11.

## What is left

- **Synchronisation primitive** (`dx11-ab.sh`, `esync-only` / `server-sync`): this Proton runs
  ntsync, and the sync path is a per-frame CPU cost as well.
- If neither moves it, the conclusion is that ~15% is DXVK's inherent D3D11 overhead on this
  driver, and the deliverable is this document plus an upstream report — not a workaround that
  quietly trades away correctness.

## Traps this work paid for (now encoded in the tools)

- GravityMark's API flags are `-direct3d11` / `-direct3d12` / `-vulkan`; `-dx11` exits 1 silently
  through a dialog.
- Both builds need `bin/<exe>` with `data.zip` one level up.
- The Linux `.run` is a Makeself archive whose licence prompt loops forever on EOF.
- A killed run leaves the shared Wine prefix wedged (the game sits at 0% CPU after swapchain
  creation) — and an orphaned client keeps rendering at 100% CPU, contaminating every later
  measurement. Runs are now reaped by identity, not by process group.
- MangoHud (0.8.4) writes its log only on a clean exit, ignores `output_file`, rejects a config
  that names an unavailable metric, and writes `elapsed` in nanoseconds.
