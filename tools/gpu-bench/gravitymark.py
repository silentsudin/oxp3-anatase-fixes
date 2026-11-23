#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cross-API measurement with GravityMark: one scene, four API paths.

Why this tool exists next to d3d-api.py: a game measured under DXVK vs VKD3D vs native Vulkan
mixes the API path with the game's own per-API content. GravityMark ships the *same* scene as a
Windows build (D3D11, D3D12, Vulkan) and a Linux build (Vulkan), and has a deterministic
benchmark mode with a per-frame time report, so the difference between the runs is the API path
alone - no gameplay, no scene differences, no operator timing.

Paths measured:
    d3d11     Windows build under Proton -> DXVK over ANV
    d3d12     Windows build under Proton -> VKD3D-Proton over ANV
    vk-win    Windows build under Proton -> Vulkan (the Wine/wrapper cost, API held constant)
    vk-native Linux build, host Mesa      -> the ceiling for this GPU

Usage:
    tools/gpu-bench/gravitymark.py setup            # extract both builds from the cache
    tools/gpu-bench/gravitymark.py run --api d3d11 --width 1280 --height 720 --label warm
    tools/gpu-bench/gravitymark.py matrix --repeats 3
    tools/gpu-bench/gravitymark.py report <dir-or-result.json> ...

Each run writes <out>/<api>-<WxH>-<label>/ with the frametime report and result.json (mean fps,
mean/99th frametime, 1% low, total frames, and the stack versions), so repeats can be compared
and the noise band is explicit.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CACHE = Path(os.environ.get("OXP3_GPU_BENCH_CACHE", HOME / ".cache/oxp3-fixes/gpu-bench"))
DL = CACHE / "dl"
LINUX_DIR = DL / "gm-linux"
WIN_DIR = DL / "gm-win"
RES = CACHE / "results"
PROTON = Path(os.environ.get(
    "OXP3_PROTON", HOME / ".local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/proton"))
SNIPER = HOME / ".local/share/Steam/steamapps/common/SteamLinuxRuntime_sniper/run"
PREFIX = CACHE / "gm-prefix"

APIS = {
    "d3d11": ["-direct3d11"],
    "d3d12": ["-direct3d12"],
    "vk-win": ["-vulkan"],
    "vk-native": ["-vulkan"],
}


