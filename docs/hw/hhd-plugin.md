# The Handheld Daemon (hhd) plugin API, as used by `oxp3-hhd-voltap`

Host: Anatase, hhd `4.1.12+git.154.geca609b0f54a` at
`/usr/lib/python3.15/site-packages/hhd`. Every reference below is
`file:line` in that install and was read while writing this note; line numbers
will drift with hhd updates, so re-check the anchors before relying on them.

This document answers four questions: **how a plugin is discovered, what it may
do, how input events flow (and where a plugin cannot intervene)**, and why the
volume-tap normaliser lives where it does. It is the contract the package in
`tools/hhd-oxp3-voltap/` implements defensively.

## 1. Discovery: one entry-point group

hhd looks for third-party providers in the `hhd.plugins` entry-point group:

```python
# hhd/__main__.py:236-254
for autodetect in entry_points(group="hhd.plugins"):
    name = autodetect.name
    detector_names.append(name)
    if name in blacklist:
        logger.info(f"Skipping blacklisted provider '{name}'.")
    if whitelist and name not in whitelist:
        logger.info(f"Skipping provider '{name}' due to whitelist.")
        continue

    detectors[autodetect.name] = autodetect.load()
...
plugins[name] = autodetect([])
```

Consequences that matter for a third-party package:

* The entry point must be a **zero-argument callable returning
  `Sequence[HHDPlugin]`** (`hhd/plugins/plugin.py:161-164`). It is *not* a
  class, and it is called with `[]` here (not with the plugins detected so far,
  despite the protocol signature `autodetect(existing)`).
* `autodetect.load()` is **not inside a try block** (`__main__.py:245`). An
  exception while importing your module aborts hhd's startup. The module must
  therefore import cleanly even when `hhd` itself is missing or changed; do all
  `hhd.*` imports behind `try/except` or lazily.
* The result is flattened, then **sorted by `plugin.priority`** ascending
  (`__main__.py:279-283`), so lower numbers run earlier.
* A blacklist file is read and rewritten at `<hhd package dir>/plugins.yml`
  (`__main__.py:229-230, 248`), and an `HHD_PLUGINS` environment whitelist is
  honoured (`__main__.py:236`). Caveat in this build: the blacklist branch only
  logs, it does not `continue` (`__main__.py:240-241`); the whitelist does
  (`__main__.py:242-244`). So `plugins.yml` is not a reliable off switch here -
  uninstall the package, or use `HHD_PLUGINS` with the complete list.
* If the provider returns an empty sequence, hhd just logs it in the provider
  list; nothing breaks.
* A distribution can be installed by any tool as long as
  `importlib.metadata` sees it: package directory plus
  `<name>-<version>.dist-info/entry_points.txt`. That is what our `install.sh`
  writes into `/usr/local/lib/python3.15/site-packages`.

## 2. The plugin object and its lifecycle

`HHDPlugin` is a plain base class, not an ABC (`hhd/plugins/plugin.py:130-158`):

| member | when hhd calls it | notes |
|---|---|---|
| `name`, `priority`, `log` | read once after autodetect | `log` is a short journal tag |
| `open(emit, ctx)` | once, after all plugins are collected (`__main__.py:350`) | give the plugin its `Emitter` here |
| `settings()` | on initial config compile (`__main__.py:403`) and every settings reload (`__main__.py:686`) | return `HHDSettings`; `{}` is valid |
| `validate(tags, config, value)` | while config is validated (`__main__.py:701`, and profile loads `466`) | return `True` only for keys you own |
| `prepare(conf)` | every loop tick, **reverse priority order** (`__main__.py:712`) | do work before `update` |
| `update(conf)` | every loop tick (`__main__.py:715`) | main per-tick hook |
| `notify(events)` | every tick, **only when top-level events exist** (`__main__.py:709`) | control-plane events, see §3 |
| `close()` | once at shutdown, in the `finally` block (`__main__.py:858-881`) | release threads/fds here |

Failure handling: each of those calls goes through `run_plugin_cmd`
(`__main__.py:310-343`). An exception is logged, the plugin is **removed from
the list and hhd keeps running** (it sets `settings_changed`). That is a safety
net, not a design target: a plugin that raises from `open()` simply never runs.
`autodetect()` itself is called inside a try (`__main__.py:247-269`), but the
*import* is not (§1).

Threading: the lifecycle calls happen on hhd's main loop thread. A plugin that
needs a thread (the built-in `debug` and `powerbutton` plugins do) starts it in
`open()` and stops it in `close()`; state lives on the instance. `prepare`/
`update`/`notify` run roughly every 2 s (`POLL_DELAY`, `__main__.py:62`) unless
an event wakes the loop, so they are not suitable for sub-100 ms work.
Instance state survives configuration reloads; `open()` is not called again.

