"""Reviewed schema3 human corrections for offline C16 replay.

Preserves the behavior-policy identity and actual publication provenance. Local
success requires a separately inspected observation before native Home; Home and
episode-save events alone never imply a successful RL terminal.
"""
from __future__ import annotations

import bisect
from collections import Counter
import json
import math
from pathlib import Path

from f1_toys.rlt_replay import CAMERAS, RIGHT, contained, digest, rows

HUMAN_SOURCES = {"human_arm", "human_hand", "human_hold"}


def audit_human_raw(root, review_path):
    """Verify immutable raw bytes and the approved review; no schema mutation."""
    root, review_path = Path(root), Path(review_path)
    manifest = json.loads((root / "journal/manifest.json").read_text())
    payload = json.loads((root / "payload/manifest.json").read_text())
    review = json.loads(review_path.read_text())
    source_sha = digest(root / "journal/manifest.json")
    for doc in (manifest, payload):
        if not doc["complete"] or doc["drop_count"] != 0 or doc["writer_error"]:
            raise ValueError("Incomplete/dropped human recording")
    if manifest["schema_version"] != 3 or manifest["contract"] != "f1_upper_16d_v1":
        raise ValueError("Expected schema3 physical full16 contract")
    if manifest["episode_id"] != root.name or manifest["action_contract"]["dimension"] != 16:
        raise ValueError("Raw identity/action contract mismatch")
    binding = manifest["binding"]
    if binding["execution_mode"] != "powered" or not binding.get("rtc_hil") or not binding.get("model_id"):
        raise ValueError("Expected real RTC-HIL behavior identity")
    if review.get("manifest_sha256") != source_sha or review.get("contract") != manifest["contract"]:
        raise ValueError("Review is not bound to this raw manifest")
    if review.get("human_review_accepted") is not True or not review.get("accepted_interventions"):
        raise ValueError("Human correction lacks approved intervention")
    if not review.get("source_group") or not review.get("accepted_time_ranges_ns"):
        raise ValueError("Review lacks source group or time range")
    if binding.get("process_run_id", review["source_group"]) != review["source_group"]:
        raise ValueError("Review source group differs from recorded process")
    verified = {}
    required = {"observations", "control", "events", "policy_chunks"}
    if not required <= manifest["stream_sha256"].keys() or not required <= manifest["counts"].keys():
        raise ValueError("Missing raw streams")
    for stream, expected in manifest["stream_sha256"].items():
        path = contained(root / "journal", stream + ".jsonl")
        if digest(path) != expected:
            raise ValueError("Journal digest mismatch: " + stream)
        verified[str(path.relative_to(root))] = expected
    for name, expected in manifest["payload_sha256"].items():
        if digest(contained(root / "payload", name)) != expected:
            raise ValueError("Payload digest mismatch")
        verified["payload/" + name] = expected
    for info in payload["pack_files"].values():
        path = contained(root / "payload", info["path"])
        if path.stat().st_size != info["bytes"] or digest(path) != info["sha256"]:
            raise ValueError("Image pack digest mismatch")
        verified["payload/" + info["path"]] = info["sha256"]
    streams = {name: rows(contained(root / "journal", name + ".jsonl")) for name in manifest["counts"]}
    for name, expected in manifest["counts"].items():
        if len(streams[name]) != expected or any(r["episode_id"] != root.name or r["schema_version"] != 3 for r in streams[name]):
            raise ValueError("Raw count/schema/episode mismatch")
    frames = rows(root / "payload/frames.jsonl")
    if len(frames) != payload["count"] or len({f["observation_id"] for f in frames}) != len(frames):
        raise ValueError("Payload identity/count mismatch")
    return streams, frames, {"source_manifest_sha256": source_sha,
        "review_sha256": digest(review_path), "review": review,
        "files_sha256": verified, "model_id": binding["model_id"],
        "runtime_identity": binding["runtime_identity"], "source_group": review["source_group"],
        "raw_training_ready_unchanged": manifest["training_ready"]}


def _finite16(values):
    return isinstance(values, list) and len(values) == 16 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in values)


def publication_provenance(controls):
    """Map each actual joint/gripper publication to its persistent target."""
    result = {}
    for row in controls:
        if row.get("publication_complete") is not True or row.get("stage") != "completed" or row.get("execution_mode") != "powered":
            continue
        if not _finite16(row.get("submitted")):
            continue
        start, end = row.get("started_monotonic_ns"), row.get("completed_monotonic_ns")
        if not isinstance(start, int) or not isinstance(end, int) or not start <= end == row["monotonic_ns"]:
            continue
        for publication in row.get("publications", []):
            stamp = publication.get("monotonic_ns")
            if not isinstance(stamp, int) or not start <= stamp <= end or not publication.get("publisher_gid"):
                continue
            indices = range(7, 14) if publication.get("topic_key") == "joint_command" else (15,) if publication.get("topic_key") == "right_gripper_command" else ()
            for index in indices:
                key, value = (index, stamp), row["submitted"][index]
                if key in result and result[key] != value:
                    raise ValueError("Conflicting publication provenance")
                result[key] = value
    return result


