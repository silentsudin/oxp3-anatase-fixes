# Suspend / S0ix power on the OneXPlayer 3 (s2idle, near-zero drain)

Target: closing the lid should cost well under **1 %/h** of the 87.7 Wh pack, and the machine should be
cold to the touch when it comes back out of a bag - not merely "screen off". This file records what the
machine actually does today, what we changed, how to prove it and how to undo it.

Everything here was measured on 2026-11-08 on kernel `7.2.7-an28.fc45.x86_64` (Anatase, 60 boots in the
journal). Nothing in this document has been applied to the host: the `sudo` lines below are the exact
commands, run them in the order given.

- Measurement tool: [`tools/sleep-power-measure.sh`](../../tools/sleep-power-measure.sh)
- Wake-source policy: [`files/oxp3-sleep-wakeup`](../../files/oxp3-sleep-wakeup) +
  [`files/oxp3-sleep-wakeup.service`](../../files/oxp3-sleep-wakeup.service) +
  [`files/99-oxp3-sleep-wakeup.rules`](../../files/99-oxp3-sleep-wakeup.rules)
- What-woke-it logging: [`files/oxp3-sleep-log`](../../files/oxp3-sleep-log) with
  `oxp3-sleep-pre.service` / `oxp3-sleep-log.service`

## TL;DR

1. **This device has never suspended under Linux.** All 60 retained boots contain zero
   `PM: suspend entry` lines, and `/sys/power/suspend_stats/success` is 0. "Suspend is inefficient" is
   currently "suspend does not happen": the two hours after a lid close are spent as an awake desktop
   (7.8 W package power measured here, plus panel and radios), which is exactly the warm-to-touch,
   flat-battery report.
2. s2idle itself is set up correctly by the firmware and the kernel: `/sys/power/mem_sleep` is
   `[s2idle] deep`, `/sys/power/state` is `freeze mem`, and the kernel logs
   `Low-power S0 idle used by default for system suspend`. No kernel-side switch is missing.
3. Nine ACPI wake sources and ten device wake bits are armed, including both internal HID endpoints,
   the gamepad, USB4/Type-C and the AC adapter. That is the part that turns "asleep" into "woke up at
   03:00, stayed awake".
4. Nothing in the Windows side programs an EC sleep mode that Linux would have to replicate: the vendor
   console only calls Win32 sleep/hibernate, and the Intel helper DLL and its kernel driver contain no
   suspend or wake code at all (Ghidra, section "Windows/EC evidence").

## 1. Baseline: what the machine reports

| Fact | Value | How to read it |
|---|---|---|
| suspend state | `/sys/power/mem_sleep` = `[s2idle] deep` | s2idle selected; `deep` (S3) is offered but is not a real PTL state |
| states | `/sys/power/state` = `freeze mem` | only these two can be written |
| hibernation | `/sys/power/disk` = `[disabled]` | secure-boot lockdown (`/sys/kernel/security/lockdown` = `none [integrity] confidentiality`) - hibernate is unavailable, so "sleep then hibernate" cannot be a fallback |
| suspend counter | `suspend_stats/success` = 0, `fail` = 0, `total_hw_sleep` = 0 | **no suspend has ever completed on this boot** |
| suspend anomaly | `max_hw_sleep` = 523,986,009,990 ns (~524 s) with success = 0 | internally inconsistent: an attempt would bump `success` or one of the `failed_*` counters. Treat as stale, re-read after the first controlled suspend (open point 2) |
| history | `journalctl --list-boots` = 60 boots, `journalctl -k \| grep -c "PM: suspend entry"` = **0** | the machine has not suspended once in the retained journal |
| wake sources | 9 ACPI entries `*enabled`, 7 PCI + 2 USB device wake bits enabled | section 2.3 |
| inhibitors | 9 (`systemd-inhibit --list`) | one `block`: PowerDevil, `handle-power-key:handle-suspend-key:handle-hibernate-key:handle-lid-switch`; the rest are `delay` (hhd for HDMI-CEC, kwin for locking, UPower, NetworkManager, ModemManager, rtkit, deepseek-harness) |
| KDE power config | `~/.config/powerdevilrc` contains **only** `[AC][SuspendAndShutdown] AutoSuspendAction=0` | no `LidAction`, no `[Battery]` group: the lid action is PowerDevil's implicit default and there is no record of what it is |
| platform profile | `balanced` (`low-power balanced performance`) | `low-power` would cut *awake* drain; not applied, see R5 |
| battery | 70.253 Wh of 87.665 Wh at 80 %, `Charging`; design 84.549 Wh | capacity health 103.7 %; 1 %/h = **0.877 Wh/h = 0.877 W** |
| temperatures at 23:50 (with the agent workload running) | `x86_pkg_temp` 56 °C, `TCPU` 56 °C, `iwlwifi` 59 °C, `acpitz` 27.8 °C | 56 °C idle-ish is a 7.8 W package, not a suspended machine |
| RAPL publish | `/run/rapl-power/package-0` = 7.8 W, `core` 5.0 W, `uncore` 0.1 W | from our own `rapl-power` service (1 s averages, 0.1 W) |
| kernel cmdline | `rhgb quiet root=UUID=... rw xe.enable_panel_replay=0` | no `mem_sleep_default`, nothing suspend-related |
| NVMe | `Predator SSD GM7 1TB` (Biwin HP FX700, `1dee:1602`); boot: `nvme 0000:01:00.0: platform quirk: setting simple suspend`; `nvme_core.default_ps_max_latency_us` = 100000, `force_apst` = N | section 2.4 |
| PCIe ASPM | `/sys/module/pcie_aspm/parameters/policy` = `default` (options: `default performance powersave powersupersave`) | needs a root `lspci -vv` to see the per-link L1 substates, see open point 3 |

