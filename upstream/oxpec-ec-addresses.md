# The OXP3 EC register map: the vendor's numbers, recovered

Status: **addresses recovered and cross-checked** (2026-11-09). Nothing here was written to a device;
no host change was made. This file replaces the "the addresses are unknown" blocker recorded in
[`docs/hw/power-fan.md`](../docs/hw/power-fan.md) §5.4 and [`docs/hw/battery-tdp.md`](../docs/hw/battery-tdp.md) §2.1.

Audience: whoever extends `oxpec` (or hhd) next. The point of this file is that a maintainer can now
name every register we care about with two independent sources behind it, instead of guessing `0x??`.

## 1. The answer, first

All vendor EC addresses live in one flat space and are written as **`0x400 + <ACPI-EC offset>`**; the
low byte is the register the EC actually sees. `oxpec` already uses five of these offsets by number,
which is what makes the map trustworthy rather than plausible.

| what | vendor value | EC offset | width | encoding | confidence |
|---|---:|---:|---:|---|---|
| fan duty (`addrFanSpeed`) | `0x44B` (1099) | `0x4B` | 1 | 0…184 raw; 184 = max (see §4) | **confirmed** |
| fan auto/manual (`addrFanAutomate`) | `0x44A` (1098) | `0x4A` | 1 | `0x00` = EC auto, `0x01` = manual | **confirmed** |
| fan tach H (`fanSpeedHECAddr`) | `0x458` (1112) | `0x58` | 1 | RPM high byte; 16-bit big-endian pair | **confirmed** |
| fan tach L (`fanSpeedLECAddr`) | `0x459` (1113) | `0x59` | 1 | RPM low byte | **confirmed** |
| charge limit (`addrChargeLimit`) | `0x4A3` (1187) | `0xA3` | 1 | percent, 0…100 | **confirmed** |
| bypass power (`addrBypassPower`) | `0x4A4` (1188) | `0xA4` | 1 | `0x00` off / `0x01` inhibit-while-awake / `0x03` inhibit-always | **confirmed** |
| force-charge minimum (`addrForceChargeMin`) | `0x4A5` (1189) | `0xA5` | 1 | percent (0…100 assumed) | candidate, address high / unit medium |
| app function enable (`appFunENECAddr`) | `0x4EB` (1259) | `0xEB` | 1 | vendor takeover flag; value unknown | candidate, medium |
| handle power (`addrHandlePower`) | `0x42D` (1069) | `0x2D` | 1 | on `0x01` / off `0x00` / "restore" not configured (`-1`) | candidate, medium |
| power-supply mode (`addrPowerSupplyMode`) | `0x4E3` (1251) | `0xE3` | 1 | `1` battery, `2` Type-C ≥100 W, `4` DC-in, `8` Type-C ≤65 W (+`500`/`501` remapped to `1`) | candidate, medium (values high) |
| board sensor 1 (`addrBoardSensor1`) | `0x460` (1120) | `0x60` | 1 | vendor temperature scale, unit unknown | candidate, medium |
| board sensor 2 (`addrBoardSensor2`) | `0x461` (1121) | `0x61` | 1 | as above | candidate, medium |
| CPU temperature (`addrCpuTemp`) | `0x470` (1136) | `0x70` | 1 | °C (the vendor reads CPU temp from the EC on this model) | candidate, medium |
| battery temperature (`addrBatteryTemp`) | `0x4A0` (1184) | `0xA0` | 1 | °C, 1-byte | candidate, medium |
| battery charge current H | `0x4A1` (1185) | `0xA1` | 1 | 16-bit big-endian pair, unit unknown | candidate, medium |
| battery charge current L | `0x4A2` (1186) | `0xA2` | 1 | as above | candidate, medium |

`tools/oxp3-powerctl.py ec map` prints this table, `ec probe` reports which of the corresponding
kernel interfaces exist on the running machine (both read-only, both unprivileged), and `ec write`
is a hard refusal — see §8.

Deliberately **not** claimed:

* `EC_ADDR_STATUS_COMMAND_PORT` / `EC_ADDR_DATA_PORT` — never passed to the layer by the app (they are
  private defaults), so no number survived. The architectural ACPI EC ports are command `0x66`, data
  `0x62`; treat that as a *candidate* only. It is also probably moot on this board: the app selects the
  WMI access path here (§5).
