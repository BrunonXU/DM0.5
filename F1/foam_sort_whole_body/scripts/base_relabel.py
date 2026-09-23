#!/usr/bin/env python3
"""Build an offline, explicitly incomplete chassis-label candidate sidecar.

Input phases use original MCAP-relative seconds; time-npz holds absolute int64
nanoseconds. All intervals are [start, end), including the final interval. A
sample exactly at source_duration_s is unknown unless a valid later interval
exists (which this source contract disallows). Unknown is NaN, never zero.

Motion uses the derivative of cubic smoothstep: v = d * 6*u*(1-u)/T.
This is a displacement/profile prior, not measured odometry or an acceptance
claim. dx/dy are integrals of body-frame command components; with simultaneous
yaw they are not world-frame displacements. No ROS is imported or published.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

import numpy as np

PHASE_SCHEMA = "f1_base_phase_review_v1"
COORDINATES = "+x forward, +y left"
REPORT_SCHEMA = "f1_base_relabel_report_v1"
MANIFEST_SCHEMA = "f1_base_relabel_manifest_v1"
NPZ_NAME = "base_relabel.npz"
REPORT_NAME = "report.json"
MANIFEST_NAME = "manifest.json"
NS_PER_SECOND = 1_000_000_000
INT64_MAX = int(np.iinfo(np.int64).max)
ALGORITHM = {
    "name": "explicit_phase_cubic_smoothstep_derivative",
    "version": 1,
    "interval": "[start_ns, end_ns), including the final segment",
    "seconds_quantization": "nearest nanosecond, ties to even; never extend a window",
    "unknown": "NaN velocities, candidate_present=false, timing_reviewed=false, segment_index=-1",
    "frame": "body-frame vx/vy and angular wz; integrated components, not world-frame poses",
    "profile": "u=(t-start)/T; v=displacement*6*u*(1-u)/T",
    "training_approved": False,
}


class RelabelError(ValueError):
    """A violated data contract or an output that must not be reused."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_info(path: Path, data: bytes | None = None) -> dict:
    if data is None:
        data = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": _sha(data), "size_bytes": len(data)}


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RelabelError(f"{field} must be a finite JSON number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise RelabelError(f"{field} must be finite") from exc
    if not math.isfinite(result):
        raise RelabelError(f"{field} must be finite")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RelabelError(f"{field} must be a nonempty string")
    return value


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    return False


def _seconds_ns(value: Any) -> int:
    return int((Decimal(str(value)) * NS_PER_SECOND).to_integral_value(rounding=ROUND_HALF_EVEN))


def validate_phases(phase: dict) -> tuple[list[dict], int, int]:
    """Validate before writing; return normalized segments, origin, duration_ns.

    Segment indices always refer to the original input list. Temporal sorting
    is used only for overlap/coverage checks, so input ordering is not a hidden
    requirement. An evidence string/object/list is required for stationary
    intervals and reviewed intervals; reviewed also needs an explicit reviewer.
    """
    if not isinstance(phase, dict) or phase.get("schema") != PHASE_SCHEMA:
        raise RelabelError(f"phase schema must be {PHASE_SCHEMA}")
    try:
        _canonical_json(phase)
    except (ValueError, TypeError) as exc:
        raise RelabelError("phase JSON must contain only finite JSON values") from exc
    _text(phase.get("episode"), "episode")
    origin_text = phase.get("source_origin_ns")
    if not isinstance(origin_text, str) or not re.fullmatch(r"0|[1-9][0-9]*", origin_text):
        raise RelabelError("source_origin_ns must be a nonnegative integer encoded as a string")
    origin = int(origin_text)
    duration_s = _number(phase.get("source_duration_s"), "source_duration_s")
    duration_ns = _seconds_ns(duration_s)
    if duration_s <= 0 or duration_ns <= 0 or origin > INT64_MAX - duration_ns:
        raise RelabelError("source duration must be positive and its absolute endpoint fit int64")
    if phase.get("coordinate_convention") != COORDINATES:
        raise RelabelError(f"coordinate_convention must be {COORDINATES!r}")
    if not isinstance(phase.get("segments"), list):
        raise RelabelError("segments must be a list (an empty list explicitly means all unknown)")
    normalized, identifiers = [], set()
    for index, segment in enumerate(phase["segments"]):
        label = f"segments[{index}]"
        if not isinstance(segment, dict):
            raise RelabelError(f"{label} must be an object")
        sid = _text(segment.get("id"), f"{label}.id")
        if sid in identifiers:
            raise RelabelError(f"duplicate segment id: {sid}")
        identifiers.add(sid)
        cycle = segment.get("cycle_id")
        if not ((isinstance(cycle, str) and cycle.strip()) or
                (type(cycle) is int and cycle >= 0)):
            raise RelabelError(f"{label}.cycle_id must be a nonempty string or nonnegative integer")
        if segment.get("kind") not in ("motion", "stationary"):
            raise RelabelError(f"{label}.kind must be motion or stationary")
        start = _number(segment.get("start_s"), f"{label}.start_s")
        end = _number(segment.get("end_s"), f"{label}.end_s")
        if not 0 <= start < end <= duration_s:
            raise RelabelError(f"{label} requires 0 <= start_s < end_s <= source_duration_s")
        start_ns, end_ns = _seconds_ns(start), _seconds_ns(end)
        if not 0 <= start_ns < end_ns <= duration_ns:
            raise RelabelError(f"{label} collapses or leaves the recording at nanosecond resolution")
        displacement = [_number(segment.get(key), f"{label}.{key}")
                        for key in ("dx_m", "dy_m", "dyaw_rad")]
        if segment["kind"] == "stationary" and any(value != 0 for value in displacement):
            raise RelabelError(f"{label} stationary requires exactly zero displacement on all axes")
        if segment["kind"] == "motion" and not any(value != 0 for value in displacement):
            raise RelabelError(f"{label} motion requires a nonzero explicit displacement")
        timing = segment.get("timing_status")
        if timing not in ("proposed", "reviewed"):
            raise RelabelError(f"{label}.timing_status must be proposed or reviewed")
        evidence = segment.get("evidence")
        if (segment["kind"] == "stationary" or timing == "reviewed") and not _has_content(evidence):
            raise RelabelError(f"{label} requires nonempty evidence for stationary/reviewed timing")
        reviewer = segment.get("reviewer", phase.get("reviewer"))
        if timing == "reviewed":
            _text(reviewer, f"{label}.reviewer (or top-level reviewer)")
        if not _has_content(segment.get("distance_provenance")):
            raise RelabelError(f"{label}.distance_provenance must state the displacement source")
        normalized.append({**segment, "input_index": index, "start_ns": start_ns, "end_ns": end_ns,
                           "displacement": displacement, "reviewer": reviewer})
    previous = None
    for segment in sorted(normalized, key=lambda item: item["start_ns"]):
        if previous and (segment["start_ns"] < previous["end_ns"] or
                         segment["start_s"] < previous["end_s"]):
            raise RelabelError(f"overlapping segments: {previous['id']} and {segment['id']}")
        previous = segment
    return normalized, origin, duration_ns


def _unknown_intervals(segments: list[dict], duration_ns: int) -> list[dict]:
    intervals, cursor = [], 0
    for segment in sorted(segments, key=lambda item: item["start_ns"]):
        if cursor < segment["start_ns"]:
            intervals.append({"start_s": cursor / NS_PER_SECOND,
                              "end_s": segment["start_ns"] / NS_PER_SECOND})
        cursor = segment["end_ns"]
    if cursor < duration_ns:
        intervals.append({"start_s": cursor / NS_PER_SECOND, "end_s": duration_ns / NS_PER_SECOND})
    return intervals


def build_candidate(phase: dict, times: np.ndarray) -> tuple[dict[str, np.ndarray], dict]:
    """Pure numeric entry point. No source mutations, output IO, or robot actions."""
    segments, origin, duration_ns = validate_phases(phase)
    times = np.asarray(times)
    if times.ndim != 1 or times.dtype.kind != "i" or times.dtype.itemsize != 8 or not len(times):
        raise RelabelError("time array must be a nonempty one-dimensional signed int64 nanosecond array")
    times = times.astype(np.int64, copy=True)
    if int(times[0]) < origin or int(times[-1]) > origin + duration_ns:
        raise RelabelError("time array must lie within the original MCAP source interval")
    if np.any(times[1:] <= times[:-1]):
        raise RelabelError("time array must be strictly increasing without duplicate timestamps")
    relative_ns = times - np.int64(origin)
    arrays = {
        "time_ns": times,
        "base_candidate_v": np.full((len(times), 3), np.nan, dtype=np.float64),
        "base_candidate_present": np.zeros(len(times), dtype=np.bool_),
        "base_timing_reviewed": np.zeros(len(times), dtype=np.bool_),
        "segment_index": np.full(len(times), -1, dtype=np.int32),
    }
    records, reasons = [], []
    if not segments:
        reasons.append({"code": "no_segments", "message": "No explicit segments; every source time remains unknown."})
    for segment in segments:
        selection = (relative_ns >= segment["start_ns"]) & (relative_ns < segment["end_ns"])
        delta = np.asarray(segment["displacement"], dtype=np.float64)
        duration = (segment["end_ns"] - segment["start_ns"]) / NS_PER_SECOND
        u = (relative_ns[selection] - segment["start_ns"]).astype(np.float64) / (segment["end_ns"] - segment["start_ns"])
        velocity = 6.0 * u[:, None] * (1.0 - u[:, None]) * delta[None, :] / duration
        peak = 1.5 * np.abs(delta) / duration
        peak_acceleration = 6.0 * np.abs(delta) / (duration * duration)
        if not (np.isfinite(velocity).all() and np.isfinite(peak).all() and np.isfinite(peak_acceleration).all()):
            raise RelabelError(f"segment {segment['id']} has nonfinite derived velocity or acceleration")
        arrays["base_candidate_v"][selection] = velocity
        arrays["base_candidate_present"][selection] = True
        arrays["base_timing_reviewed"][selection] = segment["timing_status"] == "reviewed"
        arrays["segment_index"][selection] = segment["input_index"]
        # This integration uses declared analytic zero-velocity boundary anchors
        # and only this segment's actual samples. It never bridges unknown gaps.
        integration_t = np.concatenate(([0.0], u * duration, [duration]))
        integration_v = np.vstack((np.zeros(3), velocity, np.zeros(3)))
        integral = np.sum((integration_v[1:] + integration_v[:-1]) *
                          (np.diff(integration_t)[:, None] / 2.0), axis=0)
        count = int(np.count_nonzero(selection))
        records.append({
            "id": segment["id"], "input_index": segment["input_index"], "cycle_id": segment["cycle_id"],
            "kind": segment["kind"], "start_s": segment["start_ns"] / NS_PER_SECOND,
            "end_s": segment["end_ns"] / NS_PER_SECOND, "duration_s": duration,
            "timing_status": segment["timing_status"], "reviewer": segment["reviewer"],
            "evidence": segment.get("evidence"), "distance_provenance": segment["distance_provenance"],
            "sample_count": count, "displacement_dx_dy_dyaw": delta.tolist(),
            "analytic_integral_dx_dy_dyaw": delta.tolist(),
            "boundary_augmented_trapezoid_integral_dx_dy_dyaw": integral.tolist(),
            "boundary_augmented_trapezoid_absolute_error": np.abs(integral - delta).tolist(),
            "analytic_peak_abs_vx_vy_wz": peak.tolist(),
            "analytic_peak_linear_speed_mps": float(1.5 * math.hypot(float(delta[0]), float(delta[1])) / duration),
            "analytic_peak_abs_ax_ay_alpha": peak_acceleration.tolist(),
            "analytic_peak_linear_acceleration_mps2": float(6.0 * math.hypot(float(delta[0]), float(delta[1])) / duration**2),
        })
        if segment["timing_status"] != "reviewed":
            reasons.append({"code": "timing_not_reviewed", "segment_id": segment["id"]})
        if segment["kind"] == "motion":
            reasons.append({"code": "velocity_profile_is_a_prior", "segment_id": segment["id"],
                            "message": "Displacement provenance is preserved, not independently verified; cubic speed is not measured."})
            if count < 3:
                reasons.append({"code": "motion_segment_undersampled", "segment_id": segment["id"], "sample_count": count})
    intervals = _unknown_intervals(segments, duration_ns)
    unknown_count = int(np.count_nonzero(~arrays["base_candidate_present"]))
    if intervals or unknown_count:
        reasons.append({"code": "unknown_timing_coverage", "sample_count": unknown_count,
                        "intervals": intervals, "message": "Unknown times are not stationary labels."})
    reasons.extend([
        {"code": "f1_execution_limits_not_evaluated", "message": "No velocity/acceleration clipping or hardware acceptance is implied."},
        {"code": "full_19d_acceptance_not_evaluated", "message": "Candidate timing review does not verify all action channels, cameras, masks or replay."},
    ])
    diffs = np.diff(relative_ns).astype(np.float64) / NS_PER_SECOND
    report = {
        "schema": REPORT_SCHEMA, "episode": phase["episode"], "training_approved": False,
        "source_origin_ns": str(origin), "source_duration_s": phase["source_duration_s"],
        "coordinate_convention": COORDINATES, "algorithm": ALGORITHM,
        "time_axis": {
            "units": "absolute int64 nanoseconds", "input_first_ns": str(int(times[0])),
            "input_last_ns": str(int(times[-1])), "interval_semantics": ALGORITHM["interval"],
            "endpoint_samples": int(np.count_nonzero(relative_ns == duration_ns)),
            "endpoint_note": "A sample at the recording endpoint remains unknown; no last-action repeat or inferred zero.",
            "grid_step_s": ({"min": float(diffs.min()), "median": float(np.median(diffs)), "max": float(diffs.max())}
                            if len(diffs) else None),
        },
        "counts": {"total_samples": len(times), "candidate_present": int(arrays["base_candidate_present"].sum()),
                   "timing_reviewed": int(arrays["base_timing_reviewed"].sum()), "unknown": unknown_count,
                   "motion_segments": sum(s["kind"] == "motion" for s in segments),
                   "stationary_segments": sum(s["kind"] == "stationary" for s in segments)},
        "unknown_intervals": intervals, "segments": records, "unresolved_reasons": reasons,
        "integration_note": "Per-segment trapezoid uses actual sampled velocities plus analytic zero boundary anchors; it is not a measured displacement or integration across gaps.",
        "consumer_contract": "Use explicit candidate_present/timing_reviewed masks. Never nan_to_num unknown labels. This sidecar alone grants no training or robot execution approval.",
    }
    try:
        _canonical_json(report)
    except (ValueError, TypeError) as exc:
        raise RelabelError("derived report contains nonfinite or non-JSON values") from exc
    return arrays, report


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _check_cached(output: Path, identity: dict, identity_sha: str) -> dict | None:
    if not output.exists():
        return None
    if not output.is_dir() or output.is_symlink():
        raise RelabelError("output must be a real directory dedicated to one candidate version")
    if not any(output.iterdir()):
        return None
    try:
        manifest_path = output / MANIFEST_NAME
        if manifest_path.is_symlink():
            raise RelabelError("manifest must not be a symbolic link")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("status") != "complete" or
                manifest.get("training_approved") is not False or
                manifest.get("identity_sha256") != identity_sha or manifest.get("identity") != identity or
                _sha(_canonical_json(manifest["identity"])) != identity_sha):
            raise RelabelError("input/script/parameters changed; use a new output version")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {NPZ_NAME, REPORT_NAME}:
            raise RelabelError("artifact manifest is incomplete; use a new output version")
        for name, expected in artifacts.items():
            path = output / name
            if path.is_symlink() or not path.is_file():
                raise RelabelError(f"artifact {name} is missing or is a symlink; use a new output version")
            data = path.read_bytes()
            if expected != {"sha256": _sha(data), "size_bytes": len(data)}:
                raise RelabelError(f"artifact {name} checksum mismatch; use a new output version")
        report = json.loads((output / REPORT_NAME).read_text(encoding="utf-8"))
        if report.get("identity_sha256") != identity_sha or report.get("training_approved") is not False:
            raise RelabelError("cached report has an invalid identity or acceptance status")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, RelabelError):
            raise
        raise RelabelError(f"output is not a verified complete product; use a new output version ({exc})") from exc
    return {"status": "reused", "output": str(output), "identity_sha256": identity_sha,
            "training_approved": False, "counts": report["counts"]}


