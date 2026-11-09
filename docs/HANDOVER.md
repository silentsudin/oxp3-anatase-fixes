# HANDOVER — one page to start a session on this repo

Written 2026-11-09 at revision `250dcfe` (106 commits, 7 tags; author `silentsudin`). It is a reading
guide and a status snapshot, not a new source of truth: **component state lives in
[`hw/components.yaml`](hw/components.yaml)** and the tables generated from it. Everything below is
traceable to a repo file or to a read-only command whose output is quoted. Nothing privileged was
executed for this document.

> **Start here: `tools/oxp3-health.sh`** (or `make health`) — one read-only command (~7 s) that
> answers "can I install / is it working / what is left" by composing preflight, verify, the
> firstrun checklist and status, printing only the non-`PASS` rows plus the next 1-3 commands.
> `--json` for machines, `--quiet` for one line, `--verbose` for the raw reports; the section
> contract is in [`HEALTH.md`](HEALTH.md).

> **Audit update, 2026-11-09 06:00 UTC.** The volatile counts in this page (component states,
> harness checks, `repo-test` suites, unit tests) are now generated from their sources by
> `tools/gen-docs.py`; read the `handover-state` block in §2 instead of trusting a number
> copied into prose. The dated audit matrix is
> [`state/audit-2026-11-09.txt`](state/audit-2026-11-09.txt); where this page and that file
> disagree, the audit file is newer.

## 0. Start here, in this order

| # | read | why |
|---|---|---|
| 1 | this file | what exists, what is installed, what is proven, what is blocked, what is yours to decide |
| 2 | [`hw/components.yaml`](hw/components.yaml) + the generated matrix in [`hw/README.md`](hw/README.md) | the only state truth (the roll-up is generated in §2); see [`hw/REGISTRY.md`](hw/REGISTRY.md) for the vocabulary |
| 3 | [`INSTALL-HW.md`](INSTALL-HW.md) | install / opt-out / uninstall / rollback, one command per component |
| 4 | [`VALIDATION.md`](VALIDATION.md) | the harness: its declared check ids (counted in the §2 block), what each proves, exit codes, what a `SKIP` means |
| 5 | [`QA.md`](QA.md), [`PROVENANCE.md`](PROVENANCE.md), [`upstream/README.md`](upstream/README.md) | offline evidence snapshot, the no-vendor-code rule, the decisions that are the owner's |
| 6 | the per-topic doc of whatever you touch next | each holds the recovered evidence, the implementation and its validation steps |

Then: `tools/repo-test.sh --fast` (offline; `--list` prints its inventory), `tools/oxp3-preflight.sh`
(read-only, before any install) and `tools/oxp3-verify.sh` (read-only).
For "are the seven goal items done?", read [`ACCEPTANCE.md`](ACCEPTANCE.md) and run
`tools/oxp3-acceptance.sh` (`--json` for machines): it derives each item's verdict from
`hw/components.yaml` plus the verify/health/repo-test evidence and names the exact device step that
is still missing. It never turns offline evidence into a device-proven claim.
The suite is green apart from the explained `SKIP`s listed on every run; the harness has one known
failing group (§2).
No RE was needed for this round; Ghidra is only for the vendor-binary work already recorded in the
topic docs.

## 1. What the device and this repository are

OneXPlayer 3 (`ONEXPLAYER 3`, BIOS 5.09, board `ONE-NETBOOK`), Intel Panther Lake with Arc B390
`[8086:b080]`, running Anatase `20261026.3` (rpm-ostree, kernel `7.2.7-an28.fc45.x86_64`, `/usr` is an
overlay), KDE Plasma Wayland, HHD 4.1.12 active, 60 retained boots, 87.665 Wh pack. Sources:
[`QA.md` §Environment](QA.md), [`hw/gap-audit.md` §1](hw/gap-audit.md),
[`hw/sleep-power.md` §1](hw/sleep-power.md), plus `rpm-ostree status --booted`,
`systemctl is-active hhd` and `$XDG_SESSION_TYPE` read today.

The repo does two separable jobs, and they install to different places:

* **driver stack** (`oxp3-fixes*.sh`): kernel modules (local RPM `kmod-oxp3`), pinned firmware
  (`oxp3-firmware`), patched Mesa/gamescope/MangoHud and the XeSS/XMX pieces — into the `/usr`
  overlay and `/usr/lib/firmware/updates`, so an Anatase update wipes part of it.
