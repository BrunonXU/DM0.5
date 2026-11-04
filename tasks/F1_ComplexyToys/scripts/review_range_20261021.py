"""Independent offline range review: preserved EEF, seams and success controls.

No robot/model/service calls. Output must be an explicit separate directory.
Saved candidates are verified with the reference ArmModel FK; newly replayed
success controls use the same ContinuousIK solver with FastArmModel.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from f1_toys.eef import relative_pose
from f1_toys.native.kinematics import ArmModel, ContinuousIK, pose_error
from f1_toys.native.fast_kinematics import FastArmModel


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    task = Path(__file__).resolve().parents[1]
    source = task / "Eval/artifacts/20261020-50k-ik/joined-requests.jsonl"
    root = task / "Eval/artifacts/20261020-driver-range-study"
    config = task / "src/f1_toys/native/ccs_m6_40.MvKDCfg"
    sources = [source, config, Path(__file__)]
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    keyed = {(r["episode"], r["server"]["request_id"]): r["server"] for r in rows}
    model = ArmModel(config)
    start = time.monotonic()
    summaries = {}
    verification_points = 0
    for folder, name in [
        ("results-j2-refinement", "j2_minus95_only"),
        ("results", "j2_minus96_only"),
        ("results-j2-refinement", "j2_minus97_only"),
        ("results", "j2_document_minus120_only"),
        ("results", "wrist_plus2_only_UNAPPROVED"),
        ("results", "j2_minus96_wrist_plus2_UNAPPROVED"),
        ("results", "j6_plus2_only_UNAPPROVED"),
    ]:
        path = root / folder / (name + ".json")
        sources.append(path)
        data = json.loads(path.read_text())
        settings = data["settings"]
        low, high = np.rad2deg(model.low.copy()), np.rad2deg(model.high.copy())
        low[1] = settings.get("j2_lower", -94)
        high[5] = settings.get("j6_upper", 60)
        wrist_max = settings.get("wrist_max", 110.5)
        accepted = []
        for replay in data["full_replays"]:
            raw = keyed[(replay["episode"], replay["request_id"])]
            prefix = raw["rtc_request"]["committed_actions16"]
            assert len(prefix) == replay["committed_count"]
            assert raw["rtc_request"]["coverage_steps"] == replay["coverage_steps"]
            previous = np.asarray(prefix[-1][7:14] if prefix else
                                  raw["prediction_replay"]["state16"][7:14])
            qs, steps, errors = [], [], []
            for i, diag in enumerate(replay["diagnostics"]):
                k = len(prefix) + i
                assert k + 1 == diag["step_1_based"]
                if not diag["success"]:
                    assert i == len(replay["diagnostics"]) - 1
                    break
                q = np.array(diag["joints"])
                target = relative_pose(np.array(raw["absolute_eef10"][k]))
                pe, re = pose_error(target, model.fk(1, q))
                assert pe <= .01 and re <= .001
                assert np.all(q >= low - 1e-7) and np.all(q <= high + 1e-7)
                assert 1.025 * abs(q[5]) + abs(q[6]) <= wrist_max + 1e-7
                step = float(np.max(abs(q - previous)))
                assert abs(step - diag["reference_step_max_deg"]) < 1e-7
                qs.append(q.tolist()); steps.append(step); errors.append([pe, re])
                previous = q
                verification_points += 1
            if replay["success"]:
                assert len(qs) + len(prefix) == replay["coverage_steps"]
                q = np.array(qs)
                accepted.append(dict(episode=replay["episode"], request_id=replay["request_id"],
                    accepted_points=len(q), min_j2_deg=float(q[:, 1].min()),
                    max_j6_deg=float(q[:, 5].max()),
                    max_wrist_usage_deg=float((1.025*abs(q[:, 5])+abs(q[:, 6])).max()),
                    max_step_deg=max(steps), prefix_seam_step_deg=steps[0],
                    max_position_error_mm=float(np.max(errors, axis=0)[0]),
                    max_rotation_error_deg=float(np.max(errors, axis=0)[1])))
        assert len(accepted) == data["full_requests_accepts"]
        summaries[name] = dict(settings=settings, accepted_windows=len(accepted),
            min_j2_deg=min(x["min_j2_deg"] for x in accepted),
            max_j6_deg=max(x["max_j6_deg"] for x in accepted),
            max_wrist_usage_deg=max(x["max_wrist_usage_deg"] for x in accepted),
            max_step_deg=max(x["max_step_deg"] for x in accepted),
            max_prefix_seam_step_deg=max(x["prefix_seam_step_deg"] for x in accepted),
            max_position_error_mm=max(x["max_position_error_mm"] for x in accepted),
            max_rotation_error_deg=max(x["max_rotation_error_deg"] for x in accepted),
            accepted=accepted)
    # This comparison newly replays ALL 297 previously successful windows.
    # Expanded constraints can select another local minimum, even with exact EEF.
    expanded_model = FastArmModel(config)
    expanded_model.low[1] = np.deg2rad(-97.)
    expanded = ContinuousIK(expanded_model)
    controls = []
    for row in rows:
        raw = row["server"]
        if not raw.get("success"):
            continue
        prefix = raw["rtc_request"]["committed_actions16"]
        previous = np.array(prefix[-1][7:14] if prefix else
                            raw["prediction_replay"]["state16"][7:14])
        differences, steps, qs, errors = [], [], [], []
        diagnostics = []
        for k in range(len(prefix), raw["rtc_request"]["coverage_steps"]):
            target = relative_pose(np.array(raw["absolute_eef10"][k]))
            result = expanded.solve(1, target, previous)
            diagnostics.append(result)
            if not result["success"]:
                break
            q = np.array(result["joints"])
            original = np.array(raw["diagnostics"][k-len(prefix)]["joints"])
            pe, re = pose_error(target, model.fk(1, q))
            assert pe <= .01 and re <= .001
            differences.append(float(np.max(abs(q-original))))
            steps.append(float(np.max(abs(q-previous))))
            qs.append(q.tolist()); errors.append([pe, re]); previous = q
        controls.append(dict(episode=row["episode"], request_id=raw["request_id"],
            success=diagnostics[-1]["success"],
            max_joint_difference_from_original_deg=max(differences, default=0),
            max_step_deg=max(steps, default=0), prefix_seam_step_deg=steps[0] if steps else None,
            min_j2_deg=min((q[1] for q in qs), default=None), diagnostics=diagnostics))
        if len(controls) % 50 == 0:
            print(json.dumps(dict(success_controls_processed=len(controls))), flush=True)
    (args.output / "success-controls.json").write_text(json.dumps(controls, indent=2)+"\n")
    output = dict(date="2026-10-21", source_windows=len(rows), rejection_windows=49,
        saved_accepted_points_independently_verified=verification_points,
        candidates=summaries,
        j2_minus97_success_controls=dict(windows=len(controls),
            accepted=sum(x["success"] for x in controls),
            changed_more_than_1e_6_deg=sum(x["max_joint_difference_from_original_deg"]>1e-6 for x in controls),
            max_joint_difference_from_original_deg=max(x["max_joint_difference_from_original_deg"] for x in controls),
            max_step_deg=max(x["max_step_deg"] for x in controls),
            max_prefix_seam_step_deg=max(x["prefix_seam_step_deg"] for x in controls),
            min_j2_deg=min(x["min_j2_deg"] for x in controls)),
        limits=["Saved counterfactual requests keep their original recorded committed prefix; not a complete rollout carrying new candidate prefixes.",
            "Bounds of saved trajectories are demonstrated sufficient envelopes, not globally minimal necessary limits.",
            "EEF equality is evaluated using original strict .01 mm/.001 degree gate; no projection or tolerance relaxation.",
            "No collision, cable, velocity, acceleration, tracking or integrated hardware expansion approval.",
            "Joint step metrics are 30 Hz target differences, not measured physical velocity.",
            "No changed observations or model re-predictions; request success does not mean task success."],
        model_calls=0, robot_commands=0, deployment_changes=0, elapsed_s=time.monotonic()-start)
    (args.output / "summary.json").write_text(json.dumps(output, indent=2)+"\n")
    manifest = dict(sources={str(p):digest(p) for p in sources},
        math_sources={str(p):digest(p) for p in (task / "src/f1_toys/native").glob("*.py")},
        model_calls=0, robot_commands=0, deployment_changes=0)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps({k:v for k,v in output.items() if k != "candidates"}), flush=True)


if __name__ == "__main__":
    main()
