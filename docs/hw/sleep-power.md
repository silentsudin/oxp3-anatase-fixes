# Suspend / S0ix power on the OneXPlayer 3 (s2idle, near-zero drain)

Target: closing the lid should cost well under **1 %/h** of the 87.7 Wh pack, and the machine should be
cold to the touch when it comes back out of a bag - not merely "screen off". This file records what the
machine actually does today, what we changed, how to prove it and how to undo it.

Everything here was measured on 2026-11-08/09 on kernel `7.2.7-an28.fc45.x86_64` (Anatase, 60 retained
boots). Nothing in this document has been applied to the host: the `sudo` lines below are the exact
commands, run them in the order given.

**2026-11-09 correction (trigger audit, section 2.5).** The sentence "this device has never suspended"
from the first version of this file was wrong, and the command that produced it was the reason:
`journalctl -k` is `--dmesg`, which implies `-b`, so it only ever looked at the *current* boot. Across
the retained journal the machine has suspended **nine times** (`PM: suspend entry (s2idle)` on Oct 12,
13, 16 and 19). What is true, and is the actual problem, is narrower and nastier: **in the retained
November boots nothing ever asks for suspend**, and two of the three things that could ask are wired
or blocked in a way that cannot work on this machine. Section 2.5 has the source-level evidence, the
per-path table and the policy this repo now ships so that sleep no longer depends on a desktop session
noticing a key.

- Measurement tool: [`tools/sleep-power-measure.sh`](../../tools/sleep-power-measure.sh)
- Wake-source policy: [`files/oxp3-sleep-wakeup`](../../files/oxp3-sleep-wakeup) +
  [`files/oxp3-sleep-wakeup.service`](../../files/oxp3-sleep-wakeup.service) +
  [`files/99-oxp3-sleep-wakeup.rules`](../../files/99-oxp3-sleep-wakeup.rules)
- What-woke-it logging: [`files/oxp3-sleep-log`](../../files/oxp3-sleep-log) with
  `oxp3-sleep-pre.service` / `oxp3-sleep-log.service`
- Trigger policy (new, 2026-11-09): [`files/logind-oxp3.conf`](../../files/logind-oxp3.conf) ->
  `/etc/systemd/logind.conf.d/60-oxp3-sleep.conf` and
  [`files/49-oxp3-suspend.rules`](../../files/49-oxp3-suspend.rules) ->
  `/etc/polkit-1/rules.d/49-oxp3-suspend.rules`, installed by `tools/oxp3-hw-install.sh --sleep`,
  reported by `sleep.trigger` (section 2.5)

## TL;DR

1. **Suspend works on this machine; nothing asks for it.** `/sys/power/suspend_stats/success` is 0
   *this boot* and the November boots contain no `PM: suspend entry` - but the same journal has nine
   real `PM: suspend entry (s2idle)` cycles between Oct 12 and Oct 19, all of them from
   `systemd-logind` after Steam (flatpak, over `xdg-dbus-proxy`) called `login1.Suspend`. So the
   blocker is the *trigger*, not the platform: in the November boots no actor requested sleep, because
   (a) logind has no `Handle*` policy at all and its compiled default for the power key is `poweroff`,
   (b) KDE PowerDevil holds a `block` inhibitor on `handle-power-key`/`handle-lid-switch` so logind is
   not allowed to act on those events, and (c) HandheldDaemon's own power/lid handler stands down
   unless a Steam Gamepad session is running, and even then it only forwards the press to Steam
   (section 2.5). The two hours after a "lid close" are then spent as an awake desktop (7.8 W package
   power), which is the warm-to-touch, flat-battery report.
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
| history | `journalctl --list-boots` = 60 boots; `journalctl -k \| grep -c "PM: suspend entry"` = **0**, but `-k` implies `-b` so that only counts this boot; `journalctl -b all -k \| grep -c "PM: suspend entry"` = **9** (Oct 12-19) | s2idle entry works; nothing in the November boots asks for it (see §2.5) |
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

