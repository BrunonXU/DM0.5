#!/usr/bin/env python3
"""Read-only label audit: report whether proposed H50 windows cover release.

Only the requested audit JSON is created. Labels, masks, manifests and training
approval are never changed. This checks the existing proposal starts, not a
new selection of starts and not the final image/FK-aware model reader.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def records(path: Path) -> dict:
    return {row["episode"]: row for row in map(json.loads, path.read_text().splitlines())}


def load_arrays(path: Path) -> dict:
    with np.load(path, allow_pickle=False) as z:
        return {key: z[key] for key in z.files}


def covered_frames(starts: np.ndarray, n: int, horizon: int) -> np.ndarray:
    """Independent direct expansion of the actual stored chunk starts."""
    covered = np.zeros(n, dtype=bool)
    for start in starts:
        assert 0 <= start and start + horizon <= n, "stored H50 outside source"
        covered[start : start + horizon] = True
    return covered


def overlap_count(starts: np.ndarray, indices: np.ndarray, horizon: int) -> int:
    return sum(bool(np.any((indices >= start) & (indices < start + horizon))) for start in starts)


def count(mask: np.ndarray, indices: np.ndarray) -> dict:
    return {"covered_frames": int(mask[indices].sum()), "total_frames": int(len(indices))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    proposal_dir = args.proposal_dir.resolve()
    task = Path(__file__).resolve().parents[1]
    output = (args.output or proposal_dir / "release_coverage_audit.json").resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing audit: {output}")
    summary_path = proposal_dir / "summary.json"
    masks_path = task / "results/demo_772_selection/mask_manifest.jsonl"
    candidates_path = task / "results/demo_772_selection/candidates.jsonl"
    contract_path = task / "docs/action_interface_contract.md"
    summary, masks, candidates = read_json(summary_path), records(masks_path), records(candidates_path)
    horizon = 50
    inputs = {str(p): sha(p) for p in [summary_path, masks_path, candidates_path, contract_path]}
    assert inputs[str(masks_path)] == summary["mask_registry_sha256"]
    assert inputs[str(candidates_path)] == summary["selection_sha256"]
    results = []
    for row in summary["results"]:
        if row["status"] != "joint_timing_proposal":
            continue
        episode = row["episode"]
        source_path = Path(masks[episode]["aligned_npz_path"])
        proposal_path = proposal_dir / episode.replace("/", "_") / "proposal.npz"
        report_path = proposal_path.with_name("report.json")
        for p in [source_path, proposal_path, report_path]:
            inputs[str(p)] = sha(p)
        assert inputs[str(source_path)] == masks[episode]["aligned_npz_sha256"]
        assert inputs[str(proposal_path)] == row["npz_sha256"]
        for name, expected in row["inputs"].items():
            actual = sha(Path(name))
            assert actual == expected, f"changed generating evidence: {name}"
            inputs[name] = actual
        parent, proposal = load_arrays(source_path), load_arrays(proposal_path)
        assert np.array_equal(parent["time_ns"], proposal["time_ns"])
        n = len(parent["time_ns"])
        assert parent["action"].shape == (n, 19)
        assert proposal["base_velocity_proposal"].shape == (n, 3)
        assert row["parent_split"] == masks[episode]["parent_split"] == candidates[episode]["parent_split"]
        t = (parent["time_ns"] - candidates[episode]["source_start_ns"]) / 1e9
        stages = {m["role"]: m for m in row["motion_stages"]}
        gap_start = stages["forward_out"]["end_s"]
        gap_end = stages["back_return"]["start_s"]
        assert gap_start < gap_end
        grip = parent["action"][:, 14]
        delta = np.diff(grip, prepend=grip[0])
        # Contract: 0=open, 100=closed. Require >1 point movement to avoid
        # tiny quantization noise; report both opening and subsequent closing.
        changes = np.flatnonzero((t > gap_start) & (t < gap_end) & (np.abs(delta) > 1.0))
        openings = changes[delta[changes] < 0]
        groups = []
        for i in changes:
            if not groups or i - groups[-1][-1] > 4 or np.sign(delta[i]) != np.sign(delta[groups[-1][-1]]):
                groups.append([int(i)])
            else:
                groups[-1].append(int(i))
        starts = np.flatnonzero(proposal["proposed_chunk_start"])
        parent_starts = np.flatnonzero(parent["chunk_start_candidate_valid"])
        covered = covered_frames(starts, n, horizon)
        parent_covered = covered_frames(parent_starts, n, horizon)
        parent_state = parent["state_valid"].all(axis=1)
        parent_action = parent["action_candidate_valid"].all(axis=1)
        finite = np.isfinite(proposal["base_velocity_proposal"]).all(axis=1)
        proposal_valid = proposal["proposed_joint_valid"]
        for start in starts:
            assert proposal_valid[start : start + horizon].all()
            assert finite[start : start + horizon].all()
            assert parent_action[start : start + horizon].all()
        events = []
        for group in groups:
            indices = np.asarray(group)
            first, last = int(indices[0]), int(indices[-1])
            span = np.arange(first, last + 1)
            observed_base = proposal["base_velocity_proposal"][span][finite[span]]
            events.append({
                "kind": "opening_release_candidate" if delta[first] < 0 else "closing_after_release_candidate",
                "start_s": float(t[first]), "end_s": float(t[last]),
                "changed_indices": indices.tolist(), "changed_times_s": t[indices].tolist(),
                "command_values_before_change": grip[indices - 1].tolist(),
                "command_values_after_change": grip[indices].tolist(),
                "delta_points": delta[indices].tolist(),
                "command_before_event": float(grip[first - 1]), "command_after_event": float(grip[last]),
                "feedback_left_gripper_range_over_event": [float(parent["state"][span, 14].min()), float(parent["state"][span, 14].max())],
                "parent_state_all21_valid": count(parent_state, indices),
                "parent_action_all19_valid": count(parent_action, indices),
                "parent_joint_frame_valid": count(parent["joint_frame_candidate_valid"], indices),
                "parent_h50_covered": count(parent_covered, indices),
                "proposal_finite_base": count(finite, indices),
                "proposal_joint_valid": count(proposal_valid, indices),
                "proposal_h50_covered": count(covered, indices),
                "proposal_h50_covered_full_event_span": count(covered, span),
                "parent_h50_overlapping_event": overlap_count(parent_starts, indices, horizon),
                "proposal_h50_overlapping_event": overlap_count(starts, indices, horizon),
                "max_abs_proposed_base_on_finite_event_span": np.max(np.abs(observed_base), axis=0).tolist() if len(observed_base) else None,
            })
        results.append({
            "episode": episode, "parent_split": row["parent_split"],
            "source_path": str(source_path), "proposal_path": str(proposal_path),
            "source_frames": n, "proposed_h50_starts": int(len(starts)),
            "placement_gap_s": [gap_start, gap_end], "events": events,
            "all_gripper_changed_frames": int(len(changes)),
            "all_gripper_changed_frames_parent_valid": int(parent_action[changes].sum()),
            "all_gripper_changed_frames_proposal_valid": int(proposal_valid[changes].sum()),
            "all_gripper_changed_frames_h50_covered": int(covered[changes].sum()),
            "h50_overlapping_all_gripper_changes": overlap_count(starts, changes, horizon),
            "opening_changed_frames": int(len(openings)),
            "opening_changed_frames_h50_covered": int(covered[openings].sum()),
            "h50_overlapping_opening": overlap_count(starts, openings, horizon),
            "release_candidate_present": bool(len(openings)),
            "release_candidate_absent_from_proposed_h50": bool(len(openings) and not covered[openings].any()),
        })
    splits = {}
    for split in sorted({r["parent_split"] for r in results}):
        rows = [r for r in results if r["parent_split"] == split]
        splits[split] = {
            "sources": len(rows),
            "sources_with_opening_event": sum(r["release_candidate_present"] for r in rows),
            "sources_with_opening_covered_by_h50": sum(r["h50_overlapping_opening"] > 0 for r in rows),
            "opening_changed_frames": sum(r["opening_changed_frames"] for r in rows),
            "opening_changed_frames_h50_covered": sum(r["opening_changed_frames_h50_covered"] for r in rows),
            "h50_overlapping_opening": sum(r["h50_overlapping_opening"] for r in rows),
        }
    report = {
        "schema": "f1_proposal_release_coverage_audit_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "script_sha256": sha(Path(__file__)), "input_sha256": inputs,
        "scope": {"parent_candidates": summary["scope"], "materialized_proposals": len(results),
                  "does_not_cover": "Other proposal versions, final model-reader release and physical task success."},
        "method": {
            "left_gripper_action_index_zero_based": 14,
            "scale_and_direction": "Contract: 0 fully open; 100 closed. Negative command delta is opening; positive is closing.",
            "phase_localization": "Strictly between proposed forward_out.end_s and back_return.start_s; a temporal release candidate, not proof of successful placement.",
            "event_rule": "abs(per-frame command delta)>1.0 point; same-sign change frames no more than 4 source indices apart are one event; all changed indices reported.",
            "horizon": horizon,
            "coverage_rule": "Expand each existing true proposed_chunk_start into [start,start+50); no new starts; report changed-frame and full-event-span coverage.",
            "source_of_truth": "Independent read of parent action/masks and stored proposal starts; no generator function called.",
        },
        "by_split": splits, "results": results,
        "finding": "Counts refer only to this proposal set; missing opening coverage is not repaired by validation coverage. See by_split and per-source events.",
        "limitations": [
            "A command to open is not visual confirmation that foam was released successfully.",
            "Phase gap comes from candidate boundaries; an incorrect boundary can mislocalize an event. Events outside that gap are not audited here.",
            "The >1 point threshold can miss very slow opening; direction-split grouping is diagnostic and all chosen frames are disclosed.",
            "No complete eight-box/task-coverage census is implied by four materialized proposals.",
            "These are proposal H50 starts. Final image/FK/reader filters can remove more windows, so proposal coverage is an upper bound.",
            "No nonzero finite proposed base velocity at an event is only absence of this particular contradiction, not proof that unknown event frames are stationary.",
        ],
        "labels_changed": False, "training_approved": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output), "scope": report["scope"], "by_split": splits}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
