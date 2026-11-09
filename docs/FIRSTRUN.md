# First run after the reboot — checklist and one-shot evidence bundle

Date: **2026-11-09**. Scope: `tools/oxp3-firstrun.sh`, `tools/oxp3_firstrun.py`,
`tests/unit/test_firstrun.py`, the `firstrun` suite in `tools/repo-test.sh`.

This is the **one command the owner runs after rebooting** once the hardware fixes are
installed. It answers two questions in a single shot:

1. *Is every install step actually in effect on this boot?* — an ordered checklist
   with the live state of each step (and the exact fix command for the ones that are
   still open).
2. *What evidence does a later session need?* — `--collect` writes a redacted bundle
   (verifier + status + unit/udev/firmware/power/sleep/session/audio/display/XMX/decky
   state + bounded journals) that can be handed back as-is, with no further round trips.

The tool is **strictly read-only**. It never writes outside its own bundle directory,
never starts/stops/restarts a unit, never touches udev, sysfs, the EC or the initramfs,
**never suspends**, and never asks for root. Every privileged step below is a documented
copy-paste command, not something the collector does.

Related: [INSTALL-HW.md](INSTALL-HW.md) (the install guide), [VALIDATION.md](VALIDATION.md)
(what each verifier row proves), [hw/sleep-power.md](hw/sleep-power.md) (the suspend
policy this round is really about), [PROVENANCE.md](PROVENANCE.md) (no vendor blobs).

## 1. The sequence (what to run, in order)

```sh
cd /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes

# 1. install the hardware fixes (root; one command)
sudo tools/oxp3-hw-install.sh --all

# 2. the logind drop-in is read at logind start -> restart it (or reboot, step 3)
sudo systemctl restart systemd-logind

# 3. reboot (required for the Bluetooth initramfs and for a clean pre/post comparison)
sudo systemctl reboot

# 4. after the reboot, prove sleep once (this is the only step that suspends; run it
#    yourself, from a console or over SSH -- the collector will not do it)
sudo systemctl suspend            # then wake it; or: sudo tools/sleep-power-measure.sh suspend -w 600

# 5. one command: checklist + evidence bundle
tools/oxp3-firstrun.sh --collect
```

Then hand back the directory it prints (section 6). The checklist alone is free and
safe at any time:

```sh
tools/oxp3-firstrun.sh                # same as --check-only
tools/oxp3-firstrun.sh --check-only --json
```

`--collect` runs as your user. Root is only needed for a few probes (DMI serials,
`/proc/acpi/wakeup` contents in some setups, a journal your user cannot read); when one
of those is unavailable the row is reported as `denied`/`unavailable` with the reason
instead of failing. If you do run it under `sudo`, the collector switches to the
invoking user's home (`SUDO_USER`) so the user-scope files are still found, and it
records `"root": true` in the manifest.

## 2. The checklist

Every row is a read-only probe; the state vocabulary is:

| state | meaning |
|---|---|
| `ok` | the step is present and in effect |
| `todo` | not installed (or installed but off); the command column has the fix |
| `partial` | some of the pieces are present (e.g. firmware but no initramfs drop-in) |
| `pending` | installed but needs one documented action to take effect (e.g. a logind restart after the drop-in) |
| `unknown` | readable but not assertable here (no user bus, unreadable timestamp, …) |
| `n/a` | this machine has no such interface |

Rows, in install order: driver stack · hardware install (libexec helpers) · preflight
verdict (`tools/oxp3-preflight.sh`, read-only: safe / partial on warnings / todo on
blockers) · udev rules +
volume hwdb · sleep trigger policy (logind + polkit) · logind restarted after the drop-in
· suspend units enabled · Bluetooth firmware + initramfs + `hci0` · saved power profile ·
battery care · IMU blacklist · speaker EQ · RGB config · home-button unit · display
(HDR/ICC/vibrance) · XMX driver + ICD + session env · Decky plugin · a real suspend
recorded since boot · the bundle itself.

The `preflight` row is the install-prerequisite verdict from
[PREFLIGHT.md](PREFLIGHT.md): `ok` when every prerequisite is satisfied or only
diagnostics warnings remain, `partial` when a warning leaves a degraded path, `todo`
when an install would be blocked. It is read-only and is reported as `unknown` in the
collector's fixture mode.

`logind-restart` is the interesting one for this round: the drop-in's mtime is compared
with `systemd-logind`'s `ActiveEnterTimestamp`, so "installed but not in effect" is a
visible `pending` row instead of a mystery.