* **hardware fixes** (`tools/oxp3-hw-install.sh`): volume rocker, home button/top LEDs, body RGB,
  suspend policy, Bluetooth initramfs, power/fan/battery, adaptive TDP, IMU blacklist, speaker EQ —
  into `/etc`, `/usr/local` and `~/.config`, which survive updates. Driver stack must be reinstalled
  after each update; hardware fixes are re-asserted (idempotent).

Live evidence of the split (read-only, today): `rpm-ostree status --booted` lists
`LocalPackages: kmod-oxp3-7.2.7_an28.fc45.x86_64-202610292314, oxp3-firmware-1-202610292312` and
`Initramfs: regenerate`; `modinfo -F filename xe` resolves to
`/lib/modules/7.2.7-an28.fc45.x86_64/updates/.../xe.ko.xz`; the host Vulkan driver is
`ec418f04e589965a` (Mesa 27.0.0-dev in the overlay) while the packaged `mesa-vulkan-drivers` is still
26.2.4 — i.e. the overlay install is in effect on this boot.

## 2. State (generated from `components.yaml`; do not re-type facts here)

The state roll-up and the volatile counts that this page used to copy by hand are generated from
their sources below - the component data, the harness inventory, the test-runner registry and the
unit-test run. Change the source, then `tools/gen-docs.py --write`; never edit the block.

<!-- BEGIN GENERATED: handover-state (source: docs/hw/components.yaml + tools/oxp3-verify.sh + tools/repo-test.sh + tests/unit; regenerate: tools/gen-docs.py --write) -->
State roll-up and the volatile counts this page used to re-type. Every value below is
computed by `tools/gen-docs.py` from the named source; **do not hand-edit this block**.

**25 components**: **10** `working`, **4** `partial`, **1** `blocked`, **2** `not-a-gap`, **8** `awaiting-install`.
Counted from the `state:` field of each component in `docs/hw/components.yaml`.

| state | components (doc link) |
|---|---|
| `working` (10) | gamescope ([hdr-vibrance](hw/hdr-vibrance.md)), mesa ([BEST-CONFIG](BEST-CONFIG.md)), mangohud ([BEST-CONFIG](BEST-CONFIG.md)), kernel-modules ([WORKING-BUILD-2026-11-08](WORKING-BUILD-2026-11-08.md)), firmware ([WORKING-BUILD-2026-11-08](WORKING-BUILD-2026-11-08.md)), rapl-power ([sagv](hw/sagv.md)), audio ([audio-harman-apo](hw/audio-harman-apo.md)), power ([power-fan](hw/power-fan.md)), display ([hdr-vibrance](hw/hdr-vibrance.md)), decky ([decky-plugin](decky-plugin.md)) |
| `partial` (4) | xess-xmx ([xess-xmx](xess-xmx.md)), fan ([power-fan](hw/power-fan.md)), battery ([battery-tdp](hw/battery-tdp.md)), fingerprint ([fingerprint](hw/fingerprint.md)) |
| `blocked` (1) | sagv ([sagv](hw/sagv.md)) |
| `not-a-gap` (2) | imu ([camera-imu](hw/camera-imu.md)), camera ([camera-imu](hw/camera-imu.md)) |
| `awaiting-install` (8) | sched-ext ([cpu-sched](cpu-sched.md)), volume ([volume-buttons](hw/volume-buttons.md)), leds ([home-button-leds](hw/home-button-leds.md)), rgb ([rgb-coverage](hw/rgb-coverage.md)), daemons ([DAEMONS](DAEMONS.md)), sleep ([sleep-power](hw/sleep-power.md)), bluetooth ([bluetooth](hw/bluetooth.md)), adaptive-tdp ([battery-tdp](hw/battery-tdp.md)) |
| **total** (25) | every component in the data file; add a component there, not here |

| volatile fact | value | how this number was obtained |
|---|---|---|
| checks declared by `tools/oxp3-verify.sh` | **86** ids | counted from its `record`/`simple_ind` lines, the same extraction the harness uses for its inventory; the default-run pass/fail/skip split is host data, not a repository fact |
| suites in `tools/repo-test.sh` | **42** (**37** in `--fast`, **42** in `--full`) | parsed from the `def_suite` registrations and their mode column |
| tests in `tests/unit` | **918** | run `python3 -m unittest discover -s tests/unit -q` and parse `Ran N tests` (the same discovery `tools/repo-test.sh` uses) |
<!-- END GENERATED: handover-state -->

The full matrix with summaries, implementation paths, install entrypoints, verify ids and caveats is
generated into [`hw/README.md`](hw/README.md) and [`INSTALL-HW.md` §5](INSTALL-HW.md); read those,
not a copy.

