# oxp3-hhd-voltap

Volume rocker tap normalisation for the OneXPlayer 3, implemented **inside
Handheld Daemon (HHD)** as an `hhd.plugins` plugin.

Why this exists: `tools/oxp3-voltap` used to grab HHD's own virtual
`Handheld Daemon Volume Keyboard` (`event23`). HHD's OXP keyboard producer
(`hhd.device.oxp.base.OxpAtKbd`, `VID:PID 0001:0001`, no name filter) matches
that device too, so its `grab()` failed with `OSError: [Errno 16] Device or
resource busy`, HHD assumed the controller had disconnected, and the emulated
Steam controller (`28DE:12FF`) disappeared for a few seconds. The supported
path is to not touch any device: this plugin normalises the volume buttons in
HHD's own producer path.

Read the API study with file/line references: `docs/hw/hhd-plugin.md`.

## What it does

* discovered through the `hhd.plugins` entry-point group as `oxp3_voltap`
  (`oxp3_hhd_voltap:autodetect`);
* at `open()`, defensively wraps `hhd.device.oxp.base.OxpAtKbd.produce` so the
  translated `key_volumeup`/`key_volumedown` buttons go through our tap state
  machine (`tap.py`): one clean tap per press, minimum hold, bounce gap,
  duplicate/orphan/late-release handling, and a bounded synthetic release;
* turns HHD's own fixed tap synthesis off for that producer so a press is never
  normalised twice;
* never opens, reads, grabs or creates an input device;
* if the HHD shape is different (module/class/method missing, changed
  signature) it logs a warning and no-ops; if anything raises inside the filter
  it logs once, restores HHD's handling and passes HHD's events through - an
  exception can never take the controller down.

Tuning (read once at plugin load, no HHD config schema involved):

| variable | default | meaning |
|---|---|---|
| `OXP3_HHD_VOLTAP=0` | on | disable without uninstalling |
| `OXP3_HHD_VOLTAP_HOLD_MS` | 60 | minimum synthetic tap hold |
| `OXP3_HHD_VOLTAP_GAP_MS` | 60 | bounce window after a tap |
| `OXP3_HHD_VOLTAP_MAX_HOLD_MS` | 500 | cap for any synthetic hold |

## Install on the immutable host

Target: `/usr/local/lib/python3.15/site-packages` (`/usr/local` is
`/var/usrlocal` on Anatase, so it survives rpm-ostree updates). No pip needed.

```bash
sudo tools/hhd-oxp3-voltap/install.sh install
# then restart hhd so it imports the plugin:
sudo systemctl restart hhd
```

Restarting HHD drops the emulated controller for a few seconds; that is the
only visible interruption. The installer never restarts anything itself.

Remove exactly our files:

```bash
sudo tools/hhd-oxp3-voltap/install.sh uninstall
sudo systemctl restart hhd     # unload the plugin from the running daemon
```

Read-only inspection:

```bash
tools/hhd-oxp3-voltap/install.sh status   # files + entry-point resolution
tools/hhd-oxp3-voltap/install.sh check    # can this HHD build be hooked?
```

Offline/sandbox use: `--site DIR` (and `--root DIR`) install into a scratch
prefix; `OXP3_HHD_VOLTAP_PYTHON` selects the interpreter used for the
post-install probe.

Orchestrator: `tools/oxp3-hw-install.sh --volume` installs this plugin by
default; `--volume-daemon` installs the legacy `oxp3-voltap` daemon instead.

## Uninstall by hand

```bash
sudo rm -rf /usr/local/lib/python3.15/site-packages/oxp3_hhd_voltap \
            /usr/local/lib/python3.15/site-packages/oxp3_hhd_voltap-0.1.0.dist-info
sudo systemctl restart hhd
```

Nothing else is installed: no unit, no udev rule, no config file.

## Tests

```sh
python3 -m unittest tests/unit/test_hhd_voltap_plugin.py -v   # from the repo
```

The plugin does not need HHD to import: without `hhd.plugins` it falls back to
a plain base class so the unit tests and `install.sh check` work offline.

Licence: GPL-2.0-or-later. Our own code; no vendor binaries or code.
