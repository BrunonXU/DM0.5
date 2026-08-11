"""Persist and validate O10 command-space snapshots without touching hardware.

Purpose
-------
Keep the latest successfully acknowledged 20D Split hand command so recovery
never has to reinterpret unreliable reboot-time feedback as a target. The
separately reviewed Home record uses the same strict schema.

Inputs / outputs
----------------
Input is one finite 20D O10 command plus run metadata. Output is a versioned
JSON record with the frozen joint order; loading rejects malformed or unsafe
records.

Method
------
Write a temporary file in the destination directory, fsync it, then atomically
replace a runtime record. Non-overwrite mode remains available for immutable
review records, but the runtime controller only replaces ``last_success``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .joint_layout import POLICY_JOINT_LIMITS, POLICY_JOINT_NAMES

HAND_COMMAND_SCHEMA_VERSION = 1
HAND_JOINT_NAMES = POLICY_JOINT_NAMES[14:]
HAND_LIMITS = POLICY_JOINT_LIMITS[14:]


def build_hand_command_record(
    positions: np.ndarray,
    *,
    kind: str,
    run_id: str,
    command_index: int,
    source: str = "split_runtime_successful_move_ee_pos",
) -> dict[str, Any]:
    """Build one unapproved command-space record after strict validation."""
    values = np.asarray(positions, dtype=np.float64)
    if values.shape != (20,) or not np.all(np.isfinite(values)):
        raise ValueError("O10 command snapshot must be finite 20D")
    if np.any(values < HAND_LIMITS[:, 0]) or np.any(values > HAND_LIMITS[:, 1]):
        raise ValueError("O10 command snapshot exceeds GDK 2.6.3 limits")
    if kind not in {"home_candidate", "last_success"}:
        raise ValueError(f"unsupported hand command record kind {kind!r}")
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    if command_index < 1:
        raise ValueError("command_index must be positive")
    if source not in {
        "split_runtime_successful_move_ee_pos",
        "hand_home_recovery_successful_move_ee_pos",
    }:
        raise ValueError(f"unsupported hand command source {source!r}")
    return {
        "schema_version": HAND_COMMAND_SCHEMA_VERSION,
        "kind": kind,
        "approved": False,
        "source": source,
        "run_id": run_id,
        "command_index": int(command_index),
        "wall_time": time.time(),
        "wall_time_utc": datetime.now(timezone.utc).isoformat(),
        "group": "dual_tool",
        "target_type": "o10_t2",
        "joint_names": list(HAND_JOINT_NAMES),
        "positions": values.tolist(),
    }


def validate_hand_command_record(payload: object) -> dict[str, Any]:
    """Return a normalized record or reject any contract mismatch."""
    if not isinstance(payload, dict):
        raise ValueError("hand command record must be a JSON object")
    if payload.get("schema_version") != HAND_COMMAND_SCHEMA_VERSION:
        raise ValueError("unsupported hand command schema version")
    if payload.get("group") != "dual_tool":
        raise ValueError("hand command group must be dual_tool")
    if payload.get("target_type") != "o10_t2":
        raise ValueError("hand command target_type must be o10_t2")
    if payload.get("source") not in {
        "split_runtime_successful_move_ee_pos",
        "hand_home_recovery_successful_move_ee_pos",
    }:
        raise ValueError("hand command source is not an acknowledged O10 command")
    if payload.get("kind") not in {"home_candidate", "last_success"}:
        raise ValueError("hand command kind is invalid")
    if not isinstance(payload.get("approved"), bool):
        raise ValueError("hand command approved must be a JSON boolean")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("hand command run_id is invalid")
    command_index = payload.get("command_index")
    if (
        not isinstance(command_index, int)
        or isinstance(command_index, bool)
        or command_index < 1
    ):
        raise ValueError("hand command command_index must be a positive integer")
    try:
        wall_time = float(payload["wall_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("hand command wall_time is invalid") from exc
    if not np.isfinite(wall_time) or wall_time <= 0:
        raise ValueError("hand command wall_time must be finite and positive")
    names = tuple(str(name) for name in payload.get("joint_names", []))
    if names != HAND_JOINT_NAMES:
        raise ValueError("hand command joint names/order do not match frozen O10")
    positions = np.asarray(payload.get("positions"), dtype=np.float64)
    if positions.shape != (20,) or not np.all(np.isfinite(positions)):
        raise ValueError("hand command positions must be finite 20D")
    if np.any(positions < HAND_LIMITS[:, 0]) or np.any(
        positions > HAND_LIMITS[:, 1]
    ):
        raise ValueError("hand command positions exceed GDK 2.6.3 limits")
    normalized = dict(payload)
    normalized["joint_names"] = list(HAND_JOINT_NAMES)
    normalized["positions"] = positions.tolist()
    normalized["approved"] = payload["approved"]
    normalized["wall_time"] = wall_time
    return normalized


def load_hand_command_record(path: Path) -> dict[str, Any]:
    """Load one strict UTF-8 JSON command record."""
    return validate_hand_command_record(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _write_temp(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    )
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def save_hand_command_record(
    path: Path,
    payload: dict[str, Any],
    *,
    overwrite: bool,
) -> bool:
    """Atomically save a validated record; return False if the path exists."""
    normalized = validate_hand_command_record(payload)
    temporary = _write_temp(path, normalized)
    try:
        if overwrite:
            os.replace(temporary, path)
            return True
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)
