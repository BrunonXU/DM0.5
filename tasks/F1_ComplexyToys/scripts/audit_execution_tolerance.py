#!/usr/bin/env python3
"""Offline execution alternatives on frozen 50K requests. Never controls a robot.

All observations/targets remain historical. Projection changes achieved EEF;
expanded bounds are unapproved sensitivity cases. Retrospective rollback anchors
are not collision-checked return paths and cannot restore scene/object state.
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
from f1_toys.native.kinematics import ContinuousIK, pose_error


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+"\n")


def margins(q):
    return dict(j2_lower_deg=float(q[1]+94),
                j6_deg=float(60-abs(q[5])),
                wrist_deg=float(110.5-1.025*abs(q[5])-abs(q[6])))


def source_reference(r):
    prefix = r["rtc_request"]["committed_actions16"]
    return np.asarray(prefix[-1][7:14] if prefix else r["prediction_replay"]["state16"][7:14])


def project(ik, target, reference_deg, failed, position_mm, rotation_deg, max_iterations=150):
    """Minimize worst normalized pose error under original joint/wrist bounds.

    Epigraph minimax avoids selecting a low weighted sum that hides one large
    pose component. Tiny reference regularizer breaks ties; no fixed direction.
    """
    model = ik.model
    reference = np.deg2rad(reference_deg)
    scales = np.array([position_mm/1000, np.deg2rad(rotation_deg)])
    cache = [None, None]

    def evaluate(q):
        if cache[0] is None or not np.array_equal(q, cache[0]):
            cache[:] = [q.copy(), model.residual(1, q, target)]
        e, jac = cache[1]
        errors = np.array([e[:3]@e[:3], e[3:]@e[3:]]) / scales**2
        gradient = np.vstack([2*e[:3]@jac[:3], 2*e[3:]@jac[3:]]) / scales[:, None]**2
        return errors, gradient

    def cost(y):
        delta = y[:7]-reference
        return y[7]+1e-6*(delta@delta), np.r_[2e-6*delta, 1.]

    seed = np.deg2rad(failed["candidate_joints"])
    initial = np.r_[seed, max(evaluate(seed)[0])+1e-9]
    start = time.perf_counter()
    result = minimize(cost, initial, jac=True, method="SLSQP",
        bounds=list(zip(model.low, model.high))+[(0., None)],
        constraints=[
            dict(type="ineq", fun=lambda y: y[7]-evaluate(y[:7])[0],
                 jac=lambda y: np.c_[-evaluate(y[:7])[1], np.ones(2)]),
            dict(type="ineq", fun=lambda y: model.wrist_max-model.wrist@y[:7],
                 jac=lambda y: np.c_[-model.wrist, np.zeros(4)])],
        options=dict(maxiter=max_iterations, ftol=1e-10))
    q = result.x[:7]
    p, a = pose_error(target, model.fk_rad(1, q))
    legal = model.admissible_rad(q)
    accepted = bool(legal and p <= position_mm+1e-8 and a <= rotation_deg+1e-8)
    return dict(success=accepted, projected=True,
                joints=np.rad2deg(q).tolist() if accepted else None,
                candidate_joints=np.rad2deg(q).tolist(),
                position_error_mm=p, rotation_error_deg=a, within_limits=legal,
                reference_step_max_deg=float(np.max(abs(np.rad2deg(q-reference)))),
                optimizer_success=bool(result.success), optimizer_status=int(result.status),
                iterations=int(result.nit), projection_latency_ms=(time.perf_counter()-start)*1000)


def replay(ik, r, budget=None, fast_projection=False, projection_iterations=150):
    prefix = r["rtc_request"]["committed_actions16"]
    reference = source_reference(r)
    diagnostics, joints = [], [x[7:14] for x in prefix]
    start = time.perf_counter()
    projection_active = False
    for k in range(len(prefix), r["rtc_request"]["coverage_steps"]):
        target = relative_pose(np.asarray(r["absolute_eef10"][k]))
        if projection_active and fast_projection:
            result = project(ik, target, reference,
                             dict(candidate_joints=reference.tolist()), *budget,
                             max_iterations=projection_iterations)
            result["direct_projection"] = True
        else:
            result = ik.solve(1, target, reference)
            result["projected"] = False
        if not result["success"] and budget:
            if not result.get("projected"):
                result = project(ik, target, reference, result, *budget,
                                 max_iterations=projection_iterations)
        projection_active = bool(result.get("projected") and
            (result["position_error_mm"] > ik.options.position_tolerance_mm or
             result["rotation_error_deg"] > ik.options.rotation_tolerance_deg))
        result["step_1_based"] = k+1
        diagnostics.append(result)
        if not result["success"]:
            break
        reference = np.asarray(result["joints"])
        joints.append(reference.tolist())
    return dict(request_id=r["request_id"], original_success=r["success"],
                success=bool(diagnostics[-1]["success"]), diagnostics=diagnostics,
                committed_count=len(prefix), coverage_steps=r["rtc_request"]["coverage_steps"],
                joints=joints, latency_ms=(time.perf_counter()-start)*1000)


def prefix_and_rollback(rows, model):
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[row["episode"]].append(row["server"])
    prefix_reports, anchors, retry_runs = [], [], []
    for episode, requests in grouped.items():
        requests.sort(key=lambda r: r["request_id"])
        streak = []
        for i, r in enumerate(requests):
            if r["success"]:
                if streak:
                    retry_runs.append(dict(episode=episode, rejected_ids=streak,
                        next_success_id=r["request_id"]))
                    streak = []
                continue
            streak.append(r["request_id"])
            context = r["rtc_request"]
            committed = len(context["committed_actions16"])
            suffix = len(r["diagnostics"])-1
            valid = committed+suffix
            d, s = context["delay_steps"], context["shift_steps"]
            transport = next(x for x in rows if x["episode"] == episode
                             and x["server"]["request_id"] == r["request_id"])["client_timing"]
            elapsed_ms = transport.get("timing", {}).get("client_total_ms")
            prefix_reports.append(dict(episode=episode, request_id=r["request_id"],
                committed_count=committed, valid_new_points=suffix, valid_total=valid,
                minimum_current_schedule_points=s+d,
                covers_current_schedule=bool(valid >= s+d),
                covers_current_schedule_plus_two_guard=bool(valid >= s+d+2),
                # Optimistic mathematical bound only: arbitrary earlier scheduling
                # would need a new protocol, real latency budget and fresh images.
                minimum_d_shift_d_points=2*d,
                covers_optimistic_shift_d=bool(valid >= 2*d),
                covers_hypothetical_d14_s16=bool(valid >= 30),
                recorded_client_total_ms=elapsed_ms))
            state = np.asarray(r["prediction_replay"]["state16"])
            q = state[7:14]
            past = [(j, old) for j, old in enumerate(requests[:i]) if old["success"]]
            candidates = []
            pending_grips = [float(x[15]) for x in context["committed_actions16"]]
            for chunks_back in (1, 2, 3):
                if len(past) < chunks_back:
                    continue
                j, old = past[-chunks_back]
                oq = np.asarray(old["prediction_replay"]["state16"][7:14])
                grips = [float(x["prediction_replay"]["state16"][15]) for x in requests[j:i+1]]
                ps, rot = pose_error(model.fk(1, q), model.fk(1, oq))
                candidates.append(dict(successful_requests_back=chunks_back,
                    anchor_request_id=old["request_id"],
                    anchor_age_s=(r["observation_monotonic_ns"]-old["observation_monotonic_ns"])/1e9,
                    anchor_measured_joints=oq.tolist(), anchor_margins=margins(oq),
                    max_joint_displacement_deg=float(np.max(abs(oq-q))),
                    eef_displacement_mm=ps, eef_rotation_deg=rot,
                    observed_gripper_span=max(grips)-min(grips),
                    gripper_endpoint_delta=float(old["prediction_replay"]["state16"][15]-state[15]),
                    observed_gripper_crossed_half=bool(min(grips)<50<=max(grips)),
                    # Heuristic screening, not hardware/task safety criteria.
                    has_j2_1deg_and_wrist_3deg_margin=bool(oq[1]+94>=1 and
                        110.5-1.025*abs(oq[5])-abs(oq[6])>=3)))
            anchors.append(dict(episode=episode, request_id=r["request_id"],
                measured_margins=margins(q), candidates=candidates,
                committed_gripper_crosses_half=bool(pending_grips and
                    min(pending_grips)<50<=max(pending_grips))))
        if streak:
            retry_runs.append(dict(episode=episode, rejected_ids=streak, next_success_id=None))
    return dict(prefix=prefix_reports, rollback=anchors, rejection_runs=retry_runs,
        summary=dict(rejected_requests=len(prefix_reports),
            valid_new_points_histogram=dict(collections.Counter(x["valid_new_points"] for x in prefix_reports)),
            covers_original_schedule=sum(x["covers_current_schedule"] for x in prefix_reports),
            covers_original_schedule_with_two_guard=sum(x["covers_current_schedule_plus_two_guard"] for x in prefix_reports),
            covers_hypothetical_d14_s16=sum(x["covers_hypothetical_d14_s16"] for x in prefix_reports),
            rollback_anchor_any_screened=sum(any(c["has_j2_1deg_and_wrist_3deg_margin"] and
                not c["observed_gripper_crossed_half"] for c in x["candidates"]) for x in anchors),
            rollback_any_gripper_crossing=sum(any(c["observed_gripper_crossed_half"]
                for c in x["candidates"]) for x in anchors),
            rollback_pending_gripper_transition=sum(x["committed_gripper_crosses_half"] for x in anchors),
            rejection_streaks=len(retry_runs),
            streaks_followed_by_recorded_success=sum(x["next_success_id"] is not None for x in retry_runs)),
        caveats=[
            "Anchor is measured state at a previous successful inference request, not proof that entire returned chunk executed.",
            "1/2/3 successful requests back is not a fixed time offset; retry intervals can make anchors stale.",
            "Gripper crossing is measured range screen, not contact/grasp classification. Stable gripper does not prove unchanged object.",
            "No dense measured reverse path, collision/contact check, or counterfactual re-prediction.",
            "Recorded success after a rejection may follow automatic retry, Enter, or operator intervention; no causal attribution."])


def whole_horizon(model, targets, reference_deg, seed_deg):
    """Local joint trajectory optimization, with exact original EEF equalities."""
    n = len(targets)
    reference = np.deg2rad(reference_deg)
    seed = np.deg2rad(seed_deg)
    wrist_matrix = np.kron(np.eye(n), model.wrist)
    cache = [None, None]

    def evaluate(x):
        if cache[0] is None or not np.array_equal(x, cache[0]):
            residuals, jacobian = np.zeros(n*6), np.zeros((n*6, n*7))
            for k, (q, target) in enumerate(zip(x.reshape(n, 7), targets)):
                residuals[k*6:k*6+6], jacobian[k*6:k*6+6, k*7:k*7+7] = model.residual(1, q, target)
            cache[:] = [x.copy(), (residuals, jacobian)]
        return cache[1]

    def cost(x):
        q = x.reshape(n, 7)
        delta = q-np.vstack([reference, q[:-1]])
        gradient = delta.copy()
        gradient[:-1] -= delta[1:]
        # Simultaneous margin objective acts on all future points.
        wrist_shortage = np.maximum(np.deg2rad(6)-(model.wrist_max-q@model.wrist.T), 0)
        j2_shortage = np.maximum(np.deg2rad(3)-(q[:, 1]-model.low[1]), 0)
        value = .5*np.sum(delta**2)+.5*np.sum(wrist_shortage**2)+.5*np.sum(j2_shortage**2)
        gradient += wrist_shortage@model.wrist
        gradient[:, 1] -= j2_shortage
        return value, gradient.ravel()

    # Check cost gradient independently; constraints reuse audited FK Jacobian.
    probe = np.clip(seed.ravel(), np.tile(model.low, n), np.tile(model.high, n))
    value, gradient = cost(probe)
    for k in np.linspace(0, len(probe)-1, min(12, len(probe))).astype(int):
        dx = np.zeros(len(probe))
        dx[k] = 1e-6
        assert abs((cost(probe+dx)[0]-cost(probe-dx)[0])/2e-6-gradient[k]) < 1e-6
    started = time.perf_counter()
    result = minimize(cost, probe, jac=True, method="SLSQP",
        bounds=list(zip(np.tile(model.low, n), np.tile(model.high, n))),
        constraints=[
            dict(type="eq", fun=lambda x: evaluate(x)[0], jac=lambda x: evaluate(x)[1]),
            dict(type="ineq", fun=lambda x: model.wrist_max-wrist_matrix@x,
                 jac=lambda x: -wrist_matrix)],
        options=dict(maxiter=120, ftol=1e-11))
    q = result.x.reshape(n, 7)
    errors = [pose_error(t, model.fk_rad(1, qi)) for t, qi in zip(targets, q)]
    success = all(model.admissible_rad(qi) and e[0] <= .01 and e[1] <= .001
                  for qi, e in zip(q, errors))
    return dict(success=bool(success), optimizer_success=bool(result.success),
                optimizer_status=int(result.status), message=str(result.message), iterations=int(result.nit),
                points=n, joints=np.rad2deg(q).tolist(), errors=errors,
                max_position_error_mm=max(x[0] for x in errors),
                max_rotation_error_deg=max(x[1] for x in errors),
                max_joint_step_deg=float(np.max(abs(np.rad2deg(q-np.vstack([reference,q[:-1]]))))),
                latency_ms=(time.perf_counter()-started)*1000)


def horizon_cases(rows, ik, controls_only=False):
    chosen = {}
    for row in rows:
        if not row["server"]["success"]:
            chosen.setdefault(row["episode"], row["server"])
    reports = []
    for episode, r in chosen.items():
        prefix = r["rtc_request"]["committed_actions16"]
        targets = [relative_pose(np.asarray(t)) for t in
                   r["absolute_eef10"][len(prefix):r["rtc_request"]["coverage_steps"]]]
        # Generate a complete warm start, including failed candidates, without
        # pretending those failed candidates were executable.
        q = source_reference(r)
        seed = []
        for target in targets:
            d = ik.solve(1, target, q)
            q = np.asarray(d["candidate_joints"])
            seed.append(q)
        previous = next(x["server"] for x in rows if x["episode"] == episode
                        and x["server"]["request_id"] == r["rtc_request"]["base_plan_id"])
        if controls_only:
            old = np.asarray(previous["actions"])[len(previous["rtc_request"]["committed_actions16"]):,7:14]
            known_targets = [relative_pose(np.asarray(t)) for t in previous["absolute_eef10"][
                len(previous["rtc_request"]["committed_actions16"]):previous["rtc_request"]["coverage_steps"]]]
            report = whole_horizon(ik.model, known_targets, source_reference(previous), old)
            report.update(episode=episode, request_id=previous["request_id"],
                case="known_success_control", current_committed_prefix_preserved=True,
                future_information_required=False)
            assert report["success"], report
            reports.append(report)
            continue
        for lookback in (False, True):
            if lookback:
                old_part = np.asarray(previous["actions"])[20:40, 7:14]
                np.testing.assert_allclose(old_part, np.asarray(prefix)[:, 7:14], atol=1e-8, rtol=0)
                full_targets = [ik.model.fk(1, q) for q in old_part]+targets
                full_seed = np.vstack([old_part, seed])
                anchor = source_reference(previous)
            else:
                full_targets, full_seed, anchor = targets, np.asarray(seed), source_reference(r)
            report = whole_horizon(ik.model, full_targets, anchor, full_seed)
            report.update(episode=episode, request_id=r["request_id"],
                case="one_chunk_earlier_ORACLE" if lookback else "fixed_committed_prefix",
                current_committed_prefix_preserved=not lookback,
                future_information_required=lookback)
            reports.append(report)
            print(json.dumps({k: report[k] for k in ("episode","case","success","latency_ms",
                                                    "max_position_error_mm","max_rotation_error_deg")}), flush=True)
    return reports


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--mode", choices=("projection", "projection-fast", "horizon", "horizon-controls", "history"), required=True)
    p.add_argument("--projection-iterations", type=int, default=150)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    config = Path(__file__).resolve().parents[1]/"src/f1_toys/native/ccs_m6_40.MvKDCfg"
    ik = ContinuousIK(FastArmModel(config))
    rows = [json.loads(x) for x in a.source.read_text().splitlines()]
    failures = [x for x in rows if not x["server"]["success"]]
    started = time.perf_counter()
    summary = []
    if a.mode == "history":
        report = prefix_and_rollback(rows, ik.model)
        dump(a.output/"results.json", report)
        summary = report["summary"]
    elif a.mode in ("horizon", "horizon-controls"):
        report = horizon_cases(rows, ik, controls_only=a.mode=="horizon-controls")
        dump(a.output/"results.json", report)
        summary = [{k: x[k] for k in ("episode","request_id","case","success",
                    "max_position_error_mm","max_rotation_error_deg","max_joint_step_deg","latency_ms")} for x in report]
    else:
        # Original full-window successes are negative controls; fallback-only
        # behavior must reproduce their returned joints, not merely still pass.
        controls = []
        grouped = collections.defaultdict(list)
        for row in rows:
            if row["server"]["success"]:
                grouped[row["episode"]].append(row)
        for group in grouped.values():
            controls.extend([group[0], group[len(group)//2], group[-1]])
        cases = (("baseline", None), ("p0p5_r0p1", (.5,.1)), ("p1_r0p5", (1,.5)), ("p2_r2", (2,2)))
        if a.mode == "projection-fast":
            cases = (("p1_r0p5", (1,.5)), ("p2_r2", (2,2)))
        for name, budget in cases:
            output = []
            for i, row in enumerate(failures+controls):
                result = replay(ik, row["server"], budget, fast_projection=a.mode=="projection-fast",
                                projection_iterations=a.projection_iterations)
                result["episode"] = row["episode"]
                if row["server"]["success"]:
                    assert result["success"] and not any(d["projected"] for d in result["diagnostics"])
                    np.testing.assert_allclose(result["joints"],
                        np.asarray(row["server"]["actions"])[:,7:14], atol=1e-7, rtol=0)
                output.append(result)
                if (i+1)%10 == 0:
                    print(json.dumps(dict(case=name, completed=i+1,total=len(failures+controls))),flush=True)
            passed = [x for x in output[:len(failures)] if x["success"]]
            diagnostics = [d for x in passed for d in x["diagnostics"]]
            report = dict(case=name, budget=budget, originally_failed=len(failures),
                full_windows_pass=len(passed), controls=len(controls), controls_unchanged=True,
                passed_ids=[[x["episode"],x["request_id"]] for x in passed],
                max_position_error_mm=max((x["position_error_mm"] for x in diagnostics), default=None),
                max_rotation_error_deg=max((x["rotation_error_deg"] for x in diagnostics), default=None),
                max_joint_step_deg=max((x["reference_step_max_deg"] for x in diagnostics), default=None),
                projected_points_in_accepted_windows=sum(x["projected"] for x in diagnostics),
                latency_p50_ms=float(np.median([x["latency_ms"] for x in output[:len(failures)]])),
                latency_p95_ms=float(np.percentile([x["latency_ms"] for x in output[:len(failures)]],95)))
            dump(a.output/(name+".json"),dict(summary=report,requests=output))
            summary.append(report)
            dump(a.output/"progress.json",summary)
            print(json.dumps(report),flush=True)
    dump(a.output/"summary.json",summary)
    dump(a.output/"manifest.json",dict(
        source=str(a.source), source_sha256=hashlib.sha256(a.source.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        kinematics_sha256=ik.model.config_sha256,
        mode=a.mode, robot_commands=0, model_calls=0, gpu_calls=0, deployment_changes=0,
        projection_iterations=a.projection_iterations,
        elapsed_s=time.perf_counter()-started,
        scope="Frozen historical D20/S20 K42 targets and observed references; NOT a counterfactual closed-loop rollout.",
        caveats=["Projection budgets are experimental screens, not established insertion tolerances.",
                 "Bounds and wrist coupling unchanged; no new collision/contact or velocity/acceleration certification.",
                 "Projection latency includes failed strict IK before fallback; CPU priority and load differ from production.",
                 "Whole-horizon SLSQP is a finite local candidate; failure is not a global infeasibility proof.",
                 "Oracle lookback changes a plan before historical commitment using future information unavailable online.",
                 "Rollback is retrospective anchor screening, not a validated return controller or model re-prediction."]))
    print(json.dumps(dict(mode=a.mode,summary=summary)),flush=True)


if __name__ == "__main__":
    main()
