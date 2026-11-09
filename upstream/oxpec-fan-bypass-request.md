# RFC draft — `oxpec`: expose the EC fan curves and the real charging-bypass byte on OneXPlayer 3

Status: **draft, not submitted**. No account, no GitLab/GitHub permissions and no vendor support
are available here, and nothing below has been applied to a kernel tree. The code in §6 is our own
**sketch**, written for review, not compiled against the real `oxpec` driver; §7 lists what a
maintainer (or we, with hardware access) must verify before it could be a patch.

Prepared: 2026-11-09, from the evidence in this repository. Target tree for the API references:
`drivers/platform/x86/oxpec.c` at 7.2.7 (`7.2.7-an19-8` local source tree; the unit runs
`7.2.7-an28.fc45`). Device: ONEXPLAYER 3, DMI `ONE-NETBOOK` / `ONEXPLAYER 3`, BIOS 5.09, board
`oxp_g1_i` in the current driver.

Related local evidence (all our own prose, no vendor code):

* [`docs/hw/power-fan.md`](../docs/hw/power-fan.md) — fan/EC inventory, the three curve tables, the
  PWM scaling, the existing kernel gap.
* [`docs/hw/battery-tdp.md`](../docs/hw/battery-tdp.md) — battery/charge inventory, vendor bypass
  semantics (mode 1 “always”, mode 2 “screen-on only”, off), the missing EC addresses.
* [`docs/hw/gap-audit.md`](../docs/hw/gap-audit.md) — rows 2 and 5, and the §5 note about IL
  immediates.

---

## 1. The ask, in one paragraph

The OXP3 EC owns an intelligent fan controller with three temperature→duty curve tables, an
auto/manual switch and a fan-preset/lock policy, plus a charging-bypass byte with three vendor
states (off / always / only-while-screen-on). Linux currently reaches only a single PWM duty, the
auto/manual bit and a tachometer, and the charge limit and charge-inhibit path is either the ACPI
`_BTP`/`_BCM` method or the existing `oxpec` `power_supply_ext` (see §3.2 — this needs to be settled
first). We ask for: (a) a **hwmon ABI** for the EC curve tables and their auto/manual selection,
using the existing `pwmN_auto_pointM_temp` / `_pwm` shape; (b) the **existing
`power_supply`/`charge_behaviour` ABI** to be backed by the EC bypass byte on this board, with the
vendor’s three states mapped onto `auto`, `inhibit-charge`, `inhibit-charge-awake`; (c) no new
`debugfs` user-facing knobs. The blocker is not the design, it is the **EC register map**: the
vendor field *names* are recovered, the *addresses* are not (§5).

## 2. What the hardware can do (vendor evidence, names and semantics only)

From the managed `CompatLayerCT.exe` (PE32+ Mono/.NET, 8,759,656 bytes) that ships inside
OneXConsole 0.10.4, extracted read-only to a scratch cache outside this repository:

```
addrFanSpeed, addrFanAutomate, DevsCPUFan, DevsCPUFanCurve,
DevsGPUFan, DevsGPUFanCurve, DevsMidFanCurve, fanSetFanSpeed, fanGetFanSpeed,
FAN_MAX_SPEED_VALUE, fanSpeedHECAddr/fanSpeedLECAddr, addrHandlePower,
addrCpuTemp, addrBoardSensor1/2, addrChargeLimit, addrForceChargeMin,
addrBypassPower, addrPowerSupplyMode, batterySetChargeLimit,
batterySetByPassPowerMode, batteryGetPowerSupplyMode, batteryInitEc,
EC_ADDR_BYPASS_POWER, EC_VALUE_BYPASS_POWER_MODE1,
EC_VALUE_BYPASS_POWER_MODE2, EC_VALUE_BYPASS_POWER_MODE_OFF,
EC_ADDR_CHARGE_LIMIT, EC_ADDR_FORCE_CHARGE_MIN, EC_ADDR_POWER_SUPPLY_MODE,
EC_ADDR_OXP_BATTERY_TEMP, EC_ADDR_OXP_BATTERY_CHARGE_CURRENT_H/_L,
EC_ADDR_OXP_SET_TDP_ABLE, EC_ADDR_STATUS_COMMAND_PORT, EC_ADDR_DATA_PORT
```