### 2.1 In the current boots nothing asks, so there is nothing to be "inefficient" about yet

Zero `PM: suspend entry` this boot, `success` = 0, and no `failed_*` counters either - so the kernel is
not rejecting suspends, it is never being *asked*. What decides that on this machine is a combination
of KDE, logind's own configuration and HandheldDaemon; section 2.5 takes it apart. In short: KDE
PowerDevil holds the `block` inhibitor for `handle-power-key`/`handle-suspend-key`/`handle-lid-switch`
(so logind must not act on those), `powerdevilrc` records no `LidAction`/`PowerButtonAction`, and
handheldd only handles the key while a Steam Gamepad session exists. The Steam log in the same boot
shows the other half of the problem:

    16:16:53 steam[...]: [Backend]: Preventing machine to sleep

That is a userspace idle inhibitor; it does not block a lid-triggered suspend, but it does block the
"idle desktop after 15 minutes" path, so both paths have to be checked. One live fact from
PowerDevil 6.7.5 softens the KDE part of the story: the *effective* lid policy it applies is
`lidAction = 1` (Sleep) with `triggersLidAction = true` (read over D-Bus on 2026-11-09), i.e. a lid
event, if one is delivered while PowerDevil runs, should turn into a logind suspend. No
`systemd-logind: Power key pressed short` line exists in any retained November boot, so the lid/key
paths were simply never exercised there.

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

### 2.5 Why a suspend request never reaches the kernel (2026-11-09 trigger audit)

The previous version of this file concluded "the machine has never suspended" from
`journalctl -k | grep -c "PM: suspend"`. `journalctl -k` is `--dmesg`, and `--dmesg` implies `-b`: the
count was always 0 because it only ever looked at the boot the command ran in. Re-run across the whole
retained journal and the picture inverts:

    journalctl -b all -k --no-pager | grep "PM: suspend entry"
    Oct 12 11:00:11  PM: suspend entry (s2idle)
    Oct 12 11:13:52  PM: suspend entry (s2idle)
    Oct 12 22:01:18  PM: suspend entry (s2idle)
    Oct 13 01:36:02  PM: suspend entry (s2idle)
    Oct 13 13:55:40  PM: suspend entry (s2idle)
    Oct 13 20:43:23  PM: suspend entry (s2idle)
    Oct 13 22:16:10  PM: suspend entry (s2idle)
    Oct 16 23:29:30  PM: suspend entry (s2idle)
    Oct 19 22:57:45  PM: suspend entry (s2idle)