### Installed vs awaiting install (live, read-only)

`tools/oxp3-hw-install.sh --status` today: `libexec: vol=no sleep-wakeup=no sleep-log=no powerctl=no
adaptivetdp=no`, `udev rules: hid=no sleep-wakeup=no volume-hwdb=no`, `sleep trigger:
logind-drop-in=no polkit-rule=no`, `user units: homebtn=-/- rgbd=-/-`, `power: profile-file=no live
PL1/PL2/PL4=35/37/122 W`, `battery: limit=80% behaviour=[auto] inhibit-charge
inhibit-charge-awake`, `bluetooth: fw-in-updates=2/2 dracut-conf=no hci0=yes`, `audio:
eq-dropin=yes`, `imu: modprobe-blacklist=yes`.

So of the hardware-fix set this host has exactly two artefacts — the speaker EQ drop-in
(`~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf`) and the IMU modprobe snippet
(`/etc/modprobe.d/oxp3-bmi160.conf`). The driver-stack half *is* installed (`LocalPackages`, overlay
Mesa, `rapl-power.service` active, the XMX release tree and `~/.config/xess-xmx/oxp3-env.sh`). Every
`awaiting-install` component is still uninstalled; that state means code-complete and
offline-validated, not "unknown" ([`hw/REGISTRY.md` §3](hw/REGISTRY.md)).

### Validated vs unvalidated