and the UI/i18n semantics (zh-CN bundle):

| feature | vendor strings | meaning |
|---|---|---|
| fan mode | `cooling_runmode_intelli`, `cooling_runmode_fixspeed`, `qs_fan_lock` | intelligent curve vs fixed speed, plus a lock |
| fan presets | `qs_fan_presetting1`, `qs_fan_presetting2` | two quick presets |
| per-profile fan | `fanSpeed` in `perf_conf.ini` (0/0/200 for Quiet/Balanced/Performance) | fixed duty, not the curve |
| curves | `DevsCPUFanCurve`, `DevsGPUFanCurve`, `DevsMidFanCurve` | **three** tables, CPU/GPU/mid |
| bypass | `battery_protection_bypasspowermode`, `…_effect_always`, `…_effect_when_screen_on`, `…_hint` | mode 1 applies always; mode 2 is ineffective while asleep/off; “plugged in → charging stops, device is powered, less heat” |
| charge cap | `battery_protection_chargelimit`, `…_hint` | stored in the EC, effective in S5 too |

That is capability evidence, not a register map. We know the EC can do it; we do not know at which
offsets, in which encoding, nor how many points each table has.

## 3. What Linux exposes today

### 3.1 Fan (`oxp_ec`, board `oxp_g1_i`)

Measured on the unit (`docs/hw/power-fan.md` §1, §5), and matching the driver’s board table:

| attribute | value/behaviour | source |
|---|---|---|
| `fan1_input` | 1388–2414 rpm, 2 EC registers at `0x76` | hwmon |
| `pwm1` | 0–255, driver-scaled to EC 0–184 via `(val * 184) / 255` | hwmon |
| `pwm1_enable` | 0 full / 1 manual / 2 EC auto | hwmon |
| `tt_toggle` | prerequisite for manual PWM; HHD sets it to 1 | platform sysfs |
| curves / presets / lock / per-partition | **nothing** | — |

### 3.2 Charge limit and bypass — an important correction to our own notes

`docs/hw/battery-tdp.md` §1/§3.2 attributes `charge_control_end_threshold` and `charge_behaviour`
to ACPI `_BTP`/`_BCM`. In the 7.2.7 source tree we have, `oxp_g1_i` (the OXP3 board) is in
`oxp_psy_ext_supported()` and the `power_supply_ext` uses the X1 register pair
`0xA3` (limit) / `0xA4` (inhibit) with masks `0x01` (awake) / `0x02` (off) / `0x03` (always).
That would mean the OXP3 already writes an EC bypass register through `oxpec`, and that the observed
attributes might come from the driver rather than from ACPI.

This must be settled before the RFC claims “no interface”: on the unit, check whether `oxpec` is
bound during the measurement (`dmesg | grep -i oxpec`), whether `BAT0/uevent` shows the extension,
and whether a write through `charge_behaviour` changes EC `0xA4`. **If `0xA4` is confirmed, two of
the addresses in §5 are already known** and the remaining work is the three value encodings, the
curve tables and the `addrForceChargeMin`/temperature fields. The draft keeps both possibilities
open rather than asserting one.

What the current ABI does not express, either way:

* the vendor’s **mode 2 is “screen on”**, while `inhibit-charge-awake` is defined as “while the
  system is awake” (not suspended). These are close but not identical. A driver that maps one to the
  other must say so in the ABI text, and userspace (upower, our `oxp3-powerctl`) must not be misled.
* the charger tier (`addrPowerSupplyMode`; barrel / USB-C ≥100 W / DC-in / USB-C ≤65 W / ≥140 W) has
  no ABI. `power_supply` has `POWER_SUPPLY_PROP_USB_TYPE` for USB-PD, but the barrel/DC-in
  distinction has no standard property. **Do not invent one in this series**; it is a separate
  discussion and needs the EC encoding first.