* `EC_ADDR_THIRTY_PARTY_BATTERY`, `EC_ADDR_OXP_SET_TDP_ABLE` — same reason; no value anywhere in the
  shipped payload.
* `EC_ADDR_FAN_SPEED_H` / `EC_ADDR_FAN_SPEED_L` as *Global* fields exist but the OXP3 path uses the
  `InitBaseEc()` locals instead; the numbers are the `0x58`/`0x59` rows above.

## 2. Why the previous Ghidra run found nothing — measured, not assumed

`CompatLayerCT.exe` (from OneXConsole 0.10.4, `resources/resources/…`, 8 759 656 bytes, PE32+,
CLI IL-only, .NET Framework 4.8, MVID `d98b45f0-a3ac-4713-9564-81e4b2a1351b`) is not merely
"obfuscated": **its entire named API surface has been replaced by a single `throw` stub**, and what
the obfuscator kept under control-character names does not contain the constants either.

| measurement | value |
|---|---|
| types / fields / methods / typerefs | 866 / 4246 / 4942 / 644 |
| methods with a body | 3706 |
| methods whose body RVA is `0x42b930` | **2829** |
| methods whose body RVA is `0x2944` (trivial `.ctor` stub) | 189 |
| the remaining 688 | small `.ctor`s, compiler-generated `<>c` lambdas, and a handful of genuinely large bodies in obfuscated-named types (largest seen: token `0x0600129b`, RVA `0x44a508`, 10 233 bytes of real CIL) |
| occurrences of `0x4A3`/`0x4A4`/`0x44A`/`0x44B`/`0x458`/`0x4E3`/`0xE3`… as `ldc.i4` immediates anywhere in the IL | **0** (also 0 for the port bytes `0x62`/`0x66`) |

Every one of the 2829 bodies is the same 11 bytes:

```
ldstr "Runtime exception"; newobj <Exception>; throw
```

So the whole *named* API surface — `Global::.cctor`, `EC.ECRamReadByte`,
`WinController::batterySetChargeLimit`, `Global::InitFanEc`, … — throws. What survives is metadata
(field names, method names, **parameter names**, P/Invoke imports, 1353 constants) plus a corpus of
real bodies that the obfuscator left behind under control-character names. Those bodies are not where
the EC map is either: **no** `ldc.i4` in the entire decoded IL is `0x4A3`, `0x4A4`, `0x44A`, `0x44B`,
`0x458`, `0x4E3` or a port byte `0x62`/`0x66`, and no body loads any `EC_ADDR_*` field.

Consequence: any plan that says "read the `ldc.i4` next to `EC_ADDR_*`" is chasing something that is
not in the file. The numbers live somewhere else — in the Electron layer. `.text` also carries
~3.3 MB of zero fill and a ~450 KB high-entropy blob, but there is no native component (`ILOnly` is
set) and no decryptor body, so there is nothing to decrypt *with*.

## 3. Where the numbers actually are: the app's own bootstrap

`resources/app.asar` (61 137 651 bytes, OneXConsole 0.10.4) contains the minified main-process
bundle. The per-OEM bootstrap declares the EC numbers as **decimal** locals and passes them *into*
`CompatLayerCT.exe` over its local IPC (`\\.\pipe\CompatLayerCT`, `POST /battery/initEc` etc.). That
is why the assembly only ever needs field *names*.

Declarations, bundle module starting at asar offset `0x00f24d01`, constant block at `0x00f24f33`:

```
O=1187, M=1188, z=1189, N=11, D=1251,
w=1265, k=1142, x=1143, C=1098, T=1099, A=255,
E=1069, I=3, R=2, P=-1
```

The `ONEXPLAYER 3` / `ONEXPLAYER 3 EVA-01` branch (`0x00f2fca2` … `0x00f3072f`, 2701 bytes) overrides
the ones that differ per board:

```
0x00f2fd36  q.ecAccessType = 2                     // ECAccessType::OxpWMI
0x00f303fc  A = 184                                // FAN_MAX_SPEED_VALUE: 0…184
0x00f306bf  w = 1259, k = 1112, x = 1113            // 0x4EB, 0x458, 0x459
0x00f306e2  q.supportFunc.enableBatteryProtection = true
0x00f306fd  N = 3                                  // EC_VALUE_BYPASS_POWER_MODE2
0x00f30701  E = 1069, I = 1, R = 0                 // handle-power addr/on/off
```