So s2idle entry works. What is missing in the November boots is an *actor*. The Oct 16 chain is the
only complete one in the journal and it names every hop:

    Oct 16 23:29:28 systemd-logind: Power key pressed short.
    Oct 16 23:29:28 hhd[1184]: PBTN INFO Executing short power button press.
    Oct 16 23:29:28 hhd[1184]: PBTN INFO S3 sleep supported, sleep button will work.
    Oct 16 23:29:28 hhd[1184]: '/app/bin/steam', '-ifrunning', 'steam://shortpowerpress']`
    Oct 16 23:29:30 systemd-logind: suspend requested from client PID 2150 ('xdg-dbus-proxy') (unit user@1000.service)...
    Oct 16 23:29:30 systemd-logind: The system will suspend now!
    Oct 16 23:29:30 kernel: PM: suspend entry (s2idle)

Read carefully: logind *logged* the key press but did not act on it. At audit time a `block`
inhibitor on `handle-power-key` was held by PowerDevil (section 2.5.4), which is the only mechanism that
produces exactly that pair of lines - but the Oct 16 journal does not record the inhibitor list of that
moment, so treat "PowerDevil swallowed it" as the consistent explanation, not as a measured fact of
that boot. HandheldDaemon forwarded the press to Steam, and **Steam** (flatpak, behind
`xdg-dbus-proxy`) made the `login1.Suspend` call. Suspend depended on Steam's own idea of what a
short power press means.

#### 2.5.1 HandheldDaemon 4.1.12: what it does with the power key and the lid

Package `hhd-4.1.12+git.154.geca609b0f54a-1.fc45.ludos.noarch`, source at
`/usr/lib/python3.15/site-packages/hhd/plugins/powerbutton/base.py` (read on 2026-11-09, line numbers are
that version):

| line | code | what it means here |
|---|---|---|
| 30 | `INHIBIT_WHAT = "handle-power-key:handle-lid-switch"` | hhd claims the power key **and** the lid from logind |
| 89-106 | `def acquire(self)` ... `inhibit(INHIBIT_WHAT, "HandheldDaemon", ..., "block")` then `logger.info("Inhibited logind power button and lid switch handling.")` | a logind **block** inhibitor, held for as long as its fd stays open. The journal proves it fires: `Nov 08 16:15:31 hhd: PBTN INFO Inhibited logind power button and lid switch handling.` |
| 341-371 | `def power_button_run(...)`: `if not is_steam_gamepad_running(): inhibitor.release(); close_power_devices(devices); wait` | **the handler is inert unless a Steam Gamepad session is running.** On the KDE desktop it releases the inhibitor and stops watching, which is exactly the state `systemd-inhibit --list` shows right now (hhd holds only its HDMI-CEC `sleep` *delay*). |
| 371 | `if not inhibitor.active and not inhibitor.acquire(): close_power_devices(...)` | if it cannot take the inhibitor, it stops handling at all rather than fight logind |
| 311-322 | `def run_steam_shortpress()`: `return run_steam_command("steam://shortpowerpress")` when `supports_sleep()`, else `emergency_hibernate(shutdown=False)` | a short press is **forwarded to Steam**; hhd never suspends itself on that path |
| 327-338 | `def execute_power_action()`: `worked = run_steam_shortpress()` ... `if not worked: logger.error("Power button action did not work. Calling 'systemctl suspend'"); os.system("systemctl suspend")` | the only place hhd suspends directly is the **fallback**, and only if Steam returns failure |
| 395-398 | `if is_lid_close_event(event) and external_display_connected(): continue` | a lid close with any DP/HDMI display connected is ignored on purpose |

The two helpers behind that (`plugins/power/power.py`, `plugins/plugin.py`):

- `supports_sleep()` (`power.py:216-253`) returns True if `mem_sleep` contains `deep`, else it reads bit
  21 of the FADT to decide whether s2idle exists. On this box it takes the first branch and logs
  `S3 sleep supported` (the message is misleading - `deep` being *offered* is enough). If it had
  returned False, hhd's "sleep button" would `systemctl hibernate` (`power.py:267`) and fail: `/sys/power/disk`
  is `[disabled]` under the integrity lockdown.
- `is_steam_gamepad_running()` is just `_running` (`plugin.py:400`) - the flag the Steam plugin sets when a
  Steam Gamepad/virtual device is up, i.e. in Game Mode, not in an ordinary desktop Steam session.
- `run_steam_command()` (`plugin.py:405-430`) resolves the running Steam, and for a flatpak does
  `flatpak enter <instance> /app/bin/steam -ifrunning <command>`; that is the `/app/bin/steam` line in the
  Oct 16 journal.

Failure modes this creates on this device, in order of likelihood: **(1)** hhd is silent because no Steam
Gamepad is running (desktop); **(2)** Steam receives `steam://shortpowerpress` but its configured power
action is not Sleep (SteamOS semantics do not apply to a Fedora flatpak); **(3)** the flatpak
`-ifrunning` handoff fails, hhd logs "Power button action did not work" and *then* suspends through
`systemctl suspend` - which would work, so if you see no `PM: suspend entry` you can rule this out.

#### 2.5.2 The three paths a sleep action can take, and what each one needs