def human_observation_reason(row, frame, decision_ns, cfg, intervention):
    if row.get("hil_state") != "hil_latched" or row.get("intervention_id") != intervention:
        return "observation_not_approved_human"
    available, capture = row["available_monotonic_ns"], row["capture_started_monotonic_ns"]
    if not capture <= available <= decision_ns or decision_ns - available > cfg["max_observation_age_ns"]:
        return "observation_stale_or_future"
    if row.get("camera_discontinuity"):
        return "camera_discontinuity"
    if not _finite16(row.get("state16")):
        return "state_nonfinite"
    stamps = row.get("state_timestamps_ns", [])
    if len(stamps) != 16 or any(not isinstance(stamps[i], int) or not 0 <= decision_ns-stamps[i] <= cfg["max_state_age_ns"] for i in RIGHT):
        return "right_state_stale_or_missing"
    # Schema3 declares full physical16. An explicitly supplied invalid mask is
    # still rejected; no fabricated schema4 mask is written to the recording.
    mask = row.get("state_valid_mask")
    if mask is not None and (len(mask) != 16 or not all(mask[i] is True for i in RIGHT)):
        return "right_state_invalid"
    if frame is None or frame["observation_id"] != row["observation_id"]:
        return "payload_missing"
    if frame["state16"] != row["state16"] or frame["available_monotonic_ns"] != available:
        return "payload_observation_mismatch"
    cameras = {f["camera"]: f for f in frame["frames"]}
    if any(c not in cameras for c in CAMERAS):
        return "camera_missing"
    if any(not 0 <= decision_ns-cameras[c]["host_received_monotonic_ns"] <= cfg["max_camera_age_ns"] for c in CAMERAS):
        return "camera_stale_or_future"
    return None


def human_control_reason(row, epoch, intervention, provenance):
    if row.get("hil_state") != "hil_latched" or row.get("source") not in HUMAN_SOURCES:
        return "non_human_or_home_control"
    if row.get("policy_epoch") != epoch or row.get("intervention_id") != intervention:
        return "epoch_or_intervention_boundary"
    if row.get("execution_mode") != "powered" or row.get("publication_complete") is not True or row.get("stage") != "completed":
        return "unpublished_control"
    start, end = row.get("started_monotonic_ns"), row.get("completed_monotonic_ns")
    if not isinstance(start, int) or not isinstance(end, int) or not start <= end == row["monotonic_ns"]:
        return "invalid_publication_completion"
    if not _finite16(row.get("submitted")):
        return "invalid_submitted"
    stamps = row.get("action_source_timestamps_ns", [])
    if len(stamps) != 16:
        return "missing_right_publication"
    mask = row.get("action_valid_mask")
    if mask is not None and (len(mask) != 16 or not all(mask[i] is True for i in RIGHT)):
        return "right_action_invalid"
    for i in RIGHT:
        stamp = stamps[i]
        if not isinstance(stamp, int) or not 0 < stamp <= end:
            return "noncausal_publication"
        if provenance.get((i, stamp)) != row["submitted"][i]:
            return "unverified_publication_target"
        if i != 15 and stamp < start:
            return "joint_target_not_published_this_tick"
    return None


