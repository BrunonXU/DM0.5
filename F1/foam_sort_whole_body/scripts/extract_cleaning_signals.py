#!/usr/bin/env python3
"""Extract unscaled, named low-dimensional MCAP signals without changing raw data.

Requires mcap, mcap-ros2-support and numpy. All timestamps are MCAP log_time in
nanoseconds. Arrays are stably sorted by that clock; original record-order timing
anomalies are reported. Images are never decoded. Full-record bounds and timing
for unselected topics come from MCAP message indexes, checked against statistics.
An episode JSON is the completion marker for its atomically replaced NPZ.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import traceback

import numpy as np
from mcap.data_stream import ReadDataStream
from mcap.reader import make_reader
from mcap.records import MessageIndex
from mcap_ros2.decoder import DecoderFactory


SCHEMA_VERSION = 1
EXTRACTOR_REVISION = "20260914-v1"
GAP_NS = 100_000_000
GRIPPER_CHANGE_RAW = 1.0
ARM_NAMES = [f"arm_{side}_j{i}" for side in ("l", "r") for i in range(1, 8)]
HAL_NAMES = ARM_NAMES + [f"body_j{i}" for i in range(1, 6)]
LEAD_NAMES = [f"arm_{side}_joint{i}" for side in ("L", "R") for i in range(1, 8)]
SIGNALS = {
    "/hal/joint_states": ("hal", "hal_q", HAL_NAMES),
    "/lead/joint_states": ("lead", "lead_q", LEAD_NAMES),
    "/motion_ctl/gripper/left": ("left_cmd", "left_cmd", None),
    "/motion_ctl/gripper/right": ("right_cmd", "right_cmd", None),
    "/motion_ctl/gripper/left/state": ("left_state", "left_state", ["left_gripper_position"]),
    "/motion_ctl/gripper/right/state": ("right_state", "right_state", ["right_gripper_position"]),
    "/move/State": ("base", "base_v", None),
}
EXPECTED_KEYS = {key for stem, value_key, _ in SIGNALS.values()
                 for key in (stem + "_t_ns", value_key)}


def fingerprint(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_npz(path, arrays):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            np.savez_compressed(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def time_stats(values):
    original = np.asarray(values, dtype=np.int64)
    if not len(original):
        return {"count": 0, "gaps_gt_0_1s": []}
    ordered = np.sort(original, kind="stable")
    diffs = np.diff(ordered)
    original_diffs = np.diff(original)
    positive = diffs[diffs > 0]
    gaps = np.flatnonzero(diffs > GAP_NS)
    return {
        "count": len(original), "first_ns": int(ordered[0]), "last_ns": int(ordered[-1]),
        "record_first_ns": int(original[0]), "record_last_ns": int(original[-1]),
        "record_backward_steps": int(np.count_nonzero(original_diffs < 0)),
        "record_duplicate_steps": int(np.count_nonzero(original_diffs == 0)),
        "duplicate_timestamp_count": int(np.count_nonzero(diffs == 0)),
        "zero_timestamp_count": int(np.count_nonzero(original == 0)),
        "median_positive_interval_s": float(np.median(positive) / 1e9) if len(positive) else None,
        "max_gap_s": float(diffs.max() / 1e9) if len(diffs) else None,
        "gap_threshold_s": 0.1, "gap_count": len(gaps),
        "gaps_gt_0_1s": [
            {"start_ns": int(ordered[i]), "end_ns": int(ordered[i + 1]),
             "duration_s": float(diffs[i] / 1e9)} for i in gaps],
    }


def named_positions(message, selected, topic):
    names = list(message.name)
    positions = list(message.position)
    if len(names) != len(positions) or len(set(names)) != len(names):
        raise ValueError(f"{topic}: malformed or duplicate joint names/positions")
    indices = {name: i for i, name in enumerate(names)}
    missing = set(selected).difference(indices)
    if missing:
        raise ValueError(f"{topic}: missing required joints {sorted(missing)}; got {names}")
    return [positions[indices[name]] for name in selected]


def gripper_candidates(timestamps, values):
    """Crossings of a raw-value difference from the last candidate's anchor.

    This also detects gradual changes whose per-message increments are small.
    The first sample initializes the anchor and is not itself a change event.
    """
    events = []
    anchor = 0
    for i in range(1, len(values)):
        delta = float(values[i] - values[anchor])
        if abs(delta) >= GRIPPER_CHANGE_RAW:
            events.append({"anchor_index": anchor, "index": i,
                           "anchor_t_ns": int(timestamps[anchor]), "t_ns": int(timestamps[i]),
                           "from_value": float(values[anchor]), "to_value": float(values[i]),
                           "delta_raw": delta, "direction": "increase" if delta > 0 else "decrease"})
            anchor = i
    return {"method": "absolute difference from last candidate anchor >= threshold_raw",
            "threshold_raw": GRIPPER_CHANGE_RAW, "count": len(events), "events": events,
            "interpretation": "Raw position-change candidates only; no cycle, grasp, or success label."}


def extract_one(task):
    source_string, root_string, output_string = task
    path, root, output = map(Path, (source_string, root_string, output_string))
    relative = path.relative_to(root)
    episode = relative.parent.as_posix()
    npz_path = output / relative.parent.parent / (relative.parent.name + ".npz")
    json_path = npz_path.with_suffix(".json")
    started = time.monotonic()
    before = fingerprint(path)
    metadata = {"schema_version": SCHEMA_VERSION, "extractor_revision": EXTRACTOR_REVISION,
                "source_relative_path": relative.as_posix(), "episode": episode,
                "source_fingerprint": before, "size": before["size"], "mtime_ns": before["mtime_ns"],
                "time_axis": "MCAP log_time nanoseconds", "value_transform": "none",
                "gripper_array_shape": "(N,)", "flags": []}
    try:
        if json_path.exists() and npz_path.exists():
            try:
                previous = json.loads(json_path.read_text())
                if (previous.get("status") == "complete"
                        and previous.get("schema_version") == SCHEMA_VERSION
                        and previous.get("extractor_revision") == EXTRACTOR_REVISION
                        and previous.get("source_relative_path") == relative.as_posix()
                        and previous.get("source_fingerprint") == before
                        and previous.get("npz_fingerprint") == fingerprint(npz_path)
                        and set(previous.get("arrays", {})) == EXPECTED_KEYS):
                    if fingerprint(path) != before:
                        raise ValueError("Source fingerprint changed during resume check")
                    return {"episode": episode, "status": "reused", "elapsed_s": round(time.monotonic()-started, 3),
                            "source_fingerprint": before, "npz_relative_path": npz_path.relative_to(output).as_posix()}
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        times, rows = defaultdict(list), defaultdict(list)
        with path.open("rb") as stream:
            reader = make_reader(stream, decoder_factories=[DecoderFactory()], validate_crcs=True)
            summary = reader.get_summary()
            if summary is None or summary.statistics is None:
                raise ValueError("Missing required MCAP summary/statistics")
            stats = summary.statistics
            channels = summary.channels
            counts = Counter()
            for cid, count in stats.channel_message_counts.items():
                counts[channels[cid].topic] += count
            missing_topics = set(SIGNALS).difference(counts)
            if missing_topics:
                raise ValueError(f"Missing required topics: {sorted(missing_topics)}")
            index_times = defaultdict(list)
            for chunk in sorted(summary.chunk_indexes, key=lambda item: item.chunk_start_offset):
                for cid, offset in chunk.message_index_offsets.items():
                    stream.seek(offset)
                    opcode, _ = struct.unpack("<BQ", stream.read(9))
                    if opcode != 7:
                        raise ValueError(f"Expected MessageIndex at byte {offset}, got opcode {opcode}")
                    index = MessageIndex.read(ReadDataStream(stream))
                    if index.channel_id != cid:
                        raise ValueError("MessageIndex channel differs from summary")
                    index_times[channels[cid].topic].extend(
                        timestamp for timestamp, _ in sorted(index.records, key=lambda item: item[1]))
            if {topic: len(values) for topic, values in index_times.items()} != dict(counts):
                raise ValueError("Full-record index counts differ from MCAP summary")
            if sum(counts.values()) != stats.message_count:
                raise ValueError("Per-topic counts differ from MCAP total message count")
            index_start = min(min(values) for values in index_times.values())
            index_end = max(max(values) for values in index_times.values())
            if (index_start, index_end) != (stats.message_start_time, stats.message_end_time):
                raise ValueError("Full-record index bounds differ from MCAP summary")
            metadata.update({"start_ns": index_start, "end_ns": index_end,
                             "duration_s": (index_end-index_start)/1e9,
                             "all_record_count": stats.message_count,
                             "full_record_bounds_source": "MCAP summary verified against all message indexes",
                             "topics": {topic: dict(time_stats(values), source="MCAP message index")
                                        for topic, values in sorted(index_times.items())}})
            for entry in reader.iter_decoded_messages(topics=list(SIGNALS), log_time_order=False):
                topic, message = entry.channel.topic, entry.decoded_message
                stem, _, selected = SIGNALS[topic]
                times[stem].append(entry.message.log_time)
                if topic == "/move/State":
                    value = [message.speed.linear.x, message.speed.linear.y, message.speed.angular.z]
                elif selected is not None:
                    value = named_positions(message, selected, topic)
                    if stem.endswith("_state"):
                        if len(message.position) != 1:
                            raise ValueError(f"{topic}: expected one gripper feedback position")
                        value = value[0]
                else:
                    value = float(message.position)
                rows[stem].append(value)
        arrays = {}
        metadata["arrays"] = {}
        metadata["gripper_change_candidates"] = {}
        for topic, (stem, value_key, selected) in SIGNALS.items():
            timestamps = np.asarray(times[stem], dtype=np.int64)
            values = np.asarray(rows[stem], dtype=np.float64)
            if not len(timestamps) or len(timestamps) != counts[topic]:
                raise ValueError(f"{topic}: decoded count {len(timestamps)} differs from summary {counts[topic]}")
            if not np.array_equal(timestamps, np.asarray(index_times[topic], dtype=np.int64)):
                raise ValueError(f"{topic}: decoded log_time sequence differs from message index")
            if not np.all(np.isfinite(values)):
                bad = np.argwhere(~np.isfinite(values))[:10].tolist()
                raise ValueError(f"{topic}: nonfinite raw signal values at indices {bad}")
            order = np.argsort(timestamps, kind="stable")
            timestamps, values = timestamps[order], values[order]
            arrays[stem + "_t_ns"], arrays[value_key] = timestamps, values
            for key in (stem + "_t_ns", value_key):
                metadata["arrays"][key] = {"shape": list(arrays[key].shape), "dtype": str(arrays[key].dtype)}
            topic_stats = metadata["topics"][topic]
            topic_stats.update({"source": "decoded CDR verified against message index",
                                "value_key": value_key, "timestamp_key": stem + "_t_ns",
                                "selected_source_names": selected,
                                "min": np.min(values, axis=0).tolist(), "max": np.max(values, axis=0).tolist(),
                                "first_value": values[0].tolist(), "last_value": values[-1].tolist(),
                                "nonfinite_values": 0})
            if stem in ("left_cmd", "right_cmd", "left_state", "right_state"):
                metadata["gripper_change_candidates"][stem] = gripper_candidates(timestamps, values)
        for topic, topic_stats in metadata["topics"].items():
            for field in ("record_backward_steps", "duplicate_timestamp_count", "zero_timestamp_count", "gap_count"):
                if topic_stats.get(field):
                    metadata["flags"].append(field + ":" + topic)
        metadata["column_order"] = {"hal_q": HAL_NAMES, "lead_q": LEAD_NAMES,
                                    "base_v": ["speed.linear.x", "speed.linear.y", "speed.angular.z"]}
        metadata["value_provenance"] = {
            "hal_q": "raw joint feedback position; unit contract unverified",
            "lead_q": "raw leader position; actuator-command equivalence unverified",
            "gripper": "raw position opening scale; no normalization",
            "base_v": "recorded derived/estimated speed; not measured velocity or verified command",
        }
        metadata["source_fingerprint_after_read"] = fingerprint(path)
        if metadata["source_fingerprint_after_read"] != before:
            raise ValueError("Source fingerprint changed while reading; output rejected")
        atomic_npz(npz_path, arrays)
        if fingerprint(path) != before:
            raise ValueError("Source fingerprint changed before JSON commit; output rejected")
        metadata.update({"status": "complete", "npz_fingerprint": fingerprint(npz_path),
                         "npz_relative_path": npz_path.relative_to(output).as_posix(),
                         "source_unchanged": True, "elapsed_s": round(time.monotonic()-started, 3)})
        atomic_json(json_path, metadata)
        return {"episode": episode, "status": "complete", "elapsed_s": metadata["elapsed_s"],
                "source_fingerprint": before, "npz_relative_path": metadata["npz_relative_path"],
                "signal_counts": {stem: len(times[stem]) for stem, _, _ in SIGNALS.values()}}
    except Exception as exc:
        metadata.update({"status": "error", "error": str(exc), "traceback": traceback.format_exc(),
                         "elapsed_s": round(time.monotonic()-started, 3)})
        atomic_json(json_path, metadata)
        return {"episode": episode, "status": "error", "error": str(exc), "elapsed_s": metadata["elapsed_s"]}


def normalized_episode(value):
    parts = value.strip("/").split("/")
    return "/".join(part.removesuffix("_derived").removeprefix("episode_") for part in parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--episode", action="append", default=[],
                        help="Repeatable exact batch/episode selector; short names must be unambiguous")
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if root == output or root in output.parents:
        parser.error("--output must be outside the read-only source tree")
    if not root.is_dir():
        parser.error(f"Source directory not found: {root}")
    paths = sorted(root.rglob("*.mcap"))
    episode_counts = Counter(path.parent for path in paths)
    duplicate_episodes = [str(path.relative_to(root)) for path, n in episode_counts.items() if n != 1]
    if duplicate_episodes:
        parser.error(f"Expected exactly one MCAP per episode; found multiple: {duplicate_episodes}")
    source_count = len(paths)
    if args.episode:
        selected = set()
        for selector in args.episode:
            normalized = normalized_episode(selector)
            matched = [path for path in paths if normalized_episode(path.parent.relative_to(root).as_posix()) == normalized
                       or ("/" not in normalized and normalized_episode(path.parent.name) == normalized)]
            if len(matched) != 1:
                parser.error(f"Episode {selector!r} matched {len(matched)} files; use full batch/episode")
            selected.add(matched[0])
        paths = sorted(selected)
    if not paths:
        parser.error("No MCAP files selected")
    output.mkdir(parents=True, exist_ok=True)
    initial_fingerprints = {path.relative_to(root).as_posix(): fingerprint(path) for path in paths}
    started = time.monotonic()
    results = []

    def save_summary(finished=False):
        status_counts = dict(Counter(row["status"] for row in results))
        summary = {"schema_version": SCHEMA_VERSION, "extractor_revision": EXTRACTOR_REVISION,
                   "source": str(root), "output": str(output), "source_episode_count": source_count,
                   "selected_episode_count": len(paths), "completed_episode_count": len(results),
                   "status_counts": status_counts, "finished": finished, "workers": args.workers,
                   "elapsed_s": round(time.monotonic()-started, 3),
                   "updated_at": datetime.now(timezone.utc).isoformat(),
                   "source_fingerprints_before": initial_fingerprints,
                   "time_axis": "MCAP log_time nanoseconds", "gripper_array_shape": "(N,)",
                   "value_transform": "none; float64 preserves decoded ROS numeric values",
                   "column_order": {"hal_q": HAL_NAMES, "lead_q": LEAD_NAMES,
                                    "base_v": ["speed.linear.x", "speed.linear.y", "speed.angular.z"]},
                   "episodes": sorted(results, key=lambda row: row["episode"])}
        if finished:
            changed = [relative for relative, initial in initial_fingerprints.items()
                       if fingerprint(root / relative) != initial]
            summary["source_fingerprints_changed"] = changed
            summary["source_unchanged"] = not changed
            summary["success"] = not changed and not status_counts.get("error", 0)
        atomic_json(output / "summary.json", summary)
        return summary

    save_summary()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(extract_one, (str(path), str(root), str(output))): path for path in paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {"episode": path.parent.relative_to(root).as_posix(), "status": "error",
                       "error": f"Worker exception: {exc}", "traceback": traceback.format_exc()}
            results.append(row)
            if row["status"] == "error":
                print("ERROR " + json.dumps(row, ensure_ascii=False), flush=True)
            if len(results) % 10 == 0 or len(results) == len(paths):
                summary = save_summary()
                print(json.dumps({key: summary[key] for key in
                                  ("completed_episode_count", "selected_episode_count", "status_counts", "elapsed_s")}), flush=True)
    summary = save_summary(finished=True)
    if not summary["success"]:
        raise SystemExit("Extraction failed: inspect error episodes/source changes in summary.json")


if __name__ == "__main__":
    main()
