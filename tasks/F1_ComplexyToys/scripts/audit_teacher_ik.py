#!/usr/bin/env python3
"""CPU-only teacher EEF/IK roundtrip. No policy, ROS, SDK or robot connection.

Recorded observations stay exogenous: this is a decoder audit, not a simulated
closed-loop robot rollout. Failed scheduled windows restart only as explicitly
labelled diagnostic chains; no claim about runtime recovery timing is made.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from f1_toys.eef import encode_actions, relative_pose
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.native.kinematics import ContinuousIK, pose_error
from f1_toys.rtc import decode_window


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def margin(q):
    return [float(q[1]+94), float(60-abs(q[5])),
            float(110.5-1.025*abs(q[5])-abs(q[6]))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1] / "src/f1_toys"
    ik = ContinuousIK(FastArmModel(source / "native/ccs_m6_40.MvKDCfg"))
    manifest = dict(
        robot_commands=0, model_calls=0, gpu_calls=0, deployment_changes=0,
        source_sha256={str(p.relative_to(source)): sha(p) for p in
                       [source/"eef.py", source/"rtc.py", source/"native/kinematics.py",
                        source/"native/fast_kinematics.py"]},
        audit_script_sha256=sha(__file__), inputs={}, fps=30, horizon=50,
        scheduled_shift=20, committed_prefix=20, coverage=42,
        limitations=[
            "Only validated label intervals present in the frozen dataset; not Home or omitted intervals.",
            "Original measured observations are exogenous; no counterfactual feedback/closed-loop claim.",
            "Scheduled chains reset at gaps/rejections; this is not runtime retry/hold timing simulation.",
            "Independent windows cover a 20-step schedule plus each final anchor, not every training anchor.",
            "Continuous stream stops at first rejection per label segment.",
            "Numerical IK acceptance does not certify velocity, acceleration, collision or tracking.",
            "Known teacher joints are only a feasibility witness, never an online seed or robot plan."])
    all_results = []
    started = time.monotonic()
    for split in ("train", "validation"):
        folder = args.dataset / split
        data_manifest = json.loads((folder/"manifest.json").read_text())
        manifest["inputs"][split] = {}
        for name in ("arrays.npz", "samples.jsonl"):
            digest = sha(folder/name)
            assert digest == data_manifest["files_sha256"][name], (split, name)
            manifest["inputs"][split][name] = digest
        arrays = np.load(folder/"arrays.npz")
        states, submitted, actions = (arrays[k] for k in ("states", "submitted", "actions"))
        rows = [json.loads(line) for line in (folder/"samples.jsonl").read_text().splitlines()]
        assert len(rows) == len(states) == len(submitted) == len(actions)
        groups = collections.defaultdict(list)
        for index, row in enumerate(rows):
            groups[row["episode_id"]].append(index)
        for episode, indices in groups.items():
            origin = min(rows[i]["anchor_monotonic_ns"] for i in indices)
            anchors, grid = {}, {}
            overlap_checks = 0
            for i in indices:
                row = rows[i]
                g = round((row["anchor_monotonic_ns"]-origin)*30/1e9)
                assert g not in anchors
                anchors[g] = i
                for k, (ns, sequence) in enumerate(zip(
                        row["action_grid_ns"], row["action_control_sequences"])):
                    point = round((ns-origin)*30/1e9)
                    assert point == g+k and abs(ns-origin-point*1e9/30) < 3
                    if point in grid:
                        previous = grid[point]
                        assert sequence == previous["sequence"]
                        np.testing.assert_array_equal(
                            submitted[i, k], submitted[previous["index"], previous["offset"]])
                        overlap_checks += 1
                    # Most recent available observation supplies float32 EEF representation.
                    grid[point] = dict(index=i, offset=k, sequence=sequence, ns=ns)
            points = sorted(grid)
            segments = []
            for g in points:
                if not segments or g != segments[-1][-1]+1:
                    segments.append([])
                segments[-1].append(g)
            schedule, next_due = [], -1
            for g in sorted(anchors):
                if g >= next_due:
                    schedule.append(g)
                    next_due = g+20
            if schedule[-1] != max(anchors):
                schedule.append(max(anchors))
            report = dict(episode=episode, split=split, windows=len(indices),
                          unique_label_points=len(grid), label_segments=len(segments),
                          overlap_checks=overlap_checks, scheduled_windows=len(schedule))
            target_cache = {}

            def target(g):
                if g not in target_cache:
                    item = grid[g]
                    i, k = item["index"], item["offset"]
                    target_cache[g] = ik.model.fk(1, states[i, 7:14]) @ relative_pose(actions[i, k])
                return target_cache[g]

            # A known original joint solution witnesses representational feasibility.
            witness_errors, teacher_bounds, teacher_margins = [], [], []
            for g in points:
                item = grid[g]
                q = submitted[item["index"], item["offset"], 7:14].astype(float)
                teacher_bounds.append(ik.model.admissible_rad(np.deg2rad(q)))
                witness_errors.append(pose_error(target(g), ik.model.fk(1, q)))
                teacher_margins.append(margin(q))
            report["teacher_witness"] = dict(
                within_bounds=sum(teacher_bounds), points=len(points),
                max_pose_error_mm_deg=np.max(witness_errors, axis=0).tolist(),
                min_margins_j2_j6_wrist_deg=np.min(teacher_margins, axis=0).tolist(),
                within_original_pose_gate=sum(
                    p <= ik.options.position_tolerance_mm and r <= ik.options.rotation_tolerance_deg
                    for p, r in witness_errors))

            # Full chronological trajectory, with no per-window reseeding.
            stream_results = []
            for segment in segments:
                first = grid[segment[0]]
                reference = states[first["index"], 7:14].astype(float)
                trace, failure = [], None
                for g in segment:
                    item = grid[g]
                    q_teacher = submitted[item["index"], item["offset"], 7:14]
                    d = ik.solve(1, target(g), reference)
                    q = np.asarray(d["candidate_joints"])
                    trace.append(dict(grid=g, source_index=item["index"], source_offset=item["offset"],
                                      teacher=q_teacher.tolist(), reconstructed=q.tolist(),
                                      margin=margin(q), max_joint_step_deg=d["reference_step_max_deg"],
                                      delta_teacher_deg=float(np.max(abs(q-q_teacher))),
                                      success=d["success"]))
                    if not d["success"]:
                        failure = dict(grid=g, diagnostic=d, original_teacher=q_teacher.tolist(),
                                       witness_error_mm_deg=pose_error(target(g), ik.model.fk(1, q_teacher)))
                        break
                    reference = q
                stream_results.append(dict(
                    first_grid=segment[0], last_grid=segment[-1], total_points=len(segment),
                    accepted_points=sum(t["success"] for t in trace), failure=failure, trace=trace))
            report["continuous_stream"] = dict(
                segments=len(stream_results),
                passed_segments=sum(s["failure"] is None for s in stream_results),
                accepted_points=sum(s["accepted_points"] for s in stream_results),
                max_delta_teacher_deg=max(t["delta_teacher_deg"] for s in stream_results for t in s["trace"]))

            # Actual decode_window implementation. First independent windows,
            # then the same schedule carrying its reconstructed committed prefix.
            mode_results = {}
            for mode in ("independent42", "rtc_chained42"):
                previous, previous_g = None, None
                tests = []
                for g in schedule:
                    i = anchors[g]
                    physical = actions[i]
                    np.testing.assert_array_equal(physical, encode_actions(ik.model, states[i], submitted[i]))
                    committed = np.empty((0, 16))
                    if mode == "rtc_chained42" and previous is not None and g-previous_g == 20:
                        committed = np.asarray(previous["actions"][20:40])
                    d = decode_window(ik, physical, states[i], submitted[i, 0], committed, 42)
                    if d["success"] and len(committed):
                        np.testing.assert_array_equal(np.asarray(d["actions"][:20]), committed)
                    candidates = np.asarray([x["candidate_joints"] for x in d["diagnostics"]])
                    original = submitted[i, len(committed):len(committed)+len(candidates), 7:14]
                    tests.append(dict(
                        grid=g, source_index=i, observation_id=rows[i]["observation_id"],
                        committed_count=len(committed), success=d["success"],
                        failed_step=None if d["success"] else len(committed)+len(d["diagnostics"]),
                        max_delta_teacher_deg=float(np.max(abs(candidates-original))),
                        min_margins_j2_j6_wrist_deg=np.min([margin(q) for q in candidates], axis=0).tolist(),
                        reconstructed=candidates.tolist(), teacher=original.tolist(),
                        max_joint_step_deg=max(x["reference_step_max_deg"] for x in d["diagnostics"]),
                        failure=None if d["success"] else d["diagnostics"][-1]))
                    previous, previous_g = (d if d["success"] else None), g
                mode_results[mode] = tests
                report[mode] = dict(
                    windows=len(tests), passed=sum(t["success"] for t in tests),
                    guided=sum(t["committed_count"] > 0 for t in tests),
                    first_failure=next(({k: t[k] for k in ("grid", "source_index", "failed_step",
                                                          "committed_count", "failure")}
                                        for t in tests if not t["success"]), None),
                    max_delta_teacher_deg=max(t["max_delta_teacher_deg"] for t in tests))
            detail = dict(summary=report, continuous_stream=stream_results, **mode_results)
            (args.output/(episode+".json")).write_text(json.dumps(detail)+"\n")
            all_results.append(report)
            print(json.dumps(report), flush=True)
            (args.output/"progress.json").write_text(json.dumps(all_results, indent=2)+"\n")
    manifest["completed"] = True
    manifest["elapsed_s"] = time.monotonic()-started
    manifest["episodes"] = len(all_results)
    (args.output/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    (args.output/"summary.json").write_text(json.dumps(all_results, indent=2)+"\n")
    print(json.dumps(dict(completed=True, episodes=len(all_results), elapsed_s=manifest["elapsed_s"])), flush=True)


if __name__ == "__main__":
    main()
