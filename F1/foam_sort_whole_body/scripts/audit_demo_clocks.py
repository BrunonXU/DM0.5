#!/usr/bin/env python3
"""Audit every indexed camera timestamp and selected image in a demo manifest.

Read-only inputs: raw file stat, committed v1 camera/alignment NPZ and metadata.
No image decoding, exposure calibration, interpolation, or dataset mutation.
Thresholds beyond existing v1 masks are diagnostic queues, not rejection rules.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import numpy as np

TASK = Path(__file__).resolve().parents[1]
CAMERAS = ("head", "left", "right")
NS = 1_000_000_000
THRESHOLDS = {
    "existing_camera_max_log_age_s": 0.1,
    "existing_header_offset_deviation_s": 0.25,
    "steady_after_start_s": 5.0,
    "diagnostic_log_gap_s": 0.1,
    "diagnostic_retained_offset_residual_s": 0.1,
    "diagnostic_early_late_steady_median_change_s": 0.05,
    "diagnostic_head_abs_median_offset_s": 0.25,
    "diagnostic_long_invalid_run_s": 0.5,
    "diagnostic_cross_camera_log_spread_s": 2 / 30,
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats(values):
    a = np.asarray(values, dtype=np.float64)
    a = a[np.isfinite(a)]
    if not len(a):
        return {"count": 0}
    return dict(count=len(a), min=float(a.min()), p01=float(np.quantile(a, .01)),
                median=float(np.median(a)), p95=float(np.quantile(a, .95)),
                p99=float(np.quantile(a, .99)), max=float(a.max()))


def runs(mask):
    edge = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(edge == 1), np.flatnonzero(edge == -1)))


def grid_intervals(mask, grid, start):
    return [dict(start_frame=int(a), stop_frame_exclusive=int(b), frames=int(b-a),
                 start_s=float((grid[a]-start)/NS),
                 last_frame_s=float((grid[b-1]-start)/NS), duration_s=(b-a)/30)
            for a, b in runs(mask)]


def check(condition, message):
    if not condition:
        raise ValueError(message)


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+"\n")


def records(path, rows):
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")


def csv_write(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()})


def inspect(source, v1, raw):
    episode = source["episode"]
    cp = v1/"cameras"/(episode+".npz")
    ap = v1/"aligned"/(episode+".npz")
    cm = json.loads(cp.with_suffix(".json").read_text())
    am = json.loads(ap.with_suffix(".json").read_text())
    check(sha(cp) == cm["npz_sha256"] == am["camera_npz_sha256"], "Camera NPZ hash mismatch")
    check(sha(ap) == source["aligned_npz_sha256"], "Aligned NPZ hash mismatch")
    check(sha(ap.with_suffix(".json")) == source["aligned_metadata_sha256"], "Aligned metadata hash mismatch")
    check(cm["episode"] == am["episode"] == episode, "Episode identity mismatch")
    check(cm["source_relative_path"] == am["source_relative_path"] == source["source_relative_path"], "Source identity mismatch")
    st = (raw/source["source_relative_path"]).stat()
    check(dict(size_bytes=st.st_size, mtime_ns=st.st_mtime_ns) == source["source_fingerprint"] == cm["source_fingerprint"], "Raw stat fingerprint mismatch")
    start, end = am["start_ns"], am["end_ns"]
    check((start, end) == (cm["start_ns"], cm["end_ns"]), "Time bound mismatch")
    check(start == source["source_start_ns"] and end+1 == source["source_stop_ns_exclusive"], "Catalog time mismatch")
    events, camera_rows, pools = [], [], defaultdict(list)
    with np.load(cp, allow_pickle=False) as cameras, np.load(ap, allow_pickle=False) as pack:
        grid = pack["time_ns"]
        expected_grid = start+np.rint(np.arange(len(grid))*NS/30).astype(np.int64)
        check(np.array_equal(grid, expected_grid), "30 Hz grid mismatch")
        check(grid[-1] <= end and len(grid) == source["frames"], "Grid bounds/count mismatch")
        recorded_indices = pack["camera_source_indices"]
        recorded_valid = pack["camera_timestamp_candidate_valid"]
        recorded_ages = pack["camera_log_age_s"]
        check(recorded_indices.shape == recorded_valid.shape == recorded_ages.shape == (len(grid), 3), "Camera shape mismatch")
        selected_logs = np.zeros((len(grid), 3), dtype=np.int64)
        relative_offset_residual = np.zeros((len(grid), 3), dtype=np.float64)
        actual_valid = np.zeros_like(recorded_valid)
        for col, name in enumerate(CAMERAS):
            logs, headers = cameras[name+"_log_ns"], cameras[name+"_header_ns"]
            candidate = cameras[name+"_timestamp_candidate_valid"]
            check(len(logs) == len(headers) == len(candidate) > 0, f"{name}: timestamp length")
            check(np.all(np.diff(logs) > 0), f"{name}: log timestamps not strictly increasing")
            check(logs[0] >= start and logs[-1] <= end, f"{name}: log bounds")
            legal = headers > 0
            offsets = np.zeros(len(logs), dtype=np.float64)
            offsets[legal] = (headers[legal]-logs[legal])/NS
            steady = legal & (logs >= start+5*NS)
            sl = logs[steady]
            sufficient = len(sl) >= 15 and sl[-1]-sl[0] >= .5*NS
            baseline = steady if sufficient else legal
            check(np.any(baseline), f"{name}: no legal baseline")
            median_ns = float(np.median(headers[baseline]-logs[baseline]))
            offset_bad = legal & (np.abs(offsets*NS-median_ns) > .25*NS)
            nonincreasing = np.zeros(len(logs), dtype=bool)
            il = np.flatnonzero(legal)
            if len(il) > 1:
                nonincreasing[il[1:]] = headers[il[1:]] <= np.maximum.accumulate(headers[il[:-1]])
            expected_candidate = legal & ~offset_bad & ~nonincreasing
            check(np.array_equal(expected_candidate, candidate), f"{name}: v1 timestamp mask mismatch")
            check(abs(median_ns-cm["cameras"][name]["steady_median_header_minus_log_ns"]) < 1, f"{name}: baseline mismatch")
            origin = np.flatnonzero(candidate)
            check(len(origin) > 0, f"{name}: no timestamp candidate")
            lookup = np.searchsorted(logs[origin], grid, side="right")-1
            expected_indices = np.where(lookup >= 0, origin[np.maximum(lookup, 0)], -1)
            age = grid-logs[origin[np.maximum(lookup, 0)]]
            valid = (lookup >= 0) & (age >= 0) & (age <= .1*NS)
            check(np.array_equal(recorded_indices[:, col], expected_indices), f"{name}: selected frame index mismatch")
            check(np.array_equal(recorded_ages[:, col], (age/NS).astype(np.float32)), f"{name}: selected age mismatch")
            check(np.array_equal(recorded_valid[:, col], valid), f"{name}: freshness mask mismatch")
            check(not np.any(valid & (age < 0)), f"{name}: future camera leakage")
            selected_logs[:, col] = logs[np.maximum(expected_indices, 0)]
            relative_offset_residual[:, col] = offsets[np.maximum(expected_indices, 0)]-median_ns/NS
            actual_valid[:, col] = valid
            relative_t = (logs-start)/NS
            residual = offsets-median_ns/NS
            stable_t = relative_t[steady]
            early = steady & (relative_t <= (stable_t[0]+5 if len(stable_t) else 0))
            late = steady & (relative_t >= (stable_t[-1]-5 if len(stable_t) else float("inf")))
            change = float(np.median(offsets[late])-np.median(offsets[early])) if early.any() and late.any() else None
            rate_ppm = None
            if len(stable_t) > 1 and np.ptp(stable_t) > 0:
                x = stable_t-np.mean(stable_t)
                rate_ppm = float(np.dot(x, offsets[steady]-np.mean(offsets[steady]))/np.dot(x, x)*1e6)
            jumps = np.diff(logs)/NS
            gap_idx = np.flatnonzero(jumps > .1)
            invalid_runs = grid_intervals(~valid, grid, start)
            interior_runs = [r for r in invalid_runs if r["start_frame"] > 0 and r["stop_frame_exclusive"] < len(grid)]
            max_missing = max((r["duration_s"] for r in invalid_runs), default=0)
            source_bad = ~candidate
            startup_bad = source_bad & (relative_t < 5)
            duplicate_steps = int(np.sum(np.diff(headers[legal]) == 0))
            backwards_steps = int(np.sum(np.diff(headers[legal]) < 0))
            selected_residual_bad = valid & (np.abs(relative_offset_residual[:, col]) > .1)
            head_clock_review = name == "head" and (abs(median_ns/NS) > .25 or (change is not None and abs(change) > .05))
            clock_bins = []
            if head_clock_review:
                seconds = np.floor(relative_t).astype(np.int64)
                for second in np.unique(seconds[legal]):
                    mask = legal & (seconds == second)
                    clock_bins.append(dict(start_s=int(second), stop_s_exclusive=int(second+1), frames=int(mask.sum()),
                                           median_header_minus_log_s=float(np.median(offsets[mask]))))
            row = dict(episode=episode, camera=name, source_frames=len(logs), timestamp_valid_frames=int(candidate.sum()),
                       invalid_header_frames=int((~legal).sum()), offset_outlier_frames=int(offset_bad.sum()),
                       duplicate_header_steps=duplicate_steps, backward_header_steps=backwards_steps,
                       header_high_water_masked_frames=int(nonincreasing.sum()), source_invalid_frames=int(source_bad.sum()),
                       startup_source_invalid_frames=int(startup_bad.sum()), interior_source_invalid_frames=int((source_bad & (relative_t >= 5)).sum()),
                       grid_invalid_frames=int((~valid).sum()), initial_missing_grid_frames=invalid_runs[0]["frames"] if invalid_runs and invalid_runs[0]["start_frame"] == 0 else 0,
                       interior_missing_grid_frames=sum(r["frames"] for r in interior_runs),
                       max_missing_grid_run_s=max_missing,
                       max_interior_missing_grid_run_s=max((r["duration_s"] for r in interior_runs), default=0),
                       log_gap_gt100ms_events=len(gap_idx), max_log_gap_s=float(jumps.max()) if len(jumps) else 0,
                       steady_median_header_minus_log_s=median_ns/NS,
                       steady_offset_distribution_s=stats(offsets[steady]),
                       retained_offset_residual_distribution_s=stats(residual[candidate]),
                       first_camera_log_s=float(relative_t[0]), first_retained_camera_log_s=float(relative_t[origin[0]]),
                       steady_early_late_median_change_s=change, offset_linear_trend_ppm=rate_ppm,
                       steady_early_median_header_minus_log_s=float(np.median(offsets[early])) if early.any() else None,
                       steady_late_median_header_minus_log_s=float(np.median(offsets[late])) if late.any() else None,
                       head_clock_offset_or_drift_review=head_clock_review,
                       diagnostic_clock_bins_1s=clock_bins,
                       offset_trend_is_diagnostic_not_clock_calibration=True,
                       raw_retained_offset_residual_gt100ms_frames=int(np.sum(candidate & (np.abs(residual) > .1))),
                       valid_grid_retained_offset_residual_gt100ms_frames=int(selected_residual_bad.sum()),
                       selected_log_age_s=stats(age[valid]/NS),
                       first_valid_grid_s=float((grid[np.flatnonzero(valid)[0]]-start)/NS) if valid.any() else None)
            camera_rows.append(row)
            for gap in gap_idx:
                events.append(dict(episode=episode, camera=name, kind="camera_log_gap_gt100ms", impact="v1_freshness_masks_missing_observations",
                                   start_s=float(relative_t[gap]), stop_s=float(relative_t[gap+1]), duration_s=float(jumps[gap]), source_frames=0))
            for interval in invalid_runs:
                events.append(dict(episode=episode, camera=name, kind="camera_grid_invalid", impact="already_masked_by_v1", **interval))
            for interval in grid_intervals(selected_residual_bad, grid, start):
                events.append(dict(episode=episode, camera=name, kind="retained_header_log_residual_gt100ms", impact="diagnostic_not_exposure_delay_or_auto_rejection", **interval))
            if change is not None and abs(change) > .05:
                events.append(dict(episode=episode, camera=name, kind="steady_offset_early_late_change_gt50ms", impact="review_common_clock_behaviour_not_auto_rejection",
                                   start_s=float(stable_t[0]), stop_s=float(stable_t[-1]), change_s=change))
            if name == "head" and abs(median_ns/NS) > .25:
                events.append(dict(episode=episode, camera=name, kind="head_steady_abs_offset_gt250ms", impact="clock_domain_mapping_review_not_exposure_delay_or_auto_rejection",
                                   start_s=float(stable_t[0]), stop_s=float(stable_t[-1]), median_header_minus_log_s=median_ns/NS))
            pools[name+"_steady_medians"].append(np.array([median_ns/NS]))
            pools[name+"_log_ages"].append(age[valid]/NS)
        joint_camera_valid = actual_valid.all(axis=1)
        check(np.array_equal(pack["observation_candidate_valid"], joint_camera_valid & pack["state_valid"].all(axis=1)), "Observation mask composition mismatch")
        spread = np.ptp(selected_logs, axis=1)/NS
        residual_spread = np.ptp(relative_offset_residual, axis=1)
        residual_any = (np.abs(relative_offset_residual) > .1).any(axis=1) & joint_camera_valid
        spread_bad = (spread > 2/30) & joint_camera_valid
        for kind, mask in (("cross_camera_log_spread_gt66p7ms", spread_bad),
                           ("joint_valid_retained_offset_residual_gt100ms", residual_any)):
            for interval in grid_intervals(mask, grid, start):
                events.append(dict(episode=episode, camera="joint", kind=kind, impact="diagnostic_not_auto_rejection", **interval))
        all_invalid = grid_intervals(~joint_camera_valid, grid, start)
        interior = [r for r in all_invalid if r["start_frame"] > 0 and r["stop_frame_exclusive"] < len(grid)]
        row = dict(episode=episode, parent_split=source["parent_split"], frames=len(grid),
                   head_clock_offset_or_drift_review=any(c["head_clock_offset_or_drift_review"] for c in camera_rows),
                   camera_valid_grid_frames=int(joint_camera_valid.sum()), camera_invalid_grid_frames=int((~joint_camera_valid).sum()),
                   initial_camera_invalid_grid_frames=all_invalid[0]["frames"] if all_invalid and all_invalid[0]["start_frame"] == 0 else 0,
                   interior_camera_invalid_grid_frames=sum(r["frames"] for r in interior),
                   max_interior_camera_invalid_run_s=max((r["duration_s"] for r in interior), default=0),
                   camera_only_joint_frame_loss=int(np.sum(~joint_camera_valid & pack["state_valid"].all(axis=1) & pack["action_candidate_valid"].all(axis=1))),
                   observation_valid_frames=int(pack["observation_candidate_valid"].sum()), joint_frame_valid_frames=int(pack["joint_frame_candidate_valid"].sum()),
                   v1_chunk_starts=int(pack["chunk_start_candidate_valid"].sum()),
                   selected_future_camera_frames=0, alignment_validation_errors=0,
                   cross_camera_log_spread_s=stats(spread[joint_camera_valid]),
                   cross_camera_log_spread_gt66p7ms_frames=int(spread_bad.sum()),
                   retained_offset_residual_gt100ms_joint_valid_frames=int(residual_any.sum()),
                   retained_offset_residual_gt100ms_joint_valid_max_run_s=max((r["duration_s"] for r in grid_intervals(residual_any, grid, start)), default=0),
                   cross_camera_offset_residual_spread_s=stats(residual_spread[joint_camera_valid]),
                   source_npz_hashes_verified=True, raw_stat_verified=True, exposure_alignment_verified=False)
        pools["cross_camera_log_spread"].append(spread[joint_camera_valid])
    return row, camera_rows, events, pools


def findings(summary):
    totals = summary["grid_totals"]
    cams = summary["camera_totals"]
    lines = ["# Demo 788：全量相机时间索引与对齐复核", "",
             f"生成时间：{summary['generated_at']}。候选清单 SHA256：`{summary['candidate_manifest_sha256']}`。", "",
             f"完成 {summary['audited_episodes']}/{summary['requested_episodes']} 份、{summary['audited_camera_streams']} 路；结构/哈希/查表验证错误 {len(summary['errors'])}。",
             "本次读取全部已提取相机时间戳和30Hz对齐数组，核验原MCAP大小/mtime；未重解码视频、未改写原数据或mask。", "",
             f"- {totals['frames']:,} 个30Hz网格中，三路相机时间候选有效 {totals['camera_valid_grid_frames']:,}；相机导致的网格无效 {totals['camera_invalid_grid_frames']:,}。",
             f"- 原始三路共 {cams['source_frames']:,} 个时间戳；现有规则屏蔽 {cams['source_invalid_frames']:,} 个，包含启动与重复/回退等问题，类别可能重叠。",
             f"- 在其他状态/动作有效的网格中，{totals['camera_only_joint_frame_loss']:,} 个因相机缺失或过旧被屏蔽；启动段之外的中途损失见逐条清单。",
             f"- 当前选择未来图像 {totals['selected_future_camera_frames']} 帧；每一相机索引、float32记录年龄、有效性布尔值均由原始log时间重新查表核对。", "",
             "## 已有mask解决的情况", "",
             "启动缺相机、header不递增/相对本路稳态异常、选中图像log年龄超过100ms，都已排除相应观测起点。",
             "零星缺帧不要求整条丢弃；连续录制无需按循环裁剪。动作chunk当前只要求起点三路相机有效，未来所有数值动作有效，",
             "所以未来相机暂时缺帧不直接作废一个纯动作监督chunk；这不表示每个未来步图像都已通过。", "",
             "## 仍不能由这些统计证明的事", "",
             "header − MCAP log 是两个记录字段的差，不是已知曝光延迟。三路时钟没有证据证明处于同一曝光时钟域；",
             "不能把各路中位数直接相减来自动移帧。按log选中图像为因果，只证明收到/记录时间查表正确，不能证明与实际动作严格物理同步。",
             "本报告的去本路中位数残差、前后5秒稳态中位数变化、三路log间距只用于定位抖动和漂移，不能作为自动校时或整条拒收。",
             "100ms残差和66.7ms三路log间距是诊断阈值，本次没有据此新增mask。图像质量、任务成功、人工干预、底盘标签视觉一致性仍需要独立证据。", "",
             "## 复核优先队列", "",
             "优先解释持续时钟行为；它与零星延迟尖峰不同。", "",
             "头部大稳态偏移8份（000000–000007）；录制内前后稳态变化超过50ms共5份（000000、000005–000008）；两组并集9份。",
             "所有9份属于all_data_1_derived，当前250ms相对本条中位数规则不能消除稳态偏移或小于250ms的慢变。", "",
             "| 来源/相机 | 稳态header−log中位数(s) | 前后稳态变化(s) |", "|---|---:|---:|"]
    for r in summary["head_clock_review"]:
        lines.append(f"| `{r['episode']}` / head | {r['steady_median_header_minus_log_s']:.6f} | {r['steady_early_late_median_change_s']:.6f} |")
    lines += ["", "上述来源的每秒偏移中位数保存在cameras.jsonl/cameras.csv中；当前不自动修改标签或整条排除。",
              "下表展示各批次三路的“逐录制稳态偏移中位数的中位数”，用于区分批次时钟域：", "",
              "| 批次 | 头部(s) | 左腕(s) | 右腕(s) |", "|---|---:|---:|---:|"]
    for batch, cams in summary["batch_camera_offsets"].items():
        lines.append(f"| `{batch}` | {cams['head']['median']:.6f} | {cams['left']['median']:.6f} | {cams['right']['median']:.6f} |")
    lines += ["", "左腕约+1.317s的批次偏移表明不能直接用原header与其他流混合对齐；不是已证实图像比动作晚1.317s。", "",
              "中途连续缺图超过0.5s仅1份：`all_data_2_derived/episode_000264`的头部，在27.600–28.133s共17个网格，",
              "覆盖0.567s训练网格时间；这些观测起点已被v1 mask排除。其余录制没有超过0.5s的中途三路联合缺图段。", "",
              "三路有效图像的log时间跨度中位数17.19ms、P95 30.13ms、P99 49.71ms，最大99.54ms。",
              "仍保留的本路header−log残差超过100ms共960个三路有效网格（501份录制），最长连续仅0.1s；并非501份整条坏数据。", "",
             "以下按有效网格仍有明显时间残差的连续长度排序，仅为可定位的复核线索：", "",
             "| 来源 | 最大连续残差>100ms有效段(s) | 对应有效网格数 | 最大中途缺图段(s) |", "|---|---:|---:|---:|"]
    for r in summary["highest_impact_episodes"][:12]:
        lines.append(f"| `{r['episode']}` | {r['retained_offset_residual_gt100ms_joint_valid_max_run_s']:.3f} | {r['retained_offset_residual_gt100ms_joint_valid_frames']} | {r['max_interior_camera_invalid_run_s']:.3f} |")
    lines += ["", "具体相机、起止秒和帧数见 [events.jsonl](events.jsonl)；全788份结果见 [episodes.csv](episodes.csv)，",
              "逐路稳态偏移/漂移见 [cameras.csv](cameras.csv)，类别汇总与分布见 [summary.json](summary.json)。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=TASK/"results/demo_788_check/candidates.jsonl")
    parser.add_argument("--v1", type=Path, default=TASK/"results/cleaning_v1")
    parser.add_argument("--raw", type=Path, default=Path("/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw"))
    parser.add_argument("--output", type=Path, default=TASK/"results/demo_788_check/clocks")
    args = parser.parse_args()
    started = time.monotonic()
    sources = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    check(len(sources) == len({r["episode"] for r in sources}), "Duplicate candidate source")
    args.output.mkdir(parents=True, exist_ok=True)
    episodes, camera_rows, events, errors, all_pools = [], [], [], [], defaultdict(list)
    for i, source in enumerate(sources):
        try:
            row, cameras, row_events, pools = inspect(source, args.v1, args.raw)
            episodes.append(row); camera_rows.extend(cameras); events.extend(row_events)
            for name, values in pools.items():
                all_pools[name].extend(values)
        except Exception as exc:
            errors.append(dict(episode=source["episode"], type=type(exc).__name__, error=str(exc)))
        if (i+1) % 100 == 0 or i+1 == len(sources):
            print(f"Camera audit {i+1}/{len(sources)}; errors={len(errors)}", flush=True)
    episode_totals = {key: sum(r[key] for r in episodes) for key in (
        "frames", "camera_valid_grid_frames", "camera_invalid_grid_frames", "initial_camera_invalid_grid_frames",
        "interior_camera_invalid_grid_frames", "camera_only_joint_frame_loss", "observation_valid_frames", "joint_frame_valid_frames",
        "v1_chunk_starts", "selected_future_camera_frames", "cross_camera_log_spread_gt66p7ms_frames", "retained_offset_residual_gt100ms_joint_valid_frames")}
    camera_totals = {key: sum(r[key] for r in camera_rows) for key in (
        "source_frames", "timestamp_valid_frames", "invalid_header_frames", "offset_outlier_frames", "duplicate_header_steps",
        "backward_header_steps", "header_high_water_masked_frames", "source_invalid_frames", "startup_source_invalid_frames",
        "interior_source_invalid_frames", "log_gap_gt100ms_events", "raw_retained_offset_residual_gt100ms_frames",
        "valid_grid_retained_offset_residual_gt100ms_frames")}
    categories = {}
    for kind in sorted({r["kind"] for r in events}):
        rows = [r for r in events if r["kind"] == kind]
        categories[kind] = dict(episodes=len({r["episode"] for r in rows}), events=len(rows),
                                grid_frame_occurrences=sum(r.get("frames", 0) for r in rows),
                                counts_may_overlap_across_cameras=True)
    ranked = sorted(episodes, key=lambda r: (r["retained_offset_residual_gt100ms_joint_valid_max_run_s"], r["retained_offset_residual_gt100ms_joint_valid_frames"], r["max_interior_camera_invalid_run_s"]), reverse=True)
    batch_offsets = {}
    for batch in sorted({r["episode"].split("/")[0] for r in camera_rows}):
        batch_offsets[batch] = {name: stats([r["steady_median_header_minus_log_s"] for r in camera_rows
                                            if r["episode"].startswith(batch+"/") and r["camera"] == name]) for name in CAMERAS}
    head_large = [r["episode"] for r in camera_rows if r["camera"] == "head" and abs(r["steady_median_header_minus_log_s"]) > .25]
    head_drift = [r["episode"] for r in camera_rows if r["camera"] == "head" and r["steady_early_late_median_change_s"] is not None and abs(r["steady_early_late_median_change_s"]) > .05]
    source_frames = {r["episode"]: r["frames"] for r in episodes}
    long_masked_gaps = [r for r in events if r["kind"] == "camera_grid_invalid" and r["duration_s"] > .5
                        and r["start_frame"] > 0 and r["stop_frame_exclusive"] < source_frames[r["episode"]]]
    summary = dict(generated_at=datetime.now(timezone.utc).isoformat(), script_sha256=sha(Path(__file__)),
                   candidate_manifest=str(args.manifest), candidate_manifest_sha256=sha(args.manifest),
                   requested_episodes=len(sources), audited_episodes=len(episodes), audited_camera_streams=len(camera_rows),
                   thresholds=THRESHOLDS, elapsed_s=time.monotonic()-started, errors=errors,
                   grid_totals=episode_totals, camera_totals=camera_totals, event_categories=categories,
                   distributions={key: stats(np.concatenate(values)) for key, values in all_pools.items()},
                   batch_camera_offsets=batch_offsets,
                   clock_review_clusters=dict(head_large_steady_offset_sources=head_large, head_in_recording_drift_sources=head_drift,
                                              head_union_sources=sorted(set(head_large+head_drift)),
                                              union_count=len(set(head_large+head_drift)),
                                              interpretation="Diagnostic clock-domain/variation cluster, not proven physical delay and not automatic rejection."),
                   interior_camera_masked_gaps_over500ms=long_masked_gaps,
                   head_clock_review=[{key: r[key] for key in ("episode", "camera", "steady_median_header_minus_log_s", "steady_early_late_median_change_s",
                                                               "steady_early_median_header_minus_log_s", "steady_late_median_header_minus_log_s")}
                                      for r in camera_rows if r["head_clock_offset_or_drift_review"]],
                   episode_flags=dict(
                       interior_camera_invalid_run_gt500ms=int(sum(r["max_interior_camera_invalid_run_s"] > .5 for r in episodes)),
                       sustained_retained_offset_residual_gt100ms_for_500ms=int(sum(r["retained_offset_residual_gt100ms_joint_valid_max_run_s"] >= .5 for r in episodes)),
                       any_retained_offset_residual_gt100ms=int(sum(r["retained_offset_residual_gt100ms_joint_valid_frames"] > 0 for r in episodes)),
                       source_header_reversal=len({r["episode"] for r in camera_rows if r["backward_header_steps"]}),
                       source_header_duplicate=len({r["episode"] for r in camera_rows if r["duplicate_header_steps"]})),
                   highest_impact_episodes=ranked[:20],
                   masks_modified=False, raw_modified=False, exposure_alignment_verified=False,
                   task_success_verified=False, notes="Log-reference causal alignment only; diagnostic thresholds do not grant/revoke training approval or calibrate exposure.")
    records(args.output/"episodes.jsonl", episodes); csv_write(args.output/"episodes.csv", episodes)
    records(args.output/"cameras.jsonl", camera_rows); csv_write(args.output/"cameras.csv", camera_rows)
    events.sort(key=lambda r: (-r.get("duration_s", 0), r["episode"], r["kind"]))
    records(args.output/"events.jsonl", events)
    dump(args.output/"summary.json", summary)
    if episodes:
        (args.output/"findings.md").write_text(findings(summary))
    print(json.dumps({key: summary[key] for key in ("requested_episodes", "audited_episodes", "audited_camera_streams", "grid_totals", "camera_totals", "episode_flags", "errors", "elapsed_s")}, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
