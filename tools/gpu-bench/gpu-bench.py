#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Headless GPU benchmark harness for the OneXPlayer 3 (Intel Arc B390).

Everything is CLI/headless: vkpeak (Vulkan compute/matrix), clpeak (Vulkan
coopmat/numeric, JSON), vkmark (Vulkan graphics via its headless winsys) and
vulkaninfo.  Results and per-run GPU/CPU samples are written as JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(os.environ.get("OXP3_GPU_BENCH_CACHE", Path.home() / ".cache/oxp3-fixes/gpu-bench"))
BIN = CACHE / "bin"
SHARE = CACHE / "share"
DEFAULT_OUT = CACHE / "results"

def run(cmd, timeout=600, log_path=None, cwd=None):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        rc = 124
    except FileNotFoundError:
        out, err, rc = "", f"missing: {cmd[0]}", 127
    text = out + (("\n[stderr]\n" + err) if err else "")
    if log_path:
        Path(log_path).write_text(text)
    return {"cmd": [str(c) for c in cmd], "rc": rc, "seconds": round(time.time() - t0, 3), "output": text}

def first(*paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None

def find_bin(name):
    p = BIN / name
    return str(p) if p.exists() else shutil.which(name)

class Sampler:
    """Sample GPU clock/util (nvtop JSON) and package energy (RAPL)."""

    def __init__(self):
        self.stop = threading.Event()
        self.thread = None
        self.gpu = []
        self.energy_start = self._energy()
        self.energy_end = None
        self.stat_start = self._stat()
        self.stat_end = None

    @staticmethod
    def _energy():
        for base in ("/sys/class/powercap/intel-rapl:0",):
            try:
                return int((Path(base) / "energy_uj").read_text())
            except Exception:
                return None
        return None

    @staticmethod
    def _stat():
        try:
            f = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            vals = [int(x) for x in f]
            return sum(vals), vals[3] + vals[4]
        except Exception:
            return None

    def _sample(self):
        if shutil.which("nvtop"):
            try:
                p = subprocess.run(["nvtop", "-s"], capture_output=True, text=True, timeout=2)
                d = json.loads(p.stdout)[0]

                def num(v):
                    m = re.search(r"[0-9]+(?:\.[0-9]+)?", str(v or ""))
                    return float(m.group(0)) if m else 0.0

                self.gpu.append((num(d.get("gpu_clock")), num(d.get("gpu_util"))))
            except Exception:
                pass

    def _loop(self):
        while not self.stop.is_set():
            self._sample()
            self.stop.wait(0.25)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def finish(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.energy_end = self._energy()
        self.stat_end = self._stat()
        clocks = [c for c, _ in self.gpu if c]
        util = [u for _, u in self.gpu]
        energy = None
        if self.energy_start is not None and self.energy_end is not None:
            energy = (self.energy_end - self.energy_start) / 1e6
        cpu = None
        if self.stat_start and self.stat_end:
            dt = self.stat_end[0] - self.stat_start[0]
            db = (self.stat_end[1] - self.stat_start[1])
            cpu = 100.0 * db / dt if dt else None
        return {
            "samples": len(self.gpu),
            "gpu_clock_mhz_avg": round(sum(clocks) / len(clocks), 1) if clocks else None,
            "gpu_clock_mhz_max": max(clocks) if clocks else None,
            "gpu_util_pct_avg": round(sum(util) / len(util), 1) if util else None,
            "gpu_util_pct_max": max(util) if util else None,
            "package_energy_j": round(energy, 2) if energy is not None else None,
            "cpu_busy_pct": round(cpu, 1) if cpu is not None else None,
        }

def meta():
    info = {"timestamp": datetime.now(timezone.utc).isoformat(), "kernel": os.uname().release}
    for key, path in {
        "cmdline": "/proc/cmdline",
        "sched_ext": "/sys/kernel/sched_ext/root/ops",
        "governor": "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
        "epp": "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference",
        "gpu_busy": "/sys/class/drm/card1/device/gpu_busy_percent",
    }.items():
        try:
            info[key] = Path(path).read_text().strip()
        except Exception:
            info[key] = None
    for cmd, key in ((["lspci", "-nn"], "lspci"), (["lpmd_state"], "lpmd")):
        pass
    try:
        p = subprocess.run(["systemctl", "is-active", "intel_lpmd"], capture_output=True, text=True)
        info["intel_lpmd"] = p.stdout.strip()
    except Exception:
        info["intel_lpmd"] = None
    try:
        info["lspci_vga"] = "\n".join(
            l for l in subprocess.run(["lspci", "-nn"], capture_output=True, text=True).stdout.splitlines()
            if "VGA" in l or "Display" in l
        )
    except Exception:
        info["lspci_vga"] = None
    return info

def inventory(outdir):
    v = run([shutil.which("vulkaninfo") or "vulkaninfo", "--summary"], timeout=60,
            log_path=outdir / "vulkaninfo-summary.txt")
    full = run([shutil.which("vulkaninfo") or "vulkaninfo"], timeout=120,
               log_path=outdir / "vulkaninfo.txt")
    text = v["output"]
    dev = {}
    for key, pat in {
        "deviceName": r"deviceName\s*=\s*(.+)",
        "driverName": r"driverName\s*=\s*(.+)",
        "driverInfo": r"driverInfo\s*=\s*(.+)",
        "apiVersion": r"apiVersion\s*=\s*(.+)",
    }.items():
        m = re.search(pat, text)
        dev[key] = m.group(1).strip() if m else None
    sel = " ".join(str(v) for v in dev.values() if v)
    dev["software_fallback"] = bool(re.search(r"llvmpipe|swiftshader|lvp", sel, re.I))
    dev["is_intel"] = bool(re.search(r"Intel|Arc", sel, re.I))
    exts = sorted(set(re.findall(r"VK_[A-Z0-9_]+", full["output"])))
    inv = {"device": dev, "extensions": exts, "vulkaninfo_rc": v["rc"],
           "summary_file": "vulkaninfo-summary.txt", "full_file": "vulkaninfo.txt"}
    (outdir / "inventory.json").write_text(json.dumps(inv, indent=2))
    return inv

VKPEAK_RE = re.compile(r"^\s*([a-z0-9-]+)\s*=\s*([0-9.]+)\s+([A-Za-z/]+)", re.M)

def bench_vkpeak(outdir, quick=False):
    exe = find_bin("vkpeak")
    if not exe:
        return {"status": "missing"}
    cmd = [exe, "0", "all"]
    if quick:
        cmd = [exe, "0", "fp16-matrix,int8-matrix,bf16-matrix,bf16-dotprod,fp8-matrix,bf8-matrix,copy-d2d"]
    s = Sampler(); s.start()
    r = run(cmd, timeout=600, log_path=outdir / "vkpeak.txt"); prof = s.finish()
    metrics = {}
    for k, val, unit in VKPEAK_RE.findall(r["output"]):
        metrics[k] = {"value": float(val), "unit": unit}
    zero = [k for k, m in metrics.items() if m["value"] == 0]
    res = {"status": "ok" if r["rc"] == 0 else "error", "metrics": metrics,
           "zero_metrics": zero, "profile": prof, **r}
    (outdir / "vkpeak.json").write_text(json.dumps(res, indent=2))
    return res

def bench_clpeak(outdir):
    exe = find_bin("clpeak")
    if not exe:
        return {"status": "missing"}
    js = outdir / "clpeak.json"
    s = Sampler(); s.start()
    r = run([exe, "--vulkan", "--devices", "vulkan:0", "--compute", "--matrix-compute",
             "--numeric-error", "-o", str(js)], timeout=600,
            log_path=outdir / "clpeak.txt")
    prof = s.finish()
    data = None
    if js.exists():
        try:
            data = json.loads(js.read_text())
        except Exception:
            data = None
    unsupported = []
    if data:
        for dev in data.get("devices", []):
            for t in dev.get("tests", []):
                for m in t.get("metrics", []):
                    if m.get("status") == "unsupported":
                        unsupported.append({"test": t.get("id"), "metric": m.get("id"),
                                            "reason": m.get("reason")})
    res = {"status": "ok" if r["rc"] == 0 and data else "error", "unsupported": unsupported,
           "profile": prof, "json": data, **r}
    (outdir / "clpeak-run.json").write_text(json.dumps(res, indent=2))
    return res

VKMARK_SCENE_RE = re.compile(r"^\[([^\]]+)\](.*?):\s+FPS:\s+([0-9.]+)", re.M)
VKMARK_SCORE_RE = re.compile(r"vkmark Score:\s+([0-9.]+)")

def bench_vkmark(outdir, quick=False, duration=3):
    exe = find_bin("vkmark")
    if not exe:
        return {"status": "missing"}
    data = SHARE / "vkmark-data"
    if not data.exists():
        return {"status": "missing-data", "data_dir": str(data)}
    s = Sampler(); s.start()
    r = run([exe, "--winsys", "headless", "--winsys-dir", str(BIN), "--data-dir", str(data),
             "-b", f":duration={1 if quick else duration}"], timeout=600,
            log_path=outdir / "vkmark.txt")
    prof = s.finish()
    scenes = []
    for name, opts, fps in VKMARK_SCENE_RE.findall(r["output"]):
        scenes.append({"scene": name.strip(), "options": opts.strip(), "fps": float(fps)})
    m = VKMARK_SCORE_RE.search(r["output"])
    res = {"status": "ok" if r["rc"] == 0 else "error",
           "score": float(m.group(1)) if m else None, "scenes": scenes,
           "profile": prof, **r}
    (outdir / "vkmark.json").write_text(json.dumps(res, indent=2))
    return res

def opencl_status(outdir):
    cl = shutil.which("clinfo")
    if not cl:
        return {"installed": False}
    r = run([cl, "-l"], timeout=30, log_path=outdir / "clinfo.txt")
    platforms = len(re.findall(r"Platform #", r["output"]))
    devices = len(re.findall(r"Device #|^\s+Device", r["output"], re.M))
    # clinfo -l prints one line per device after the platform header
    lines = [l for l in r["output"].splitlines() if l.strip() and not l.startswith("Platform")]
    return {"installed": True, "platforms": platforms, "device_lines": len(lines),
            "raw": r["output"]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-vkmark", action="store_true")
    ap.add_argument("--skip-vkpeak", action="store_true")
    args = ap.parse_args()
    outdir = Path(args.out) if args.out else DEFAULT_OUT / f"{datetime.now():%Y%m%d-%H%M%S}-{args.tag}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"results: {outdir}")
    m = meta(); (outdir / "meta.json").write_text(json.dumps(m, indent=2))
    inv = inventory(outdir)
    print("device:", inv["device"])
    if inv["device"].get("software_fallback"):
        print("ERROR: Vulkan device is a software fallback", file=sys.stderr)
    results = {"meta": m, "inventory": inv, "opencl": opencl_status(outdir)}
    if not args.skip_vkpeak:
        print("vkpeak..."); results["vkpeak"] = bench_vkpeak(outdir, args.quick)
    print("clpeak..."); results["clpeak"] = bench_clpeak(outdir)
    if not args.skip_vkmark:
        print("vkmark..."); results["vkmark"] = bench_vkmark(outdir, args.quick)
    (outdir / "summary.json").write_text(json.dumps(results, indent=2))
    # Markdown summary
    lines = [f"# GPU benchmark {m['timestamp']}", "", f"- kernel: `{m['kernel']}`",
             f"- scheduler: `{m.get('sched_ext')}`",
             f"- governor: `{m.get('governor')}` EPP: `{m.get('epp')}`",
             f"- device: `{inv['device'].get('deviceName')}` driver `{inv['device'].get('driverInfo')}`", ""]
    vk = results.get("vkpeak", {})
    if vk.get("metrics"):
        lines += ["## vkpeak", "", "| metric | value | unit |", "|---|---:|---|"]
        for k, v in vk["metrics"].items():
            lines.append(f"| {k} | {v['value']:.2f} | {v['unit']} |")
        lines += ["", f"zero metrics: {', '.join(vk.get('zero_metrics', [])) or 'none'}", ""]
    cl = results.get("clpeak", {})
    if cl.get("json"):
        lines += ["## clpeak coopmat", ""]
        for dev in cl["json"].get("devices", []):
            for t in dev.get("tests", []):
                for met in t.get("metrics", []):
                    if met.get("status") == "unsupported":
                        lines.append(f"- **unsupported** {met['id']}: {met.get('reason')}")
                    elif met.get("value") is not None:
                        lines.append(f"- {met['id']}: {met['value']:.2f} {met.get('unit','')}")
        lines.append("")
    vkm = results.get("vkmark", {})
    if vkm.get("score") is not None:
        lines += ["## vkmark", "", f"score: **{vkm['score']:.0f}**", ""]
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")
    print((outdir / "summary.md").read_text())
    if inv["device"].get("software_fallback"):
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
