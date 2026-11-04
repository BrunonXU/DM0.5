#!/usr/bin/env python3
"""Offline multi-start audit of saved failed targets. No model/robot/network I/O.

Changing the reference here probes alternative IK branches only; it does not
certify continuous trajectories, velocities, collisions or hardware reachability.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from f1_toys.eef import solver, physical_actions, relative_pose
from f1_toys.native.kinematics import ContinuousIK, IKOptions, pose_error


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    raw = a.log.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    rows = [r for r in rows if r.get("shadow_only") is False and r.get("reason") == "ik_rejected"]
    assert rows, "No powered IK rejections"
    bundle_bytes = (a.bundle / "bundle.json").read_bytes()
    version = hashlib.sha256(bundle_bytes).hexdigest()
    assert all(r["model_version"] == version for r in rows)
    stats = json.loads((a.bundle / "norm_stats.json").read_text())
    (a.output / "inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    base = solver()
    extended = ContinuousIK(base.model, IKOptions(max_iterations=300, fallback_max_iterations=500))
    rng = np.random.default_rng(20261019)
    results = []
    for row in rows:
        replay = row["prediction_replay"]
        state = np.asarray(replay["state16"])
        actions = physical_actions(replay["normalized_actions32"], stats["action"])
        index = len(row["diagnostics"]) - 1
        target = base.model.fk(1, state[7:14]) @ relative_pose(actions[index])
        reference = np.asarray(row["diagnostics"][index-1]["joints"] if index else state[7:14])
        candidate = np.asarray(row["diagnostics"][-1]["candidate_joints"])
        errors = pose_error(target, base.model.fk(1, candidate))
        np.testing.assert_allclose(errors, [row["diagnostics"][-1]["position_error_mm"],
                                          row["diagnostics"][-1]["rotation_error_deg"]], atol=1e-8, rtol=1e-7)
        baseline = base.solve(1, target, reference)
        seeds = [("original_reference_extended", reference), ("failed_candidate_extended", candidate)]
        for scale in (5, 20):
            for i in range(4):
                q = np.clip(reference + rng.normal(0, scale, 7), np.rad2deg(base.model.low), np.rad2deg(base.model.high))
                # Seed feasibility only; never project solved/output joints.
                wrist_usage = 1.025*abs(q[5]) + abs(q[6])
                if wrist_usage > 110.5:
                    q[5:] *= 110.5 / wrist_usage
                seeds.append((f"local_{scale}deg_{i}", q))
        for i in range(4):
            q = rng.uniform(np.rad2deg(base.model.low), np.rad2deg(base.model.high))
            wrist_usage = 1.025*abs(q[5]) + abs(q[6])
            if wrist_usage > 110.5:
                q[5:] *= 110.5 / wrist_usage
            seeds.append((f"global_{i}", q))
        attempts = []
        for name, seed in seeds:
            result = extended.solve(1, target, seed)
            result.update(seed_name=name, seed_deg=seed.tolist(),
                          original_reference_max_delta_deg=float(np.max(np.abs(np.asarray(result["candidate_joints"])-reference))))
            attempts.append(result)
        accepted = [r for r in attempts if r["success"]]
        item = dict(session_id=row["session_id"], request_id=row["request_id"], failure_step_1_based=index+1,
                    target=target.tolist(), original_reference_deg=reference.tolist(),
                    saved_candidate_errors=list(errors), baseline=baseline, attempts=attempts,
                    alternatives_found=len(accepted),
                    nearest_accepted_max_delta_deg=min((r["original_reference_max_delta_deg"] for r in accepted), default=None))
        results.append(item)
        print(json.dumps({k:item[k] for k in ("session_id", "request_id", "failure_step_1_based", "alternatives_found", "nearest_accepted_max_delta_deg")}), flush=True)
        (a.output / "results.json").write_text(json.dumps(results, indent=2)+"\n")
    summary = dict(source=str(a.log), source_sha256=hashlib.sha256(raw).hexdigest(), model_version=version,
                   samples=len(results), baseline_rejected=sum(not r["baseline"]["success"] for r in results),
                   targets_with_alternative=sum(r["alternatives_found"] > 0 for r in results),
                   seed=20261019, attempts_per_target=14, same_joint_wrist_bounds_and_pose_tolerances=True,
                   robot_commands=0, limitations="Finite multi-start cannot prove infeasibility; alternate solutions do not certify a safe continuous prefix.")
    (a.output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
