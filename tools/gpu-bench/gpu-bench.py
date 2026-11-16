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
TOOLS = CACHE / "tools"

# oneVPL GPU runtime (intel-vpl-gpu-rt) extracted by media-setup.sh: without it
# hevc_qsv fails with "Error creating a MFX session: -9".
_npu = TOOLS / "npu" / "usr" / "lib" / "x86_64-linux-gnu"
_ze = TOOLS / "npu" / "ze" / "lib64"
if _npu.exists():
    os.environ["LD_LIBRARY_PATH"] = str(_npu) + ":" + str(_ze) + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else "")
_vpl = TOOLS / "vpl" / "usr" / "lib64"
if _vpl.exists():
    os.environ["LD_LIBRARY_PATH"] = str(_vpl) + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else "")
DEFAULT_OUT = CACHE / "results"

# If opencl-setup.sh has been run, make rusticl load the extracted libclc.
_shim = BIN / "libclc-shim.so"
_clc = SHARE / "clc"
if _shim.exists() and (_clc / "spirv-mesa3d-.spv").exists():
    os.environ.setdefault("OXP3_LIBCLC_DIR", str(_clc))
    os.environ["LD_PRELOAD"] = str(_shim)
    os.environ.setdefault("RUSTICL_ENABLE", "iris")

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
    full = run([shutil.which("vulkaninfo") or "vulkaninfo", "--show-all"], timeout=180,
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
    coop = []
    for block in re.findall(r"VkCooperativeMatrixPropertiesKHR:\n((?:\s+\w+\s*=.*\n)+)", full["output"]):
        fields = dict(re.findall(r"^\s*(\w+)\s*=\s*(\S+)", block, re.M))
        def as_int(k):
            try: return int(fields.get(k, "0"))
            except ValueError: return 0
        coop.append({"MSize": as_int("MSize"), "NSize": as_int("NSize"), "KSize": as_int("KSize"),
                     "AType": fields.get("AType"), "BType": fields.get("BType"),
                     "CType": fields.get("CType"), "ResultType": fields.get("ResultType"),
                     "scope": fields.get("scope")})
    target = [c for c in coop if c["MSize"] == 16 and c["NSize"] == 8 and c["KSize"] == 16
              and c["AType"] == "COMPONENT_TYPE_FLOAT16_KHR"
              and c["BType"] == "COMPONENT_TYPE_FLOAT16_KHR"]
    inv = {"device": dev, "extensions": exts,
           "cooperative_matrix_properties": coop,
           "has_16x8x16_f16": bool(target),
           "vulkaninfo_rc": v["rc"],
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

def bench_opencl(outdir):
    exe = find_bin("clpeak")
    if not exe:
        return {"status": "missing"}
    js = outdir / "clpeak-opencl.json"
    s = Sampler(); s.start()
    r = run([exe, "--opencl", "--devices", "opencl:0", "--compute",
             "--single-precision-compute", "--half-precision-compute", "-o", str(js)],
            timeout=300, log_path=outdir / "clpeak-opencl.txt")
    prof = s.finish()
    data = None
    if js.exists():
        try:
            data = json.loads(js.read_text())
        except Exception:
            pass
    unsupported = []
    if data:
        for dev in data.get("devices", []):
            for t in dev.get("tests", []):
                for m in t.get("metrics", []):
                    if m.get("status") == "unsupported":
                        unsupported.append({"test": t.get("id"), "metric": m.get("id"),
                                            "reason": m.get("reason")})
    res = {"status": "ok" if r["rc"] == 0 and data else "error",
           "unsupported": unsupported, "profile": prof, "json": data, **r}
    (outdir / "clpeak-opencl-run.json").write_text(json.dumps(res, indent=2))
    return res

def bench_gravity(outdir, rt=False, asteroids=50000):
    exe = TOOLS / "GravityMark" / "bin" / "GravityMark.x64"
    if not exe.exists():
        return {"status": "missing", "path": str(exe)}
    tag = "rt" if rt else "raster"
    cmd = ["gamescope", "--backend", "headless", "-W", "1280", "-H", "720", "--",
           str(exe), "-vulkan"]
    if rt:
        cmd.append("-raytracing")
        cmd.append("1")
    cmd += ["-benchmark", "1", "-close", "1", "-count", "1",
            "-asteroids", str(asteroids), "-times", str(outdir / f"gravitymark-{tag}-times.txt"),
            "-fps", "1"]
    s = Sampler(); s.start()
    r = run(cmd, timeout=420, log_path=outdir / f"gravitymark-{tag}.txt", cwd=str(exe.parent))
    prof = s.finish()
    def grab(pat):
        m = re.search(pat, r["output"])
        return float(m.group(1)) if m else None
    res = {"status": "ok" if r["rc"] == 0 and "Benchmark Finished" in r["output"] else "error",
           "api": "Vulkan RT" if rt else "Vulkan",
           "score": grab(r"Score:\s*([0-9.]+)"),
           "fps": grab(r"FPS:\s*([0-9.]+)"),
           "scene_seconds": grab(r"Time:\s*([0-9.]+)"),
           "profile": prof, **r}
    (outdir / f"gravitymark-{tag}.json").write_text(json.dumps(res, indent=2))
    return res

def bench_cts(outdir):
    binp = Path(os.environ.get("OXP3_CTS_BIN", str(TOOLS / "vkcts" / "deqp-vk")))
    if not binp.exists():
        return {"status": "missing", "path": str(binp), "hint": "build VK-GL-CTS deqp-vk or set OXP3_CTS_BIN"}
    cases = {
        "cooperative_matrix": "dEQP-VK.*cooperative_matrix*",
        "ray_query": "dEQP-VK.ray_query.*",
        "mesh_shader": "dEQP-VK.mesh_shader.*",
    }
    res = {"status": "ok", "binary": str(binp), "suites": {}}
    for name, pattern in cases.items():
        r = run([str(binp), "--deqp-case=" + pattern,
                 "--deqp-log-filename=" + str(outdir / f"cts-{name}.qpa")],
                timeout=1800, cwd=str(binp.parent),
                log_path=outdir / f"cts-{name}.txt")
        nums = {}
        for key in ("Passed", "Failed", "Not supported", "Warnings"):
            m = re.search(key + r":\s*(\d+)/(\d+)", r["output"])
            if m:
                nums[key.lower().replace(" ", "_")] = {"count": int(m.group(1)), "total": int(m.group(2))}
        res["suites"][name] = {"status": "ok" if r["rc"] == 0 else "error", **nums,
                               "rc": r["rc"], "seconds": r["seconds"]}
        if r["rc"] != 0:
            res["status"] = "error"
    (outdir / "cts.json").write_text(json.dumps(res, indent=2))
    return res

def bench_ai(outdir):
    bench = TOOLS / "ov" / "bin" / "benchmark_app"
    models = TOOLS / "ov-models"
    if not bench.exists() or not (models / "add.xml").exists():
        return {"status": "missing", "path": str(bench)}
    res = {"status": "ok", "tests": {}}
    for model in ("add", "conv"):
        for device in ("NPU", "CPU"):
            key = f"{model}-{device}"
            r = run([str(bench), "-m", str(models / f"{model}.xml"), "-d", device,
                     "-niter", "100", "-api", "sync"], timeout=180,
                    log_path=outdir / f"ai-{key}.txt")
            def num(pat):
                m = re.search(pat, r["output"])
                return float(m.group(1)) if m else None
            res["tests"][key] = {
                "status": "ok" if r["rc"] == 0 and "Throughput" in r["output"] else "error",
                "throughput_fps": num(r"Throughput:\s*([0-9.]+)"),
                "median_ms": (lambda m: (float(m.group(1)) / 1000.0 if m.group(2) == "us" else float(m.group(1))) if m else None)(re.search(r"Median:\s*([0-9.]+)\s*(us|ms)", r["output"])),
                "first_inference_ms": num(r"First inference took\s*([0-9.]+)\s*ms"),
                "rc": r["rc"], "output": r["output"][-2000:]}
    (outdir / "ai.json").write_text(json.dumps(res, indent=2))
    return res

def bench_blender(outdir):
    exe = TOOLS / "blender" / "blender"
    if not exe.exists():
        return {"status": "missing", "path": str(exe)}
    base = [str(exe), "-b", "--factory-startup", "-E", "CYCLES",
            "-o", str(outdir / "blender_"), "-F", "PNG", "-f", "1"]
    s = Sampler(); s.start()
    r = run(base, timeout=600, log_path=outdir / "blender-cpu.txt")
    prof = s.finish()
    times = re.findall(r"^Time:\s*([0-9:.]+)", r["output"], re.M)
    res = {"status": "ok" if r["rc"] == 0 and "Saved:" in r["output"] else "error",
           "time": times[-1] if times else None, "profile": prof, **r}
    (outdir / "blender.json").write_text(json.dumps(res, indent=2))
    return res

def bench_media(outdir):
    ff = TOOLS / "ffmpeg" / "bin" / "ffmpeg"
    if not ff.exists():
        return {"status": "missing", "path": str(ff)}
    tests = {}
    base = [str(ff), "-hide_banner", "-nostdin", "-f", "lavfi",
            "-i", "testsrc2=size=1920x1200:rate=60", "-t", "5"]
    variants = {
        "hevc_qsv": base + ["-c:v", "hevc_qsv", "-preset", "fast", "-f", "null", "-"],
        "hevc_vaapi": [str(ff), "-hide_banner", "-nostdin", "-vaapi_device",
                       "/dev/dri/renderD128"] + base[3:] +
                      ["-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi", "-f", "null", "-"],
        "libx265": base + ["-c:v", "libx265", "-preset", "ultrafast", "-f", "null", "-"],
    }
    for name, cmd in variants.items():
        s = Sampler(); s.start()
        r = run(cmd, timeout=180, log_path=outdir / f"ffmpeg-{name}.txt")
        prof = s.finish()
        m = re.search(r"speed=([0-9.]+)x", r["output"])
        status = "ok" if r["rc"] == 0 else "error"
        tests[name] = {"status": status, "speed_x": float(m.group(1)) if m else None,
                       "profile": prof, **r}
    (outdir / "media.json").write_text(json.dumps(tests, indent=2))
    return tests

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

def build_gaps(results):
    gaps = []
    inv = results.get("inventory", {})
    dev = inv.get("device", {})
    if dev.get("software_fallback"):
        gaps.append({"area": "vulkan", "item": "device", "status": "software-fallback",
                     "detail": dev.get("deviceName")})
    if inv.get("cooperative_matrix_properties") and not inv.get("has_16x8x16_f16"):
        gaps.append({"area": "vulkan-compute", "item": "coopmat 16x8x16 f16", "status": "unadvertised",
                     "detail": "only M=8/N=16 properties are exposed (Longshot needs M=16)"})
    vk = results.get("vkpeak", {})
    for k in vk.get("zero_metrics", []):
        gaps.append({"area": "vulkan-compute", "item": k, "status": "zero",
                     "detail": "vkpeak reports 0 (unexposed or hardware-unsupported)"})
    for key, area in (("clpeak", "vulkan-compute"), ("opencl_clpeak", "opencl")):
        for u in results.get(key, {}).get("unsupported", []):
            gaps.append({"area": area, "item": u.get("metric"), "status": "unsupported",
                         "detail": u.get("reason")})
    for name, v in (results.get("media") or {}).items():
        if isinstance(v, dict) and v.get("status") not in (None, "ok"):
            gaps.append({"area": "media", "item": name, "status": v.get("status"),
                         "detail": (v.get("output", "") or "").strip().splitlines()[-1:]})
    bl = results.get("blender") or {}
    if bl and bl.get("status") not in (None, "ok"):
        gaps.append({"area": "render", "item": "cycles", "status": bl.get("status"),
                     "detail": "Blender render failed"})
    ocl = results.get("opencl") or {}
    if ocl.get("installed") and ocl.get("device_lines", 0) == 0:
        gaps.append({"area": "opencl", "item": "device", "status": "unavailable",
                     "detail": "rusticl platform with zero devices (libclc missing?)"})
    return gaps

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-vkmark", action="store_true")
    ap.add_argument("--gravitymark", action="store_true", help="also run GravityMark raster")
    ap.add_argument("--gravitymark-rt", action="store_true", help="also run GravityMark ray tracing")
    ap.add_argument("--blender", action="store_true", help="also run a Blender Cycles CPU render")
    ap.add_argument("--full", action="store_true", help="run graphics/RT/render extras too")
    ap.add_argument("--ai", action="store_true", help="also run OpenVINO NPU/CPU models")
    ap.add_argument("--cts", action="store_true", help="also run targeted VK-GL-CTS suites")
    ap.add_argument("--skip-vkpeak", action="store_true")
    args = ap.parse_args()
    if args.full:
        args.gravitymark = True
        args.gravitymark_rt = True
        args.blender = True
        args.ai = True
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
    print("opencl..."); results["opencl_clpeak"] = bench_opencl(outdir)
    if not args.skip_vkmark:
        print("media..."); results["media"] = bench_media(outdir)
    if args.gravitymark:
        print("gravitymark raster..."); results["gravitymark"] = bench_gravity(outdir, False)
    if args.gravitymark_rt:
        print("gravitymark RT..."); results["gravitymark_rt"] = bench_gravity(outdir, True, 20000)
    if args.blender:
        print("blender..."); results["blender"] = bench_blender(outdir)
    if args.ai:
        print("ai..."); results["ai"] = bench_ai(outdir)
    if args.cts:
        print("cts..."); results["cts"] = bench_cts(outdir)
    print("vkmark..."); results["vkmark"] = bench_vkmark(outdir, args.quick)
    results["gaps"] = build_gaps(results)
    (outdir / "gaps.json").write_text(json.dumps(results["gaps"], indent=2))
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
    ocl = results.get("opencl_clpeak", {})
    if ocl.get("json"):
        lines += ["## OpenCL (rusticl)", ""]
        for dev in ocl["json"].get("devices", []):
            for t in dev.get("tests", []):
                for met in t.get("metrics", []):
                    if met.get("status") == "unsupported":
                        lines.append(f"- **unsupported** {met['id']}: {met.get('reason')}")
                    elif met.get("value") is not None:
                        lines.append(f"- {met['id']}: {met['value']:.2f}")
        lines.append("")
    med = results.get("media", {})
    if med:
        lines += ["## media (FFmpeg, 1920x1200 testsrc2, 5 s)", "", "| encoder | speed |", "|---|---:|"]
        for name, v in med.items():
            if isinstance(v, dict):
                lines.append(f"| {name} | {v.get('speed_x')}x ({v.get('status')}) |")
        lines.append("")
    for key, label in (("gravitymark", "GravityMark Vulkan"), ("gravitymark_rt", "GravityMark Vulkan RT")):
        g = results.get(key, {})
        if g.get("fps") is not None:
            lines += [f"## {label}", "", f"score: **{g.get('score')}** fps: **{g.get('fps')}** "
                      f"(scene {g.get('scene_seconds')} s)", ""]
    cts = results.get("cts", {})
    if isinstance(cts, dict) and cts.get("suites"):
        lines += ["## VK-GL-CTS (targeted)", "", "| suite | passed | failed | unsupported |", "|---|---:|---:|---:|"]
        for name, v in cts["suites"].items():
            lines.append(f"| {name} | {v.get('passed', {}).get('count')}/{v.get('passed', {}).get('total')} | "
                         f"{v.get('failed', {}).get('count')} | {v.get('not_supported', {}).get('count')} |")
        lines.append("")
    ai = results.get("ai", {})
    if isinstance(ai, dict) and ai.get("tests"):
        lines += ["## OpenVINO", "", "| model-device | throughput (FPS) | median (ms) |", "|---|---:|---:|"]
        for k, v in ai["tests"].items():
            lines.append(f"| {k} | {v.get('throughput_fps')} | {v.get('median_ms')} |")
        lines.append("")
    bl = results.get("blender", {})
    if bl.get("time"):
        lines += ["## Blender Cycles", "", f"CPU render: **{bl['time']}**", ""]
    vkm = results.get("vkmark", {})
    if vkm.get("score") is not None:
        lines += ["## vkmark", "", f"score: **{vkm['score']:.0f}**", ""]
    lines += ["## Gaps", ""]
    if results.get("gaps"):
        for g in results["gaps"]:
            detail = g.get("detail")
            if isinstance(detail, list):
                detail = " ".join(str(x) for x in detail)
            lines.append(f"- **{g['area']} / {g['item']}**: {g['status']} - {detail or ''}")
    else:
        lines.append("- none detected")
    lines.append("")
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")
    print((outdir / "summary.md").read_text())
    if inv["device"].get("software_fallback"):
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
