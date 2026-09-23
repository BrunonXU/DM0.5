"""Portable replay data contract. This module needs only the Python standard library."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re


ARM_NAMES = tuple(f"arm_{side}_j{i}" for side in ("l", "r") for i in range(1, 8))
GRIPPER_NAMES = ("left_gripper_position", "right_gripper_position")
BODY_NAMES = tuple(f"body_j{i}" for i in range(1, 6))
BASE_NAMES = ("base_vx", "base_vy", "base_wz")
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FRAMES_BYTES = 256 * 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_FRAMES = 1_000_000
MAX_TIME_NS = 2**63 - 1


def expected_layout():
    """Hardware-valued data; no Placo transform, normalization or clipping."""
    return {
        "state": {"names": list(ARM_NAMES + GRIPPER_NAMES + BODY_NAMES),
                  "units": ["deg"] * 14 + ["gripper_0_open_100_closed"] * 2 + ["deg"] * 5},
        "action": {"names": list(ARM_NAMES + GRIPPER_NAMES + BASE_NAMES),
                   "units": ["deg"] * 14 + ["gripper_0_open_100_closed"] * 2 + ["m/s", "m/s", "rad/s"]},
    }


@dataclass(frozen=True)
class ReplayFrame:
    time_ns: int
    source_time_ns: int
    state: tuple
    action: tuple


@dataclass(frozen=True)
class ReplayBundle:
    frames: list
    duration_ns: int
    manifest: dict
    sha256: str
    path: Path


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def decode_json(data):
    """Reject JSON ambiguities that would change commands between readers."""
    try:
        return json.loads(data, object_pairs_hook=_unique_pairs, parse_constant=_bad_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc


def _integer(value, label, minimum=0, maximum=MAX_TIME_NS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a lowercase SHA256")
    return value


def _vector(value, length, label):
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label} must contain exactly {length} numbers")
    result = []
    for item in value:
        if type(item) not in (float, int):
            raise ValueError(f"{label} must contain finite numbers, not booleans")
        try:
            number = float(item)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{label} contains an unrepresentable number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} contains a non-finite number")
        result.append(number)
    return tuple(result)


def load_bundle(path):
    """Validate a candidate or replay_ready bundle without publishing any commands.

    ``duration_ns`` includes exactly the recorded final frame's hold interval,
    ending at the next source timestamp. ``sha256`` identifies manifest bytes.
    ``path`` is the resolved bundle directory. Checks establish file/contract
    integrity only; they do not grant execution acceptance.
    """
    path = Path(path).expanduser()
    manifest_path = path / "manifest.json" if path.is_dir() else path
    if manifest_path.name != "manifest.json" or manifest_path.is_symlink():
        raise ValueError("Use a bundle directory or a regular manifest.json")
    root = manifest_path.parent.resolve(strict=True)
    if not manifest_path.is_file():
        raise ValueError("Bundle manifest.json is missing")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest is too large")
    with manifest_path.open("rb") as stream:
        manifest_bytes = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest is too large")
    manifest = _object(decode_json(manifest_bytes), "manifest")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported replay schema_version")
    if manifest.get("kind") != "f1_joint_replay":
        raise ValueError("Unsupported bundle kind")
    if manifest.get("status") not in ("candidate", "replay_ready"):
        raise ValueError("Bundle status must be candidate or replay_ready")
    if manifest.get("layout") != expected_layout():
        raise ValueError("State/action names, order, units or layout differ from the F1 replay contract")
    duration = _integer(manifest.get("duration_ns"), "duration_ns", 1)
    source = _object(manifest.get("source"), "source")
    for field in ("cleaning_version", "episode", "source_relative_path", "mapping_reference"):
        _text(source.get(field), f"source.{field}")
    start_index = _integer(source.get("start_index"), "source.start_index")
    stop_index = _integer(source.get("stop_index_exclusive"), "source.stop_index_exclusive", 1)
    if stop_index <= start_index:
        raise ValueError("Source index range must be nonempty and half-open")
    for field in ("aligned_npz", "aligned_json"):
        info = _object(source.get(field), f"source.{field}")
        _text(info.get("path"), f"source.{field}.path")
        _integer(info.get("size_bytes"), f"source.{field}.size_bytes", 1)
        _sha(info.get("sha256"), f"source.{field}.sha256")
    timing = _object(manifest.get("timing"), "timing")
    if timing.get("reference") != "source_log_time":
        raise ValueError("Unsupported replay time reference")
    origin = _integer(timing.get("relative_origin_ns"), "timing.relative_origin_ns")
    source_end = _integer(timing.get("end_exclusive_source_time_ns"), "timing.end_exclusive_source_time_ns", 1)
    final_hold = _integer(timing.get("final_hold_ns"), "timing.final_hold_ns", 1)
    if source_end - origin != duration:
        raise ValueError("duration_ns must end at the recorded next-source boundary")
    frame_info = _object(manifest.get("frames"), "frames")
    if frame_info.get("path") != "frames.jsonl":
        raise ValueError("frames.path must be frames.jsonl inside the bundle")
    count = _integer(frame_info.get("count"), "frames.count", 1, MAX_FRAMES)
    if count != stop_index - start_index:
        raise ValueError("Frame count differs from the source index range")
    expected_size = _integer(frame_info.get("size_bytes"), "frames.size_bytes", 1, MAX_FRAMES_BYTES)
    expected_sha = _sha(frame_info.get("sha256"), "frames.sha256")
    frames_path = root / "frames.jsonl"
    if frames_path.is_symlink() or frames_path.resolve(strict=True).parent != root or not frames_path.is_file():
        raise ValueError("frames.jsonl must be a regular file inside the bundle")
    if frames_path.stat().st_size != expected_size:
        raise ValueError("frames.jsonl size mismatch")
    frames, digest, size = [], hashlib.sha256(), 0
    with frames_path.open("rb") as stream:
        while True:
            line = stream.readline(MAX_FRAME_BYTES + 1)
            if not line:
                break
            size += len(line)
            if len(line) > MAX_FRAME_BYTES or size > MAX_FRAMES_BYTES or len(frames) >= count:
                raise ValueError("Frame stream exceeds its declared bounds")
            if not line.endswith(b"\n"):
                raise ValueError("Every JSONL frame must end in a newline")
            digest.update(line)
            row = _object(decode_json(line), f"frame {len(frames)}")
            if set(row) != {"time_ns", "source_time_ns", "state", "action"}:
                raise ValueError("Unexpected frame fields")
            relative = _integer(row["time_ns"], "frame.time_ns")
            absolute = _integer(row["source_time_ns"], "frame.source_time_ns")
            if absolute - origin != relative or (not frames and relative != 0):
                raise ValueError("Frame time must be relative to the first source timestamp")
            if frames and relative <= frames[-1].time_ns:
                raise ValueError("Frame timestamps must be strictly increasing")
            if relative >= duration:
                raise ValueError("Frame timestamp must precede the exclusive end boundary")
            frames.append(ReplayFrame(relative, absolute,
                                      _vector(row["state"], 21, "state"),
                                      _vector(row["action"], 19, "action")))
    if len(frames) != count or size != expected_size or digest.hexdigest() != expected_sha:
        raise ValueError("frames.jsonl count, size or SHA256 mismatch")
    if duration - frames[-1].time_ns != final_hold:
        raise ValueError("Final hold must match exactly the last recorded source interval")
    return ReplayBundle(frames, duration, manifest, hashlib.sha256(manifest_bytes).hexdigest(), root)
