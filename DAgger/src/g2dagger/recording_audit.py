"""Payload/index integrity audit; training eligibility remains deliberately false."""
import hashlib
import json
from pathlib import Path

from .recording_observation import CAMERAS, DeviceTimestamp, TimestampedFrame, TimestampedObservation


def audit_payload_archive(path):
    root = Path(path)
    errors, warnings = [], []
    previous = {}
    observation_ids = set()
    count = 0
    max_host_skew_ns = 0
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        if not manifest.get("complete") or manifest.get("drop_count") or manifest.get("writer_error"):
            errors.append("archive incomplete or writer loss")
        if manifest.get("prebuffer") and (manifest["prebuffer"]["capacity_evictions"] or manifest["prebuffer"]["oversize_drops"]):
            warnings.append("prebuffer capacity shortened requested context")
        last_available = -1
        with (root / "frames.jsonl").open() as index:
            for line in index:
                row = json.loads(line)
                if row["observation_id"] in observation_ids:
                    errors.append("duplicate observation ID")
                observation_ids.add(row["observation_id"])
                if row["available_monotonic_ns"] < last_available:
                    errors.append("observation clock regressed")
                last_available = row["available_monotonic_ns"]
                frames = []
                for frame in row.pop("frames"):
                    relative = Path(frame.pop("path"))
                    target = (root / relative).resolve()
                    if relative.is_absolute() or not target.is_relative_to(root.resolve()):
                        raise ValueError("payload path escapes archive")
                    payload = target.read_bytes()
                    if len(payload) != frame.pop("payload_bytes") or hashlib.sha256(payload).hexdigest() != frame.pop("payload_sha256"):
                        errors.append("payload size/hash mismatch")
                    if frame.pop("archive_frame_index") != count:
                        errors.append("archive frame index gap")
                    stamp = DeviceTimestamp(**frame.pop("timestamp"))
                    f = TimestampedFrame(timestamp=stamp, payload=payload, **frame)
                    old = previous.get(f.camera)
                    if old:
                        if f.frame_id <= old.frame_id:
                            errors.append(f"{f.camera}: duplicate/regressed frame ID")
                        elif f.frame_id != old.frame_id + 1:
                            warnings.append(f"{f.camera}: source frame ID gap")
                        if (stamp.unit, stamp.clock_domain) != (old.timestamp.unit, old.timestamp.clock_domain):
                            errors.append(f"{f.camera}: clock domain/unit changed")
                        elif stamp.value <= old.timestamp.value:
                            errors.append(f"{f.camera}: duplicate/regressed device timestamp")
                    previous[f.camera] = f
                    frames.append(f)
                obs = TimestampedObservation(frames=tuple(frames), **row)
                if not all(obs.measured_valid_34d):
                    warnings.append("missing/invalid measured feedback")
                receive = [f.host_received_monotonic_ns for f in frames]
                max_host_skew_ns = max(max_host_skew_ns, max(receive) - min(receive))
                count += 1
        if count != manifest["count"] or not count:
            errors.append("archive empty or count mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"invalid payload archive: {exc}")
    return dict(valid=not errors, training_ready=False, errors=errors,
                warnings=sorted(set(warnings)), count=count,
                max_host_receive_skew_ns=max_host_skew_ns,
                device_cross_camera_skew_ns=None,
                limitations=["host receive skew is not exposure skew", "no device/host clock mapping",
                             "native payload hash check does not prove video decode or training labels"])


def audit_journal_payload_links(journal_path, payload_path):
    """Require exact acquisition metadata agreement, including missing feedback."""
    errors = []
    try:
        with (Path(payload_path) / "frames.jsonl").open() as handle:
            payloads = {row["observation_id"]: row for row in map(json.loads, handle)}
        with (Path(journal_path) / "observations.jsonl").open() as handle:
            for row in map(json.loads, handle):
                source = row.get("acquisition")
                payload = payloads.get(row.get("payload_observation_id"))
                if source is None or payload is None:
                    errors.append("missing journal acquisition/payload reference")
                    continue
                cleaned = dict(payload)
                cleaned["frames"] = [{k: v for k, v in f.items() if k not in
                    ("path", "payload_sha256", "payload_bytes", "archive_frame_index")}
                    for f in payload["frames"]]
                if cleaned != source or row["measured_34d"] != source["measured_34d"]:
                    errors.append("journal/payload acquisition mismatch")
                if row["monotonic_ns"] != source["capture_started_monotonic_ns"]:
                    errors.append("journal/payload capture time mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"invalid journal/payload links: {exc}")
    return errors