(`C`, `T`, `S`, `L`, `O`, `M`, `z`, `D` keep their defaults. Many sibling OEM branches only change
`A`, `k`, `x` and `N`, e.g. `A=184,k=1112,x=1113` at `0x00f28863` — the same registers with a
different fan scale.)

Call sites, with the callee's parameter names taken from metadata (§3.1) so the argument order is not
a guess:

| asar offset | call | binds |
|---|---|---|
| `0x00f320ab` | `l.initBaseEc(w, k, x)` | `appFunENECAddr=0x4EB`, `fanSpeedHECAddr=0x458`, `fanSpeedLECAddr=0x459` |
| `0x00f321d3` | `J.initPowerSupplyModeEC(D)` | `addrPowerSupplyMode=0x4E3` |
| `0x00f3220f` | `_.init(q.fanMode, C, S, L, T, A)` | `addrFanAutomate=0x44A`, `valueOn=0`, `valueOff=1`, `addrFanSpeed=0x44B`, `valueFanSpeedValue=184` |
| `0x00f32231` | `l.initHandleEc(E, I, R, P)` | `addrHandlePower=0x42D`, `on=1`, `off=0`, `restore=-1` |
| `0x00f3224f` | `l.initOXPSensorEc(1120, 1121, 1136, 1184, 1185, 1186)` | board1 `0x460`, board2 `0x461`, cpu `0x470`, batt `0x4A0`, current H `0x4A1`, current L `0x4A2` |
| `0x00f33bed` | `J.initEc(O, M, z, 0, 1, N)` | `addrChargeLimit=0x4A3`, `addrBypassPower=0x4A4`, `addrForceChargeMin=0x4A5`, `off=0`, `mode1=1`, `mode2=3` |

### 3.1 Metadata that pins the argument order (our own words, not vendor code)

Parameter names survive the obfuscation, which is what makes the table above exact. Tokens are
build-local to MVID `d98b45f0-…`:

```
0x0600043a  Global::InitBaseEc(appFunENECAddr, fanSpeedHECAddr, fanSpeedLECAddr)
0x0600043b  Global::InitFanEc(addrFanAutomate, valueFanAutomateOn, valueFanAutomateOff,
                              addrFanSpeed, valueFanSpeedValue)
0x0600043c  Global::InitHandleEc(addrHandlePower, valueHandlePowerOn, valueHandlePowerOff,
                                 valueHandlePowerOnResotre)          // sic
0x0600043d  Global::InitOXPSensorEc(addrBoardSensor1, addrBoardSensor2, addrCpuTemp,
                                    addrBatteryTemp, addrBatteryChargeCurrentH,
                                    addrBatteryChargeCurrentL)
0x0600043e  Global::InitBatteryEc(addrChargeLimit, addrBypassPower, addrForceChargeMin,
                                  valueBypassPowerModeOff, valueBypassPowerMode1,
                                  valueBypassPowerMode2)
0x06000481  IController::fanInit(fanMode, addrFanAutomate, valueFanAutomateOn,
                                 valueFanAutomateOff, addrFanSpeed, valueFanSpeedValue)
0x060004c4  IController::batteryInitEc(String addrChargeLimit, String addrBypassPower,
                                       String addrForceChargeMin, …)
0x0400042a…0x04000448  Global::EC_ADDR_STATUS_COMMAND_PORT … EC_ADDR_THIRTY_PARTY_BATTERY
                       (all `public static ushort`/`byte`, no constant value — see §2)
```

## 4. Encodings

* **Fan duty** — `oxpec` already scales 0…255 → 0…184 for `oxp_g1_i`. The vendor's
  `valueFanSpeedValue` for this board *is* 184, so the EC takes the raw 0…184 and there is no second
  scale to discover. The value the vendor UI sends is the same 0…255 userspace number.
* **Fan mode** — `0x00` = the EC runs its own curve, `0x01` = duty register honoured
  (`PWM_MODE_AUTO`/`PWM_MODE_MANUAL` upstream). Both sources agree, so "intelligent fan curve" on this
  machine means *the EC's own closed loop*, not a table the driver writes.