def run(phase_json: Path, time_npz: Path, output: Path, time_key: str = "time_ns") -> dict:
    """Build and atomically publish, or reuse only exactly verified artifacts.

    Computation/write failures occur in a unique sibling staging directory.
    They cannot mark a final result complete; a subsequent invocation retries.
    Existing completed/mismatching data is never overwritten or deleted.
    """
    phase_json, time_npz, output = Path(phase_json), Path(time_npz), Path(output)
    if output.is_symlink():
        raise RelabelError("output must not be a symbolic link")
    output = output.absolute()
    phase_bytes, npz_bytes = phase_json.read_bytes(), time_npz.read_bytes()
    try:
        phase = json.loads(phase_bytes)
    except (ValueError, UnicodeError) as exc:
        raise RelabelError(f"invalid phase JSON: {exc}") from exc
    validate_phases(phase)
    _text(time_key, "time_key")
    identity = {
        "phase_json": _file_info(phase_json, phase_bytes), "time_npz": _file_info(time_npz, npz_bytes),
        "script": _file_info(Path(__file__)), "parameters": {"time_key": time_key, "algorithm": ALGORITHM},
        "runtime": {"python": sys.version.split()[0], "numpy": np.__version__},
    }
    identity_sha = _sha(_canonical_json(identity))
    cached = _check_cached(output, identity, identity_sha)
    if cached:
        return cached
    try:
        with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as payload:
            if time_key not in payload.files:
                raise RelabelError(f"time npz has no array named {time_key!r}")
            times = payload[time_key]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, RelabelError):
            raise
        raise RelabelError(f"cannot read numeric time NPZ: {exc}") from exc
    arrays, report = build_candidate(phase, times)
    report.update({"identity_sha256": identity_sha, "identity": identity,
                   "created_at": datetime.now(timezone.utc).isoformat()})
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        np.savez_compressed(stage / NPZ_NAME, **arrays)
        npz_info = _file_info(stage / NPZ_NAME)
        report["candidate_npz"] = {"name": NPZ_NAME, "sha256": npz_info["sha256"], "size_bytes": npz_info["size_bytes"]}
        _write_json(stage / REPORT_NAME, report)
        artifacts = {}
        for name in (NPZ_NAME, REPORT_NAME):
            info = _file_info(stage / name)
            artifacts[name] = {"sha256": info["sha256"], "size_bytes": info["size_bytes"]}
        manifest = {"schema": MANIFEST_SCHEMA, "status": "complete", "identity_sha256": identity_sha,
                    "identity": identity, "artifacts": artifacts, "training_approved": False}
        _write_json(stage / MANIFEST_NAME, manifest)
        # A final directory is published only after all product hashes exist.
        try:
            os.rename(stage, output)
        except OSError:
            cached = _check_cached(output, identity, identity_sha)
            if cached:
                return cached
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"status": "created", "output": str(output), "identity_sha256": identity_sha,
            "training_approved": False, "counts": report["counts"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase-json", type=Path, required=True)
    parser.add_argument("--time-npz", type=Path, required=True)
    parser.add_argument("--time-key", default="time_ns")
    parser.add_argument("--output", type=Path, required=True, help="New dedicated candidate version directory")
    args = parser.parse_args()
    try:
        result = run(args.phase_json, args.time_npz, args.output, args.time_key)
    except (RelabelError, OSError) as exc:
        parser.exit(2, f"base_relabel: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
