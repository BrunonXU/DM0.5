"""Summarize complete zero-output RTC journals without modifying source data."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("run", type=Path)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
journal = a.run / "journal"
assert journal.is_dir(), "Session has not published a complete journal"
manifest = json.loads((journal / "manifest.json").read_text())
assert manifest.get("complete"), manifest


def read(name):
    return [json.loads(line) for line in (journal / (name + ".jsonl")).read_text().splitlines()]


def stats(values):
    return dict(zip(("p50", "p95", "p99", "max"),
                    np.percentile(values, [50, 95, 99, 100]).tolist())) if values else {}


events = read("events")
control = read("control")
timings = [r["timing"] for r in events if r["event"] == "policy_transport_timing"]
adopted = [r for r in events if r["event"] == "rtc_plan_adopted"]
report = dict(run=str(a.run), events=dict(Counter(r["event"] for r in events)),
              requests=len(timings), outcomes=dict(Counter(r.get("outcome") for r in timings)),
              plans_adopted=len(adopted), metrics_ms={}, control_records=len(control),
              published_records=sum(r.get("publication_complete") is True for r in control),
              submitted_records=sum(r.get("submitted") is not None for r in control),
              execution_modes=dict(Counter(r.get("execution_mode") for r in control)),
              journal_sha256={name: hashlib.sha256((journal/name).read_bytes()).hexdigest()
                              for name in ("events.jsonl", "control.jsonl", "policy_chunks.jsonl")},
              limitation="Zero-output stationary scene; no closed-loop success inference.")
assert report["published_records"] == report["submitted_records"] == 0, report
for key in ("client_total_ms", "network_rtt_ms", "pack_ms", "observation_to_response_ms"):
    report["metrics_ms"][key] = stats([r[key] for r in timings if isinstance(r.get(key), (int, float))])
for key in sorted({k for r in timings for k in (r.get("server") or {})}):
    report["metrics_ms"]["server_" + key] = stats([
        r["server"][key] for r in timings if r.get("server") and key in r["server"]])
report["metrics_ms"]["rtt_minus_worker_ms"] = stats([
    r["network_rtt_ms"]-r["server"]["worker_total"] for r in timings
    if r.get("network_rtt_ms") is not None and (r.get("server") or {}).get("worker_total") is not None])
report["model_ids"] = sorted({r["model_id"] for r in timings if r.get("model_id")})
contexts = {(r["request_id"], r["policy_epoch"]): r["rtc"] for r in events
            if r["event"] == "powered_policy_request" and isinstance(r.get("rtc"), dict)}
report["phase_metrics_ms"] = {}
for phase in ("bootstrap", "guided", "guided_success"):
    selected = []
    for row in timings:
        context = contexts.get((row["request_id"], row["policy_epoch"]))
        if context is None:
            continue
        matches = context["bootstrap"] if phase == "bootstrap" else not context["bootstrap"]
        if matches and (phase != "guided_success" or row["outcome"] == "success"):
            selected.append(row)
    metrics = {key: stats([r[key] for r in selected if isinstance(r.get(key), (int, float))])
               for key in ("client_total_ms", "network_rtt_ms", "pack_ms", "observation_to_response_ms")}
    for key in sorted({k for r in selected for k in (r.get("server") or {})}):
        metrics["server_"+key] = stats([r["server"][key] for r in selected if key in (r.get("server") or {})])
    metrics["rtt_minus_worker_ms"] = stats([
        r["network_rtt_ms"]-r["server"]["worker_total"] for r in selected
        if r.get("network_rtt_ms") is not None and (r.get("server") or {}).get("worker_total") is not None])
    report["phase_metrics_ms"][phase] = dict(
        count=len(selected), outcomes=dict(Counter(r["outcome"] for r in selected)), metrics=metrics)
report["elapsed_control_seconds"] = (control[-1]["monotonic_ns"]-control[0]["monotonic_ns"])/1e9
a.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report), flush=True)
