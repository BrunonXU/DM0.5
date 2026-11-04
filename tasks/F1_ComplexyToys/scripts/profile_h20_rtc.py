"""Offline fixed-seed H20 runtime tuning; no robot connection or publication."""
import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import time
import ctypes
from contextlib import contextmanager

import numpy as np
from PIL import Image
import torch
from f1_toys.inference import Policy


@contextmanager
def threadpool_limits(limits):
    # Only this benchmark process; no environment/package/system changes.
    paths = {line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
             if "openblas" in line and ".so" in line}
    for path in paths:
        lib = ctypes.CDLL(path)
        for symbol in ("openblas_set_num_threads64_", "openblas_set_num_threads",
                       "scipy_openblas_set_num_threads"):
            if hasattr(lib, symbol):
                getattr(lib, symbol)(ctypes.c_int(limits))
                break
        else:
            raise RuntimeError("Unknown OpenBLAS threading API: " + path)
    yield

parser = argparse.ArgumentParser(description=__doc__)
for name in ("inputs", "bundle", "core", "compat", "output"):
    parser.add_argument("--" + name, type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
# Reuse the deployed compatibility shim without running its server main.
source = args.compat.read_text().split("from f1_toys.server import main")[0]
namespace = {"__file__": str(args.compat)}
exec(compile(source, str(args.compat), "exec"), namespace)
row = json.loads((args.inputs / "inputs.jsonl").read_text().splitlines()[0])
replay = row["request"]["prediction_replay"]
images = {k: Image.open(args.inputs / v).convert("RGB") for k, v in row["images"].items()}
policy = Policy(args.bundle, args.core)
from f1_toys import rtc_optimization
rtc_optimization.compile_suffix(policy)
ctx = dict(bootstrap=True, delay_steps=20, shift_steps=20, coverage_steps=42,
           plan_origin_ns=0, base_plan_id=0, committed_actions16=[], previous_absolute_eef10=[])
first, _, _ = policy.predict_rtc(images, replay["state16"], replay["last_submitted16"], ctx, seed=42)
assert first["success"], first
ctx.update(bootstrap=False, base_plan_id=1,
           committed_actions16=first["actions"][20:40],
           previous_absolute_eef10=first["absolute_eef10"][20:])
baseline = None
report = {}
affinity = os.sched_getaffinity(0)
for name, threads, pin in (
    ("threads4", 4, None), ("threads1", 1, None), ("threads2", 2, None),
    ("threads4_numa0", 4, set(range(8, 16))), ("threads1_numa0", 1, set(range(8, 16))),
):
    os.sched_setaffinity(0, affinity if pin is None else pin)
    torch.set_num_threads(threads)
    with threadpool_limits(limits=threads):
        rows = []
        for i in range(25):
            result, raw, _ = policy.predict_rtc(images, replay["state16"],
                                               replay["last_submitted16"], ctx, seed=42)
            assert result["success"], result
            assert np.array_equal(result["actions"][:20], ctx["committed_actions16"])
            if baseline is None:
                baseline = raw.copy()
                np.save(args.output / "baseline.npy", raw)
            assert np.array_equal(raw, baseline), (name, float(np.max(abs(raw-baseline))))
            if i >= 5:
                rows.append(result["timing_ms"])
        report[name] = {key: dict(zip(("p50", "p95", "max"),
                            np.percentile([r[key] for r in rows], [50, 95, 100]).tolist()))
                        for key in rows[0]}
        print(name, json.dumps(report[name]), flush=True)
        (args.output / "results.json").write_text(json.dumps(report, indent=2))
os.sched_setaffinity(0, affinity)
profiler = cProfile.Profile()
profiler.enable()
for _ in range(10):
    policy.predict_rtc(images, replay["state16"], replay["last_submitted16"], ctx, seed=42)
profiler.disable()
with (args.output / "cpu-profile.txt").open("w") as stream:
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumtime").print_stats(60)