| how the user asks | who must act | exact call | state on this host (2026-11-09) |
|---|---|---|---|
| Steam Gamemode power menu -> "Sleep" | Steam (client), then logind | `org.freedesktop.login1.Manager.Suspend` on the system bus, polkit action `org.freedesktop.login1.suspend` | permitted (`allow_active=yes`, `pkcheck` exit 0) and proven by the Oct 16 journal; not exercised in November because Steam was not in Game Mode |
| KDE application launcher -> Sleep | PowerDevil -> logind | PowerDevil performs the suspend through logind (`org.kde.Solid.PowerManagement.Actions.SuspendSession` is its action object, no suspend method of its own); polkit as above | permitted; PowerDevil 6.7.5 answers `lidAction = 1` (Sleep), `triggersLidAction = true` |
| power button | whoever owns `handle-power-key` | `login1.Suspend` (PowerDevil) or `HandlePowerKey=` (logind, *only* if no `block` on `handle-power-key`) | **broken twice**: PowerDevil holds the `block` inhibitor, and logind's compiled default would be `poweroff`, not `suspend` - so with PowerDevil gone the key shuts the machine down instead of sleeping |
| lid close | PowerDevil (or logind without a `block`) | `login1.Suspend` | armed in PowerDevil but never observed in the retained November boots; no `Power key pressed short` line either, so the case never happened there |
| `systemctl suspend` / a script as the user | logind | `login1.Suspend`, polkit as above | works, and is the shortest proof (section 6) |
| hhd fallback (Gamemode, Steam unreachable) | hhd (root) -> systemd | `systemctl suspend` | works; only reached when Steam returns failure |

#### 2.5.3 What logind is actually configured to do: nothing (the `poweroff` trap)

    systemd-analyze cat-config systemd/logind.conf | grep -v '^\s*#' | grep -v '^\s*$'
    [Login]

There is **no** `/etc/systemd/logind.conf`, no `logind.conf.d` directory and therefore not a single
`Handle*` setting on this host: every one of them is systemd's compiled default, and systemd's default
for `HandlePowerKey` is **`poweroff`** (`HandleSuspendKey`, `HandleLidSwitch` default to `suspend`,
`HandleLidSwitchDocked` to `ignore`, `HandlePowerKeyLongPress` to `ignore`). Two consequences:

- a power key that reaches logind **shuts the machine down** instead of sleeping it. That is a data-loss
  bug waiting for the day PowerDevil is not running (another desktop, a rescue session, a Gamescope-only
  session);
- there is no configuration to inspect when suspend "does nothing", which is why the audit had to start
  from the journal rather than from `/etc`.

We verified the compiled defaults through the same `cat-config` output: systemd prints them as
`#HandlePowerKey=poweroff` before the drop-in section, and the verify parser reads both layers
(section 2.5.6).

#### 2.5.4 Inhibitors: what `block` on `handle-*` does and does not do

`systemd-inhibit --list` on 2026-11-09 showed 9 inhibitors: one `block` and eight `delay`.

| who | what | mode | does it stop a suspend? |
|---|---|---|---|
| PowerDevil (org_kde_powerde) | `handle-power-key:handle-suspend-key:handle-hibernate-key:handle-lid-switch` | block | **No.** It tells logind "do not act on these *events*": logind logs `Power key pressed short.` and stops there. It does not veto `Manager.Suspend` calls - which is why the Oct 16 Steam path worked while this inhibitor was held. |
| HandheldDaemon | `sleep` (`Restore HDMI-CEC state before sleep`) | delay | No (delays the transition, up to `InhibitDelayMaxSec`) |
| kwin_wayland | `sleep` (lock screen first) | delay | No |
| UPower, NetworkManager, ModemManager, rtkit, deepseek-harness | `sleep` | delay | No |
| *any* `block` on `sleep` | e.g. a firmware updater | block | **Yes.** This is the only kind of inhibitor that makes `systemctl suspend`/`login1.Suspend` refuse with "Operation inhibited". `sleep.trigger` fails when it sees one. |