* the charge *start* threshold (`addrForceChargeMin`) has no attribute. Only add
  `charge_control_start_threshold` if the EC really stores a lower bound; the value we see today is
  a single ceiling.
* battery temperature (`EC_ADDR_OXP_BATTERY_TEMP`) is not exposed; it is a natural `tempN_input`
  later, but it needs the address and scale.

## 4. Proposed ABI

### 4.1 Fan curves — hwmon auto-point attributes

The standard hwmon shape for “temperature → duty” curves already exists:
`pwmN_auto_pointM_temp` (millidegrees Celsius) and `pwmN_auto_pointM_pwm` (0–255), plus optional
`_temp_hyst`. Use it; do not add a private binary blob and do not use `debugfs` (§4.3). We also
deliberately do **not** propose `fan1_curve_*`: that would be a second, private ABI next to the
existing auto-point convention.

Because the EC has **three tables that drive one physical fan**, the draft proposes:

* `fan1_input` stays the single tachometer.
* `pwm1`, `pwm2`, `pwm3` are **curve domains**, labelled in the documentation as
  `cpu`, `gpu`, `mid`; each gets `pwmN_auto_pointM_{temp,pwm}`. They are not three fans and must not
  be documented as such.
* `pwm1_enable` keeps its current global meaning (0 full / 1 manual / 2 EC auto). If the EC really
  has a per-table auto/manual bit, a `pwmN_enable` per channel can be added once the map proves it;
  the sketch does not assume it.
* Presets (`qs_fan_presetting*`) and the lock (`qs_fan_lock`) are **out of scope**: they are opaque
  policy IDs, likely a vendor command byte, and do not belong in hwmon until decoded. A reviewer
  should not have to bless guessed semantics.

**This shape is contingent on the table layout.** If the decoding shows the table is not a list of
(temp, duty) pairs (for example an opaque 3×N vendor structure, or a per-partition fan controller
rather than a whole-fan curve), the right answer is to expose only `pwmN_enable` for auto/manual and
**keep the tables out of the kernel**. The RFC explicitly offers that fallback; a wrong abstraction
is worse than a missing one.

Required evidence before the ABI is frozen: point count, temperature unit/scale, duty scale (the
driver already scales PWM to EC 0–184, so the curve bytes probably use the same 0–184 range, not
0–255), ordering, whether the table is indexed by duty or by temperature, and whether writes are
plain byte writes or need a commit/command byte.

### 4.2 Charging bypass — `power_supply` charge behaviour

Keep the established class ABI; add no new attribute name for the vendor byte:

| vendor state | `charge_behaviour` value | note |
|---|---|---|
| off | `auto` | normal charging |
| mode 1 (always) | `inhibit-charge` | charging stops while on the adapter |
| mode 2 (screen-on only) | `inhibit-charge-awake` | approximation: kernel “awake” ≠ vendor “screen on”; document it |

`charge_control_end_threshold` continues to be the percentage cap (already present in the 7.2.7
tree’s extension for this board). Values are clamped 0–100 in the driver and read back; policy
(which percentage, when) stays in userspace.

Implementation notes for review:

* extend the existing `power_supply_ext` rather than registering a second battery extension;
* `charge_behaviours` must advertise exactly the states the EC can represent on this board;
* on read, map back from the raw byte and return `-EINVAL` for an unknown value instead of guessing
  `auto`;
* on write, read-modify-write if the byte has reserved bits (vendor values are given as complete
  constants, so this is probably a whole-byte write, but verify), then read back and fail loudly on
  mismatch.

### 4.3 Why hwmon / power_supply and not debugfs

1. **These are user-facing controls**, not debugging aids: `power-profiles-daemon`, upower, KDE’s
   battery applet, lm-sensors/fancontrol and HHD all consume the class ABIs. debugfs is root-only,
   not a stable ABI and explicitly not for this.
2. **Permissions and lifetime**: hwmon/power_supply devices get proper `0644` attributes, udev
   tagging and device-model lifetime. A debugfs file would need custom permission handling and
   would disappear whenever `CONFIG_DEBUG_FS` is off — which it is on many production kernels.