def out(msg: str = "") -> None:
    print(msg, flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def stack_versions() -> dict:
    v = {"kernel": sh(["uname", "-r"]).stdout.strip()}
    f = Path("/usr/lib64/libvulkan_intel.so")
    if f.exists():
        s = sh(["strings", str(f)]).stdout
        v["mesa_cm_injection"] = "ANV_CM_HELPER" in s
        for line in s.splitlines():
            if line.startswith("26.") or line.startswith("27."):
                v["mesa"] = line.strip()
                break
    for key, pat in (("dxvk", "files/lib64/wine/dxvk/version"),
                     ("vkd3d", "files/lib64/wine/vkd3d-proton/version")):
        try:
            v[key] = (PROTON.parent / pat).read_text().strip().split()[-1]
        except OSError:
            v[key] = None
    v["proton"] = PROTON.parent.name
    return v


# --- setup -----------------------------------------------------------------------------------
def extract_makeself(run: Path, dest: Path) -> None:
    """A GravityMark .run is a Makeself archive whose inner installer prompts for its licence,
    so the payload is cut out directly: find the gzip stream and untar it."""
    import gzip
    import io
    import tarfile

    data = run.read_bytes()
    off = data.find(b"\x1f\x8b\x08")
    if off < 0:
        raise SystemExit(f"no gzip payload in {run}")
    raw = gzip.GzipFile(fileobj=io.BytesIO(data[off:])).read()
    dest.mkdir(parents=True, exist_ok=True)
    tarfile.open(fileobj=io.BytesIO(raw)).extractall(dest)


def cmd_setup(a) -> int:
    DL.mkdir(parents=True, exist_ok=True)
    run = next(iter(sorted(DL.glob("GravityMark_*.run"))), None)
    msi = next(iter(sorted(DL.glob("GravityMark_*.msi"))), None)
    if not LINUX_DIR.exists():
        if run is None:
            out("no GravityMark_*.run in the cache (download it from tellusim.com into "
                f"{DL})")
            return 1
        out(f"extracting {run.name}")
        extract_makeself(run, LINUX_DIR)
    if not (WIN_DIR / "bin" / "GravityMark.exe").exists() and msi is not None:
        out(f"extracting {msi.name}")
        shutil.rmtree(WIN_DIR, ignore_errors=True)
        WIN_DIR.mkdir(parents=True, exist_ok=True)
        r = sh(["7z", "x", "-y", f"-o{WIN_DIR}", str(msi)])
        if r.returncode != 0:
            out(r.stdout[-800:] + r.stderr[-800:])
            return 1
        # the Windows build expects bin/ with ../data.zip, like the Linux one
        (WIN_DIR / "bin").mkdir(exist_ok=True)
        for name in ("GravityMark.exe", "Tellusim_x64.dll", "d3dcompiler_47.dll",
                     "dxcompiler.dll", "dxil.dll", "openxr_loader.dll"):
            src = WIN_DIR / name
            if src.exists():
                shutil.copy2(src, WIN_DIR / "bin" / name)
    ok = (LINUX_DIR / "bin" / "GravityMark.x64").exists() and \
        (WIN_DIR / "bin" / "GravityMark.exe").exists() and (WIN_DIR / "data.zip").exists()
    out(f"linux build: {LINUX_DIR / 'bin' / 'GravityMark.x64'}")
    out(f"windows build: {WIN_DIR / 'bin' / 'GravityMark.exe'}")
    out(f"proton: {PROTON}")
    return 0 if ok else 1


# --- statistics ------------------------------------------------------------------------------
def parse_times(path: Path) -> list[float]:
    """GravityMark's -times report: '<index>: <timestamp> <delta seconds> <flags>'."""
    deltas: list[float] = []
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            try:
                deltas.append(float(parts[2]))
            except ValueError:
                continue
    return deltas


def summarise(deltas: list[float], warmup: int = 0) -> dict:
    d = deltas[warmup:] if warmup and len(deltas) > warmup else deltas
    if not d:
        return {"frames": 0}
    total = sum(d)
    s = sorted(d)
    # 1% low: the mean of the slowest 1% of frames (at least one), which is the conventional
    # definition and unambiguous for short logs. The p99 entry is that tail's first frame, so
    # the two numbers describe the same slice instead of disagreeing by one sample.
    tail = s[-max(1, len(s) // 100):]
    low_ft = sum(tail) / len(tail)
    p99 = s[-max(1, len(s) // 100)]
    return {
        "frames": len(d),
        "seconds": round(total, 3),
        "fps_mean": round(len(d) / total, 2),
        "frametime_ms_mean": round(1000 * total / len(d), 3),
        "frametime_ms_p99": round(1000 * p99, 3),
        "fps_1pct_low": round(1.0 / low_ft, 2),
        "fps_min": round(1.0 / max(d), 2),
    }


# --- runs ------------------------------------------------------------------------------------
def run_gravitymark(api: str, width: int, height: int, count: int, dest: Path,
                    timeout: int, extra_env: dict | None = None,
                    taskset: str | None = None) -> tuple[int, str]:
    times = dest / "times.csv"
    common = ["-benchmark", "1", "-close", "1", "-count", str(count),
              "-width", str(width), "-height", str(height), "-vsync", "0",
              "-fullscreen", "0", *APIS[api]]
    env = os.environ.copy()
    env["DISPLAY"] = env.get("DISPLAY", ":0")
    if api == "vk-native":
        cmd = [str(LINUX_DIR / "bin" / "GravityMark.x64"), *common, "-times", str(times)]
        cwd = LINUX_DIR / "bin"
    else:
        if not PROTON.exists():
            raise SystemExit(f"no proton at {PROTON}")
        env.update({
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(HOME / ".local/share/Steam"),
            "STEAM_COMPAT_DATA_PATH": str(PREFIX),
            # DXVK's startup dump lists which Vulkan features it enabled and which it fell back
            # on; without it a slow d3d11 run says nothing about why. The path is a Windows one
            # (Z: is the host root inside the prefix).
            "DXVK_LOG_LEVEL": "info",
            "DXVK_LOG_PATH": "Z:" + str(dest).replace("/", "\\"),
            "DXVK_HUD": "0",
            "VKD3D_DEBUG": "warn",
        })
        PREFIX.mkdir(parents=True, exist_ok=True)
        # Z: is the host root inside the prefix, so the report lands where we want it
        zpath = "Z:" + str(times).replace("/", "\\")
        cmd = [str(PROTON), "run", "./GravityMark.exe", *common, "-times", zpath]
        cwd = WIN_DIR / "bin"
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v is not None})
    if taskset:
        cmd = ["taskset", "-c", taskset, *cmd]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout + p.stderr)[-4000:]


def cmd_run(a) -> int:
    api = a.api
    if api not in APIS:
        out(f"unknown api {api!r}; known: {', '.join(APIS)}")
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    label = a.label or "run"
    dest = Path(a.out or RES / f"gravitymark-{stamp}") / f"{api}-{a.width}x{a.height}-{label}"
    dest.mkdir(parents=True, exist_ok=True)
    out(f"== GravityMark {api} {a.width}x{a.height} x{a.count}")
    extra = dict(kv.split("=", 1) for kv in (a.env or []))
    rc, log = run_gravitymark(api, a.width, a.height, a.count, dest, a.timeout, extra,
                              getattr(a, "taskset", None))
    (dest / "log.txt").write_text(log)
    times = dest / "times.csv"
    deltas = parse_times(times) if times.exists() else []
    res = {
        "tool": "gravitymark", "api": api, "width": a.width, "height": a.height,
        "passes": a.count, "label": label, "rc": rc,
        "note": a.note, "started": stamp, "env": extra,
        "taskset": getattr(a, "taskset", None),
        "stack": stack_versions(),
        "summary": summarise(deltas, a.warmup),
    }
    (dest / "result.json").write_text(json.dumps(res, indent=2) + "\n")
    out(f"   rc={rc} frames={res['summary'].get('frames')} "
        f"fps={res['summary'].get('fps_mean')} ft={res['summary'].get('frametime_ms_mean')}ms")
    if not deltas:
        out("   no frametime report: GravityMark writes it under the Wine prefix if Z: is "
            "unavailable, see log.txt")
    return 0 if deltas else 1


def cmd_matrix(a) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = Path(a.out or RES / f"gravitymark-{stamp}")
    apis = a.apis.split(",") if a.apis else ["d3d11", "d3d12", "vk-win", "vk-native"]
    rc = 0
    for api in apis:
        for i in range(a.repeats):
            label = f"warm{i}" if i == 0 else f"r{i}"
            sub = argparse.Namespace(api=api, width=a.width, height=a.height, count=a.count,
                                     label=label, out=str(root), timeout=a.timeout,
                                     warmup=0, note=a.note, env=a.env,
                                     taskset=getattr(a, "taskset", None))
            rc |= cmd_run(sub)
    (root / "matrix.json").write_text(json.dumps({"apis": apis, "repeats": a.repeats,
                                                  "resolution": [a.width, a.height],
                                                  "passes": a.count}, indent=2) + "\n")
    return rc


def cmd_report(a) -> int:
    rows = []
    for p in map(Path, a.paths):
        files = sorted(p.glob("*/result.json")) if p.is_dir() else [p]
        for f in files:
            if not f.exists():
                continue
            d = json.loads(f.read_text())
            s = d.get("summary") or {}
            if s.get("frames"):
                rows.append({"api": d["api"], "res": f"{d['width']}x{d['height']}",
                             "label": d.get("label", ""), **s})
    if not rows:
        out("no results")
        return 1
    out(f"{'api':10s} {'res':10s} {'label':7s} {'fps':>7s} {'ft_ms':>7s} {'p99':>7s} {'1%low':>7s}")
    for r in sorted(rows, key=lambda x: (x["api"], x["label"])):
        out(f"{r['api']:10s} {r['res']:10s} {r['label']:7s} {r['fps_mean']:>7.2f} "
            f"{r['frametime_ms_mean']:>7.2f} {r['frametime_ms_p99']:>7.2f} {r['fps_1pct_low']:>7.2f}")
    # per-API median over repeats, and the gap to the best API at the same resolution
    for res in sorted({r["res"] for r in rows}):
        per = {}
        for r in rows:
            if r["res"] == res:
                per.setdefault(r["api"], []).append(r["fps_mean"])
        if len(per) < 2:
            continue
        med = {k: statistics.median(v) for k, v in per.items()}
        best = max(med.values())
        out("")
        out(f"median fps at {res} (gap to the fastest path):")
        for api in sorted(med, key=lambda k: -med[k]):
            out(f"  {api:10s} {med[api]:7.2f}  {100 * (med[api] / best - 1):+6.1f}%"
                f"  (n={len(per[api])})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="extract both GravityMark builds from the cache")
    s.set_defaults(func=cmd_setup)
    s = sub.add_parser("run", help="one GravityMark run")
    s.add_argument("--api", required=True, choices=sorted(APIS))
    s.add_argument("--width", type=int, default=1280)
    s.add_argument("--height", type=int, default=720)
    s.add_argument("--count", type=int, default=1, help="benchmark passes")
    s.add_argument("--warmup", type=int, default=0, help="frames to drop at the start")
    s.add_argument("--label", default="")
    s.add_argument("--note", default="")
    s.add_argument("--out", default=None)
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("--env", action="append", metavar="K=V",
                   help="extra environment for the run, e.g. DXVK_CONFIG='dxvk.enableGraphicsPipelineLibrary = False'")
    s.add_argument("--taskset", default=None, help="CPU list for the run, e.g. 0-1")
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("matrix", help="every API, N repeats")
    s.add_argument("--apis", default=None)
    s.add_argument("--repeats", type=int, default=3)
    s.add_argument("--width", type=int, default=1280)
    s.add_argument("--height", type=int, default=720)
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--note", default="")
    s.add_argument("--out", default=None)
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("--env", action="append", metavar="K=V")
    s.add_argument("--taskset", default=None)
    s.set_defaults(func=cmd_matrix)
    s = sub.add_parser("report", help="compare result files")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_report)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
