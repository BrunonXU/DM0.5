"""Schema4 published-command replay audit for the bounded offline RLT pilot.

Timestamps use the recorder host clock. Images retain their original bytes and
receipt times; exposure synchronization and actuator acceptance are not claimed.
All frame/stream/pack hashes are checked before deriving a replay dataset.
"""
from __future__ import annotations

import bisect
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

RIGHT = (*range(7, 14), 15)
CAMERAS = ("head", "right_wrist")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rows(path):
    with Path(path).open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def contained(root, relative):
    target = (Path(root) / relative).resolve()
    if Path(root).resolve() not in target.parents:
        raise ValueError("Source path escapes raw recording")
    return target


def audit_raw(root):
    root = Path(root)
    manifest = json.loads((root / "journal/manifest.json").read_text())
    payload = json.loads((root / "payload/manifest.json").read_text())
    for doc in (manifest, payload):
        if not doc["complete"] or doc["drop_count"] != 0 or doc["writer_error"]:
            raise ValueError("Incomplete/dropped raw recording")
    if manifest["schema_version"] != 4:
        raise ValueError("This adapter is explicitly for schema4")
    if manifest["episode_id"] != root.name:
        raise ValueError("Raw identity mismatch")
    binding = manifest["binding"]
    if binding["model_id"] != "f1-toys-eef-81444e7071723c1f" or binding["execution_mode"] != "powered":
        raise ValueError("Expected real B1250 execution")
    verified = {}
    for stream, expected in manifest["stream_sha256"].items():
        path = root / "journal" / (stream + ".jsonl")
        if digest(path) != expected:
            raise ValueError("Journal digest mismatch: " + stream)
        verified[str(path.relative_to(root))] = expected
    for name, expected in manifest["payload_sha256"].items():
        if digest(contained(root / "payload", name)) != expected:
            raise ValueError("Payload index digest mismatch")
        verified["payload/" + name] = expected
    for info in payload["pack_files"].values():
        path = contained(root / "payload", info["path"])
        if path.stat().st_size != info["bytes"] or digest(path) != info["sha256"]:
            raise ValueError("Image pack digest mismatch")
        verified["payload/" + info["path"]] = info["sha256"]
    streams = {name: rows(root / "journal" / (name + ".jsonl")) for name in manifest["counts"]}
    for name, expected in manifest["counts"].items():
        if len(streams[name]) != expected:
            raise ValueError("Journal count mismatch")
        if any(r["episode_id"] != root.name for r in streams[name]):
            raise ValueError("Mixed journal identity")
    frames = rows(root / "payload/frames.jsonl")
    if len(frames) != payload["count"] or len({f["observation_id"] for f in frames}) != len(frames):
        raise ValueError("Payload count/identity mismatch")
    return streams, frames, {"source_manifest_sha256": digest(root / "journal/manifest.json"),
        "files_sha256": verified, "model_id": binding["model_id"],
        "runtime_identity": binding["runtime_identity"],
        "raw_training_ready_unchanged": manifest["training_ready"]}


def observation_reason(row, frame, decision_ns, cfg):
    if row.get("hil_state") != "policy":
        return "observation_not_policy"
    available, capture = row["available_monotonic_ns"], row["capture_started_monotonic_ns"]
    if not capture <= available <= decision_ns or decision_ns - available > cfg["max_observation_age_ns"]:
        return "observation_stale_or_future"
    if row.get("camera_discontinuity"):
        return "camera_discontinuity"
    if len(row.get("state16", [])) != 16 or not all(math.isfinite(v) for v in row["state16"]):
        return "state_nonfinite"
    mask, stamps = row.get("state_valid_mask", []), row.get("state_timestamps_ns", [])
    if len(mask) != 16 or len(stamps) != 16 or not all(mask[i] for i in RIGHT):
        return "right_state_missing"
    if any(not isinstance(stamps[i], int) or not 0 <= decision_ns - stamps[i] <= cfg["max_state_age_ns"] for i in RIGHT):
        return "right_state_stale_or_future"
    if frame is None or frame["observation_id"] != row["observation_id"]:
        return "payload_missing"
    if frame["state16"] != row["state16"] or frame["available_monotonic_ns"] != available:
        return "payload_observation_mismatch"
    cameras = {f["camera"]: f for f in frame["frames"]}
    if any(c not in cameras for c in CAMERAS):
        return "camera_missing"
    if any(not 0 <= decision_ns - cameras[c]["host_received_monotonic_ns"] <= cfg["max_camera_age_ns"] for c in CAMERAS):
        return "camera_stale_or_future"
    return None


