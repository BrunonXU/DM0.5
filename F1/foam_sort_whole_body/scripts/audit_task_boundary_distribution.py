#!/usr/bin/env python3
"""Audit task boundary evidence without inferring a six/five-stage chassis route.

Read frozen numeric signals and existing head-image pair diagnostics. No video
decode, metric velocity reconstruction, training mask, or label is produced.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from propose_base_phase_intervals import pair_kind

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "f1_task_boundary_distribution_v1"
LIMITATIONS = [
    "A command returning to 100 is a close-command candidate, not verified foam contact or grasp success.",
    "The first message already at 100 does not prove that grasp started with recording.",
    "The first return to 100 can follow a small partial opening, so no 50-percent crossing is required.",
    "Final HAL pose proximity is relative to this recording's final feedback, not a calibrated home target.",
    "Image-pair motion is pixel evidence, not robot-frame direction or chassis travel distance.",
    "Low image motion is not a certified chassis stop; unknown coverage is never made zero.",
    "An unrecorded manual forward step cannot be proved or reconstructed from these recordings.",
    "Uniform terminal arm/gripper behavior cannot establish a uniform five-stage chassis path.",
    "An event selected as task completion is different from the conservative training-mask boundary.",
]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def stats(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": min(values), "p05": float(np.quantile(values, .05)),
            "median": float(np.median(values)), "p95": float(np.quantile(values, .95)), "max": max(values)}


def max_span(q):
    return float(np.max(np.ptp(q, axis=0))) if len(q) else None


def command_summary(t, q, duration, tail_s):
    """Exact 100 transitions, preserving initial closed state and every transition."""
    if not len(t) or len(t) != len(q) or not np.isfinite(q).all() or np.any(np.diff(t) < 0):
        raise ValueError("Missing/nonfinite/nonmonotonic gripper stream")
    closed = q == 100.0
    change = np.flatnonzero(np.diff(q) != 0) + 1
    starts = np.flatnonzero(closed & np.r_[False, ~closed[:-1]])
    # The first observed sample cannot count as an observed closure transition.
    starts = starts[starts > 0]
    events = []
    for i in starts:
        stop = np.flatnonzero(~closed[i:])
        j = int(i + stop[0]) if len(stop) else len(t)
        end = float(t[j]) if j < len(t) else min(duration, float(t[-1]))
        events.append({"start_s": float(t[i]), "closed_until_s": end,
                       "closed_dwell_observed_s": max(0.0, end - float(t[i])),
                       "previous_value": float(q[i - 1]), "observed_value": 100.0})
    sustained = [e for e in events if e["closed_dwell_observed_s"] >= .5]
    last_changed = float(t[change[-1]]) if len(change) else None
    tail = q[t >= tail_s]
    return {"first_sample_s": float(t[0]), "last_sample_s": float(t[-1]),
            "initial_value": float(q[0]), "final_value": float(q[-1]),
            "initial_already_100": bool(closed[0]), "all_values_100": bool(closed.all()),
            "return_to_100_events": events, "sustained_return_to_100_count": len(sustained),
            "first_sustained_return_to_100_s": sustained[0]["start_s"] if sustained else None,
            "last_sustained_return_to_100_s": sustained[-1]["start_s"] if sustained else None,
            "last_value_change_s": last_changed,
            "last_change_minus_held_start_s": last_changed - tail_s if last_changed is not None else None,
            "tail_ptp": float(np.ptp(tail)) if len(tail) else None,
            "tail_all_100": bool(len(tail) and np.all(tail == 100.0)),
            "value_changes_during_tail": int(np.count_nonzero(t[change] >= tail_s))}


def held_start(t, q):
    changed = np.flatnonzero(np.any(np.diff(q, axis=0) != 0, axis=1)) + 1
    i = int(changed[-1]) if len(changed) else 0
    assert np.array_equal(q[i:], np.broadcast_to(q[-1], q[i:].shape))
    return float(t[i]), i


def settled_to_final(t, q, threshold):
    error = np.max(np.abs(q - q[-1]), axis=1)
    suffix_max = np.maximum.accumulate(error[::-1])[::-1]
    ix = int(np.flatnonzero(suffix_max <= threshold)[0])
    return {"threshold_deg": threshold, "start_s": float(t[ix]),
            "observed_dwell_s": float(t[-1] - t[ix]),
            "meaning": "all subsequent feedback within threshold of own final sample; not home-command timing"}


def final_arm_window(t, q, duration, length):
    start = max(0., duration - length)
    lo = int(np.searchsorted(t, start, side="left"))
    # Include the causal held sample at the window start, never a future sample.
    lo = max(0, lo - 1)
    hi = int(np.searchsorted(t, duration, side="right"))
    local_t, local_q = t[lo:hi], q[lo:hi]
    first_age = max(0., start - float(local_t[0])) if len(local_t) else None
    last_age = duration - float(local_t[-1]) if len(local_t) else None
    max_gap = float(np.max(np.diff(local_t))) if len(local_t) > 1 else None
    covered = bool(len(local_t) >= 3 and local_t[0] <= start and first_age <= .1 and last_age <= .1 and max_gap <= .1)
    span_left, span_right = max_span(local_q[:, :7]), max_span(local_q[:, 7:])
    classification = ("not_covered" if not covered else "feedback_changes_over_1deg" if max(span_left, span_right) > 1. else "feedback_change_within_1deg_not_physical_stop_proof")
    return {"window_length_s": length, "sample_count_including_held": len(local_t),
            "left_max_ptp_deg": span_left, "right_max_ptp_deg": span_right,
            "held_age_at_start_s": first_age, "last_sample_age_at_source_end_s": last_age,
            "max_signal_gap_s": max_gap, "covered_with_100ms_diagnostic": covered,
            "classification": classification}


def background_window(pairs, start, end):
    seconds = Counter({k: 0.0 for k in ("motion", "low_motion", "unknown")})
    counts = Counter({k: 0 for k in ("motion", "low_motion", "unknown")})
    last_end = start
    used = []
    for pair in pairs:
        a, b = max(start, pair["start_s"]), min(end, pair["end_s"])
        if b <= a:
            continue
        if a < last_end - 1e-7:
            raise ValueError("Overlapping pair coverage")
        seconds["unknown"] += max(0.0, a - last_end)
        kind, reasons = pair_kind(pair)
        seconds[kind] += b - a
        counts[kind] += 1
        used.append((pair, kind, reasons))
        last_end = b
    seconds["unknown"] += max(0.0, end - last_end)
    assert abs(sum(seconds.values()) - (end - start)) < 1e-6
    last_reliable = next(((p, k) for p, k, _ in reversed(used) if k != "unknown"), None)
    last_motion = next((p for p, k, _ in reversed(used) if k == "motion"), None)
    return {"start_s": start, "end_s": end, "duration_s": end - start,
            "seconds": dict(seconds), "pair_counts": dict(counts),
            "last_observed_pair_kind": used[-1][1] if used else "unknown",
            "last_reliable_pair_kind": last_reliable[1] if last_reliable else None,
            "last_reliable_pair_interval_s": [last_reliable[0]["start_s"], last_reliable[0]["end_s"]] if last_reliable else None,
            "last_motion_pair_interval_s": [last_motion["start_s"], last_motion["end_s"]] if last_motion else None,
            "last_motion_to_source_end_s": end - last_motion["end_s"] if last_motion else None,
            "motion_after_window_start_observed": bool(counts["motion"]),
            "chassis_direction": None, "chassis_stop_confirmed": False}


def audit(row, mask, screen):
    episode = row["episode"]
    signal = Path(row["signal_npz_path"])
    meta_path = screen / "sources" / (episode + ".json")
    pair_path = screen / "sources" / (episode + ".pairs.jsonl")
    meta = json.loads(meta_path.read_text())
    hashes = {"signal_npz": sha(signal), "pairs_jsonl": sha(pair_path), "pair_metadata": sha(meta_path)}
    if hashes["signal_npz"] != row["signal_npz_sha256"] or hashes["signal_npz"] != meta["input_hashes"]["signal_npz_sha256"]:
        raise ValueError("Signal binding mismatch: " + episode)
    if hashes["pairs_jsonl"] != meta["pairs_sha256"]:
        raise ValueError("Pair hash mismatch: " + episode)
    pairs = rows(pair_path)
    if any(p["episode"] != episode for p in pairs):
        raise ValueError("Mixed pair source")
    origin = row["source_start_ns"]
    duration = row["source_duration_ns"] / 1e9
    with np.load(signal, allow_pickle=False) as z:
        ht = (z["hal_t_ns"] - origin) / 1e9
        lt = (z["lead_t_ns"] - origin) / 1e9
        hq, lq = z["hal_q"][:, :14], z["lead_q"]
        if not np.isfinite(hq).all() or not np.isfinite(lq).all():
            raise ValueError("Nonfinite arms")
        tail_s, lead_idx = held_start(lt, lq)
        grippers = {}
        for side in ("left", "right"):
            for kind in ("cmd", "state"):
                key = side + "_" + kind
                grippers[key] = command_summary((z[key + "_t_ns"] - origin) / 1e9, z[key], duration, tail_s)
        first_close = grippers["left_cmd"]["first_sustained_return_to_100_s"]
        before = {"boundary_s": first_close, "interpretation": "pre-close arm movement may be valid approach; not auto-trimmed"}
        for name, t, q in (("hal", ht, hq), ("lead", lt, lq)):
            v = q[t < first_close] if first_close is not None else q[:0]
            before[name + "_sample_count"] = len(v)
            before[name + "_left_max_ptp_deg"] = max_span(v[:, :7])
            before[name + "_right_max_ptp_deg"] = max_span(v[:, 7:])
        tail_q = hq[ht >= tail_s]
        final = hq[-1]
        settled = [settled_to_final(ht, hq, threshold) for threshold in (1., 2., 5.)]
        tail = {"raw_lead_held_start_s": tail_s, "raw_lead_held_duration_s": float(lt[-1] - tail_s),
                "raw_lead_held_sample_count": len(lq) - lead_idx, "raw_lead_tail_exact_constant": True,
                "conservative_mask_start_s": mask["tail_mask_start_s"],
                "hal_left_tail_max_ptp_deg": max_span(tail_q[:, :7]),
                "hal_right_tail_max_ptp_deg": max_span(tail_q[:, 7:]),
                "hal_final_deg": final.tolist(), "hal_own_final_proximity": settled,
                "final_arm_windows": {str(length): final_arm_window(ht, hq, duration, length) for length in (.5, 1.)},
                "hal_last_sample_s": float(ht[-1])}
    whole_background = background_window(pairs, 0., duration)
    final_motion = whole_background["last_motion_pair_interval_s"]
    after_last_motion = background_window(pairs, final_motion[1] if final_motion else 0., duration)
    after_last_motion["last_motion_exists"] = final_motion is not None
    after_last_motion["observability_class"] = (
        "no_reliable_motion_in_recording" if final_motion is None else
        "some_low_motion_evidence_then_or_amid_unknown" if after_last_motion["seconds"]["low_motion"] > 0 else
        "unknown_only_after_last_motion")
    return {"schema": SCHEMA, "episode": episode, "batch": episode.split("/")[0],
            "source_origin_ns": str(origin), "source_duration_s": duration, "parent_split": row["parent_split"],
            "training_approved": False, "physical_stage_count": None, "manual_forward_step_confirmed": False,
            "grippers": grippers, "before_first_close": before, "tail": tail,
            "background": {"whole_recording": whole_background,
                           "lead_held_tail": background_window(pairs, tail_s, duration),
                           "last_two_seconds": background_window(pairs, max(0., duration - 2.), duration),
                           "last_three_seconds": background_window(pairs, max(0., duration - 3.), duration),
                           "last_five_seconds": background_window(pairs, max(0., duration - 5.), duration),
                           "after_last_reliable_motion": after_last_motion,
                           "after_hal_within_1deg_of_final": background_window(pairs, settled[0]["start_s"], duration)},
            "provenance": {"signal_npz_path": str(signal), "pair_path": str(pair_path), "hashes": hashes}}


def aggregate(items):
    def count(fn):
        return sum(bool(fn(r)) for r in items)
    out = {"episodes": len(items), "first_close_s": stats(r["before_first_close"]["boundary_s"] for r in items),
           "first_left_command_s": stats(r["grippers"]["left_cmd"]["first_sample_s"] for r in items),
           "left_cmd_initial_already_100": count(lambda r: r["grippers"]["left_cmd"]["initial_already_100"]),
           "left_cmd_closed_return_count_distribution": dict(Counter(str(r["grippers"]["left_cmd"]["sustained_return_to_100_count"]) for r in items)),
           "first_close_missing": count(lambda r: r["before_first_close"]["boundary_s"] is None),
           "lead_held_duration_s": stats(r["tail"]["raw_lead_held_duration_s"] for r in items),
           "left_final_command_100": count(lambda r: r["grippers"]["left_cmd"]["final_value"] == 100),
           "left_final_feedback_100": count(lambda r: r["grippers"]["left_state"]["final_value"] == 100),
           "right_all_command_and_feedback_100": count(lambda r: all(r["grippers"][k]["all_values_100"] for k in ("right_cmd", "right_state"))),
           "left_cmd_no_change_during_held_tail": count(lambda r: r["grippers"]["left_cmd"]["value_changes_during_tail"] == 0),
           "left_feedback_no_change_during_held_tail": count(lambda r: r["grippers"]["left_state"]["value_changes_during_tail"] == 0),
           "last_left_command_change_before_held_s": stats(-r["grippers"]["left_cmd"]["last_change_minus_held_start_s"] for r in items if r["grippers"]["left_cmd"]["last_change_minus_held_start_s"] is not None),
           "hal_left_tail_span_deg": stats(r["tail"]["hal_left_tail_max_ptp_deg"] for r in items),
           "hal_right_tail_span_deg": stats(r["tail"]["hal_right_tail_max_ptp_deg"] for r in items),
           "pre_close_hal_left_span_deg": stats(r["before_first_close"]["hal_left_max_ptp_deg"] for r in items),
           "pre_close_hal_right_span_deg": stats(r["before_first_close"]["hal_right_max_ptp_deg"] for r in items),
           "pre_close_hal_left_span_over_2deg": count(lambda r: (r["before_first_close"]["hal_left_max_ptp_deg"] or 0) > 2),
           "pre_close_hal_no_samples": count(lambda r: r["before_first_close"]["hal_sample_count"] == 0),
           "pre_close_hal_right_span_over_2deg": count(lambda r: (r["before_first_close"]["hal_right_max_ptp_deg"] or 0) > 2),
           "both_hal_arms_tail_span_over_2deg": count(lambda r: r["tail"]["hal_left_tail_max_ptp_deg"] > 2 and r["tail"]["hal_right_tail_max_ptp_deg"] > 2),
           "hal_final_pose_max_deviation_from_pool_median_deg": stats(r["tail"]["hal_final_max_deviation_from_pool_median_deg"] for r in items),
           "hal_within_1deg_of_own_final_observed_dwell_s": stats(r["tail"]["hal_own_final_proximity"][0]["observed_dwell_s"] for r in items),
           "hal_final_window_classification": {str(length): dict(Counter(r["tail"]["final_arm_windows"][str(length)]["classification"] for r in items)) for length in (.5, 1.)},
           "background": {}}
    for window in items[0]["background"]:
        out["background"][window] = {"episodes_with_motion_pairs": count(lambda r: r["background"][window]["pair_counts"]["motion"] > 0),
                                    "episodes_with_low_motion_pairs": count(lambda r: r["background"][window]["pair_counts"]["low_motion"] > 0),
                                    "episodes_with_unknown_duration": count(lambda r: r["background"][window]["seconds"]["unknown"] > 1e-6),
                                    "episodes_with_more_than_half_second_motion": count(lambda r: r["background"][window]["seconds"]["motion"] > .5),
                                    "seconds": {k: sum(r["background"][window]["seconds"][k] for r in items) for k in ("motion", "low_motion", "unknown")},
                                    "last_observed_pair_kind_counts": dict(Counter(r["background"][window]["last_observed_pair_kind"] for r in items)),
                                    "last_reliable_pair_kind_counts": dict(Counter(str(r["background"][window]["last_reliable_pair_kind"]) for r in items))}
    out["after_last_motion_observability_classes"] = dict(Counter(r["background"]["after_last_reliable_motion"]["observability_class"] for r in items))
    return out


def self_checks():
    t = np.arange(5, dtype=float)
    x = command_summary(t, np.array([100., 70., 100., 100., 100.]), 5., 4.)
    assert x["first_sustained_return_to_100_s"] == 2 and x["initial_already_100"]
    assert command_summary(t, np.full(5, 100.), 5., 3.)["first_sustained_return_to_100_s"] is None
    assert command_summary(t, np.array([70., 100., 100., 0., 100.]), 5., 3.)["sustained_return_to_100_count"] == 1
    assert background_window([], 1., 5.)["seconds"] == {"motion": 0., "low_motion": 0., "unknown": 4.}
    q = np.array([[1., 1.], [2., 3.], [2., 3.], [2., 3.], [2., 3.]])
    assert held_start(t, q) == (1., 1)
    q = np.array([[0.], [9.], [0.], [1.], [0.]])
    assert settled_to_final(t, q, 1.)["start_s"] == 2.
    return {"checks": 6, "passed": True, "cases": ["initial closed does not count as close event", "constant closed is not grasp proof", "unobserved final dwell not invented", "missing images stay unknown", "raw lead suffix exactness", "final-pose proximity uses whole suffix"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--selection", type=Path, default=ROOT / "results/demo_772_selection")
    p.add_argument("--screen", type=Path, default=ROOT / "results/demo_775_phase_review/base_phase_screen")
    p.add_argument("--output", type=Path, default=ROOT / "results/task_boundary_distribution_v1")
    args = p.parse_args()
    checks = self_checks()
    if args.output.exists():
        raise ValueError("Refuse to overwrite results; use a new output directory")
    candidates = args.selection / "candidates.jsonl"
    masks = args.selection / "mask_manifest.jsonl"
    source_hashes = {"candidates_sha256": sha(candidates), "mask_manifest_sha256": sha(masks)}
    selected, by_mask = rows(candidates), {r["episode"]: r for r in rows(masks)}
    if len(selected) != len({r["episode"] for r in selected}) or set(by_mask) != {r["episode"] for r in selected}:
        raise ValueError("Selection identity mismatch")
    output = []
    for i, row in enumerate(selected):
        output.append(audit(row, by_mask[row["episode"]], args.screen))
        if (i + 1) % 100 == 0:
            print(json.dumps({"episodes_completed": i + 1}), flush=True)
    median = np.median([r["tail"]["hal_final_deg"] for r in output], axis=0)
    for r in output:
        r["tail"]["hal_final_max_deviation_from_pool_median_deg"] = float(np.max(np.abs(np.asarray(r["tail"]["hal_final_deg"]) - median)))
    assert source_hashes == {"candidates_sha256": sha(candidates), "mask_manifest_sha256": sha(masks)}
    summary = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
               "training_approved": False, "selection_unchanged": True, "provenance": source_hashes,
               "script_sha256": sha(__file__), "pair_classifier_script_sha256": sha(ROOT / "scripts/propose_base_phase_intervals.py"),
               "route_distance_user_confirmation": {"forward_backward_m": .5, "lateral_m": 1.6, "meaning": "user-confirmed real route distances; not used to infer unobserved phases"},
               "definitions": {"close_command": "enter exact100 after a recorded non100 value, followed by >=0.5s observed closed platform; no initial sample event",
                               "held_tail": "raw all14 lead exactly equal to final command through end", "final_pose": "recording's own last HAL14 sample", "thresholds": "1/2/5deg only sensitivity diagnostics, not robot acceptance limits",
                               "two_three_five_second_windows": "descriptive trailing diagnostic windows, not task boundary or mask"},
               "overall": aggregate(output), "by_batch": {batch: aggregate([r for r in output if r["batch"] == batch]) for batch in sorted({r["batch"] for r in output})},
               "hal_pool_median_final_deg": median.tolist(), "limitations": LIMITATIONS}
    reps = {"reference_000057": [r for r in output if r["episode"] == "all_data_3_derived/episode_000057"],
            "largest_pre_close_arm_movement": sorted(output, key=lambda r: r["before_first_close"]["hal_left_max_ptp_deg"] or 0, reverse=True)[:8],
            "largest_final_pose_difference": sorted(output, key=lambda r: r["tail"]["hal_final_max_deviation_from_pool_median_deg"], reverse=True)[:8],
            "tail_has_image_motion": sorted(output, key=lambda r: r["background"]["lead_held_tail"]["seconds"]["motion"], reverse=True)[:8],
            "left_gripper_changes_during_held_tail": [r for r in output if r["grippers"]["left_cmd"]["value_changes_during_tail"] > 0],
            "close_event_count_not_two": [r for r in output if r["grippers"]["left_cmd"]["sustained_return_to_100_count"] != 2]}
    args.output.mkdir(parents=True)
    (args.output / "episodes.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in output))
    save(args.output / "summary.json", summary)
    save(args.output / "representatives.json", reps)
    save(args.output / "validation.json", {"self_checks": checks, "episodes": len(output), "all_signal_and_pair_hashes_verified": True,
                                         "all_window_duration_sums_checked": True, "source_selection_and_mask_unchanged": source_hashes,
                                         "no_old_base_velocity_values_loaded": True, "no_training_acceptance": True})
    a = summary["overall"]
    lines = ["# 任务起止分布：数值与已有背景证据", "", "2026-09-15。只读当前候选信号与已有图像对缓存；原数据、候选、mask和标签未改。", "",
             "**可检查共同的夹爪/双臂起止形态；这些证据不足以把全体直接判成五段底盘运动，也不能证明录制外人工前进一步。**", "",
             "## 方法与可复现入口", "", "闭合候选取已观测非100→100、随后持续至少0.5秒的命令平台，避免把录制开始已经闭合误作一次抓取。100含义依接口清单为闭合。命令事件不等于物体接触或成功。", "",
             "双臂末端以原lead14维最后一个严格恒定后缀核对HAL变化；到位候选只表示接近本条最后HAL样本，不等于已知回零目标。背景完全复用原半秒图像对诊断，不读取旧六段速度来判运动方向。", "", "```bash",
             "cd /home/user/projects/F1/foam_sort_whole_body",
             "/tmp/f1-foam-audit-venv/bin/python scripts/audit_task_boundary_distribution.py --output results/task_boundary_distribution_v2",
             "```", "", "输出目录必须不存在；每次结果绑定脚本、选择集、mask清单、每条信号和背景pair SHA256。", "",
             "## 全体统计", "", f"- 检查 {a['episodes']} 份；首次持续闭合候选缺失 {a['first_close_missing']} 份。",
             f"- 持续闭合候选次数分布：`{a['left_cmd_closed_return_count_distribution']}`。次数不是完整任务循环数。",
             f"- 首次持续闭合候选时间中位 {a['first_close_s']['median']:.3f}s；其前左臂HAL活动超过2°：{a['pre_close_hal_left_span_over_2deg']}份，右臂：{a['pre_close_hal_right_span_over_2deg']}份。不能从夹完那一刻统一精裁，否则丢掉接近/对准示教。",
             f"- 首次闭合前HAL没有样本覆盖：{a['pre_close_hal_no_samples']}份，不能把缺测解释为夹前手臂静止。",
             f"- 原lead末端严格恒定时长中位 {a['lead_held_duration_s']['median']:.3f}s；这段区间中双臂HAL跨度各超过2°：{a['both_hal_arms_tail_span_over_2deg']}份。这不是说录像最后时刻双臂仍在移动。",
             f"- 左夹爪最终命令100：{a['left_final_command_100']}份；最终反馈100：{a['left_final_feedback_100']}份；右夹爪命令反馈全程100：{a['right_all_command_and_feedback_100']}份。",
             f"- lead恒定尾段中左夹爪命令不再变化：{a['left_cmd_no_change_during_held_tail']}份；反馈不再变化：{a['left_feedback_no_change_during_held_tail']}份。末次左夹爪命令变化比lead恒定起点早的时间中位 {a['last_left_command_change_before_held_s']['median']:.3f}s。‘夹爪恢复’与‘双臂复位完成’不能当同一时间戳。", "",
             f"- 末0.5秒HAL可观察性与变化：`{a['hal_final_window_classification']['0.5']}`；末1秒：`{a['hal_final_window_classification']['1.0']}`。1°是诊断尺度，不是已验收的到位容差。",
             f"- 连续接近本条最终HAL姿态至最后样本（全14维各距最终值≤1°）的观测停留中位 {a['hal_within_1deg_of_own_final_observed_dwell_s']['median']:.3f}s。", "",
             "| 批次 | 份数 | 首次闭合前左臂>2° | 双臂尾部各>2° | 尾部夹爪命令不变 | 尾部有可靠图像运动 |", "|---|---:|---:|---:|---:|---:|"]
    for batch, b in summary["by_batch"].items():
        lines.append(f"| {batch} | {b['episodes']} | {b['pre_close_hal_left_span_over_2deg']} | {b['both_hal_arms_tail_span_over_2deg']} | {b['left_cmd_no_change_during_held_tail']} | {b['background']['lead_held_tail']['episodes_with_motion_pairs']} |")
    lines += ["", "## 底盘末段的证据边界", "", "| 观察区间 | 有motion证据的份数 | 有low-motion证据的份数 | 有unknown覆盖的份数 | motion/low-motion/unknown总秒数 |", "|---|---:|---:|---:|---|"]
    for window, b in a["background"].items():
        s = b["seconds"]
        lines.append(f"| {window} | {b['episodes_with_motion_pairs']} | {b['episodes_with_low_motion_pairs']} | {b['episodes_with_unknown_duration']} | {s['motion']:.3f} / {s['low_motion']:.3f} / {s['unknown']:.3f} |")
    lines += ["", "同一份可同时有三类区间，因此各类份数不能相加。末端未采样尾隙也保持unknown。低图像运动不是底盘停车，可靠图像运动也可能受未测底座俯仰/相机挠动影响，不能自动归类向前。",
              "", f"最后一个可靠motion图像对之后的可观察性：`{a['after_last_motion_observability_classes']}`。部分low-motion证据不会覆盖旁边unknown，也不是确认停止。这两个类别仅回答‘末尾还有没有可观察证据’，不回答‘有没有第六段’。", "",
              "## 代表与验收", "", "[逐条结果](episodes.jsonl)、[按批完整统计](summary.json)、[异常及边界代表](representatives.json)、[验证](validation.json)。", "",
              "建议保留首次夹取前的真实接近过程；任务目标可以定义到横移返回后双臂/夹爪恢复，但需按逐条画面确认终点及底盘停留。若后续前进一步发生在录制外，应明确由人工执行，不能给当前录制补一段第六阶段。", "",
              "还需逐条或经验证的分组规则建立真实底盘阶段，尤其是最后左移/双臂复位是否重叠、是否有额外前进。772份的方向和五阶段分布均尚未由此审计确认；本结果不改变完整端到端训练验收状态。", "", "## 限制", ""]
    lines += ["- " + line for line in LIMITATIONS]
    (args.output / "report.md").write_text("\n".join(lines) + "\n")
    save(args.output / "manifest.json", {"schema": SCHEMA, "files": {p.name: sha(p) for p in args.output.iterdir() if p.is_file()}, "training_approved": False})
    print(json.dumps({"episodes": len(output), "output": str(args.output), "overall": a}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