3. **Discoverability**: `sensors` and `upower` enumerate the class; a debugfs knob is invisible to
   them.
4. **Review cost**: a class ABI forces us to define semantics (units, ranges, what “auto” means),
   which is exactly the missing work here. A debugfs knob would let us guess in public.

The existing dev-only knobs (`tt_toggle`) are fine because they are a firmware prerequisite, not a
user control; new controls should not follow that precedent.

## 5. Recovering the EC addresses — what we tried, what is next

### 5.1 What we tried, and the outcome

* **Ghidra on `CompatLayerCT.exe`** via the isolated wrapper:
  `tools/ghidra-pod.sh battdp …/CompatLayerCT.exe '^(battery|addr|EC_)'` →
  `DumpAll.java> WROTE …/decomp.txt functions=0 decompiled=0`. The PE/CLI parses (the field names
  above come from its strings), but the bodies are CIL; Ghidra’s decompiler has no IL
  front end, so there is nothing to read. Recorded in [`docs/hw/battery-tdp.md`](../docs/hw/battery-tdp.md) §2.1.
* **Native neighbours**: `tools/intel/PowerPlugin.dll` is RAPL/MSR-only, `UtilPlugin.dll` and
  `HWAPIExtDLL.dll` carry no battery/EC constants. The native EC driver object is not in the NSIS
  payload.
* **.NET decompiler**: not installed on this host (`ilspycmd`, `ilspy`, `monodis`, `ikdasm`,
  `ildasm`, `dotnet`, `mono`, `dnSpy` all absent). No network install was attempted. So the IL
  immediates remain unread.
* **ACPI**: `DSDT`/`SSDT` are root-only here; `CONFIG_ACPI_EC_DEBUGFS` is not built on this kernel
  and `/sys/kernel/debug` is not readable from the session. Not attempted.

### 5.2 Recommended order (cheapest, least risky first)

1. **Read the IL with a real .NET decompiler** (ILSpy CLI, dnSpy, or `ilspycmd` in a throwaway
   container). Search for `batterySetByPassPowerMode`, `batterySetChargeLimit`, `batteryInitEc`;
   the `EC_ADDR_*`/`EC_VALUE_*` fields are almost certainly `static readonly` bytes assigned from
   `ldc.i4` in the type initializer. That is the whole map for the battery side. This is the
   highest-value next step and does not touch the device.
2. **Windows EC trace** (ports `0x62`/`0x66`, the vendor itself uses `PawnIOLib`/`inpoutx64`) while
   toggling exactly one UI control at a time: record command/data writes, then dump EC RAM before
   and after. This gives addresses *and* encodings, including mode 1 vs mode 2.
3. **ACPI dump** (`acpidump -b`, `iasl -d`, grep the EC methods and the SSDTs behind the four
   unbound WMI GUIDs from `docs/hw/gap-audit.md` §1). If the firmware has an `_Qxx`/EC method for
   the fan or bypass, it documents the register for us.
4. **Differential EC dump around a known value**: read the known charge cap (80 %) from the EC and
   look for a byte holding `0x50` near the candidate `0xA3`; toggle `charge_behaviour` and see which
   byte changes near `0xA4`. This is non-destructive and can confirm the X1-derived guess.
5. **Only then write**: a wrong EC byte can stop charging, spin the fan or confuse the firmware; a
   full power cycle is the usual recovery, but this must be a supervised experiment, not a probe.

The addresses must be in the patch as named board constants with a comment naming the evidence
(trace/IL dump), exactly like the existing X1/Apex constants. No guessed `0x??` in a submitted
patch.

## 6. Patch sketch (our own code; **not compiled**, not a submission)

