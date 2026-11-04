#!/usr/bin/env python3
"""Read-only F1 timing audit; a pairing diagnostic, never a training exporter.

Only --output is written, exclusively. Original journals and image packs are read.
The optional independent-frame comparison uses host receipt <= grid, not exposure
time. Frames whose original whole observation was not ready are explicitly marked.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterable

CAMERAS = ("head", "left_wrist", "right_wrist")
MS = 1_000_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def stats(values: Iterable[float]) -> dict:
    values = sorted(values)
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None}

    def percentile(q: float) -> float:
        position = (len(values) - 1) * q
        low = int(position)
        high = min(low + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (position - low)

    return {"count": len(values), "min": values[0], "p50": percentile(.5),
            "p95": percentile(.95), "p99": percentile(.99), "max": values[-1]}


def differences(values: list[int]) -> list[int]:
    return [b - a for a, b in zip(values, values[1:])]


def verify_recorded_hash(path: Path, expected: str) -> str:
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"source hash mismatch: {path}")
    return actual


def contained_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"payload path outside source directory: {relative}")
    return path


def verify_frame_bytes(payload: Path, frame: dict) -> str:
    with contained_file(payload, frame["path"]).open("rb") as stream:
        stream.seek(frame["payload_offset"])
        data = stream.read(frame["payload_bytes"])
    if len(data) != frame["payload_bytes"]:
        raise ValueError("truncated frame payload")
    digest = hashlib.sha256(data).hexdigest()
    if digest != frame["payload_sha256"]:
        raise ValueError("frame payload SHA256 mismatch")
    return digest


def frame_pools(observations: list[dict]) -> dict[str, list[dict]]:
    pools = {camera: [] for camera in CAMERAS}
    for observation in observations:
        frames = {frame["camera"]: frame for frame in observation["frames"]}
        if set(frames) != set(CAMERAS) or len(observation["frames"]) != len(CAMERAS):
            raise ValueError("expected exactly one frame for each F1 camera")
        for camera in CAMERAS:
            frame = frames[camera]
            stamp = frame["timestamp"]
            if stamp["unit"] != "ns":
                raise ValueError("only explicitly labelled nanosecond stamps are supported")
            pools[camera].append(dict(frame, source_observation_id=observation["observation_id"],
                source_observation_available_ns=observation["available_monotonic_ns"]))
    for camera, frames in pools.items():
        times = [frame["host_received_monotonic_ns"] for frame in frames]
        if any(delta <= 0 for delta in differences(times)):
            raise ValueError(f"non-increasing host receipts: {camera}")
    return pools


def camera_metrics(frames: list[dict], payload: Path) -> dict:
    received = [frame["host_received_monotonic_ns"] for frame in frames]
    stamps = [frame["timestamp"]["value"] for frame in frames]
    host_delta, source_delta = differences(received), differences(stamps)
    repeats = []
    callback_gaps = []
    for previous, current in zip(frames, frames[1:]):
        if current["frame_id"] != previous["frame_id"] + 1:
            callback_gaps.append({"previous_frame_id": previous["frame_id"],
                "frame_id": current["frame_id"], "host_gap_ms":
                (current["host_received_monotonic_ns"] - previous["host_received_monotonic_ns"]) / MS})
        if current["timestamp"]["value"] == previous["timestamp"]["value"]:
            a, b = verify_frame_bytes(payload, previous), verify_frame_bytes(payload, current)
            repeats.append({"previous_frame_id": previous["frame_id"], "frame_id": current["frame_id"],
                "source_stamp_ns": current["timestamp"]["value"],
                "previous_payload_sha256": a, "payload_sha256": b, "bytes_differ": a != b})
    # Origins differ (ROS header vs host monotonic); only centered changes are compared.
    offset_changes = [(host - received[0] - (stamp - stamps[0])) / MS
                      for host, stamp in zip(received, stamps)]
    return {"frames": len(frames), "clock_domains": sorted({f["timestamp"]["clock_domain"] for f in frames}),
        "host_interval_ms": stats(delta / MS for delta in host_delta),
        "source_stamp_interval_ms": stats(delta / MS for delta in source_delta),
        "host_increment_minus_source_increment_ms": stats((a - b) / MS for a, b in zip(host_delta, source_delta)),
        "host_minus_source_change_from_first_ms": stats(offset_changes),
        "receipt_to_original_observation_available_ms": stats(
            (f["source_observation_available_ns"] - f["host_received_monotonic_ns"]) / MS for f in frames),
        "source_stamp_regressions": sum(delta < 0 for delta in source_delta),
        "source_stamp_repeat_count": len(repeats),
        "repeated_stamp_pairs_with_different_verified_bytes": sum(r["bytes_differ"] for r in repeats),
        "repeated_stamp_pairs": repeats, "callback_gaps": callback_gaps}


def latest_past(frames: list[dict], received: list[int], grid_ns: int) -> dict | None:
    index = bisect_right(received, grid_ns) - 1
    return frames[index] if index >= 0 else None


def compare_grid(observations: list[dict], grid: list[dict], episode_id: str) -> dict:
    """Diagnostic-only whole-episode search; no label/QC/boundary certification."""
    pools = frame_pools(observations)
    source_observations = {obs["observation_id"]: obs for obs in observations}
    if len(source_observations) != len(observations):
        raise ValueError("duplicate source observation ID")
    times = {c: [f["host_received_monotonic_ns"] for f in pools[c]] for c in CAMERAS}
    old_ages, new_ages = ({c: [] for c in CAMERAS} for _ in range(2))
    old_skews, new_skews, old_available_ages, new_available_ages = [], [], [], []
    changes, not_ready = Counter(), Counter()
    rows = []
    for row in grid:
        if row["session_id"] != episode_id:
            raise ValueError("grid session does not match source episode")
        original_observation = source_observations.get(row["source_row"])
        if original_observation is None:
            raise ValueError("grid source_row not found in audited source observations")
        if row["source_observation_monotonic_ns"] != original_observation["available_monotonic_ns"]:
            raise ValueError("grid source observation availability does not match audited source")
        if set(row["images"]) != set(CAMERAS):
            raise ValueError("grid cameras do not match audited source")
        for frame in original_observation["frames"]:
            original_image = row["images"][frame["camera"]]
            if any(key not in original_image or original_image[key] != value for key, value in frame.items()):
                raise ValueError("grid frame provenance does not match audited source: " + frame["camera"])
            if "received_ns" in original_image and original_image["received_ns"] != frame["host_received_monotonic_ns"]:
                raise ValueError("grid frame receipt alias does not match audited source")
        t = row["time_ns"]
        selected = {c: latest_past(pools[c], times[c], t) for c in CAMERAS}
        if any(frame is None for frame in selected.values()):
            raise ValueError("grid precedes first receipt of a required camera")
        old_times = [row["images"][c]["host_received_monotonic_ns"] for c in CAMERAS]
        if max(old_times) > t or row["source_observation_monotonic_ns"] > t:
            raise ValueError("existing grid contains a future receipt or observation")
        new_times = [selected[c]["host_received_monotonic_ns"] for c in CAMERAS]
        old_skews.append((max(old_times) - min(old_times)) / MS)
        new_skews.append((max(new_times) - min(new_times)) / MS)
        old_available_ages.append((t - row["source_observation_monotonic_ns"]) / MS)
        new_available_ages.append((t - max(new_times)) / MS)
        sources = {}
        for camera in CAMERAS:
            frame, original = selected[camera], row["images"][camera]
            old_ages[camera].append((t - original["host_received_monotonic_ns"]) / MS)
            new_ages[camera].append((t - frame["host_received_monotonic_ns"]) / MS)
            changed = original["frame_id"] != frame["frame_id"]
            group_not_ready = frame["source_observation_available_ns"] > t
            changes[camera] += changed
            not_ready[camera] += group_not_ready
            sources[camera] = {key: frame[key] for key in (
                "frame_id", "source_observation_id", "source_observation_available_ns",
                "host_received_monotonic_ns", "timestamp", "path", "payload_offset", "payload_bytes", "payload_sha256")}
            sources[camera].update(original_frame_id=original["frame_id"], changed=changed,
                original_group_not_ready_at_grid=group_not_ready)
        rows.append({"grid_ns": t, "segment_id": row["segment_id"],
                     "source_row": row["source_row"], "selected_frames": sources})
    return {"kind": "diagnostic_only_latest_host_receipt_per_camera",
        "rows": len(grid), "training_export_created": False,
        "scope": "same existing grid; all saved frames in this source episode; actions/state unchanged, not revalidated",
        "limitations": ["No exposure synchronization or calibrated clock mapping",
            "No new QC/control/episode-boundary validation; these pairings are not training approvals",
            "Whole-observation interface cannot supply frames marked original_group_not_ready_at_grid",
            "No decode/copy/transport latency is added to callback receipt; online readiness must be measured"],
        "original": {"camera_receipt_age_at_grid_ms": {c: stats(old_ages[c]) for c in CAMERAS},
            "cross_camera_host_receipt_skew_ms": stats(old_skews),
            "whole_observation_available_age_at_grid_ms": stats(old_available_ages)},
        "independent_camera_candidate": {"camera_receipt_age_at_grid_ms": {c: stats(new_ages[c]) for c in CAMERAS},
            "cross_camera_host_receipt_skew_ms": stats(new_skews),
            "latest_selected_callback_age_ms": stats(new_available_ages),
            "changed_frame_count": dict(changes), "original_group_not_ready_count": dict(not_ready)},
        "selection_provenance": rows}


def audit_episode(root: Path, *, verify_packs: bool = False) -> tuple[dict, list[dict]]:
    journal, payload = root / "journal", root / "payload"
    manifest = json.loads((journal / "manifest.json").read_text())
    payload_manifest = json.loads((payload / "manifest.json").read_text())
    if not manifest.get("complete") or not payload_manifest.get("complete"):
        raise ValueError(f"incomplete episode: {root}")
    verified = {}
    for name, expected in manifest["stream_sha256"].items():
        path = journal / f"{name}.jsonl"
        verified[str(path)] = verify_recorded_hash(path, expected)
    for name, expected in manifest["payload_sha256"].items():
        path = contained_file(payload, name)
        verified[str(path)] = verify_recorded_hash(path, expected)
    if verify_packs:
        for pack in payload_manifest["pack_files"].values():
            path = contained_file(payload, pack["path"])
            verified[str(path)] = verify_recorded_hash(path, pack["sha256"])
    raw_observations = read_jsonl(journal / "observations.jsonl")
    observations = read_jsonl(payload / "frames.jsonl")
    if not observations or len(observations) != len(raw_observations):
        raise ValueError("empty or mismatched journal/payload observations")
    for raw, obs in zip(raw_observations, observations):
        # Payload adds archive locations; all recorded journal fields must match.
        for key in ("observation_id", "available_monotonic_ns", "state_timestamps_ns"):
            if raw[key] != obs[key]:
                raise ValueError(f"journal/payload observation mismatch: {key}")
        if [{k: f[k] for k in rf} for rf, f in zip(raw["frames"], obs["frames"])] != raw["frames"]:
            raise ValueError("journal/payload camera metadata mismatch")
    pools = frame_pools(observations)
    available = [obs["available_monotonic_ns"] for obs in observations]
    if any(delta <= 0 for delta in differences(available)):
        raise ValueError("non-increasing observation availability")
    skews, source_skews = [], []
    for obs in observations:
        receipts = [f["host_received_monotonic_ns"] for f in obs["frames"]]
        stamps = [f["timestamp"]["value"] for f in obs["frames"]]
        skews.append((max(receipts) - min(receipts)) / MS)
        source_skews.append((max(stamps) - min(stamps)) / MS)
    return {"episode_id": manifest["episode_id"], "source_directory": str(root.resolve()),
        "source_manifest_sha256": sha256(journal / "manifest.json"),
        "verified_files_sha256": verified, "all_pack_hashes_verified": verify_packs,
        "repeated_stamp_pair_bytes_verified": True,
        "recorded_runtime_identity": manifest.get("binding", {}).get("runtime_identity"),
        "observations": len(observations), "span_s": (available[-1] - available[0]) / 1e9,
        "observation_interval_ms": stats(x / MS for x in differences(available)),
        "cross_camera_host_receipt_skew_ms": stats(skews),
        "cross_camera_unverified_header_skew_ms": stats(source_skews),
        "state_oldest_receipt_age_at_observation_available_ms": stats(
            (o["available_monotonic_ns"] - min(o["state_timestamps_ns"])) / MS for o in observations),
        "camera_discontinuity_observations": sum(o.get("camera_discontinuity", False) for o in observations),
        "cameras": {c: camera_metrics(pools[c], payload) for c in CAMERAS}}, observations


def output_path_check(output: Path, roots: list[Path]) -> None:
    resolved = output.resolve()
    if any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("output must not be inside source data directories")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite: {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True,
                        help="One episode directory, or parent containing episode directories")
    parser.add_argument("--grid-source", type=Path, help="Optional existing f1_source.jsonl, one source session")
    parser.add_argument("--verify-packs", action="store_true", help="Also hash all image packs (~1 GiB for four examples)")
    parser.add_argument("--output", type=Path, help="New JSON file; omitted means stdout; never overwritten")
    args = parser.parse_args(argv)
    source_roots = [args.raw_root]
    if args.grid_source:
        source_roots.extend([args.grid_source.parent, args.grid_source.resolve().parent])
    if args.output:
        output_path_check(args.output, source_roots)
    episodes = [args.raw_root] if (args.raw_root / "journal/manifest.json").is_file() else sorted(
        p for p in args.raw_root.iterdir() if (p / "journal/manifest.json").is_file())
    if not episodes:
        raise ValueError("no source episodes found")
    # Collection members can be symlinks to original episodes outside raw_root.
    # Protect every resolved episode before reading/auditing or creating output.
    source_roots.extend(root.resolve() for root in episodes)
    if args.output:
        output_path_check(args.output, source_roots)
    grid = read_jsonl(args.grid_source) if args.grid_source else []
    if args.grid_source and not grid:
        raise ValueError("grid source is empty")
    grid_session = grid[0]["session_id"] if grid else None
    report = {"schema": "f1_timing_audit_v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": sha256(Path(__file__)), "read_only_sources": True, "training_ready": False,
        "units": "ns in provenance; ms in statistics; seconds for span",
        "episodes": []}
    matched_grid = False
    for root in episodes:
        result, observations = audit_episode(root, verify_packs=args.verify_packs)
        if result["episode_id"] == grid_session:
            result["grid_source_sha256"] = sha256(args.grid_source)
            result["grid_pairing_comparison"] = compare_grid(observations, grid, grid_session)
            matched_grid = True
        report["episodes"].append(result)
    if grid and not matched_grid:
        raise ValueError("grid session not found among raw episodes")
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
        print(json.dumps({"output": str(args.output), "episodes": len(episodes), "training_ready": False}))
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