def control_reason(row, epoch):
    if row.get("hil_state") != "policy" or row.get("source") not in ("policy", "hold"):
        return "non_policy_or_recovery_control"
    if row.get("policy_epoch") != epoch or row.get("intervention_id") != 0:
        return "epoch_or_intervention_boundary"
    if row.get("execution_mode") != "powered" or row.get("publication_complete") is not True or row.get("stage") != "completed":
        return "unpublished_control"
    action = row.get("submitted")
    if not isinstance(action, list) or len(action) != 16 or not all(math.isfinite(v) for v in action):
        return "invalid_submitted"
    mask, stamps = row.get("action_valid_mask", []), row.get("action_source_timestamps_ns", [])
    if len(mask) != 16 or len(stamps) != 16 or not all(mask[i] for i in RIGHT):
        return "missing_right_action"
    if any(not isinstance(stamps[i], int) or not 0 < stamps[i] <= row["monotonic_ns"] for i in RIGHT):
        return "noncausal_action_publication"
    return None


def select_segment(streams, payload, segment, cfg):
    observations, controls = streams["observations"], streams["control"]
    ot = [r["available_monotonic_ns"] for r in observations]
    ct = [r["monotonic_ns"] for r in controls]
    if any(b <= a for a, b in zip(ot, ot[1:])) or any(b <= a for a, b in zip(ct, ct[1:])):
        raise ValueError("Source clocks must increase strictly")
    frames = {r["observation_id"]: r for r in payload}
    origin = payload[0]["available_monotonic_ns"]
    start = origin + round(segment["start_s"] * 1e9)
    end = origin + round(segment["end_s"] * 1e9)
    count, fps = cfg["model"]["chunk_len"], cfg["fps"]
    span = round(count * 1e9 / fps)
    stride = round(cfg["anchor_stride_steps"] * 1e9 / fps)
    if end - start < span or end > min(ot[-1], ct[-1]):
        raise ValueError("Segment outside recorded valid time")
    # Include a complete final chunk ending exactly at the reviewed boundary.
    anchors = sorted(set([*range(start, end - span + 1, stride), end - span]))
    records, rejected = [], Counter()
    for t in anchors:
        next_t = t + span
        oi, ni = bisect.bisect_right(ot, t)-1, bisect.bisect_right(ot, next_t)-1
        if min(oi, ni) < 0:
            rejected["no_causal_observation"] += 1
            continue
        obs, nxt = observations[oi], observations[ni]
        reason = observation_reason(obs, frames.get(obs["observation_id"]), t, cfg)
        reason = reason or observation_reason(nxt, frames.get(nxt["observation_id"]), next_t, cfg)
        if not reason and (obs["capture_started_monotonic_ns"] < start or nxt["policy_epoch"] != obs["policy_epoch"]):
            reason = "observation_crosses_segment_or_epoch"
        first, last = bisect.bisect_right(ct, t)-1, bisect.bisect_right(ct, next_t)-1
        if not reason and (first < 0 or t-ct[first] > cfg["max_control_gap_ns"] or next_t-ct[last] > cfg["max_control_gap_ns"]):
            reason = "missing_or_stale_control"
        if not reason:
            # Inspect every 250Hz command, not only the resampled 30Hz labels.
            for k in range(first, last+1):
                reason = control_reason(controls[k], obs["policy_epoch"])
                if not reason and k > first and (ct[k]-ct[k-1] > cfg["max_control_gap_ns"] or
                    controls[k]["command_sequence"] != controls[k-1]["command_sequence"]+1):
                    reason = "control_sequence_or_time_gap"
                if reason:
                    break
        if reason:
            rejected[reason] += 1
            continue
        grid = [t + round(i * 1e9 / fps) for i in range(count)]
        selected = [controls[bisect.bisect_right(ct, g)-1] for g in grid]
        final = next_t == end
        outcome = segment["outcome"]
        if outcome not in ("success", "failure", "truncated"):
            raise ValueError("Unknown outcome cannot be rewarded")
        reward = [0.0]*count
        if final and outcome == "success":
            reward[-1] = 1.0
        records.append({"segment_id": segment["id"], "source_run": segment["source_run"],
            "split": segment["split"], "decision_ns": t, "next_decision_ns": next_t,
            "observation_id": obs["observation_id"], "next_observation_id": nxt["observation_id"],
            "state16": obs["state16"], "next_state16": nxt["state16"],
            "action_grid_ns": grid, "action_control_sequences": [c["stream_sequence"] for c in selected],
            "submitted16": [c["submitted"] for c in selected],
            "action_sources": [c["source"] for c in selected], "intervene_flags": [False]*count,
            "rewards": reward, "terminated": final and outcome != "truncated",
            "truncated": final and outcome == "truncated", "segment_outcome": outcome,
            "discount": cfg["gamma_per_step"]**count,
            "epoch": obs["policy_epoch"], "actual_publications_verified": True,
            "actuator_acceptance_verified": False,
            "action_space": "normalized_relative_eef10_from_submitted_joints_v1"})
    return records, {"segment": segment, "accepted": len(records), "rejected": dict(rejected),
        "terminal_transitions": sum(r["terminated"] for r in records),
        "truncated_transitions": sum(r["truncated"] for r in records),
        "success_rewards": sum(sum(r["rewards"]) for r in records)}