Reproduce the whole table on demand:

    tools/sleep-power-measure.sh audit          # as user; add sudo for S0ix residency

## 2. Findings

### 2.1 It never suspends, so there is nothing to be "inefficient" about yet

Zero `PM: suspend entry`, `success` = 0, and no `failed_*` counters either - so the kernel is not
rejecting suspends, it is never being *asked*. The only thing that decides that on this machine is
KDE: PowerDevil holds the `block` inhibitor for `handle-lid-switch`, so `systemd-logind` will not act on
the lid, and `powerdevilrc` has no `LidAction` recorded. The Steam log in the same boot shows the other
half of the problem:

    16:16:53 steam[...]: [Backend]: Preventing machine to sleep

That is a userspace idle inhibitor; it does not block a lid-triggered suspend, but it does block the
"idle desktop after 15 minutes" path, so both paths have to be checked.

### 2.2 s2idle configuration is already right

`mem_sleep` = `[s2idle]`, `state` = `freeze mem`, and the kernel took the low-power path by itself
(`Low-power S0 idle used by default for system suspend`). `intel_pmc_core` is loaded, so the S0ix
residency counter exists - it is just root-only:

    sudo cat /sys/kernel/debug/pmc_core/slp_s0_residency_usec

The unprivileged `/sys/devices/system/cpu/cpuidle/low_power_idle_*_residency_us` counters read 0 on
x86 (they are the ACPI-LPI/ARM interface), so **do not** use them as evidence either way.

### 2.3 Wake sources: what can turn a suspend into a 7.8 W desktop

ACPI table (`/proc/acpi/wakeup`), the nine entries currently `*enabled`:

| Entry | Device | Keep? | Why |
|---|---|---|---|
| `LID0` | `PNP0C0D:00` lid | **yes** | opening the lid must wake it |
| `PWRB` | `PNP0C0C:00` power button | **yes** | the only real wake control on a handheld |
| `SLPB` | `PNP0C0E:00` sleep button | **yes** | harmless, EC-driven |
| `AWAC` | `ACPI000E:00` wake alarm | **yes** | this is what `rtcwake`/`alarmtimer` programs; without it there is no timed wake |
| `XHCI` | `0000:00:14.0` standalone xHCI | no | USB-A ports, the internal HID pair and the gamepad all hang off it |
| `TXHC` | `0000:00:0d.0` Type-C xHCI | no | USB4/Type-C |
| `TDM0` | `0000:00:0d.2` Thunderbolt/USB4 DMA | no | same subsystem |
| `TRP0` / `TRP1` | `0000:00:07.0/1` Type-C root ports | no | same subsystem |
| `RP03` | `0000:00:1c.0` card reader root port | no | storage does not need to wake the machine |
| `RP09` | `0000:00:06.0` NVMe root port | no | ditto; its PME can fire while the link retrains |

