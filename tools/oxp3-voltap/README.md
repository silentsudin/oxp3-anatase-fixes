# oxp3-voltap

Normalises the ONEXPLAYER 3 volume rocker into exactly one tap per physical
press. Root cause, evidence and the full validation story:
[`docs/hw/volume-buttons.md`](../../docs/hw/volume-buttons.md).

```
oxp3-voltap run       # daemon: grab the volume device, emit clean taps (root)
oxp3-voltap probe     # read-only: EV_KEY timing table + verdict (root)
oxp3-voltap check     # candidates, capabilities, what would be grabbed
oxp3-voltap selftest  # create the uinput shim and emit two taps (root)
```

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

## What it does

* grabs the device that carries the rocker — by default the *Handheld Daemon
  Volume Keyboard* that hhd publishes, because hhd owns the AT keyboard;
* drops `EV_KEY value == 2` (kernel soft repeat), duplicate presses and contact
  bounce;
* emits one 30 ms down/up pair per press through `OXP3 volume tap shim`
  (`phys-oxp3-voltap`), which has no `EV_REP`, so nothing can repeat it;
* turns a release without a press into one tap (that is what hhd emits when the
  kernel already force-released the scancode, so the hhd source patch is not
  needed for the rocker to work);
* force-releases any key that somehow stays down (500 ms watchdog), releases
  everything on SIGTERM, and re-attaches within ~1 s when hhd recreates its
  device.

Tuning lives in `/etc/oxp3-voltap.conf` (`VOLTAP_HOLD_MS`, `VOLTAP_GAP_MS`,
`VOLTAP_MAX_HOLD_MS`, `VOLTAP_ORPHAN_TAP`, `VOLTAP_NAME/PHYS/DEVICES`,
`VOLTAP_STATS`).

## Notes

* Needs `python3-evdev` (already present: hhd depends on it) and root, because
  `/dev/input/event*` is `root:input 0660` and `/dev/uinput` is `root:root 0600`.
* The daemon refuses, with a diagnostic, to grab devices that advertise
  `EV_REP` or REL/ABS axes — a full keyboard proxy is out of scope; the
  documented alternative for that case is the kernel force-release hwdb.
* Licence: GPL-2.0-or-later. No vendor code or binaries are included.
