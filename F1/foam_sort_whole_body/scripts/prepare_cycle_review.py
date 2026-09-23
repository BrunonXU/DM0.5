#!/usr/bin/env python3
"""Prepare source-bound cycle annotations and export reviewed logical slices.

This consumes immutable cleaning_v1 candidates. It never rewrites MCAP, v1
arrays, source splits, or base labels. A successful validation is not training
or robot approval. See results/cleaning_v2_review/annotation_guide.md.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import yaml

TASK = Path(__file__).resolve().parents[1]
SCHEMA = "f1_cycle_review_v2.1"
GATES = ("boundary", "task", "human", "base", "interface", "time")
VISUAL_GATES = {"boundary", "task", "human", "base"}
EVIDENCE_KINDS = {"continuous_three_camera_review", "interface_mapping_review",
                  "clock_alignment_review", "sparse_visual_hint"}
STREAMS = ("hal", "lead", "left_cmd", "right_cmd", "left_state", "right_state", "base")


def require(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def object_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def write_jsonl(path, rows):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def interval(value, duration_ns, where):
    require(isinstance(value, list) and len(value) == 2 and
            all(type(x) is int for x in value), f"{where}: interval must be two integer nanoseconds")
    require(0 <= value[0] < value[1] <= duration_ns, f"{where}: interval outside source bounds")
    return tuple(value)


def overlapping(left, right):
    return max(left[0], right[0]) < min(left[1], right[1])


def nonoverlap(values, where):
    ordered = sorted(values)
    require(all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])),
            f"{where}: overlapping half-open intervals")


def covers(spans, target):
    cursor = target[0]
    for a, b in sorted(spans):
        if a > cursor:
            break
        cursor = max(cursor, b)
    return cursor >= target[1]


def subtract_intervals(source, exclusions):
    cursor, stop = source
    result = []
    for a, b in sorted(exclusions):
        if b <= cursor or a >= stop:
            continue
        if a > cursor:
            result.append((cursor, min(a, stop)))
        cursor = max(cursor, b)
    if cursor < stop:
        result.append((cursor, stop))
    return result


def template_cycle(number, span, base_status="unreviewed_weak"):
    return {"cycle_id": f"cycle_{number:03d}", "interval_ns": list(span),
            "status": "proposed", "task_outcome": "unknown", "human_intervention": "unknown",
            "base_label_status": base_status,
            "reviews": {key: {"status": "pending", "evidence_refs": []} for key in GATES},
            "rejection_reason": "", "notes": ""}


def source_identity(meta):
    return {"source_relative_path": meta["source_relative_path"],
            "source_fingerprint": {"size_bytes": meta["source_size_bytes"],
                                   "mtime_ns": meta["source_mtime_ns"]},
            "source_start_ns": meta["start_ns"],
            "source_stop_ns_exclusive": meta["end_ns"] + 1,
            "source_duration_ns": meta["end_ns"] + 1 - meta["start_ns"],
            "parent_split": meta["split"]}


def make_catalog_row(meta, v1, findings, j6, j4):
    episode = meta["episode"]
    reasons = set(meta["review_reasons"])
    if episode in j6:
        reasons.add("lead_left_j6_above_document_limit_requires_mapping_evidence")
    if episode in j4:
        reasons.add("lead_left_j4_small_document_boundary_difference_not_auto_rejection")
    hint = findings.get(episode, {})
    if hint.get("human_near_gripper_evidence"):
        reasons.add("possible_human_reach_near_right_gripper_not_confirmed_contact")
    if meta["confirmed_bad_base_masked"]:
        priority = 0
    elif episode in j6 or hint.get("human_near_gripper_evidence"):
        priority = 1
    elif meta["multiple_event_review_candidate"]:
        priority = 2
    elif reasons & {"estimated_speed_over_2mps_review_not_hardware_limit", "fallback_movement_window",
                    "feedback_gap_over_1s", "late_first_camera_message_clock_outlier"}:
        priority = 3
    else:
        priority = 4
    return {"episode": episode, **source_identity(meta), "review_priority": priority,
            "review_reasons": sorted(reasons), "confirmed_bad_base_masked": meta["confirmed_bad_base_masked"],
            "aligned_npz_sha256": digest((v1 / "aligned" / episode).with_suffix(".npz")),
            "aligned_metadata_sha256": digest((v1 / "aligned" / episode).with_suffix(".json")),
            "frames": meta["frames"], "v1_chunk_candidates": meta["chunk_candidate_starts"],
            "state_dim": 21, "action_dim": 19, "grid_hz": 30, "chunk_horizon": 50,
            "observed_cycle_count_hint": hint.get("visually_observed_transport_cycles"),
            "rough_intervals_s_hint": hint.get("rough_visual_cycle_intervals_s", []),
            "sparse_review_summary_hint": hint.get("summary"),
            "hint_evidence": "../cleaning_v1/cycle_review/findings.json" if hint else None,
            "hint_is_final_boundary_or_outcome_evidence": False}


def make_annotation(row, hint):
    duration = row["source_duration_ns"]
    cycles = []
    for number, span in enumerate(row["rough_intervals_s_hint"], 1):
        # The 162.98s rough hint is rounded beyond a source's actual end. Keep
        # that original hint in the immutable catalog; clamp only this proposal.
        a, b = [min(duration, max(0, round(x * 1e9))) for x in span]
        if a < b:
            cycles.append(template_cycle(number, (a, b),
                          "known_bad" if row["confirmed_bad_base_masked"] else "unreviewed_weak"))
    exclusions = []
    if hint.get("rough_wait_and_replenishment_interval_s"):
        a, b = hint["rough_wait_and_replenishment_interval_s"]
        exclusions.append({"exclusion_id": "replenishment_proposal", "interval_ns": [round(a * 1e9), round(b * 1e9)],
                           "status": "proposed", "kind": "human_replenishment",
                           "evidence_refs": [], "notes": "粗略区间含等待；精查后只排除实际介入/复位，不自动删除正常等待。"})
    return {"schema": SCHEMA, "episode": row["episode"],
            "catalog_entry_sha256": object_digest(row), "parent_split": row["parent_split"],
            "recording_review": {"status": "pending", "cycle_count": None, "evidence_refs": []},
            "evidence": [], "cycles": cycles, "exclusions": exclusions, "notes": ""}


def prepare(v1, root):
    require(not (root / "source_catalog.jsonl").exists(), "Review already prepared; annotations are never overwritten")
    require(v1.resolve() != root.resolve() and v1.resolve() not in root.resolve().parents,
            "Review output must be separate from cleaning_v1")
    validation = read(v1 / "validation.json")
    require(validation["success"] and not validation["errors"], "v1 validation did not pass")
    metas = read_jsonl(v1 / "episode_manifest.jsonl")
    assignments = read(v1 / "source_split.json")["assignments"]
    require(len(metas) == len(assignments) == validation["validated_episodes"], "Incomplete v1 source set")
    findings_path = v1 / "cycle_review/findings.json"
    findings = {x["episode"]: x for x in read(findings_path)["records"]}
    range_path = TASK / "results/interface_review_20260914/documented_range_audit.json"
    fields = {x["field"]: x for x in read(range_path)["streams"]["lead_q"]["fields"]}
    j6, j4 = [set(fields[x]["episodes_out_of_document_range"]) for x in ("arm_L_joint6", "arm_L_joint4")]
    paths = {key: v1 / key for key in ("episode_manifest.jsonl", "source_split.json", "summary.json",
              "validation.json", "config_snapshot.yaml", "review_overrides_snapshot.yaml", "review_queue.csv")}
    paths.update({"cycle_findings": findings_path, "documented_range_audit": range_path})
    pinned = {name: {"path": str(path.resolve()), "sha256": digest(path)} for name, path in paths.items()}
    rows = []
    for meta in metas:
        require(meta["split"] == assignments[meta["episode"]], "v1 split mismatch")
        require((meta["state_dim"], meta["action_dim"], meta["grid_hz"], meta["chunk_horizon"]) == (21, 19, 30, 50),
                "Unsupported v1 contract")
        rows.append(make_catalog_row(meta, v1, findings, j6, j4))
    rows.sort(key=lambda x: (x["review_priority"], x["episode"]))
    root.mkdir(parents=True, exist_ok=True)
    write_jsonl(root / "source_catalog.jsonl", rows)
    for row in rows:
        path = root / "annotations" / (row["episode"] + ".json")
        require(not path.exists(), f"Existing annotation would be overwritten: {path}")
        write(path, make_annotation(row, findings.get(row["episode"], {})))
    write(root / "baseline.json", {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
          "v1_root": str(v1.resolve()), "inputs": pinned,
          "source_catalog_sha256": digest(root / "source_catalog.jsonl"),
          "source_count": len(rows), "state_dim": 21, "action_dim": 19, "grid_hz": 30, "chunk_horizon": 50,
          "known_bad_base_sources": validation["known_bad_base_episodes"],
          "raw_files_modified": 0, "training_approved": False})


def validate_evidence(annotation, row, root):
    evidence = {}
    for item in annotation["evidence"]:
        identifier = item["evidence_id"]
        require(isinstance(identifier, str) and identifier and identifier not in evidence, "Duplicate/empty evidence id")
        require(item["kind"] in EVIDENCE_KINDS, "Unknown evidence kind")
        require(isinstance(item["reviewer"], str) and item["reviewer"].strip(), "Evidence reviewer missing")
        stamp = datetime.fromisoformat(item["reviewed_at"])
        require(stamp.tzinfo is not None, "Evidence review timestamp requires timezone")
        path = Path(item["path"])
        path = path if path.is_absolute() else root / path
        require(path.is_file() and digest(path) == item["sha256"], "Evidence missing or checksum changed")
        if item["kind"] in {"continuous_three_camera_review", "sparse_visual_hint"}:
            interval(item["interval_ns"], row["source_duration_ns"], "evidence")
            require(item["episode"] == row["episode"], "Visual evidence belongs to another recording")
        if item["kind"] == "continuous_three_camera_review":
            require(set(item["cameras"]) == {"head", "left", "right"}, "Continuous review must cover three cameras")
        evidence[identifier] = item
    return evidence


def visual_evidence_covers(refs, evidence, span):
    return covers([evidence[x]["interval_ns"] for x in refs
                   if evidence[x]["kind"] == "continuous_three_camera_review"], span)


def check_refs(refs, evidence):
    require(isinstance(refs, list) and all(isinstance(x, str) and x in evidence for x in refs),
            "Unresolved review evidence reference")


def reviewed_cycle(cycle, row, exclusions, evidence):
    """A pending annotation can be stored, but cannot become a logical slice."""
    require(cycle["status"] == "reviewed", "Pending/rejected cycle cannot be exported")
    require(cycle["task_outcome"] == "success", "Only reviewed successful complete cycles enter this baseline")
    require(cycle["human_intervention"] in {"none", "excluded_intervals"}, "Human intervention is unknown")
    require(not row["confirmed_bad_base_masked"], "Known bad base cannot be restored by an annotation")
    require(cycle["base_label_status"] == "reviewed_original_weak", "Base label is not reviewed original weak supervision")
    span = cycle["interval_ns"]
    intersecting = [x for x in exclusions if overlapping(x["interval_ns"], span)]
    require(all(x["status"] == "reviewed" for x in intersecting), "Unresolved exclusion intersects reviewed cycle")
    if cycle["human_intervention"] == "none":
        require(not intersecting, "Cycle marked no intervention intersects exclusion")
    if cycle["human_intervention"] == "excluded_intervals":
        require(bool(intersecting), "Intervention marked excluded but no exclusion interval exists")
    for key in GATES:
        gate = cycle["reviews"][key]
        refs = gate["evidence_refs"]
        check_refs(refs, evidence)
        require(gate["status"] == "verified" and refs, f"{key}: review not verified with evidence")
        if key in VISUAL_GATES:
            require(visual_evidence_covers(refs, evidence, span), f"{key}: requires continuous three-camera coverage")
        else:
            required = "interface_mapping_review" if key == "interface" else "clock_alignment_review"
            require(any(evidence[x]["kind"] == required for x in refs), f"{key}: wrong evidence kind")


def validate_annotation(annotation, row, root):
    require(annotation["schema"] == SCHEMA and annotation["episode"] == row["episode"], "Annotation identity/schema mismatch")
    require(annotation["catalog_entry_sha256"] == object_digest(row), "Annotation source fingerprint changed")
    require(annotation["parent_split"] == row["parent_split"], "Child split must inherit frozen original recording split")
    evidence = validate_evidence(annotation, row, root)
    duration = row["source_duration_ns"]
    exclusion_spans, ids = [], set()
    for exclusion in annotation["exclusions"]:
        require(exclusion["exclusion_id"] not in ids, "Duplicate exclusion id")
        ids.add(exclusion["exclusion_id"])
        span = interval(exclusion["interval_ns"], duration, "exclusion")
        exclusion_spans.append(span)
        require(exclusion["status"] in {"proposed", "reviewed"}, "Unknown exclusion status")
        require(exclusion["kind"] in {"human_replenishment", "human_reset", "human_contact", "invalid_task_segment"},
                "Unknown exclusion kind")
        check_refs(exclusion["evidence_refs"], evidence)
        if exclusion["status"] == "reviewed":
            require(visual_evidence_covers(exclusion["evidence_refs"], evidence, span), "Exclusion lacks continuous review")
    nonoverlap(exclusion_spans, "exclusions")
    spans, ids, accepted = [], set(), []
    for cycle in annotation["cycles"]:
        identifier = cycle["cycle_id"]
        require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_-]+", identifier) and identifier not in ids,
                "Cycle id must be unique safe text")
        ids.add(identifier)
        spans.append(interval(cycle["interval_ns"], duration, "cycle"))
        require(cycle["status"] in {"proposed", "reviewed", "rejected"}, "Unknown cycle status")
        require(cycle["task_outcome"] in {"unknown", "success", "failure", "incomplete"}, "Unknown task outcome")
        require(cycle["human_intervention"] in {"unknown", "none", "excluded_intervals"}, "Unknown human intervention status")
        require(cycle["base_label_status"] in {"unreviewed_weak", "known_bad", "reviewed_original_weak"}, "Unsupported base label status")
        require(not row["confirmed_bad_base_masked"] or cycle["base_label_status"] == "known_bad",
                "Known bad base status cannot be cleared by annotation; a separate relabel artifact is required")
        require(set(cycle["reviews"]) == set(GATES), "Missing/extra review gates")
        for gate in cycle["reviews"].values():
            require(gate["status"] in {"pending", "verified"}, "Unknown gate status")
            check_refs(gate["evidence_refs"], evidence)
            require(gate["status"] != "verified" or gate["evidence_refs"], "Verified gate needs evidence")
        if cycle["status"] == "rejected":
            require(bool(cycle["rejection_reason"].strip()), "Rejected cycle requires a reason")
        if cycle["status"] == "reviewed":
            reviewed_cycle(cycle, row, annotation["exclusions"], evidence)
            accepted.append(cycle)
    nonoverlap(spans, "cycles")
    recording = annotation["recording_review"]
    require(recording["status"] in {"pending", "reviewed"}, "Unknown recording review status")
    check_refs(recording["evidence_refs"], evidence)
    if recording["status"] == "reviewed":
        require(type(recording["cycle_count"]) is int and recording["cycle_count"] == len(annotation["cycles"]),
                "Reviewed recording count must match enumerated cycles")
        require(visual_evidence_covers(recording["evidence_refs"], evidence, (0, duration)), "Full recording coverage is missing")
        require(all(x["status"] != "proposed" for x in annotation["cycles"]), "Recording still has proposed cycles")
    else:
        require(recording["cycle_count"] is None, "Pending recording cycle count must remain unknown")
    return accepted


def cycle_slices(cycle, row, annotation, arrays, earliest_source_ns):
    """Reuse causal v1 samples; never carry samples across a cut or intervention.

    Each exclusion splits continuous time even if it falls between grid frames.
    Last-source timestamps also prevent a post-cut observation from carrying a
    camera or signal sample originating before the new segment.
    """
    require(cycle["status"] == "reviewed" and not row["confirmed_bad_base_masked"], "Only reviewed non-quarantined slices")
    horizon = row["chunk_horizon"]
    assert horizon == 50
    intervals = subtract_intervals(cycle["interval_ns"], [x["interval_ns"] for x in annotation["exclusions"]])
    grid = arrays["time_ns"]
    origin = row["source_start_ns"]
    output = []
    for number, (start, stop) in enumerate(intervals):
        first, last = np.searchsorted(grid, [origin + start, origin + stop], side="left")
        local = arrays["future_signal_action_candidate_valid"][first:last].copy()
        local &= earliest_source_ns["signal"][first:last] >= origin + start
        starts = np.zeros(last - first, dtype=bool)
        if len(local) >= horizon:
            starts[:len(local) - horizon + 1] = np.lib.stride_tricks.sliding_window_view(local, horizon).all(axis=1)
        starts &= arrays["camera_timestamp_candidate_valid"][first:last].all(axis=1)
        starts &= earliest_source_ns["camera"][first:last] >= origin + start
        # This is a restriction of validated v1 supervision, never an expansion.
        starts &= arrays["chunk_start_candidate_valid"][first:last]
        indices = np.flatnonzero(starts) + first
        if len(indices):
            output.append({"episode": row["episode"], "cycle_id": cycle["cycle_id"], "segment": number,
                           "source_interval_ns": [start, stop], "source_start_frame": int(first),
                           "source_stop_frame_exclusive": int(last), "parent_split": row["parent_split"],
                           "chunk_horizon": horizon, "chunk_start_source_frames": indices.tolist(),
                           "aligned_npz_sha256": row["aligned_npz_sha256"],
                           "annotation_sha256": object_digest(annotation),
                           "base_label_origin": "posthoc_weak_unmodified", "training_approved": False})
    return output


def load_cut_source_times(v1, row, arrays):
    grid = arrays["time_ns"]
    signal_times = []
    with np.load((v1 / "signals" / row["episode"]).with_suffix(".npz"), allow_pickle=False) as signals:
        for name in STREAMS:
            stamps = signals[name + "_t_ns"]
            indices = np.searchsorted(stamps, grid, side="right") - 1
            signal_times.append(np.where(indices >= 0, stamps[np.maximum(indices, 0)], -1))
    camera_times = []
    with np.load((v1 / "cameras" / row["episode"]).with_suffix(".npz"), allow_pickle=False) as cameras:
        for column, name in enumerate(("head", "left", "right")):
            indices = arrays["camera_source_indices"][:, column]
            stamps = cameras[name + "_log_ns"]
            camera_times.append(np.where(indices >= 0, stamps[np.maximum(indices, 0)], -1))
    return {"signal": np.min(signal_times, axis=0), "camera": np.min(camera_times, axis=0)}


def check_baseline(root):
    baseline = read(root / "baseline.json")
    require(baseline["schema"] == SCHEMA, "Unknown baseline schema")
    for item in baseline["inputs"].values():
        require(digest(Path(item["path"])) == item["sha256"], f"Baseline input changed: {item['path']}")
    require(digest(root / "source_catalog.jsonl") == baseline["source_catalog_sha256"], "Source catalog changed")
    return baseline


def validate(root, export=False):
    baseline = check_baseline(root)
    v1 = Path(baseline["v1_root"])
    assignments = read(v1 / "source_split.json")["assignments"]
    metas = {x["episode"]: x for x in read_jsonl(v1 / "episode_manifest.jsonl")}
    rows = read_jsonl(root / "source_catalog.jsonl")
    require(len(rows) == len(metas) == baseline["source_count"], "Source catalog count mismatch")
    require({x["episode"] for x in rows} == set(metas), "Source catalog coverage mismatch")
    expected_annotations = {(root / "annotations" / (x["episode"] + ".json")).resolve() for x in rows}
    require({x.resolve() for x in (root / "annotations").rglob("*.json")} == expected_annotations,
            "Missing or extra source annotations")
    raw_root = Path(yaml.safe_load((v1 / "config_snapshot.yaml").read_text())["source"])
    errors, totals, priorities, issues, split_counts, slices, queue = [], Counter(), Counter(), Counter(), Counter(), [], []
    for row in rows:
        episode = row["episode"]
        try:
            require(row["parent_split"] == assignments[episode], "Frozen source split changed")
            require(all(row[k] == value for k, value in source_identity(metas[episode]).items()), "Source identity changed")
            require(row["confirmed_bad_base_masked"] == (episode in baseline["known_bad_base_sources"]), "Base quarantine mismatch")
            stat = (raw_root / row["source_relative_path"]).stat()
            require({"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns} == row["source_fingerprint"], "Raw fingerprint changed")
            pack = (v1 / "aligned" / episode).with_suffix(".npz")
            require(digest(pack) == row["aligned_npz_sha256"] and
                    digest(pack.with_suffix(".json")) == row["aligned_metadata_sha256"], "v1 candidate pack changed")
            annotation_path = root / "annotations" / (episode + ".json")
            annotation = read(annotation_path)
            accepted = validate_annotation(annotation, row, root)
            with np.load(pack, allow_pickle=False) as data:
                require(data["state"].shape == (row["frames"], 21) and data["action"].shape == (row["frames"], 19), "Candidate shape changed")
                require(not data["training_approved"].item(), "Unexpected v1 training approval")
                if row["confirmed_bad_base_masked"]:
                    require(not data["action_candidate_valid"][:, 16:].any() and
                            not data["chunk_start_candidate_valid"].any(), "Known bad base gained supervision")
                if accepted:
                    # Immutable intermediate hashes/fingerprints are checked before
                    # deriving source times used to guard new cut boundaries.
                    camera = (v1 / "cameras" / episode).with_suffix(".npz")
                    require(digest(camera) == metas[episode]["camera_npz_sha256"], "Camera index changed")
                    signal = (v1 / "signals" / episode).with_suffix(".npz").stat()
                    require({"size": signal.st_size, "mtime_ns": signal.st_mtime_ns} == metas[episode]["signal_npz_fingerprint"], "Signals changed")
                    times = load_cut_source_times(v1, row, data)
                    for cycle in accepted:
                        slices.extend(cycle_slices(cycle, row, annotation, data, times))
            totals["validated_sources"] += 1
            totals["proposed_cycle_intervals"] += sum(x["status"] == "proposed" for x in annotation["cycles"])
            totals["reviewed_successful_cycles"] += len(accepted)
            totals["fully_reviewed_recordings"] += annotation["recording_review"]["status"] == "reviewed"
            totals["known_bad_base_sources_preserved"] += row["confirmed_bad_base_masked"]
            split_counts[row["parent_split"]] += 1
            priorities[row["review_priority"]] += 1
            issues.update(row["review_reasons"])
            queue.append({"episode": episode, "priority": row["review_priority"], "parent_split": row["parent_split"],
                          "duration_s": row["source_duration_ns"] / 1e9, "cycle_count": annotation["recording_review"]["cycle_count"],
                          "proposed_intervals": len(annotation["cycles"]), "reviewed_successful_cycles": len(accepted),
                          "review_status": annotation["recording_review"]["status"], "reasons": row["review_reasons"],
                          "annotation": str(annotation_path.relative_to(root))})
        except (ValueError, KeyError, TypeError, OSError, IndexError) as exc:
            errors.append({"episode": episode, "error": str(exc)})
    totals["logical_slices"] = len(slices)
    totals["chunk_starts"] = sum(len(x["chunk_start_source_frames"]) for x in slices)
    report = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(), "success": not errors,
              "source_count": len(rows), "totals": dict(totals), "split_counts": dict(split_counts),
              "priority_counts": dict(sorted(priorities.items())), "issue_counts": dict(issues), "errors": errors,
              "raw_files_modified": 0, "v1_files_modified": 0, "approved_training_episodes": 0,
              "meaning": "Annotation/index integrity only; sparse hints are not completed cycle, success, clock or interface validation.",
              "limitations": ["Remaining cycle counts and outcomes require continuous review.",
                              "No repaired base labels are implemented; known-bad base cannot be unmasked here.",
                              "No formal training reader, normalization, robot replay or model training is run.",
                              "Raw source identity is checked by size/mtime; candidate packs use SHA256."]}
    write(root / "validation.json", report)
    write_jsonl(root / "review_queue.jsonl", queue)
    with (root / "review_queue.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("episode", "priority", "parent_split", "duration_s", "cycle_count",
                                                    "proposed_intervals", "reviewed_successful_cycles", "review_status", "reasons", "annotation"))
        writer.writeheader()
        writer.writerows({**row, "reasons": json.dumps(row["reasons"], ensure_ascii=False)} for row in queue)
    if export and not errors:
        write_jsonl(root / "logical_slices.jsonl", slices)
        write(root / "export_manifest.json", {"schema": SCHEMA, "source_catalog_sha256": baseline["source_catalog_sha256"],
              "annotation_collection_sha256": object_digest({str(path.relative_to(root)): digest(path) for path in sorted(expected_annotations)}),
              "logical_slices_sha256": digest(root / "logical_slices.jsonl"), "logical_slices": len(slices),
              "chunk_starts": totals["chunk_starts"], "state_dim": 21, "action_dim": 19, "grid_hz": 30, "chunk_horizon": 50,
              "training_approved": False, "scope": "Reviewed logical indexes only; no padded tails or rewritten signal/image arrays."})
    print(json.dumps({"success": not errors, **dict(totals), "errors": len(errors)}, ensure_ascii=False))
    return not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "validate", "export"))
    parser.add_argument("--v1", type=Path, default=TASK / "results/cleaning_v1")
    parser.add_argument("--output", type=Path, default=TASK / "results/cleaning_v2_review")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.v1.resolve(), args.output.resolve())
    return 0 if validate(args.output.resolve(), export=args.command == "export") else 1


if __name__ == "__main__":
    raise SystemExit(main())