```c
// SPDX-License-Identifier: GPL-2.0+
/*
 * SKETCH ONLY - written for the RFC, not built against the real oxpec tree.
 * Placeholders marked UNKNOWN must be replaced by evidence before any patch.
 */

/* One EC curve table. The layout below is the *hoped* layout: N pairs of
 * (temperature, duty) bytes, table base = reg, point M at reg + 2*M. */
struct oxp_fan_curve {
	const char *label;	/* "cpu", "gpu", "mid" */
	u8 reg;			/* UNKNOWN: EC base address */
	u8 points;		/* UNKNOWN: 0 disables the channel */
	u8 temp_min, temp_max;	/* UNKNOWN: valid EC range, deg C */
	u8 duty_max;		/* 184 on this board, see pwm scaling */
};

/* UNKNOWN: replaced by the recovered map; points=0 means "not exposed". */
#define OXP_CURVE_COUNT		ARRAY_SIZE(oxp_curves)
#define OXP_MAX_POINTS		16	/* upper bound for the attribute table */

static const struct oxp_fan_curve oxp_curves[] = {
	{ .label = "cpu", .reg = 0x00, .points = 0, .temp_min = 0, .temp_max = 100, .duty_max = 184 },
	{ .label = "gpu", .reg = 0x00, .points = 0, .temp_min = 0, .temp_max = 100, .duty_max = 184 },
	{ .label = "mid", .reg = 0x00, .points = 0, .temp_min = 0, .temp_max = 100, .duty_max = 184 },
};

/* One dynamic sysfs attribute: pwm<curve>_auto_point<point>_{temp,pwm} */
struct oxp_curve_attr {
	struct device_attribute dev_attr;
	u8 curve;
	u8 point;
	bool is_temp;
};

struct oxp_curve_attr *oxp_curve_attrs;	/* devm_kcalloc'ed at probe */

static ssize_t oxp_curve_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct oxp_curve_attr *ca = container_of(attr, struct oxp_curve_attr,
						 dev_attr);
	const struct oxp_fan_curve *c = &oxp_curves[ca->curve];
	long val;
	int ret;

	if (ca->point >= c->points)
		return -EINVAL;

	ret = read_from_ec(c->reg + 2 * ca->point + (ca->is_temp ? 0 : 1), 1,
			   &val);
	if (ret)
		return ret;

	if (ca->is_temp)
		val *= 1000;	/* hwmon auto_point_temp is millidegrees */

	return sysfs_emit(buf, "%ld\n", val);
}

static ssize_t oxp_curve_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct oxp_curve_attr *ca = container_of(attr, struct oxp_curve_attr,
						 dev_attr);
	const struct oxp_fan_curve *c = &oxp_curves[ca->curve];
	long val, raw, check;
	u8 reg;
	int ret;

	if (ca->point >= c->points || kstrtol(buf, 10, &val))
		return -EINVAL;

	if (ca->is_temp) {
		if (val % 1000 || val < c->temp_min * 1000L ||
		    val > c->temp_max * 1000L)
			return -EINVAL;
		raw = val / 1000;
	} else {
		if (val < 0 || val > 255)
			return -EINVAL;
		/* The EC duty range is 0..184 here; scale like pwm1 does. */
		raw = (val * c->duty_max) / 255;
	}

	reg = c->reg + 2 * ca->point + (ca->is_temp ? 0 : 1);
	ret = write_to_ec(reg, raw);
	if (ret)
		return ret;

	/* No silent acceptance: read back and compare the *raw* value. */
	ret = read_from_ec(reg, 1, &check);
	if (ret)
		return ret;
	if (check != raw)
		return -EIO;

	return count;
}

static int oxp_curve_attrs_create(struct device *hwmon)
{
	unsigned int n = 0, i, m;
	int ret;

	oxp_curve_attrs = devm_kcalloc(hwmon, OXP_CURVE_COUNT * OXP_MAX_POINTS * 2,
				       sizeof(*oxp_curve_attrs), GFP_KERNEL);
	if (!oxp_curve_attrs)
		return -ENOMEM;

	for (i = 0; i < OXP_CURVE_COUNT; i++) {
		for (m = 0; m < oxp_curves[i].points; m++) {
			for (int t = 0; t < 2; t++) {
				struct oxp_curve_attr *ca = &oxp_curve_attrs[n++];

				ca->curve = i;
				ca->point = m;
				ca->is_temp = t;
				sysfs_attr_init(&ca->dev_attr.attr);
				ca->dev_attr.attr.name =
					devm_kasprintf(hwmon, GFP_KERNEL,
						       "pwm%u_auto_point%u_%s",
						       i + 1, m + 1,
						       t ? "temp" : "pwm");
				if (!ca->dev_attr.attr.name)
					return -ENOMEM;
				ca->dev_attr.attr.mode = 0644;
				ca->dev_attr.show = oxp_curve_show;
				ca->dev_attr.store = oxp_curve_store;

				ret = device_create_file(hwmon, &ca->dev_attr);
				if (ret)
					return ret;
			}
		}
	}

	return 0;
}
/* Removal must mirror the loop with device_remove_file(), or the probe
 * should register a devm action so a partial failure cannot leave dangling
 * attributes. Not shown in this sketch. */
```