Device wake bits (`/sys/.../power/wakeup` = `enabled`) - 7 PCI + 2 USB:

    0000:00:06.0  0000:00:07.0  0000:00:07.1  0000:00:0d.0  0000:00:0d.2  0000:00:14.0
    0000:00:1c.0  usb 3-4  usb 3-5

Neither table is a value: `/proc/acpi/wakeup` entries are **toggles** and the device bits come back
`enabled` on every re-enumeration, which is why the policy is a script plus a udev rule rather than a
one-shot write. The AC adapter (`ADP1`) is enabled as a *device* wakeup as well; plugging the charger
should not wake a handheld either.

The two USB ones are internal, fixed-mount endpoints that never autosuspend either:

| USB | IDs | What | runtime PM |
|---|---|---|---|
| `3-4` | `1997:2433` | "mini keyboard" endpoint (usbhid) | `control=on`, always `active` |
| `3-5` | `1a86:fe00` | EC bridge (usbhid, serial `0123456789`) - the device hhd writes EC registers through | `control=on`, always `active` |
| `3-7` | `045e:028e` | internal gamepad (xpad) | `control=on`, always `active`; unbinding it around sleep is already our `oxp3-xpad-sleep` (saves a 5 s suspend timeout) |

That is the actual bag-drain mechanism on this class of device: anything that nudges the pad, a Type-C
cable or the lid in a bag wakes the machine, and the machine then runs an awake desktop at ~8 W until
someone notices.

### 2.4 Storage, firmware and the EC (what we can and cannot control)

- The kernel states `platform quirk: setting simple suspend` for the NVMe at boot: the platform ACPI
  declares that the storage device's D3 transition is owned by the firmware, so the driver takes the
  simple-suspend path. Consequence to check during the first real suspend: whether the SSD ever leaves
  D0 (open point 5).
- `nvme_core.default_ps_max_latency_us` = 100000 µs and `force_apst` = N. This knob can only *remove*
  power states, never add them, so it is **not** a fix for drain; `0` would disable APST entirely and
  make the SSD hotter. Do not touch it (R4).
- Lockdown `integrity` disables hibernation, so "suspend then hibernate" is not available as a way to
  reduce overnight drain - the s2idle drain itself has to be right (open point 6).
- **Windows/EC evidence** - does the vendor do anything to the EC at sleep time that Linux would have to
  reproduce? No:
  - `OneXConsole_0.10.4-fix1.exe` is an NSIS wrapper around an Electron app (`resources/app.asar`,
    `node-hid`, `@serialport`). Its power UI calls Win32 only: `sleepWindows()`, `hibernateWindows()`,
    `triggerMonitorOff()`, plus a configurable "sleep to hibernate" timeout
    (`handykit_sleep_to_hibernate`, `getSleepHibernateTimeout`). The EC is reached exclusively through
    HID reports from JavaScript - there is no EC register write for sleep.
  - Ghidra on `resources/resources/tools/intel/PowerPlugin.dll` (the Intel power plug-in the console
    ships): the only sleep-related import is the Win32 **delay** `Sleep`, used as `Sleep(1)` inside a
    10-try polling loop around an MSR/telemetry read; no `SetSuspendState`, no `ExitWindowsEx`
    (11 functions reference the matched imports). It is a monitor plug-in, not a suspend path.
  - The kernel-side `CCHWAPIExt.sys` imports only `KeStallExecutionProcessor` and
    `__C_specific_handler`: nothing there touches power transitions.
  - Conclusion: suspend entry on this machine is the ACPI/EC firmware handshake (`_PTS`/`_WAK`, LPIT,
    S0ix entry). There is no vendor EC sleep mode for us to implement - which is good, because the
    Linux side then only has to (a) actually suspend, (b) not be woken, (c) let the package reach S0ix.

Reproduce the two Ghidra results (isolated per-agent container, no shared bridge):

    tools/ghidra-pod.sh sleep /path/to/PowerPlugin.dll '^Sleep$'
    # the dump lands in ~/.cache/ghidra-agents/sleep/out/decomp.txt

## 3. Recommended configuration, ranked

