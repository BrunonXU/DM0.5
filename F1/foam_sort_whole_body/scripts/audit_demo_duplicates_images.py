#!/usr/bin/env python3
"""Audit selected whole demos for exact copies and sampled image decode issues.

Original MCAP and cleaning arrays are read only. Timestamp-indexed image samples
are not full-frame image verification, task success review, or exposure sync.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import io
import itertools
import json
from pathlib import Path
import time

from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory
import numpy as np
from PIL import Image, ImageStat

TASK = Path(__file__).resolve().parents[1]
CAMERAS = {"head": "/camera/head/color/image_raw/compressed",
           "left": "/camera/left_wrist/color/image_raw/compressed",
           "right": "/camera/right_wrist/color/image_raw/compressed"}
SIGNALS = {"hal": "hal_q", "lead": "lead_q", "left_cmd": "left_cmd",
           "right_cmd": "right_cmd", "left_state": "left_state", "right_state": "right_state"}
SCHEMA = "f1_demo_content_audit_v1"
FRACTIONS = (0.1, 0.5, 0.9)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def obj_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def hash_array(hasher, name, values):
    values = np.ascontiguousarray(values.astype(values.dtype.newbyteorder("<"), copy=False))
    header = json.dumps([name, str(values.dtype), list(values.shape)], separators=(",", ":")).encode()
    hasher.update(len(header).to_bytes(8, "little"))
    hasher.update(header)
    hasher.update(values.tobytes())


def trajectory_hashes(signals):
    relative = hashlib.sha256()
    values_only = hashlib.sha256()
    origin = min(int(signals[stream + "_t_ns"][0]) for stream in SIGNALS)
    for stream, value_key in SIGNALS.items():
        values = signals[value_key]
        if stream == "hal":
            values = values[:, :14]  # Body/base excluded from the arm/gripper copy signature.
        hash_array(relative, stream + "_relative_t_ns", signals[stream + "_t_ns"] - origin)
        hash_array(relative, value_key, values)
        hash_array(values_only, value_key, values)
    base = hashlib.sha256()
    hash_array(base, "base_relative_t_ns", signals["base_t_ns"] - origin)
    hash_array(base, "base_v", signals["base_v"])
    # Exact moving HAL blocks provide additional partial-copy evidence. Stride
    # limits cost; this is explicitly not an exhaustive overlap/near-copy search.
    blocks = []
    q, times = signals["hal_q"][:, :14], signals["hal_t_ns"]
    for start in range(0, max(0, len(q) - 29), 15):
        window = q[start:start + 30]
        delta_t = np.diff(times[start:start + 30])
        if not np.any(np.diff(window, axis=0) != 0) or np.any(delta_t <= 0) or np.any(delta_t > 100_000_000):
            continue
        h = hashlib.sha256()
        hash_array(h, "hal_arms_30_samples", window)
        blocks.append({"sha256": h.hexdigest(), "start_index": start,
                       "start_log_ns": int(times[start]), "end_log_ns": int(times[start + 29])})
    return {"arm_gripper_relative_time_sha256": relative.hexdigest(),
            "arm_gripper_values_sha256": values_only.hexdigest(),
            "weak_base_relative_time_sha256": base.hexdigest(),
            "hal_moving_blocks": blocks}


def pick_samples(cameras, row):
    selected = []
    for fraction in FRACTIONS:
        target = row["source_start_ns"] + round(row["source_duration_ns"] * fraction)
        for camera in CAMERAS:
            logs = cameras[camera + "_log_ns"]
            eligible = np.flatnonzero(cameras[camera + "_timestamp_candidate_valid"])
            if not len(eligible):
                selected.append({"camera": camera, "fraction": fraction, "status": "no_valid_index_frame"})
                continue
            index = int(eligible[np.argmin(np.abs(logs[eligible] - target))])
            selected.append({"camera": camera, "fraction": fraction, "index": index,
                             "log_ns": int(logs[index]), "header_ns": int(cameras[camera + "_header_ns"][index]),
                             "requested_log_ns": target,
                             "distance_from_request_s": abs(int(logs[index]) - target) / 1e9,
                             "indexed_timestamp_candidate_valid": True})
    return selected


def decode_image(message, decoded, sample):
    payload = bytes(decoded.data)
    result = {**sample, "actual_log_ns": int(message.log_time),
              "compressed_format_field": str(decoded.format), "payload_bytes": len(payload),
              "jpeg_payload_sha256": hashlib.sha256(payload).hexdigest()}
    try:
        with Image.open(io.BytesIO(payload)) as image:
            actual_format, width, height, mode = image.format, image.width, image.height, image.mode
            image.load()  # Forces full decode of this selected frame.
            rgb = image.convert("RGB")
            gray = rgb.convert("L")
            stats = ImageStat.Stat(gray)
            pixels = np.asarray(gray)
            black_fraction = float(np.mean(pixels <= 4))
            result.update(status="decoded", decoded_format=actual_format, width=width, height=height,
                          source_mode=mode, gray_mean=float(stats.mean[0]), gray_std=float(stats.stddev[0]),
                          gray_min=int(pixels.min()), gray_max=int(pixels.max()),
                          fraction_gray_le_4=black_fraction,
                          black_frame_candidate=black_fraction >= 0.995,
                          flat_frame_candidate=float(stats.stddev[0]) < 1.0,
                          decoded_rgb_sha256=hashlib.sha256(rgb.tobytes()).hexdigest())
    except Exception as exc:
        result.update(status="image_decode_error", error=repr(exc))
    return result


def process_source(row, output, run_identity):
    started = time.monotonic()
    target = output / "sources" / (row["episode"] + ".json")
    identity = {"run_identity": run_identity, "candidate_row_sha256": obj_sha(row)}
    if target.is_file():
        previous = json.loads(target.read_text())
        if previous.get("identity") == identity and previous.get("status") == "complete":
            return previous
    result = {"schema": SCHEMA, "episode": row["episode"], "parent_split": row["parent_split"],
              "identity": identity, "source_start_ns": row["source_start_ns"],
              "source_stop_ns_exclusive": row["source_stop_ns_exclusive"],
              "source_fingerprint": row["source_fingerprint"], "raw_path": row["raw_path"],
              "frames": [], "issues": [], "created_at": utc_now()}
    try:
        raw = Path(row["raw_path"])
        before = raw.stat()
        expected = row["source_fingerprint"]
        if (before.st_size, before.st_mtime_ns) != (expected["size_bytes"], expected["mtime_ns"]):
            raise ValueError("Raw stat differs from selected source")
        for name in ("signal", "camera"):
            if file_sha(row[name + "_npz_path"]) != row[name + "_npz_sha256"]:
                raise ValueError(name + " pack SHA mismatch")
        with np.load(row["signal_npz_path"], allow_pickle=False) as signals:
            result.update(trajectory_hashes(signals))
        with np.load(row["camera_npz_path"], allow_pickle=False) as cameras:
            samples = pick_samples(cameras, row)
        seek_windows = []
        with raw.open("rb") as stream:
            reader = make_reader(stream, decoder_factories=[DecoderFactory()])
            summary = reader.get_summary()
            result["mcap_chunk_index_count"] = len(summary.chunk_indexes) if summary else 0
            if not summary or not summary.chunk_indexes:
                raise ValueError("Indexed seeking unavailable; refusing repeated whole-MCAP scans")
            for fraction in FRACTIONS:
                phase = [s for s in samples if s["fraction"] == fraction and "log_ns" in s]
                missing = [s for s in samples if s["fraction"] == fraction and "log_ns" not in s]
                result["frames"].extend(missing)
                if not phase:
                    continue
                lookup = {(CAMERAS[s["camera"]], s["log_ns"]): s for s in phase}
                found = set()
                begin, end = min(s["log_ns"] for s in phase), max(s["log_ns"] for s in phase) + 1
                seek_windows.append({"start_ns": begin, "stop_ns_exclusive": end})
                for _, channel, message, decoded in reader.iter_decoded_messages(
                        topics=list(CAMERAS.values()), start_time=begin, end_time=end):
                    key = (channel.topic, int(message.log_time))
                    if key in lookup and key not in found:
                        result["frames"].append(decode_image(message, decoded, lookup[key]))
                        found.add(key)
                for key in lookup.keys() - found:
                    result["frames"].append({**lookup[key], "status": "indexed_frame_not_found"})
        after = raw.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError("Raw stat changed during read")
        result["source_stat_unchanged"] = True
        result["seek_windows"] = seek_windows
        result["frames"].sort(key=lambda s: (s["fraction"], s["camera"]))
        for sample in result["frames"]:
            if sample["status"] != "decoded":
                result["issues"].append(sample["status"])
            for flag in ("black_frame_candidate", "flat_frame_candidate"):
                if sample.get(flag):
                    result["issues"].append(flag)
            if sample.get("status") == "decoded" and sample["decoded_format"] != "JPEG":
                result["issues"].append("non_JPEG_actual_format")
        result["issues"] = sorted(set(result["issues"]))
        result["status"] = "complete"
    except Exception as exc:
        result.update(status="error", error=repr(exc))
    result["elapsed_s"] = time.monotonic() - started
    write_json(target, result)
    return result


def group_records(rows, key):
    groups = defaultdict(list)
    for row in rows:
        if key in row:
            groups[row[key]].append(row)
    return [{"signature": signature, "episodes": [r["episode"] for r in group],
             "splits": sorted({r["parent_split"] for r in group}),
             "cross_split": len({r["parent_split"] for r in group}) > 1}
            for signature, group in groups.items() if len(group) > 1]


def summarize(rows, candidates, output, run_identity, raw_sha_budget):
    by_episode = {row["episode"]: row for row in rows}
    exact_relative = group_records(rows, "arm_gripper_relative_time_sha256")
    exact_values = group_records(rows, "arm_gripper_values_sha256")
    sizes_bounds = defaultdict(list)
    for row in rows:
        sizes_bounds[(row["source_fingerprint"]["size_bytes"], row["source_start_ns"], row["source_stop_ns_exclusive"])].append(row)
    bounds_groups = [{"size_start_stop": list(key), "episodes": [r["episode"] for r in group],
                     "cross_split": len({r["parent_split"] for r in group}) > 1}
                    for key, group in sizes_bounds.items() if len(group) > 1]
    overlaps = []
    ordered = sorted(rows, key=lambda r: r["source_start_ns"])
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if right["source_start_ns"] >= left["source_stop_ns_exclusive"]:
                break
            overlaps.append({"episodes": [left["episode"], right["episode"]],
                             "overlap_s": (min(left["source_stop_ns_exclusive"], right["source_stop_ns_exclusive"]) - right["source_start_ns"]) / 1e9,
                             "cross_split": left["parent_split"] != right["parent_split"],
                             "evidence": "log_clock_range_overlap_not_alone_proof_of_duplicate"})
    image_groups = defaultdict(list)
    block_groups = defaultdict(set)
    dimension_counts = defaultdict(Counter)
    format_counts = Counter()
    frame_statuses = Counter()
    black_candidates, flat_candidates = [], []
    for row in rows:
        for frame in row["frames"]:
            frame_statuses[frame["status"]] += 1
            if "jpeg_payload_sha256" in frame:
                image_groups[frame["jpeg_payload_sha256"]].append({"episode": row["episode"],
                    "split": row["parent_split"], "camera": frame["camera"], "log_ns": frame["log_ns"]})
            if frame["status"] == "decoded":
                dimension_counts[frame["camera"]][f'{frame["width"]}x{frame["height"]}'] += 1
                format_counts[frame["decoded_format"]] += 1
            reference = {"episode": row["episode"], "camera": frame["camera"], "log_ns": frame.get("log_ns")}
            if frame.get("black_frame_candidate"):
                black_candidates.append(reference)
            if frame.get("flat_frame_candidate"):
                flat_candidates.append(reference)
        for block in row.get("hal_moving_blocks", []):
            block_groups[block["sha256"]].add(row["episode"])
    repeated_payloads = [{"sha256": h, "samples": group,
                          "cross_source": len({s["episode"] for s in group}) > 1,
                          "cross_split": len({s["split"] for s in group}) > 1}
                         for h, group in image_groups.items() if len(group) > 1]
    block_pairs = Counter()
    for group in block_groups.values():
        for pair in itertools.combinations(sorted(group), 2):
            block_pairs[pair] += 1
    motion_overlap = [{"episodes": list(pair), "shared_sampled_30_frame_block_count": count,
                       "cross_split": by_episode[pair[0]]["parent_split"] != by_episode[pair[1]]["parent_split"]}
                      for pair, count in block_pairs.most_common()]
    # Only strong copy candidates are eligible for expensive full-file SHA.
    raw_candidates = set()
    for group in exact_relative + exact_values + bounds_groups:
        raw_candidates.update(group["episodes"])
    payload_pairs = Counter()
    for group in repeated_payloads:
        for pair in itertools.combinations(sorted({s["episode"] for s in group["samples"]}), 2):
            payload_pairs[pair] += 1
    for pair, count in payload_pairs.items():
        if count >= 3:
            raw_candidates.update(pair)
    hashed, deferred, used = [], [], 0
    for episode in sorted(raw_candidates):
        row = by_episode[episode]
        size = row["source_fingerprint"]["size_bytes"]
        if used + size > raw_sha_budget:
            deferred.append(episode)
            continue
        st = Path(row["raw_path"]).stat()
        if (st.st_size, st.st_mtime_ns) != (size, row["source_fingerprint"]["mtime_ns"]):
            raise ValueError("Raw source changed before candidate SHA: " + episode)
        sha = file_sha(row["raw_path"])
        after = Path(row["raw_path"]).stat()
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise ValueError("Raw source changed during candidate SHA: " + episode)
        hashed.append({"episode": episode, "parent_split": row["parent_split"], "raw_sha256": sha})
        used += size
    confirmed_raw = group_records(hashed, "raw_sha256")
    duplicate_report = {"exact_arm_gripper_relative_time_groups": exact_relative,
                        "exact_arm_gripper_values_groups": exact_values,
                        "same_size_and_absolute_bounds_groups": bounds_groups,
                        "absolute_log_time_overlaps": overlaps,
                        "repeated_sampled_jpeg_payloads": repeated_payloads,
                        "shared_sampled_hal_motion_blocks": motion_overlap,
                        "strong_raw_copy_candidates": sorted(raw_candidates),
                        "candidate_raw_sha256": hashed, "raw_sha_deferred_by_budget": deferred,
                        "confirmed_exact_raw_duplicate_groups": confirmed_raw}
    write_json(output / "duplicates.json", duplicate_report)
    with (output / "sources.jsonl").open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "sources.csv").open("w", newline="") as handle:
        fields = ["episode", "parent_split", "status", "samples", "decoded", "issues", "elapsed_s",
                  "arm_gripper_relative_time_sha256", "arm_gripper_values_sha256"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({"episode": row["episode"], "parent_split": row["parent_split"], "status": row["status"],
                             "samples": len(row["frames"]), "decoded": sum(s["status"] == "decoded" for s in row["frames"]),
                             "issues": ";".join(row["issues"]), "elapsed_s": round(row["elapsed_s"], 4),
                             "arm_gripper_relative_time_sha256": row.get("arm_gripper_relative_time_sha256"),
                             "arm_gripper_values_sha256": row.get("arm_gripper_values_sha256")})
    summary = {"schema": SCHEMA, "created_at": utc_now(), "run_identity": run_identity,
               "candidate_count": len(candidates), "audited_sources": len(rows),
               "source_statuses": dict(Counter(r["status"] for r in rows)),
               "split_counts": dict(Counter(r["parent_split"] for r in rows)),
               "samples_expected": len(candidates) * 9, "sample_statuses": dict(frame_statuses),
               "camera_sample_dimensions": {k: dict(v) for k, v in dimension_counts.items()},
               "decoded_format_counts": dict(format_counts),
               "black_frame_candidates": black_candidates, "flat_frame_candidates": flat_candidates,
               "sources_with_image_issue": [r["episode"] for r in rows if r["issues"]],
               "exact_numeric_relative_time_duplicate_groups": len(exact_relative),
               "exact_numeric_values_duplicate_groups": len(exact_values),
               "same_size_absolute_bounds_groups": len(bounds_groups),
               "absolute_log_overlap_pairs": len(overlaps),
               "cross_split_log_overlap_pairs": sum(x["cross_split"] for x in overlaps),
               "repeated_sampled_jpeg_payload_groups": len(repeated_payloads),
               "cross_source_repeated_sampled_payload_groups": sum(x["cross_source"] for x in repeated_payloads),
               "cross_split_repeated_sampled_payload_groups": sum(x["cross_split"] for x in repeated_payloads),
               "shared_sampled_motion_block_pairs": len(motion_overlap),
               "cross_split_shared_motion_block_pairs": sum(x["cross_split"] for x in motion_overlap),
               "raw_copy_candidates": len(raw_candidates), "raw_files_sha256_computed": len(hashed),
               "raw_bytes_sha256_read": used, "confirmed_exact_raw_duplicate_groups": len(confirmed_raw),
               "cross_split_exact_numeric_groups": sum(x["cross_split"] for x in exact_relative),
               "sampling": {"fractions_of_source_duration": list(FRACTIONS), "cameras": CAMERAS,
                            "selection": "nearest timestamp-candidate-valid camera index; exact topic/log_ns seek",
                            "decode": "Pillow load and RGB conversion for each selected frame",
                            "black_candidate": "at least 99.5% grayscale pixels <= 4/255; review flag only",
                            "flat_candidate": "grayscale standard deviation < 1/255; review flag only",
                            "motion_blocks": "30 consecutive HAL arm samples, stride 15, nonconstant, positive gaps <= 0.1s"},
               "limitations": ["Image sampling is not full-frame decoding or full-video content review.",
                               "Relative trajectory hashes detect exact equality, not near duplicates or semantic similarity.",
                               "Sampled motion blocks and JPEGs do not exclude all retimed or partial overlaps.",
                               "Matching absolute time ranges alone does not prove a shared recording or clock.",
                               "MCAP size/mtime checks are not cryptographic proof of unchanged raw bytes.",
                               "No task success, cycle boundaries, synchronization, interface safety or training approval inferred."],
               "raw_files_modified": 0, "v1_files_modified": 0, "annotations_modified": 0,
               "training_approved": False}
    write_json(output / "summary.json", summary)
    lines = ["# 788整条候选：重复与图像内容抽检", "", f"候选 {len(candidates)}；完成 {len(rows)}，状态 {summary['source_statuses']}。",
             f"三路各早/中/晚实际解码：{dict(frame_statuses)}，预期 {len(candidates) * 9} 帧。", "",
             f"- 双臂/夹爪完整原数值＋相对时间完全重复组：{len(exact_relative)}；忽略时间仅值完全重复组：{len(exact_values)}。",
             f"- 源大小＋绝对起止时间相同组：{len(bounds_groups)}；绝对记录时间重叠对：{len(overlaps)}。",
             f"- 抽样JPEG payload重复组：{len(repeated_payloads)}（跨源 {summary['cross_source_repeated_sampled_payload_groups']}，跨分组 {summary['cross_split_repeated_sampled_payload_groups']}）。",
             f"- 抽样30帧HAL移动块跨源完全相同对：{len(motion_overlap)}；跨分组 {summary['cross_split_shared_motion_block_pairs']}。",
             f"- 强复制候选 {len(raw_candidates)} 源，仅对其中 {len(hashed)} 源计算完整MCAP SHA，读 {used} 字节；确认原文件完全重复组 {len(confirmed_raw)}。",
             f"- 黑画面候选 {len(black_candidates)} 帧，低纹理候选 {len(flat_candidates)} 帧；只标记，不自动拒收。", "",
             "## 样本图像规格", "", "```json", json.dumps(summary["camera_sample_dimensions"], ensure_ascii=False, indent=2), "```", "",
             "## 结论边界", "", "每源只在10%、50%、90%附近选时间索引有效帧，按准确topic/log时间读取原JPEG并完整解码该帧。",
             "这不是全帧解码，也不是任务成功、循环数或图像与动作物理同步验收。重复哈希只能判断已比较数据的完全一致，不能证明不存在近重复。",
             "绝对时间重叠仅提供线索；后补底盘标签单独计算哈希，不把全零底盘/恒定右夹爪当作源重复证据。",
             "原文件、v1及标注未修改，未自动批准训练。详情见 `summary.json`、`duplicates.json`、`sources.csv` 和 `sources/`。", ""]
    (output / "findings.md").write_text("\n".join(lines))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=TASK / "results/demo_788_check/candidates.jsonl")
    parser.add_argument("--output", type=Path, default=TASK / "results/demo_788_check/content")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--raw-sha-budget-gib", type=float, default=16)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4:
        raise ValueError("Use 1–4 workers for bounded MCAP I/O")
    rows = [json.loads(line) for line in args.candidates.read_text().splitlines() if line.strip()]
    if args.limit is not None:
        rows = rows[:args.limit]
    if len({r["episode"] for r in rows}) != len(rows):
        raise ValueError("Duplicate candidate episode IDs")
    args.output.mkdir(parents=True, exist_ok=True)
    identity = {"schema": SCHEMA, "script_sha256": file_sha(__file__),
                "candidate_manifest_sha256": file_sha(args.candidates), "source_count": len(rows)}
    write_json(args.output / "run_config.json", {**identity, "created_at": utc_now(), "workers": args.workers,
                                               "raw_sha_budget_gib": args.raw_sha_budget_gib})
    results = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_source, row, args.output, identity): row["episode"] for row in rows}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if len(results) % 20 == 0 or len(results) == len(rows) or result["status"] != "complete":
                progress = {"completed": len(results), "total": len(rows), "elapsed_s": round(time.monotonic() - started, 2),
                            "last_episode": result["episode"], "last_status": result["status"],
                            "errors": sum(r["status"] == "error" for r in results)}
                write_json(args.output / "progress.json", progress)
                print(json.dumps(progress), flush=True)
    results.sort(key=lambda r: r["episode"])
    summary = summarize(results, rows, args.output, identity, int(args.raw_sha_budget_gib * 1024 ** 3))
    print(json.dumps({"status": "finished", "sources": len(results), "errors": summary["source_statuses"].get("error", 0),
                      "sample_statuses": summary["sample_statuses"], "elapsed_s": round(time.monotonic() - started, 2)}), flush=True)
    if summary["source_statuses"].get("error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
