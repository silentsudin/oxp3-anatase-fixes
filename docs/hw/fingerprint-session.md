# Fingerprint bring-up session: one guided, reversible root runbook

Status: **the runbook is written, its state machine and rollback are tested
offline; it has never run against the sensor.** What it replaces:
`docs/hw/fingerprint-transport.md` §9 step 1-5 was a hand-run sequence (dump
DSDT, `iasl -d`, copy the `_CRS` bytes out of the disassembly by hand, modprobe
spidev, write `driver_override`, bind, dry-run, read, unbind, unload) with the
rollback kept in the operator's head. That is easy to get wrong and hard to
reproduce. `tools/fingerprint/oxp3-fp-session.sh` performs the same procedure
as a state machine: it checks the preconditions, extracts and parses the
`_CRS` itself, shows every command before running it, asks before every
mutation, records what it changed *before* changing it, and restores the
previous binding on exit, on error and on Ctrl-C - then verifies the restore.

Owner: silentsudin. Host: Anatase 20261026.3, kernel 7.2.7-an28.fc45.
Companions: [`fingerprint.md`](fingerprint.md) (protocol, command set),
[`fingerprint-transport.md`](fingerprint-transport.md) (§4 resources, §8-§9
transport and the manual procedure), `tools/fingerprint/README.md`.

## 1. The exact command

From the repository root, as root (the runbook binds a kernel driver and the
DSDT is `0400 root`):

```sh
sudo tools/fingerprint/oxp3-fp-session.sh
```

Look at the plan first if you want; this mutates nothing:

```sh
tools/fingerprint/oxp3-fp-session.sh --dry-run          # whole plan + preconditions
tools/fingerprint/oxp3-fp-session.sh --non-interactive  # same, plus copy/paste commands
```

Other entry points: `--resume [DIR]` (continue a session that was killed or
lost its terminal), `--selftest` (offline, no root), `--version`, `--help`.
Useful flags: `--out DIR` (transcript location), `--reads "fw info"` (narrow
the allowlisted reads; default `fw scheme info`), `--no-irq` (never request the
`GpioInt`), `--allow-reset` (permit the probe to pulse a `GpioIo` reset line -
off by default, never implied, and inert unless `_CRS` actually has a `GpioIo`;
it is then the only action in the runbook that is not a read, and the plan and
the command say so), `--allow-existing-bind`,
`--try-matrix` (one bounded read-only experiment: reset polarity
active-low/active-high/hold x settle 10/150 ms x IRQ edge rising/falling/both;
needs `--allow-reset`, an IRQ pin and a reset pin; the table is written to
`matrix.json`), `--try-write-shape` (the follow-up experiment: selector bit 0
x write padding after the selector x read length, for `0xA0` then `0xA1`/`0xC1`;
needs `--allow-reset`, an IRQ pin and a reset pin; the table is written to
`write-shape.json`), `--reset-polarity active-high|active-low` and
`--settle-ms N` (the now-known-good electrical settings; defaults
active-high/10 ms), `--allow-takeover`, `--crs FILE` (use an already-extracted
resource template),
`--gpio N` / `--irq-edge E` / `--gpio-chip NAME` (override the `_CRS` value
for an experiment; the plan prints what it will use), `--irq-pin N` /
`--reset-pin N` (the runtime `_INI`/`GNUM` lines; the template has 0), and
`--gpio-backend auto|gpiod|chardev|sysfs|poll` (force the GPIO backend; the
default also honours `$FPBF63112_GPIO_BACKEND`).

### Next owner command (normal read-only run, not a matrix)

The electrical side is pinned and the read frame is recovered
(`fingerprint-transport.md` §8.9): active-high reset, 10 ms settle, rising
edge, 4 MHz, mode 0, and the probe's default `--read-shape frame` (selector +
the measured lead bytes). The next run is the ordinary read, not another
experiment:

```sh
sudo tools/fingerprint/oxp3-fp-session.sh --reads "fw scheme info" \
    --reset-polarity active-high --settle-ms 10
```

If the session wrapper cannot run on the host, the equivalent direct probe
command (the one the wrapper prints) is:

```sh
sudo python3 tools/fingerprint/oxp3-fp-probe.py --spidev /dev/spidev0.0 \
    --speed 4000000 --mode 0 --bits 8 --reset-polarity active-high \
    --settle-ms 10 --gpio 18 --gpio-chip gpiochip1 --irq-edge rising \
    --reset-gpio 16 --allow-writes --allow-reset fw scheme info
```

