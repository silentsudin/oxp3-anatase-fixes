#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare two gpu-bench result directories (summary.json)."""
import argparse, json, sys
from pathlib import Path

def load(d):
    d = Path(d)
    p = d / "summary.json"
    if not p.exists():
        sys.exit(f"missing {p}")
    return json.loads(p.read_text())

def metrics(s):
    out = {}
    vk = s.get("vkpeak", {}).get("metrics", {})
    for k, v in vk.items():
        out[f"vkpeak/{k}"] = v["value"]
    for key, label in (("clpeak", "clpeak-vk"), ("opencl_clpeak", "clpeak-cl")):
        tests = s.get(key, {}).get("json")
        if tests:
            for dev in tests.get("devices", []):
                for t in dev.get("tests", []):
                    for m in t.get("metrics", []):
                        if m.get("value") is not None:
                            out[f"{label}/{m['id']}"] = m["value"]
    if s.get("vkmark", {}).get("score") is not None:
        out["vkmark/score"] = s["vkmark"]["score"]
    for g in ("gravitymark", "gravitymark_rt"):
        v = s.get(g, {})
        if v.get("fps") is not None:
            out[f"{g}/fps"] = v["fps"]
            out[f"{g}/score"] = v.get("score")
    for name, v in (s.get("media") or {}).items():
        if isinstance(v, dict) and v.get("speed_x") is not None:
            out[f"media/{name}"] = v["speed_x"]
    for key, v in (s.get("ai", {}).get("tests") or {}).items():
        if v.get("throughput_fps") is not None:
            out[f"ai/{key}"] = v["throughput_fps"]
    if s.get("blender", {}).get("time"):
        out["blender/cpu_time"] = s["blender"]["time"]
    if s.get("blender_gpu", {}).get("time"):
        out["blender/gpu_time"] = s["blender_gpu"]["time"]
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline"); ap.add_argument("candidate")
    ap.add_argument("--min-pct", type=float, default=0.0)
    args = ap.parse_args()
    a, b = metrics(load(args.baseline)), metrics(load(args.candidate))
    keys = sorted(set(a) | set(b))
    print(f"{'metric':42} {'baseline':>12} {'candidate':>12} {'delta%':>9}")
    for k in keys:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            print(f"{k:42} {str(av):>12} {str(bv):>12} {'-':>9}")
            continue
        pct = (bv - av) / av * 100 if av else 0.0
        if abs(pct) >= args.min_pct:
            print(f"{k:42} {av:12.3f} {bv:12.3f} {pct:+8.2f}%")

if __name__ == "__main__":
    main()
