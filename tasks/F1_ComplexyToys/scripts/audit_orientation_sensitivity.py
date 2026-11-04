#!/usr/bin/env python3
"""Fixed-target orientation sensitivity, never a deployment correction."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from f1_toys.eef import relative_pose
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.native.kinematics import ContinuousIK


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    config = Path(__file__).resolve().parents[1]/"src/f1_toys/native/ccs_m6_40.MvKDCfg"
    ik = ContinuousIK(FastArmModel(config))
    rows = [r for r in map(json.loads, a.source.read_text().splitlines())
            if r["server"].get("reason") == "ik_rejected"]
    reports = []
    for degrees in (-2, -1, 0, .5, 1, 2):
        correction = Rotation.from_euler("y", degrees, degrees=True).as_matrix()
        results = []
        for row in rows:
            r = row["server"]
            prefix = r["rtc_request"]["committed_actions16"]
            reference = np.asarray(prefix[-1][7:14] if prefix else r["prediction_replay"]["state16"][7:14])
            diagnostics = []
            for k in range(len(prefix), r["rtc_request"]["coverage_steps"]):
                target = relative_pose(np.asarray(r["absolute_eef10"][k]))
                target[:3, :3] = target[:3, :3] @ correction
                d = ik.solve(1, target, reference)
                d["step_1_based"] = k+1
                diagnostics.append(d)
                if not d["success"]:
                    break
                reference = np.asarray(d["joints"])
            results.append(dict(
                episode=row["episode"], request_id=r["request_id"], success=diagnostics[-1]["success"],
                committed_count=len(prefix), coverage=r["rtc_request"]["coverage_steps"], diagnostics=diagnostics))
        report = dict(local_y_degrees=degrees, requests=len(results),
                      full_windows_pass=sum(r["success"] for r in results),
                      passed_request_ids=[[r["episode"], r["request_id"]] for r in results if r["success"]],
                      max_joint_step_accepted_windows_deg=max(
                          (d["reference_step_max_deg"] for r in results if r["success"]
                           for d in r["diagnostics"]), default=None))
        reports.append(report)
        (a.output/("rotation-"+str(degrees)+".json")).write_text(json.dumps(results)+"\n")
        print(json.dumps(report), flush=True)
    (a.output/"summary.json").write_text(json.dumps(reports, indent=2)+"\n")
    (a.output/"manifest.json").write_text(json.dumps(dict(
        source_sha256=hashlib.sha256(a.source.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        robot_commands=0, model_calls=0, deployment_changes=0,
        limits_changed=False, pose_tolerances_changed=False, targets_changed=True,
        caveats=[
            "The entire uncommitted suffix is rotated in each original target's local Y axis.",
            "Original committed joints stay fixed; the seam has no validated transition.",
            "Task/object orientation tolerance is unknown; mathematical acceptance is not insertion success.",
            "Static recorded-target sensitivity; no model re-prediction, counterfactual feedback or whole-episode execution.",
            "No velocity/acceleration/collision certification; do not deploy as a constant offset."
        ]), indent=2)+"\n")


if __name__ == "__main__":
    main()
