# HDR, vibrance and colour on the OneXPlayer 3 (eDP panel + Gamemode + Plasma)

Target: everything the vendor's OneXConsole does to the display — HDR on/off, "screen vibrance", enhanced
brightness/contrast, "paper eye protection" — mapped onto what Linux exposes, with our own tooling, exact steps,
validation and rollback.

Measured 2026-11-09 on kernel `7.2.7-an28.fc45.x86_64`, Plasma `6.7.5` / KWin `6.7.5`,
gamescope `3.16.23.3-1.fc45.oxp3.202610292302`, vkBasalt **not** installed (repo has `0.3.2.10-9.fc45`).
Nothing here needs `sudo` except the two shell edits explicitly marked *root*; no kernel change is needed for any
HDR or colour feature we can actually reach.

- Tools: [`tools/display/display-mode.sh`](../../tools/display/display-mode.sh) (HDR/WCG/ICC via KWin),
  [`tools/display/vibrance.sh`](../../tools/display/vibrance.sh) (shader + vkBasalt plumbing),
  [`tools/display/oxp3-edid.py`](../../tools/display/oxp3-edid.py) (panel capability decoder),
  [`tools/display/oxp3-icc.py`](../../tools/display/oxp3-icc.py) (ICC v2 profile from EDID),
  [`tools/display/oxp3-vibrance.fx`](../../tools/display/oxp3-vibrance.fx),
  [`tools/display/vkbasalt/oxp3-vibrance.conf`](../../tools/display/vkbasalt/oxp3-vibrance.conf)
- Prior evidence: [`docs/hw/gap-audit.md`](gap-audit.md) row 9 (`qs_hdr_switcher`, `screen_vibrance`, SAGV).

## TL;DR

1. **The panel is a real HDR panel and KWin already knows it.** EDID says 10 bit/colour, DisplayPort,
   1920x1200@144, gamma 2.2, D65, primaries ≈1.5× sRGB area, CTA-861 colorimetry **BT.2020 RGB** and an
   **HDR static metadata type 1 / PQ** block claiming 1107 cd/m² peak, 476 cd/m² maxFALL, 0.0007 cd/m² min.
   `kscreen-doctor -o` reports `HDR: disabled`, `Color resolution: automatic (10), range: [6; 12] bits per color`
   — i.e. HDR is a **userspace toggle**, not a kernel gap.  (The old audit line "S3, needs a driver" was a
   tooling gap: `edid-decode`/`drm_info` are not installed.)