| layer | command | result on this host |
|---|---|---|
| offline suites | `tools/repo-test.sh --fast` | run it for this host's PASS/FAIL/SKIP verdict; the suite total and the `--fast` subset are in the block above |
| unit tests | `make unit` | run it for this host's verdict; the test total is in the block above |
| harness | `tools/oxp3-verify.sh` | run it for this host's PASS/FAIL/SKIP verdict; the declared id count is in the block above. This page's recorded run still failed `bt.journal bt.controller bt.diag inst.backups` (the last one is the rollback round's unbacked in-place edits, not a preflight finding) |
| status | `tools/oxp3-status.sh --json` | `overall=warn`, `ok=23 warn=5` (warns: `xmx.trace`, `sched.state`, `sched.scx_cosmos`, `sched.unit`, `flatpak.steam_env`) |
| snapshots | `docs/state/` | dated evidence (55-check verify run, host snapshot, status JSON, selftest) — earlier inventory, not the current counts |

The Bluetooth failures are one real defect and its documented fix: both Bluetooth firmware files are in
`/usr/lib/firmware/updates/intel` with matching pinned hashes, but this boot's initramfs predates them,
so the kernel logged `Failed to load Intel firmware file …(-2)`; `bt.diag` reports it correctly
([`hw/bluetooth.md`](hw/bluetooth.md), `docs/state/verify-default-2026-11-09.txt`).

**Not yet executed on this host** (device-side acceptance, deliberately outside the no-sudo rule):
`vol.hold`, `led.write`, `sleep.suspend`, `sleep.measure`, `fan.savemode`, the fingerprint probe, and
any real suspend since the policy was written ([`VALIDATION.md` §9](VALIDATION.md)).

## 3. Evidence trail — which doc holds the proof, which test proves the claim

Reverse-engineering evidence is per-topic; the harness proves the fix is *present*, not that the device
behaves. Both are needed; neither substitutes for the other.

| subsystem | RE / measurement evidence | claim proven by |
|---|---|---|
| volume rocker | [`hw/volume-buttons.md`](hw/volume-buttons.md): hhd 4.1.12 source, `event4`/`event23` inventory, `force_release` readback, atkbd scancode table | `vol.*` checks; `tests/unit/test_voltap.py`; `oxp3-voltap selftest` (root, not run) |
| home button / top LEDs | [`hw/home-button-leds.md`](hw/home-button-leds.md): recovered HID report, 5 zones / 11 presets, obfuscation limits | `led-selftest`/`hid-selftest` (captured frames 9/9), `hid.node`, `led.sides`, `led.zones` |
| body RGB | [`hw/rgb-coverage.md`](hw/rgb-coverage.md) + captured frames [`hw/rgb-frames-2026-11-09.txt`](hw/rgb-frames-2026-11-09.txt) | `rgbd --check` (5 zones / 10 frames), `rgbd.timing`, `tests/unit/test_rgbd.py` |
| daemons (resume/hotplug) | [`DAEMONS.md`](DAEMONS.md): contract, journal vocabulary, failure modes | `tests/unit/test_{lifecycle,homebtn_lifecycle,rgbd_lifecycle}.py`, `life.fd`, `life.reconnect`, `life.sim` (`--full`) |
| suspend / wake policy | [`hw/sleep-power.md` §2.3-2.6](hw/sleep-power.md): 9 real s2idle entries, HHD 4.1.12 line quotes, logind/polkit state, inhibitor semantics, suspend-counter ABI (`max_hw_sleep` is a ceiling, not a duration) | `sleep.*` checks; `sleep.stats` reports the counter tuple and says `UNKNOWN` - never `PASS`/`FAIL` - when `success=0` meets a hardware-sleep number |
| Bluetooth | [`hw/bluetooth.md`](hw/bluetooth.md): PCIe `8086:e476`, boot-log timing, SHA-256 of both blobs, `MODULE_FIRMWARE` absence | `bt.*` checks (5), `oxp3-bt-firmware.sh --check` |
| power envelope / fan / battery | [`hw/power-fan.md`](hw/power-fan.md), [`hw/battery-tdp.md`](hw/battery-tdp.md): vendor `perf_conf.ini` table, `PerfMode.exe` Ghidra, EC findings | `pwr.*`, `fan.*`, `bat.*` checks; `tests/unit/test_powerctl.py`, `test_adaptivetdp.py` |
| speaker EQ | [`hw/audio-harman-apo.md`](hw/audio-harman-apo.md): recovered 8-band model (0.03 dB error) | `eq-model` suite, `aud.model`, `aud.eq` |
| display HDR / ICC / vibrance | [`hw/hdr-vibrance.md`](hw/hdr-vibrance.md): EDID/PQ/BT.2020, gamescope flags | `dsp.*`; `tests/unit/test_edid.py`, `test_icc.py`; `gamewrap` suite |
| SAGV | [`hw/sagv.md`](hw/sagv.md): MCHBAR pair `0x5DA0/0x5DA4=0x80000122`, busy bit, `enable_sagv` semantics | `tools/oxp3-sagv.sh status`/`probe` (probe exits 3 by design) |
| camera / IMU | [`hw/camera-imu.md`](hw/camera-imu.md): PCI/USB/ACPI inventory (`_STA=0` everywhere for camera), IIO 100 Hz BMI260 | `imu.selftest`, `imu.iio`; camera = the `grep …/OVTI*/status` command row |
| fingerprint | [`hw/fingerprint.md`](hw/fingerprint.md) + [`tools/fingerprint/README.md`](../tools/fingerprint/README.md): BF63112 SPI frame/CRC/image/retry | its Python suite (`python3 -m unittest discover -s tools/fingerprint/tests -q` prints the count), 324 C checks / 10 vectors, libfprint v1.94.9 syntax check; **no hardware transcript** |
| XeSS XMX | [`xess-xmx.md`](xess-xmx.md): CM patch series, IGC 2.10.10, binary comparison, `-doubleGRF` root cause | `gfx.xmx.*` (driver prefix `4b1d55ea` matches the live `~/.cache/oxp3-fixes/xmx-driver` build; `trace` SKIPs by design) |
| Mesa / gamescope / kernel / firmware / MangoHud | [`WORKING-BUILD-2026-11-08.md`](WORKING-BUILD-2026-11-08.md), [`BEST-CONFIG.md`](BEST-CONFIG.md), [`xess-xmx.md`](xess-xmx.md), `patches/**` | `gfx.mesa`, `gfx.working` (`tools/check-working.sh` 7/7), `gfx.xe`, `inst.paths` |
| sched_ext | [`cpu-sched.md`](cpu-sched.md) + [`cpu-sched-results.csv`](cpu-sched-results.csv) (round-3 numbers) | `sched.*` (unit not installed here, so `SKIP`/warn) |
| Decky / status | [`decky-plugin.md`](decky-plugin.md) | `decky-selftest`, `decky-cli`, `decky-package` (byte-identical 33,843 B zip), `decky.*` checks |

Companion evidence you can cite in a bug report: `docs/state/verify-default-2026-11-09.txt` (verbatim
harness run), `docs/state/verify-default-2026-11-09.json`, `docs/state/snapshot-2026-11-09.txt`
(host snapshot), `docs/state/oxp3-status-2026-11-09.json` (redacted), `docs/state/verify-selftest-2026-11-09.txt`.
Each `components.yaml` `last_validated.evidence` points at the file that carries today's proof.

## 4. Corrections log — what we got wrong, and where the correction lives

1. **"This machine has never suspended" was a measurement bug.** `journalctl -k` is `--dmesg`, which
   implies `-b`, so the count only ever saw the current boot. `journalctl -b all -k` shows **nine**
   `PM: suspend entry (s2idle)` between Oct 12 and 19. The real defect is the *trigger*: logind had no
   `Handle*` policy (power key default `poweroff`), PowerDevil holds a `block` inhibitor on
   `handle-*`, and HHD only forwards the key to Steam while a Steam Gamepad session runs. Fixed as a
   shipped logind drop-in + polkit rule, and encoded in `sleep.stats` (`success>0`, with
   `last_hw_sleep>0` only when the kernel exposes the hardware-sleep attrs). See
   [`hw/sleep-power.md` §2.5](hw/sleep-power.md), commits `11c4bf5` → `250dcfe`.
2. **"Camera is a Linux gap" was refuted.** There is no IPU7 PCI function (`8086:b05d` absent), every
   `OVTI*`/`INT3472` ACPI device is `_STA=0`, the vendor package carries no camera driver and the
   console has no camera UI ⇒ not-a-gap on this unit (other SKUs may populate it).
   See [`hw/camera-imu.md` §2](hw/camera-imu.md), commit `ffcff3f`.
3. **"Bluetooth firmware is missing" was the wrong diagnosis.** Both `ibt-00a0-0291-*.sfi` blobs are
   present, byte-identical to upstream, hash-pinned; the driver probes **inside the initramfs** before
   the ostree root is mounted and `btintel_pcie` has no `MODULE_FIRMWARE()` entries, so dracut cannot
   pack them. Root cause is the initramfs, and the fix is a dracut drop-in + module reload. It also
   refutes the "USB CNVi part" assumption in the older audit row. See [`hw/bluetooth.md`](hw/bluetooth.md),
   commit `560f241`; `hw/gap-audit.md` §1 is superseded on that row.
4. **Toolchain finding (`CMK2H`).** The shipped XMX driver is built on an Arch snapshot; our Fedora 45
   build differs (no `CMK2H` marker, ~29.71 MB vs 29.93 MB) and even an Arch-snapshot rebuild with the
   same source commit, patch bytes, meson flags and package list still differs (`b354bd5a`, 29,925,304 B)
   and still artifacted. Conclusion: the shipped binary is not reproducible from the tagged source.
   See [`xess-xmx.md`](xess-xmx.md) "Source/toolchain reproduction attempts", commits `a34547b`,
   `492a765`.
5. **"Our driver build is the cause of the white dots" was wrong.** Symbol sets (20,737) and all 75 CM
   functions have identical instruction counts in the release binary and our build; the only deltas are
   build-path-derived constants plus the `CMK2H` data marker, and linkage differs only by
   `libdisplay-info.so.3`. The binary is the same program. The artifact was **`-doubleGRF` kernel
   codegen** (256-GRF on Xe3); filtering it to 128 GRF in `tools/xmx-cm-compile-nodgrf.sh` is clean
   *and* faster. See [`xess-xmx.md`](xess-xmx.md), commits `8dd8f34`, `4a72bf8`, `b33185a`.
6. **Two smaller self-corrections.** The earlier sleep text also mis-stated `block` semantics: a
   `block` on `handle-*` stops logind from acting on the *event*, it does not veto `login1.Suspend`
   (the Oct 16 Steam call proves it) — corrected in [`hw/sleep-power.md` §2.5.4](hw/sleep-power.md).
   And "Flatpak Steam is not supported" was incomplete: the sandbox is not the obstacle, the missing
   pieces are per-game launch options and the shim inside the prefix — [`hw/flatpak-xmx.md`](hw/flatpak-xmx.md).
7. **The `max_hw_sleep` "anomaly" was an ABI/unit misread, not a kernel bug.** The tuple
   `success=0` next to `max_hw_sleep=523986009990` looked internally inconsistent. It is not:
   `max_hw_sleep` is a probe-time reporting ceiling (`(2^32-1) x 122` **microseconds** = about
   6.06 days on this platform) and is not a measurement; the old "~524 s" reading treated
   microseconds as nanoseconds; `success`/`last_hw_sleep`/`total_hw_sleep` are boot-local and
   never persisted, and `CLOCK_BOOTTIME - CLOCK_MONOTONIC` was 0.000 s at 16.4 h uptime. Now
   documented in [`hw/sleep-power.md` §2.6](hw/sleep-power.md), reported as `UNKNOWN` by
   `sleep.stats`, and explained by the `sleep.stats-inconsistent` triage rule. The kernel and ABI
   quotes behind it are in [`hw/sleep-power.md` §2.6](hw/sleep-power.md) (read from the local kernel
   tree, which stays out of the repo).
8. **Corrections made *while writing this file*** (small, factual, no behaviour change): the
   `hw/sleep-power.md` baseline row still said "has not suspended once" (now cites the 9-entry count and
   the `-k`/`-b` trap); `hw/sagv.md` §5.4 referred to "a device that never suspended"; `docs/TESTING.md`
   §3/§9 had suite counts that had drifted. Those counts are now generated in §2 instead of copied
   into prose, and the dated tables in `TESTING.md` are kept only as a record.

## 5. The owner's next three actions (exact commands)

**A1 — install the installable `awaiting-install` components and clear the three failing checks.**
This covers `volume`, `leds`, `rgb`, `sleep`, `bluetooth`, and the `daemons` lifecycle that ships
inside them; `sched-ext` needs `oxp3-fixes-fast.sh install` **and** `scx_cosmos` present, and
`adaptive-tdp` is opt-in by design. Review, then run; the only disruptive step is the
reboot (initramfs + module reload), and `--uninstall` reverses the rest.

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes
sudo tools/oxp3-hw-install.sh --dry-run --sleep --leds --power --rgb --volume   # review, writes nothing
sudo tools/oxp3-hw-install.sh --sleep --leds --power --rgb --volume
sudo systemctl restart systemd-logind            # the logind drop-in is read at logind start
sudo tools/oxp3-bt-firmware.sh                   # pinned files + dracut conf + rpm-ostree initramfs --enable
# reboot, then:
tools/oxp3-verify.sh                             # bt.journal / bt.controller / bt.diag should clear
```

`--volume` is the standalone daemon and **conflicts with the hhd volume-rocker patch** — pick one
([`INSTALL-HW.md` §3](INSTALL-HW.md)). `--audio`, `--fan-curve`, `--adaptive-tdp`, `--battery-limit`
are the opt-in extras and are *not* implied by the above ([`INSTALL-HW.md` §2.3](INSTALL-HW.md)).

**A2 — produce the device transcripts nothing in the repo can prove** (root-only / physical). Record
the outputs under `docs/state/` and update the matching `last_validated` in `components.yaml`.

```sh
sudo OXP3_ALLOW_SUSPEND=1 tools/oxp3-verify.sh --full --with-changes   # sleep.suspend, vol.hold, led.write, fan.savemode
sudo tools/sleep-power-measure.sh suspend -w 600                       # bag-drain %/h; run on battery
sudo tools/oxp3-voltap/oxp3-voltap selftest                            # a hold = one step, not a ramp
# fingerprint probe: read-only queries, manual only, needs spidev bound + root; steps in docs/hw/fingerprint.md
python3 -m unittest discover -s tools/fingerprint/tests                # offline, already green; prints its own count
tools/fingerprint/tests/run-c-tests.sh                                 # 324 checks / 10 vectors (podman fallback)
```

**A3 — make the two decisions the repo cannot make for you.** Both are prepared, not applied.

```sh
# licence: pick A (permissive tools + LGPL fingerprint subtree) or B (one copyleft id); see PROVENANCE.md §7
tools/apply-license.py --check --spdx MIT --except 'tools/fingerprint/**=LGPL-2.1-or-later'
tools/apply-license.py --write --spdx MIT --holder "silentsudin" --except 'tools/fingerprint/**=LGPL-2.1-or-later'
git diff --stat && tools/repo-test.sh --fast
# upstream: nothing has been submitted; three drafts, each lists the evidence still missing —
#   docs/upstream/README.md (+ upstream/*.md). Submitting means editing the status line there.
```

## 6. What a future round should do next (ranked: value / effort / risk)

| # | action | effort | risk | unblocks |
|---|---|---|---|---|
| 1 | Run A1, then re-record `docs/state/` snapshots | S | low (reboot) | the installable components leave `awaiting-install` (plus `sched-ext` if `scx_cosmos` is installed); the recorded harness failures clear |
| 2 | Run A2 transcripts and update `components.yaml` `last_validated` | S-M | low (physical) | turns "waiting for install" into "validated on device"; `sleep.suspend`, `vol.hold`, `led.write` |
| 3 | Apply the licence choice (A3) and commit | S | none functional | removes the only DECISION PENDING in [`QA.md`](QA.md); makes reuse terms explicit |
| 4 | Fix the two stale texts still visible: the Decky/status hint `"Flatpak Steam is not supported by the XMX project"` (`tools/decky-oxp3-status/main.py:1654`) contradicts [`hw/flatpak-xmx.md`](hw/flatpak-xmx.md); annotate `hw/gap-audit.md` §1 as superseded on the Bluetooth row | XS | none | stops new sessions re-learning the corrected facts |
| 5 | Flatpak Steam end-to-end trace (needs one Proton prefix + one launch with `XESS_LOG=1`); decide native-only otherwise | M | low (user-space, `$HOME` only) | closes the last XMX open item; `gfx.xmx.trace`, `flatpak.steam_env` |
| 6 | Port the CM series to whatever Mesa the installer pins and re-verify `gfx.xmx.driver` (`4b1d55ea` is pinned today) | M | medium (driver rebuild + re-measure) | XMX stops being `partial` in the registry sense |
| 7 | Fingerprint: get a hardware transcript, then finish the libfprint driver (match-in-sensor, host stores a handle) | L | medium-high (hardware, root, SPI) | the only genuinely unimplemented hardware feature |
| 8 | SAGV: send the `intel_pmdemand` draft when the point→frequency mapping is measured | L-XL | high (kernel work, needs root reboots) | the one `blocked` component |
| 9 | Re-assert the driver stack after the next Anatase update, twice if the kernel changed | S | low | keeps `working` components working ([`INSTALL-HW.md` §2.4](INSTALL-HW.md)) |

## 7. How to work in this repo

### Truth, generation, and the loop to run at the end of every round

* Per-component facts live **only** in [`hw/components.yaml`](hw/components.yaml); the generated
  tables in `README.md`, `hw/README.md` and `INSTALL-HW.md`, plus the §2 roll-up here, are generated. Never
  hand-edit a `<!-- BEGIN GENERATED: … -->` block. Add or change a fact, then
  `tools/gen-docs.py --write`, then commit both.
* Adding a `record <id>` to `tools/oxp3-verify.sh` requires owning it in some component (or listing it
  in `unmapped_checks` with a reason) or `tools/gen-docs.py --check` fails — that is the intended drift
  alarm ([`hw/REGISTRY.md` §2](hw/REGISTRY.md)).
* End-of-round: `tools/gen-docs.py --check && tools/repo-test.sh --fast` (the `gen-docs` suite is part
  of it). For device work add `tools/oxp3-verify.sh`.

### Tooling

| tool | use |
|---|---|
| `tools/repo-test.sh [--fast\|--full\|--only GLOB\|--list\|--json]` / `make test` | every offline check; skips are explained, never silent; logs in `/tmp/oxp3-repo-test/<run>` |
| `tools/oxp3-verify.sh [--json] [--full] [--with-changes]` | hardware acceptance: the declared id count is in the §2 block; `PASS/FAIL/SKIP/UNKNOWN`; default is read-only; `--with-changes` (needs `--full`) makes exactly two persistent changes; only `sleep.suspend` needs `OXP3_ALLOW_SUSPEND=1` |
| `tools/oxp3-preflight.sh [--json] [--strict] [--component NAME]` | read-only prerequisite verdict before an install; the installer refuses on FAIL unless `--force` ([PREFLIGHT.md](PREFLIGHT.md)) |
| `tools/oxp3-status.sh [--json\|--strict\|--selftest]` | same backend as the Decky panel |
| `tools/oxp3-hw-install.sh --status\|--dry-run\|--root DIR\|--uninstall` | the hardware entry point; `--root` writes into an offline prefix |
| `tools/ghidra-pod.sh <agent-name> <binary> [regex]` | isolated headless Ghidra in podman; dump at `~/.cache/ghidra-agents/<agent-name>/out/decomp.txt`; one **unique** name per agent/task so project dirs never collide, and never commit the dump |

`make help` lists the short targets. Parallel agents are expected: use a distinct name in
`tools/ghidra-pod.sh` and in commit messages, keep write scopes disjoint, and re-run
`gen-docs --check` after touching any doc that contains a generated block.

### The no-vendor-code rule (and where it is enforced)

The tree contains only our own code, our own prose and derived data — no vendor binary, firmware,
decompiler dump, string table or copied source. Enforcement: [`PROVENANCE.md`](PROVENANCE.md) (policy,
per-category rules, examples), `tools/provenance-audit.py` (must stay at **0 hard** findings; `--all`
classifies every file), `tools/apply-license.py` (headers, once the licence is chosen), the pinned
SHA-256 download tables for firmware/XeSS payloads, and `.gitignore` for caches/keys. Patch files in
`patches/**` are third-party context and keep their upstream licences. When you learn a behaviour from
a vendor artefact, record the *fact* and the observation method in the matching `docs/hw/` topic doc —
never the bytes.

### Traps we hit (each one cost a session)

| trap | what happened | rule |
|---|---|---|
| **PipeWire card wedge** | a throwaway PipeWire instance grabbed the SOF speaker PCM; the card went `Dummy Output` until reboot (`snd_sof` NULL-deref, `D`-state process) | isolate experiments with `PIPEWIRE_RUNTIME_DIR` ([`hw/README.md`](hw/README.md), [`hw/audio-harman-apo.md`](hw/audio-harman-apo.md)) |
| **`eventN` reuse** | daemons keyed on `/dev/input/event23` grabbed the wrong hardware after a re-enumeration | discover by name/phys/vendor/product + `HID_ID`, treat `N` as a scan result only ([`DAEMONS.md` §1.1](DAEMONS.md)) |
| **`journalctl -k` implies `-b`** | "never suspended" came from a command that could not see any other boot | use `journalctl -b all -k` for history; this is the bug behind correction 1 |
| **HHD grabs the volume device** | `atkbd`'s AT keyboard (`event4`) is grabbed by hhd (`grab=True`), so the desktop only ever sees hhd's uinput `event23`; a fix "when hhd is running and patched" is fragile | prefer the hhd virtual keyboard, use the physical one only when free, and honour the "pick one path" rule ([`hw/volume-buttons.md`](hw/volume-buttons.md)) |
| **`ConditionPathExists` masking** | a unit with `ConditionPathExists=/dev/uinput` failed once at boot and stayed dead with **no restart** instead of retrying | conditions gate *start*, not health; the daemons retry in-process and the units use `Restart=always` ([`DAEMONS.md` §1.5](DAEMONS.md)) |
| **Obfuscated .NET assemblies** | `CompatLayerCT.exe` metadata parses but 4,942 methods share 648 RVAs (2,829 point at one decoy body); Ghidra reports `functions=0` for a pure CLR image, and no .NET decompiler is installed | recover protocol facts from device traffic/JS profiles, not from the IL; do not execute the obfuscated code ([`hw/rgb-coverage.md`](hw/rgb-coverage.md)) and note the gap in the upstream drafts |

Two more that are cheap to forget: `--sides 01` raises `ValueError` (ids are parsed with `int(x, 0)`,
use `--sides 6`), and `/usr` is an ostree overlay — anything installed there is gone on the next
deployment, so keep persistent work in `/etc`, `/usr/local` or `$HOME` ([`INSTALL-HW.md` §0](INSTALL-HW.md)).

## 8. Open points (no questions asked; these are for the owner)

* **`/etc/oxp3/oxp3-helper`** (root-owned, 2,086 B, dated Oct 12) exists on the host and is **not in
  this repository**: a narrow whitelist helper (`param`, `getparam`, `dpcd`, `debug`) whose header says
  it is allowed through `/etc/sudoers.d/oxp3-helper`. Its sudoers grant could not be read
  unprivileged. Decide whether to adopt it into the repo (with review) or retire it.
* **Decky/status hint is stale** for Flatpak Steam (`main.py:1654`) while `hw/flatpak-xmx.md` implements
  the route; cosmetic, but it contradicts a doc.
* **`/usr` overlay vs `LocalPackages`**: `kmod-oxp3`/`oxp3-firmware` show as *LocalPackages*, not
  *LayeredPackages*; re-check after the next Anatase update that both still apply, and note that the
  docs' "layered" wording covers the module path, not an rpm-ostree layer.
* **`sleep.stats` and `xmx.trace`** are `SKIP`/`UNKNOWN`/`warn` by design until a real suspend and an
  igdext trace exist (`sleep.stats` says "max_hw_sleep is a ceiling, not a sleep" on a boot with no
  suspend, and is `UNKNOWN`, not a pass or a fail, in that state); do not "fix" them with a
  config edit.
* **`hw/gap-audit.md`** is the earliest audit and is superseded on the Bluetooth row; its rankings are
  still useful, its baseline table is not current.
* Root-only claims in the topic docs (`spi` clock/GPIO, `lspci -vv` L1 substates, S0ix residency) are
  marked as unverified; they remain so. No reboot was performed and nothing on the host was changed
  while producing this document.
