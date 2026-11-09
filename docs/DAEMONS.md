# Daemon lifecycle contract (suspend, resume, hotplug)

The three long-running OXP3 daemons all sit on a device node that can change
identity while they run:

| daemon | source | sink | unit |
|---|---|---|---|
| `tools/oxp3-homebtn.py` | vendor hidraw `1a86:fe00`, usage page `0xFF00` | uinput keyboard (`KEY_HOMEPAGE`) or `--exec` | `oxp3-homebtn.service` (user) |
| `tools/oxp3-rgbd.py` | same hidraw node (write-only), plus hhd journal/`hhdctl` | RGB frames on the same node | `oxp3-rgbd.service` (user) |
| `tools/oxp3-voltap/oxp3-voltap` | evdev device carrying the volume rocker (default: hhd's `Handheld Daemon Volume Keyboard`) | uinput shim (`OXP3 volume tap shim`) | `oxp3-voltap.service` (root) |

A suspend with USB re-enumeration, a USB reset, a hhd restart or an unplug
invalidates the fd; the kernel does not always tell us with a friendly error.
This document is the contract the daemons implement, the journal lines that
prove it, and the failure modes we deliberately leave alone.

The shared primitives live in [`tools/oxp3_lifecycle.py`](../tools/oxp3_lifecycle.py):
`Backoff`, `DeviceGone`, `error_reason`/`is_device_error`, `ResumeDetector`,
`Interval`, and the identity-based discovery (`hidraw_identity`/`find_hidraw`,
`input_identity`/`find_input_event`). They are stdlib-only and unit-tested in
`tests/unit/test_lifecycle.py`.

---

## 1. Contract per daemon

### 1.1 Common rules

1. **Identity, never node number.** A device is rediscovered by
   `vendor:product` plus its stable name/physical path:
   * hidraw: `HID_ID` (`0003:00001A86:0000FE00`), `HID_NAME`, `HID_PHYS`
     from `/sys/class/hidraw/hidrawN/device/uevent`;
   * input: `name`, `phys`, `id/vendor`, `id/product` from
     `/sys/class/input/eventN/device/`.

   The `N` in `hidrawN`/`eventN` is used only as the *result* of a fresh
   scan. It is never stored as the identity and never used to decide "the
   same device". A configured `--device /dev/input/event23` is a first hint:
   if that number now belongs to different hardware, the daemon refuses it
   and searches for the remembered identity.

2. **A dead fd is a device loss, not a crash.** `EOF` (0-byte read),
   `ENODEV`, `ENXIO`, `EBADF`, `EPIPE`, `EIO`, `ESHUTDOWN` and a failed
   `fstat` all mean "close and reopen". Nothing is retried on a known-dead
   fd, and no loop spins on it.

3. **Bounded, logged backoff.** 0.5, 1, 2, 4, 8, 15, 30, 30 s … The ladder
   resets after any success. Retrying never stops (a docked device may come
   back an hour later), but the delay is capped so the journal and CPU stay
   quiet. Permanent errors (`EACCES`, `EPERM`, `EROFS`) are logged with the
   install hint and retried slowly rather than treated as a crash.

4. **The process does not exit on transient errors.** systemd
   `Restart=always` + `RestartSec` is the last line of defence, not the
   recovery mechanism. `lifecycle` errors are handled in-process, so normal
   resume/hotplug produces no restart and keeps counters (`reconnect #N`).

5. **Timers are monotonic.** Intervals and debounce deadlines use
   `CLOCK_MONOTONIC`; `CLOCK_REALTIME` is used only for human-readable log
   timestamps. A suspend (or NTP step) therefore cannot make a watchdog fire
   immediately on wake or stall it for hours.

6. **Resume detection is rootless.** `CLOCK_BOOTTIME - CLOCK_MONOTONIC`
   grows only while the system is suspended. The baseline is taken on the
   first sample, the jump is logged once, and the daemon then re-applies its
   state. No D-Bus, no logind policy, no root.

7. **uinput is disposable.** If the shim's `write` fails (uinput module
   reload, resume, `/dev/uinput` access lost), the daemon destroys and
   recreates it once, re-sends only what still needs to be down (nothing:
   the old device died with its keys), and keeps running if recreation
   fails. A compositor that ignores the new device is outside our control
   (see §5).

### 1.2 `oxp3-homebtn.py` (hidraw reader → uinput)

* **Reconnects to:** `/dev/hidrawN` of `1a86:fe00` without an input child,
  matching the captured identity (vid/pid/name/phys). Refuses a node whose
  identity changed even if the node number is the same.
* **Triggers:** read `OSError`/EOF, `select()` error, `os.fstat()` failure,
  or a 5 s idle-health check that finds the node gone / re-assigned.