It sends only `0xA0`, `0xA1` and `0xC1`, keeps the read allowlist and every
safety gate, and pulses the reset only because `--allow-reset` is explicit.
Expected on success: `fw` decodes the 7-byte record (`state`, project
`0x7F50`, version), `scheme` returns its table and `info` the 9-byte geometry.
`--read-shape selector` reruns the DLL-exact one-byte frame for comparison and
is expected to leave the payload 3/2 bytes into the window.

Exit codes: `0` done and restore verified, `1` a step failed (the restore still
ran and is reported), `2` usage or precondition refusal (nothing was changed),
`3` the restore could not be verified (**check `summary.json`, then restore by
hand with the commands printed at the end**), `130` interrupted.

## 2. What it does, step by step

Each phase prints what it is about to do and the exact command; every mutation
needs its own `[y/N]` answer and is journaled before and after. A declined
answer stops the run with nothing changed (or with the already-made mutations
restored).

| # | phase | what happens | mutates? |
|---|---|---|---|
| 1 | **preconditions** | root check; the probe is this repository's file (not a symlink, not edited: marker + compiles, invoked as `python3 -I -B`); `fpbf63112.acpi/aml` import; `ACPI\BF63112` device present and `hid` matches; the `spi-BF63112:*` device exists; spidev loaded or resolvable for this kernel; **which driver is bound**; no other process holds the sensor's `/dev`/sysfs nodes; a session lock (so two runbooks cannot interleave) | no |
| 2 | **extract `_CRS`** | read `/sys/firmware/acpi/tables/DSDT` (or `acpidump -b` when sysfs is unavailable, or your `--crs FILE`), then locate the FPRT `_CRS` resource template **in the raw AML** (`fpbf63112/aml.py`) and validate it by parsing it. `iasl`, when installed, only writes a readable FPRT excerpt to the transcript - it is never the source of the bytes | no (temp dir only) |
| 3 | **parse `_CRS`** | run the probe's own parser (`acpi_info --crs`) and print the interpreted values: SPI clock, mode, bits, chip select, `GpioInt` pin/edge/polarity and any `GpioIo` reset pin | no |
| 4 | **plan** | print every following command, then ask `Proceed?` | no |
| 5 | **bind** | print the commands, ask once (the takeover question names the driver being displaced), then `modprobe spidev` (only if not loaded), write `spidev` to `driver_override`, write the device name to `spidev/bind`; read every write back; discover the new `/dev/spidevN.M` from sysfs | **yes** |
| 6 | **dry run** | run the probe with `--dry-run`: the exact selector byte and two-entry transfer for each read, no device opened | no |
| 7 | **read** | print the exact gated command, then require the literal word `probe` (`skip` stops before touching the sensor; anything else refuses). Runs only `fw scheme info` (`0xA0`, `0xA1`, `0xC1`) unless `--reads` narrows it - never enrol/identify/remove/JTAG/restore/update. With `--try-matrix` the probe instead runs the 18-cell polarity/settle/edge matrix (table -> `matrix.json`); with `--try-write-shape` it runs the 60-cell selector/padding/read-length experiment (table -> `write-shape.json`) | talks to the sensor (read-only; both experiments also pulse the reset line, already gated by `--allow-reset`) |
| 8 | **restore** | see §3; then verify | **yes** |
| 9 | **summary** | `summary.json`, `README.txt`, the journal and the transcript are written | transcript only |

`--resume DIR` re-runs the preconditions (the host may have changed), reuses
the extracted `_CRS` and parse from that directory, recognises a bind that the
recorded session created, and finishes at the restore. It does **not** trust the
journal blindly: a device that is no longer bound by us is treated as external
and left alone.

## 3. What it changes - and how it comes back

| change | before | restore | verified by |
|---|---|---|---|
| load `spidev` | module not loaded | `modprobe -r spidev` **only if this session loaded it and no other SPI device is bound to spidev** | `/sys/module/spidev` gone |
| `driver_override` | recorded byte-for-byte before the first write (hex in `session.state`) | the previous content written back (empty string when the attribute was empty; note that sysfs has no "remove", so empty is the neutral value) | read-back compared |
| `spi-BF63112:00` driver | recorded (`none`, `spidev`, or another driver) | `unbind`, then re-bind the recorded driver if there was one | `driver` symlink compared with the recorded value |

Nothing else is touched: no file outside the transcript directory, no package,
unit, config, boot parameter or firmware. Two cases are deliberately **not**
automatic:

