# Home button / LED integration harness (virtual HID)

End-to-end test for `tools/oxp3-homebtn.py`, `tools/oxp3-led.py` and
`tools/oxp3-rgbd.py` against a **virtual** vendor HID device created through
`/dev/uhid`. Nothing here needs the OneXPlayer 3.

```sh
sudo tests/integration/hid/run.sh                 # the whole harness
sudo tests/integration/hid/run.sh --keep          # keep device + daemons for debugging
sudo tests/integration/hid/run.sh --timeout 180   # raise the watchdog

tools/repo-test.sh --integration                  # all opt-in integration suites
OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only hid-integration
make test-integration
```

## What it creates

One `uhid` HID device with the report descriptor documented in
[`docs/hw/home-button-leds.md`](../../../docs/hw/home-button-leds.md): usage page
`0xFF00`, usage `0x0001`, no report ID, 64-byte input and output reports. It
uses the identity the tools look for (USB `1a86:fe00`) but an unmistakable test
name/phys:

* name: `OXP3 uhid integration test (virtual 1a86:fe00)`
* phys: `phys-oxp3-uhid-integration-test`

The kernel publishes it as a normal `/dev/hidrawN`; no input child is created
(the vendor usage page is not an input application, exactly like interface
`1.2` on real hardware).

## Scenarios

| # | what | asserted |
|---|---|---|
| 0 | kernel parity | descriptor bytes equal `oxp3_hid.vendor_report_descriptor()`, no input child, identity rediscovered by name+phys |
| 1 | home button | real `oxp3-homebtn.py --device <node> --uinput-name "OXP3 Home Button (integration test)"`; inject the documented `0xB2` press/release; read back `KEY_HOMEPAGE` down/up from the uinput keyboard **the daemon created** (unique test name, `0003:0001:0001`, key bit 172) |
| 2 | LED | real `oxp3-led.py --device <node> solid ff6600 --sides top --level medium`; the harness receives the `UHID_OUTPUT` reports and compares them byte-for-byte with the encoder (brightness frame then 18 RGB triplets + trailer), and with the tool's own `send <hex>` lines |
| 3 | rgbd | real `oxp3-rgbd.py --once --device <node> --config <tmp>` applies exactly the planned frames; `--check` names the virtual node and exits 0 |
| 4 | reconnect | destroy the uhid device → daemon logs `device lost:`/`retrying in`; recreate it with the same identity → daemon logs `reconnect #1 to <new node> (identity ...)` and a second injected press/release still becomes `KEY_HOMEPAGE` |

Safety assertions: every node path in the daemons' logs and every `hidraw` fd of
the daemon under test is one of the harness's virtual nodes; the tools are
pinned with `--device`, so their discovery scan is never used while a real
`1a86:fe00` interface may be present.

## Concurrency on a real machine

While the virtual device exists, any *discovery-based* instance of our tools or
of HHD that (re)scans for `1a86:fe00` can select it: `find_vendor_hidraw()`
picks the first input-less vendor node and cannot tell a uhid device from the
physical interface. The harness itself pins every process it starts with
`--device`, so **it** can never touch the real node, and the virtual node only
exists for the duration of the run. A concurrently running writer (e.g. HHD's
RGB plugin on a device hotplug) would show up as extra `UHID_OUTPUT` frames and
makes the exact-byte assertions fail loudly; rerun with HHD idle if that
happens.

## Requirements and skips

Write access to `/dev/uhid` **and** `/dev/uinput`, plus read access to the
created event node. Root always works:

```sh
sudo tests/integration/hid/run.sh
```

Without that access the harness exits **77** (SKIP) and prints the same command.
It never runs `sudo` itself, never writes to a real hidraw node, and does not
toggle any host state beyond the ephemeral device/daemons it owns.

## Cleanup

By default the uhid device is destroyed, the daemons are SIGTERMed (then
killed), readers are closed and the absence of leftovers is asserted — even when
a scenario fails, via `finally` plus SIGTERM/SIGINT handlers. `--keep` is the
explicit debug exception: it leaves the virtual device alive (a small holder
process keeps the uhid fd open) and the daemons running, and prints the exact
`kill … && rm -rf …` cleanup command.

## What still needs the real device

* the physical home-button press path and the MCU's real `0xB2` timing/
  autorepeat (the harness injects what the documentation describes),
* the udev rule/ACL (`90-oxp3-hid.rules`) and the USB sysfs identity — a uhid
  device has no USB port/physical path, so `HID_PHYS` here is synthetic,
* USB reset/re-enumeration and suspend/resume (the harness simulates the node
  disappearing, not a real bus event),
* whether the firmware accepts the frames (the harness proves what we send, not
  how the LEDs look).