* **Watchdog:** every `--health` seconds (default 5): verifies the open fd
  and re-reads the sysfs identity behind the recorded node path; logs a
  heartbeat at most every 5 min. `--health 0` disables it.
* **uinput:** created lazily; on `ENODEV`/`EINVAL`/`EBADF` from `write` it is
  recreated once and the pressed set is cleared (no phantom release).
* **Happy path unchanged:** same frame parsing, same `KEY_HOMEPAGE` /
  `--exec` behaviour, same 0.25 s select timeout.

### 1.3 `oxp3-rgbd.py` (state keeper)

* **Reconnects to:** the vendor hidraw node, freshly discovered per apply
  (the daemon holds **no** long-lived fd, so a stale fd cannot exist). On
  resume (`CLOCK_BOOTTIME` jump) it re-applies after a 2 s grace for HHD.
* **Triggers:** device presence transitions, hhd journal frames, `hhdctl`
  mode changes, config changes, hhd settle bursts, the 30 s watchdog, and
  the retry deadline after a failed open/write.
* **Timers:** the watchdog and the 0.5 s/3 s debounce are
  `CLOCK_MONOTONIC` intervals; an hour of suspend produces one apply on
  wake, not a burst.
* **Watcher threads** (`device`, `resume`, `hhd_poll`, `journal`) are
  supervised: an exception is logged and the thread is restarted; the main
  loop also restarts a dead thread and logs `watchdog: watcher thread …`.
* **No exclusive open:** HHD and the daemon still share the node.

### 1.4 `oxp3-voltap` (grabbed evdev → uinput)

* **Reconnects to:** the configured/auto-selected input device by
  name/phys/vendor/product. With an explicit `--device`, the remembered
  identity is used to find the device at its new `eventN`.
* **Triggers:** `select()`/read errors (`ENODEV`, `EBADF`), a device that
  disappears from the inventory, or a 10 s idle-health check that finds the
  sysfs identity changed.
* **Loss handling:** releases every key it thinks is down on the *old*
  uinput device, drops the grab, then rescans with backoff. The tapper's
  anti-bounce state is preserved across the drop (it is time-based, not
  fd-based).
* **uinput:** recreated on write failure, then the same pair is retried once
  so a lost release cannot leave the mixer running.

### 1.5 Units

| unit | restart | notes |
|---|---|---|
| `oxp3-homebtn.service` | `Restart=always`, `RestartSec=2`, `StartLimitIntervalSec=0` | user unit; no sandbox that would block hidraw/uinput or `--exec` children |
| `oxp3-rgbd.service` | `Restart=always`, `RestartSec=3`, `StartLimitIntervalSec=0` | `ConditionPathExists=%h/.config/oxp3-rgb.toml` is intentional (no config = no work) |
| `oxp3-voltap.service` | `Restart=always`, `RestartSec=2`, `TimeoutStopSec=10`, `StartLimitIntervalSec=0`, `After=hhd.service dev-uinput.device` | `ConditionPathExists=/dev/uinput` was removed: a condition failure leaves the unit dead with no restart, while the daemon can retry until uinput appears |

Rationale: `Restart=on-failure` masks a daemon that exits 0 after a
transient error, and the default start rate limit (5 starts / 10 s) can turn
a crash loop into a silently dead unit. Both are disabled; the in-process
retry makes restarts rare anyway. `systemd-analyze verify` passes for all
three (`make units`).

---

## 2. Journal vocabulary (health proof)

Every lifecycle transition uses one of these prefixes; the verify checks and
the tests parse them.

| line | meaning |
|---|---|
| `listening on /dev/hidrawN (identity …)` | homebtn opened the vendor node (repeated on every reopen) |
| `grabbed /dev/input/eventN (… phys=… id=…)` | voltap holds the device (existing format kept) |
| `applied N frame(s) to /dev/hidrawN (why)` | rgbd wrote the configured state |
| `device lost: <reason>; …` | fd/node gone (EOF, ENODEV, EBADF, select, identity change) |
| `retrying in X.Xs (attempt N)` | the current backoff delay |
| `reconnect #N to <node> (identity …)` | a successful reopen after at least one loss |
| `uinput: recreated after <reason>` / `uinput: unavailable (…)` | shim lifecycle |
| `watchdog: listening on …` / `watchdog: N grabbed device(s) live` | idle-health heartbeat |
| `watchdog: watcher thread <name> died` / `watcher … failed` | supervision |
| `resume detected: X.Xs of suspended time` | BOOTTIME jump |

Examples (one device reset):