### R1 - make suspend actually happen (highest impact, zero risk)

Set the lid action explicitly instead of relying on an implicit default, and make sure nothing blocks
it. This is a user-level change (no sudo). The authoritative path is the KCM:
**System Settings > Power Management > Energy Saving** - set *When laptop lid closed: Sleep* for both AC
and Battery, and check *When inactive: Sleep* for Battery (the profile file currently records only
`[AC][SuspendAndShutdown] AutoSuspendAction=0`, i.e. no lid policy at all). The scripted equivalent:

    # PowerDevil action enum: 0 nothing, 1 sleep, 2 hibernate, 3 shutdown, 4 lock screen, 8 logout
    kwriteconfig6 --file powerdevilrc --group AC      --key LidAction 1
    kwriteconfig6 --file powerdevilrc --group Battery --key LidAction 1
    # AutoSuspendAction is a timeout in seconds, 0 = never (e.g. 900 = 15 min)
    kwriteconfig6 --file powerdevilrc --group AC      --key AutoSuspendAction 0
    kwriteconfig6 --file powerdevilrc --group Battery --key AutoSuspendAction 900
    # closing the lid with an external monitor attached is ignored unless this is false:
    kwriteconfig6 --file powerdevilrc --group AC      --key InhibitLidActionWhenExternalMonitorPresent false
    kwriteconfig6 --file powerdevilrc --group Battery --key InhibitLidActionWhenExternalMonitorPresent false
    kreadconfig6 --file powerdevilrc --group Battery --key LidAction      # read it back
    # then let PowerDevil reload: System Settings > Power Management, or log out and back in

Verify the *effect*, not the file: the next lid close must produce `PM: suspend entry` in
`journalctl -k -b`.

Check what else can hold the machine awake (`block` matters, `delay` is fine):

    systemd-inhibit --list
    journalctl -b -u powerdevil -u steam --no-pager | grep -iE "inhibit|sleep"

Expected: the first `PM: suspend entry (s2idle)` in the journal, and `suspend_stats/success` > 0.
This alone moves the machine from ~8 W awake to whatever s2idle costs.

### R2 - disarm the wake sources that should not wake it (shipped here)

`oxp3-sleep-wakeup.service` applies a bag-safe policy at boot: lid, power button, sleep button and the
ACPI wake alarm stay armed; xHCI (both controllers), USB4/Type-C, the card reader root port, the NVMe
root port, the AC adapter and both internal HID endpoints are disarmed. `99-oxp3-sleep-wakeup.rules`
re-applies the USB/PCI part after every resume re-enumeration (a freshly probed device comes back with
`wakeup=enabled`).

`/proc/acpi/wakeup` entries are **toggles**, not values, so the script only writes a name that is
currently `*enabled`; it is idempotent and safe to re-run.

To keep wake-by-gamepad (nice at home, bad in a bag), set `ALLOW_USB_WAKE=1` in the unit; to let the
charger wake it, `ALLOW_AC_WAKE=1`.

### R3 - keep the sleep transition short and observable (shipped here)

`oxp3-xpad-sleep` already unbinds the pad before sleep (5 s timeout avoided on every suspend). The new
`oxp3-sleep-pre.service` / `oxp3-sleep-log.service` pair records, per suspend and in the journal
(`journalctl -t oxp3-sleep`):

- whether the kernel counted a success and how long the hardware really slept,
- the S0ix residency delta from `intel_pmc_core`,
- **which wakeup source fired** (event-count deltas, with the resolved device path).

That is what turns "it woke up in the bag again" into a named wake source.

### R4 - what *not* to set

| Do not | Because |
|---|---|
| `mem_sleep_default=deep` | the platform logs `Low-power S0 idle used by default`; S3 on Panther Lake is not a usable state. Test it only as an experiment (open point 4) |
| `nvme_core.default_ps_max_latency_us=0` | disables APST (the opposite of what we want) and makes the SSD and the chassis hotter; the default 100000 µs already admits every GM7 power state |
| `usbcore.autosuspend=1` in the kernel cmdline | autosuspend is per-device policy; the EC bridge (`1a86:fe00`) is what hhd uses for EC reads, and autosuspending it risks hhd/TDP failures for no measurable gain |
| forcing the two internal HID endpoints to `control=auto` | same reason; they are 2 of the 3 devices the EC path depends on |
| disabling `AWAC`/`rtc_cmos`/`alarmtimer` wake | that is the timed-wake path (`rtcwake`); without it no scheduled resume |