Charge side (same sketch; extends the existing extension rather than adding a second one):

```c
/* UNKNOWN: values from the vendor constants, not from the field names. */
struct oxp_charge_map {
	u8 limit_reg;
	u8 bypass_reg;
	u8 bypass_off;
	u8 bypass_always;
	u8 bypass_awake;
};

static const struct oxp_charge_map oxp_g1_charge = {
	.limit_reg		= 0xA3,	/* candidate, confirm per §3.2 */
	.bypass_reg		= 0xA4,	/* candidate, confirm per §3.2 */
	.bypass_off		= 0x00,	/* EC_VALUE_BYPASS_POWER_MODE_OFF */
	.bypass_always		= 0x03,	/* EC_VALUE_BYPASS_POWER_MODE1 */
	.bypass_awake		= 0x01,	/* EC_VALUE_BYPASS_POWER_MODE2 */
};

static int oxp_charge_get(struct power_supply *psy,
			  const struct power_supply_ext *ext, void *data,
			  enum power_supply_property psp,
			  union power_supply_propval *val)
{
	const struct oxp_charge_map *m = data;
	long raw;
	int ret;

	switch (psp) {
	case POWER_SUPPLY_PROP_CHARGE_CONTROL_END_THRESHOLD:
		ret = read_from_ec(m->limit_reg, 1, &raw);
		if (ret)
			return ret;
		if (raw < 0 || raw > 100)
			return -EINVAL;
		val->intval = raw;
		return 0;
	case POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR:
		ret = read_from_ec(m->bypass_reg, 1, &raw);
		if (ret)
			return ret;
		if (raw == m->bypass_off)
			val->intval = POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO;
		else if (raw == m->bypass_always)
			val->intval = POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE;
		else if (raw == m->bypass_awake)
			val->intval =
				POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE_AWAKE;
		else
			return -EINVAL;	/* do not guess an unknown EC state */
		return 0;
	default:
		return -EINVAL;
	}
}

static int oxp_charge_set(struct power_supply *psy,
			  const struct power_supply_ext *ext, void *data,
			  enum power_supply_property psp,
			  const union power_supply_propval *val)
{
	const struct oxp_charge_map *m = data;
	long raw, check;
	int ret;

	switch (psp) {
	case POWER_SUPPLY_PROP_CHARGE_CONTROL_END_THRESHOLD:
		if (val->intval < 0 || val->intval > 100)
			return -EINVAL;
		ret = write_to_ec(m->limit_reg, val->intval);
		if (ret)
			return ret;
		ret = read_from_ec(m->limit_reg, 1, &check);
		if (ret)
			return ret;
		return check == val->intval ? 0 : -EIO;
	case POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR:
		switch (val->intval) {
		case POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO:
			raw = m->bypass_off;
			break;
		case POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE:
			raw = m->bypass_always;
			break;
		case POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE_AWAKE:
			raw = m->bypass_awake;
			break;
		default:
			return -EINVAL;	/* only advertise states we can set */
		}
		ret = write_to_ec(m->bypass_reg, raw);
		if (ret)
			return ret;
		ret = read_from_ec(m->bypass_reg, 1, &check);
		if (ret)
			return ret;
		return check == raw ? 0 : -EIO;
	default:
		return -EINVAL;
	}
}
```

ABI documentation text we would add (our prose; hwmon auto-point attributes are already documented,
so this is the board-specific part):

