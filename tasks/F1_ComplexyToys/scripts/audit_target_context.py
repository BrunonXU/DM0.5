#!/usr/bin/env python3
"""Offline geometry diagnostics, not a target correction or executable plan."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from f1_toys.eef import relative_pose
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.native.kinematics import ContinuousIK


def changed(pose, kind, axis, amount):
    pose = pose.copy()
    if kind == "translation_mm_native":
        pose[axis, 3] += amount/1000
    else:
        vector = np.zeros(3)
        vector[axis] = np.deg2rad(amount)
        pose[:3, :3] = pose[:3, :3] @ Rotation.from_rotvec(vector).as_matrix()
    return pose


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--replay", type=Path, required=True)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1]/"src/f1_toys"
    ik = ContinuousIK(FastArmModel(source/"native/ccs_m6_40.MvKDCfg"))
    teachers = {}
    hashes = {}
    for split in ("train", "validation"):
        root = args.dataset/split
        manifest = json.loads((root/"manifest.json").read_text())
        for name in ("arrays.npz", "samples.jsonl"):
            digest = hashlib.sha256((root/name).read_bytes()).hexdigest()
            assert digest == manifest["files_sha256"][name]
            hashes[split+"/"+name] = digest
        with np.load(root/"arrays.npz") as a:
            submitted = a["submitted"]
        rows = [json.loads(l) for l in (root/"samples.jsonl").read_text().splitlines()]
        for i, r in enumerate(rows):
            teachers.setdefault(r["episode_id"], dict(split=split, points={}))
            for k, seq in enumerate(r["action_control_sequences"]):
                teachers[r["episode_id"]]["points"][seq] = submitted[i, k, 7:14]
    for t in teachers.values():
        t["sequences"] = list(t.pop("points").items())
        t["q"] = np.asarray([q for _, q in t["sequences"]])
        t["poses"] = np.asarray([ik.model.fk(1, q) for q in t["q"]])
    chosen = {}
    for row in json.loads(args.inputs.read_text()):
        chosen.setdefault(row["episode"], row)
    replay = {(r["episode"], r["server"]["request_id"]): r["server"]
              for r in map(json.loads, args.replay.read_text().splitlines())}
    reports = []
    for episode, row in chosen.items():
        target, reference = np.asarray(row["target"]), np.asarray(row["reference"])
        nearest = []
        for teacher_episode, t in teachers.items():
            position = np.linalg.norm(t["poses"][:, :3, 3]-target[:3, 3], axis=1)*1000
            angle = np.rad2deg(Rotation.from_matrix(
                np.swapaxes(t["poses"][:, :3, :3], 1, 2) @ target[:3, :3]).magnitude())
            # Explicit diagnostic distance only, not a calibrated task metric.
            index = int(np.argmin((position/10)**2+(angle/5)**2))
            q = t["q"][index]
            result = ik.solve(1, target, q)
            nearest.append(dict(
                teacher_episode=teacher_episode, split=t["split"],
                control_sequence=t["sequences"][index][0],
                position_difference_mm=float(position[index]), rotation_difference_deg=float(angle[index]),
                teacher_reference=q.tolist(), result=result,
                max_delta_from_current_reference_deg=float(np.max(
                    abs(np.asarray(result["candidate_joints"])-reference)))))
        perturbations = []
        server = replay[(episode, row["request_id"])]
        prefix = server["rtc_request"]["committed_actions16"]
        for kind, amounts in (("translation_mm_native", (1, 2, 5)),
                              ("rotation_deg_local_tool", (.25, .5, 1, 2))):
            for axis in range(3):
                for absolute in amounts:
                    for amount in (-absolute, absolute):
                        altered = changed(target, kind, axis, amount)
                        result = ik.solve(1, altered, reference)
                        whole = None
                        if result["success"]:
                            q = np.asarray(prefix[-1][7:14] if prefix else
                                           server["prediction_replay"]["state16"][7:14])
                            diagnostics = []
                            for k in range(len(prefix), server["rtc_request"]["coverage_steps"]):
                                pose = changed(relative_pose(np.asarray(server["absolute_eef10"][k])),
                                               kind, axis, amount)
                                d = ik.solve(1, pose, q)
                                diagnostics.append(d)
                                if not d["success"]:
                                    break
                                q = np.asarray(d["joints"])
                            whole = dict(success=diagnostics[-1]["success"],
                                         solved=sum(d["success"] for d in diagnostics),
                                         max_joint_step_deg=max(d["reference_step_max_deg"] for d in diagnostics),
                                         diagnostics=diagnostics)
                        perturbations.append(dict(kind=kind, axis=axis, amount=amount,
                                                  first_point=result, full_window=whole))
        report = dict(episode=episode, request_id=row["request_id"], nearest_teachers=nearest,
                      perturbations=perturbations)
        (args.output/(episode+".json")).write_text(json.dumps(report)+"\n")
        summary = dict(
            episode=episode, request_id=row["request_id"],
            original_target_from_teacher_seeds_pass=sum(n["result"]["success"] for n in nearest),
            nearest_teacher_pose_by_metric=min(
                [{k: n[k] for k in ("teacher_episode", "split", "position_difference_mm",
                                    "rotation_difference_deg")} for n in nearest],
                key=lambda n:(n["position_difference_mm"]/10)**2+(n["rotation_difference_deg"]/5)**2),
            full_window_perturbations_pass=[
                {k: c[k] for k in ("kind", "axis", "amount")} for c in perturbations
                if c["full_window"] and c["full_window"]["success"]])
        reports.append(summary)
        print(json.dumps(summary), flush=True)
    (args.output/"summary.json").write_text(json.dumps(reports, indent=2)+"\n")
    (args.output/"manifest.json").write_text(json.dumps(dict(
        inputs_sha256=hashes, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        robot_commands=0, model_calls=0, deployment_changes=0,
        hard_limits_changed=False, pose_gate_changed=False,
        caveats=[
            "Perturbations CHANGE the task targets; they are diagnostics, not successful replay of original intent.",
            "Constant suffix offsets can introduce a prefix seam; no transition, collision or insertion-clearance certification.",
            "Native arm XYZ and local tool rotations are not calibrated object/hole axes.",
            "Teacher nearest-pose metric uses 10mm/5deg scales for diagnostics only.",
            "Validation demos are diagnostic witnesses only; not used for training or parameter fitting.",
            "Different scene geometry and original feedback remain unmodelled.",
            "Finite teacher seeds cannot prove global unreachability."
        ]), indent=2)+"\n")


if __name__ == "__main__":
    main()