That is the semantics the previous version of this file got wrong: `handle-lid-switch` being blocked by
KDE does not explain a suspend that never happens, because KDE is *supposed* to make the call instead.
And a `block` on `sleep` - the kind that would actually refuse - was **not present at any point of the
audit**.

#### 2.5.5 The polkit / D-Bus side is not the blocker (but it is a trap in Gamemode)

    pkaction --action-id org.freedesktop.login1.suspend --verbose
      implicit any:      auth_admin_keep
      implicit inactive: auth_admin_keep
      implicit active:   yes
    pkcheck --action-id org.freedesktop.login1.suspend --process $$                # exit 0 (yes)
    pkcheck --action-id org.freedesktop.login1.suspend-multiple-sessions --process $$  # exit 0 (yes)
    pkcheck --action-id org.freedesktop.login1.hibernate --process $$             # exit 0 (yes)
    pkcheck --action-id org.freedesktop.login1.suspend-ignore-inhibit --process $$ # exit 2 (challenge)

systemd's own policy lets an **active local session** suspend without a prompt, and this session
(`plasmalogin`, `Class=user`, `Active=yes`, seat0, tty7) is exactly that. So permission is *not* why
sleep never happened. It becomes a trap the moment the request comes from a context that is not the
active session - and the Gamemode session is the interesting case:

- `desktop`/`XDG_CURRENT_DESKTOP=gamescope` (`/usr/share/wayland-sessions/gamemode.desktop` ->
  `/usr/bin/anatase-gamemode-session`); only Plasma starts
  `polkit-kde-authentication-agent-1`, so in Gamemode there is **no authentication agent at all**.
- If a call ever needs authentication there, it fails instantly with "Interactive authentication
  required" and the kernel is never asked. That is silent from the couch: nothing appears on screen.

Hence the polkit rule in section 2.5.6: it converts the challenge into a silent yes for a physically
present `wheel` user, so the request path no longer depends on an agent existing. It deliberately does
**not** grant `org.freedesktop.login1.suspend-ignore-inhibit`, which would skip `sleep` block
inhibitors as well (that action stays `auth_admin_keep`).

#### 2.5.6 The trigger policy shipped here

| file in the repo | installed as | what it fixes |
|---|---|---|
| [`files/logind-oxp3.conf`](../../files/logind-oxp3.conf) | `/etc/systemd/logind.conf.d/60-oxp3-sleep.conf` | `HandlePowerKey=suspend`, `HandleSuspendKey=suspend`, lid suspend on battery/AC, docked lid ignored, long-press still `poweroff`; kills the `poweroff` trap of 2.5.3 |
| [`files/49-oxp3-suspend.rules`](../../files/49-oxp3-suspend.rules) | `/etc/polkit-1/rules.d/49-oxp3-suspend.rules` | `login1.suspend`/`-multiple-sessions`/`hibernate` for a local, active `wheel` session; makes Gamemode (and any agent-less context) work; never grants the inhibit bypass |

Install, prove, remove - section 4, 6 and 7. Neither file needs a desktop session, and neither one
changes the *decision* to sleep: they only make sure that when something asks, the ask is honoured.

What the drop-in does *not* fix: while PowerDevil runs it still owns `handle-power-key`, so logind's
`HandlePowerKey=suspend` only takes over when no desktop is claiming the key (a plain console, a
Gamescope-only session, a session where PowerDevil is not running). The KDE-side counterpart is a
Power Management setting, not a logind one - see R1.

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