def select_human_segment(streams, payload, segment, cfg, review):
    """Build C16 records only within an approved human recovery and endpoint."""
    if cfg["model"]["chunk_len"] != 16 or cfg["fps"] != 30:
        raise ValueError("This human replay adapter is audited for C16/30Hz")
    start, end = segment["start_ns"], segment["end_ns"]
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        raise ValueError("Expected exact monotonic bounds")
    if segment.get("source_group", review["source_group"]) != review["source_group"] or segment["split"] != review["split"]:
        raise ValueError("Review source group/split mismatch")
    if review.get("human_review_accepted") is not True or not any(a <= start < end <= b for a,b in review["accepted_time_ranges_ns"]):
        raise ValueError("Segment outside approved review")
    if any(a <= end and b >= start for a,b in review.get("environment_assistance_ranges_ns", [])):
        raise ValueError("Segment crosses environment assistance")
    phases = {p["stage"]: p for p in review["phase_ranges"]}
    local_objective = cfg.get('objective') == 'approach_place_release_v1'
    if local_objective:
        if start != phases['alignment_insertion']['start_ns'] or not phases['release']['start_ns'] <= end <= phases['withdrawal']['end_ns']:
            raise ValueError('Expected reviewed approach-to-release bounds')
        evidence = segment.get('terminal_evidence', {})
        if (evidence.get('placement_release_confirmed') is not True
                or not evidence.get('reviewer') or not evidence.get('evidence_refs')
                or evidence.get('available_monotonic_ns') != end):
            raise ValueError('Local placement requires a separately reviewed release endpoint')
    elif start != phases["alignment_insertion"]["start_ns"] or not phases["withdrawal"]["start_ns"] <= end <= phases["withdrawal"]["end_ns"]:
        raise ValueError("Expected approved human start and pre-Home withdrawal end")
    if len(review["accepted_interventions"]) != 1:
        raise ValueError("Expected one approved intervention per segment")
    intervention = review["accepted_interventions"][0]
    observations, controls = streams["observations"], streams["control"]
    if any(r.get("episode_id", segment["source_run"]) != segment["source_run"] for r in (*observations, *controls)):
        raise ValueError("Mixed source episode")
    ot, ct = [r["available_monotonic_ns"] for r in observations], [r["monotonic_ns"] for r in controls]
    if not ot or not ct or any(b <= a for a,b in zip(ot, ot[1:])) or any(b <= a for a,b in zip(ct, ct[1:])):
        raise ValueError("Source clocks must increase strictly")
    frames = {f["observation_id"]: f for f in payload}
    if len(frames) != len(payload):
        raise ValueError("Duplicate payload observation")
    count, fps = 16, 30
    span = round(count * 1e9 / fps)
    stride = round(cfg["anchor_stride_steps"] * 1e9 / fps)
    if stride <= 0 or end-start < span or end > min(ot[-1], ct[-1]):
        raise ValueError("Segment outside recorded C16 range")
    evidence = segment.get("terminal_evidence", {})
    outcome = segment.get("outcome")
    if outcome not in ("success", "failure", "truncated"):
        raise ValueError("Unknown outcome cannot be rewarded")
    confirmed_key = 'placement_release_confirmed' if local_objective else 'success_after_retreat_confirmed'
    if outcome == "success" and (review.get("task_success") is not True or evidence.get(confirmed_key) is not True or not evidence.get("evidence_refs") or evidence.get("available_monotonic_ns") != end):
        raise ValueError("Success requires inspected pre-Home terminal evidence")
    anchors = sorted(set([*range(start, end-span+1, stride), end-span]))
    provenance = publication_provenance(controls)
    records, rejected = [], Counter()
    for t in anchors:
        next_t = t+span
        oi, ni = bisect.bisect_right(ot,t)-1, bisect.bisect_right(ot,next_t)-1
        if min(oi,ni) < 0:
            rejected["no_causal_observation"] += 1
            continue
        obs, nxt = observations[oi], observations[ni]
        reason = human_observation_reason(obs,frames.get(obs["observation_id"]),t,cfg,intervention)
        reason = reason or human_observation_reason(nxt,frames.get(nxt["observation_id"]),next_t,cfg,intervention)
        if not reason and (obs["capture_started_monotonic_ns"] < start or nxt["policy_epoch"] != obs["policy_epoch"]):
            reason = "observation_crosses_segment_or_epoch"
        first,last = bisect.bisect_right(ct,t)-1,bisect.bisect_right(ct,next_t)-1
        if not reason and (first < 0 or t-ct[first] > cfg["max_control_gap_ns"] or next_t-ct[last] > cfg["max_control_gap_ns"]):
            reason = "missing_or_stale_control"
        if not reason:
            for k in range(first,last+1):
                reason = human_control_reason(controls[k],obs["policy_epoch"],intervention,provenance)
                if not reason and k > first and (ct[k]-ct[k-1] > cfg["max_control_gap_ns"] or controls[k]["command_sequence"] != controls[k-1]["command_sequence"]+1):
                    reason = "control_sequence_or_time_gap"
                if reason:
                    break
        final = next_t == end
        if not reason and final and outcome == "success" and (nxt["observation_id"] != evidence.get("observation_id") or nxt["available_monotonic_ns"] != end or nxt["state16"][15] > 1 or controls[last]["submitted"][15] > 1):
            reason = "terminal_observation_or_open_gripper_mismatch"
        if reason:
            rejected[reason] += 1
            continue
        grid = [t+round(i*1e9/fps) for i in range(count)]
        selected = [controls[bisect.bisect_right(ct,g)-1] for g in grid]
        reward = [0.0]*count
        if final and outcome == "success":
            reward[-1] = 1.0
        records.append({"segment_id":segment["id"],"source_run":segment["source_run"],
            "source_group":review["source_group"],"split":segment["split"],
            "decision_ns":t,"next_decision_ns":next_t,"observation_id":obs["observation_id"],
            "next_observation_id":nxt["observation_id"],"state16":obs["state16"],"next_state16":nxt["state16"],
            "action_grid_ns":grid,"action_control_sequences":[c["stream_sequence"] for c in selected],
            "submitted16":[c["submitted"] for c in selected],"action_sources":[c["source"] for c in selected],
            "intervene_flags":[True]*count,"rewards":reward,"terminated":final and outcome!="truncated",
            "truncated":final and outcome=="truncated","segment_outcome":outcome,
            "discount":cfg["gamma_per_step"]**count,"epoch":obs["policy_epoch"],
            "intervention_id":intervention,"actual_publications_verified":True,
            "actuator_acceptance_verified":False,"action_space":"normalized_relative_eef10_from_submitted_joints_v1"})
    return records,{"segment":segment,"accepted":len(records),"rejected":dict(rejected),
        "terminal_transitions":sum(r["terminated"] for r in records),
        "truncated_transitions":sum(r["truncated"] for r in records),
        "success_rewards":sum(sum(r["rewards"]) for r in records)}
