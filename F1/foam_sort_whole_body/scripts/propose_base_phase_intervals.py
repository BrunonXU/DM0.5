#!/usr/bin/env python3
"""Produce background-motion review proposals, never robot-coordinate labels.

Reuse the hashed half-second image-pair audit and original numeric/cache arrays.
No total-duration window prior, largest-cluster selection, gap filling, metric
conversion, or automatic human acceptance is performed. All source intervals,
including unknown coverage, survive in the output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from audit_base_phase_cache import held_interval

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("motion", "low_motion", "unknown")
LIMITATIONS = [
    "Image displacement is not measured chassis velocity or robot-frame direction.",
    "Low image motion is not proof of chassis stop; unknown is not zero velocity.",
    "Half-second image pairs give temporal envelopes, not exact change times.",
    "Head/body gates are diagnostic confounding checks, not execution limits.",
    "No gripper command event is a confirmed grasp/release or travel boundary.",
    "Every proposal remains unreviewed; six physical phase intervals and distances require evidence.",
]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def javascript_safe(value, key=""):
    """Absolute nanoseconds exceed JS integer precision; retain their exact text."""
    if isinstance(value, dict):
        return {k: javascript_safe(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [javascript_safe(v, key) for v in value]
    if isinstance(value, int) and key.endswith("_ns"):
        return str(value)
    return value


def angle_difference(left, right):
    a = np.array([left["motion"]["dx_px"], left["motion"]["dy_px"]], dtype=float)
    b = np.array([right["motion"]["dx_px"], right["motion"]["dy_px"]], dtype=float)
    if not np.isfinite(a).all() or not np.isfinite(b).all() or not np.linalg.norm(a) or not np.linalg.norm(b):
        return None
    return math.degrees(math.acos(float(np.clip(np.dot(a, b) / np.linalg.norm(a) / np.linalg.norm(b), -1, 1))))


def pair_kind(pair):
    pose = pair["head_body"]
    motion = pair["motion"]
    reasons = list(pair["uncertainty_reasons"])
    if not pose.get("covered") or not pose.get("all_finite") or not pose.get("diagnostic_pose_stable"):
        reasons.append("head_body_not_covered_finite_and_stable")
    if pair["evidence_status"] == "uncertain" or motion.get("uncertain") or reasons:
        return "unknown", sorted(set(reasons))
    if motion["status"] == "motion" and motion.get("trusted_motion"):
        return "motion", []
    if motion["status"] == "low_motion":
        return "low_motion", []
    return "unknown", ["no_reliable_motion_class"]


def span_pose(rows, hal_t, head_body):
    coverage, values = held_interval(hal_t, head_body,
                                    rows[0]["previous_head_log_ns"], rows[-1]["current_head_log_ns"])
    span = np.ptp(values, axis=0) if len(values) else np.full(8, np.nan)
    head = float(np.max(span[:3])) if np.isfinite(span).all() else None
    body = float(np.max(span[3:])) if np.isfinite(span).all() else None
    return {"covered": coverage["covered"], "head_max_ptp_deg": head, "body_max_ptp_deg": body,
            "diagnostic_pose_stable": bool(coverage["covered"] and head is not None
                                           and head <= 0.3 and body <= 0.2)}


def consecutive(left, right):
    return (right["pair_index"] == left["pair_index"] + 1
            and left["current_head_log_ns"] == right["previous_head_log_ns"])


def interval(rows, kind, hal_t, head_body, reason=None):
    poses = span_pose(rows, hal_t, head_body)
    confidences = [r["motion"]["confidence"] for r in rows if kind != "unknown"]
    speeds = [r["motion"]["speed_px_s"] for r in rows if kind != "unknown"]
    directions = [math.degrees(math.atan2(r["motion"]["dy_px"], r["motion"]["dx_px"]))
                  for r in rows if kind == "motion"]
    return {
        "kind": kind, "start_s": rows[0]["start_s"], "end_s": rows[-1]["end_s"],
        "duration_s": rows[-1]["end_s"] - rows[0]["start_s"],
        "pair_indices": [r["pair_index"] for r in rows], "pair_count": len(rows),
        "min_confidence": min(confidences) if confidences else None,
        "median_speed_px_s": float(np.median(speeds)) if speeds else None,
        "image_direction_deg_per_pair": directions,
        "whole_interval_head_body": poses,
        "uncertainty_reasons": sorted(set(reason or []) | {v for r in rows for v in pair_kind(r)[1]}),
        "original_zero_label_pair_indices": [r["pair_index"] for r in rows if r["base"].get("all_zero")],
        "original_nonzero_label_pair_indices": [r["pair_index"] for r in rows if r["base"].get("any_nonzero")],
        "review_status": "proposed", "robot_direction": None, "chassis_stop_confirmed": False,
    }


def build_intervals(pairs, duration, hal_t, head_body, turn_angle):
    intervals, transitions = [], []
    run, kind = [], None
    for pair in pairs:
        current_kind, _ = pair_kind(pair)
        joins = bool(run and current_kind == kind and consecutive(run[-1], pair))
        if joins and kind != "unknown":
            joins = span_pose(run + [pair], hal_t, head_body)["diagnostic_pose_stable"]
        if run and consecutive(run[-1], pair):
            previous = run[-1]
            previous_kind, _ = pair_kind(previous)
            if previous_kind != "unknown" and current_kind != "unknown":
                # Adjacent individually stable pairs can together cross a pose change.
                combined_pose = span_pose([previous, pair], hal_t, head_body)
                difference = angle_difference(previous, pair) if previous_kind == current_kind == "motion" else None
                change = ("image_direction_change" if difference is not None and difference >= turn_angle
                          else "image_motion_appears" if previous_kind == "low_motion" and current_kind == "motion"
                          else "image_motion_becomes_low" if previous_kind == "motion" and current_kind == "low_motion"
                          else None)
                if change and combined_pose["diagnostic_pose_stable"]:
                    transitions.append({"kind": change, "start_s": previous["start_s"], "end_s": pair["end_s"],
                                        "pair_indices": [previous["pair_index"], pair["pair_index"]],
                                        "angle_difference_deg": difference,
                                        "whole_interval_head_body": combined_pose,
                                        "review_status": "proposed", "robot_phase": None,
                                        "interpretation": "image_change_envelope_not_exact_chassis_boundary"})
                    if difference is not None and difference >= turn_angle:
                        joins = False
        if run and not joins:
            intervals.append(interval(run, kind, hal_t, head_body))
            run = []
        run.append(pair)
        kind = current_kind
    if run:
        intervals.append(interval(run, kind, hal_t, head_body))
    # Make every unobserved part of the source explicit, including startup/end.
    complete, end = [], 0.0
    for item in intervals:
        if item["start_s"] > end + 1e-8:
            complete.append({"kind": "unknown", "start_s": end, "end_s": item["start_s"],
                             "duration_s": item["start_s"] - end, "pair_indices": [], "pair_count": 0,
                             "uncertainty_reasons": ["no_cached_pair_coverage"], "review_status": "proposed",
                             "robot_direction": None, "chassis_stop_confirmed": False})
        complete.append(item)
        end = item["end_s"]
    if end < duration - 1e-8:
        complete.append({"kind": "unknown", "start_s": end, "end_s": duration, "duration_s": duration - end,
                         "pair_indices": [], "pair_count": 0, "uncertainty_reasons": ["no_cached_pair_coverage"],
                         "review_status": "proposed", "robot_direction": None, "chassis_stop_confirmed": False})
    return complete, transitions


def episode_output(episode, anchor, root, output, turn_angle):
    name = episode["episode"]
    phase_path = root / "results/demo_775_phase_review/base_phase_screen/sources" / (name + ".json")
    pairs_path = phase_path.with_suffix(".pairs.jsonl")
    phase = json.loads(phase_path.read_text())
    assert sha(pairs_path) == phase["pairs_sha256"]
    verified_paths = {k: Path(phase["input_paths"][k]) for k in ("cache_npz", "cache_metadata", "signal_npz")}
    for key, path in verified_paths.items():
        assert sha(path) == phase["input_hashes"][key + "_sha256"], key
    metadata = json.loads(verified_paths["cache_metadata"].read_text())
    origin, duration = int(metadata["source_start_ns"]), int(metadata["source_duration_ns"]) / 1e9
    assert abs(duration - episode["source_duration_s"]) < 1e-8
    assert origin == int(anchor["source_start_ns"])
    with np.load(verified_paths["cache_npz"], allow_pickle=False) as cache:
        hal_t, head_body = cache["hal_log_ns"], cache["head_body_q"]
        head_t, hashes, source_indices = cache["head_log_ns"], cache["head_jpeg_sha256"], cache["head_source_indices"]
    with np.load(verified_paths["signal_npz"], allow_pickle=False) as signals:
        assert np.array_equal(hal_t, signals["hal_t_ns"])
        assert np.array_equal(head_body[:, 3:], signals["hal_q"][:, -5:])
        assert np.isfinite(signals["base_v"]).all()
    pairs = [json.loads(line) for line in pairs_path.read_text().splitlines() if line.strip()]
    last_end = -1
    for p in pairs:
        assert p["episode"] == name and p["start_s"] >= last_end - 1e-8
        assert 0 <= p["start_s"] < p["end_s"] <= duration + 1e-8
        for which in ("previous", "current"):
            idx = p[which + "_cache_frame_index"]
            assert int(head_t[idx]) == p[which + "_head_log_ns"]
            assert str(hashes[idx]) == p[which + "_jpeg_sha256"]
            assert int(source_indices[idx]) == p[which + "_source_frame_index"]
        assert abs(p["start_s"] - (p["previous_head_log_ns"] - origin) / 1e9) < 1e-9
        assert abs(p["end_s"] - (p["current_head_log_ns"] - origin) / 1e9) < 1e-9
        last_end = p["end_s"]
    intervals, transitions = build_intervals(pairs, duration, hal_t, head_body, turn_angle)
    manual = []
    for i, item in enumerate(intervals):
        item["id"] = f"interval_{i:03d}"
        if item["kind"] == "unknown":
            left = intervals[i-1]["kind"] if i else None
            right = intervals[i+1]["kind"] if i+1 < len(intervals) else None
            manual.append({"kind": "unknown_visual_interval", "start_s": item["start_s"], "end_s": item["end_s"],
                           "reason": "无法从缓存判定；核对移动/停顿与方向" if "motion" in (left, right)
                                     else "缓存没有可靠背景结论；不能补零或排除移动",
                           "priority": 1 if "motion" in (left, right) else 2,
                           "pair_indices": item["pair_indices"], "review_status": "pending"})
    for t in transitions:
        manual.append({"kind": t["kind"], "start_s": t["start_s"], "end_s": t["end_s"], "priority": 1,
                       "reason": "复核图像运动转折的真实边界与底盘阶段；包络不可直接作为标签",
                       "pair_indices": t["pair_indices"], "review_status": "pending"})
    manual.sort(key=lambda item: (item["start_s"], item["end_s"]))
    summary = {f"{k}_intervals": sum(i["kind"] == k for i in intervals) for k in KINDS}
    summary.update({f"{k}_duration_s": sum(i["duration_s"] for i in intervals if i["kind"] == k) for k in KINDS})
    summary.update(pair_count=len(pairs), turn_candidates=sum(t["kind"] == "image_direction_change" for t in transitions),
                   visual_change_envelopes=len(transitions), manual_review_items=len(manual),
                   can_auto_rebuild_six_phases=False, reviewed_physical_phase_count=0,
                   missing_requirements=["six_physical_start_stop_intervals", "robot_frame_direction_confirmation",
                                         "distance_evidence_or_explicit_weak_prior", "human_review_and_acceptance"])
    # Independent coverage accounting: each original pair appears exactly once.
    covered_indices = [idx for i in intervals for idx in i["pair_indices"]]
    assert covered_indices == [p["pair_index"] for p in pairs]
    assert abs(sum(i["duration_s"] for i in intervals) - duration) < 1e-7
    assert all(abs(a["end_s"] - b["start_s"]) < 1e-8 for a, b in zip(intervals, intervals[1:]))
    assert all(i["whole_interval_head_body"]["diagnostic_pose_stable"] for i in intervals if i["kind"] != "unknown")
    data = {"schema": "f1_base_visual_suggestions_v1", "episode": name, "id": episode["id"],
            "time_axis": "seconds_from_mcap_log_origin", "source_origin_ns": str(origin), "source_duration_s": duration,
            "review_status": "proposed", "training_accepted": False, "labels_reconstructed": False,
            "summary": summary, "intervals": intervals, "transitions": transitions, "manual_review": manual,
            "gripper_navigation_hints": javascript_safe(anchor["gripper_command_events"]),
            "gripper_interpretation": "commands_only_not_grasp_release_confirmation_or_base_phase_labels",
            "pair_evidence_file": episode["id"] + ".pairs.jsonl",
            "limitations": LIMITATIONS,
            "provenance": {"source_pairs_path": str(pairs_path), "source_pairs_sha256": sha(pairs_path),
                           "source_audit_path": str(phase_path), "source_audit_sha256": sha(phase_path),
                           "source_inputs": {key: {"path": str(path), "sha256": sha(path)} for key, path in verified_paths.items()},
                           "all_pair_time_index_and_jpeg_hashes_verified": True, "whole_run_pose_gate_applied": True}}
    save(output / (episode["id"] + ".json"), data)
    evidence_path = output / data["pair_evidence_file"]
    evidence_path.write_text("".join(json.dumps(javascript_safe(p), ensure_ascii=False, allow_nan=False) + "\n" for p in pairs))
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "results/base_relabel_v1/visual_candidates")
    parser.add_argument("--turn-angle-deg", type=float, default=35.0,
                        help="Navigation-only image direction change threshold; not a robot limit or acceptance gate.")
    args = parser.parse_args()
    if not 0 < args.turn_angle_deg < 180:
        parser.error("turn angle must lie between 0 and 180 degrees")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.root / "results/20260914/videos/base_review/manifest.json"
    anchor_path = args.root / "results/base_anchor_review_20260915/anchor_analysis.json"
    original = json.loads(manifest_path.read_text())
    anchors = {a["episode"]: a for a in json.loads(anchor_path.read_text())["episodes"]}
    results = [episode_output(e, anchors[e["episode"]], args.root, args.output, args.turn_angle_deg)
               for e in original["episodes"]]
    manifest = {"schema": "f1_base_visual_suggestions_v1", "generated_at": datetime.now(timezone.utc).isoformat(),
                "time_axis": "seconds_from_mcap_log_origin", "review_status": "proposed", "training_accepted": False,
                "settings": {"turn_angle_deg": args.turn_angle_deg, "turn_threshold_scope": "navigation_only",
                             "gap_filling": False, "fixed_phase_ratios": False, "metric_velocity_inferred": False,
                             "max_whole_interval_head_ptp_deg": 0.3, "max_whole_interval_body_ptp_deg": 0.2,
                             "max_pose_coverage_gap_s": 0.1},
                "provenance": {"script_sha256": sha(Path(__file__)), "manifest_sha256": sha(manifest_path),
                               "anchor_analysis_sha256": sha(anchor_path), "numpy_version": np.__version__,
                               "interval_reader_sha256": sha(Path(__file__).with_name("audit_base_phase_cache.py"))},
                "episodes": [{"episode": d["episode"], "id": d["id"], "data_file": d["id"] + ".json",
                              "data_sha256": sha(args.output / (d["id"] + ".json")), "summary": d["summary"]} for d in results],
                "limitations": LIMITATIONS}
    save(args.output / "manifest.json", manifest)
    aggregate = {"source_count": len(results), "pair_count": sum(d["summary"]["pair_count"] for d in results),
                 "source_duration_s": sum(d["source_duration_s"] for d in results),
                 "automatically_reconstructed_or_accepted_sources": 0,
                 "physical_phase_intervals_requiring_confirmation": 6 * len(results),
                 "timestamp_boundaries_requiring_confirmation": 12 * len(results),
                 "boundary_count_meaning": "start/end fields; physically contiguous phases may share the same time",
                 "validation": "all source hashes, pair timestamps, cached source indices/JPEG hashes, full timeline coverage and whole-run pose stability checked",
                 "episodes": manifest["episodes"]}
    for kind in KINDS:
        aggregate[kind + "_duration_s"] = sum(d["summary"][kind + "_duration_s"] for d in results)
    save(args.output / "summary.json", aggregate)
    lines = ["# 底盘阶段候选：六条真实来源", "", "这些结果是审核导航，未重建速度，未验收。时间统一为原始 MCAP 起点后的秒。",
             "", "## 原理与限制", "", "先保留每个相邻画面对的证据，再汇总连续可靠区间。未知不并入静止，不跨未知比较方向，不按录像比例划去返程。",
             "两帧之间只知道整体画面位移，因此每个转折用两对的时间包络表示；停止不能由低像素运动单独确认。",
             "连续区间还检查全区间头/身体变化，避免逐对小变化累计成明显头动。", "",
             "## 本次结果", "", "| 来源 | 可靠运动 / 秒 | 可靠低运动 / 秒 | 未知 / 秒 | 图像方向转折候选 |", "|---|---:|---:|---:|---:|"]
    for d in results:
        s = d["summary"]
        lines.append(f"| {d['episode']} | {s['motion_duration_s']:.3f} | {s['low_motion_duration_s']:.3f} | {s['unknown_duration_s']:.3f} | {s['turn_candidates']} |")
    lines += ["", "## 最少仍需人工介入的内容", "", "六条都没有足够证据自动确认六段实际移动。需逐条确认后退/横移/前进的 6 个起止区间（12 个起止字段；连续段可共享边界），独立标出停顿，并确认机器人方向与距离来源。",
              "以下短区间优先定位未知与图像转折，不能用它们替代完整六段验收；低运动区间仍须结合场景确认停稳。"]
    for d in results:
        lines += ["", "### " + d["episode"], "", "可靠背景运动片段：" + "；".join(f"{i['start_s']:.3f}–{i['end_s']:.3f}s" for i in d["intervals"] if i["kind"] == "motion") + "。", "",
                  "优先复核时间包络："]
        lines += [f"- {i['start_s']:.3f}–{i['end_s']:.3f}s：{i['reason']}。" for i in d["manual_review"] if i["priority"] == 1]
        lines += ["", "所有未知段（含非优先项）、逐对原证据及夹爪导航提示，见本来源 JSON。"]
    lines += ["", "## 验证", "", aggregate["validation"] + "。", "输入源及策略 SHA256 随 manifest 保存；原始 MCAP、原标签、既有 mask/候选名单不写入。", ""]
    (args.output / "report.md").write_text("\n".join(lines))
    print(json.dumps({k: v for k, v in aggregate.items() if k != "episodes"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
