"""Preserve surplus source frames when independent camera clocks drift."""
import hashlib
import json
from pathlib import Path

import numpy as np

from recording.upstream.lerobot_v3 import CAMERA_KEYS


def archive_frame(root, key, frame, reason):
    root = Path(root)
    folder = root / key
    folder.mkdir(parents=True, exist_ok=True)
    data, stamp, arrival, width, height, sequence, _ = frame
    path = folder / f"{sequence:012d}.jpg"
    with path.open("xb") as stream:
        stream.write(data)
    record = dict(camera=key, source_timestamp_ns=int(stamp),
                  arrival_timestamp_ns=int(arrival), sequence=int(sequence),
                  width=int(width), height=int(height), reason=str(reason),
                  file=str(path.relative_to(root)), sha256=hashlib.sha256(data).hexdigest())
    with (root / "extras.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    return record


def read_extras(root, verify=True):
    root = Path(root).resolve()
    manifest = root / "extras.jsonl"
    records = []
    if not manifest.exists():
        return records
    for line in manifest.read_text().splitlines():
        record = json.loads(line)
        if record["camera"] not in CAMERA_KEYS:
            raise ValueError("unknown alignment camera")
        path = (root / record["file"]).resolve()
        if root not in path.parents:
            raise ValueError("alignment image path escaped archive")
        if verify and hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("alignment image checksum mismatch")
        records.append(record)
    return records


def validate_coverage(sources, extras, max_gap_ns):
    """Require real images for every intermediate source timestamp, not padding."""
    for index, key in enumerate(CAMERA_KEYS):
        selected = np.asarray(sources[:, index], dtype=np.int64)
        if len(selected) > 1 and np.any(np.diff(selected) <= 0):
            raise ValueError(f"{key}: selected timestamps repeat or reverse")
        extra = [row["source_timestamp_ns"] for row in extras if row["camera"] == key]
        combined = np.sort(np.asarray(list(selected) + extra, dtype=np.int64))
        if len(combined) < 2:
            continue
        gaps = np.diff(combined)
        if np.any(gaps <= 0):
            raise ValueError(f"{key}: archived/source timestamps repeat")
        if int(np.max(gaps)) > max_gap_ns:
            raise ValueError(f"{key}: source coverage has a frame gap")
