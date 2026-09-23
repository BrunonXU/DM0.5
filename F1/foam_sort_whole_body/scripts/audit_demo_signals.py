#!/usr/bin/env python3
"""Read-only numeric/weak-base diagnostics for the approved demo source manifest.

Review thresholds below are diagnostic search criteria, never robot limits or
automatic exclusion rules. Inputs are pinned v1 packs plus original small JSON;
no raw MCAP rewrite, clipping, relabeling, training or robot execution occurs.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

TASK = Path(__file__).resolve().parents[1]
STREAMS = {"hal": ("hal_q", 19), "lead": ("lead_q", 14),
           "left_cmd": ("left_cmd", 1), "right_cmd": ("right_cmd", 1),
           "left_state": ("left_state", 1), "right_state": ("right_state", 1),
           "base": ("base_v", 3)}
ARM_NAMES = [f"{side}_j{i}" for side in ("left", "right") for i in range(1, 8)]
STATE_NAMES = ARM_NAMES + ["left_gripper", "right_gripper"] + [f"body_j{i}" for i in range(1, 6)]
ACTION_NAMES = ARM_NAMES + ["left_gripper", "right_gripper", "vx", "vy", "wz"]
LOW = np.array([-178, -120, -178, -145, -178, -60, -90] * 2)
HIGH = np.array([178, 120, 178, 60, 178, 60, 90] * 2)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def stats(values):
    values = np.asarray(values)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"count": 0}
    return dict(count=int(len(values)), min=float(values.min()), p50=float(np.median(values)),
                p95=float(np.quantile(values, .95)), p99=float(np.quantile(values, .99)), max=float(values.max()))


def intervals(mask, times, origin):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False]))
    return [{"first_sample_s": float((times[a] - origin) / 1e9),
             "last_sample_s": float((times[b - 1] - origin) / 1e9),
             "samples": int(b - a)} for a, b in zip(edges[::2], edges[1::2])]


def top_changes(times, values, origin, valid_pairs=None, count=5):
    delta = np.diff(values, axis=0)
    dt = np.diff(times) / 1e9
    ok = (dt > 0) & (dt <= .1) & np.isfinite(delta).all(axis=1)
    if valid_pairs is not None:
        ok &= valid_pairs
    ids = np.flatnonzero(ok)
    if not len(ids):
        return {"max_step": 0.0, "max_rate": 0.0, "steps_gt_10": 0, "rate_gt_300": 0, "top": []}
    magnitude = np.max(np.abs(delta[ids]), axis=1)
    rate = magnitude / dt[ids]
    top = ids[np.argsort(magnitude)[-count:][::-1]]
    return {"max_step": float(magnitude.max()), "max_rate": float(rate.max()),
            "steps_gt_10": int((magnitude > 10).sum()), "rate_gt_300": int((rate > 300).sum()),
            "top": [{"before_s": float((times[i] - origin) / 1e9),
                     "after_s": float((times[i + 1] - origin) / 1e9),
                     "column": int(np.abs(delta[i]).argmax()),
                     "step": float(np.abs(delta[i]).max()), "dt_s": float(dt[i])} for i in top]}


def template_velocity(t, segments, offset=0.0):
    """Candidate cubic smoothstep derivative inferred from final numeric data."""
    out = np.zeros((len(t), 3), dtype=np.float64)
    for a, b, x0, y0, x1, y1, _ in segments:
        selected = (t >= a + offset) & (t <= b + offset)
        u = (t[selected] - a - offset) / (b - a)
        out[selected, :2] = 6 * u[:, None] * (1 - u[:, None]) * np.array([x1 - x0, y1 - y0]) / (b - a)
    return out


def infer_template_offset(t, velocity, segments):
    # The first moving sample in the first segment lies on its rising half at
    # these observed sampling rates. Check both roots and keep full-pack best
    # fit; failed fits stay diagnostic mismatches, never rewritten labels.
    a, b, x0, y0, x1, y1, _ = segments[0]
    displacement = np.array([x1 - x0, y1 - y0])
    axis = int(np.abs(displacement).argmax())
    ids = np.flatnonzero(np.abs(velocity[:, axis]) > 1e-10)
    candidates = [0.0]
    if len(ids) and displacement[axis] != 0:
        first = ids[0]
        ratio = velocity[first, axis] * (b - a) / displacement[axis]
        discriminant = 1 - 2 * ratio / 3
        if discriminant >= -1e-10:
            root = np.sqrt(max(discriminant, 0.0))
            for u in ((1 - root) / 2, (1 + root) / 2):
                candidates.append(float(t[first] - a - u * (b - a)))
    tested = [(float(np.max(np.abs(template_velocity(t, segments, offset) - velocity))), offset)
              for offset in candidates]
    error, offset = min(tested)
    return offset, error, tested[0][0]


def audit_one(row, v1, raw):
    ep = row["episode"]
    ap = v1 / "aligned" / (ep + ".npz")
    sp = v1 / "signals" / (ep + ".npz")
    sj = sp.with_suffix(".json")
    require(digest(ap) == row["aligned_npz_sha256"], "aligned SHA differs from source catalog")
    require(digest(ap.with_suffix(".json")) == row["aligned_metadata_sha256"], "aligned metadata changed")
    sm, am = read(sj), read(ap.with_suffix(".json"))
    require(sm["status"] == "complete" and sm["episode"] == ep == am["episode"], "identity/commit mismatch")
    before = sp.stat()
    require(sm["npz_fingerprint"] == {"size": before.st_size, "mtime_ns": before.st_mtime_ns}, "signal fingerprint changed")
    source = raw / row["source_relative_path"]
    st = source.stat()
    require(row["source_fingerprint"] == {"size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns}, "raw source fingerprint changed")
    weak_path = source.parent / "estimated_move_state.json"
    weak_bytes = weak_path.read_bytes()
    weak = json.loads(weak_bytes)
    origin = row["source_start_ns"]
    require(origin == sm["start_ns"] and row["parent_split"] == am["split"], "origin/split mismatch")
    result = {"episode": ep, "split": row["parent_split"], "duration_s": row["source_duration_ns"] / 1e9,
              "frames": row["frames"], "input_hashes": {"aligned_npz_sha256": row["aligned_npz_sha256"],
                    "signal_npz_sha256": digest(sp), "signal_metadata_sha256": digest(sj),
                    "weak_json_sha256": hashlib.sha256(weak_bytes).hexdigest()},
              "diagnostic_flags": [], "streams": {}}
    flags = result["diagnostic_flags"]
    with np.load(sp, allow_pickle=False) as s, np.load(ap, allow_pickle=False) as a:
        for name, (key, width) in STREAMS.items():
            values = s[key]
            times = s[name + "_t_ns"]
            values = values[:, None] if values.ndim == 1 else values
            require(values.shape == (len(times), width) and len(times) > 0, f"{name} unexpected shape")
            dt = np.diff(times) / 1e9
            gaps = np.flatnonzero(dt > .1)
            stream = {"count": len(times), "nonfinite": int((~np.isfinite(values)).sum()),
                      "nonincreasing_steps": int((dt <= 0).sum()),
                      "dt_s": stats(dt), "gaps_gt_100ms": int(len(gaps)), "gaps_gt_1s": int((dt > 1).sum()),
                      "first_s": float((times[0] - origin) / 1e9),
                      "last_s": float((times[-1] - origin) / 1e9),
                      "min": np.min(values, axis=0).tolist(), "max": np.max(values, axis=0).tolist(),
                      "span": np.ptp(values, axis=0).tolist(),
                      "gaps": [{"before_s": float((times[i] - origin) / 1e9),
                                "after_s": float((times[i + 1] - origin) / 1e9),
                                "duration_s": float(dt[i]),
                                "value_step": float(np.abs(values[i + 1] - values[i]).max())} for i in gaps]}
            require(stream["nonfinite"] == 0, f"{name} nonfinite raw numeric signal")
            require(stream["nonincreasing_steps"] == 0, f"{name} duplicate/backward timestamps")
            if name in ("lead", "hal"):
                q = values[:, :14]
                excess = np.maximum(np.maximum(LOW - q, q - HIGH), 0)
                stream["document_range_excess_max"] = float(excess.max())
                stream["document_range_points"] = int((excess > 0).sum())
                stream["document_range_columns"] = [ARM_NAMES[i] for i in np.flatnonzero((excess > 0).any(axis=0))]
                stream["changes"] = top_changes(times, q, origin)
                if stream["changes"]["steps_gt_10"]:
                    flags.append(f"{name}_step_gt_10deg_within_100ms_diagnostic")
                if excess.max() > .01:
                    flags.append(f"{name}_document_range_excess_gt_0.01deg_diagnostic")
            elif name not in ("base",):
                require(np.all((values >= 0) & (values <= 100)), f"{name} out of gripper 0..100")
            if len(gaps):
                flags.append(f"{name}_message_gap_gt_100ms")
            result["streams"][name] = stream

        state, action = a["state"], a["action"]
        sv, av = a["state_valid"], a["action_candidate_valid"]
        n = len(a["time_ns"])
        require(state.shape == sv.shape == (n, 21) and action.shape == av.shape == (n, 19), "aligned shape mismatch")
        require(np.isfinite(state[sv]).all() and np.isfinite(action[av]).all(), "nonfinite value under true mask")
        require(not bool(a["training_approved"]), "candidate unexpectedly approved")
        joint = a["joint_frame_candidate_valid"]
        require(np.array_equal(joint, sv.all(axis=1) & av.all(axis=1) & a["camera_timestamp_candidate_valid"].all(axis=1)), "joint mask mismatch")
        future = sv.all(axis=1) & av.all(axis=1)
        h = row["chunk_horizon"]
        expected = np.zeros(n, dtype=bool)
        sums = np.r_[0, np.cumsum(future, dtype=np.int64)]
        if n >= h:
            expected[:n-h+1] = (sums[h:] - sums[:-h]) == h
        expected &= a["camera_timestamp_candidate_valid"].all(axis=1)
        require(np.array_equal(expected, a["chunk_start_candidate_valid"]), "chunk crosses invalid numeric frames or tail")
        result["validity"] = {"joint_frames": int(joint.sum()), "numeric_frames": int(future.sum()),
                              "chunk_starts": int(expected.sum()), "invalid_numeric_frames": int((~future).sum()),
                              "initial_numeric_invalid_frames": int(np.flatnonzero(future)[0]) if future.any() else n,
                              "interior_numeric_invalid_frames": int((~future[np.flatnonzero(future)[0]:np.flatnonzero(future)[-1]+1]).sum()) if future.any() else n,
                              "tail_frames_without_full_chunk": min(h - 1, n)}
        require(expected.any(), "candidate has no valid chunk")
        # Causal contemporaneous command-feedback difference; not an actuator
        # tracking error measurement, since topics lack execution ACK matching.
        pairs = sv[:, :14] & av[:, :14]
        difference = np.where(pairs, np.abs(action[:, :14] - state[:, :14]), np.nan)
        rows_valid = pairs.all(axis=1)
        maximum = np.max(difference[rows_valid], axis=1)
        maximum_grid = np.full(n, np.nan)
        maximum_grid[rows_valid] = maximum
        worst = int(np.nanargmax(maximum_grid))
        result["lead_hal_difference"] = {"meaning": "same_log_grid_causal_samples_not_execution_ack_tracking_error",
            "absolute_deg": stats(difference), "per_joint_p99_deg": np.nanquantile(difference, .99, axis=0).tolist(),
            "per_joint_max_deg": np.nanmax(difference, axis=0).tolist(),
            "frames_any_joint_gt_20deg": int((maximum > 20).sum()),
            "worst_s": float((a["time_ns"][worst] - origin) / 1e9),
            "worst_joint": ARM_NAMES[int(np.nanargmax(difference[worst]))],
            "intervals_gt_20deg": intervals(maximum_grid > 20, a["time_ns"], origin)}
        if (maximum > 20).any():
            flags.append("lead_hal_same_time_difference_gt_20deg_diagnostic")
        result["body_span_raw_units"] = np.ptp(s["hal_q"][:, 14:19], axis=0).tolist()
        result["state_min"] = np.nanmin(np.where(sv, state, np.nan), axis=0).tolist()
        result["state_max"] = np.nanmax(np.where(sv, state, np.nan), axis=0).tolist()
        result["action_min"] = np.nanmin(np.where(av, action, np.nan), axis=0).tolist()
        result["action_max"] = np.nanmax(np.where(av, action, np.nan), axis=0).tolist()

        segments = weak["motion_segments"]
        require(len(segments) == 6 and all(len(x) == 7 for x in segments), "weak template segment layout mismatch")
        require(all(np.isfinite(x[:6]).all() and x[1] > x[0] for x in segments), "invalid template numbers")
        require(all(a[1] <= b[0] + 1e-9 for a, b in zip(segments, segments[1:])), "template segments overlap")
        require(weak["samples"] == len(s["base_t_ns"]), "JSON/base sample count mismatch")
        t = (s["base_t_ns"] - origin) / 1e9
        v = s["base_v"]
        speed = np.linalg.norm(v[:, :2], axis=1)
        offset, error, unshifted_error = infer_template_offset(t, v, segments)
        moving = speed > 1e-9
        active_axes = (np.abs(v) > 1e-9).sum(axis=1)
        dt = np.diff(t)
        acc = np.linalg.norm(np.diff(v[:, :2], axis=0), axis=1) / dt
        integration = np.sum((v[1:, :2] + v[:-1, :2]) * dt[:, None] / 2, axis=0)
        integral_path = float(np.sum((speed[1:] + speed[:-1]) * dt / 2))
        base = {"label_origin": weak.get("status"), "method": weak["detected_windows"]["method"],
                "confidence": weak["detected_windows"]["confidence"],
                "assumptions": weak["assumptions"], "detected_windows_json": weak["detected_windows"],
                "base_times_equal_hal_times": bool(np.array_equal(s["base_t_ns"], s["hal_t_ns"])),
                "peak_xy_speed_mps": float(speed.max()), "peak_time_s": float(t[int(speed.argmax())]),
                "speed_distribution_mps": stats(speed), "moving_fraction": float(moving.mean()),
                "first_velocity": v[0].tolist(), "last_velocity": v[-1].tolist(),
                "last_aligned_velocity": action[-1, 16:19].tolist(),
                "last_numeric_valid_velocity": action[np.flatnonzero(future)[-1], 16:19].tolist(),
                "moving_intervals": intervals(moving, s["base_t_ns"], origin),
                "simultaneous_active_axes_samples": int((active_axes > 1).sum()),
                "max_wz": float(np.abs(v[:, 2]).max()),
                "max_adjacent_xy_velocity_change_mps": float(np.linalg.norm(np.diff(v[:, :2], axis=0), axis=1).max()),
                "label_numerical_acceleration_mps2": stats(acc),
                "sample_integral_xy_m": integration.tolist(), "sample_integral_path_m": integral_path,
                "integral_interpretation": "numerical integral of weak labels, NOT measured travel or odometry",
                "json_template_path_m": float(sum(np.hypot(x[4]-x[2], x[5]-x[3]) for x in segments)),
                "smoothstep_derivative_inference": {"best_constant_time_offset_s": offset,
                    "zero_offset_max_abs_difference_mps": unshifted_error,
                    "best_max_abs_difference_mps": error,
                    "matches_within_1e_6_mps": error < 1e-6,
                    "meaning": "numerical fit of final labels to JSON geometry; no evidence of measured speed/exposure alignment"}}
        if "fallback" in base["method"]:
            flags.append("weak_base_window_fallback")
        if base["peak_xy_speed_mps"] > 2:
            flags.append("weak_base_peak_gt_2mps_diagnostic_not_hardware_limit")
        if np.linalg.norm(v[-1, :2]) > .01:
            flags.append("recording_ends_with_nonzero_weak_base_velocity_gt_0.01mps")
        if np.linalg.norm(v[0, :2]) > .01:
            flags.append("recording_starts_with_nonzero_weak_base_velocity_gt_0.01mps")
        if error >= 1e-6:
            flags.append("weak_base_numeric_template_mismatch_after_constant_offset_fit")
        if abs(offset) > .001:
            flags.append("weak_base_json_time_origin_differs_from_final_mcap_start_gt_1ms")
        if np.linalg.norm(integration) > .05:
            flags.append("weak_label_sample_integral_closure_over_5cm_diagnostic")
        result["base"] = base
    after = sp.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "signal pack changed during read")
    require(digest(ap) == row["aligned_npz_sha256"] and weak_path.read_bytes() == weak_bytes, "input changed during read")
    result["status"] = "numeric_integrity_pass_diagnostics_not_acceptance"
    return result


def summarize(rows, errors, args, candidates_hash):
    counts = Counter(flag for row in rows for flag in row["diagnostic_flags"])
    fields = {}
    for name, names in (("state", STATE_NAMES), ("action", ACTION_NAMES)):
        lo = np.min([r[name + "_min"] for r in rows], axis=0)
        hi = np.max([r[name + "_max"] for r in rows], axis=0)
        fields[name] = [{"column": i, "name": key, "min": float(lo[i]), "max": float(hi[i]),
                         "constant_exact": bool(lo[i] == hi[i])} for i, key in enumerate(names)]
    def top(key, n=12):
        return [{"episode": r["episode"], "value": key(r)} for r in sorted(rows, key=key, reverse=True)[:n]]
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "kind": "demo788_numeric_diagnostics_not_robot_or_task_acceptance",
           "candidate_manifest": str(args.candidates.resolve()), "candidate_sha256": candidates_hash,
           "script_sha256": digest(Path(__file__)), "candidate_count": len(rows) + len(errors),
           "numeric_integrity_pass_count": len(rows), "integrity_errors": errors,
           "split_counts": dict(Counter(r["split"] for r in rows)),
           "total_frames": sum(r["frames"] for r in rows),
           "total_duration_s": sum(r["duration_s"] for r in rows),
           "validity_totals": {k: sum(r["validity"][k] for r in rows) for k in rows[0]["validity"]},
           "diagnostic_source_counts": dict(counts),
           "diagnostic_sources": {flag: [r["episode"] for r in rows if flag in r["diagnostic_flags"]] for flag in counts},
           "all_data_extrema_not_training_normalization": fields,
           "body_per_source_span_raw_units": [stats([r["body_span_raw_units"][i] for r in rows]) for i in range(5)],
           "stream_counts": {name: {"samples": sum(r["streams"][name]["count"] for r in rows),
                "gaps_gt_100ms": sum(r["streams"][name]["gaps_gt_100ms"] for r in rows),
                "gaps_gt_1s": sum(r["streams"][name]["gaps_gt_1s"] for r in rows),
                "source_count_with_gaps_gt_100ms": sum(r["streams"][name]["gaps_gt_100ms"] > 0 for r in rows),
                "global_max_gap_s": max(r["streams"][name]["dt_s"]["max"] for r in rows)} for name in STREAMS},
           "weak_base_methods": dict(Counter(r["base"]["method"] for r in rows)),
           "weak_base_peak_speed_mps": stats([r["base"]["peak_xy_speed_mps"] for r in rows]),
           "weak_base_peak_numerical_acceleration_mps2": stats([r["base"]["label_numerical_acceleration_mps2"]["max"] for r in rows]),
           "weak_base_time_origin_offset_s": stats([r["base"]["smoothstep_derivative_inference"]["best_constant_time_offset_s"] for r in rows]),
           "weak_base_template_fit_max_error_mps": max(r["base"]["smoothstep_derivative_inference"]["best_max_abs_difference_mps"] for r in rows),
           "top_peak_weak_speeds": top(lambda r: r["base"]["peak_xy_speed_mps"]),
           "top_weak_time_origin_offsets_abs_s": top(lambda r: abs(r["base"]["smoothstep_derivative_inference"]["best_constant_time_offset_s"])),
           "top_lead_hal_differences_deg": top(lambda r: r["lead_hal_difference"]["absolute_deg"]["max"]),
           "top_lead_steps_deg": top(lambda r: r["streams"]["lead"]["changes"]["max_step"]),
           "top_hal_steps_deg": top(lambda r: r["streams"]["hal"]["changes"]["max_step"]),
           "training_exclusions_added": [], "training_approved": False,
           "raw_or_v1_modified": False, "training_or_robot_execution_started": False,
           "limitations": ["No full visual review, task success/cycle counting, or physical clock calibration.",
                "No MCAP payload reread; uses prior decoded signal packs, verifies pinned aligned hashes and source fingerprints.",
                "Thresholds (10deg step, 20deg command-feedback difference, 2m/s weak peak, 5cm integral closure) are diagnostic criteria, not hardware limits or automatic rejection.",
                "Estimated base speeds, accelerations and integrals are synthetic-label properties, not physical robot measurements.",
                "Extrema include both source splits for auditing; they are not final normalization statistics."]}
    return out


def report(summary, path):
    s = summary
    flags = s["diagnostic_source_counts"]
    lines = ["# 788候选：数值与后补底盘只读复核", "",
        f"本次逐条读取 {s['candidate_count']} 份候选的原始信号提取包、对齐包和后补标签JSON；数值完整性通过 {s['numeric_integrity_pass_count']}，错误 {len(s['integrity_errors'])}。",
        "未改原始文件、v1 mask、任何标签或训练清单；诊断阈值不作为硬件限位或自动剔除条件。", "",
        f"共 {s['total_frames']:,} 个30Hz格点、{s['validity_totals']['joint_frames']:,} 个当前三路图像/数值联合有效帧、{s['validity_totals']['chunk_starts']:,} 个50步候选起点。",
        "有限值、21D/19D形状、夹爪0–100、各数值流时间严格递增、来源SHA/指纹、动作chunk不跨无效数值/录制尾部均重新检查。", "",
        "| 诊断项（不是自动拒收） | 涉及来源数 |", "| --- | ---: |"]
    lines += [f"| `{flag}` | {count} |" for flag, count in flags.items()]
    lines += ["", "## 底盘标签", "",
        f"实际存储的弱速度峰值：中位 {s['weak_base_peak_speed_mps']['p50']:.3f} m/s，最大 {s['weak_base_peak_speed_mps']['max']:.3f} m/s。2 m/s仅为复核线索，实际F1速度限制未由本次检查确认。",
        f"对最终base_v拟合JSON六段几何的三次平滑曲线导数（允许一个固定时间原点差），最大逐值残差 {s['weak_base_template_fit_max_error_mps']:.3g} m/s。这里只证明内部数值关系，不能证明画面移动与这些弱标签相符。",
        f"拟合的JSON时间原点相对最终MCAP起点差：范围 [{s['weak_base_time_origin_offset_s']['min']:.6f}, {s['weak_base_time_origin_offset_s']['max']:.6f}] 秒。正值表示标签曲线在最终MCAP时间轴上更晚出现；未调整时间轴。",
        "速度积分只用于发现弱模板闭合/采样不一致，不能叫实测里程。末尾非零速度必须保持尾部mask，不能重复末帧作为真实未来动作，也不能直接无限保持最后指令。", "",
        "## 关节与身体", "",
        "双臂数值同文档度数范围对照，目标/反馈的同格点差值受到异步取样、跟随滞后、饱和和其他控制路径影响，不是逐条执行ACK。大差值需回到具体时间定位，不能直接裁剪。",
        "body5保持原始单位，列跨度统计见summary；小范围抖动不是身体策略动作。右夹爪和wz全局常量维的极值也记录在summary，后续归一化必须正确处理常量。", "",
        "逐条证据：[episodes.jsonl](episodes.jsonl)、[episodes.csv](episodes.csv)；汇总和精确来源名单：[summary.json](summary.json)。",
        "本检查不能授予任务成功、图像曝光同步、真实速度恢复或机器人可执行性验收。"]
    path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=TASK / "results/demo_788_check/candidates.jsonl")
    parser.add_argument("--v1", type=Path, default=TASK / "results/cleaning_v1")
    parser.add_argument("--raw", type=Path, default=Path("/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw"))
    parser.add_argument("--output", type=Path, default=TASK / "results/demo_788_check/signals")
    parser.add_argument("--expected-count", type=int, default=788)
    args = parser.parse_args()
    require(not args.output.exists(), "Output already exists; choose a new run directory")
    require(args.raw.resolve() not in args.output.resolve().parents and args.v1.resolve() not in args.output.resolve().parents,
            "Output must not be inside source or v1")
    data = args.candidates.read_bytes()
    candidates_hash = hashlib.sha256(data).hexdigest()
    candidates = [json.loads(line) for line in data.splitlines() if line.strip()]
    require(len(candidates) == len({r['episode'] for r in candidates}) == args.expected_count, "Candidate count/uniqueness mismatch")
    args.output.mkdir(parents=True)
    rows, errors = [], []
    with (args.output / "episodes.jsonl").open("w") as handle:
        for index, row in enumerate(candidates, 1):
            try:
                result = audit_one(row, args.v1, args.raw)
                handle.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                rows.append(result)
            except Exception as exc:
                errors.append({"episode": row['episode'], "error": f"{type(exc).__name__}: {exc}"})
            if index % 100 == 0 or index == len(candidates):
                print(json.dumps({"done": index, "pass": len(rows), "errors": len(errors)}), flush=True)
    require(args.candidates.read_bytes() == data, "Candidate manifest changed during read")
    if not rows:
        write(args.output / "summary.json", {"integrity_errors": errors})
        raise RuntimeError("All sources failed; inspect summary")
    summary = summarize(rows, errors, args, candidates_hash)
    write(args.output / "summary.json", summary)
    with (args.output / "episodes.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["episode", "split", "duration_s", "frames", "joint_frames", "chunk_starts", "peak_weak_base_mps",
                  "weak_base_method", "weak_base_origin_offset_s", "weak_base_template_max_error", "last_weak_base_speed_mps",
                  "max_lead_step_deg", "max_hal_step_deg", "max_lead_hal_difference_deg", "diagnostic_flags"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({"episode": r["episode"], "split": r["split"], "duration_s": r["duration_s"], "frames": r["frames"],
                "joint_frames": r["validity"]["joint_frames"], "chunk_starts": r["validity"]["chunk_starts"],
                "peak_weak_base_mps": r["base"]["peak_xy_speed_mps"], "weak_base_method": r["base"]["method"],
                "weak_base_origin_offset_s": r["base"]["smoothstep_derivative_inference"]["best_constant_time_offset_s"],
                "weak_base_template_max_error": r["base"]["smoothstep_derivative_inference"]["best_max_abs_difference_mps"],
                "last_weak_base_speed_mps": float(np.linalg.norm(r["base"]["last_velocity"][:2])),
                "max_lead_step_deg": r["streams"]["lead"]["changes"]["max_step"],
                "max_hal_step_deg": r["streams"]["hal"]["changes"]["max_step"],
                "max_lead_hal_difference_deg": r["lead_hal_difference"]["absolute_deg"]["max"],
                "diagnostic_flags": json.dumps(r["diagnostic_flags"], ensure_ascii=False)})
    report(summary, args.output / "findings.md")
    print(json.dumps({"sources": len(rows), "errors": errors, "diagnostic_source_counts": summary["diagnostic_source_counts"]}, ensure_ascii=False), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