* **Charge limit** — percent, 0…100, written as-is; the vendor UI range is 50…100 step 5, and upstream
  `oxpec` already reads/writes the same byte as `POWER_SUPPLY_PROP_CHARGE_CONTROL_END_THRESHOLD`.
* **Bypass power** — three values, and they are a bit mask, not an enum:
  `0x00` off, `0x01` inhibit while awake only, `0x03` inhibit always (`0x01|0x02`). Upstream's
  `OXP_CHARGE_INHIBIT_MASK_AWAKE`/`_OFF`/`_ALWAYS` are the same bits, and the vendor's per-OEM
  `MODE2` value is exactly "awake bit OR that board's always-mask" — `0x03` on the OXP3, `0x0B`
  (`0x0A|0x01`) on the default branch the X1 uses, which matches the upstream comment that the X1
  mask is `0x0A`.
  **This corrects an earlier guess in this repo**: `MODE1` is the *awake-only* variant and `MODE2` is
  *always*, i.e. `0x01`/`0x03` — the values in the previous draft were right but the labels were
  swapped.
* **Power-supply mode** — a small flag set, not a counter: `1` battery, `2` Type-C ≥100 W, `4` DC-in,
  `8` Type-C ≤65 W, with the app remapping `500`/`501` to `1` before display. Byte width.

## 5. EC access protocol on this board

From the P/Invoke table (411 `ImplMap` rows) and the `ECAccessType` enum (values are literals in
metadata, so they are exact):

```
ECAccessType::WinRing0 = 1     ECAccessType::OxpWMI = 2
```

`EC.DlPortReadPortUchar` / `EC.DlPortWritePortUchar` → `InpOutx64.dll`; `EC.UsePawnIO`,
`EC.ECRamReadByte`, `EC.ECRamDirectWrite`, `EC.CMOSReadByte/WriteByte`;
`pawnio.LpcAcpiEcModule`, `LpcIoModule`, `IntelMSRSingleModule`, `RyzenSMUSingleModule` →
`PawnIOLib.dll` (`pawnio_open`/`load`/`execute`/`close`), with the signed modules shipped in
`resources/tools/pawnio/` (`LpcACPIEC.bin`, `LpcCrOSEC.bin`, `LpcIO.bin`, `IntelMSR.bin`,
`RyzenSMU.bin`). The app starts the layer as `CompatLayerCT.exe <port> <app> [2] [pioforce]` and
prints `[PawnIO success]`/`[PawnIO failure]`, so `InpOutx64` is the fallback and PawnIO the preferred
path — but for the **OXP3 the app selects `ecAccessType = 2` = `OxpWMI`**, i.e. an OXP ACPI-WMI method
does the EC transaction; the port-I/O and PawnIO paths are for boards where that WMI method is absent.

Practical reading for Linux: the vendor's own numbers do not imply it pokes ports `0x62`/`0x66` on
this model, and the handshake (IBF/OBF, `0x80` RD_EC / `0x81` WR_EC) is the firmware's business
inside that WMI method. Which is why a Windows trace on this board may show `WMIC`/ACPI traffic rather
than port I/O — see §7.

## 6. What is *not* an EC gap: fan curves

There is no vendor EC fan-curve table for this machine, so there is nothing to expose:

* The app has no fan-curve IPC at all — only `/fan/init`, `/fan/automate/{0|1}`, `/fan/setFanSpeed/{v}`,
  `/fan/getFanSpeed`. No curve array is ever sent for the OXP3.
* `DevsCPUFanCurve`, `DevsGPUFanCurve`, `DevsMidFanCurve` belong to `CompatLayerCT.AsusACPI`, and they
  are **ASUS WMI device ids**, not EC addresses — literals in metadata: `0x110024`, `0x110025`,
  `0x110032` (siblings `DevsCPUFan` `0x110022`, `DevsGPUFan` `0x110023`). They are inert on this board.
* `cooling_runmode_intelli` ("Smart fan curve" / "intelligent fan curve") is the **Bluetooth
  water-cooling accessory** (`CoolingSystemHelper`, `cooling_runStatus_pump_abnormal`) — a separate
  gadget with its own fan, not the internal EC fan.
* The eight-point tables that do exist as static data in the assembly
  (`<PrivateImplementationDetails>` `FieldRVA`, e.g. temperatures `1E 28 32 3C 46 50 5A 5A` =
  30,40,50,60,70,80,90,90 next to speeds `80 07 …` = 1920 rpm) have **no reachable referent**: the
  methods that used them are the stubbed ones, and no IPC passes a curve. Treat as dead data.