**The KDE-side conflict, if there is one.** PowerDevil claims `handle-power-key`,
`handle-suspend-key`, `handle-hibernate-key` and `handle-lid-switch` with a `block` inhibitor for as
long as it runs (verified live: `systemd-inhibit --list`). It does that because Plasma *is* the thing
that handles those events; a `block` there is not a bug. The consequence is that the logind drop-in in
R1b only takes effect when no desktop is claiming the key. If the power key does nothing on the KDE
desktop, the setting to change is PowerDevil's, not logind's:

    # read what PowerDevil currently applies (it is not exposed over D-Bus for the power key,
    # so read the file and then the KCM):
    kreadconfig6 --file powerdevilrc --group AC --key PowerButtonAction      # empty = KDE default
    kreadconfig6 --file powerdevilrc --group Battery --key PowerButtonAction
    # the authoritative path is the KCM:
    #   System Settings > Power Management > Energy Saving > "When power button pressed" = Sleep
    #   (same page: "When laptop lid closed" = Sleep for AC and Battery)

The scripted equivalent for the lid half is in R1 above. For the power button, set the KCM value
instead of guessing the enum: the key lives in `[AC][SuspendAndShutdown]` / `[Battery][SuspendAndShutdown]`
next to `AutoSuspendAction` in the same file, but this host has never written it, so its effective
value is PowerDevil's default and the KCM is the only place that shows which that is (open point 9).
Either way, once PowerDevil is set to Sleep, the KDE path and the shipped logind policy agree, and it
stops mattering who wins the event.

### R1b - a trigger that does not need a desktop session (shipped here)

Sections 2.5.1-2.5.5 end with the same conclusion: every path that worked or could work runs through
`org.freedesktop.login1.Manager.Suspend`, and the *policy* for that call was undefined on this host
(logind had no configuration; the power key would `poweroff`; there is no polkit agent in Gamemode).
The two shipped files close that:

    files/logind-oxp3.conf        -> /etc/systemd/logind.conf.d/60-oxp3-sleep.conf
        HandlePowerKey=suspend, HandleSuspendKey=suspend, lid=suspend (AC+battery), docked lid=ignore,
        long-press power=poweroff. Logind itself will sleep on the keys whenever it is the one
        receiving them instead of shutting the machine down.

    files/49-oxp3-suspend.rules   -> /etc/polkit-1/rules.d/49-oxp3-suspend.rules
        login1.suspend / -multiple-sessions / hibernate -> YES for a local, active session of a
        `wheel` user; everything else falls through to the stock policy (inactive/remote still
        authenticates). Deliberately not granted: suspend-ignore-inhibit.

`tools/oxp3-hw-install.sh --sleep` installs both (files only; the installer prints the one privileged
follow-up and never restarts logind behind a running session). `tools/oxp3-verify.sh` reports the
result as `sleep.trigger`; nothing there writes, and `pkcheck` only asks for a verdict.

Proof that needs no desktop at all (run it as the user; you will be asked for nothing because the
action is `allow_active=yes`):

    systemctl suspend        # or: gdbus call --system --dest org.freedesktop.login1                              #       --object-path /org/freedesktop/login1                              #       --method org.freedesktop.login1.Manager.Suspend false
    # after wake:
    tools/oxp3-verify.sh --full        # sleep.stats: success > 0, last_hw_sleep > 0
    journalctl -b all -k | grep "PM: suspend entry"

