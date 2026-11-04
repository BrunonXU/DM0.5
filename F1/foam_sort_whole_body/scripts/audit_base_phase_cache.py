#!/usr/bin/env python3
"""Audit cached head background motion against complete raw weak-base intervals.

Offline review queue only. Does not modify caches, sources, masks or candidates.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import traceback

import numpy as np

import head_background_motion as motion

TASK = Path(__file__).resolve().parents[1]
SETTINGS = {
    "schema": "f1_base_phase_cache_review_v1",
    "max_head_causal_age_s": 0.1,
    "max_signal_coverage_gap_s": 0.1,
    "max_head_ptp_deg": 0.3,
    "max_body_ptp_deg": 0.2,
    "min_continuous_interval_s": 1.0,
    "base_zero_rule": "every raw held value exactly zero in all three components",
    "pose_threshold_scope": "diagnostic_confounding_gate_not_hardware_limits",
    "minimum_hal_samples_inclusive": 3,
    "training_accepted": False,
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def held_interval(times, values, start_ns, end_ns):
    """Include the value held at start and EVERY subsequent sample through end.

    Duplicate messages exactly at start are retained conservatively. Times are
    original MCAP log nanoseconds: no JSON/head-origin offset is added here.
    """
    begin = int(np.searchsorted(times, start_ns, side="right")) - 1
    first_equal = int(np.searchsorted(times, start_ns, side="left"))
    if first_equal < len(times) and int(times[first_equal]) == start_ns:
        begin = first_equal
    end = int(np.searchsorted(times, end_ns, side="right"))
    result = {"covered": False, "count_including_held": 0, "samples_strictly_after_start": 0,
              "held_age_at_start_s": None, "last_age_at_end_s": None, "max_coverage_gap_s": None,
              "all_finite": False, "start_index": begin, "end_index_exclusive": end}
    if begin < 0 or end <= begin or end_ns <= start_ns:
        return result, np.empty((0, values.shape[1]), dtype=float)
    segment = values[begin:end]
    segment_times = times[begin:end]
    start_age = (start_ns - int(segment_times[0])) / 1e9
    end_age = (end_ns - int(segment_times[-1])) / 1e9
    gaps = np.diff(segment_times).astype(float) / 1e9
    max_gap = max(start_age, end_age, float(gaps.max()) if len(gaps) else 0.0)
    finite = bool(np.isfinite(segment).all())
    result.update(count_including_held=len(segment),
                  samples_strictly_after_start=int(np.sum(segment_times > start_ns)),
                  held_age_at_start_s=start_age, last_age_at_end_s=end_age,
                  max_coverage_gap_s=max_gap, all_finite=finite,
                  covered=finite and 0 <= start_age <= SETTINGS["max_signal_coverage_gap_s"]
                  and 0 <= end_age <= SETTINGS["max_signal_coverage_gap_s"]
                  and max_gap <= SETTINGS["max_signal_coverage_gap_s"])
    return result, segment


def _finite_list(values):
    return [float(value) if np.isfinite(value) else None for value in values]


def supervision_span(times, joint_mask, base_mask, start_ns, end_ns, tail_ns):
    first = int(np.searchsorted(times, start_ns, side="left"))
    last = int(np.searchsorted(times, end_ns, side="right"))
    count = last - first
    return {
        "tail_relation": "before" if end_ns < tail_ns else "after" if start_ns >= tail_ns else "crosses",
        "entire_pair_before_tail_boundary": bool(end_ns < tail_ns),
        "before_tail_duration_s": max(0, min(end_ns, tail_ns) - start_ns) / 1e9,
        "aligned_frame_count": count,
        "joint_supervised_frame_count": int(joint_mask[first:last].sum()),
        "base_supervised_frame_count": int(base_mask[first:last].sum()),
        "all_joint_frames_supervised": bool(count and joint_mask[first:last].all()),
        "all_base_frames_supervised": bool(count and base_mask[first:last].all()),
    }


def merge_intervals(pairs, row, tail_ns, times, joint_mask, base_mask, hal_times, hal_q):
    intervals = []
    start = 0
    eligible = {"zero_label_motion_review", "nonzero_label_low_motion_review"}
    while start < len(pairs):
        pair = pairs[start]
        if pair["evidence_status"] not in eligible:
            start += 1
            continue
        end = start + 1
        while (end < len(pairs)
               and pairs[end]["evidence_status"] == pair["evidence_status"]
               and pairs[end]["previous_head_log_ns"] == pairs[end - 1]["current_head_log_ns"]):
            # Small pairwise drift must not silently accumulate into a large
            # head/body movement over a supposedly stable merged interval.
            pose_coverage, poses = held_interval(hal_times, hal_q, pair["previous_head_log_ns"], pairs[end]["current_head_log_ns"])
            span = np.ptp(poses, axis=0) if len(poses) else np.full(8, np.nan)
            if not pose_coverage["covered"] or np.max(span[:3]) > 0.3 or np.max(span[3:]) > 0.2:
                break
            end += 1
        run = pairs[start:end]
        first_ns, last_ns = run[0]["previous_head_log_ns"], run[-1]["current_head_log_ns"]
        duration = (last_ns - first_ns) / 1e9
        if duration >= SETTINGS["min_continuous_interval_s"]:
            pose_coverage, poses = held_interval(hal_times, hal_q, first_ns, last_ns)
            full_pose_span = np.ptp(poses, axis=0)
            supervision = supervision_span(times, joint_mask, base_mask, first_ns, last_ns, tail_ns)
            before_active = supervision["before_tail_duration_s"] >= 1.0 and supervision["joint_supervised_frame_count"] > 0
            zero_trigger = pair["evidence_status"] == "zero_label_motion_review"
            intervals.append({
                "episode": row["episode"], "parent_split": row["parent_split"],
                "kind": pair["evidence_status"], "priority": (1 if zero_trigger else 2) if before_active else (3 if zero_trigger else 4),
                "start_log_ns": first_ns, "end_log_ns": last_ns,
                "start_s": (first_ns - row["source_start_ns"]) / 1e9,
                "end_s": (last_ns - row["source_start_ns"]) / 1e9,
                "duration_s": duration, "pair_count": len(run),
                "pair_indices": [p["pair_index"] for p in run],
                "median_speed_px_s": float(np.median([p["motion"]["speed_px_s"] for p in run])),
                "median_confidence": float(np.median([p["motion"]["confidence"] for p in run])),
                "minimum_consistent_rois": min(p["motion"]["consistent_roi_count"] for p in run),
                "max_head_pair_ptp_deg": max(p["head_body"]["head_max_ptp_deg"] for p in run),
                "max_body_pair_ptp_deg": max(p["head_body"]["body_max_ptp_deg"] for p in run),
                "full_interval_head_body_ptp_deg": _finite_list(full_pose_span),
                "full_interval_head_max_ptp_deg": float(np.max(full_pose_span[:3])),
                "full_interval_body_max_ptp_deg": float(np.max(full_pose_span[3:])),
                "full_interval_pose_stable": bool(pose_coverage["covered"] and np.max(full_pose_span[:3]) <= 0.3 and np.max(full_pose_span[3:]) <= 0.2),
                "supervision": supervision,
                "base_zero_all_interval_samples": zero_trigger,
                "source_accepted": False, "automatic_source_exclusion": False,
                "interpretation": "review_visual_weak_label_timing_not_calibrated_chassis_motion",
            })
        start = end
    return intervals


def process(row, mask_row, args, identity):
    started = time.monotonic()
    episode = row["episode"]
    output_json = (args.output / "sources" / episode).with_suffix(".json")
    pair_path = output_json.with_suffix(".pairs.jsonl")
    cache_json = (args.cache / "sources" / episode).with_suffix(".json")
    cache_npz = cache_json.with_suffix(".npz")
    result = {"episode": episode, "parent_split": row["parent_split"], "status": "error",
              "identity": identity, "created_at": stamp(), "source_accepted": False}
    try:
        cache = json.loads(cache_json.read_text())
        assert cache["status"] == "complete", cache.get("error", "cache not complete")
        assert cache["identity"] == identity["cache_identity"]
        assert cache["episode"] == episode and cache["source_start_ns"] == row["source_start_ns"]
        assert cache["source_duration_ns"] == row["source_duration_ns"]
        assert cache["source_fingerprint"] == row["source_fingerprint"]
        assert cache["signal_npz_sha256"] == row["signal_npz_sha256"]
        assert cache["camera_npz_sha256"] == row["camera_npz_sha256"]
        assert cache["head_body_columns"] == ["head_j1", "head_j2", "head_j3", *[f"body_j{i}" for i in range(1, 6)]]
        assert cache["source_stat_unchanged"] and cache["head_body_matches_existing_body_cache"]
        source_stat = Path(row["raw_path"]).stat()
        assert {"size_bytes": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns} == row["source_fingerprint"]
        mask_path = Path(mask_row["aligned_npz_path"])
        mask_json = mask_path.with_suffix(".json")
        input_hashes = {"cache_npz_sha256": sha(cache_npz), "cache_metadata_sha256": sha(cache_json),
                        "signal_npz_sha256": sha(row["signal_npz_path"]),
                        "mask_npz_sha256": sha(mask_path), "mask_metadata_sha256": sha(mask_json)}
        assert input_hashes["cache_npz_sha256"] == cache["npz_sha256"]
        assert input_hashes["signal_npz_sha256"] == row["signal_npz_sha256"]
        assert input_hashes["mask_npz_sha256"] == mask_row["aligned_npz_sha256"]
        assert input_hashes["mask_metadata_sha256"] == mask_row["aligned_metadata_sha256"]
        result["input_hashes"] = input_hashes
        result["input_paths"] = {"cache_npz": str(cache_npz), "cache_metadata": str(cache_json),
                                 "signal_npz": row["signal_npz_path"], "mask_npz": str(mask_path), "mask_metadata": str(mask_json)}
        if output_json.exists():
            old = json.loads(output_json.read_text())
            if (old.get("status") == "complete" and old.get("identity") == identity
                    and old.get("input_hashes") == input_hashes and pair_path.exists()
                    and sha(pair_path) == old["pairs_sha256"]):
                return old
        with np.load(cache_npz, allow_pickle=False) as data:
            gray = data["head_gray"]
            logs, headers, queries = data["head_log_ns"], data["head_header_ns"], data["query_ns"]
            source_indices, ages = data["head_source_indices"], data["head_age_s"]
            jpeg_hashes = data["head_jpeg_sha256"]
            hal_times, hal_q = data["hal_log_ns"], data["head_body_q"]
        assert gray.shape == (len(logs), 270, 480) and gray.dtype == np.uint8
        assert all(len(x) == len(logs) for x in (headers, queries, source_indices, ages, jpeg_hashes))
        assert hal_q.shape == (len(hal_times), 8)
        assert np.all(np.diff(logs) >= 0) and np.all(np.diff(queries) > 0)
        assert np.all(np.diff(hal_times) >= 0)
        assert np.allclose(ages, (queries - logs) / 1e9, rtol=0, atol=1e-12)
        assert np.all(np.diff(queries) == round(cache["sampling_step_s"] * 1e9))
        with np.load(row["signal_npz_path"], allow_pickle=False) as data:
            base_times, base_v = data["base_t_ns"], data["base_v"]
            assert np.array_equal(hal_times, data["hal_t_ns"])
            assert np.array_equal(hal_q[:, 3:], data["hal_q"][:, -5:], equal_nan=True)
        assert base_v.shape == (len(base_times), 3) and np.all(np.diff(base_times) >= 0)
        meta = json.loads(mask_json.read_text())
        with np.load(mask_path, allow_pickle=False) as data:
            aligned_times = data["time_ns"]
            joint_mask = data["joint_frame_candidate_valid"]
            action_mask = data["action_candidate_valid"]
            base_mask = action_mask[:, 16:19].all(axis=1)
            chunk_mask = data["chunk_start_candidate_valid"]
            assert not bool(data["training_approved"])
        tail_index = mask_row["tail_mask_start_index"]
        assert tail_index is not None and 0 <= tail_index < len(aligned_times)
        tail_ns = int(aligned_times[tail_index])
        assert int(aligned_times[0]) == row["source_start_ns"] == meta["start_ns"]
        assert meta["episode"] == mask_row["episode"] == episode
        assert meta["end_ns"] + 1 == row["source_stop_ns_exclusive"]
        assert meta["parent_aligned_npz_sha256"] == row["aligned_npz_sha256"] == mask_row["parent_aligned_npz_sha256"]
        assert meta["parent_aligned_metadata_sha256"] == row["aligned_metadata_sha256"]
        assert meta["frames"] == len(aligned_times) == mask_row["frames_retained"]
        assert meta["tail_mask_start_index"] == tail_index
        tail_s = (tail_ns - row["source_start_ns"]) / 1e9
        assert abs(tail_s - mask_row["tail_mask_start_s"]) < 1e-9
        assert abs(tail_s - meta["tail_mask_start_s"]) < 1e-9
        assert action_mask.shape == (len(aligned_times), 19)
        assert not action_mask[tail_index:].any() and not joint_mask[tail_index:].any()
        assert not chunk_mask[max(0, tail_index - meta["chunk_horizon"] + 1):].any()
        assert int(joint_mask.sum()) == mask_row["joint_valid_frames_after"]
        assert int(chunk_mask.sum()) == mask_row["chunk_starts_after"]
        result["tail_boundary"] = {"index": tail_index, "relative_source_s": tail_s, "log_ns": tail_ns,
                                   "manifest_metadata_arrays_verified": True,
                                   "meaning": "first_all_19D_masked_grid_frame_not_physical_stop_or_reset_onset"}

        pairs = []
        for i in range(len(logs) - 1):
            start_ns, end_ns = int(logs[i]), int(logs[i + 1])
            dt = (end_ns - start_ns) / 1e9
            reasons = []
            fresh = bool(0 <= ages[i] <= 0.1 and 0 <= ages[i + 1] <= 0.1)
            if not fresh:
                reasons.append("stale_or_future_head_query")
            repeated_jpeg = str(jpeg_hashes[i]) == str(jpeg_hashes[i + 1])
            repeated_gray = bool(np.array_equal(gray[i], gray[i + 1]))
            repeated_source = bool(start_ns == end_ns or source_indices[i] == source_indices[i + 1])
            if repeated_jpeg or repeated_gray or repeated_source:
                reasons.append("repeated_or_frozen_frame_suspected")
            if dt <= 0 or dt > cache["sampling_step_s"] + 0.1 or dt < cache["sampling_step_s"] - 0.1:
                reasons.append("irregular_actual_head_interval")
            registration = motion.analyze_pair(gray[i], gray[i + 1], dt)
            if registration["uncertain"]:
                reasons.append("uncertain_background_registration")
            base, values = held_interval(base_times, base_v, start_ns, end_ns)
            base.update(all_zero=bool(base["covered"] and np.all(values == 0)),
                        all_nonzero=bool(base["covered"] and np.all(np.any(values != 0, axis=1))),
                        any_nonzero=bool(base["all_finite"] and np.any(values != 0)),
                        max_abs_components=_finite_list(np.max(np.abs(values), axis=0)) if len(values) else [None] * 3)
            if not base["covered"]:
                reasons.append("raw_base_interval_not_fully_covered")
            pose, poses = held_interval(hal_times, hal_q, start_ns, end_ns)
            spans = np.ptp(poses, axis=0) if len(poses) else np.full(8, np.nan)
            head_ptp = float(np.max(spans[:3])) if np.isfinite(spans[:3]).all() else None
            body_ptp = float(np.max(spans[3:])) if np.isfinite(spans[3:]).all() else None
            pose.update(ptp_deg=_finite_list(spans), head_max_ptp_deg=head_ptp, body_max_ptp_deg=body_ptp,
                        diagnostic_pose_stable=bool(pose["covered"] and len(poses) >= 3
                                                    and head_ptp is not None and body_ptp is not None
                                                    and head_ptp <= 0.3 and body_ptp <= 0.2))
            if not pose["covered"] or len(poses) < 3:
                reasons.append("raw_head_body_interval_not_fully_covered")
            elif not pose["diagnostic_pose_stable"]:
                reasons.append("head_or_body_motion_confounds_chassis_interpretation")
            if reasons:
                evidence = "uncertain"
            elif registration["trusted_motion"] and base["all_zero"]:
                evidence = "zero_label_motion_review"
            elif registration["status"] == "low_motion" and base["all_nonzero"]:
                evidence = "nonzero_label_low_motion_review"
            else:
                evidence = "no_priority_trigger_observed_not_accepted"
            pair = {"episode": episode, "pair_index": i, "previous_cache_frame_index": i, "current_cache_frame_index": i + 1,
                    "previous_head_log_ns": start_ns, "current_head_log_ns": end_ns,
                    "previous_head_header_ns": int(headers[i]), "current_head_header_ns": int(headers[i + 1]),
                    "previous_source_frame_index": int(source_indices[i]), "current_source_frame_index": int(source_indices[i + 1]),
                    "previous_query_ns": int(queries[i]), "current_query_ns": int(queries[i + 1]),
                    "start_s": (start_ns - row["source_start_ns"]) / 1e9,
                    "end_s": (end_ns - row["source_start_ns"]) / 1e9, "actual_dt_s": dt,
                    "previous_causal_age_s": float(ages[i]), "current_causal_age_s": float(ages[i + 1]),
                    "previous_jpeg_sha256": str(jpeg_hashes[i]), "current_jpeg_sha256": str(jpeg_hashes[i + 1]),
                    "repeated_jpeg": repeated_jpeg, "repeated_gray": repeated_gray, "repeated_source_frame": repeated_source,
                    "evidence_status": evidence, "uncertainty_reasons": reasons,
                    "motion": registration, "base": base, "head_body": pose,
                    "supervision": supervision_span(aligned_times, joint_mask, base_mask, start_ns, end_ns, tail_ns)}
            pairs.append(pair)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = pair_path.with_suffix(pair_path.suffix + ".tmp")
        with temporary.open("w") as stream:
            for pair in pairs:
                stream.write(json.dumps(pair, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        temporary.replace(pair_path)
        intervals = merge_intervals(pairs, row, tail_ns, aligned_times, joint_mask, base_mask, hal_times, hal_q)
        result.update(status="complete", frames=len(logs), pairs=len(pairs),
                      pairs_path=str(pair_path), pairs_sha256=sha(pair_path),
                      evidence_status_counts=dict(Counter(p["evidence_status"] for p in pairs)),
                      motion_status_counts=dict(Counter(p["motion"]["status"] for p in pairs)),
                      uncertainty_reason_counts=dict(Counter(reason for p in pairs for reason in p["uncertainty_reasons"])),
                      head_query_age_max_s=float(ages.max()) if len(ages) else None,
                      repeated_jpeg_pairs=sum(p["repeated_jpeg"] for p in pairs),
                      repeated_gray_pairs=sum(p["repeated_gray"] for p in pairs),
                      intervals=intervals, interval_count=len(intervals),
                      priority_1_intervals=sum(i["priority"] == 1 for i in intervals),
                      priority_2_intervals=sum(i["priority"] == 2 for i in intervals),
                      source_review_status="priority_review" if intervals else "no_continuous_priority_trigger_observed_not_accepted",
                      raw_source_fingerprint_rechecked=True)
    except Exception as error:
        result.update(error=repr(error), traceback=traceback.format_exc())
    result["elapsed_s"] = time.monotonic() - started
    write_json(output_json, result)
    return result


def write_aggregates(args, rows, selected, results, identity, started, finished=False):
    ordered = sorted(results.values(), key=lambda r: r["episode"])
    complete = [r for r in ordered if r["status"] == "complete"]
    errors = [r for r in ordered if r["status"] != "complete"]
    intervals = [i for r in complete for i in r["intervals"]]
    intervals.sort(key=lambda i: (i["priority"], -i["supervision"]["before_tail_duration_s"], -i["duration_s"], i["episode"]))
    progress = {"updated_at": stamp(), "status": ("complete_with_errors" if errors else "complete") if finished else "running",
                "requested": len(selected), "total_candidate_scope": len(rows), "complete": len(complete),
                "errors": len(errors), "pending": len(selected) - len(ordered),
                "pairs_analyzed": sum(r["pairs"] for r in complete), "sampled_frames": sum(r["frames"] for r in complete),
                "elapsed_s": time.monotonic() - started, "source_acceptances": 0, "source_exclusions": 0,
                "priority_interval_count": len(intervals)}
    write_json(args.output / "progress.json", progress)
    if not finished and len(ordered) % 25 != 0:
        return progress
    evidence_counts, motion_counts, uncertain_counts = Counter(), Counter(), Counter()
    for result in complete:
        evidence_counts.update(result["evidence_status_counts"])
        motion_counts.update(result["motion_status_counts"])
        uncertain_counts.update(result["uncertainty_reason_counts"])
    summary = {**progress, "identity": identity, "evidence_status_counts": dict(evidence_counts),
               "motion_status_counts": dict(motion_counts), "uncertainty_reason_counts": dict(uncertain_counts),
               "interval_priority_counts": dict(Counter(i["priority"] for i in intervals)),
               "source_counts_with_interval_priority": {str(p): len({i["episode"] for i in intervals if i["priority"] == p}) for p in range(1, 5)},
               "sources_with_any_priority_interval": sum(bool(r["intervals"]) for r in complete),
               "sources_with_uncertain_pairs": sum(r["evidence_status_counts"].get("uncertain", 0) > 0 for r in complete),
               "source_errors": [{"episode": r["episode"], "error": r.get("error")} for r in errors],
               "coverage": [{"episode": row["episode"], "status": results.get(row["episode"], {}).get("status", "pending")}
                            for row in selected],
               "top_intervals": intervals[:30], "all_sources_accepted": False,
               "interpretation": "sampled_weak_label_phase_review_queue_only_no_visual_or_training_acceptance"}
    write_json(args.output / "summary.json", summary)
    write_json(args.output / "coverage.json", {"candidate_sha256": identity["candidate_sha256"], "scope": len(rows),
                                               "requested": len(selected), "coverage": summary["coverage"]})
    interval_path = args.output / "review_intervals.jsonl"
    temporary = interval_path.with_suffix(".jsonl.tmp")
    with temporary.open("w") as stream:
        for interval in intervals:
            stream.write(json.dumps(interval, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(interval_path)
    with (args.output / "episode_summaries.csv").open("w", newline="") as stream:
        columns = ["episode", "parent_split", "status", "frames", "pairs", "uncertain_pairs", "zero_label_motion_pairs",
                   "nonzero_label_low_motion_pairs", "interval_count", "priority_1_intervals", "priority_2_intervals", "tail_boundary_s", "elapsed_s", "error"]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for result in ordered:
            record = {key: result.get(key) for key in columns}
            counts = result.get("evidence_status_counts", {})
            record.update(uncertain_pairs=counts.get("uncertain", 0), zero_label_motion_pairs=counts.get("zero_label_motion_review", 0),
                          nonzero_label_low_motion_pairs=counts.get("nonzero_label_low_motion_review", 0),
                          tail_boundary_s=result.get("tail_boundary", {}).get("relative_source_s"))
            writer.writerow(record)
    with (args.output / "review_intervals.csv").open("w", newline="") as stream:
        columns = ["priority", "episode", "parent_split", "kind", "start_s", "end_s", "duration_s", "pair_count",
                   "before_tail_duration_s", "tail_relation", "joint_supervised_frame_count", "median_speed_px_s", "median_confidence", "minimum_consistent_rois"]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for interval in intervals:
            record = {key: interval.get(key) for key in columns}
            record.update({key: interval["supervision"][key] for key in ("before_tail_duration_s", "tail_relation", "joint_supervised_frame_count")})
            writer.writerow(record)
    lines = ["# 775份底盘阶段背景筛查", "", f"日期：2026-09-15；状态：`{progress['status']}`。", "",
             f"已完成{len(complete)}/{len(selected)}份，错误{len(errors)}份；共{progress['pairs_analyzed']}个相邻原head图像对。"
             f"生成{len(intervals)}段至少1秒的复核区间，未自动排除、修改mask或验收任何来源。", "",
             "## 方法与有效范围", "",
             "- 每0.5秒查询最新因果原head，使用真实log时间差；两图age均≤0.1秒。header时间保留但不偏移曝光时钟。",
             "- 中央六ROI相位相关、位移后NCC及方向一致性；单位仅480×270图像像素、像素/秒，不是底盘m/s。",
             "- 重复JPEG、相同灰度、重复源帧、弱注册、遮挡疑似、图像过旧或原信号覆盖不足均保留uncertain，不能当clean。",
             "- base_t_ns/base_v按两图间每条原样本及起点保持值判全段exact-zero；不只看端点，不添加JSON起点偏移。",
             "- HAL按相同完整区间计算head3/body5峰峰值；head≤0.3°、body≤0.2°为诊断姿态门槛，不是硬件限位。",
             "- 合并区间也复核完整HAL峰峰值，累计漂移超过诊断门槛即断开，避免逐对小变化被合成长时姿态稳定。",
             "- 信号起点/终点保持年龄及最大采样间隔≤0.1秒，HAL至少3条；超过或有缺值则不确定。",
             "- tail界由775 mask manifest、派生NPZ time_ns及JSON三方校验，检查尾后19D mask全false和chunk不跨界；尾界不是物理停车或复位时刻。",
             "- 本轮按冻结的775旧tail mask manifest报告监督范围；000080另有启动前11帧的更严mask，本轮未覆盖写该处理版本。",
             "- priority 1：零弱标签且背景运动、姿态稳定，连续≥1秒且尾界前≥1秒并至少含一个有效联合监督帧；priority 2：非零弱标签且可靠低图像位移，采用相同尾前时长/有效帧条件。并不要求全部区间均可监督。",
             "- priority 3/4分别为对应模式但未同时满足尾界前≥1秒及至少一个有效联合监督帧；低图像位移也不能证明真实底盘停止。",
             "- 固定ROI不保证永远不含机械臂/人物，地面视差和头部未记录运动也可能混杂；必须查看原图后决定处理。", "",
             "## 统计", "", "```json", json.dumps({"evidence": dict(evidence_counts), "uncertainty_reasons": dict(uncertain_counts),
                                                            "interval_priorities": summary["interval_priority_counts"],
                                                            "sources_with_priority": summary["source_counts_with_interval_priority"]}, ensure_ascii=False, indent=2), "```", "",
             "## 优先复核区间", "", "|优先级|来源|类型|原log相对源起点区间(s)|尾前时长(s)|一致ROI下限|", "|---|---|---|---|---|---|"]
    for interval in intervals[:30]:
        lines.append(f"|{interval['priority']}|{interval['episode']}|{interval['kind']}|{interval['start_s']:.3f}–{interval['end_s']:.3f}|{interval['supervision']['before_tail_duration_s']:.3f}|{interval['minimum_consistent_rois']}|")
    lines += ["", "## 证据和复现", "", "`summary.json`保存覆盖与统计，`coverage.json`列每份状态；`episode_summaries.csv`和`review_intervals.csv/jsonl`便于筛选。"
              "逐来源`sources/<episode>.json`保存输入hash、尾界校验和区间；`.pairs.jsonl`保留每对图像hash、准确时间、完整ROI指标、HAL峰峰值、base区间覆盖及监督mask统计。", "",
              "6000D，工作目录`/home/user/projects`：", "", "```bash",
              "/tmp/f1-foam-audit-venv/bin/python F1/foam_sort_whole_body/scripts/audit_base_phase_cache.py --workers 2", "```", "",
              "方法正例/合成验证见`../motion_method/index.html`；18项合成验证和13对既有正例均通过，不代表775来源的检出精度或训练验收。", "",
              "未读取原MCAP图像二次抽帧；仅读既有cache、原signals和派生mask，并复核原source的size/mtime。未SSH、ROS、训练或真机动作。"]
    (args.output / "findings.md").write_text("\n".join(lines) + "\n")
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=TASK / "results/demo_775_selection/candidates.jsonl")
    parser.add_argument("--mask-manifest", type=Path, default=TASK / "results/demo_775_selection/mask_manifest.jsonl")
    parser.add_argument("--cache", type=Path, default=TASK / "results/demo_775_phase_review/frame_cache")
    parser.add_argument("--output", type=Path, default=TASK / "results/demo_775_phase_review/base_phase_screen")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cache-wait-seconds", type=float, default=1800)
    args = parser.parse_args()
    assert 1 <= args.workers <= 4
    rows = [json.loads(line) for line in args.candidates.read_text().splitlines() if line.strip()]
    masks = [json.loads(line) for line in args.mask_manifest.read_text().splitlines() if line.strip()]
    mask_by_episode = {row["episode"]: row for row in masks}
    assert len(rows) == len({r["episode"] for r in rows}) == len(masks) == len(mask_by_episode)
    assert {r["episode"] for r in rows} == set(mask_by_episode)
    cache_identity = json.loads((args.cache / "run_config.json").read_text())
    identity = {"settings": SETTINGS, "script_sha256": sha(Path(__file__)),
                "method_version": motion.METHOD_VERSION, "method_sha256": sha(Path(motion.__file__)),
                "candidate_sha256": sha(args.candidates), "mask_manifest_sha256": sha(args.mask_manifest),
                "cache_identity": cache_identity, "cache_run_config_sha256": sha(args.cache / "run_config.json")}
    assert identity["candidate_sha256"] == cache_identity["candidate_sha256"]
    assert cache_identity["step_s"] == .5 and cache_identity["width"] == 480
    assert cache_identity["script_sha256"] == sha(TASK / "scripts/cache_base_phase_frames.py")
    config_path = args.output / "run_config.json"
    if config_path.exists():
        assert json.loads(config_path.read_text()) == identity, "Method/input changed; use a new output directory"
    else:
        write_json(config_path, identity)
    selected = rows[:args.limit] if args.limit else rows
    pending, futures, results = {r["episode"]: r for r in selected}, {}, {}
    started = time.monotonic()
    write_aggregates(args, rows, selected, results, identity, started)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while pending or futures:
            for episode, row in list(pending.items()):
                if len(futures) >= args.workers:
                    break
                metadata_path = (args.cache / "sources" / episode).with_suffix(".json")
                if metadata_path.exists():
                    futures[pool.submit(process, row, mask_by_episode[episode], args, identity)] = episode
                    del pending[episode]
                elif time.monotonic() - started > args.cache_wait_seconds:
                    result = {"episode": episode, "parent_split": row["parent_split"], "status": "error", "error": "cache_wait_timeout",
                              "identity": identity, "source_accepted": False}
                    write_json((args.output / "sources" / episode).with_suffix(".json"), result)
                    results[episode] = result
                    del pending[episode]
            if futures:
                done, _ = wait(futures, timeout=2.0, return_when=FIRST_COMPLETED)
                for future in done:
                    episode = futures.pop(future)
                    results[episode] = future.result()
                    progress = write_aggregates(args, rows, selected, results, identity, started)
                    if len(results) % 25 == 0 or results[episode]["status"] == "error" or len(results) == len(selected):
                        print(json.dumps({**progress, "last_episode": episode, "last_status": results[episode]["status"],
                                          "last_error": results[episode].get("error")}, ensure_ascii=False), flush=True)
            elif pending:
                write_aggregates(args, rows, selected, results, identity, started)
                time.sleep(2)
    final = write_aggregates(args, rows, selected, results, identity, started, finished=True)
    print(json.dumps(final, ensure_ascii=False, indent=2), flush=True)
    if final["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