So the honest interface for the internal fan is: auto/manual + duty + tach, which `oxpec` already
exposes as `pwm1_enable`, `pwm1`, `fan1_input`. A userspace curve (what
[`tools/oxp3-powerctl.py`](../tools/oxp3-powerctl.py) `fan curve` already does) is the only place a
curve can live — the EC's automatic mode is the vendor's curve.

## 7. What one Windows EC trace would still settle

Everything below is address-or-semantics detail, not a blocker for the confirmed rows:

1. **`0xA5` (force-charge minimum)** — unit and whether it is a floor in percent. Set charge limit to
   60 % in the vendor UI, then look at `0xA5`; a single trace settles it.
2. **`0x60`/`0x61` (board sensors) and `0x70` (CPU temp)** — the scale (`°C` vs `0.5 °C` vs `°C+offset`).
   Compare `0x70` against the vendor UI's CPU temperature at two different loads; a two-point trace
   gives the affine map.
3. **`0xA0`/`0xA1`/`0xA2`** — battery temp/charge current units, and which of H/L is which end of the
   16-bit pair (the vendor names say high-first).
4. **`0xE3` power-supply mode** — whether a Type-C ≥140 W supply yields `4`/`8`/`500`, and whether the
   app's `500`/`501 → 1` remap means the EC really emits those values.
5. **`0xEB` (app function enable)** and **`0x2D` (handle power)** — what they gate. `0x2D`'s
   `on=1/off=0` is clear; its *meaning* is not.
6. **Command/data ports** — confirm whether the OXP3 WMI path uses `0x66`/`0x62` at all, and record
   the IB protocol (whether it waits on IBF/OBF, and the read/write opcodes `0x80`/`0x81`). Only
   matters if someone wants a port-I/O fallback.
7. **Confirmation of `0x58`/`0x59` versus the running driver's `0x76`** — see
   [`docs/hw/power-fan.md`](../docs/hw/power-fan.md) §2.3: upstream master reads the tach for
   `oxp_g1_i` at `0x58` (`OXP_2_SENSOR_FAN_REG`), the vendor agrees, and an older note in this repo
   said `0x76`. A trace (or `ec_sys` on a debug kernel) says which register moves with rpm.

## 8. How this was produced (reproducible, nothing committed)

Tooling lived outside the repo and was removed afterwards; only the numbers above were kept.

* **Container**: `mcr.microsoft.com/dotnet/sdk:8.0` under rootless podman
  (`--userns=keep-id`, `:z` on the bind mount, SELinux is enforcing here), so the vendor binary was
  read from `/tmp` and never executed. `ilspycmd` refused to install into the .NET 8 SDK
  ("settings file… format version is higher than supported"), so the metadata work was done with a
  ~200-line `System.Reflection.Metadata` program instead: type/field/method/param/constant dumps,
  CIL decoding with per-instruction offsets, `FieldRVA` blobs, `ImplMap` (P/Invoke) inventory, and
  Costura resource extraction (67 `costura.*.compressed` assemblies, deflate — all third-party:
  NAudio, SharpDX, Newtonsoft, PawnIO/ViGEm wrappers; none of them vendor EC code).
* **Assembly**: `CompatLayerCT.exe` sha256 `7a88ba6d2d94e4b717b06c76dab640ba153f59bbd4335e272b4b76ddbaeb3985`;
  the same digest appears in all four extractions of the 0.10.4 payload, so there is no second build
  to compare against.
* **App**: strings and offsets read straight out of `app.asar` (the asar is a plain
  header + concatenated files container; a small Python reader is enough).
* **Cross-check**: `drivers/platform/x86/oxpec.c` from upstream master, fetched 2026-11-09 — see
  [`docs/hw/power-fan.md`](../docs/hw/power-fan.md) §2.5 for the register-by-register comparison.
* **Cleaned up**: the SDK image, the extracted `costura` assemblies and the decompiled text were
  deleted from `/tmp`; no container is left running and no image was added beyond the transient SDK.

No vendor binary, decompiled dump or copied IL appears in this repository. Everything above is either
our own prose, a derived constant with the offset that produced it, or a metadata fact (name, token,
width) restated in our own words.
