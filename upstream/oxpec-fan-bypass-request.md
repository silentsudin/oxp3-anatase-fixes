# RFC draft — `oxpec`: back the OneXPlayer 3 charging-bypass byte with the EC (and close the fan-curve question)

Status: **draft, not submitted**. No account, no GitLab/GitHub permissions and no vendor support
are available here, and nothing below has been applied to a kernel tree. The code in §6 is our own
**sketch**, written for review, not compiled against the real `oxpec` driver; §7 lists what a
maintainer (or we, with hardware access) must verify before it could be a patch.

Prepared: 2026-11-09, from the evidence in this repository. Target tree for the API references:
`drivers/platform/x86/oxpec.c` at 7.2.7 (`7.2.7-an19-8` local source tree; the unit runs
`7.2.7-an28.fc45`). Device: ONEXPLAYER 3, DMI `ONE-NETBOOK` / `ONEXPLAYER 3`, BIOS 5.09, board
`oxp_g1_i` in the current driver.

> **Revision, 2026-11-09 (later the same day).** The EC register map is no longer the blocker: the
> addresses and the value encodings were recovered from the app's own bootstrap and are
> cross-checked against upstream `oxpec`. See [`oxpec-ec-addresses.md`](oxpec-ec-addresses.md) for
> the table and the method. Two claims below were corrected in the process: (a) there is **no
> three-table EC fan curve on this machine** — `DevsCPUFanCurve/GPUFanCurve/MidFanCurve` are ASUS
> WMI device ids, and the “intelligent fan curve” string belongs to the Bluetooth water-cooling
> accessory; (b) the vendor **mode 1 is the awake-only inhibit** (`0x01`) and **mode 2 is the always
> inhibit** (`0x03`), i.e. the opposite of what §4.2 originally assumed. §5 and the sketch in §6 are
> updated accordingly; the ask in §1 narrows to the charge path plus the already-existing fan ABI.

Related local evidence (all our own prose, no vendor code):

* [`docs/hw/power-fan.md`](../docs/hw/power-fan.md) — fan/EC inventory, the three curve tables, the
  PWM scaling, the existing kernel gap.
* [`docs/hw/battery-tdp.md`](../docs/hw/battery-tdp.md) — battery/charge inventory, vendor bypass
  semantics (off / mode 1 / mode 2) and, now, the recovered EC addresses.
* [`upstream/oxpec-ec-addresses.md`](oxpec-ec-addresses.md) — the register map with provenance,
  confidence and the remaining candidates.
* [`docs/hw/gap-audit.md`](../docs/hw/gap-audit.md) — rows 2 and 5, and the §5 note about IL
  immediates.

---

## 1. The ask, in one paragraph

After the register map was recovered (§5) the ask shrinks to almost nothing, which is the honest
outcome of this investigation:

* **(a) Nothing for the fan.** The OXP3 EC does not hold vendor curve tables: it has an
  auto/manual bit (`0x4A`), a duty byte (`0x4B`) and a 16-bit tach (`0x58`/`0x59`), and “intelligent
  fan curve” is the EC's own closed loop. `oxpec` **already exposes all of it** as `pwm1`,
  `pwm1_enable` and `fan1_input`. A curve has to live in userspace, where our
  [`tools/oxp3-powerctl.py`](../tools/oxp3-powerctl.py) `fan curve` already puts it. §4.1 and the
  fan half of §6 are kept only as a labelled design for boards that *do* have tables.
* **(b) The charge path, mostly already done.** The bypass byte is EC `0xA4` with `0x00` off /
  `0x01` inhibit-while-awake / `0x03` inhibit-always, and the charge cap is EC `0xA3` in percent.
  Both are already driven through the existing `power_supply_ext` for `oxp_g1_i` in the 7.2.7 tree
  (`charge_behaviour`, `charge_control_end_threshold`). What is missing is one byte,
  `addrForceChargeMin` = `0xA5`, which has no ABI — and no unit we can prove.
* **(c) No new `debugfs` user-facing knobs**, still true, and now with less pressure for any new
  sysfs at all.

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
| bypass | `battery_protection_bypasspowermode`, `…_effect_always`, `…_effect_when_screen_on`, `…_hint` | mode 1 = ineffective while asleep/off (`0x01`); mode 2 = always effective (`0x03`); “plugged in → charging stops, device is powered, less heat” |
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

* the vendor’s **mode 1 is the awake-only inhibit**, which is what `inhibit-charge-awake` means,
  so the ABI maps cleanly (`0x01`); **mode 2 is the always-inhibit** (`0x03`). The residual
  difference is that the kernel’s “awake” is a system-suspend boundary, not a panel state — say so
  in the ABI text, and userspace (upower, our `oxp3-powerctl`) must not be misled.
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

### 4.1 Fan curves — hwmon auto-point attributes **(withdrawn, premise falsified)**

> 2026-11-09: there are no vendor curve tables on this board. `DevsCPUFanCurve`/`GPUFanCurve`/
> `MidFanCurve` are ASUS WMI device ids (`0x110024`/`0x110025`/`0x110032`), the app never sends a
> curve over its fan IPC, and the “intelligent fan curve” string belongs to the Bluetooth
> water-cooling accessory. What follows is kept as a design sketch for a board that does have
> tables; do not implement it for `oxp_g1_i`. See
> [`oxpec-ec-addresses.md`](oxpec-ec-addresses.md) §6.

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
| mode 1 | `inhibit-charge-awake` | EC `0x01` = the awake bit only; ineffective while asleep/off |
| mode 2 | `inhibit-charge` | EC `0x03` = awake bit OR the board's “always” bit(s); the OXP3 always-mask is `0x02` |

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