```
oxp3-homebtn: device lost: ENODEV (No such device); reopening by identity
oxp3-homebtn: device lost: 1a86:fe00 vendor HID interface (usage page 0xFF00) not present; retrying in 1.0s (attempt 2)
oxp3-homebtn: listening on /dev/hidraw9 (identity 1a86:fe00 name='ONEXPLAYER 3' phys=usb-…-3/input2)
oxp3-homebtn: reconnect #1 to /dev/hidraw9 (identity 1a86:fe00 …)
```

---

## 3. Failure modes we deliberately do not handle

1. **A wedged Python process** (deadlock, GIL starvation, `D`-state in a
   syscall) is not detected. The health watchdog checks the device, not
   thread progress; systemd's `Restart=always` only helps if the process
   exits. A future `WatchdogSec=` + `sd_notify` would cover it; our daemons
   do not speak the notify protocol.
2. **A device that never re-enumerates** (firmware crash, hub power loss)
   cannot be recovered in userspace. We log the retry ladder forever and
   never fake a device; the fix is a physical replug or `usb_reset`.
3. **HHD as a competing writer.** `oxp3-rgbd` cannot prevent HHD from
   overwriting frames (hidraw is not exclusive). The journal watcher,
   polling fallback and watchdog make the race short-lived; clean takeover
   is described in `docs/hw/rgb-coverage.md`.
4. **A compositor that ignores a recreated uinput device.** We recreate the
   shim correctly, but some compositors cache the old device until the
   session restarts. The journal shows the recreation; that is the limit of
   our evidence.
5. **Identity reuse by identical hardware.** Two identical 1a86:fe00
   interfaces with the same name and phys (not the case on this device)
   would be indistinguishable; the daemon picks the first matching node.
6. **Permissions changes at runtime.** A `chmod`/ACL change on the node or
   `/dev/uinput` produces `EACCES`; we log the install hint and retry
   slowly. We never change permissions ourselves.
7. **Suspend/resume of the *policy*.** We re-apply state after resume; we do
   not try to keep the machine awake, wake it, or fight logind/UPower
   inhibitors (see `docs/hw/sleep-power.md`).
8. **A config file deleted while rgbd runs.** The last parsed config stays
   active (`ConditionPathExists` only gates unit start); we do not stop
   writing lights when the user deletes the file.
9. **Node appearance is polled** (1 s device watcher / rescan), not watched
   with udev/netlink. This is a deliberate trade: no root, no libudev
   dependency, bounded wakeups. A reconnect may therefore lag a hotplug by
   up to one poll interval.
10. **No retry of `--exec` commands.** If the launched command fails,
    homebtn logs nothing about it (subprocess is fire-and-forget); that is
    existing behaviour.

---

## 4. Tests

| suite | what it proves |
|---|---|
| `tests/unit/test_lifecycle.py` | backoff ladder/cap/reset, errno classification, `sleep_interruptible`, resume detector (one report per jump, no report for a small step), monotonic `Interval` after an hour of suspend, identity parsing and discovery across renumbering (hidraw + input), numeric node ordering |
| `tests/unit/test_homebtn_lifecycle.py` | happy path press/release; EOF/ENODEV/EBADF/EPIPE → loss; select EBADF; open failures back off 0.5/1/2 s and reset; permission hint; recycled node identity; vanished node; uinput write failure recreation; uinput unavailable does not crash; reader classifies EAGAIN/EINTR/EOF |
| `tests/unit/test_rgbd_lifecycle.py` | simulated suspend (BOOTTIME +120 s, MONOTONIC +1 s) → one `resume detected` and one schedule; baseline after start; watchdog does not fire after +3600 s suspend; missing device → backoff → reappearance → `reconnect #1`; ENODEV write retry; `EACCES` is not a device loss; ladder cap; watcher crash restart; dead-thread restart; health-note rate limit |
| `tests/unit/test_voltap.py` | the tap state machine and the rewritten discovery/selection (owned by the vol-tap change) |

Run them all offline: `make unit` (or
`python3 -m unittest discover -s tests/unit -q`). No root, no device, no
network.

---

## 5. Validation

### 5.1 Automated (read-only, no root)

```sh
tools/oxp3-verify.sh            # adds life.fd and life.reconnect
tools/oxp3-verify.sh --json     # same checks, machine-readable
```

* `life.fd` — for every active daemon, reads `/proc/<pid>/fd` and reports
  whether it currently holds a live `hidraw`/`event`/`uinput` fd.
  `oxp3-voltap` (root) needs root to inspect its fds, so in the default run
  it degrades to `SKIP` with a hint; `oxp3-rgbd` opens the node per apply by
  design and is reported as such rather than failed.
* `life.reconnect` — parses this boot's journal for the vocabulary in §2:
  `PASS` when there is no loss, or when every loss has a later
  `reconnect #`/open line; `FAIL` when a `device lost:` was never followed by
  a successful open; `SKIP` when no lifecycle daemon ran this boot.