* **spidev was already bound before the session.** The runbook refuses unless
  `--allow-existing-bind` is given, then uses it as-is and never unbinds it; the
  summary marks `external_bind: true`.
* **another driver was bound** (e.g. a future `bf63112` in-tree driver). It
  refuses unless `--allow-takeover`, and then re-binds that driver on exit; if
  the re-bind fails, exit is `3` and the exact command is printed.

The restore runs from the `EXIT` trap (so failures, `set -u` errors, EOF on
stdin and a closed terminal are covered) and from the `INT`/`TERM`/`HUP` trap
(Ctrl-C), and it is idempotent: a session that already restored skips it. After
restoring, the script reads the live tree again and only reports success when
the driver and `driver_override` match the recorded pre-session values.

Manual undo, if you ever need it outside the script:

```sh
printf '%s\n' spi-BF63112:00 | sudo tee /sys/bus/spi/drivers/spidev/unbind
printf '\n' | sudo tee /sys/bus/spi/devices/spi-BF63112:00/driver_override
sudo modprobe -r spidev          # only if you loaded it
```

## 4. The transcript directory

Default `docs/state/fp-session-<UTC>/`, or `--out DIR` (a resumed session reuses
its directory). Machine-local evidence; nothing in it is secret (no key, no
firmware, no vendor binary), but it does contain your firmware table excerpt,
so treat it as host-specific data and do not commit it.

| file | content |
|---|---|
| `session.log` | the full console output, tee'd while the session runs |
| `journal.tsv` | append-only log, fields separated by `US` (`\x1f`): `utc-timestamp ␟ phase ␟ step ␟ status ␟ detail`. Phases: `session env crs parse bind dryrun probe answer mutation restore`. Statuses: `ok failed declined planned begin end skipped verified resumed interrupted` |
| `session.state` | write-ahead state, `KEY=value` lines: `DEV`, `ORIGINAL_DRIVER`, `PREV_OVERRIDE_HEX`, `BOUND_BY_US`, `SPIDEV_PATH`, `CRS_SHA`, `CONN_*`, `PROBE_RAN`, `ROOT` (fixture runs only). Written **before** the mutations it describes, which is what makes `--resume` and post-crash rollback possible |
| `dsdt.aml` | the table that was scanned (a copy, when readable) |
| `fprt-crs.bin` | the extracted `_CRS` resource template (the exact bytes) |
| `fprt-crs.asl` | iasl's FPRT excerpt, when iasl is installed (human-readable evidence only) |
| `acpi-info.txt` | the probe's `acpi_info` output: descriptors and the derived connection |
| `probe-dryrun.txt` | the probe's `--dry-run` byte plan |
| `probe-readonly.txt` | the device's answers (only when the word `probe` was typed) |
| `matrix.json` | the `--try-matrix` table: every cell (polarity, settle, edge), the bytes read, the verdict, the winning cell (or the all-attempts verdict); same content embedded in `summary.json` |
| `answers.tsv` | the parsed answers: command, byte count, hex, interpretation |
| `sys-before.txt`, `sys-after.txt` | driver / override / module / node / `bytes_tx`, before and after |
| `commands.txt` | the exact probe commands, for copy/paste |
| `summary.json` | what ran, what the device answered, what remains unknown (below) |
| `README.txt` | a one-screen index of the above |

`summary.json` (schema 1) top-level keys: `schema tool version unique_name
run_id started_at finished_at mode fixture root_prefix host transcript device
acpi binding mutations probe restore unknowns files`.

* `acpi`: `method` (`sysfs-dsdt`, `acpidump`, `crs-argument`, `resumed`),
  `crs_sha256`, `crs_bytes`, `speed_hz`, `mode`, `bits`, `chip_select`,
  `irq_pin`, `irq_edge`, `irq_active_low`, `reset_pin`.
* `binding`: `previous_driver`, `previous_override_hex`, `bound_by_session`,
  `external_bind`, `module_loaded_before/_by_session`, `irq_used`.
* `probe`: `ran`, `exit`, `dry_run_exit`, `reads`, `answers[]` with
  `command`, `bytes`, `hex`, `interpretation`.
* `matrix`: `null` unless `--try-matrix` ran, otherwise the complete
  `matrix.json` object (`outcome`, `verdict`, `planned`, `attempted`,
  `attempts[]` with `polarity`, `settle_ms`, `edge`, `bytes_hex`, `queries[]`,
  and `winning`).
