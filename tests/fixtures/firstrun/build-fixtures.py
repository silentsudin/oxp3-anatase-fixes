#!/usr/bin/env python3
"""Build the synthetic first-run bundles used by tests/unit/test_triage.py.

The fixtures are committed, so this script is not run by the test suite: it is the
generator that keeps them consistent and the place to add a scenario. Run it from
anywhere; it rewrites ``tests/fixtures/firstrun/<scenario>/`` in place.

Each scenario is a small but *internally consistent* tree in the layout
``tools/oxp3_firstrun.py`` writes (docs/FIRSTRUN.md section 3): the manifest
checklist, the verifier rows, the unit states and the file-level evidence all
agree with each other, which is what makes the expectations in the test suite
meaningful. All content here is our own prose and synthetic values - no vendor
bytes, no real serial numbers, no journal from this host.

    python3 tests/fixtures/firstrun/build-fixtures.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
KERNEL = "7.2.7-an28.fc45.x86_64"
MESA = "ec418f04e589965adeb26da96fb282915e3dfd7ab98b0d6eab69f9878f3578e9"
XMX = "4b1d55ea9483417f3f1d0f5c2b7c1f1a4e8c9b0d2a6f3e5c7b9d1f3a5c7e9b0d"
BAD_XMX = "dcc0f2c4a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c"

# A live Plasma session: PowerDevil holds the handle-* block inhibitor, so logind
# does not act on the key events and the PowerDevil action decides.
POWERDEVIL_INHIBITOR = (
    "PowerDevil       1000 <user> 147067 org_kde_powerde "
    "handle-power-key:handle-suspend-key:handle-hibernate-key:handle-lid-switch "
    "KDE handles power events   block"
)

GROUP = {
    "vol.": "volume",
    "hid.": "leds",
    "led.": "leds",
    "homebtn.": "leds",
    "rgbd.": "leds",
    "life.": "daemons",
    "sleep.": "sleep",
    "bt.": "bluetooth",
    "pwr.": "power",
    "bat.": "battery",
    "aud.": "audio",
    "dsp.": "display",
    "gfx.": "graphics",
    "sched.": "sched",
    "decky.": "decky",
    "imu.": "imu",
    "sys.": "system",
}

TITLE = {
    "vol.unit": "oxp3-voltap service",
    "vol.grab": "volume event device grabbed",
    "vol.topology": "volume device selection",
    "vol.holder": "physical AT keyboard holder",
    "vol.hwdb": "force-release hwdb",
    "hid.rule": "hidraw udev rule",
    "led.status": "vendor HID protocol visible",
    "led.zones": "addressable RGB zones",
    "hid.access": "hidraw node writable by user",
    "homebtn.unit": "home-button unit",
    "homebtn.mapping": "home button maps to KEY_HOMEPAGE",
    "homebtn.press": "a real button press was decoded",
    "led.write": "LED write accepted by the device",
    "rgbd.unit": "RGB keeper unit and config",
    "rgbd.check": "RGB config encodes",
    "life.fd": "daemons hold live device fds",
    "life.reconnect": "device loss recovered",
    "life.sim": "lifecycle simulation",
    "sleep.mem": "s2idle selected",
    "sleep.stats": "a real suspend happened",
    "sleep.units": "wake policy applied at boot",
    "sleep.wakeup": "wake sources disarmed",
    "sleep.trigger": "a suspend request has a permitted path",
    "sleep.audit": "suspend record readable",
    "sleep.suspend": "real s2idle cycle",
    "bt.files": "pinned firmware files",
    "bt.dracut": "initramfs carries the firmware",
    "bt.journal": "no firmware-load failure this boot",
    "bt.controller": "controller visible",
    "bt.diag": "firmware diagnosis",
    "pwr.zones": "both RAPL package zones",
    "pwr.pl1": "TDP sync invariant",
    "pwr.profile": "saved profile applied",
    "pwr.fan": "EC fan node sane",
    "pwr.adaptive": "adaptive TDP inert/consistent",
    "pwr.rapl": "rapl-power publishes values",
    "bat.threshold": "charge limit reachable",
    "bat.behaviour": "charge inhibit reachable",
    "aud.scripts": "EQ can be toggled",
    "aud.server": "audio session exists",
    "aud.eq": "filter-chain sink loaded",
    "aud.dummy": "no Dummy Output fallback",
    "aud.model": "8-band model intact",
    "dsp.hdr": "compositor state readable",
    "dsp.icc": "ICC state readable",
    "dsp.panel": "panel EDID claims",
    "dsp.vibrance": "saturation shader present",
    "gfx.mesa": "host Mesa build",
    "gfx.xmx.driver": "XMX driver build",
    "gfx.xmx.helper": "nodgrf compile helper",
    "gfx.xmx.igc": "IGC/ocloc present",
    "gfx.xmx.kernels": "XMX kernel cache",
    "gfx.xmx.icd": "per-game XMX ICD",
    "gfx.xmx.trace": "igdext trace produced",
    "gfx.xe": "patched xe.ko loaded",
    "gfx.working": "recorded working build matches",
    "sched.state": "sched_ext enabled",
    "sched.scx": "scx_cosmos running",
    "sched.unit": "gaming-scheduler unit",
    "decky.dir": "plugin installed",
    "decky.backend": "backend imports",
    "decky.status": "status backend agrees",
    "imu.selftest": "IMU usable",
    "imu.iio": "IIO device selection",
}

# ---------------------------------------------------------------- check tables
# (state, summary, detail)
HEALTHY = {
    "vol.unit": ("pass", "daemon running (active)", ""),
    "vol.grab": ("pass", "grabbed /dev/input/event23 (hhd virtual)", ""),
    "vol.topology": ("pass", "hhd-virtual selected (hhd vbtn)", ""),
    "vol.holder": ("pass", "physical AT keyboard held by hhd", ""),
    "vol.hwdb": ("pass", "compiled; force-release entry present", ""),
    "hid.rule": ("pass", "udev rule installed", ""),
    "led.status": ("pass", "protocol readable on /dev/hidraw4", ""),
    "led.zones": ("pass", "5 zones, 11 presets", ""),
    "hid.access": ("pass", "/dev/hidraw4 writable by user", ""),
    "homebtn.unit": ("pass", "unit active", ""),
    "homebtn.mapping": ("pass", "--key homepage, no --exec", ""),
    "homebtn.press": ("pass", "press decoded this boot", ""),
    "led.write": ("skip", "gated: only with --full", ""),
    "rgbd.unit": ("pass", "unit active, config present", ""),
    "rgbd.check": ("pass", "5 zones / 10 frames encode", ""),
    "life.fd": ("pass", "hidraw + event/uinput fds live", ""),
    "life.reconnect": ("pass", "one device loss, recovered", ""),
    "life.sim": ("skip", "gated: only with --full", ""),
    "sleep.mem": ("pass", "mem_sleep: [s2idle] deep", ""),
    "sleep.stats": ("pass", "last s2idle sleep 8.4s (success=1, fail=0)", ""),
    "sleep.units": ("pass", "3/3 enabled and active", ""),
    "sleep.wakeup": ("pass", "9 wake sources disarmed as documented", ""),
    "sleep.trigger": ("pass", "drop-in parsed; pkcheck yes; no sleep block", ""),
    "sleep.audit": ("pass", "capability and record readable", ""),
    "sleep.suspend": ("skip", "gated: needs --full, --with-changes and OXP3_ALLOW_SUSPEND=1",
                      ""),
    "bt.files": ("pass", "2/2 pinned files present", ""),
    "bt.dracut": ("pass", "drop-in present; initramfs carries the blobs", ""),
    "bt.journal": ("pass", "no firmware-load failure this boot", ""),
    "bt.controller": ("pass", "btmgmt sees the controller", ""),
    "bt.diag": ("pass", "files ok=2/2, journal failure=0", ""),
    "pwr.zones": ("pass", "both zones readable", ""),
    "pwr.pl1": ("pass", "PL1 35 W on both zones (tdp-sync invariant holds)", ""),
    "pwr.profile": ("pass", "balanced == saved balanced", ""),
    "pwr.fan": ("pass", "auto mode, pwm 51/255 (enable=2), 1399 rpm", ""),
    "pwr.adaptive": ("pass", "inactive and disabled (opt-in not used)", ""),
    "pwr.rapl": ("pass", "active", ""),
    "bat.threshold": ("pass", "charge_control_end_threshold=80", ""),
    "bat.behaviour": ("pass", "charge_behaviour: [auto] inhibit-charge", ""),
    "aud.scripts": ("pass", "eq-on.sh and eq-off.sh executable", ""),
    "aud.server": ("pass", "audio server reachable", ""),
    "aud.eq": ("pass", "oxp3_speaker_eq.sink is the default sink", ""),
    "aud.dummy": ("pass", "default sink: oxp3_speaker_eq.sink", ""),
    "aud.model": ("pass", "8-band model reproduces (pre-gain -8 dB)", ""),
    "dsp.hdr": ("pass", "HDR=disabled, WCG=disabled", ""),
    "dsp.icc": ("pass", "no ICC profile applied (panel profile: none)", ""),
    "dsp.panel": ("pass", "EDID reports 10-bit PQ/HDR10 wide gamut", ""),
    "dsp.vibrance": ("pass", "shader present", ""),
    "gfx.mesa": ("pass", "%s (match)" % MESA[:16], ""),
    "gfx.xmx.driver": ("pass", "%s (match)" % XMX[:16], ""),
    "gfx.xmx.helper": ("pass", "xmx-cm-compile-nodgrf.sh", ""),
    "gfx.xmx.igc": ("pass", "ocloc-25.18.1", ""),
    "gfx.xmx.kernels": ("pass", "30 kernel(s)", ""),
    "gfx.xmx.icd": ("pass", "icd-xmx.json -> xmx driver", ""),
    "gfx.xmx.trace": ("pass", "igdext_trace.log found (562 lines)", ""),
    "gfx.xe": ("pass", "layered (xe.ko.xz)", ""),
    "gfx.working": ("pass", "state matches the recorded working build", ""),
    "sched.state": ("skip", "gaming scheduler not installed (opt-in)", ""),
    "sched.scx": ("skip", "gaming scheduler not installed (opt-in)", ""),
    "sched.unit": ("skip", "not installed (opt-in)", ""),
    "decky.dir": ("pass", "plugin dir with plugin.json/main.py/dist", ""),
    "decky.backend": ("pass", "backend imports", ""),
    "decky.status": ("pass", "overall=ok (ok=25 warn=0 fail=0 unknown=0)", ""),
    "imu.selftest": ("pass", "RESULT: PASS (0 warning(s))", ""),
    "imu.iio": ("pass", "ACPI 10EC5280 selected", ""),
}

PREINSTALL = dict(HEALTHY)
PREINSTALL.update({
    "vol.unit": ("skip", "not installed", ""),
    "vol.grab": ("skip", "daemon not running", ""),
    "vol.topology": ("pass", "physical AT keyboard is free (at-kbd)", ""),
    "vol.holder": ("skip", "holder scan needs root (/proc/*/fdinfo)", ""),
    "vol.hwdb": ("skip", "not installed", ""),
    "hid.rule": ("skip", "not installed", "no /etc/udev/rules.d/90-oxp3-hid.rules"),
    "led.status": ("pass", "protocol readable on /dev/hidraw4", ""),
    "led.zones": ("pass", "5 zones, 11 presets", ""),
    "hid.access": ("skip", "udev rule not installed", ""),
    "homebtn.unit": ("skip", "user unit not installed", ""),
    "homebtn.mapping": ("skip", "unit not installed", ""),
    "homebtn.press": ("skip", "unit not installed", ""),
    "led.write": ("skip", "gated: only with --full", ""),
    "rgbd.unit": ("skip", "no user config", ""),
    "rgbd.check": ("skip", "no user config", ""),
    "life.fd": ("skip", "no lifecycle daemon active", ""),
    "life.reconnect": ("skip", "no lifecycle journal in this boot", ""),
    "life.sim": ("skip", "gated: only with --full", ""),
    "sleep.stats": ("skip", "no suspend attempt since boot", ""),
    "sleep.units": ("skip", "not installed", ""),
    "sleep.wakeup": ("skip", "sleep policy not installed", ""),
    "sleep.trigger": ("skip", "trigger policy not installed",
                      "logind power=poweroff suspend=suspend (compiled default)"),
    "bt.dracut": ("skip", "no dracut drop-in", ""),
    "bt.journal": ("fail", "kernel logged a firmware load failure",
                   "Nov 08 16:15:18 <host> kernel: Bluetooth: hci0: Failed to load "
                   "Intel firmware file intel/ibt-00a0-0291-iml.sfi (-2)"),
    "bt.controller": ("fail", "hci0 exists but btmgmt lists 0 controllers (no firmware)", ""),
    "bt.diag": ("fail", "action needed (action)", "files ok=2/2, journal failure=1"),
    "pwr.profile": ("skip", "no saved profile", ""),
    "pwr.adaptive": ("skip", "not installed (opt-in not used)", ""),
    "gfx.xmx.trace": ("skip", "trace not present (expected unless tracing is enabled)", ""),
    "sched.state": ("skip", "gaming scheduler not installed (opt-in)", ""),
    "sched.scx": ("skip", "gaming scheduler not installed (opt-in)", ""),
    "sched.unit": ("skip", "not installed (opt-in)", ""),
    "decky.dir": ("skip", "not installed", ""),
    "decky.backend": ("skip", "not installed", ""),
    "decky.status": ("pass", "overall=warn (ok=23 warn=5 fail=0 unknown=0)", ""),
})


def checks(table):
    out = []
    for cid, spec in table.items():
        state, summary = spec[0], spec[1]
        detail = spec[2] if len(spec) > 2 else ""
        group = next((g for p, g in GROUP.items() if cid.startswith(p)), "system")
        out.append({
            "id": cid,
            "group": group,
            "title": TITLE.get(cid, cid),
            "state": state,
            "summary": summary,
            "detail": detail,
            "remediation": "" if state == "pass" else "fix: see docs/VALIDATION.md",
        })
    return out


# ---------------------------------------------------------------- file writers
def checklist_rows(states, default=("ok", "")):
    """18 install-order rows; states maps id -> (state, detail)."""
    order = [
        ("driver-stack", "driver stack (kernel, Mesa, gamescope)"),
        ("hw-install", "hardware install (libexec helpers)"),
        ("udev-rules", "udev rules + volume force-release hwdb"),
        ("sleep-policy", "sleep trigger policy (logind drop-in + polkit)"),
        ("logind-restart", "logind restarted after the drop-in"),
        ("sleep-units", "suspend policy units enabled"),
        ("bluetooth", "Bluetooth firmware (updates/ + initramfs)"),
        ("power-profile", "power envelope (PL1/PL2/PL4)"),
        ("battery-care", "battery charge limit / inhibit"),
        ("imu-blacklist", "IMU modprobe blacklist"),
        ("audio-eq", "speaker EQ drop-in"),
        ("rgb-config", "RGB keeper config"),
        ("homebtn-unit", "home button / LED user unit"),
        ("display", "HDR / ICC / vibrance tooling"),
        ("xmx", "XMX driver + per-game ICD"),
        ("decky-plugin", "Decky status plugin"),
        ("suspend-evidence", "a real suspend recorded since boot"),
        ("collector", "evidence bundle collected"),
    ]
    return [
        {"id": sid, "title": title, "state": states.get(sid, default)[0],
         "detail": states.get(sid, default)[1], "command": ""}
        for sid, title in order
    ]


def manifest(checklist, kernel=KERNEL, generated="2026-11-09T04:06:52Z", uptime=42696.63,
             counts=None):
    counts = counts or {"artifacts": 22, "ok": 22, "missing": 0, "denied": 0,
                        "unavailable": 0, "timeout": 0, "error": 0, "skipped": 0,
                        "truncated": 0}
    return {
        "schema": 1,
        "tool": "oxp3-firstrun",
        "version": "1.0.0",
        "generated_at": generated,
        "mode": "collect",
        "out": "/tmp/fixture-bundle",
        "repo": "~/git/linux_scratch/oxp3-anatase-fixes",
        "root": False,
        "kernel": kernel,
        "os": "Anatase",
        "limits": {"max_bytes": 5242880, "max_lines": 2000},
        "counts": counts,
        "journal": {"available": True, "detail": "ok",
                    "files": ["journal/oxp3-units.txt", "journal/kernel.txt",
                              "journal/sleep-tag.txt", "journal/oxp3-user-units.txt"]},
        "redaction": {"total": 0, "rules": {}, "files_with_matches": 0},
        "checklist": checklist,
        "artifacts": [],
        "notes": [],
        "complete": True,
        "partial_data": False,
    }


def environment(kernel=KERNEL, uptime=42696.63, dirty=False):
    git = {"branch": "main", "head": "eb384f7", "dirty": "", "dirty_lines": "0"}
    if dirty:
        git.update({"dirty": "M tools/oxp3-triage.py", "dirty_lines": "1"})
    return "\n".join([
        "== identity",
        "kernel           : %s" % kernel,
        "os               : Anatase",
        "arch             : x86_64",
        "boot_id          : (redacted: bare UUIDs are treated as device identifiers)",
        "uptime_s         : %s" % uptime,
        "euid             : 1000",
        "repo             : ~/git/linux_scratch/oxp3-anatase-fixes",
        "git              : %s" % json.dumps(git, sort_keys=True),
        "",
        "== /proc/cmdline",
        "BOOT_IMAGE=(hd0,gpt2)/ostree/anatase/vmlinuz-7.2.7-an28.fc45.x86_64 "
        "rhgb quiet root=UUID=<redacted:uuid> rw xe.enable_panel_replay=0",
        "",
        "== DMI / board identifiers (values redacted by policy)",
        "  sys_vendor       ONE-NETBOOK",
        "  product_name     ONEXPLAYER 3",
        "  board_name       ONEXPLAYER 3",
        "  bios_version     5.09",
        "  bios_date        08/10/2026",
        "  product_serial   (not readable)",
        "",
        "== module parameters (xe / xe.enable_*)",
        "  enable_sagv                              0",
        "",
    ])


def udev(files, hwdb_present=False, hwdb_mtime="2026-11-09T03:58:00Z",
         hwdb_bin_mtime="2026-11-09T04:00:00Z", logind_dropin=False,
         polkit=False, dracut=False, power_json=False, imu=True):
    paths = [
        ("/etc/udev/rules.d/90-oxp3-hid.rules", "hidraw access rule"),
        ("/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules", "sleep wakeup rule"),
        ("/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb", "volume force-release hwdb"),
        ("/etc/polkit-1/rules.d/49-oxp3-suspend.rules", "suspend polkit rule"),
        ("/etc/systemd/logind.conf.d/60-oxp3-sleep.conf", "logind sleep policy"),
        ("/etc/modprobe.d/oxp3-bmi160.conf", "IMU blacklist"),
        ("/etc/dracut.conf.d/60-oxp3-bt-firmware.conf", "Bluetooth initramfs drop-in"),
        ("/etc/oxp3/power.json", "saved power profile"),
    ]
    present = {
        "/etc/udev/rules.d/90-oxp3-hid.rules": True,
        "/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules": True,
        "/etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb": hwdb_present,
        "/etc/polkit-1/rules.d/49-oxp3-suspend.rules": polkit,
        "/etc/systemd/logind.conf.d/60-oxp3-sleep.conf": logind_dropin,
        "/etc/modprobe.d/oxp3-bmi160.conf": imu,
        "/etc/dracut.conf.d/60-oxp3-bt-firmware.conf": dracut,
        "/etc/oxp3/power.json": power_json,
    }
    present.update(files)
    lines = ["== installed rules and drop-ins", ""]
    for path, label in paths:
        if present.get(path):
            lines.append("  %-64s %-9s %s" % (path, "present", "a" * 64))
            lines.append("      %-60s %s  %s B" % (label, hwdb_mtime, 410))
        else:
            lines.append("  %-64s %-9s" % (path, "MISSING"))
    lines += ["", "== volume hwdb"]
    if hwdb_present:
        lines.append("  /etc/udev/hwdb.d/84-oxp3-volume-keys-taps.hwdb %s" % ("a" * 64))
    else:
        lines.append("  (no 84-oxp3-volume-keys*.hwdb installed)")
    if hwdb_present:
        lines.append("  /etc/udev/hwdb.bin present, mtime %s" % hwdb_bin_mtime)
    else:
        lines.append("  /etc/udev/hwdb.bin present, mtime 2026-11-08T16:16:31Z")
    lines += ["", "== udevadm verify on the installed rules"]
    for path in ("/etc/udev/rules.d/90-oxp3-hid.rules",
                 "/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules"):
        lines.append("  %s: %s" % (path, "clean" if present.get(path) else "not installed"))
    return "\n".join(lines) + "\n"


def firmware(files_present=True, dracut=False, hci0=True, modules=True):
    lines = ["== pinned Bluetooth firmware (paths and hashes only, never the bytes)", ""]
    for name, size, digest in (
        ("ibt-00a0-0291-iml.sfi", 63056, "69beb77387a79a75e741ff6f88f782a0fc44c8b2efcc7bcb41c413122928026"),
        ("ibt-00a0-0291-pci.sfi", 998916, "6ea03de60bdd6231387f561fbae53b1aaedfd906437b3c2216b45df1d4f01e45"),
    ):
        if files_present:
            lines.append("  %-70s %-9s %s B  %s" % (
                "/usr/lib/firmware/updates/intel/" + name, "present", size, digest))
            lines.append("      mtime 2026-11-08T14:02:11Z")
        else:
            lines.append("  %-70s %-9s %s B" % (
                "/usr/lib/firmware/updates/intel/" + name, "MISSING", "?"))
    lines += ["", "== module state", ""]
    for module in ("btintel_pcie", "btintel", "bluetooth"):
        lines.append("  %-16s %s" % (module, "loaded" if modules else "not loaded"))
    lines.append("  modinfo btintel_pcie: /lib/modules/%s/updates/drivers/bluetooth/"
                 "btintel_pcie.ko.xz" % KERNEL)
    lines += ["", "== controller / initramfs drop-in", ""]
    lines.append("  /sys/class/bluetooth/hci0 %s" % ("present" if hci0 else "absent"))
    if dracut:
        lines.append("  /etc/dracut.conf.d/60-oxp3-bt-firmware.conf:")
        lines.append('    install_items+=" /usr/lib/firmware/updates/intel/'
                     'ibt-00a0-0291-iml.sfi /usr/lib/firmware/updates/intel/'
                     'ibt-00a0-0291-pci.sfi "')
    else:
        lines.append("  /etc/dracut.conf.d/60-oxp3-bt-firmware.conf MISSING")
    lines.append("  (the running initramfs only carries the files after a rebuild and reboot)")
    return "\n".join(lines) + "\n"


def sleep_txt(success=0, fail=0, failed_suspend=0, failed_resume=0, last_hw=0,
              max_hw=523986009990, inhibitors=None, mem_sleep="[s2idle] deep",
              trigger_note=None):
    lines = ["== sleep capability", "",
             "  /sys/power/mem_sleep     %s" % mem_sleep,
             "  /sys/power/state         freeze mem",
             "  /sys/power/disk          [disabled]",
             "", "== suspend_stats (this boot)"]
    for name, value in (("success", success), ("fail", fail),
                        ("failed_suspend", failed_suspend), ("failed_resume", failed_resume),
                        ("last_failed_dev", ""), ("last_failed_errno", 0),
                        ("last_failed_step", ""), ("last_hw_sleep", last_hw),
                        ("max_hw_sleep", max_hw),
                        ("total_hw_sleep", last_hw)):
        lines.append("  %-20s %s" % (name, value))
    lines += ["", "== wake sources", "== /proc/acpi/wakeup (enabled entries)",
              "  LID0  S3  *enabled  platform:PNP0C0D:00",
              "  XHCI  S3  *enabled  pci:0000:00:14.0",
              "", "== /sys/class/wakeup (armed sources)",
              "  wakeup0                  device:15                          active=0 events=0",
              "  wakeup23                 0000:00:1c.0                       active=0 events=0",
              "", "== repository audit (tools/sleep-power-measure.sh audit, read-only)",
              "== suspend capability",
              "mem_sleep       : %s" % mem_sleep,
              "state           : freeze mem ",
              "hibernate (disk): [disabled]",
              "lockdown        : none [integrity] confidentiality",
              "platform_profile: balanced",
              "", "== kernel suspend record (this boot)",
              "  success            %s" % success,
              "  fail               %s" % fail,
              "  last_failed_dev    ",
              "  last_hw_sleep      %s" % last_hw,
              "  (root shows the S0ix residency and the pmc_core blocker lists)", ""]
    if trigger_note:
        lines += ["== suspend trigger note", "  " + trigger_note, ""]
    if inhibitors is not None:
        lines += ["== suspend inhibitors",
                  "  WHO              UID  USER  PID    COMM            WHAT                                                                       WHY                                                        MODE"]
        for row in inhibitors:
            lines.append("  " + row)
        lines.append("")
        lines.append("  %d inhibitors listed." % len(inhibitors))
    return "\n".join(lines) + "\n"


def audio(server=True, sinks=("oxp3_speaker_eq.sink",), default="oxp3_speaker_eq.sink",
          cards=("0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp",), eq_dropin=True):
    lines = ["== ALSA / SOF cards", ""]
    if cards:
        for card in cards:
            lines.append(card)
            lines.append("                      ONE_NETBOOK-ONEXPLAYER3-Defaultstring")
    else:
        lines.append("(no /proc/asound/cards)")
    lines += ["", "== PipeWire / PulseAudio", ""]
    if server:
        lines += ["-- pactl info",
                  "Server String: /run/user/1000/pulse/native",
                  "Server Name: PulseAudio (on PipeWire 1.6.9)",
                  "Default Sink: %s" % default,
                  "Cookie: <redacted:cookie>",
                  "",
                  "-- pactl list short sinks"]
        for index, sink in enumerate(sinks):
            lines.append("%d\t%s\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED" % (40 + index, sink))
        lines += ["", "-- pactl get-default-sink", str(default), "",
                  "-- filter-chain modules"]
        lines.append("29\tlibpipewire-module-filter-chain\t{" if eq_dropin
                     else "  (none)")
    else:
        lines += ["-- pactl info", "  (no server: connection refused)"]
    lines += ["", "== speaker EQ drop-in"]
    eq_path = "~/.config/pipewire/pipewire.conf.d/51-oxp3-speaker-eq.conf"
    lines.append("  %s %s" % (eq_path, "present" if eq_dropin else "MISSING"))
    if eq_dropin:
        lines.append("  sha256 8caebc145da1919d18f2a63b5be486b4800de1c12864a58dde2c030a4d2081ab")
        lines.append("    # OneXPlayer 3 internal-speaker EQ (ours)")
    lines += ["", "== audio units (user scope)", "",
              "  oxp3-audio-route.service     active",
              "  pipewire.service             active",
              "  wireplumber.service          active"]
    return "\n".join(lines) + "\n"


def xmx(env=None, driver_present=True, driver_hash=XMX, icd=True, kernels=30,
        ocloc=("present", "ocloc-25.18.1"), flatpak_override=True, helper="nodgrf"):
    env = env if env is not None else {
        "VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/intel_icd.x86_64.json",
        "VKD3D_DISABLE_EXTENSIONS": "VK_EXT_descriptor_buffer",
        "ANV_CM_KERNEL_DIR": "~/.cache/xess-xmx/kernels",
    }
    lines = ["== session environment (graphics variables only)", ""]
    for key in sorted(env):
        lines.append("%s=%s" % (key, env[key]))
    lines += ["", "== driver candidates (sha256 of the shipped bytes)", ""]
    lines.append("  %-62s %-9s %s" % ("/usr/lib64/libvulkan_intel.so",
                                      "present" if driver_present else "MISSING",
                                      MESA if driver_present else ""))
    lines += ["", "== per-game ICD files"]
    for relpath in (".config/xess-xmx/icd-xmx.json", ".config/xess-xmx/oxp3-env.sh",
                    ".config/xess-xmx/flatpak-env.sh"):
        present = icd if relpath.endswith("icd-xmx.json") else (
            True if relpath.endswith("oxp3-env.sh") else False)
        lines.append("  %-45s %-9s %s" % ("~/" + relpath, "present" if present else "MISSING",
                                          "b" * 64 if present else ""))
    lines += ["", "== XMX kernel cache"]
    lines.append("  ~/.cache/xess-xmx/kernels: %d entries" % kernels)
    for name in ("0436535cffb0dad9_b080_605b6597.cmk", "07103134d39b8579_b080_605b6597.cmk"):
        lines.append("    " + name)
    lines += ["", "== compiler (IGC / ocloc)"]
    state, value = ocloc
    if state == "present":
        lines.append("  ~/.local/share/xess-xmx/igc/neo/bin/%s present 5016 B ceb5ac61431c01bf"
                     % value)
    else:
        lines.append("  ocloc: not found (no PATH entry, no ~/.local/share/xess-xmx install)")
    lines += ["", "== Steam Flatpak overrides"]
    if flatpak_override:
        lines += ["  [Environment]",
                  "  force_vk_vendor=0",
                  "  ANV_CM_HELPER=~/.local/share/xess-xmx/tools/%s.sh" % (
                      "xmx-cm-compile-nodgrf" if helper == "nodgrf" else "cm-compile"),
                  "  VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer"]
    else:
        lines.append("  (none written)")
    return "\n".join(lines) + "\n"


def units(active=(), not_found=(), failed=()):
    names = ("oxp3-voltap.service", "oxp3-sleep-wakeup.service", "oxp3-sleep-pre.service",
             "oxp3-sleep-log.service", "oxp3-power-profile.service",
             "oxp3-power-resume.service", "oxp3-fan-curve.service",
             "oxp3-adaptivetdp.service", "oxp3-tdp-sync.service", "oxp3-rapl-pp1.service",
             "oxp3-gaming-sched.service", "oxp3-xpad-sleep.service")
    lines = ["== system units (systemctl show)", ""]
    for name in names:
        if name in not_found:
            lines.append("%-32s load=not-found file=           active=inactive  sub=dead"
                         "       restarts=0   exec=0" % name)
        else:
            lines.append("%-32s load=loaded    file=enabled    active=%-9s sub=%-10s"
                         " restarts=0   exec=0" % (name, "active" if name in active else
                                                   "inactive", "running" if name in active
                                                   else "dead"))
    lines.append("%-32s %s" % ("oxp3-audio-resume@.service",
                               "template unit: see list-unit-files"))
    lines.append("%-32s load=loaded    file=enabled    active=active    sub=running  "
                 "restarts=0   exec=0" % "rapl-power.service")
    lines += ["", "== systemctl --failed", ""]
    lines.extend(failed or ["(none)"])
    lines += ["", "== systemctl list-unit-files 'oxp3-*'", ""]
    lines.append("oxp3-audio-resume@.service indirect disabled")
    lines.append("oxp3-rapl-pp1.service      enabled  disabled")
    return "\n".join(lines) + "\n"


def user_units(homebtn=True, rgbd=True, bus=True):
    lines = ["== user units (systemctl --user show)", ""]
    if not bus:
        lines.append("(no user systemd bus in this context: run from the desktop session)")
    for name, present in (("oxp3-homebtn.service", homebtn), ("oxp3-rgbd.service", rgbd),
                          ("oxp3-audio-route.service", True)):
        if present and bus:
            lines.append("%-32s load=loaded    file=enabled    active=active    sub=running"
                         % name)
        else:
            lines.append("%-32s %s" % (name, "missing (no data)" if not bus
                                       else "missing (no data)"))
    lines += ["", "== files (user scope, from the invoking user's home)"]
    for relpath in (".config/oxp3-rgb.toml", ".config/systemd/user/oxp3-homebtn.service",
                    ".config/systemd/user/oxp3-rgbd.service",
                    ".config/xess-xmx/icd-xmx.json"):
        lines.append("  %-58s %s" % ("~/" + relpath, "present" if rgbd else "missing"))
    return "\n".join(lines) + "\n"


def power(mem="balanced", saved=True):
    lines = ["== platform profile", "", "  current : %s" % mem,
             "  choices : low-power balanced performance", "",
             "== saved profile (/etc/oxp3/power.json)"]
    lines.append('  {"profile": "balanced", "pl1_uw": "35000000"}' if saved else "  (not saved)")
    lines += ["", "== powercap / RAPL", "== intel-rapl-mmio:0",
              "  constraint_0_name              long_term",
              "  constraint_0_power_limit_uw    35000000",
              "  constraint_1_name              short_term",
              "  constraint_1_power_limit_uw    52000000",
              "  constraint_2_name              peak_power",
              "  constraint_2_power_limit_uw    122000000"]
    return "\n".join(lines) + "\n"


def session_power(pba=None, lid=None, auto=None, auto_action=None,
                  config_present=True, agent=True, logind=True):
    """One session/power.txt variant.

    ``pba``/``lid`` are PowerDevil action enums (0 nothing, 1 sleep, 2 hibernate,
    3 shutdown, 4 lock, 8 logout); None means the key is not recorded, so
    PowerDevil's built-in default applies. ``agent=None`` models pgrep missing.
    """
    lines = ["# Session power policy: logind, PowerDevil, session polkit agent",
             "# status:  ok", "", "== logind session facts", ""]
    if logind:
        lines += [
            "-- loginctl show-session self -p Type -p Class -p Active -p IdleHint -p State",
            "  Type=wayland", "  Class=user", "  Active=yes", "  State=active",
            "  IdleHint=no", "",
            "-- loginctl list-sessions",
            "  SESSION  UID USER  SEAT  LEADER CLASS   TTY  IDLE SINCE",
            "       43 1000 <user> seat0 146697 user    tty7 no   -",
            "",
        ]
    else:
        lines += ["  unknown (tool missing: loginctl) - session facts not readable", ""]
    lines += ["== KDE PowerDevil settings (powerdevilrc)", "",
              "  tool: kreadconfig6",
              "  file: powerdevilrc"]
    values = {"PowerButtonAction": pba, "LidAction": lid,
              "AutoSuspend": auto, "AutoSuspendAction": auto_action}
    for group in ("AC", "Battery"):
        for key in ("PowerButtonAction", "LidAction", "AutoSuspend", "AutoSuspendAction"):
            value = values[key]
            lines.append("  %-38s = %s" % ("%s.%s" % (group, key),
                                           "(unset)" if value is None else value))
    lines += ["  note: Plasma 6 stores these keys in [AC][SuspendAndShutdown] /",
              "        [Battery][SuspendAndShutdown]; AutoSuspendAction is a timeout in",
              "        seconds (0 = never), not an action.",
              "  note: kreadconfig reads the config of the user this collector runs as;",
              "        the excerpt below is from ~/.config/powerdevilrc.", "",
              "== ~/.config/powerdevilrc", "",
              "  path    : ~/.config/powerdevilrc"]
    if config_present:
        body = []
        for group in ("AC", "Battery"):
            recorded = [(key, value) for key, value in values.items() if value is not None]
            if recorded:
                body.append("[%s][SuspendAndShutdown]" % group)
                body.extend("%s=%s" % (key, value) for key, value in recorded)
        if not body:
            body = ["[AC][SuspendAndShutdown]", "AutoSuspendAction=0"]
        lines += ["  exists  : yes", "  excerpt : first %d line(s)" % len(body),
                  "  --- begin ~/.config/powerdevilrc ---"]
        lines.extend("    " + line for line in body)
        lines.append("  --- end ~/.config/powerdevilrc ---")
    else:
        lines += ["  exists  : no",
                  "  (PowerDevil has not written a config; its built-in defaults apply)"]
    lines += ["", "== session polkit agent (Gamemode has none)", ""]
    if agent is None:
        lines += ["  unknown (tool missing: pgrep)",
                  "  session polkit agent: unknown (tool missing: pgrep)"]
    elif agent:
        lines += [
            "  polkit-kde-authentication-agent-1  : running (pid 147061)",
            "      147061 /usr/libexec/kf6/polkit-kde-authentication-agent-1",
            "  polkit-gnome-authentication-agent-1 : not running (pgrep exit 1)",
            "  session polkit agent: present",
        ]
    else:
        lines += [
            "  polkit-kde-authentication-agent-1  : not running (pgrep exit 1)",
            "  polkit-gnome-authentication-agent-1 : not running (pgrep exit 1)",
            "  session polkit agent: absent",
        ]
    return "\n".join(lines) + "\n"


def display(hdr="disabled"):
    return "\n".join([
        "== display-mode status (kscreen-doctor, ANSI stripped)", "",
        "== connected outputs",
        "Output: 1 eDP-1 <redacted:uuid>",
        "\tenabled",
        "\tconnected",
        "\tPanel",
        "\tModes:  1:1920x1200@144.00*  2:1920x1200@60.00",
        "\tVrr: Never",
        "\tHDR: %s" % hdr,
        "\tWide Color Gamut: disabled",
        "\tICC profile: none",
        "\tColor resolution: automatic (10), range: [6; 12] bits per color",
        "", "== vibrance tooling", "shader present; gamescope: not running", "",
        "== ICC profiles", "  /usr/share/color/icc                        (none)",
        "",
    ])


def decky(installed=True):
    lines = ["== Decky plugin inventory", ""]
    lines.append("  plugin dir: ~/homebrew/plugins (%d plugin(s))" % (14 if not installed else 15))
    lines.extend("    " + name for name in ("SDH-CssLoader", "StorageCleaner", "vibrantDeck"))
    lines += ["", "== oxp3-status plugin"]
    if installed:
        lines += ["  plugin.json            present   " + "c" * 64,
                  "  dist/index.js          present   " + "d" * 64,
                  "  main.py                present   " + "e" * 64,
                  "  version: 1.0.0  name: OXP3 Status"]
    else:
        lines.append("  not installed (no ~/homebrew/plugins/oxp3-status)")
    lines += ["", "== loader unit / zip", "  plugin_loader.service: active",
              "  built zip: (not built here)"]
    return "\n".join(lines) + "\n"


def kernel_journal(lines):
    header = ["# kernel lines: bluetooth/suspend/audio/GPU",
              "# command: journalctl -b -k --no-pager -o short-precise -n 800 "
              "--grep=bluetooth|hci0|btintel|suspend|PM:|snd_|sof-|xe |drm",
              "# exit:    0", ""]
    return "\n".join(header + lines) + "\n"


def unit_journal(lines):
    header = ["# journal for the oxp3 units",
              "# command: journalctl -b --no-pager -o short-precise -n 600 -u oxp3-*",
              "# exit:    0", ""]
    return "\n".join(header + lines) + "\n"


def verify_txt(table, exit_code):
    lines = ["oxp3-verify 1.0.0  mode=default  kernel=%s  root=no" % KERNEL, ""]
    for cid, spec in table.items():
        lines.append("[%-6s] %-10s %-22s %s" % (spec[0].upper(), "-", cid, spec[1]))
    lines += ["", "== summary: %d checks -> exit %d" % (len(table), exit_code)]
    return "\n".join(lines) + "\n"


def status(indicators, overall, counts):
    groups = {}
    for iid, spec in indicators.items():
        group = iid.split(".")[0]
        groups.setdefault(group, []).append({
            "id": iid,
            "label": iid,
            "state": spec[0],
            "value": spec[1],
            "detail": spec[2] if len(spec) > 2 else "",
            "path": "",
        })
    return {
        "schema": 1,
        "version": "1.0.0",
        "generated_at": "2026-11-09T04:06:40Z",
        "overall": overall,
        "counts": counts,
        "host": {"user": "<redacted-user>", "home": "<redacted-home>", "kernel": KERNEL,
                 "hostname": "<redacted-host>"},
        "game": None,
        "env_sources": [{"name": "Heroic config", "live": False, "keys": 8}],
        "groups": [{"id": gid, "title": gid, "state": "ok", "indicators": items}
                   for gid, items in groups.items()],
        "notes": [],
    }


HEALTHY_INDICATORS = {
    "mesa.host": ("ok", "%s (match)" % MESA[:16], ""),
    "mesa.env.vk_driver_files": ("ok", "libvulkan_intel.so -> XMX driver %s" % XMX[:16], ""),
    "xmx.icd": ("ok", "icd-xmx.json -> xmx driver", ""),
    "xmx.helper": ("ok", "xmx-cm-compile-nodgrf.sh", ""),
    "xmx.igc": ("ok", "ocloc-25.18.1", ""),
    "xmx.shim": ("ok", "igdext64.dll present", ""),
    "xmx.driver": ("ok", "%s (match)" % XMX[:16], ""),
    "xmx.kernels": ("ok", "30 kernel(s)", ""),
    "xmx.trace": ("ok", "igdext_trace.log 562 lines", ""),
    "sched.state": ("warn", "disabled", ""),
    "sched.scx_cosmos": ("warn", "not running", ""),
    "sched.unit": ("warn", "not installed", ""),
    "xe.module": ("ok", "layered (xe.ko.xz)", ""),
    "rapl.service": ("ok", "active", ""),
    "power.pl1": ("ok", "35.00 W / 35.00 W", ""),
    "power.profile": ("ok", "balanced", ""),
    "flatpak.runtime": ("ok", "4d043cc2851a11fd (recorded)", ""),
    "flatpak.steam_env": ("ok", "xmx-cm-compile-nodgrf.sh", ""),
    "sys.kernel": ("ok", KERNEL, ""),
    "sys.gpu": ("ok", "8086:B080 (xe), Mesa 27.0.0", ""),
    "sys.cpu": ("ok", "governor powersave x14; EPP balance_power x14", ""),
    "sys.battery": ("ok", "80%, Charging", ""),
    "sys.thermal": ("ok", "CPU package 55\u00b0C, max 55\u00b0C", ""),
}

PREINSTALL_INDICATORS = dict(HEALTHY_INDICATORS)
PREINSTALL_INDICATORS.update({
    "xmx.trace": ("warn", "not found in the prefix", ""),
    "flatpak.steam_env": ("warn", "cm-compile.sh",
                          "the override currently uses cm-compile.sh (-doubleGRF kernels)"),
})


# ---------------------------------------------------------------- scenarios
def base_files(root, table, *, checklist_states, indicators, overall, counts,
               verify_exit, fw=None, sleep=None, audio_txt=None, xmx_txt=None,
               udev_txt=None, units_txt=None, journal_kernel=None, journal_units=None,
               env=None, power_txt=None, display_txt=None, decky_txt=None,
               user_txt=None, session_txt=None, hardware=True, uptime=42696.63):
    (root / "verify").mkdir(parents=True, exist_ok=True)
    (root / "status").mkdir(exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    (root / "units").mkdir(exist_ok=True)
    (root / "udev").mkdir(exist_ok=True)
    (root / "firmware").mkdir(exist_ok=True)
    (root / "power").mkdir(exist_ok=True)
    (root / "sleep").mkdir(exist_ok=True)
    (root / "audio").mkdir(exist_ok=True)
    (root / "display").mkdir(exist_ok=True)
    (root / "gfx").mkdir(exist_ok=True)
    (root / "decky").mkdir(exist_ok=True)
    (root / "session").mkdir(exist_ok=True)
    (root / "system").mkdir(exist_ok=True)
    (root / "journal").mkdir(exist_ok=True)
    rows = checklist_rows(checklist_states)

    (root / "MANIFEST.json").write_text(
        json.dumps(manifest(rows, uptime=uptime), indent=2) + "\n")
    (root / "README.txt").write_text(
        "oxp3 first-run evidence bundle (synthetic fixture)\n"
        "This tree is generated by tests/fixtures/firstrun/build-fixtures.py.\n")
    (root / "REDACTIONS.txt").write_text("# fixture: nothing redacted\n")
    checklist_lines = [
        "oxp3 first-run checklist - OneXPlayer 3 hardware fixes",
        "repo:   ~/git/linux_scratch/oxp3-anatase-fixes",
        "user:   <user>   home: ~   root: no",
        "kernel: %s" % KERNEL,
        "",
        " #  %-34s %-8s %s" % ("step", "state", "detail"),
        "---  %-34s %-8s %s" % ("-" * 34, "-" * 8, "-" * 30),
    ]
    for index, row in enumerate(rows, 1):
        checklist_lines.append(" %2d  %-34s %-8s %s" % (
            index, row["title"][:34], row["state"], row["detail"]))
    checklist_lines += ["", "checklist: %d row(s)" % len(rows), ""]
    (root / "checklist.txt").write_text("\n".join(checklist_lines))
    (root / "verify/verify.json").write_text(json.dumps({
        "schema": 1, "tool": "oxp3-verify", "version": "1.0.0",
        "generated_at": "2026-11-09T04:06:30Z", "mode": "default", "root": False,
        "with_changes": False, "kernel": KERNEL,
        "counts": {"total": len(table),
                   "pass": sum(1 for s in table.values() if s[0] == "pass"),
                   "fail": sum(1 for s in table.values() if s[0] == "fail"),
                   "skip": sum(1 for s in table.values() if s[0] == "skip"),
                   "unknown": sum(1 for s in table.values() if s[0] == "unknown")},
        "exit_code": verify_exit, "checks": checks(table),
    }, indent=2) + "\n")
    (root / "verify/verify.txt").write_text(verify_txt(table, verify_exit))
    (root / "status/status.json").write_text(
        json.dumps(status(indicators, overall, counts), indent=2) + "\n")
    (root / "status/hw-install-status.txt").write_text(
        "libexec: vol=yes sleep-wakeup=yes sleep-log=yes powerctl=yes adaptivetdp=no\n"
        "udev rules: hid=yes sleep-wakeup=yes volume-hwdb=yes\n")
    (root / "state/snapshot.txt").write_text(
        "== environment\nkernel: %s\nuptime: %ss\n" % (KERNEL, uptime))
    (root / "units/system.txt").write_text(
        units_txt if units_txt is not None else units(
            active=("oxp3-voltap.service", "oxp3-sleep-wakeup.service",
                    "oxp3-sleep-pre.service", "oxp3-sleep-log.service"),
            not_found=("oxp3-gaming-sched.service",)))
    (root / "units/user.txt").write_text(
        user_txt if user_txt is not None else user_units())
    (root / "udev/rules.txt").write_text(udev_txt if udev_txt is not None else udev(
        {}, hwdb_present=True, logind_dropin=True, polkit=True, dracut=True,
        power_json=True))
    (root / "firmware/bluetooth.txt").write_text(fw if fw is not None else firmware(
        dracut=True))
    (root / "sleep/sleep.txt").write_text(
        sleep if sleep is not None else sleep_txt(
            success=1, last_hw=8600000000, inhibitors=[POWERDEVIL_INHIBITOR]))
    (root / "audio/audio.txt").write_text(
        audio_txt if audio_txt is not None else audio())
    (root / "display/display.txt").write_text(
        display_txt if display_txt is not None else display())
    (root / "gfx/xmx.txt").write_text(xmx_txt if xmx_txt is not None else xmx())
    (root / "decky/decky.txt").write_text(
        decky_txt if decky_txt is not None else decky(True))
    (root / "system/environment.txt").write_text(env if env is not None else environment())
    if hardware:
        (root / "system/hardware.txt").write_text(
            "== lspci -nn\n00:02.0 VGA compatible controller [0300]: Intel Corporation "
            "Panther Lake [8086:b080]\n== lsusb\nBus 001 Device 001: ID 1d6b:0002 "
            "Linux Foundation 2.0 root hub\n")
    else:
        (root / "system/hardware.txt").unlink(missing_ok=True)
    (root / "power/power.txt").write_text(
        power_txt if power_txt is not None else power())
    (root / "session/power.txt").write_text(
        session_txt if session_txt is not None else session_power(
            pba="1", lid="1", auto="false", auto_action="0", agent=True))
    (root / "journal/kernel.txt").write_text(kernel_journal(journal_kernel or []))
    (root / "journal/oxp3-units.txt").write_text(unit_journal(journal_units or []))
    (root / "journal/oxp3-user-units.txt").write_text(unit_journal([]))
    (root / "journal/sleep-tag.txt").write_text(unit_journal([]))
    (root / "repo/repo-test-fast.txt").parent.mkdir(exist_ok=True)
    (root / "repo/repo-test-fast.txt").write_text(
        "repo-test --fast: 26 suites: 20 pass, 0 fail, 6 skip\n")


def write(name, builder):
    root = HERE / name
    if root.exists():
        shutil.rmtree(str(root))
    root.mkdir(parents=True)
    builder(root)
    print("wrote %s" % root.relative_to(HERE.parent.parent.parent))


def healthy(root):
    base_files(root, HEALTHY,
               checklist_states={}, indicators=HEALTHY_INDICATORS, overall="warn",
               counts={"ok": 25, "warn": 3, "fail": 0, "unknown": 0, "na": 0},
               verify_exit=0,
               journal_kernel=["Nov 09 00:04:31.120000 <host> kernel: Bluetooth: hci0: "
                               "Found Intel (R) Bluetooth 6.0, firmware variant 0x01",
                               "Nov 09 00:04:31.240000 <host> kernel: PM: suspend entry "
                               "(s2idle)"])


def preinstall(root):
    base_files(root, PREINSTALL,
               checklist_states={
                   "hw-install": ("todo", "0/5 helpers installed"),
                   "udev-rules": ("todo", "0/2 rules, hwdb missing"),
                   "sleep-policy": ("todo", "drop-in missing, polkit missing"),
                   "logind-restart": ("unknown", "drop-in absent"),
                   "sleep-units": ("todo", "0/3 enabled"),
                   "bluetooth": ("partial", "firmware 2/2, dracut missing, hci0 present"),
                   "power-profile": ("todo", "profile not saved; PL1 live 35.0 W"),
                   "rgb-config": ("todo", "missing"),
                   "homebtn-unit": ("todo", "not installed"),
                   "decky-plugin": ("todo", "oxp3-status not installed (14 plugin(s))"),
                   "suspend-evidence": ("todo", "success=0 fail=0 hw_sleep=0"),
               },
               indicators=PREINSTALL_INDICATORS, overall="warn",
               counts={"ok": 23, "warn": 5, "fail": 0, "unknown": 0, "na": 0},
               verify_exit=1,
               fw=firmware(dracut=False), udev_txt=udev(
                   {"/etc/udev/rules.d/90-oxp3-hid.rules": False,
                    "/etc/udev/rules.d/99-oxp3-sleep-wakeup.rules": False},
                   hwdb_present=False, logind_dropin=False, polkit=False, dracut=False,
                   power_json=False),
               units_txt=units(active=("oxp3-tdp-sync.service",),
                               not_found=("oxp3-voltap.service", "oxp3-sleep-wakeup.service",
                                          "oxp3-sleep-pre.service", "oxp3-sleep-log.service",
                                          "oxp3-power-profile.service",
                                          "oxp3-power-resume.service",
                                          "oxp3-fan-curve.service",
                                          "oxp3-adaptivetdp.service",
                                          "oxp3-gaming-sched.service")),
               sleep=sleep_txt(success=0, last_hw=0, max_hw=523986009990, inhibitors=[
                   "HandheldDaemon   0    root  6810   systemd-inhibit sleep       "
                   "Handheld Daemon: Restore HDMI-CEC state before sleep   delay",
                   "PowerDevil       1000 <user> 147067 org_kde_powerde "
                   "handle-power-key:handle-suspend-key:handle-hibernate-key:handle-lid-switch "
                   "KDE handles power events   block",
                   "compositor       1000 <user> 146825 kwin_wayland sleep   "
                   "Ensuring that the screen gets locked before going to sleep delay",
               ]),
               audio_txt=audio(),
               xmx_txt=xmx(env={"VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/"
                                                 "intel_icd.x86_64.json",
                                "VKD3D_DISABLE_EXTENSIONS": "VK_EXT_descriptor_buffer"},
                           flatpak_override=True, helper="plain"),
               decky_txt=decky(False),
               user_txt=user_units(homebtn=False, rgbd=False, bus=True),
               power_txt=power(saved=False),
               session_txt=session_power(pba="3", lid="3", auto="false",
                                         auto_action="0", agent=False),
               journal_kernel=["Nov 09 00:04:22.900000 <host> kernel: snd_sof: "
                               "sof_ipc4_widget_setup: ipc4: widget parse error",
                               "Nov 08 16:15:18.349280 <host> kernel: Bluetooth: hci0: "
                               "Failed to load Intel firmware file intel/"
                               "ibt-00a0-0291-iml.sfi (-2)"])


def bt_not_loaded(root):
    table = dict(HEALTHY)
    table.update({
        "bt.dracut": ("pass", "drop-in present; initramfs carries the blobs", ""),
        "bt.journal": ("fail", "kernel logged a firmware load failure",
                       "Nov 09 03:58:11 <host> kernel: Bluetooth: hci0: Failed to load "
                       "Intel firmware file intel/ibt-00a0-0291-iml.sfi (-2)"),
        "bt.controller": ("fail", "hci0 exists but btmgmt lists 0 controllers (no firmware)",
                          ""),
        "bt.diag": ("fail", "action needed (action)", "files ok=2/2, journal failure=1"),
    })
    base_files(root, table,
               checklist_states={"bluetooth": ("partial", "firmware 2/2, dracut present, "
                                                          "hci0 present")},
               indicators=HEALTHY_INDICATORS, overall="ok",
               counts={"ok": 28, "warn": 3, "fail": 0, "unknown": 0, "na": 0},
               verify_exit=1, fw=firmware(dracut=True),
               journal_kernel=["Nov 09 03:58:11.100000 <host> kernel: Bluetooth: hci0: "
                               "Failed to load Intel firmware file intel/"
                               "ibt-00a0-0291-iml.sfi (-2)"])


def xmx_no_trace(root):
    table = dict(HEALTHY)
    table.update({
        "gfx.xmx.driver": ("fail", "%s (expected %s)" % (BAD_XMX[:16], XMX[:16]), ""),
        "gfx.xmx.helper": ("fail", "cm-compile.sh", "not the 128-GRF helper"),
        "gfx.xmx.igc": ("fail", "ocloc not found", ""),
        "gfx.xmx.trace": ("skip", "trace not present (expected unless tracing is enabled)", ""),
    })
    indicators = dict(HEALTHY_INDICATORS)
    indicators.update({
        "xmx.driver": ("warn", "%s (expected %s)" % (BAD_XMX[:16], XMX[:16]), ""),
        "xmx.helper": ("warn", "cm-compile.sh", "not the 128-GRF helper"),
        "xmx.igc": ("fail", "ocloc not found", ""),
        "xmx.trace": ("warn", "not found in the prefix", ""),
        "flatpak.steam_env": ("warn", "cm-compile.sh",
                              "the override uses cm-compile.sh (-doubleGRF kernels)"),
    })
    base_files(root, table,
               checklist_states={"xmx": ("partial", "icd present, ocloc missing")},
               indicators=indicators, overall="warn",
               counts={"ok": 20, "warn": 6, "fail": 1, "unknown": 0, "na": 0},
               verify_exit=1,
               xmx_txt=xmx(env={"VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/"
                                                 "intel_icd.x86_64.json"},
                           ocloc=("missing", ""), helper="plain"),
               journal_kernel=["Nov 09 02:11:07.400000 <host> kernel: i915 0000:00:02.0: "
                               "xe: no CM helper in the environment"])


def audio_wedged(root):
    table = dict(HEALTHY)
    table.update({
        "aud.eq": ("fail", "drop-in installed but oxp3_speaker_eq.sink is missing", ""),
        "aud.dummy": ("unknown", "only auto_null (Dummy Output) visible",
                      "no hardware sink in this context: no audio seat here, or the SOF "
                      "card is wedged"),
        "aud.server": ("pass", "audio server reachable", ""),
    })
    base_files(root, table,
               checklist_states={"audio-eq": ("pending", "drop-in present, sink not loaded")},
               indicators=HEALTHY_INDICATORS, overall="warn",
               counts={"ok": 23, "warn": 4, "fail": 0, "unknown": 1, "na": 0},
               verify_exit=1,
               audio_txt=audio(sinks=("auto_null",), default="auto_null"),
               journal_kernel=["Nov 09 01:40:12.000000 <host> kernel: sof-audio-pci-intel-ptl "
                               "0000:00:1f.3: sof_ipc4_widget_setup: ipc4: widget parse "
                               "error"])


def sleep_never_triggered(root):
    table = dict(HEALTHY)
    table.update({
        "sleep.stats": ("skip", "no suspend attempt since boot", ""),
        "sleep.trigger": ("pass", "drop-in parsed; pkcheck yes; no sleep block", ""),
    })
    base_files(root, table,
               checklist_states={
                   "sleep-policy": ("ok", "drop-in present, polkit present"),
                   "logind-restart": ("pending", "drop-in newer than logind start "
                                                 "(2026-11-09T03:41:12Z)"),
                   "suspend-evidence": ("todo", "success=0 fail=0 hw_sleep=0"),
               },
               indicators=HEALTHY_INDICATORS, overall="ok",
               counts={"ok": 25, "warn": 3, "fail": 0, "unknown": 0, "na": 0},
               verify_exit=0,
               udev_txt=udev({}, hwdb_present=True, logind_dropin=True, polkit=True,
                             dracut=True, power_json=True,
                             hwdb_mtime="2026-11-09T03:58:00Z"),
               session_txt=session_power(pba="3", lid="3", auto="false",
                                         auto_action="0", agent=True),
               sleep=sleep_txt(success=0, last_hw=0, inhibitors=[
                   "HandheldDaemon   0    root  6810   systemd-inhibit sleep       "
                   "Handheld Daemon: Restore HDMI-CEC state before sleep   delay",
                   "PowerDevil       1000 <user> 147067 org_kde_powerde "
                   "handle-power-key:handle-suspend-key:handle-hibernate-key:handle-lid-switch "
                   "KDE handles power events   block",
                   "compositor       1000 <user> 146825 kwin_wayland sleep   "
                   "Ensuring that the screen gets locked before going to sleep delay",
               ]))


def volume_degraded(root):
    table = dict(HEALTHY)
    table.update({
        "vol.topology": ("fail", "conflict predicted: /dev/input/event4 held by hhd",
                         "physical AT keyboard is grabbed by hhd (pid 6810)"),
    })
    base_files(root, table,
               checklist_states={"udev-rules": ("ok", "2/2 rules, hwdb present")},
               indicators=HEALTHY_INDICATORS, overall="warn",
               counts={"ok": 24, "warn": 3, "fail": 0, "unknown": 0, "na": 0},
               verify_exit=1,
               journal_units=["Nov 09 03:59:02.100000 <host> oxp3-voltap[7120]: "
                              "device lost: /dev/input/event23 disappeared; retrying in 1s",
                              "Nov 09 03:59:03.120000 <host> oxp3-voltap[7120]: DEGRADED: "
                              "every candidate is held or refused (EBUSY): "
                              "/dev/input/event4: [Errno 16] Device or resource busy; "
                              "status=degraded-busy exit=3; retrying in 1s"])


def missing_files(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "MANIFEST.json").write_text(json.dumps(manifest(
        checklist_rows({"hw-install": ("todo", "0/5 helpers installed"),
                        "bluetooth": ("todo", "firmware 0/2, dracut missing"),
                        "suspend-evidence": ("todo", "success=?")},
                       default=("unknown", "not collected")),
        counts={"artifacts": 2, "ok": 0, "missing": 0, "denied": 0, "unavailable": 0,
                "timeout": 0, "error": 0, "skipped": 0, "truncated": 0},
    ), indent=2) + "\n")
    (root / "checklist.txt").write_text(
        "oxp3 first-run checklist - OneXPlayer 3 hardware fixes\n"
        "kernel: %s\n\n"
        " #  %-34s %-8s %s\n"
        "---  %-34s %-8s %s\n"
        "  1  %-34s %-8s %s\n"
        "  2  %-34s %-8s %s\n"
        "\nchecklist: 0 ok, 2 todo\n" % (
            KERNEL, "step", "state", "detail", "-" * 34, "-" * 8, "-" * 30,
            "hardware install (libexec helpers)", "todo", "0/5 helpers installed",
            "Bluetooth firmware (updates/ + initram", "todo", "firmware 0/2"))


SCENARIOS = (
    ("healthy", healthy),
    ("preinstall", preinstall),
    ("bt-not-loaded", bt_not_loaded),
    ("xmx-no-trace", xmx_no_trace),
    ("audio-wedged", audio_wedged),
    ("sleep-never-triggered", sleep_never_triggered),
    ("volume-degraded", volume_degraded),
    ("missing-files", missing_files),
)


def main() -> int:
    for name, builder in SCENARIOS:
        write(name, builder)
    print("%d fixtures in %s" % (len(SCENARIOS), HERE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