### 5.1 What happened: the IL is empty, the numbers are in the app

The first attempt (Ghidra via `tools/ghidra-pod.sh battdp`) decompiled **0** functions, and a proper
.NET run explains why: `CompatLayerCT.exe` is not packed, it is **stripped**. 2829 of its 3706 method
bodies point at one RVA whose whole content is `throw new Exception("Runtime exception")`, 189 more
share a trivial constructor stub, and only 35 bodies are distinct (`<Module>::.cctor` and compiler
lambdas). Metadata is intact — that is where the field and *parameter* names come from — but there is
no `ldc.i4` to read, and no native component or decryptor to undo, so **nothing was ever hidden in the
IL**. Reading it "properly" was the wrong plan, not an unlucky one.

The numbers are one layer up. `app.asar`'s main-process bootstrap declares the per-OEM EC addresses
as decimal locals and passes them *into* the layer over its named pipe (`/battery/initEc`,
`/fan/init`, `/battery/initPowerSupplyModeEC`, …). The assembly only ever needs the *names*, which is
exactly what metadata shows. Full table, offsets and method in
[`oxpec-ec-addresses.md`](oxpec-ec-addresses.md); the short version for this patch:

| what | EC offset | what the driver must do |
|---|---:|---|
| `addrFanAutomate` | `0x4A` | already in the tree (`pwm1_enable`, `0x00` auto / `0x01` manual) |
| `addrFanSpeed` | `0x4B` | already in the tree (`pwm1`, scaled to 0…184 on this board) |
| `fanSpeedHECAddr`/`L` | `0x58`/`0x59` | `fan1_input`, 2 bytes — upstream master already reads `0x58` for `oxp_g1_i` |
| `addrChargeLimit` | `0xA3` | already in the tree (`charge_control_end_threshold`, percent) |
| `addrBypassPower` | `0xA4` | already in the tree (`charge_behaviour`, `0x00`/`0x01`/`0x03`) |

Everything the kernel needs for the **battery** side is therefore already implemented, and the
remaining ask is much smaller than this draft originally assumed (`addrForceChargeMin` at `0xA5` is
the only charge byte with no ABI). What is *not* already there is none of the three fan curves —
because on this board they do not exist as EC tables (§6 of `oxpec-ec-addresses.md`).

### 5.2 What is genuinely still open

1. **`0xA5` force-charge minimum**: unit (percent floor?) — one Windows trace with the vendor slider
   settles it (§7 of `oxpec-ec-addresses.md`).
2. **Sensor scales** (`0x60`, `0x61`, `0x70`, `0xA0`…`0xA2`): addresses are candidate-grade, the
   encodings are not known; a two-point comparison against the vendor UI gives the affine map.
3. **`0x2D` “handle power” and `0xEB` “app function enable”**: written by the vendor layer, meaning
   unknown, no ABI proposed here.
4. **Command/data ports** (`0x66`/`0x62`): irrelevant for the OXP3 path, which selects
   `ECAccessType::OxpWMI` rather than port I/O; only needed if someone wants a port fallback.
5. **One real-device confirmation**: a trace (or `ec_sys` on a debug kernel) that toggling
   `charge_behaviour` changes `0xA4` and that `0x58`/`0x59` tracks rpm. Nothing below is submitted
   before that, but the values in the sketch are now sourced, not guessed.


The addresses must be in the patch as named board constants with a comment naming the evidence
(trace/IL dump), exactly like the existing X1/Apex constants. No guessed `0x??` in a submitted
patch.

## 6. Patch sketch (our own code; **not compiled**, not a submission)

> The fan-curve half of this sketch (up to the charge section) is **withdrawn** for the same reason
> as §4.1: no tables exist on this board. The charge half is current and every value in it now has
> a source — see [`oxpec-ec-addresses.md`](oxpec-ec-addresses.md) §1.

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
/* Values below come from the vendor's own per-OEM bootstrap (app.asar, ONEXPLAYER 3 branch) and
 * match the constants this driver already uses for oxp_g1_i. Provenance and offsets:
 * upstream/oxpec-ec-addresses.md. Two values are still candidate-grade: the 0xA5 byte and its
 * unit; do not wire it until a trace says what it means. */
struct oxp_charge_map {
	u8 limit_reg;
	u8 bypass_reg;
	u8 bypass_off;
	u8 bypass_always;
	u8 bypass_awake;
};

static const struct oxp_charge_map oxp_g1_charge = {
	.limit_reg		= 0xA3,	/* vendor addrChargeLimit 0x4A3, percent 0..100 */
	.bypass_reg		= 0xA4,	/* vendor addrBypassPower 0x4A4, bit mask */
	.bypass_off		= 0x00,	/* EC_VALUE_BYPASS_POWER_MODE_OFF */
	.bypass_always		= 0x03,	/* EC_VALUE_BYPASS_POWER_MODE2 (awake | "off" bits) */
	.bypass_awake		= 0x01,	/* EC_VALUE_BYPASS_POWER_MODE1 (awake bit only) */
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

1. **The EC map is real.** Addresses and encodings now have two independent sources each
   ([`oxpec-ec-addresses.md`](oxpec-ec-addresses.md) §1 and §7); what is *not* settled is a
   device-side confirmation, and the `0xA5`/sensor rows that are still candidate-grade. No
   placeholder may survive in a submitted patch.
2. **The `0xA3`/`0xA4` question in §3.2 is answered: yes**, the 7.2.7 `oxp_g1_i` mapping already
   drives exactly these bytes (`OXP_X1_CHARGE_LIMIT_REG`/`_INHIBIT_REG`), which is why the observed
   `charge_control_end_threshold` exists at all. The charge patch therefore reduces to the `0xA5`
   question, not to a new register map.
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
