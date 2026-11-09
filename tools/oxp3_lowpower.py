#!/usr/bin/env python3
"""oxp3-lowpower - close the remaining s2idle drain on the OneXPlayer 3.

The measured facts this policy is built on (docs/hw/sleep-power.md, section R6)
are narrow, so the policy is narrow too:

* ``/sys/class/wakeup`` has a source literally named ``mmc0`` whose event counter
  rises by exactly one on every suspend cycle (docs/state/sleep-cycles-*/
  report.json).  It is not a PCI or USB device, so neither the wake-source
  policy nor the sleep-cycle harness ever looked at it.  Live counters say what
  it is: ``wakeup_count = 0`` and ``prevent_suspend_time_ms = 0`` (it never
  aborted a suspend), while ``max_time_ms = 5495`` and ``total_time_ms = 15946``
  match the three ``PM: suspend devices took`` windows - it is a **wakelock held
  during the device-suspend walk**, and the dominant part of that walk.  It is a
  *virtual* source with no ``power/wakeup`` bit, so it is not "disarmed": the
  SD host behind it (``rtsx_pci_sdmmc``) is parked across suspend when no card
  is in use, and rebound on resume.
* ``pcie_aspm`` policy is left at the ACPI default unless OXP3_LOWPOWER_ASPM is set;
  on this platform ``powersave`` measurably hurt (0% S0ix). It still selects
  ``[default]``, and the storage link is the one the
  kernel itself flags at boot (``nvme 0000:01:00.0: platform quirk: setting
  simple suspend``).  ASPM is therefore applied as ``powersave``, recorded,
  reversible, and *latched off* after any observed suspend/resume failure.
* The NVMe's APST knobs are already permissive (``default_ps_max_latency_us`` =
  100000us) and can only remove power states, so this tool never writes them;
  it records them so the sleep report can show whether the SSD is allowed to use
  its low-power states.
* Audio is SOF (``sof-hda-dsp``), not ``snd_hda_intel``; it has no ``power_save``
  parameter to tune, so it is measured and recorded, never guessed at.
* The two internal USB HID endpoints (the EC bridge ``1a86:fe00`` and the mini
  keyboard) are deliberately *not* autosuspended: hhd reads the EC through them
  and R4 in docs/hw/sleep-power.md forbids it.

Every write goes through one table of :class:`Change` rows.  Each row records the
previous value, is applied only when the attribute is writable, is restored by
``undo``, and a failure never propagates (the script always exits 0 on an
apply/post run, so a policy problem cannot stop a suspend).

Commands (all accept --root and --state for hardware-free tests):

    oxp3-lowpower apply        boot-time policy: ASPM, mmc wake, card-reader PM
    oxp3-lowpower pre          before suspend: park the SD host (no card in use)
    oxp3-lowpower post         after resume: rebind the SD host, latch on failure
    oxp3-lowpower undo         rebind, then put back every value apply changed
    oxp3-lowpower status       JSON facts + the last apply record (read-only)

Nothing here is a guess about a register: every path is a documented sysfs
attribute, and every decision can be re-read from the state file the tool writes.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

SCHEMA = 1
PROG = "oxp3-lowpower"
VERSION = "1.0.0"

DEFAULT_STATE_DIR = "/var/lib/oxp3-lowpower"
#: Kernel default; a value in this file is the *selected* one, shown in brackets.
ASPM_POLICY = "sys/module/pcie_aspm/parameters/policy"
#: ASPM changes are OFF by default: on this platform setting pcie_aspm.policy to
#: powersave correlated with S0ix residency falling from 100% to 0% and sleep-only
#: drain rising from 0.73 to 1.77 %/h, so the firmware/ACPI default is kept unless
#: the owner opts in (OXP3_LOWPOWER_ASPM=powersave).
ASPM_TARGET = None
#: The RTS525A card reader function (the SD host behind mmc0 on this platform).
CARD_READER = "0000:56:00.0"
#: The platform driver/cell that owns the mmc host on top of the RTS525A.
SD_HOST_DRIVER = "rtsx_pci_sdmmc"
SD_HOST_DEVICE = "rtsx_pci_sdmmc.0"
#: Devices whose PM state is recorded for the report.  Read-only facts; writing
#: power/control to any of them is not part of the policy.
WATCHED_DEVICES = (
    ("0000:00:02.0", "iGPU (xe)"),
    ("0000:00:06.0", "NVMe root port"),
    ("0000:01:00.0", "NVMe SSD"),
    ("0000:00:1c.0", "card-reader root port"),
    ("0000:56:00.0", "RTS525A card reader / mmc0"),
    ("0000:00:14.3", "CNVi Wi-Fi"),
    ("0000:00:14.7", "CNVi Bluetooth"),
    ("0000:00:1f.3", "HD Audio / SOF DSP"),
)
#: Incremented by the kernel on a failure; any growth after apply means the
#: policy that was active is a suspect.
FAILURE_COUNTERS = ("fail", "failed_suspend", "failed_resume",
                    "failed_resume_early", "failed_resume_noirq")
CHANGE_EVIDENCE = "docs/hw/sleep-power.md R6"


# --------------------------------------------------------------- small helpers
def _iso(epoch=None):
    return datetime.fromtimestamp(
        time.time() if epoch is None else epoch, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path, default=None):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return default


def read_int(path, default=None):
    value = read_text(path)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return default


def write_value(path, value):
    """Write one sysfs value.  Never raises: returns (ok, detail)."""
    try:
        pathlib.Path(path).write_text("%s\n" % value, encoding="utf-8")
        return True, ""
    except OSError as exc:
        return False, "%s: %s" % (exc.__class__.__name__, exc.strerror or exc)
    except ValueError as exc:
        return False, str(exc)


def write_enabled(path):
    return pathlib.Path(path).exists() and os.access(path, os.W_OK)


def suspend_stats(root="/"):
    base = pathlib.Path(root) / "sys/power/suspend_stats"
    out = {}
    for key in ("success",) + FAILURE_COUNTERS:
        out[key] = read_int(base / key)
    out["last_failed_dev"] = read_text(base / "last_failed_dev", "") or ""
    out["last_failed_step"] = read_text(base / "last_failed_step", "") or ""
    return out


# ------------------------------------------------------------------- ASPM
def parse_aspm_policy(text):
    """Return (selected, options) from pcie_aspm's policy parameter.

    ``"default performance [powersave] powersupersave"`` -> ("powersave", [...]).
    A value without brackets is treated as selected (some kernels print one word).
    """
    if not text:
        return None, []
    options = []
    selected = None
    for token in text.split():
        token = token.strip()
        if token.startswith("[") and token.endswith("]"):
            selected = token[1:-1]
            options.append(selected)
        else:
            options.append(token)
    if selected is None and len(options) == 1:
        selected = options[0]
    return selected, options


def aspm_facts(root="/"):
    path = pathlib.Path(root) / ASPM_POLICY
    selected, options = parse_aspm_policy(read_text(path))
    return {
        "path": str(path),
        "present": path.exists(),
        "selected": selected,
        "options": options,
        "writable": write_enabled(path),
    }


def aspm_decision(facts, latched, target=None):
    """Decide what to do about ASPM.  Pure: no filesystem access.

    Returns {"action": "set"|"keep"|"skip", "value", "reason"}.  ASPM is opt-in:
    ``target`` None means the caller wants the ACPI default kept, and
    OXP3_LOWPOWER_ASPM may name a policy to enable it explicitly.
    """
    target = target or os.environ.get("OXP3_LOWPOWER_ASPM") or ASPM_TARGET
    if not target:
        return {"action": "skip", "value": None,
                "reason": "ASPM opt-in not enabled (set OXP3_LOWPOWER_ASPM=powersave)"}
    if not facts.get("present"):
        return {"action": "skip", "value": None,
                "reason": "no pcie_aspm policy parameter (module not built/loaded)"}
    selected = facts.get("selected")
    options = facts.get("options") or []
    if target not in options:
        return {"action": "skip", "value": None,
                "reason": "target %r not offered by this kernel (%s)"
                          % (target, " ".join(options) or "no options")}
    if selected == target:
        return {"action": "keep", "value": target,
                "reason": "already %s" % target}
    if latched:
        return {"action": "skip", "value": None,
                "reason": "latched off after an observed suspend failure (%s)"
                          % (latched.get("reason") or "no reason recorded")}
    if not facts.get("writable"):
        return {"action": "skip", "value": None,
                "reason": "policy parameter is not writable (needs root)"}
    return {"action": "set", "value": target,
            "reason": "was %s; L1.2 on the storage link is required for PC10"
                      % (selected or "unknown")}


# ------------------------------------------------------------------- mmc wake
def mmc_wake_sources(root="/"):
    """Every /sys/class/wakeup source named ``mmc*``, with a resolvable device.

    The measured mmc0 source has a *dangling* ``device`` link (report.json shows
    ``"device": ""``), so the class device is tried first and the mmc_host and
    card-reader function are the documented fallbacks.
    """
    root = pathlib.Path(root)
    out = []
    for d in sorted((root / "sys/class/wakeup").glob("wakeup*")):
        name = read_text(d / "name")
        if not name or not name.startswith("mmc"):
            continue
        candidates = []
        own = False
        link = d / "device"
        try:
            if link.exists():
                resolved = pathlib.Path(os.path.realpath(link))
                if resolved.exists():
                    candidates.append(resolved)
                    own = True
        except OSError:
            pass
        for extra in (
            root / "sys/class/mmc_host" / name,
            root / "sys/class/mmc_host" / name / "device",
            root / "sys/bus/pci/devices" / CARD_READER,
        ):
            if extra.exists():
                candidates.append(extra)
        wake_path = None
        for candidate in candidates:
            attr = candidate / "power/wakeup"
            if attr.exists():
                wake_path = attr
                break
        # Only the source's own device link can carry its enable bit.  A virtual
        # source (the measured mmc0) has *no* device link at all and its counters
        # live in /sys/class/wakeup only -- a parent attribute is not the source.
        out.append({
            "id": d.name,
            "name": name,
            "enabled": (read_text(wake_path) == "enabled") if wake_path else None,
            "wakeup_path": str(wake_path) if wake_path else "",
            "writable": write_enabled(wake_path) if wake_path else False,
            "device_resolved": bool(candidates),
            "own_attribute": own,
            "stats": wakeup_stats(d),
        })
    return out


def wakeup_stats(path):
    """The counters that separate "woke the machine" from "held a wakelock".

    ``wakeup_count`` increments only when an event aborts an in-progress suspend;
    ``active_time_ms``/``max_time_ms`` say how long the source stayed active.
    """
    path = pathlib.Path(path)
    return {
        "event_count": read_int(path / "event_count"),
        "wakeup_count": read_int(path / "wakeup_count"),
        "active_count": read_int(path / "active_count"),
        "active_time_ms": read_int(path / "active_time_ms"),
        "total_time_ms": read_int(path / "total_time_ms"),
        "max_time_ms": read_int(path / "max_time_ms"),
        "prevent_suspend_time_ms": read_int(path / "prevent_suspend_time_ms"),
        "expire_count": read_int(path / "expire_count"),
    }


def mmc_wake_decision(source):
    """Pure decision for one mmc wake source."""
    stats = source.get("stats") or {}
    if not source.get("own_attribute"):
        # The measured mmc0 case: a virtual wakeup source with no device link.
        # There is no ABI to disable it; pretending otherwise would be a lie.
        return {"action": "skip", "value": None,
                "reason": "wakeup source %s is virtual (no device link), so it has "
                          "no power/wakeup to disable; its counters show "
                          "wakeup_count=%s, it did not abort a suspend - it is a "
                          "wakelock held during the device-suspend walk. Handled by "
                          "parking the SD host instead (pre/post)."
                          % (source.get("name"), stats.get("wakeup_count"))}
    if not source.get("wakeup_path"):
        return {"action": "skip", "value": None,
                "reason": "wakeup source %s has no resolvable power/wakeup "
                          "attribute; cannot disarm it from here" % source.get("name")}
    if not source.get("writable"):
        return {"action": "skip", "value": None,
                "reason": "power/wakeup is not writable (needs root)"}
    if source.get("enabled"):
        return {"action": "set", "value": "disabled",
                "reason": "armed storage host can end s2idle and keeps the reader "
                          "link out of its deepest state"}
    return {"action": "keep", "value": "disabled", "reason": "already disabled"}


# ------------------------------------------------------- SD host park (pre/post)
def sd_host_facts(root="/"):
    root = pathlib.Path(root)
    driver = root / "sys/bus/platform/drivers" / SD_HOST_DRIVER
    device = root / "sys/bus/platform/devices" / SD_HOST_DEVICE
    bound = (driver / SD_HOST_DEVICE).exists()
    return {
        "driver": SD_HOST_DRIVER,
        "device": SD_HOST_DEVICE,
        "present": driver.exists(),
        "bound": bound,
        "bind": str(driver / "bind"),
        "unbind": str(driver / "unbind"),
        "wakeup": read_text(device / "power/wakeup"),
        "writable": write_enabled(driver / "unbind"),
    }


def sd_media_facts(root="/"):
    """A card in use must never be parked: unbinding would drop the blocks."""
    root = pathlib.Path(root)
    blocks = sorted(p.name for p in (root / "sys/block").glob("mmcblk*")
                    if (root / "sys/block").exists())
    mounted = []
    for line in (read_text(root / "proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and "mmcblk" in parts[0]:
            mounted.append(parts[1])
    return {"block_devices": blocks, "mounted": mounted,
            "in_use": bool(blocks or mounted)}


def park_decision(sd, media, latch):
    """Pure decision for parking (unbinding) the SD host around a suspend."""
    if latch:
        return {"action": "skip", "value": None,
                "reason": "latched off after an observed suspend failure (%s)"
                          % (latch.get("reason") or "no reason recorded")}
    if not sd.get("present"):
        return {"action": "skip", "value": None,
                "reason": "no %s driver on this host" % SD_HOST_DRIVER}
    if not sd.get("bound"):
        return {"action": "skip", "value": None, "reason": "already unbound"}
    if not sd.get("writable"):
        return {"action": "skip", "value": None,
                "reason": "driver unbind is not writable (needs root)"}
    if media.get("in_use"):
        return {"action": "skip", "value": None,
                "reason": "an SD card is present/mounted (%s); never park a card "
                          "that is in use"
                          % ", ".join(media.get("block_devices") or
                                      media.get("mounted") or ["mmcblk"])}
    return {"action": "set", "value": SD_HOST_DEVICE,
            "reason": "the SD host's wakeup source is held active for the whole "
                      "device-suspend walk (max_time_ms matches 'PM: suspend devices "
                      "took'); parking it removes that stall and the wakeup source"}


# --------------------------------------------------------------- device facts
def device_facts(root, addr, label=""):
    base = pathlib.Path(root) / "sys/bus/pci/devices" / addr
    return {
        "addr": addr,
        "label": label,
        "present": base.exists(),
        "runtime_status": read_text(base / "power/runtime_status"),
        "control": read_text(base / "power/control"),
        "d3cold_allowed": read_text(base / "d3cold_allowed"),
        "wakeup": read_text(base / "power/wakeup"),
        "driver": (os.path.basename(os.path.realpath(base / "driver"))
                   if (base / "driver").exists() else None),
    }


def nvme_facts(root="/"):
    nvme = pathlib.Path(root) / "sys/class/nvme"
    controllers = sorted(p.name for p in nvme.glob("nvme[0-9]*")) if nvme.exists() else []
    return {
        "present": bool(controllers),
        "controllers": controllers,
        "default_ps_max_latency_us": read_int(
            pathlib.Path(root) / "sys/module/nvme_core/parameters/"
            "default_ps_max_latency_us"),
        "force_apst": read_text(
            pathlib.Path(root) / "sys/module/nvme_core/parameters/force_apst"),
        "note": "APST knobs are recorded, never written: removing power states "
                "cannot lower drain (docs/hw/sleep-power.md R4)",
    }


def audio_facts(root="/"):
    base = pathlib.Path(root) / "sys/bus/pci/devices/0000:00:1f.3"
    return {
        "present": base.exists(),
        "runtime_status": read_text(base / "power/runtime_status"),
        "control": read_text(base / "power/control"),
        "driver": "snd_sof_pci (sof-hda-dsp)" if (base / "driver").exists() else None,
        "note": "SOF has no snd_hda_intel power_save parameter to tune; measured, "
                "not guessed",
    }


def audio_decision():
    """Deliberately no write.

    The artifacts name the card ``sof-hda-dsp``; ``snd_hda_intel``'s
    ``power_save`` knob does not exist for it, and forcing
    ``power/control=auto`` on the DSP function changes runtime PM of a device
    that the system suspends anyway.  Record the state, do not invent a knob.
    """
    return {"action": "skip", "value": None,
            "reason": "audio is SOF (sof-hda-dsp): no power_save ABI to set; "
                      "state is recorded instead"}


def latched(state):
    entry = (state or {}).get("latch") or {}
    return entry if entry.get("reason") else None


# ------------------------------------------------------------------ plan/apply
def change(change_id, kind, target, attribute, want, current, why,
           expected, applied=None, detail=""):
    return {
        "id": change_id,
        "kind": kind,
        "target": target,
        "path": attribute,
        "before": current,
        "after": want,
        "why": why,
        "expected": expected,
        "evidence": CHANGE_EVIDENCE,
        "applied": applied,
        "detail": detail,
    }


def plan(root="/", state=None):
    """The complete decision list.  Pure: reads only, writes nothing."""
    state = state or {}
    latch = latched(state)
    changes = []
    facts = {
        "aspm": aspm_facts(root),
        "mmc_wake": mmc_wake_sources(root),
        "nvme": nvme_facts(root),
        "audio": audio_facts(root),
        "devices": [device_facts(root, addr, label)
                    for addr, label in WATCHED_DEVICES],
        "sd_host": sd_host_facts(root),
        "media": sd_media_facts(root),
    }
    facts["park"] = park_decision(facts["sd_host"], facts["media"], latch)

    aspm = aspm_decision(facts["aspm"], latch)
    if aspm["action"] == "set":
        changes.append(change(
            "aspm.policy", "pcie-aspm", "pcie_aspm.policy",
            facts["aspm"]["path"], aspm["value"], facts["aspm"]["selected"],
            aspm["reason"],
            "hypothesis: 0-0.4 W on the storage link; confirm with "
            "pmc_core/ltr_show and the next sleep-cycle run"))
    for source in facts["mmc_wake"]:
        decision = mmc_wake_decision(source)
        if decision["action"] != "set":
            continue
        changes.append(change(
            "mmc.%s.wakeup" % source["name"], "mmc-wake",
            "%s wakeup" % source["name"], source["wakeup_path"],
            decision["value"], "enabled", decision["reason"],
            "removes a spurious wake source and unblocks the reader's D3; "
            "drain effect expected to be small but non-zero"))

    reader = device_facts(root, CARD_READER, "RTS525A card reader / mmc0")
    if reader["present"] and reader["control"] == "on":
        changes.append(change(
            "sdcard.runtime_pm", "runtime-pm", "card reader power/control",
            str(pathlib.Path(root) / "sys/bus/pci/devices" / CARD_READER /
                "power/control"),
            "auto", "on",
            "the SD host is never used while suspended; auto lets it idle in D3 "
            "instead of being held at D0 by a userspace/udev 'on'",
            "hypothesis: 0-0.2 W when no card is inserted; confirm with "
            "power/runtime_status = suspended and the next run"))
    changes.append(change(
        "audio.runtime_pm", "record-only", "HD Audio / SOF runtime PM",
        str(pathlib.Path(root) / "sys/bus/pci/devices/0000:00:1f.3/power/control"),
        None, facts["audio"]["control"], audio_decision()["reason"],
        "no change expected from this tool; listed so the next report shows the "
        "state instead of assuming it", applied=False))
    return {"facts": facts, "changes": changes, "latch": latch,
            "skip": {"aspm": aspm if aspm["action"] != "set" else None,
                     "audio": audio_decision(),
                     "mmc": [mmc_wake_decision(source)
                             for source in facts["mmc_wake"]
                             if not mmc_wake_decision(source)["action"] == "set"],
                     "park": facts["park"]}}


def load_state(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, data):
    try:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def apply(root="/", state_path=None, dry_run=False):
    """Apply the boot-time policy, recording every pre-image in the state file."""
    state_path = pathlib.Path(state_path or
                              pathlib.Path(root) / DEFAULT_STATE_DIR.lstrip("/") /
                              "state.json")
    previous = load_state(state_path)
    recovered = _recover_sd_host(root, previous)
    parked = dict(previous.get("sd_host") or {"unbound": False})
    if recovered and recovered.get("bound"):
        parked = {"unbound": False, "at": _iso()}
    planned = plan(root, previous)
    applied = []
    for row in planned["changes"]:
        if row.get("after") is None:
            applied.append(row)
            continue
        if dry_run:
            row = dict(row, applied=None)
            applied.append(row)
            continue
        ok, detail = write_value(row["path"], row["after"])
        row = dict(row, applied=ok, detail=detail)
        applied.append(row)
    # If an earlier version applied ASPM and the owner now leaves it at the
    # firmware default, converge the host by restoring the recorded pre-image.
    if not dry_run and not (os.environ.get("OXP3_LOWPOWER_ASPM") or ASPM_TARGET):
        for row in previous.get("changes") or []:
            if row.get("id") == "aspm.policy" and row.get("applied") \
                    and row.get("before") is not None:
                ok, detail = write_value(row["path"], row["before"])
                if ok:
                    applied = [r for r in applied if r.get("id") != "aspm.policy"]
                    applied.append(dict(row, applied=False, after=row["before"],
                                        detail="reverted: ASPM is opt-in and not enabled"))
    state = {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "generated_at": _iso(),
        "applied_at": _iso(),
        "root": str(root),
        "changes": applied,
        "facts": planned["facts"],
        "skip": planned["skip"],
        "latch": previous.get("latch"),
        "sd_host": parked,
        "recovered": recovered,
        "suspend_stats": suspend_stats(root),
    }
    if not dry_run:
        save_state(state_path, state)
    return state


def _recover_sd_host(root, state):
    """Re-bind the SD host if a previous suspend left it parked (crash safety)."""
    parked = (state or {}).get("sd_host") or {}
    if not parked.get("unbound"):
        return None
    sd = sd_host_facts(root)
    if not sd["present"] or sd["bound"]:
        return {"bound": sd["bound"], "detail": "already in the normal state"}
    ok, detail = write_value(sd["bind"], sd["device"])
    return {"bound": ok, "detail": detail}


def pre(root="/", state_path=None):
    """Before suspend: park the SD host when no card is in use.

    This is the one change that targets the measured 5.2-5.5 s device-suspend
    stall: the SD host's wakelock (total_time_ms) tracks 'PM: suspend devices
    took' almost exactly, and the same source is why mmc0 shows up in every
    wake attribution with wakeup_count = 0.
    """
    state_path = pathlib.Path(state_path or
                              pathlib.Path(root) / DEFAULT_STATE_DIR.lstrip("/") /
                              "state.json")
    state = load_state(state_path)
    facts = plan(root, state)["facts"]
    decision = facts["park"]
    result = {"at": _iso(), "decision": decision, "unbound": False,
              "sd_host": facts["sd_host"], "media": facts["media"]}
    if decision["action"] == "set":
        ok, detail = write_value(facts["sd_host"]["unbind"],
                                 facts["sd_host"]["device"])
        result.update(unbound=ok, detail=detail)
    # Always record the park, even on the first suspend before any boot apply:
    # post() needs to know it has to rebind.
    if not state:
        state = {"schema": SCHEMA, "tool": PROG, "version": VERSION,
                 "generated_at": _iso()}
    state.setdefault("sd_host", {})
    state["sd_host"] = {"unbound": result["unbound"],
                        "device": SD_HOST_DEVICE,
                        "driver": SD_HOST_DRIVER,
                        "at": result["at"],
                        "reason": decision["reason"]}
    state["last_pre"] = result
    save_state(state_path, state)
    return result


def post(root="/", state_path=None):
    """After a resume: latch the policy off when the kernel counted a failure."""
    state_path = pathlib.Path(state_path or
                              pathlib.Path(root) / DEFAULT_STATE_DIR.lstrip("/") /
                              "state.json")
    state = load_state(state_path)
    if not state:
        return {"status": "no-policy-installed", "latch": None}
    rebind = None
    parked = state.get("sd_host") or {}
    if parked.get("unbound"):
        sd = sd_host_facts(root)
        if sd["present"] and not sd["bound"]:
            ok, detail = write_value(sd["bind"], sd["device"])
            rebind = {"bound": ok, "detail": detail}
        else:
            rebind = {"bound": sd["bound"], "detail": "already in the normal state"}
        state["sd_host"] = {"unbound": False, "at": _iso()}
    before = state.get("suspend_stats") or {}
    now = suspend_stats(root)
    grew = {key: (now.get(key) or 0) - (before.get(key) or 0)
            for key in FAILURE_COUNTERS}
    grew = {key: value for key, value in grew.items() if value > 0}
    aspm_applied = any(row.get("id") == "aspm.policy" and row.get("applied")
                       for row in state.get("changes") or [])
    result = {"status": "ok", "failed": grew, "aspm_active": aspm_applied,
              "last_failed_dev": now.get("last_failed_dev") or "",
              "last_failed_step": now.get("last_failed_step") or "",
              "latch": state.get("latch"), "rebind": rebind}
    if grew and aspm_applied:
        latch = {
            "reason": "suspend/resume failure observed with pcie_aspm=powersave "
                      "active (%s)" % ", ".join("%s +%d" % item
                                                for item in sorted(grew.items())),
            "at": _iso(),
            "counters": grew,
            "last_failed_dev": now.get("last_failed_dev") or "",
            "last_failed_step": now.get("last_failed_step") or "",
        }
        result["latch"] = latch
        state["latch"] = latch
    state["last_post"] = dict(result, at=_iso())
    state["suspend_stats"] = now
    save_state(state_path, state)
    return result


def undo(root="/", state_path=None):
    """Restore every value apply changed, newest record wins, idempotent."""
    state_path = pathlib.Path(state_path or
                              pathlib.Path(root) / DEFAULT_STATE_DIR.lstrip("/") /
                              "state.json")
    state = load_state(state_path)
    recovered = _recover_sd_host(root, state)
    restored = []
    for row in reversed(state.get("changes") or []):
        before = row.get("before")
        if not row.get("applied") or before is None:
            continue
        current = read_text(row.get("path", ""))
        if current is not None and current != row.get("after"):
            restored.append(dict(row, restored=False,
                                 detail="current value %r differs from what this "
                                        "tool wrote; left alone" % current))
            continue
        ok, detail = write_value(row["path"], before)
        restored.append(dict(row, restored=ok, detail=detail))
    if state:
        state["undone_at"] = _iso()
        state["restore"] = restored
        state["sd_host"] = {"unbound": False, "at": _iso()}
        # an explicit undo is a reset: forget the failure latch too
        state["latch"] = None
        save_state(state_path, state)
    return {"restored": restored, "recovered": recovered}


def status(root="/", state_path=None):
    state_path = pathlib.Path(state_path or
                              pathlib.Path(root) / DEFAULT_STATE_DIR.lstrip("/") /
                              "state.json")
    state = load_state(state_path)
    return {
        "schema": SCHEMA,
        "tool": PROG,
        "version": VERSION,
        "generated_at": _iso(),
        "state_path": str(state_path),
        "installed": bool(state),
        "apply": state,
        "latch": latched(state),
        "sd_host": (state or {}).get("sd_host") or sd_host_facts(root),
        "facts": plan(root, state)["facts"],
    }


# ----------------------------------------------------------------------- CLI
def _cmd_apply(args):
    state = apply(args.root, args.state, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(state, indent=2))
    else:
        changed = [row for row in state["changes"] if row.get("applied")]
        print("%s: %d change(s) applied, %d recorded"
              % (PROG, len(changed), len(state["changes"])))
        for row in state["changes"]:
            print("  %-24s %-9s %s -> %s%s"
                  % (row["id"], "applied" if row.get("applied") else
                     ("dry-run" if args.dry_run else "skipped"),
                     row.get("before"), row.get("after"),
                     (" (%s)" % row["detail"]) if row.get("detail") else ""))
    return 0


def _cmd_pre(args):
    result = pre(args.root, args.state)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result.get("unbound"):
        print("%s: SD host %s parked (%s)" % (PROG, SD_HOST_DEVICE,
                                             result["decision"]["reason"]))
    else:
        print("%s: SD host not parked: %s"
              % (PROG, result["decision"].get("reason") or "no reason"))
    return 0


def _cmd_post(args):
    result = post(args.root, args.state)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result.get("latch"):
        print("%s: %s" % (PROG, result["latch"]["reason"]))
    else:
        print("%s: post-resume check ok (%s)" % (PROG, result.get("status")))
    return 0


def _cmd_undo(args):
    result = undo(args.root, args.state)
    print(json.dumps(result, indent=2) if args.json else
          "%s: %d value(s) restored" % (PROG, len(result["restored"])))
    return 0


def _cmd_status(args):
    print(json.dumps(status(args.root, args.state), indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG, description="OneXPlayer 3 s2idle low-power policy "
                               "(reversible, recorded, gated)")
    parser.add_argument("--root", default="/",
                        help="sysfs root (tests use a temporary tree)")
    parser.add_argument("--state", default=None,
                        help="state file (default: %s/state.json)" % DEFAULT_STATE_DIR)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version",
                        version="%s %s" % (PROG, VERSION))
    sub = parser.add_subparsers(dest="command", required=True)
    p_apply = sub.add_parser("apply", help="apply the boot-time policy")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="print the decisions, write nothing")
    p_apply.set_defaults(func=_cmd_apply)
    p_pre = sub.add_parser("pre", help="before suspend: park the SD host")
    p_pre.set_defaults(func=_cmd_pre)
    p_post = sub.add_parser("post", help="after resume: rebind, then latch on a failure")
    p_post.set_defaults(func=_cmd_post)
    p_undo = sub.add_parser("undo", help="restore every changed value")
    p_undo.set_defaults(func=_cmd_undo)
    p_status = sub.add_parser("status", help="JSON facts and the last record")
    p_status.set_defaults(func=_cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - a policy tool must never wedge sleep
        print("%s: %s: %s" % (PROG, exc.__class__.__name__, exc), file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