## 3. What `--collect` gathers

Default output: `docs/state/firstrun-<YYYY-MM-DDTHHMMZ>/` (git-ignored). Override with
`--out DIR`. The bundle is a plain directory of text/JSON files:

| path | contents |
|---|---|
| `MANIFEST.json` | machine summary: schema, mode, counts, per-artifact status/bytes/lines/truncation/exit code, the checklist, redaction counts, limits, journal availability, notes |
| `checklist.txt` | the checklist exactly as printed at collection time (including which steps were still open) |
| `README.txt` | what the directory is, how to hand it back, how a later session reads it |
| `REDACTIONS.txt` | which redaction rules fired, how often, and in which files (counts only) |
| `verify/verify.json`, `verify/verify.txt` | `tools/oxp3-verify.sh --json` and the human report (the acceptance rows) |
| `status/status.json`, `status/hw-install-status.txt` | `oxp3-status.sh --json` and `oxp3-hw-install.sh --status` |
| `state/snapshot.txt` | `state-snapshot.sh` (environment + hardware-fix state) |
| `units/system.txt`, `units/user.txt` | `systemctl show` for every oxp3 unit (system + user), `--failed`, `list-unit-files 'oxp3-*'`, user-scope file hashes |
| `udev/rules.txt` | installed udev/polkit/logind/modprobe/dracut files with sha256, the volume hwdb, `hwdb.bin` mtime, `udevadm verify` on the installed rules |
| `firmware/bluetooth.txt` | the two pinned Bluetooth firmware files as **path + size + sha256 only**, module state, `hci0`, the dracut drop-in body |
| `power/power.txt` | platform profile + choices, every RAPL zone (MSR + MMIO constraints, tau, energy), battery/AC fields, thermals, `hwmon` fan state, the saved `power.json` |
| `sleep/sleep.txt` | `mem_sleep`/`state`/`disk`, all `suspend_stats` counters, enabled ACPI wake sources, `/sys/class/wakeup` counts, the repo's read-only sleep audit |
| `session/power.txt` | `loginctl show-session self` (fallback: the seat session id) for `Type`/`Class`/`Active`/`IdleHint`/`State`, `loginctl list-sessions`, the PowerDevil `PowerButtonAction`/`LidAction`/`AutoSuspend`/`AutoSuspendAction` values for AC and Battery read with `kreadconfig6` (and the Plasma 6 nested `[AC][SuspendAndShutdown]` group), a bounded ≤60-line `~/.config/powerdevilrc` excerpt, and whether a session polkit agent (`polkit-kde-authentication-agent-1` / `polkit-gnome-authentication-agent-1`) is running |
| `audio/audio.txt` | ALSA/SOF cards, `pactl info`/sinks/default sink, filter-chain modules, the EQ drop-in (hash + body), audio user units, PipeWire/WirePlumber versions |
| `display/display.txt` | `display-mode.sh status` (ANSI stripped) and `vibrance.sh status`, ICC profile directories |
| `gfx/xmx.txt` | the session's graphics environment (allowlisted variables), sha256 of the host/XMX drivers, per-game ICD files, kernel-cache inventory, IGC `ocloc` (path + hash, not executed), Steam Flatpak overrides |
| `decky/decky.txt` | `~/homebrew/plugins` inventory, `oxp3-status` plugin files + version, `plugin_loader` state, built zip |
| `system/environment.txt` | kernel/OS/arch, uptime, git head/branch/dirty state, `/proc/cmdline`, DMI model strings (serials redacted), xe module parameters |
| `system/hardware.txt` | `lspci -nn`, `lsusb`, `lsusb -t` |
| `repo/repo-test-fast.txt` | the tail of `tools/repo-test.sh --fast` |
| `journal/oxp3-units.txt` | `journalctl -b` for every oxp3 unit (≤600 lines) |
| `journal/oxp3-user-units.txt` | the user units' journal (≤400 lines) |
| `journal/kernel.txt` | kernel lines matching bluetooth/hci/suspend/PM/audio/SOF/GPU/drm/thermal/thunderbolt/typec/HID (≤800 lines) |
| `journal/sleep-tag.txt` | the `oxp3-sleep` audit tag (≤200 lines) |

The two JSON files are stored verbatim (no comment header) so they can be parsed
directly; the manifest records each tool's exit code (e.g. `oxp3-verify` exits 1 when a
check FAILs — that is data, not a collection failure).