### R5 - optional, measure before keeping

- `pcie_aspm.policy=powersave` at runtime (`sudo` not needed for the write, root needed):
  `echo powersave | sudo tee /sys/module/pcie_aspm/parameters/policy`. Helps *awake* idle drain (and
  can help S0ix, because PC10 needs the storage link in L1.2), but costs SSD latency and is a known
  source of NVMe timeouts on some firmware. Our default stays `default`.
- `platform_profile=low-power` (`echo low-power | sudo tee /sys/firmware/acpi/platform_profile`) cuts
  awake drain, but `oxp3-tdp-sync` deliberately keeps PL1 at hhd's 35 W while gaming; do not mix the two
  without re-measuring the game benchmarks.

## 4. Install

The files are wired into `./oxp3-fixes.sh install` (`EXTRA_UNITS`), which is the supported path:

    cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
    ./oxp3-fixes.sh install        # asks for sudo; installs units, udev rule and libexec scripts

Manual equivalent (in case only the suspend part is wanted):

    sudo install -D -m 0755 files/oxp3-sleep-wakeup /usr/local/libexec/oxp3-sleep-wakeup
    sudo install -D -m 0755 files/oxp3-sleep-log    /usr/local/libexec/oxp3-sleep-log
    sudo install -m 0644 files/oxp3-sleep-wakeup.service files/oxp3-sleep-pre.service \
                        files/oxp3-sleep-log.service /etc/systemd/system/
    sudo install -D -m 0644 files/99-oxp3-sleep-wakeup.rules \
                        /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules
    sudo udevadm control --reload
    sudo systemctl daemon-reload
    sudo systemctl enable --now oxp3-sleep-wakeup.service
    sudo systemctl enable oxp3-sleep-pre.service oxp3-sleep-log.service
    systemctl status oxp3-sleep-wakeup.service          # must say "policy applied"

Optional, if you prefer the classic hook instead of the two units (note: `/usr` is a transient overlay
on this ostree host, so a hook in `/usr/lib/systemd/system-sleep/` disappears on reboot/update):

    sudo install -D -m 0755 files/oxp3-sleep-log /usr/lib/systemd/system-sleep/oxp3-sleep-log

Kernel cmdline: **no change is required.** If you want the state choice to be explicit and immune to a
firmware default flip, the one optional argument is

    sudo rpm-ostree kargs --append=mem_sleep_default=s2idle        # takes effect after a reboot

## 5. Measuring

    tools/sleep-power-measure.sh audit                     # configuration + every armed wake source
    sudo tools/sleep-power-measure.sh suspend -w 600       # 10 min of real s2idle, then the numbers
    tools/sleep-power-measure.sh run -w 600                # 10 min in the current (awake) state
    tools/sleep-power-measure.sh snapshot                  # one line, for your own loop

`run` and `suspend` report: battery Wh before/after, the %/h rate normalised to the full pack, the
average power in W, the kernel's own suspended-time counter, RAPL SOC power (root) or our 1 s
`package-0` average, package temperature before/after/max, and a PASS/WARN/FAIL verdict against
`PASS_PCT_PER_H` (default 1.0) and `FAIL_PCT_PER_H` (default 2.0).

How to take a clean baseline (numbers on AC are meaningless and the tool says so):

1. Unplug, let it sit 2 minutes so the fuel gauge settles.
2. `tools/sleep-power-measure.sh run -w 600` with the screen off - that is the "awake, idle" number.
3. `sudo tools/sleep-power-measure.sh suspend -w 600` - that is the s2idle number.
4. Compare against: **< 1 %/h = 0.88 W = healthy s2idle**; 1-2 %/h = nothing else is wrong but
   something still wakes; > 2 %/h = the machine is effectively awake (re-check `suspend_stats` and the
   `oxp3-sleep` journal lines before blaming the kernel).

Recorded here only as a reference point, not as a baseline: awake idle on 2026-11-08 at 23:50 with four
agent workloads running was 7.8 W package / 56 °C, i.e. the machine was very much awake.