> `pwm1_auto_pointM_temp`, `_pwm` describe the EC’s CPU fan curve; `pwm2_*` the GPU curve and
> `pwm3_*` the mid/board curve. The three channels share the single tachometer `fan1_input`; they
> are curve domains, not separate fans. Temperatures are millidegrees Celsius, duties 0–255 (the
> driver scales to the EC range). The attributes exist only on boards whose EC layout has been
> recovered and are read/write. `pwm1_enable` selects full-speed/manual/EC-auto for the fan as a
> whole; in EC-auto mode the EC follows the curve it selects, and writing a curve does not by
> itself switch modes.
>
> `charge_behaviour`: `auto` = normal charging; `inhibit-charge` = the EC bypass state that applies
> always; `inhibit-charge-awake` = the vendor “only while the screen/system is on” state. The EC
> cannot distinguish “screen on” from “system awake”; the attribute follows the kernel meaning.
> `charge_control_end_threshold` is the percentage cap stored in the EC.

## 7. What a maintainer must verify before this is a patch

1. **The EC map is real.** Addresses, table layout, point count, temperature unit, duty range and
   the three bypass values, each with a trace/IL evidence reference. No placeholder may survive.
2. **The `0xA3`/`0xA4` question in §3.2.** If the current 7.2.7 `oxp_g1_i` mapping already drives
   these bytes, the patch is much smaller than this draft assumes — and the observed
   `charge_control_end_threshold` source changes. Settle it first.
3. **Existing helper bug.** `read_from_ec()` returns early on `ec_read()` failure without releasing
   the global ACPI lock (and also on unlock failure); every new user inherits it. Fix that in a
   separate, first patch.
4. **DMI scope.** The new attributes must be visible only for the board(s) with a recovered map;
   X1/F1/Apex keep their current behaviour. `oxp_board` and the `is_visible` switch are the places
   to gate.
5. **ABI review.** Does hwmon accept three `pwmN` curve domains with one tach? If not, fall back to
   the “active table only” shape or drop the tables. This is the main design question.
6. **Concurrency**: `tt_toggle`, HHD, the hwmon core and `acpi_ec` all touch the same EC; the
   driver’s global ACPI lock is the current serialization. Test with HHD running.
7. **Suspend/resume**: the EC may reload curves or drop manual mode; re-read and re-apply (or
   document that userspace must).
8. **Compile/CI**: `make W=1`, `sparse`, `checkpatch`, and a probe test on at least the OXP3 and one
   X1/Apex board so the new visibility logic is exercised.

## 8. Risks

* **Thermal**: a wrong curve can under-cool the device; the EC’s own thermal protections remain,
  but a bad table is still a real risk. Keep the EC auto mode as the default; never enable a curve
  at probe.
* **Charging**: a wrong bypass byte can stop charging or, worse, write an unrelated EC register.
  Supervised experiment only, with a full power cycle as recovery.
* **EC/firmware**: EC RAM writes are persistent until the next EC reset and may interact with
  vendor firmware in undocumented ways. The driver must not write at probe.
* **ABI regret**: the biggest risk is shipping a guessed curve shape. The RFC’s fallback (no curve
  attributes until decoded) is deliberate.

## 9. Open questions

1. Where are the EC addresses (the whole point of §5)?
2. What is the table layout and unit? Does `addrFanAutomate` gate the tables, or is the auto/manual
   mode a separate byte from `pwm1_enable`’s source?
3. Are the three tables per-partition (CPU/GPU/mid sensors) or three alternative whole-fan curves?
   The vendor names suggest partitions; the single tach suggests alternatives.
4. Is `addrBypassPower` the same byte as `0xA4`? Which of the three vendor values does the current
   `0x01/0x02/0x03` mask correspond to?
5. Does mode 2 really track the panel/screen state, or the system sleep state (which the kernel ABI
   can express)?
6. Should presets/lock ever be exposed, and if so where (not hwmon, not debugfs)?
7. Does `addrForceChargeMin` exist on this EC, and is it a lower threshold or a “force charge until”
   value?