## 4. Safety, limits and idempotency

```sh
tools/oxp3-firstrun.sh --collect --dry-run      # run every probe, write nothing
tools/oxp3-firstrun.sh --collect --no-journal   # journals recorded as skipped
tools/oxp3-firstrun.sh --collect --no-repo-test # skip the (slow) repo-test tail
tools/oxp3-firstrun.sh --collect --out DIR      # explicit destination
tools/oxp3-firstrun.sh --collect --json         # MANIFEST on stdout
tools/oxp3-firstrun.sh --check-only             # checklist only
tools/oxp3-firstrun.sh --selftest               # offline self-test (fixtures)
```

* **Total cap**: 5 MiB of file content (`--max-bytes`, minimum 32 KiB). Each artifact is
  fitted into the remaining budget; a cut artifact gets a visible
  `[truncated: bundle size budget]` marker, and an artifact that cannot be cut safely
  (a JSON file) is skipped instead of corrupted. The manifest records every truncation
  and skip, and the cap is enforced *including* the index files.
* **Per-file line cap**: 2000 lines (`--max-lines`), plus tighter per-artifact caps for
  the journals (600/800/400/200) so a noisy boot cannot eat the whole budget.
* **Per-command output cap**: 2 MiB, with a truncation marker.
* **Idempotent**: re-running with the same `--out` rewrites the same file set with the
  same bytes (verified by a unit test). It never deletes unrelated files, and it refuses
  a non-empty `--out` without `--force` so two runs cannot silently mix.
* **Exit codes**: `0` checked/collected (missing optional data is normal), `1` a real
  collection failure (bundle or manifest could not be written, or nothing usable was
  collected), `2` usage error, `3` the backend could not start (no `python3`, missing
  `tools/oxp3_firstrun.py`).
* No `sudo`, no reboot, no suspend, no unit/udev/sysfs/timer writes, no network. The
  only directory created is the bundle directory; the only other writes are the ones
  `repo-test --fast` already makes in its own log directory under `/tmp`.

## 5. Redaction guarantees

Redaction runs on **every byte before it is written**, including the manifest and the
README, so a collector that forgot a path cannot leak it.

| rule | what is replaced | replacement |
|---|---|---|
| `identity` | the invoking user's name, home directory (`/var/home/…` and `/home/…`), hostname (and its short form) | `<user>`, `~`, `<host>` |
| `mac` | MAC addresses, colon or dash form | `<mac-oui:aa:bb:cc>` (only the vendor OUI is kept, for hardware identification) |
| `serial-number` | `serial`/`serial number`/`S/N` values | `<redacted:serial>` |
| `uuid` | `uuid=`/`serial=`/`partuuid=`/`wwn=` values **and every bare UUID** (KScreen connector UUIDs, boot ID, disk IDs) | `<redacted:uuid>` |
| `email` | e-mail addresses (whole address, domain included) | `<email>` |
| `cookie` | PulseAudio `Cookie:` lines | `<redacted:cookie>` |
| `token:*` | AWS access-key ids, GitHub/GitLab/Slack tokens, `sk-…` keys, JWTs, `Bearer …` | `<redacted:<family>>` |
| `secret-assignment` | any `*token*`/`*secret*`/`*password*`/`*api_key*`/`*credential*` assignment | `<name>=<redacted:secret>` |

Deliberately **kept** (they identify bytes or hardware, not people): sha256 digests, PCI
and USB ids, kernel/OS versions, DMI vendor/product names, power/thermal/battery values,
`uptime`, and the vendor OUI of MAC addresses. Everything is idempotent and tested:
applying the redactor twice changes nothing.

`REDACTIONS.txt` and `MANIFEST.json` record only **rule names and counts per file**; the
replaced values are never stored anywhere. Residual, deliberate disclosures: the numeric
UID appears in `/run/user/1000/...` paths from `pactl`, and journal lines from our own
units carry timestamps — neither identifies a person.

## 6. Handing it back

```sh
# the path is printed at the end of --collect; the README repeats this line
tar -C /var/home/holzr/git/linux_scratch/oxp3-anatase-fixes/docs/state \
    -czf /tmp/oxp3-firstrun.tgz firstrun-2026-11-09T0410Z
sha256sum /tmp/oxp3-firstrun.tgz
```

Send the archive (or the directory) as-is. If the journals feel too personal, delete
`journal/` first — `MANIFEST.json` still records that they existed and why they were
skipped. For a machine-only hand-back without a directory, `--collect --json` prints the
manifest and `--out /tmp/x` keeps the files out of the checkout.