Templates worth reading:

* `hhd/plugins/customization/__init__.py:13-84` - minimal plugin: settings from
  a YAML file, `open()` resets state, `update()` reads/writes sysfs, autodetect
  filters by what exists on the host.
* `hhd/plugins/debug/__init__.py` and `hhd/plugins/powerbutton/base.py` -
  threads/events started in `open` and stopped in `close`.
* `adjustor/hhd.py` (third party, same entry-point group) - two plugins, a
  background thread, settings-driven enable/disable, `close()` joining it.
* `hhd/plugins/overlay/base.py` - the only code that uses
  `Emitter.register_intercept`/`grab`; see §3 for why that is not a filter.

## 3. Where input events are, and are not, visible

Controller input does **not** pass through the plugin API. The pipeline is
per-device-plugin:

```
Producer.produce(fds) -> Multiplexer.process(events) -> Consumer.consume(events)
        ^ physical evdev/hidraw/serial            ^ uinput/dualsense/sd output
        hhd/device/*                              hhd/controller/virtual/*
```

`Multiplexer.process` is `hhd/controller/base.py:705`; the producer/consumer
protocol is `hhd/controller/base.py:511-600`. The only way into that graph from
outside is the `Emitter` that the device plugin also receives:

* `Emitter.register_intercept(cb)` + `Emitter.grab(True)`
  (`hhd/controller/base.py:247-278`) make the multiplexer divert **all**
  button/axis events to `cb` and hide them from the consumers
  (`hhd/controller/base.py:1540-1590`). It self-releases after
  `GRAB_TIMEOUT = 5 s` (`base.py:204, 265`). This is the overlay's
  whole-controller capture (`hhd/plugins/overlay/base.py:113,129,389,451`);
  it is not a per-event filter and using it for volume would freeze the
  controller.
* `Emitter.inject()/inject_timed()/inject_recv()`
  (`hhd/controller/base.py:280-316`) *add* events to the multiplexer output
  (merged at `base.py:1518-1536`). There is no API to **suppress or rewrite**
  an event produced by a producer.
* `Emitter.set_gamedata()` (`hhd/plugins/plugin.py:100-127`) publishes game
  metadata for the overlay; unrelated.

Consequently `notify(events)` is called with the events that reach hhd's main
loop (`__main__.py:672-709`): `settings`, `profile`, `apply`, `state`, `acpi`,
`tdp`, `gpu`, `energy`, `platform_profile`, `special` and injected
`configuration`s - **never** `button`/`axis` events. The `InputEvent` TypedDict
in `hhd/plugins/plugin.py:76-87` is part of the declared union but no producer
in this build emits it to plugins.

So: a plugin cannot filter controller input. The normalisation has to happen
where hhd itself handles the OXP3 rocker - see §4.

## 4. Where hhd handles the OXP3 rocker

The OXP3 EC puts the volume rocker on the AT keyboard, and hhd's device plugin
(`hhd/device/oxp/base.py`) creates:

* `OxpAtKbd` (`base.py:225-302`), a `GenericGamepadEvdev` with
  `vid=[0x0001], pid=[0x0001]`, `grab=True` and (for the `oxp3` quirk)
  `volume_taps=True` (`base.py:465-476`). It matches **by VID:PID only** - the
  producer's `open()` loops every valid evdev and skips name matching when no
  name was given (`hhd/controller/physical/evdev.py:277-330`), and takes
  `EVIOCGRAB` at `evdev.py:325-326`.
* a uinput volume keyboard named `Handheld Daemon Volume Keyboard`, phys
  `phys-hhd-vbtn`, **with the same VID:PID `0x0001:0x0001`**
  (`base.py:542-551`, second instance at `806-815`), created with
  `volume_keyboard=True` (cached in `hhd/controller/virtual/uinput/__init__.py`).

That is the trap: the OXP keyboard producer also matches hhd's **own** uinput
volume keyboard. If another process holds `EVIOCGRAB` on it, hhd's
`self.dev.grab()` raises `OSError: [Errno 16] Device or resource busy`, the
device-worker exception path logs `Assuming controllers disconnected, restarting
after 3s` (`base.py:204-215`), and the emulated controller disappears. That is
exactly the outage of 2026-11-09, caused by the old `oxp3-voltap` default of
grabbing `event23`.