* `write_shape`: `null` unless `--try-write-shape` ran, otherwise the complete
  `write-shape.json` object (`outcome`, `verdict`, `planned`, `attempted`,
  `cells[]` with `selector`, `padding`, `read_len`, `hex`, `aa_offset`, `crc`,
  `candidate_offset`, `verdict`, and `shift_summary`).
* `restore`: `attempted`, `verified`, `exit`, `driver`, `override`, `notes[]`.
* `unknowns[]`: the remaining open questions, computed from what actually
  happened (e.g. "no GpioInt in `_CRS`", "no firmware answer was parsed",
  "the SPI wire opcode set is unproven").

## 5. What a successful run looks like

The probe prints one block per read. The selector is `(opcode & 0x3f) << 1`:
`0xA0` -> `0x40`, `0xA1` -> `0x42`, `0xC1` -> `0x02`. A successful session ends
with:

```
Bind spidev (the only state change)
ok    spidev loaded by this session
ok    driver_override := spidev
ok    bind spi-BF63112:00 to spidev
ok    bound: spi-BF63112:00 -> spidev, node /dev/spidev0.0

Probe plan (no device opened)
fw: opcode 0xa0 selector 0x40 read 7 B, timeout 1000/2030 ms
    40 | <- 7 B
...

Read-only probe (the sensor is touched here)
fw: opcode 0xa0 selector 0x40 answer 7 B
    00 00 04 01 02 03 04
    fw=1.2.3.4 project=BOE MOC variant=4 mode=0 raw=01020304
scheme: opcode 0xa1 selector 0x42 answer 64 B
    04 00 ...
info: opcode 0xc1 selector 0x02 answer 9 B
    00 00 00 00 40 00 40 00 00
    fields a=0 b=0  sensor=64x64  image=4096 B  flags=0x00

Restore
ok    driver_override cleared
ok    spidev module unloaded again
ok    restore verified: driver=none override= module_loaded=0
```

**These bytes are the shape, not a captured transcript**: no `0xA0`/`0xA1`/`0xC1`
answer has ever been captured on this unit. Read them as follows (layouts from
`fingerprint.md`, recovered from the USB branch):

* `fw` (`0xA0`) must be **7 bytes**: `state:u8`, project `hi/lo` (the project
  table is BOE MOC / HUAHONG MOUSE MOC / DYW MOH / NATION TEST /
  NATIONALCHIP), then a 4-byte version. The probe prints
  `fw=<a.b.c.d> project=... variant=... mode=... raw=...`.
* `scheme` (`0xA1`) is a table; the probe reads 64 bytes because the wire has no
  length field. A non-zero, non-`0xFF` payload is the evidence that the
  transport works; the table's semantics are not decoded.
* `info` (`0xC1`) must be **9 bytes**: `a:u16`, `b:u16`, `width:u16 @4`,
  `height:u16 @6`, `flags:u8 @8`; the probe prints
  `sensor=<w>x<h> image=<w*h> B flags=0x..`. `width*height` is the image size a
  driver will need.

Failure shapes, and what they mean:

| what you see | most likely cause |
|---|---|
| `answer 7 B` then all `00` | device not driving the MISO line: wrong mode/clock (from `_CRS`, so unlikely but possible) or the answer needs the IRQ first |
| all `ff` | nothing on the bus: wrong chip select/controller, or the module is held in reset |
| `no answer (buf 0B)` / probe exit non-zero | the node exists but every read timed out; check `statistics/bytes_tx` in the transcript - if it advanced, the transfers happened |
| `refusing: opcode 0x.. is ...` | you tried a destructive opcode; the probe and the runbook both refuse by opcode *and* selector |
| `refusing: --spidev ... required` | the bind did not produce a node; the transcript's `sys-after.txt` shows what the tree looked like |

Whatever the outcome, the runbook restores the binding and says whether the
restore was verified - a failed read does not leave the machine altered.

### 5.1 The retry after the all-zero probe: reset 16 and IRQ 18