## 6. Validation checklist

    # 1. a suspend really happened, and how long the hardware slept
    cat /sys/power/suspend_stats/success /sys/power/suspend_stats/last_hw_sleep
    journalctl -k -b | grep -E "PM: suspend entry|PM: suspend exit"
    journalctl -t oxp3-sleep -b            # per-resume summary + the wake source that fired

    # 2. did the package reach S0ix (root only)
    sudo cat /sys/kernel/debug/pmc_core/slp_s0_residency_usec    # must grow in s2idle

    # 3. only these may wake it
    awk '$3=="*enabled"' /proc/acpi/wakeup        # expect LID0 PWRB SLPB AWAC (+ XHCI/TXHC if opted in)
    tools/sleep-power-measure.sh audit | sed -n '/device wakeup/,/inhibitors/p'

    # 4. the drain
    sudo tools/sleep-power-measure.sh suspend -w 3600          # one hour; want <= 1 %/h

    # 5. if it woke up by itself, the wake source is named:
    journalctl -t oxp3-sleep -b | grep "wake source"

## 7. Rollback

    sudo systemctl disable --now oxp3-sleep-wakeup.service oxp3-sleep-pre.service oxp3-sleep-log.service
    sudo rm -f /etc/systemd/system/oxp3-sleep-wakeup.service /etc/systemd/system/oxp3-sleep-pre.service \
               /etc/systemd/system/oxp3-sleep-log.service /etc/udev/rules.d/99-oxp3-sleep-wakeup.rules \
               /usr/local/libexec/oxp3-sleep-wakeup /usr/local/libexec/oxp3-sleep-log
    sudo udevadm control --reload && sudo systemctl daemon-reload

The wake-source policy is applied at boot only, so a plain reboot restores the firmware defaults anyway.
Kernel arguments (if the optional one was added): `sudo rpm-ostree kargs --delete=mem_sleep_default=s2idle`.
KDE: `kwriteconfig6 --file powerdevilrc --group Battery --key LidAction --delete`.

## 8. Open points

1. **Which suspend state KDE requests.** `powerdevilrc` has no `LidAction` recorded, so the effective
   action is an implicit default. R1 sets it explicitly; if the first test still shows no
   `PM: suspend entry`, test logind directly (`systemctl suspend`) and check `handle-lid-switch` in
   `systemctl show systemd-logind`.
2. **`max_hw_sleep` = 524 s with `success` = 0.** Inconsistent with every other counter and with the
   log. Re-read after the first controlled suspend: if `success` increments and `max_hw_sleep` does not
   move, it is a stale/derived value and can be ignored; if it moves again without a success, that is a
   kernel bug worth a report.
3. **Per-link L1 substates are not visible unprivileged** (`/sys/bus/pci/devices/0000:00:06.0/link/`
   exists but exposes no `*_aspm` attributes here, and `lspci -vv` needs root). Needed to judge open
   point 5:
   `sudo lspci -vv -s 00:06.0 -s 01:00.0 | grep -E "LnkCap|LnkCtl|L1Sub|LTR"`.
4. **`deep` is offered but untested.** If s2idle drain stays above 1 %/h after R1-R3, a one-shot
   experiment (`sudo sh -c 'echo deep > /sys/power/mem_sleep'` then a `rtcwake` suspend) would show
   whether the firmware's S3 path is real on this box. Expect resume hangs.
5. **The NVMe's `simple suspend` quirk** means the firmware owns the SSD's D3 transition. If S0ix
   residency does not grow while the machine is suspended, the storage link is the first suspect;
   `pcie_aspm.policy=powersave` (R5) is the cheap test, and `sudo cat /sys/kernel/debug/pmc_core/ltr_show`
   lists the blockers directly.
6. **Hibernate is unavailable** under `integrity` lockdown (no `disk` state), so the classic
   "sleep then hibernate after 3 h" mitigation cannot be used here. If it is ever wanted, it needs a
   security-level or lockdown change in the BIOS/firmware, not a Linux-side config.
7. **`iwlwifi` runs at 59 °C** while the rest of the machine is at 27.8 °C case temperature. Not a
   suspend issue, but if awake idle drain matters later, WoWLAN/power-save on `wlo1` (already `on`) and
   the CNVi runtime PM state are where to look.
