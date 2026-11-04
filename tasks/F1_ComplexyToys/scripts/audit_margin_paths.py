#!/usr/bin/env python3
"""Offline fixed-target path counterfactuals under ORIGINAL hard IK limits.

No model, ROS, SDK, deployment or robot calls. Recorded target windows and
observations stay fixed, so success is not counterfactual task success.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from scipy.optimize import minimize

from f1_toys.eef import relative_pose
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.native.kinematics import ContinuousIK, checked_pose, pose_error


def margins(q):
    return [float(q[1]+94), float(60-abs(q[5])),
            float(110.5-1.025*abs(q[5])-abs(q[6]))]


class MarginIK:
    def __init__(self, base, wrist_deg, j2_deg, weight):
        self.base, self.model = base, base.model
        self.wrist_target, self.j2_target = np.deg2rad([wrist_deg, j2_deg])
        self.weight = weight

    def cost(self, q, reference):
        delta = q-reference
        wrist = np.maximum(self.wrist_target-(self.model.wrist_max-self.model.wrist@q), 0)
        j2 = max(self.j2_target-(q[1]-self.model.low[1]), 0)
        gradient = delta + self.weight*self.model.wrist.T@wrist
        gradient[1] -= self.weight*j2
        return (.5*np.dot(delta, delta)+.5*self.weight*(np.dot(wrist, wrist)+j2*j2), gradient)

    def solve(self, target, reference_deg):
        target = checked_pose(target)
        reference = np.deg2rad(reference_deg)
        cache = [None, None]
        def evaluate(q):
            if cache[0] is None or not np.array_equal(cache[0], q):
                cache[:] = [q.copy(), self.model.residual(1, q, target)]
            return cache[1]
        result = minimize(
            lambda q: self.cost(q, reference), np.clip(reference, self.model.low, self.model.high),
            jac=True, method="SLSQP", bounds=list(zip(self.model.low, self.model.high)),
            constraints=[
                dict(type="eq", fun=lambda q: evaluate(q)[0], jac=lambda q: evaluate(q)[1]),
                dict(type="ineq", fun=lambda q: self.model.wrist_max-self.model.wrist@q,
                     jac=lambda q: -self.model.wrist)],
            options=dict(maxiter=150, ftol=1e-11))
        q = result.x
        error = pose_error(target, self.model.fk_rad(1, q))
        success = (self.model.admissible_rad(q)
                   and error[0] <= self.base.options.position_tolerance_mm
                   and error[1] <= self.base.options.rotation_tolerance_deg)
        if not success:
            fallback = self.base.solve(1, target, reference_deg)
            fallback["margin_optimizer_fallback"] = True
            return fallback
        q = np.rad2deg(q)
        return dict(success=True, joints=q.tolist(), candidate_joints=q.tolist(),
                    position_error_mm=error[0], rotation_error_deg=error[1],
                    within_limits=True, margin_optimizer_fallback=False,
                    reference_step_max_deg=float(np.max(abs(q-reference_deg))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = Path(__file__).resolve().parents[1]/"src/f1_toys/native/ccs_m6_40.MvKDCfg"
    base = ContinuousIK(FastArmModel(config))
    grouped = collections.defaultdict(list)
    for line in args.source.read_text().splitlines():
        row = json.loads(line)
        grouped[row["episode"]].append(row["server"])
    episodes = {}
    for episode, rows in grouped.items():
        rows.sort(key=lambda r: r["request_id"])
        first_failure = next((n for n, r in enumerate(rows) if not r["success"]), None)
        if first_failure is not None:
            episodes[episode] = rows[:first_failure+1]
    cases = {"original": None, "wrist6_weight1": (6, 0, 1),
             "wrist6_weight10": (6, 0, 10), "j2_5_wrist6_weight10": (6, 5, 10)}
    # Independent finite-difference verification of the new penalty gradient.
    probe = MarginIK(base, 6, 5, 10)
    q = np.deg2rad([-60, -93, 55, -110, 20, 54, -54])
    value, gradient = probe.cost(q, q+.01)
    for i in range(7):
        delta = np.eye(7)[i]*1e-6
        difference = (probe.cost(q+delta, q+.01)[0]-probe.cost(q-delta, q+.01)[0])/2e-6
        assert abs(difference-gradient[i]) < 1e-7
    results = []
    started = time.monotonic()
    for episode, rows in episodes.items():
        for prior, row in zip(rows, rows[1:]):
            prefix = row["rtc_request"]["committed_actions16"]
            assert len(prefix) == 20 and row["rtc_request"]["shift_steps"] == 20
            np.testing.assert_allclose(np.asarray(prefix)[:, 7:14],
                                       np.asarray(prior["actions"])[20:40, 7:14], atol=1e-8, rtol=0)
        for case, settings in cases.items():
            candidate = None if settings is None else MarginIK(base, *settings)
            previous = None
            requests = []
            for row in rows:
                rtc = row["rtc_request"]
                committed = [] if previous is None else previous[20:40]
                assert len(committed) == len(rtc["committed_actions16"])
                reference = np.asarray(committed[-1] if committed else row["prediction_replay"]["state16"][7:14])
                output, diagnostics = list(committed), []
                for k in range(len(committed), rtc["coverage_steps"]):
                    target = relative_pose(np.asarray(row["absolute_eef10"][k]))
                    d = (base.solve(1, target, reference) if candidate is None
                         else candidate.solve(target, reference))
                    d["step_1_based"] = k+1
                    diagnostics.append(d)
                    if not d["success"]:
                        break
                    reference = np.asarray(d["joints"])
                    output.append(reference.tolist())
                success = diagnostics[-1]["success"]
                maximum_difference = None
                if row["success"] and success:
                    maximum_difference = float(np.max(abs(
                        np.asarray(output)-np.asarray(row["actions"])[:, 7:14])))
                requests.append(dict(
                    request_id=row["request_id"], original_success=row["success"],
                    success=success, committed_count=len(committed),
                    max_difference_recorded_joints_deg=maximum_difference,
                    min_margins_j2_j6_wrist_deg=np.min([margins(q) for q in output], axis=0).tolist()
                    if output else None, joints=output, diagnostics=diagnostics))
                if not success:
                    break
                previous = output
            failures = [r for r in requests if not r["success"]]
            summary = dict(
                episode=episode, case=case, settings=settings, requests=len(requests),
                passed=sum(r["success"] for r in requests),
                original_first_rejection=rows[-1]["request_id"],
                first_rejection=failures[0]["request_id"] if failures else None,
                original_first_rejected_window_now_passes=not failures,
                max_joint_step_deg=max(d["reference_step_max_deg"]
                                       for r in requests for d in r["diagnostics"] if d["success"]),
                max_difference_recorded_joints_deg=max(
                    (r["max_difference_recorded_joints_deg"] for r in requests
                     if r["max_difference_recorded_joints_deg"] is not None), default=0),
                min_margins_j2_j6_wrist_deg=np.min(
                    [r["min_margins_j2_j6_wrist_deg"] for r in requests if r["joints"]], axis=0).tolist())
            (args.output/(episode+"-"+case+".json")).write_text(
                json.dumps(dict(summary=summary, requests=requests))+"\n")
            results.append(summary)
            print(json.dumps(summary), flush=True)
            (args.output/"progress.json").write_text(json.dumps(results, indent=2)+"\n")
    manifest = dict(
        source=str(args.source), source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        robot_commands=0, model_calls=0, gpu_calls=0, deployment_changes=0,
        hard_bounds_changed=False, pose_tolerances_changed=False,
        gradient_check_passed=True, elapsed_s=time.monotonic()-started,
        scope="Fixed historical target/observation sequence until first historical IK rejection; reconstructed prefix carried exactly.",
        caveats=["No counterfactual model re-prediction or feedback.",
                 "Local margin penalties are candidate heuristics, not a full-horizon optimum.",
                 "No collision/dynamic certification. Alternative joints are not executable plans.",
                 "Failure of these finite candidates does not prove no continuous solution."])
    (args.output/"summary.json").write_text(json.dumps(results, indent=2)+"\n")
    (args.output/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")


if __name__ == "__main__":
    main()