The 2026-11-09 run (`docs/state/fp-session-20261109T110322Z/`) bound spidev and
read `fw`, `scheme` and `info` as **all zeros**. That run had `irq=no reset=no`:
the static template's `GpioInt`/`GpioIo` pin tables contain 0, and the real
lines exist only as the `_INI` objects `INT1`/`TRST` (`GNUM` of `0x001A0412` and
`0x001A0410`). The reset was therefore never driven and the IRQ fell back to a
timed poll. The resolved mapping (evidence in
[fingerprint-transport.md §8.6](fingerprint-transport.md#86-gpio-v2-character-device-backend-and-this-boards-lines-2026-11-09))
is `\_SB.GPI1` = ACPI `INTC10BC:01` = `/dev/gpiochip1`, IRQ **18**, reset
**16**. The exact retry is:

```sh
sudo tools/fingerprint/oxp3-fp-session.sh \
    --irq-pin 18 --reset-pin 16 --gpio-chip gpiochip1 --allow-reset
```

`--irq-pin`/`--reset-pin` supply the numbers the firmware computes at runtime;
`--gpio-chip gpiochip1` is the controller (`gpiochipN`, a `/dev` path or a chip
label are all accepted); `--allow-reset` is what makes the runbook pulse line
16 (the only non-read action, still gated and announced in the plan). No other
flag is needed: with no `gpiod` module and no `/sys/class/gpio`
(`CONFIG_GPIO_SYSFS` is off), the auto order picks the new `chardev` backend
(`gpiod -> chardev -> sysfs -> poll`). To pin that choice down, add
`--gpio-backend chardev` or export `FPBF63112_GPIO_BACKEND=chardev`; both reach
the probe unchanged.

What the plan should now say (if one of these still says "none", the override
did not take and the command was mistyped):

```
GpioInt line     : 18 (from --irq-pin; the template's pin list is a runtime placeholder)
GpioIo reset line: 16 (from --reset-pin)
GpioIo reset line 16 will be PULSED (--allow-reset)
```

A **valid** answer is non-zero, non-`0xff` and internally plausible:

```
fw: opcode 0xa0 selector 0x40 answer 7 B
    00 00 04 01 02 03 04
    fw=1.2.3.4 project=BOE MOC variant=4 mode=0 raw=01020304
scheme: opcode 0xa1 selector 0x42 answer 64 B
    04 00 01 ...                    (a table; non-zero, not all 0xff)
info: opcode 0xc1 selector 0x02 answer 9 B
    00 00 00 00 40 00 40 00 00
    fields a=0 b=0  sensor=64x64  image=4096 B flags=0x00
```

The concrete numbers are the expected *shape* (§5), not a captured answer; the
test is that the payload is no longer all zero and that `width*height` is a
sane image size. **All zeros** now means "the bus clocked, but the sensor still
did not drive MISO after a real reset pulse": the remaining suspects are the
reset polarity (the 2026-11-09 run settled active-high; `--reset-polarity` still allows active-low), the possibility that the module wants
the interrupt *before* it answers (the probe clocks first and waits after), or
that reset 16 is not the line this firmware actually toggles. **All `0xff`**
means nothing answered on the bus at all (chip select/controller), and a
timeout with no bytes means the node exists but the transfer never completed -
check `bytes_tx` in `sys-after.txt`. Whatever happens, the runbook restores the
spidev binding and says whether the restore was verified.

### 5.2 The one-run matrix after the no-edge failure

The last real run (2026-11-09) had the chardev GPIO requests succeed, pulsed
reset 16, sent `0xA0` and then failed with **"no interrupt on GPIO 18 within
979 ms"** - no edge at all. Earlier, without reset handling, the same reads
returned 7/64/9 zero bytes. The remaining hypotheses (reset polarity, settle
time, interrupt edge) are exactly what `--try-matrix` covers in one run:

```sh
sudo tools/fingerprint/oxp3-fp-session.sh \
    --try-matrix \
    --irq-pin 18 --reset-pin 16 --gpio-chip gpiochip1 --allow-reset
```

`--try-matrix` is forwarded to the probe in the read step (the exact command
is written to `commands.txt`); `--allow-reset` is mandatory because two of the
three polarities pulse line 16. The matrix is 3 x 2 x 3 = 18 cells:

* reset polarity: **active-low**, **active-high**, **hold** (no pulse - the
  control),
* settle after release (or before the first query for `hold`): **10 ms** and
  **150 ms**,
* interrupt edge requested from the GPIO backend: **rising**, **falling**,
  **both**.

Every cell sends only `0xA0` (firmware); `0xA1`/`0xC1` follow only if `0xA0`
is non-zero. Each attempt has a 1.5 s timeout, the matrix stops at the first
cell whose answer **parses** (a complete `0xAA`/`L`/CRC-32 frame or the bare
fixed-size answer, checked against the recovered per-command layout - not
merely non-zero), and the probe exits `0` on success, `1` when no cell
produced a valid answer. The console table has one row per cell:

```
#  polarity     settle  edge    0xA0 bytes received               verdict
 1 active-low     10ms rising  00 00 00 00 00 00 00 ...          zeros
```

The same table lands in `matrix.json` (and in `summary.json` under `matrix`),
so it travels with the transcript. What each outcome means is the guide in
[fingerprint-transport.md §8.7](fingerprint-transport.md#87-the-try-matrix-interpretation-guide):
nothing at all -> power/`_PS0` or the pins; zeros -> chip select/clock/mode;
timeouts only -> interrupt pin/edge or the device is not running; a non-zero
answer that does not parse -> the transport works but the framing/layout is
wrong. If a cell wins, write down its polarity/settle/edge and repeat exactly
that single setting in a normal `--reads` run before trusting it.

### 5.3 The write-shape run after the shifted answer

The 2026-11-09 matrix run found reset **active-high**, settle **10 ms**, edge
**rising** (attempt 7/18). `0xA0` then answered 64 bytes starting
`00 00 00 ff 7f 50 03` and `0xA1` answered `00 00 ff 7f 50 03` - the same
payload with a different number of leading zero bytes, so the answer is
shifted relative to our read window. `--try-write-shape` is the next step: it
keeps the now-known-good electrical settings pinned and varies only the write
shape, because the selector-only write may be consuming one or more bytes as
command payload, or the read flag may live in selector bit 0.

The exact owner command (one run, read-only, ~60 cells at 400 ms):

```sh
sudo tools/fingerprint/oxp3-fp-session.sh \
    --try-write-shape \
    --irq-pin 18 --reset-pin 16 --gpio-chip gpiochip1 \
    --reset-polarity active-high --settle-ms 10 --irq-edge rising \
    --allow-reset
```

`--allow-reset` is mandatory (every cell re-pulses the reset line with the
pinned polarity), and the probe still refuses anything outside
`0xA0`/`0xA1`/`0xC1` by opcode **and** selector. The probe command is recorded
in `commands.txt`; it is:

```sh
sudo python3 tools/fingerprint/oxp3-fp-probe.py --spidev /dev/spidev0.0 \
    --speed 4000000 --mode 0 --bits 8 \
    --reset-polarity active-high --settle-ms 10 \
    --gpio 18 --gpio-chip gpiochip1 --irq-edge rising --irq-active-low \
    --try-write-shape --allow-writes --reset-gpio 16 --allow-reset \
    --write-shape-json docs/state/fp-session-<run>/write-shape.json
```

The matrix is deterministic: for `0xA0`, then `0xA1`, then `0xC1`; selector
`(opcode & 0x3f) << 1` with bit 0 clear and set; 0/1/2/3/4 zero bytes and the
two-zeros-plus-phase hypothesis written after the selector; read length equal
to the recovered layout (7 B for `0xA0`, 9 B for `0xC1`) and 64 B. That is
2 x 6 x (2 + 1 + 2) = 60 cells, stopping at the first cell that produces a
valid answer. The console table reports the selector, padding, read length,
the first 16 bytes, the offset of any `0xAA`, the CRC verdict and the **best
candidate offset** of the plausible payload field:

```
#  cmd sel  pad   rd  first 16 bytes                    AA  crc   cand verdict
 1 fw   0x40 0       7 00 00 00 ff 7f 50 03 ...         -  none     - shifted
```

Success is narrower than the matrix's: a bare answer only counts when the
recovered layout is anchored at read offset 0 (`0xA0` project byte in
`{1,2,3,4,6}`, `0xC1` geometry `1..4096`, `0xA1` first word a plausible
length/version `1..4096`). A layout that appears only at a non-zero offset is
reported as `shifted` with that offset and does **not** stop the run - the
point is to see whether padding moves the payload towards offset 0 (e.g.
"payload starts at read offset 3 for padding 0, offset 2 for padding 1" means
the device consumes one command byte per padding byte). A CRC-valid
`0xAA | L:u32 | ... | CRC-32` frame is accepted at any offset.

If every cell fails, `write-shape.json` still carries the bytes and offsets:
all zeros/0xFF means no MISO drive at all; `timeout` with bytes means only the
IRQ is wrong; a `shifted` candidate that moves one byte per padding byte
identifies the byte the device consumes; a `crc-mismatch` at offset 0 means the
framing is the USB frame but the length/CRC do not match. The same object is
embedded in `summary.json` under `write_shape`, and `README.txt` names the
outcome. Repeat a winning row's exact selector/padding/read length in a normal
`--reads` run before trusting it.

## 6. Handing the transcript back

Give the whole `docs/state/fp-session-*` directory (or just `summary.json` +
`journal.tsv` + `probe-readonly.txt` + `acpi-info.txt`) to the next analysis
session. It answers, in one place:

1. what the `_CRS` actually contains (clock, mode, bits, `GpioInt` edge and
   polarity, whether a `GpioIo` reset line exists) - the four numbers the driver
   needs, and the two GPIO questions `fingerprint.md` lists as root-only;
2. whether the module answered at all, and with which lengths - which settles
   whether a plain read is "selector, then clock the answer in the same SPB
   message" or whether the module raises `GpioInt` first;
3. whether `0xA1` returns something plausible and what `0xC1` reports as
   geometry;
4. what was changed on the host and that it was put back (or exactly what was
   left, and why).

Do not paste a transcript that contains a real device's firmware table into a
public issue without checking with the owner; the DSDT is not a secret, but it
is host-specific.

## 7. Safety model, in one list

* **No firmware writes, no JTAG, no MCU restore, no update.** The runbook's
  read allowlist is `fw scheme info` (plus `uid count status` if you widen
  `--reads`), checked before the probe runs; the probe re-checks against
  `SAFE_READ_OPS` and refuses destructive opcodes by opcode **and** selector
  (the selector carries only six opcode bits, so `0x02` would alias JTAG).
  `0xB0`/`0xB1`/`0xB2`/`0xC2`/`0xC3`/`0xD0`/`0xD1`/`0xFF` are never sent.
* **No template deletion.** Enrol/identify/remove are never invoked, so fingers
  enrolled from Windows stay untouched.
* **A driver is never unbound by a session that did not bind it** without
  `--allow-existing-bind` / `--allow-takeover` plus a confirmation; even then
  the pre-existing state is restored or explicitly left, and reported.
* **Refuses when the device is already open** by another process (scans
  `/proc/*/fd` for the spidev node or the SPI/ACPI device), and when a second
  runbook holds the lock.
* **Refuses a shim.** There is no `--probe` option: the probe path is derived
  from the script's own location, must resolve to
  `tools/fingerprint/oxp3-fp-probe.py` inside this repository, must carry the
  probe's marker, must not be a symlink, must compile, and is run as
  `python3 -I -B` so `PYTHONPATH`, user site-packages and stale bytecode cannot
  change it. Its sha256 is printed and kept in the summary.
* **Every mutation is read back** and compared; the summary records both the
  before and the after state.

## 8. How the `_CRS` bytes are obtained

`iasl -d` gives *ASL text*, not bytes. Copying `ResourceTemplate ()` out of it
by hand is exactly the error-prone step this runbook removes, and on this host
`acpica-tools` is not installed. The bytes are already in the DSDT: a
`ResourceTemplate ()` in AML is `A4` (`Return`) + `11` (`Buffer`) + PkgLength +
size + the descriptors, and the method is `14 <PkgLength> "_CRS" ...`.
`tools/fingerprint/fpbf63112/aml.py` scans the table for the `_CRS` NameSeg,
looks forward for that `Return(Buffer)`, and accepts a candidate only when
`fpbf63112.acpi` parses it **and** it contains a `SpiSerialBus`. Candidates
inside the scope of `SPI1` are preferred over `SPI2` (this unit has an
unpopulated second FPRT node, `fingerprint.md` open point 5); a tie is reported
as ambiguous and the runbook refuses to guess, asking for `--crs FILE`. The scan
is bounded (96-byte lookahead, 4 KiB template, closed PkgLength decoding) and
rejects truncated/malformed tables rather than "finding" something.

Extraction cascade: `--crs FILE` (validated) -> `/sys/firmware/acpi/tables/DSDT`
-> `acpidump -b` (when sysfs is not readable) -> a clear refusal telling you to
run as root or supply `--crs`. `iasl`, when present, only writes the readable
excerpt.

Known limitation of that scan on this DSDT: FPRT `_CRS` is
`ConcatenateResTemplate(SBFB, SBFG)`. The scanner accepts the first `Return(Buffer)` that
parses as an SPI template, so it extracts `SBFB` (the 38-byte SPI descriptor)
and not `SBFG` (the `GpioInt` + `GpioIo` descriptor). `acpi-info.txt` therefore
reports no IRQ/reset pins even though `SBFG` exists; the real lines come from
`_INI`/`GNUM` and are passed with `--irq-pin 18 --reset-pin 16` (§5.1). Teach
the scanner to concatenate both named buffers before relying on `_CRS` for the
GPIOs.

## 9. Offline testing (what is covered without root or a sensor)

`tools/fingerprint/oxp3-fp-session.sh --selftest` (74 checks) and
`tests/fingerprint-session/run.sh` (77 checks) build fake `/sys` trees with
`--fixture-init DIR --variant ...` and drive the **real script** against them:

* the AML scan and `acpi_info` on a synthetic DSDT whose `_CRS` is an SPI
  template with a `GpioInt` (pin 27, edge, active-low) - plus a truncated
  template that must be rejected;
* `--dry-run` / `--non-interactive`: plan printed, tree byte-identical
  afterwards, no transcript under `docs/state`;
* the refusal matrix: no ACPI device, device already open by a pid, a
  pre-existing spidev bind, another driver bound, invalid `--reads`, unknown
  arguments;
* a full session in the fixture tree with typed confirmations: bind, dry run,
  `skip`, restore - asserting the driver symlink is gone, `driver_override`
  cleared, the journal has `mutation begin`/`end` and `restore verified`, and
  `summary.json` is valid JSON with `"verified": true`;
* the error path: the probe cannot open the fake node, exits 1, and the restore
  is still verified;
* a mistyped confirmation word refuses the read and captures nothing;
* SIGINT while bound: exit 130, binding restored, summary says verified;
* SIGKILL while bound (no trap possible), then `--resume`: the resumed run
  recognises the bind it owns, reuses the `_CRS`, finishes and restores;
* an external spidev bind is left alone, a takeover re-binds `elanspi`;
* `--try-matrix` reaches the probe against a fake spidev node and a
  nonexistent GPIO chip path, every cell fails closed, and the 18-attempt
  `matrix.json` (plus its embedded copy and outcome in `summary.json`) is
  asserted in the transcript.

The fixture root emulates the kernel side effects inside the tree (the driver
symlink and `/dev` node a real `bind` produces) - it is test support, never used
on a real run. `fpbf63112/aml.py` has its own 25 unit tests in
`tools/fingerprint/tests/test_aml.py`.

`tools/repo-test.sh` registers two suites: `fp-session` (the offline half, fast,
no root) and `fp-session-root` (the live `--dry-run` plan; it skips without root
or without the sensor, and never binds anything).

## 10. Limits and remaining unknowns

* **It has never run against the sensor.** Everything above about a successful
  run is the expected shape from the recovered layouts.
* The runbook does not pulse a reset line by default (that is not a read); if
  `_CRS` exposes a `GpioIo`, the summary says so and the probe is invoked
  without `--allow-reset`. With `--allow-reset` it passes `--crs` so the probe
  can see the pin, and the plan prints that the line will be pulsed. Treat that
  as a deliberate experiment, not as part of a read-only run.
* The IRQ is only used when the run knows a line (a `_CRS` `GpioInt`, or
  `--irq-pin`, or `--gpio`) and `--no-irq` is not given; the probe then
  requests the line (`gpiod`, else the GPIO v2 chardev, else sysfs) and
  releases it on close. A sysfs export is a transient sysfs write inside that
  request, which is why the request is listed in the phase output; the chardev
  backend writes nothing outside its own request fd.
* A **normal** read uses the settled reset polarity **active-high** and a
  10 ms settle (`--reset-polarity`/`--settle-ms`; the flags exist because the
  2026-11-09 matrix run found that combination, not because ACPI carries it).
  `--try-matrix` still sweeps active-low/active-high/hold and 10/150 ms for a
  future unit, but it is no longer the first experiment on this one.
* Whether the sensor needs the interrupt at all is still unproven: the probe
  clocks the selector/answer first and waits for the IRQ afterwards. If the
  first answer stays all-zero with the lines configured, the next experiment is
  to reorder that (wait for the edge, then clock the read), which is a code
  change, not a new runbook flag.
* The **write shape** is the open question after the shifted answer: whether
  the device consumes bytes after the selector, whether the read flag is
  selector bit 0, and how many leading bytes precede the real payload. The
  `--try-write-shape` run answers the first two and records the shift; a
  candidate offset that moves with the padding is the relationship to use.
* `acpidump` is used only when the sysfs DSDT cannot be read; that path is
  implemented but not exercised on this host (no `acpica-tools` here).
* Whether the first read needs the IRQ before the answer, whether the module
  enforces the SM2 session, and the real answer layouts stay open - see
  `fingerprint.md` open points and `fingerprint-transport.md` §9.
* `--resume` after a *power loss* works from `session.state`; if the state file
  itself is lost, the runbook re-reads the live tree and treats an existing
  spidev bind as external (left alone) - the safe side.
