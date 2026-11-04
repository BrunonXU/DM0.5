"""CPU-only paired replay of frozen predictions; never publishes robot commands."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from f1_toys.eef import physical_actions, decode_prefix
from f1_toys.native.kinematics import ArmModel, ContinuousIK
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.rtc import decode_window, absolute_eef


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k != "latency_ms"}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def stats(values):
    return dict(zip(("p50", "p95", "p99", "max"), np.percentile(values, [50, 95, 99, 100]).tolist()))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", nargs=2, action="append", metavar=("LOG", "STATS"), required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--limit-per-source", type=int, default=400)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    import f1_toys.native.kinematics as original
    config = Path(original.__file__).with_name("ccs_m6_40.MvKDCfg")
    models = [ArmModel(config), FastArmModel(config)]
    solvers = [ContinuousIK(m) for m in models]
    rng = np.random.default_rng(20261020)
    max_error = 0.
    # Both arms, full bounded joint box (including coupled-wrist violations):
    # the mathematical model must remain identical even outside accepted IK.
    for arm in (0, 1):
        for _ in range(1000):
            q = rng.uniform(models[0].low, models[0].high)
            a, b = [m.fk_rad(arm, q, jacobian=True) for m in models]
            for x, y in zip(a, b):
                max_error = max(max_error, float(np.max(abs(x-y))))
                np.testing.assert_array_equal(x, y)
            target = models[0].fk_rad(arm, rng.uniform(models[0].low, models[0].high))
            a, b = [m.residual(arm, q, target) for m in models]
            for x, y in zip(a, b):
                np.testing.assert_array_equal(x, y)
    sources, cases = [], []
    for log, norm in args.source:
        path = Path(log)
        payload = path.read_bytes()
        rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
        rows = [r for r in rows if "prediction_replay" in r and r.get("diagnostics")]
        selected = set(map(int, np.linspace(0, len(rows)-1, min(len(rows), args.limit_per_source), dtype=int)))
        selected.update(i for i, r in enumerate(rows) if not r["success"])
        action_stats = json.loads(Path(norm).read_text())["action"]
        sources.append(dict(path=log, sha256=hashlib.sha256(payload).hexdigest(),
                            total=len(rows), selected=len(selected),
                            stats_sha256=hashlib.sha256(Path(norm).read_bytes()).hexdigest()))
        cases.extend((log, i, rows[i], action_stats) for i in sorted(selected))
    results = []
    with (args.output / "results.jsonl").open("w") as out:
        for n, (source, index, row, action_stats) in enumerate(cases):
            replay = row["prediction_replay"]
            raw = np.asarray(replay["normalized_actions32"])
            state, held = replay["state16"], replay["last_submitted16"]
            results_pair, timings = {}, {}
            # Interleaved AB / BA; same saved model output, no random resampling.
            for which in (n % 2, 1 - n % 2):
                start = time.perf_counter()
                physical = physical_actions(raw, action_stats)
                if row.get("rtc"):
                    context = row["rtc_request"]
                    result = decode_window(solvers[which], physical, state, held,
                                           context["committed_actions16"], context["coverage_steps"])
                    result["absolute_eef10"] = absolute_eef(solvers[which], state, raw, action_stats).tolist()
                else:
                    result = decode_prefix(solvers[which], physical, state, held, row["execute_steps"])
                timings[which] = (time.perf_counter()-start)*1000
                results_pair[which] = result
            equal = clean(results_pair[0]) == clean(results_pair[1])
            item = dict(source=source, index=index, session_id=row["session_id"],
                        request_id=row["request_id"], rtc=row.get("rtc", False),
                        bootstrap=row.get("rtc_request", {}).get("bootstrap"),
                        baseline_ms=timings[0], candidate_ms=timings[1], exact=equal,
                        success=results_pair[0]["success"],
                        solver_calls=len(results_pair[0]["diagnostics"]),
                        saved_success=row["success"])
            if not equal:
                item["differences"] = {str(k): clean(v) for k, v in results_pair.items()}
            out.write(json.dumps(item)+"\n")
            out.flush()
            results.append(item)
            if n % 50 == 0:
                print(json.dumps(dict(completed=n+1, total=len(cases), exact=equal)), flush=True)
    summary = dict(sources=sources, requests=len(results), random_math_cases=2000,
                   max_math_error=max_error, exact_requests=sum(r["exact"] for r in results),
                   success=sum(r["success"] for r in results),
                   saved_success_agreement=sum(r["success"] == r["saved_success"] for r in results),
                   robot_commands=0, solver_changed=False, config_sha256=models[0].config_sha256,
                   timing_ms={})
    for group, subset in (("all", results), ("rtc_guided", [r for r in results if r["rtc"] and not r["bootstrap"]]),
                          ("rtc_bootstrap", [r for r in results if r["bootstrap"]]),
                          ("rejected", [r for r in results if not r["success"]])):
        if subset:
            summary["timing_ms"][group] = dict(count=len(subset),
                baseline=stats([r["baseline_ms"] for r in subset]),
                candidate=stats([r["candidate_ms"] for r in subset]))
    summary["source_hashes"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (Path(original.__file__), Path(__file__),
                                         Path(original.__file__).with_name("fast_kinematics.py"))}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary), flush=True)
    if summary["exact_requests"] != len(results):
        raise SystemExit("Candidate changes replay outputs; do not deploy.")


if __name__ == "__main__":
    main()