* `life.sim` — gated behind `--full`: runs the three lifecycle test modules
  above (the simulated suspend/resume, fake-fd reconnects and backoff) and
  reports the unittest exit code. Nothing on the host is touched.

The unit suite is also part of `make test` (`unit-verify` suite).

### 5.2 Manual suspend/resume (the real proof)

Prerequisite: the daemon(s) are installed and active; `journalctl -b` is
readable. No sudo is needed for the user units; the suspend itself may need
root (`systemctl suspend` usually works from the desktop session).

```sh
# 0. record the pre-suspend state
systemctl --user is-active oxp3-homebtn oxp3-rgbd
systemctl is-active oxp3-voltap
journalctl --user -u oxp3-homebtn -u oxp3-rgbd -b --no-pager | tail -5

# 1. suspend for ~30 s and wake (lid, power button, or:)
sudo systemctl suspend        # then press power to wake

# 2. the same PIDs are still there: no restart was needed
systemctl --user show -p MainPID --value oxp3-homebtn oxp3-rgbd
systemctl show -p MainPID --value oxp3-voltap

# 3. the resume path was taken and the device was reopened/reapplied
journalctl --user -u oxp3-rgbd -b --no-pager | grep -E 'resume detected|applied .*frame|device lost|reconnect #' | tail -5
journalctl --user -u oxp3-homebtn -b --no-pager | grep -E 'listening on|device lost|reconnect #' | tail -5
journalctl -u oxp3-voltap -b --no-pager | grep -E 'grabbed |device lost|reconnect #' | tail -5

# 4. the harness agrees
tools/oxp3-verify.sh | grep -E 'life\.(fd|reconnect)'
```

Acceptance: the processes keep their PID (systemd `NRestarts` unchanged —
`systemctl --user show -p NRestarts`), `resume detected` appears for rgbd,
and either no loss was logged or each loss is followed by a reopen. Pressing
the home button and both volume keys afterwards must behave exactly as
before the suspend.

If the device is re-enumerated with a new number on this kernel/firmware,
the journal additionally shows `reconnect #1`, which is the path the unit
tests simulate.

### 5.3 Manual hotplug (USB device)

Use a USB device that shows up as hidraw/input and is *not* the internal
MCU — e.g. a USB keyboard or gamepad, or the OXP3 dock if the MCU is
behind it. The internal `1a86:fe00` cannot be unplugged separately; for it,
use a USB reset instead (`sudo usb_modeswitch`/`usbreset` on the parent, or
suspend the machine with USB re-enumeration). The same observation applies.

```sh
# identify the parent (camera, dock, keyboard), then watch the daemons
udevadm monitor --udev --subsystem-match=hidraw --subsystem-match=input &
journalctl --user -u oxp3-homebtn -u oxp3-rgbd -f &
journalctl -u oxp3-voltap -f &

# unplug the device (or: echo 0 | sudo tee /sys/bus/usb/devices/<id>/authorized)
# replug it (or: echo 1 | sudo tee …/authorized)

# expected in the journal: "device lost: …" followed by
# "listening on …"/"grabbed …"/"applied …" and, if the node number changed,
# "reconnect #N". life.reconnect turns this into PASS/FAIL.
tools/oxp3-verify.sh | grep -E 'life\.(fd|reconnect)'
```

For `oxp3-voltap` the useful hotplug test is stopping/starting hhd, which
destroys and recreates its uinput device:

```sh
sudo systemctl restart hhd
journalctl -u oxp3-voltap -b --no-pager | grep -E 'releasing .*disappeared|grabbed |reconnect #' | tail -5
```

Acceptance: no restart, no key stuck down after the loss, the volume keys
still step once per press, and `life.reconnect` reports a recovered loss.

### 5.4 What to attach to a bug report

The output of `tools/oxp3-verify.sh --json` (or `life.*` rows), the
`journalctl -b` excerpts from §5.2/5.3, and `systemctl show -p NRestarts`
for the three units. That is enough to tell "device loss that recovered"
from "daemon that died and was restarted" from "daemon that never opened
the device".

---

## 6. Open points

1. `sd_notify`/`WatchdogSec` for a wedged-but-alive process (§3.1).
2. A udev/netlink watcher instead of the 1 s poll (would need libudev or a
   small netlink client; not worth root/dependency cost yet).
3. Whether a resume should trigger a *forced* re-grab in voltap even when
   the fd looks healthy (some ECs re-send state without closing the fd).
   Current behaviour relies on the 10 s identity watchdog.
4. `oxp3-rgbd`'s settle window still uses wall-clock waits for the HHD
   start-up bursts; after a suspend during that window the remaining bursts
   are re-applied back-to-back, which is harmless but noisy.