Our plugin therefore wraps **`OxpAtKbd.produce`** - the boundary where raw
volume keys have become `key_volumeup`/`key_volumedown` button events and
before the multiplexer. It:

* switches `self.volume_taps` off for that producer instance, so hhd's fixed
  60 ms tap policy (`base.py:56, 233-267`) does not run twice;
* passes every non-volume event through untouched;
* runs the tap state machine on volume buttons only, appending any due
  synthetic release;
* never calls `evdev`, never opens a device, never grabs, never creates uinput.

This uses hhd's official loading path plus one narrow, version-probed shim over
a private device class. That is a deliberate trade-off: upstream has no filter
hook, and the alternative (a second EVIOCGRAB) is what broke the controller. If
`OxpAtKbd` or `produce` is absent or has a different shape, the plugin logs and
no-ops; if the filter raises, the plugin logs once, restores hhd's own tap
handling and passes hhd's events through

## 5. State, tuning and failure policy of our plugin

* State lives on the plugin instance (`VolumeTapPlugin`), which hhd creates
  once. Per-producer normaliser state lives on the producer instance
  (`_oxp3_voltap_norm`) because each `OxpAtKbd` has its own volume keys.
* No settings are registered with hhd on purpose: a config-schema change in a
  future hhd cannot then break the plugin. Tuning is environment-only
  (`OXP3_HHD_VOLTAP`, `OXP3_HHD_VOLTAP_HOLD_MS`, `_GAP_MS`, `_MAX_HOLD_MS`),
  read once at plugin load.
* `open()` is the only place the hook is installed; `close()` restores the
  original `produce` if it is still ours. Installing twice is idempotent
  (`_oxp3_voltap_wrap` marker).
* The wrapper is wrapped in `try/except` around *our* work only; a producer
  exception is re-raised so hhd's own disconnect handling still sees it.
* A filter failure disables the state machine for that producer for the rest of
  the boot and restores `volume_taps = True`, i.e. it degrades to hhd's
  baseline instead of repeatedly perturbing input.

## 6. Minimal plugin skeleton (the shape we follow)

```python
# entry point: [project.entry-points."hhd.plugins"]
#             oxp3_voltap = "oxp3_hhd_voltap:autodetect"

try:                       # hhd import must never abort hhd's startup
    from hhd.plugins import HHDPlugin as _Base
except Exception:
    _Base = object

class MyPlugin(_Base):
    name = "my_plugin"
    priority = 90          # higher = later; sorting is ascending (__main__.py:283)
    log = "mine"

    def open(self, emit, context):        # once; must not raise
        self.emit = emit
    def settings(self):                   # config schema, {} is fine
        return {}
    def prepare(self, conf): ...          # every tick, before update
    def update(self, conf): ...           # every tick
    def notify(self, events): ...         # control-plane events only
    def close(self): ...                  # shutdown; release threads/fds

def autodetect(existing=None):            # called with [] today
    try:
        return [MyPlugin()]
    except Exception:
        return []
```

## 7. References

| topic | location |
|---|---|
| entry-point discovery, blacklist/whitelist | `hhd/__main__.py:229-260` |
| priority sort, validator | `hhd/__main__.py:279-287` |
| per-call error handling / plugin removal | `hhd/__main__.py:310-343` |
| lifecycle call sites | `hhd/__main__.py:350, 403, 686, 701, 709, 712, 715, 858-881` |
| `HHDPlugin`, `HHDAutodetect` | `hhd/plugins/plugin.py:130-164` |
| `Emitter` (inject/intercept/gamedata) | `hhd/plugins/plugin.py:100-127`, `hhd/controller/base.py:204-327` |
| controller pipeline | `hhd/controller/base.py:511-600, 705` |
| intercept suppression | `hhd/controller/base.py:1540-1590` |
| overlay owns the intercept | `hhd/plugins/overlay/base.py:113,129,389,451` |
| OXP keyboard producer + tap policy | `hhd/device/oxp/base.py:41-56, 225-302, 465-476` |
| uinput volume keyboard (same VID:PID) | `hhd/device/oxp/base.py:542-551, 806-815` |
| evdev matching + EVIOCGRAB | `hhd/controller/physical/evdev.py:277-330` |
| disconnect log after a producer error | `hhd/device/oxp/base.py:204-215` |
| example plugins | `hhd/plugins/customization/__init__.py`, `hhd/plugins/debug/__init__.py`, `adjustor/hhd.py` |

Related: [`volume-buttons.md`](volume-buttons.md) (the outage, the topology
rules, validation), [`docs/DAEMONS.md`](../DAEMONS.md) ("never grab what HHD
owns").