Nothing in the bundle is a binary, a firmware image or vendor code: firmware appears as a
path, a size and a sha256, exactly as [PROVENANCE.md](PROVENANCE.md) requires.

## 7. How a later session should read it

**Shortcut:** run `tools/oxp3-triage.py <bundle>` first — it reads every file below,
derives a per-fix verdict and a ranked action list, and cites the file and line for each
finding ([TRIAGE.md](TRIAGE.md)). Read the rest of this section when the triage says
`unknown` or when you need the raw bytes.

1. `MANIFEST.json` — `counts` (what was readable), `checklist` (what was still open),
   `artifacts[*].status/exit_code/truncated` (what to trust), `redaction` (what was
   removed), `journal.available` (whether the journal rows are meaningful).
2. `checklist.txt` — the install state at that boot; compare with the current one.
3. `verify/verify.json` — the acceptance rows (`id`, `state`, `summary`, `detail`,
   `remediation`); this is the same data `docs/VALIDATION.md` documents.
4. `units/system.txt` + `journal/oxp3-units.txt` — did the units load, and what did they
   log on this boot (resume, hotplug, backoff, real fd)?
5. `power/power.txt` — live PL1/PL2/PL4 on both RAPL zones, battery limit/behaviour,
   fan/thermal state; compare with the saved `power.json` body in the same file.
6. `sleep/sleep.txt` + `session/power.txt` + `journal/kernel.txt` — `suspend_stats`,
   armed wake sources, who owns `handle-power-key`, and what PowerDevil is set to do with
   the key and the lid. A missing `PowerButtonAction` means PowerDevil's built-in default
   applies (the bundle cannot read it); a value other than `1` (Sleep) is the finding.
   `success=0` means the suspend test had not happened yet, not that suspend is broken.
7. `audio/audio.txt`, `display/display.txt`, `gfx/xmx.txt`, `firmware/bluetooth.txt` —
   the environment-specific half: filter chain, HDR/ICC, the session's XMX wiring, and
   whether the initramfs carries the Bluetooth firmware.
8. `system/environment.txt` + `repo/repo-test-fast.txt` — kernel/cmdline/git state so a
   diff between two bundles is meaningful.

Open points to check first when something looks off: `logind-restart: pending` (drop-in
not read yet), `hci0` absent with firmware present (initramfs not rebuilt/reloaded),
`success=0` (no suspend test), `partial` rows (half-installed component), and journal rows
with `status: denied` (re-run as a user in `systemd-journal` or under `sudo`).

## 8. Tests

* `python3 tools/oxp3_firstrun.py --selftest` — 88 checks over synthetic fixtures and
  stub commands: the redaction corpus (every rule + idempotency + sha256 preservation),
  the line/byte budget, a full bundle in a temporary directory (dry-run writes nothing,
  the manifest stays valid and redacted, a second run is byte-identical), the
  `session/power.txt` blocks (logind, PowerDevil, powerdevilrc excerpt, polkit agent),
  and the checklist state machine (partial/pending/ok transitions).
* `python3 -m unittest discover -s tests/unit -p 'test_firstrun.py' -q` — the same seams
  as unit tests, plus the collector inventory, degraded-status paths and the CLI contract.
* `tools/repo-test.sh --fast --only firstrun` — the suite in the repository runner:
  self-test, a valid `--check-only --json`, and a `--dry-run` that must create nothing.

Validated on this host without any privileged action: `--check-only`, `--dry-run`, and
`--collect --no-journal` into a temporary directory (journal collection was then verified
separately with a full `--collect`, which stays under the cap).

## 9. Known limitations

* Root-only data is reported, not forced: DMI serials (redacted anyway), some
  `/proc/acpi/wakeup` states, and `lsinitrd` contents of the running initramfs (the
  bundle records the dracut drop-in and the two firmware files instead).
* A journal the user cannot read is `denied` with the reason; the rest of the bundle is
  still complete.
* The session environment (`gfx/xmx.txt`) is the environment of the shell that ran the
  collector. Launch it from the desktop session (or inside the game session) if you want
  the launcher's variables; a Steam launch's variables are not visible from here.
* User units need a user systemd bus; over SSH without `XDG_RUNTIME_DIR` they are
  reported from the filesystem (`unknown`), not guessed.
* The Decky inventory reflects `~/homebrew/plugins`, the loader path on this host.
