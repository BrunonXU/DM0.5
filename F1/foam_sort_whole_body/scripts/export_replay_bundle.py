#!/usr/bin/env python3
"""6000D: export one explicit valid aligned window, without approving robot execution."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "robot"))
from f1_replay.bundle import (MAX_FRAMES, MAX_FRAMES_BYTES, MAX_MANIFEST_BYTES,
                             MAX_TIME_NS, decode_json, expected_layout, load_bundle)


def _read_bounded(path, limit):
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"Source missing, not a regular file, or larger than {limit} bytes: {path}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"Source exceeds size bound: {path}")
    return content


def _fingerprint(path, content):
    return {"path": str(path.resolve()), "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def _integer(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_TIME_NS:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def export_bundle(aligned_npz, output, cleaning_version, start_index, stop_index, aligned_json=None):
    """Keep [start_index, stop_index); use time[stop_index] only as its end boundary.

    Every selected state/action mask must be valid. A missing next timestamp
    rejects the window: no frame deletion, gap compression or tail padding.
    Source hashes describe the exact NPZ/JSON bytes used, without reading MCAPs.
    """
    aligned_npz, output = Path(aligned_npz), Path(output)
    aligned_json = Path(aligned_json) if aligned_json is not None else aligned_npz.with_suffix(".json")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; use a new bundle directory")
    if not isinstance(cleaning_version, str) or not cleaning_version.strip():
        raise ValueError("An explicit cleaning_version is required")
    _integer(start_index, "start_index")
    _integer(stop_index, "stop_index", 1)
    if stop_index <= start_index:
        raise ValueError("Select a nonempty half-open source index range")
    npz_bytes = _read_bounded(aligned_npz, MAX_FRAMES_BYTES)
    json_bytes = _read_bounded(aligned_json, MAX_MANIFEST_BYTES)
    meta = decode_json(json_bytes)
    if not isinstance(meta, dict):
        raise ValueError("Aligned JSON must be an object")
    for field in ("episode", "source_relative_path"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise ValueError(f"Aligned JSON is missing {field}")
    if meta.get("state_dim") != 21 or meta.get("action_dim") != 19:
        raise ValueError("Aligned JSON must declare state_dim=21 and action_dim=19")
    for version_key in ("cleaning_version", "version"):
        if version_key in meta and meta[version_key] != cleaning_version:
            raise ValueError("Explicit cleaning_version differs from aligned JSON")
    if "layout" in meta and meta["layout"] != expected_layout():
        raise ValueError("Aligned JSON layout differs from the supported raw hardware contract")
    with zipfile.ZipFile(io.BytesIO(npz_bytes)) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_FRAMES_BYTES:
            raise ValueError("Expanded NPZ exceeds the supported single-episode size")
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("NPZ contains duplicate members")
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as pack:
        required = {"time_ns", "state", "action", "state_valid", "action_candidate_valid", "source_relative_path"}
        if not required.issubset(pack.files):
            raise ValueError(f"Aligned NPZ missing arrays: {sorted(required - set(pack.files))}")
        times = pack["time_ns"]
        if times.ndim != 1 or times.dtype.kind not in "iu" or len(times) < 2:
            raise ValueError("time_ns must be an integer vector with an end boundary")
        if np.any(times < 0) or np.any(times > MAX_TIME_NS) or np.any(times[1:] <= times[:-1]):
            raise ValueError("Source time_ns must be nonnegative, unique and strictly increasing int64 values")
        n = len(times)
        if type(meta.get("frames")) is not int or meta["frames"] != n:
            raise ValueError("Aligned JSON frame count differs from NPZ")
        if stop_index >= n:
            raise ValueError("stop_index must have a next source timestamp; unknown episode tail cannot be padded")
        if stop_index - start_index > MAX_FRAMES:
            raise ValueError("Selected window is too large")
        source_path = pack["source_relative_path"]
        if source_path.shape != () or source_path.dtype.kind not in "US" or str(source_path.item()) != meta["source_relative_path"]:
            raise ValueError("Aligned NPZ/JSON source identities differ")
        state, action = pack["state"], pack["action"]
        for name, values, width, mask_name in (("state", state, 21, "state_valid"),
                                                ("action", action, 19, "action_candidate_valid")):
            if values.shape != (n, width) or values.dtype not in (np.dtype("float32"), np.dtype("float64")):
                raise ValueError(f"{name} must be an (N, {width}) float32/float64 array")
            mask = pack[mask_name]
            if mask.shape != values.shape or mask.dtype.kind != "b":
                raise ValueError(f"{mask_name} must be a boolean array matching {name}")
            selected = slice(start_index, stop_index)
            if not np.all(mask[selected]):
                bad = np.argwhere(~mask[selected])[0]
                raise ValueError(f"Invalid {name} at source index {start_index + int(bad[0])}, column {int(bad[1])}; select a continuous valid window")
            if not np.isfinite(values[selected]).all():
                raise ValueError(f"Selected {name} contains non-finite values")
        for mask_name in ("future_signal_action_candidate_valid",):
            if mask_name in pack:
                mask = pack[mask_name]
                if mask.shape != (n,) or mask.dtype.kind != "b" or not mask[start_index:stop_index].all():
                    raise ValueError(f"Invalid selected {mask_name}; window cannot cross a masked boundary")
        selected_times = times[start_index:stop_index].copy()
        selected_state, selected_action = state[start_index:stop_index].copy(), action[start_index:stop_index].copy()
        origin, end = int(times[start_index]), int(times[stop_index])
    frames = []
    for stamp, state_row, action_row in zip(selected_times, selected_state, selected_action):
        frames.append({"time_ns": int(stamp) - origin, "source_time_ns": int(stamp),
                       "state": state_row.tolist(), "action": action_row.tolist()})
    frames_bytes = b"".join((json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8") for row in frames)
    if len(frames_bytes) > MAX_FRAMES_BYTES:
        raise ValueError("Exported JSONL exceeds the supported bundle size")
    manifest = {
        "schema_version": 1, "kind": "f1_joint_replay", "status": "candidate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "layout": expected_layout(), "duration_ns": end - origin,
        "timing": {"reference": "source_log_time", "relative_origin_ns": origin,
                   "end_exclusive_source_time_ns": end, "final_hold_ns": end - int(selected_times[-1])},
        "frames": {"path": "frames.jsonl", "count": len(frames), "size_bytes": len(frames_bytes),
                   "sha256": hashlib.sha256(frames_bytes).hexdigest()},
        "source": {"cleaning_version": cleaning_version, "episode": meta["episode"],
                   "source_relative_path": meta["source_relative_path"],
                   "aligned_npz": _fingerprint(aligned_npz, npz_bytes),
                   "aligned_json": _fingerprint(aligned_json, json_bytes),
                   "start_index": start_index, "stop_index_exclusive": stop_index,
                   "mapping_reference": "F1/foam_sort_whole_body/docs/action_interface_contract.md",
                   "mapping_meaning": "field_mapping_reference_only_not_execution_acceptance",
                   "numeric_transform": "none_raw_hardware_values_no_normalization_no_clipping",
                   "base_label_origin": "posthoc_weak_target_not_measured_base_motion"},
        "acceptance": {"robot_replay_verified": False, "training_reader_verified": False,
                       "note": "Structural export only; candidate status does not approve robot execution."},
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    output.mkdir(parents=True, exist_ok=False)
    try:
        with (output / "frames.jsonl").open("xb") as stream:
            stream.write(frames_bytes)
        with (output / "manifest.json.tmp").open("xb") as stream:
            stream.write(manifest_bytes)
        (output / "manifest.json.tmp").replace(output / "manifest.json")
        return load_bundle(output)
    except BaseException:
        # A failed export must never leave a completed manifest behind.
        (output / "manifest.json").unlink(missing_ok=True)
        (output / "manifest.json.tmp").unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aligned-npz", type=Path, required=True)
    parser.add_argument("--aligned-json", type=Path, help="Default: NPZ path with .json suffix")
    parser.add_argument("--cleaning-version", required=True)
    parser.add_argument("--start-index", type=int, required=True, help="Inclusive source frame index")
    parser.add_argument("--stop-index", type=int, required=True, help="Exclusive; this source timestamp must exist")
    parser.add_argument("--output", type=Path, required=True, help="A new directory; existing output is never overwritten")
    args = parser.parse_args(argv)
    try:
        bundle = export_bundle(args.aligned_npz, args.output, args.cleaning_version,
                               args.start_index, args.stop_index, args.aligned_json)
    except (ValueError, OSError, zipfile.BadZipFile, EOFError) as exc:
        parser.exit(2, f"Export refused: {exc}\n")
    print(json.dumps({"bundle": str(bundle.path), "status": bundle.manifest["status"],
                      "frames": len(bundle.frames), "duration_ns": bundle.duration_ns,
                      "manifest_sha256": bundle.sha256}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