2. **The vendor ships no colour pipeline of its own.** `HDRController.dll` is a rebuilt copy of the open-source
   *AutoActions* HDR helper (its PDB path is `…\AutoActions-main\Source\HDRController\`): it only calls
   `DisplayConfigGetDeviceInfo`/`DisplayConfigSetDeviceInfo` for "advanced colour"/HDR state.  Windows' Intel
   driver does the actual HDR.  Its "vibrance" is *AMD*-only (`atiadlxx.dll`, `AmdDynamicVibranceProxy`), and on
   this Intel SKU the UI hides it (`supportFunc.amdVibrance` is set only when the CPU model string contains
   `amd`).  Its brightness/contrast is a GDI `SetDeviceGammaRamp` LUT; "paper eye protection" is a tinted overlay
   window.  So there is nothing to port bit-for-bit: on Linux the equivalents are compositor HDR + a shader.
3. **What we reproduce:** HDR on/off in Plasma (verified live, §5.1), Gamemode HDR via two flags in the session
   launcher (root, exact steps §5.2), per-game saturation/vibrance as our own ReShade-FX shader for vkBasalt or
   gamescope (verified to compile in gamescope, §5.3), and a desktop-wide ICC profile generated from the panel's
   own EDID so KWin stops treating a 1.5×-sRGB panel as sRGB (§5.4).
4. **What stays impossible without kernel/compositor work:** writing `HDR_OUTPUT_METADATA`/`Colorspace`/`max bpc`
   behind KWin's back, a per-channel gamma/contrast ramp, a desktop-wide saturation knob, real SAGV control, and
   HDR in Gamemode without editing a root-owned launcher (§6).

## 1. Inventory: what the machine exposes today

| item | value (2026-11-09) | how |
|---|---|---|
| connector | `card1-eDP-1` connected+enabled, DP-1..4 disconnected | `/sys/class/drm/card1-eDP-1/{status,enabled}` |
| modes file | three `1920x1200` lines (144/144/60 Hz) | `/sys/class/drm/card1-eDP-1/modes` |
| modes via KWin | 1:1920x1200@144 (current), 2:144, 3:60, then scaled 1600x1200…1280x720 @60/144 | `kscreen-doctor -o` |
| panel | SDC `AMS881KB01-0`, product `0x4301`, EDID 1.4, week 40/2024, 384 B (2 ext blocks) | `tools/display/oxp3-edid.py` |
| depth / interface | 10 bit per colour, DisplayPort, gamma 2.20, D65 white (0.3135,0.3291) | same |
| gamut | R(0.6836,0.3154) G(0.2402,0.7139) B(0.1396,0.0439) → 1.50× sRGB area, +11 % vs DCI-P3 | same |
| HDR | CTA-861 colorimetry `BT2020RGB`; HDR static metadata: EOTF SDR+**PQ**, metadata type 1, 1107.1 / 475.7 / 0.00068 cd/m² | same |
| extensions | CTA-861 rev 3 (colorimetry, HDR static, video data block) + DisplayID (tag 0x70) | same |
| KWin HDR fields | `hdr: disabled`, `wcg: disabled`, `sdr-brightness: 200`, `ICC: none`, source `sRGB`, `maxBpc: 0` (auto) | `kscreen-doctor -j` |
| KWin accepts | `HDR 6–12 bit`, `Sharpness control: supported, set to 0%`, `Allow EDR: unsupported`, `Automatic brightness: unsupported` | `kscreen-doctor -o` |
| persisted config | `~/.config/kwinoutputconfig.json`: `highDynamicRange:false`, `sdrBrightness:200`, `sdrGamutWideness:0`, `wideColorGamut:false`, `hdrColorProfileSource:"EDID"`, `colorProfileSource:"sRGB"`, `maxBitsPerColor:0`, `edrPolicy:"always"`, `edidIdentifier:"SDC 17153 0 40 2024 0"` | user file |
| backlight | `intel_backlight` 6/472 raw ≈ 1 % (KWin `brightness: 0.01`) | `/sys/class/backlight` |
| gamescope | `3.16.23.3` oxp3 build; `--hdr-enabled`, `--hdr-pq-internal-enable`, `--hdr-sdr-content-nits`, `--hdr-itm-*`, `--sdr-gamut-wideness`, `--disable-color-management`, `--hdr-debug-*`, `--reshade-effect`, `--reshade-technique-idx` | `gamescope --help` |
| gamescope support | `VkLayer_FROG_gamescope_wsi` implicit layer present, `gamescopectl` present, `${XDG_CONFIG_HOME}/gamescope/{modes.cfg,edid.bin}` created by the session | `/usr/libexec/anatase-gamemode` |
| Gamemode today | gamescope started **without** `--hdr-enabled` → SDR output; session exports `STEAM_GAMESCOPE_HDR_SUPPORTED=1`, `ENABLE_GAMESCOPE_WSI=1`; `STEAM_GAMESCOPE_FORCE_HDR_DEFAULT` only for board `Galileo` | same script |
| Steam | Flatpak `org.anatase.Steam 1.0.0.87` (wrapper `/usr/bin/steam`), no per-game launch options configured yet (no `localconfig.vdf`) | `flatpak info`, `find ~/.var/app -name localconfig.vdf` |
| vkBasalt / colour | vkBasalt absent; MangoHud 0.8.4 present; `/usr/share/gamescope/reshade/Shaders` absent (user dir works); `colormgr` cannot reach a colord daemon; `dispwin`/`oyranos`/`iccdump` absent | `command -v`, runtime probe |

Two notes that matter:

* **The 1107 cd/m² figure is the panel's own claim.** KWin repeats the same numbers (`Peak brightness: 1107 nits`,
  `Max average brightness: 476 nits`, `Min brightness: 0.0007 nits`) because it reads the same CTA block — that is
  a cross-check of my decoder, **not** a photometric measurement.  A 8.8" IPS-class panel without local dimming
  cannot render 1107 cd/m² full-field; treat it as tone-mapping headroom, not perceived peak.  HDR here is
  "PQ container + wide gamut", the same class of HDR as the Steam Deck OLED/Legion Go internal panels.
* `Allow EDR: unsupported` means KWin will not do HDR "edr" compositing tricks (per-app SDR-in-HDR) on this output;
  HDR is all-or-nothing for the whole desktop.

## 2. What the vendor actually does (reverse-engineered, untrusted data)

All binaries treated as data. Decompilation: `tools/ghidra-pod.sh hdr …/HDRController.dll '^(Get|Set)…'` →
`~/.cache/ghidra-agents/hdr/out/decomp.txt`; managed side via REA on
`/tmp/oxp3-onex/app/resources/resources/CompatLayerCT.exe` (OneXConsole 0.10.4).

### 2.1 HDR: a Windows DisplayConfig toggle, nothing else

`HDRController.dll` is a 109 KB x64 PE whose `.exp`/`.lib` still carry the export names and the original build
path `C:\Users\LinkinRogAlly\Desktop\AutoActions-main\Source\HDRController\Release_x64\…` — i.e. it is the
*AutoActions* HDR helper, recompiled by the vendor. Exports: `GetHDRState`, `SetHDRState`, `GetGlobalHDRState`,
`SetGlobalHDRState`, `GetHDRSupportState`, `GetGlobalHDRSupportState`, `GetColorDepth`, `SetColorDepth`, `GetUID`.

Decompiled behaviour (all through `QueryDisplayConfig` + `DisplayConfigGet/SetDeviceInfo`):

| export | API call | semantics |
|---|---|---|
| `SetHDRState(pathId, 1/0)` | `DISPLAYCONFIG_DEVICE_INFO_SET_HDR_STATE` (type 16, size 0x18) | per-display HDR on/off |
| `GetHDRState(pathId)` | `GET_ADVANCED_COLOR_INFO_2` (type 15, size 0x24), compares the enable field to 2 | reports HDR enabled |
| `GetHDRSupportState(pathId)` | same struct, tests bit `0x10` | "display supports HDR" |
| `SetGlobalHDRState(1/0)` | same `SET_HDR_STATE` over up to 8 targets | Windows' global HDR switch |
| `GetColorDepth/SetColorDepth` | `GET_ADVANCED_COLOR_INFO` (type 9) + `SET_ADVANCED_COLOR_STATE` (type 10) | 8/10-bit desktop depth |
| `GetUID(pathId)` | `GetDisplayConfigBufferSizes`/`QueryDisplayConfig`/`malloc` | source-id → target-id mapping |

There is **no** HDR metadata authoring, no tone mapping, no LUT, no EDID handling in the vendor stack.  On Windows
the Intel driver builds the HDR infoframe; OneXConsole only flips the switch.  That is exactly the job the
compositor does on Linux.

### 2.2 Vibrance: AMD driver-side "dynamic vibrance", hidden on Intel SKUs

Managed side (`CompatLayerCT.exe`, .NET Framework 4.8):

```
CompatLayerCT.ScreenService                 (WCF REST: /screen/*)
CompatLayerCT.vibrance.common.IVibranceProxy
CompatLayerCT.vibrance.common.VibranceInfo   isInitialized, activeOutput, defaultHandle,
                                             userVibranceSettingDefault, userVibranceSettingActive,
                                             szGpuName, shouldRun, sleepInterval, displayHandles,
                                             affectPrimaryMonitorOnly, neverChangeResolution
CompatLayerCT.vibrance.AMD.AmdDynamicVibranceProxy
    SetVibranceWindowsLevel / SetVibranceIngameLevel / GetVibranceInfo / OnWinEventHook /
    IsResolutionChangeNeeded / PerformResolutionChange / HandleDvcExit / UnloadLibraryEx
    P/Invoke module: atiadlxx.dll   (AMD Display Library)
```

So the vendor has **two** vibrance levels (desktop vs in-game), an event hook that re-applies on focus/resolution
change ("DVC" = dynamic vibrance control), optionally primary-monitor-only and "never switch resolution" guards.
The real work happens inside AMD's `atiadlxx.dll`, which is not bundled (no ADL import names in the P/Invoke
table — the proxy resolves them with `LoadLibraryEx`+`GetProcAddress`).

Host `ScreenService` endpoint template names (from the managed metadata) show the whole screen API surface:
`/screen/setColorRamp/{brightnessValue}/{contrastValue}/{gammaValue}`, `/screen/setAMDVibrance/{value}`,
`/screen/setHDRState/{enabled}`, `/screen/setBrightness/{value}`, `/screen/getHDRState`,
`/screen/getHDRSupportState`, `/screen/{listResolution,setResolution,getCurrentResolution,
getCurrentSettingResolution,setDisplayRefreshRate,getDisplayRefreshRate,rotationLock,getRotationLock,
getMaxOxpEDIDIndex,existFullScreenApp}`.

Important caveat found while reading them: in the shipped 0.10.4 build the managed bodies of `ScreenService` and
`AmdDynamicVibranceProxy` are **stubs** — 36 of 66 method bodies in those types are the same 11-byte
`ldstr "Runtime exception"; newobj Exception; throw`, while the P/Invoke declarations
(`SetDeviceGammaRamp`, `GetDeviceGammaRamp`, `CreateDC/GetDC/GetDeviceCaps/ReleaseDC/DeleteDC`,
`EnumDisplaySettings`, `ChangeDisplaySettings`, `GetDevMode`, `SetWindowPos`-class window calls for the paper
overlay, `LoadLibraryEx`) are intact.  The display logic of this build therefore does not run on Windows either —
which is consistent with the classes' names being the transport of a service that is installed separately
(`resources/resources/installServiceTask.bat`).  Conclusion for us is unchanged: the *mechanisms* are as tabulated,
the *code* is not something to port.

### 2.3 Values, ranges and defaults as exposed by the UI (`app.asar`)

| control (i18n key) | English label | range / step | default | mechanism behind it |
|---|---|---|---|---|
| `screen_vibrance` | Screen Vibrance (zh: 屏幕饱和度 "screen saturation") | **−100 … +100**, step 1, marks at −100/0/100, **±50** jump buttons | 0 | AMD ADL dynamic vibrance; UI hidden unless `supportFunc.amdVibrance` |
| `screen_brightness` | Enhanced Brightness | −100 … +100, step 1 | 0 | GDI gamma-ramp LUT (`CalculateLUT` → `SetDeviceGammaRamp`) |
| `screen_contrast` | Screen Contrast | −100 … +100, step 1 | 0 | same LUT (contrast term) |
| `screen_papereye_effect` | Paper Eye Protection Mode | on/off | off | tinted overlay window (`paperWindowHwnd`) |
| `screen_papereye_effect_opacity` | Paper Effect | 0–100 % | 50 (config) / 60 (UI state) | overlay alpha |
| `qs_hdr_switcher` | HDR Switch | on/off | off | `SET_HDR_STATE` (§2.1); UI shown only when `HDRSupportState` |
| (no i18n; `/screen/setColorRamp` arg) | gamma | via service | 1.0 | LUT curve exponent |

`supportFunc.amdVibrance` is set from `os.cpus()[0].model.toLowerCase().includes("amd")` — on this Intel
Core Ultra SKU it is `false`, so **OneXConsole would not even offer a vibrance slider on this device**; the vendor
equivalent of "colour pop" on the Intel SKU is HDR + the panel's native wide gamut.

## 3. Linux capability matrix

| capability | Plasma 6.7 (KWin) | gamescope 3.16 (Gamemode) | vkBasalt | verdict |
|---|---|---|---|---|
| HDR output on this panel | **yes** – `kscreen-doctor output.eDP-1.hdr.enable`, verified §5.1 | **yes but not enabled**: needs `--hdr-enabled --hdr-pq-internal-enable` in the session launcher (root) | n/a | HDR in desktop: **available today**; in Gamemode: **one root edit away** |
| wide colour gamut (SDR) | **yes** – `wcg.enable`, plus `sdrGamutWideness` (persisted, CLI has no setter) | `--sdr-gamut-wideness 0..1` | n/a | available, but WCG *widens* SDR into the panel gamut — it is the opposite of a vibrance cut |
| HDR→SDR tone map of SDR content | yes, `sdr-brightness 100..1000` nits | `--hdr-sdr-content-nits` (default 400), `--hdr-itm-enabled` for inverse TM | n/a | available both sides |
| desktop-wide saturation / vibrance | **no knob** | n/a (compositor-level: only `--reshade-effect`) | per-game only | only ICC (§5.4) or a shader |
| per-game saturation | n/a | `--reshade-effect oxp3-vibrance.fx` (needs the same root launcher edit) | `effects = oxp3_vibrance` via `ENABLE_VKBASALT=1` | **our shader, both backends** |
| desktop-wide ICC / colour profile | **yes** – `output.<o>.iccprofile.<path>` + `colorProfileSource.ICC`, verified §5.4 | `--hdr-…nits`/gamut only, no ICC | n/a | available; this is the correct fix for oversaturation |
| per-channel gamma/contrast ramp (vendor's −100…+100) | only `brightness 0..100` (backlight) | no | shader can approximate | **not available** without owning the CRTC |
| colour temperature / night light | Plasma Night Light (time/geo based) | `GAMESCOPE_COLOR_NIGHT_MODE` (control pipe) | shader | available, different mechanism from vendor's overlay |
| clamp to 8 bit / set max bpc | `maxBpc` shown, **no CLI/UI setter** | `--generate-drm-mode`, no bpc flag | n/a | **not available** (§6) |
| panel sharpness (KWin says "supported, 0 %") | reported, no kscreen-doctor option | – | shader | **not available** |
| SAGV point / refresh policy | – | – | – | kernel `xe` work (gap-audit row 9) |

## 4. What we ship

```
tools/display/
  oxp3-edid.py            decode the panel EDID incl. CTA-861 HDR/colorimetry (-‑json, --all)
  oxp3-icc.py             generate/validate an ICC v2 matrix-TRC display profile from that EDID
  display-mode.sh         status | hdr | wcg | sdr-nits | icc | profile | reset | gamemode
  oxp3-vibrance.fx        saturation/vibrance shader (ReShade FX, self-contained)
  vkbasalt/oxp3-vibrance.conf   vkBasalt config template
  vibrance.sh             install | set | test | launch-env | status | uninstall
```

Everything is user-scope: `~/.local/share/gamescope/reshade/Shaders/`, `~/.local/share/oxp3-vkbasalt/`,
`~/.local/share/color/icc/`, `~/.config/oxp3-vkbasalt.conf`, and the state file
`$XDG_RUNTIME_DIR/oxp3-display-mode.state`.

## 5. Steps, with the verification that was actually run

### 5.1 HDR on the Plasma desktop (no root) — **verified live**

```sh
tools/display/display-mode.sh status          # panel + compositor state (this run: HDR disabled, WCG disabled)
tools/display/display-mode.sh hdr on
```

Verified immediately after (this is the check to repeat):

```
$ kscreen-doctor -o | grep -iE 'HDR|Color resolution'
	HDR: enabled
		SDR brightness: 200 nits
		SDR gamut wideness: 0%
		Peak brightness: 1107 nits
		Max average brightness: 476 nits
		Min brightness: 0.0007 nits
		HDR color profile source: EDID
		HDR ICC profile: none
	Color resolution: automatic (10), range: [6; 12] bits per color
```

Those four luminance numbers are KWin echoing the panel's EDID block — i.e. the compositor really did switch the
connector into PQ mode with our panel's metadata.  `tools/display/display-mode.sh hdr off` restored
`HDR: disabled` and left brightness at 1 % (checked).  Independent verification, if wanted:

```sh
kscreen-doctor -o                       # HDR: enabled + the four numbers above
python3 - <<'EOF'                       # and the persisted KWin state
import json; d = json.load(open('/var/home/holzr/.config/kwinoutputconfig.json'))
print([(o['connectorName'], o['highDynamicRange'], o['sdrBrightness']) for o in d[0]['data']])
EOF
```

Tuning knobs that matter on this panel: `display-mode.sh sdr-nits <100..1000>` (SDR content luminance in HDR mode;
200 matches the factory `sdrBrightness`).  That value only lands **while HDR is on** — setting it with HDR off is
silently ignored by KWin — so change it with HDR enabled, and `reset` restores the recorded value before it switches
HDR off and, for HDR-native content, In-Game HDR brightness inside the game.
Because the panel has no local dimming, HDR mostly buys the PQ transfer curve + BT.2020 gamut, not highlight
punch; expect games with dark scenes to look *better* and full-bright scenes to look *similar* to SDR.

### 5.2 HDR in Gamemode (SteamOS-style session) — **root edit, not yet applied**

The session launcher `/usr/libexec/anatase-gamemode` starts gamescope as DRM master with

```sh
gamescope_args=(
    --backend drm --generate-drm-mode fixed --xwayland-count 2 … -O '*',eDP-1
)
```

i.e. **no `--hdr-enabled`**, so Gamemode output is SDR even though the panel and gamescope both support HDR.
Exact change (the only privileged step in this document):

```sh
sudo cp -n /usr/libexec/anatase-gamemode /usr/libexec/anatase-gamemode.bak
sudo sed -i '/^ *-O .*eDP-1$/a\
    --hdr-enabled\
    --hdr-pq-internal-enable\
    --hdr-sdr-content-nits 200' /usr/libexec/anatase-gamemode
sed -n '110,120p' /usr/libexec/anatase-gamemode      # eyeball the gamescope_args array
```
Verification inside the Gamemode session:

```sh
journalctl --user -b -u anatase-gamemode | grep -iE 'hdr|pq|colorspace|hdr-enabled'
gamescopectl                       # live gamescope_control info: connector, display flags, features
```

A client must present HDR for the output to stay in PQ: with the WSI layer (`ENABLE_GAMESCOPE_WSI=1`, already
exported) gamescope exposes HDR to d3d/vkd3d swapchains; Steam's HDR toggle then becomes meaningful
(`STEAM_GAMESCOPE_HDR_SUPPORTED=1` is already set by the session).  Rollback is
`sudo cp /usr/libexec/anatase-gamemode.bak /usr/libexec/anatase-gamemode` (or re-deploy the image: `/usr/libexec`
belongs to the ostree deployment, so an update silently reverts the edit — see "open points").

### 5.3 Per-game vibrance / saturation (no root) — **shader verified, vkBasalt not installed**

```sh
tools/display/vibrance.sh install            # shader into the gamescope + vkBasalt dirs, config rendered
tools/display/vibrance.sh set 35 -10         # bake uniform defaults (there is no in-game uniform UI)
tools/display/vibrance.sh test               # compiles the shader through gamescope
tools/display/vibrance.sh launch-env         # prints the exact env / launch options
```

Verified this run (`vibrance.sh test`, which runs a nested gamescope with glxgears so a frame is actually
presented):

```
[gamescope] gamescope_reshade: Using technique: Oxp3Vibrance
[gamescope] gamescope_reshade: Compiling pass:
OK: gamescope parsed the shader, selected technique Oxp3Vibrance and compiled the pass
```

Use it in a game by one of two routes:

* **vkBasalt** (works with any Vulkan game, including inside the Gamemode session):
  ```sh
  # host Steam / Heroic / direct Proton:
  ENABLE_VKBASALT=1 \
  VK_ADD_IMPLICIT_LAYER_PATH="$HOME/.local/share/oxp3-vkbasalt/layer" \
  VKBASALT_CONFIG_FILE="$HOME/.config/oxp3-vkbasalt.conf" %command%
  ```
  vkBasalt is **not installed** on this image; three ways to get it, in order of durability:
  1. `sudo rpm-ostree install vkBasalt && sudo systemctl reboot` — package is in the Fedora 45 repo
     (`vkBasalt-0.3.2.10-9.fc45.x86_64`), layered, survives updates.  Recommended.
  2. `tools/display/vibrance.sh install --from-rpm` — `dnf5 download` + `rpm2cpio` into
     `~/.local/share/oxp3-vkbasalt/layer`, no root, no reboot; needs the env shown above.
  3. Flatpak Steam (`org.anatase.Steam`): the layer and the shader must be visible inside the sandbox —
     `flatpak override --user --filesystem=$HOME/.local/share/oxp3-vkbasalt:ro --env=ENABLE_VKBASALT=1
     --env=VK_ADD_IMPLICIT_LAYER_PATH=… --env=VKBASALT_CONFIG_FILE=… org.anatase.Steam` (printed by
     `vibrance.sh launch-env`).  Untested here: needs a real game.
* **gamescope** `--reshade-effect oxp3-vibrance.fx` — only reachable where gamescope's command line is under our
  control: a nested gamescope in Plasma (`gamescope -W 1920 -H 1200 -r 144 --reshade-effect oxp3-vibrance.fx --
  %command%`), or a second root edit adding the flag next to `--hdr-enabled` in §5.2.

Shader parameters (mirroring the vendor ranges so a value read off OneXConsole means the same thing):
`Vibrance` −100…+100 (0 = off), `Saturation` −100…+100, `VibranceProtect` 0…1 (default 0.65: 0 = plain saturation
gain, 1 = only greys are boosted), `KeepLuma` off/on.  Good starting points: +35 for washed-out sRGB games,
−25 for ports that oversaturate on this panel, −40 for night use.

### 5.4 Desktop-wide colour: stop treating the panel as sRGB (no root) — **verified live**

KWin has no saturation slider, but it *does* colourmanage per output.  Feeding it a profile that describes the
actual panel makes it convert sRGB content into the panel's real gamut instead of blasting raw primaries — which is
the root cause of the "oversaturated desktop" that the vendor's slider papers over.

```sh
tools/display/display-mode.sh icc panel      # generate ~/.local/share/color/icc/oxp3-panel.icc + apply
tools/display/display-mode.sh icc none       # clear it again
```

Verified: `kscreen-doctor -o` then reports `ICC profile: /var/home/holzr/.local/share/color/icc/oxp3-panel.icc`
and `Color profile source: ICC`, with no complaint from KWin (it renders, so lcms2 accepted the profile).
`oxp3-icc.py --check` independently re-parses the file: class `mntr`, space `RGB `, PCS `XYZ `, v2.1, nine tags,
`wtpt(D50)=(0.9642,1.0,0.8249)`, `rXYZ=(0.5724,0.2632,−0.0007)` — the matrix is Bradford-adapted from the EDID
primaries and white-normalised to D50 (an internal assertion in the generator fails if the primaries matrix is not
white-normalised, which is how I caught a broken adaptation matrix during development).

Caveats: the profile is **EDID-derived, not measured** — no colorimeter was used, so absolute accuracy is the
panel's factory claim, not a calibration.  `colord`/`colormgr` is not usable on this box (the daemon does not
answer), so nothing is installed into a system colour store; KWin is pointed straight at the file.

## 6. What is *not* possible without kernel / colour-pipeline work

1. **HDR without a compositor.** `HDR_OUTPUT_METADATA`, `Colorspace` and `max bpc` are DRM connector properties
   owned by whoever holds DRM master.  Under Plasma that is KWin, under Gamemode it is gamescope; a userspace tool
   cannot write them behind the compositor's back (and should not — the two would fight).  `kscreen-doctor` and
   gamescope flags are the only supported doors.  `/sys/kernel/debug/dri` is root-only here anyway.
2. **A per-channel gamma/contrast ramp** (the vendor's −100…+100 brightness/contrast, i.e. `SetDeviceGammaRamp`):
   KWin owns the CRTC and exposes only backlight `brightness 0..100`; there is no Wayland/kscreen API for a gamma
   LUT, and `dispwin`-style tools cannot reach the connector.  Only a shader (per game) or a KWin patch gives this.
3. **A desktop-wide saturation/vibrance knob.** KWin 6.7 has ICC, colour-profile source, `sdrGamutWideness`
   (config-only, and it *widens*), and HDR — no saturation slider.  Desktop-wide "vibrance" needs either an ICC
   profile whose TRC/matrix encodes the look (possible but not a knob), or a compositor extension.
4. **Setting the colour depth / max bpc.** KWin acknowledges `6..12 bit` and currently runs `automatic (10)`, but
   `kscreen-doctor` has no `max-bpc` option and the UI does not either — it is a KWin-side missing control, not a
   kernel gap (the `xe` driver takes it).
5. **Panel sharpness**, although `kscreen-doctor -o` prints "Sharpness control: supported, set to 0%": no setter
   exists in KWin 6.7's CLI/UI.
6. **Real HDR headroom.** No local dimming: the 1107/476 cd/m² numbers are container metadata.  Anything that
   needs sustained peak brightness or per-zone control is a panel limitation, not software.
7. **SAGV / refresh policy** behind the vendor presets: analysed separately in [sagv.md](sagv.md) — the SAGV point
   has no Linux interface at all on `xe` (only the boot-time `xe.enable_sagv=0` switch), so it needs a small kernel
   patch.  Unrelated to HDR/colour; nothing here depends on it.

## 7. Rollback (never leave the panel in a strange colour state)

```sh
tools/display/display-mode.sh reset          # HDR off, WCG off, ICC path cleared, source sRGB, brightness restored
tools/display/vibrance.sh uninstall          # removes shader, vkBasalt config, unpacked vkBasalt
sudo cp /usr/libexec/anatase-gamemode.bak /usr/libexec/anatase-gamemode   # only if §5.2 was applied
```

`display-mode.sh` saves the pre-change state to `$XDG_RUNTIME_DIR/oxp3-display-mode.state` before it touches
anything, and `reset` restores the recorded SDR-content luminance (while HDR is still on) and backlight level, then
switches HDR/WCG off and clears the ICC path.  The safe state is HDR off, WCG off,
`sRGB` profile source, no ICC path — which is exactly the factory state this document found the panel in, and what
this run left it in (verified with `kscreen-doctor -o` after every experiment).  If a session ever ends up bright,
tinted or clipped: `display-mode.sh reset`, and if KWin itself is unhappy, log out and back in — the toggles live in
`~/.config/kwinoutputconfig.json`, so removing that file's `highDynamicRange`/`iccProfilePath` keys by hand is the
last resort (KWin rewrites the file on the next change).

## 8. Open points

* §5.2 was **not applied** (root + session restart not allowed here).  The flags, their meaning and the rollback are
  written out; someone with root still has to run it once and confirm `gamescopectl` reports HDR features.
* `vkBasalt` was not exercised against a real game (not installed).  The shader itself is verified to parse and
  compile in gamescope; the vkBasalt config syntax follows 0.3.2.x (`effects`/`<name> = <path>`), and the shader
  uses the same ReShade-FX dialect gamescope accepted, which is the same lineage vkBasalt uses.
* Gamescope's ReShade parser rejects `register(t0)`-style annotations and requires a `{ Texture = …; }` state block
  plus an explicit `VertexShader` per pass; that is why our `.fx` looks slightly unusual.  It is recorded in the
  file header so the next person does not "fix" it back.
* The ICC profile is EDID-derived.  A colorimeter pass (or a DisplayCAL-style workflow on another machine) would
  replace it; `display-mode.sh icc panel` only cares that the file is a valid ICC v2 display profile.
* The display tooling is deliberately **not** part of `tools/oxp3-hw-install.sh` (that orchestrator is the root
  component set; everything in `tools/display/` is user-scope).  If it is ever wired in, it belongs in the
  "user scope last" group next to `rgb`/`audio`.
* Gamemode's launcher being root-owned on an immutable image is the real usability gap: a proper fix is an upstream
  option (an `anatase-gamemode` env knob such as `OXP3_GAMESCOPE_HDR=1`) so users do not have to edit `/usr/libexec`.
* `sdrGamutWideness` has no `kscreen-doctor` setter; it is only writable in `kwinoutputconfig.json` (KWin rewrites
  that file on config changes, so it must be set through the Plasma display settings UI or between sessions).
* Colours on the panel were never measured with a meter, and the vendor's own numbers cannot be compared: the
  Windows slider is AMD-only and this is an Intel SKU, so there is no vendor reference to reproduce.
