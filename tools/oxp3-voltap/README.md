# oxp3-voltap

Normalises the ONEXPLAYER 3 volume rocker into exactly one tap per physical
press. Root cause, evidence, topology and the full validation story:
[`docs/hw/volume-buttons.md`](../../docs/hw/volume-buttons.md).

```
oxp3-voltap run       # daemon: discover, grab, emit clean taps (root)
oxp3-voltap check     # candidates, holders, would-grab + predicted exit code
oxp3-voltap explain   # per-candidate suitability (alias: --probe)
oxp3-voltap diagnose  # capture both candidate streams for N s, print a verdict
oxp3-voltap probe     # read-only: EV_KEY timing table + verdict (root)
oxp3-voltap status    # JSON status the daemon wrote (which device, why)
oxp3-voltap selftest  # create the uinput shim and emit two taps (root)
```

`check`/`explain`/`status` read `/proc/bus/input/devices`, `/sys/class/input`
and `/proc/*/fdinfo`, so they work **without root** for the static topology;
holder lines become complete only for the root daemon.

## Install

```bash
sudo tools/oxp3-voltap/install.sh install --hwdb
```

That installs `/usr/local/libexec/oxp3-voltap` (`/usr/local` is
`/var/usrlocal` on Anatase, so it survives OSTree updates),
`/etc/oxp3-voltap.conf`, `oxp3-voltap.service`, and the kernel force-release
hwdb file `84-oxp3-volume-keys-taps.hwdb`, then enables and starts the unit.

Rollback:

```bash
sudo tools/oxp3-voltap/install.sh uninstall
```

## Topology

hhd grabs the physical AT keyboard (`EVIOCGRAB`, one grab per device) and
republishes the translated volume keys on its own uinput device
`Handheld Daemon Volume Keyboard` (`phys-hhd-vbtn`). A second grab on the AT
keyboard fails with `EBUSY`, so the daemon:

1. **prefers the hhd virtual volume keyboard** when it exists;
2. falls back to the **physical AT keyboard** only when no hhd virtual device is
   selectable and nothing else holds it - then it proxies the whole keyboard
   (non-volume keys included, with `EV_REP` repeats) through its own shim;
3. if every grab is refused, logs one `DEGRADED:` line, writes the JSON status
   and keeps retrying. `--once` and `--degrade-exit-after N` expose the
   documented exit code (0 grabbed, 2 no device, 3 every grab refused).

## What it does

* discovers candidates by name/phys/capabilities, never by a hardcoded
  `eventN`; explicit `VOLTAP_DEVICES`/`--device` overrides the choice;
* detects a running hhd by process (`/proc/*/cmdline`) **and** by the presence
  of its virtual device (a uinput device only lives while its owner does);
* identifies the process that holds an EVIOCGRAB from `/proc/*/fdinfo`
  (`grab: 1` plus the `fd` symlink) before attempting the grab;
* drops `EV_KEY value == 2` for volume keys (kernel soft repeat), duplicate
  presses and contact bounce;
* emits one 30 ms down/up pair per press through `OXP3 volume tap shim`
  (`phys-oxp3-voltap`), which has no `EV_REP` for a volume-only topology;
* turns a release without a press into one tap (that is what hhd emits when the
  kernel already force-released the scancode, so the hhd source patch is not
  needed for the rocker to work);
* force-releases any key that somehow stays down (500 ms watchdog), releases
  everything on SIGTERM, and re-attaches within ~1 s when hhd recreates its
  device;
* keeps the force-release hwdb as defence in depth: it is what lets a second
  physical press happen at all.

Tuning lives in `/etc/oxp3-voltap.conf` (`VOLTAP_HOLD_MS`, `VOLTAP_GAP_MS`,
`VOLTAP_MAX_HOLD_MS`, `VOLTAP_ORPHAN_TAP`, `VOLTAP_PREFER`,
`VOLTAP_ALLOW_PHYSICAL`, `VOLTAP_NAME/PHYS/DEVICES`, `VOLTAP_STATS`,
`VOLTAP_RESCAN_S`, `VOLTAP_STATUS_FILE`, `VOLTAP_DEGRADE_EXIT_S`).

## Notes

* Needs `python3-evdev` (already present: hhd depends on it) and root, because
  `/dev/input/event*` is `root:input 0660` and `/dev/uinput` is `root:root 0600`.
* The physical-keyboard fallback is a full keyboard proxy; holding a
  non-volume key repeats through the shim because `EV_REP` is advertised there.
  Without hhd, that is the only way to keep the built-in keyboard usable.
* Licence: GPL-2.0-or-later. No vendor code or binaries are included.