If those two commands produce a `PM: suspend entry` and a growing `success` counter, the trigger is
solved and everything left in this file is about drain and about not being woken.

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
    # the trigger policy (section 2.5.6) - files only, no service is restarted here:
    sudo install -D -m 0644 files/logind-oxp3.conf \
                        /etc/systemd/logind.conf.d/60-oxp3-sleep.conf
    sudo install -D -m 0644 files/49-oxp3-suspend.rules \
                        /etc/polkit-1/rules.d/49-oxp3-suspend.rules
    # logind reads its drop-ins at start; polkitd re-reads rules.d by itself:
    sudo systemctl restart systemd-logind.service   # drops inhibitor locks, sessions survive
    systemd-analyze cat-config systemd/logind.conf | grep -A6 '^\[Login\]'   # must show suspend
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
    sudo tools/sleep-cycle-test.sh --cycles 3              # N cycles + wake attribution + verdict
                                                           # (see docs/SLEEP-TEST.md)
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

    # 0. the trigger path exists (read-only; no desktop session needed)
    tools/oxp3-verify.sh | grep sleep.trigger
    systemd-analyze cat-config systemd/logind.conf | grep -A6 '^\[Login\]'
    pkcheck --action-id org.freedesktop.login1.suspend --process $$ ; echo "pkcheck rc=$?"
    systemd-inhibit --list | grep -E "sleep|handle-"     # want no "block" on sleep
    sudo systemctl suspend                               # the one command that proves it
    # after it wakes: success must be >= 1 and last_hw_sleep must be > 0

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
    # the trigger policy (leaving it installed is harmless: it only makes sleep work):
    sudo rm -f /etc/systemd/logind.conf.d/60-oxp3-sleep.conf /etc/polkit-1/rules.d/49-oxp3-suspend.rules
    sudo systemctl restart systemd-logind.service   # drop-ins are read at start; polkitd re-reads rules
    sudo udevadm control --reload && sudo systemctl daemon-reload
    # or, the supported one-liner:
    sudo tools/oxp3-hw-install.sh --uninstall --sleep

The wake-source policy is applied at boot only, so a plain reboot restores the firmware defaults anyway.
Kernel arguments (if the optional one was added): `sudo rpm-ostree kargs --delete=mem_sleep_default=s2idle`.
KDE: `kwriteconfig6 --file powerdevilrc --group Battery --key LidAction --delete`.

## 8. Open points

1. **Which suspend state KDE requests.** `powerdevilrc` has no `LidAction` recorded, so the effective
   action is an implicit default - read live, PowerDevil 6.7.5 applies `lidAction = 1` (Sleep) and
   `triggersLidAction = true`. R1 still sets it explicitly; if the first test shows no
   `PM: suspend entry`, test logind directly (`sudo systemctl suspend`, section 6 step 0) and compare
   `handle-lid-switch` against `systemd-inhibit --list`.
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
8. **What `steam://shortpowerpress` actually does on this Steam build.** The Oct 16 journal proves Steam
   ended up calling `login1.Suspend` after hhd forwarded the press, but not *which* Steam setting did it:
   Steam's "power button" action is a SteamOS concept and this is a Fedora flatpak. Check it in
   Gamemode (Steam > Settings > Power) and record it here; if it is set to anything but Sleep, the
   working path in Gamemode is hhd's `systemctl suspend` fallback (which only fires when Steam returns
   failure) or an explicit suspend from the power menu. Not measurable from this session, which is not
   in Gamemode.
9. **PowerDevil's default power-button action is not readable remotely.** `powerdevilrc` has no
   `PowerButtonAction` and PowerDevil exposes no property for it (only `lidAction`/`triggersLidAction`
   on `/org/kde/Solid/PowerManagement/Actions/HandleButtonEvents`; `SuspendSession` has no methods). The
   KCM (System Settings > Power Management > Energy Saving) is the authoritative view. Until it is read
   there, treat "the power button does nothing on the desktop" as unexplained rather than as a logind
   problem - and it is exactly the case the shipped drop-in cannot fix while PowerDevil holds
   `handle-power-key`.
10. **hhd 4.1.12 line numbers are version-pinned.** Section 2.5.1 quotes that package revision;
    `plugins/powerbutton/base.py` moves between releases. Re-read it before blaming hhd after an update,
    and note that hhd only acts while a Steam Gamepad session exists - which is by design upstream
    (`is_steam_gamepad_running()`), not a misconfiguration here.
11. **`pkcheck`/polkit is verified for the active session only.** The rule and the stock `allow_active=yes`
    both say yes here. The inactive/remote case (a script over ssh suspending the bag machine) still
    needs `auth_admin_keep` by design and is untested - do not widen the rule to `action.id`-only
    matching to "fix" it.
