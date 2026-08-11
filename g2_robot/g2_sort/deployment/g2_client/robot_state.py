"""冻结34D布局的严格G2 measured-state读取器。

用途：从body和左右end-effector接口恢复训练合同一致的34D state。
输入：``get_joint_states``和``get_end_state``返回的带名字GDK结构。
输出：左臂7、右臂7、左手10、右手10顺序的finite float32数组。
方法：完全按关节名映射并检查缺失/重复/非有限；名字不明确时拒绝，不补0。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .contracts import ACTION_DIM, ObservationError
from .joint_layout import (
    LEFT_ARM_JOINT_NAMES,
    LEFT_HAND_JOINT_NAMES,
    RIGHT_ARM_JOINT_NAMES,
    RIGHT_HAND_JOINT_NAMES,
)


def _finite_position(entry: Mapping[str, Any], joint_name: str) -> float:
    for key in ("motor_position", "position"):
        if key in entry and entry[key] is not None:
            value = float(entry[key])
            if not np.isfinite(value):
                raise ObservationError(f"joint {joint_name} has non-finite {key}")
            return value
    raise ObservationError(f"joint {joint_name} has no measured position")


def _named_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    parallel_names: Sequence[str] | None = None,
    context: str,
) -> dict[str, Mapping[str, Any]]:
    names: list[str] = []
    mapped: dict[str, Mapping[str, Any]] = {}
    if parallel_names:
        if len(parallel_names) != len(entries):
            raise ObservationError(
                f"{context} names/state length mismatch: "
                f"{len(parallel_names)} != {len(entries)}"
            )
        iterator = zip(parallel_names, entries, strict=True)
    else:
        iterator = (
            (str(entry.get("name", "")).strip(), entry)
            for entry in entries
        )
    for raw_name, entry in iterator:
        name = str(raw_name).strip()
        if not name:
            raise ObservationError(f"{context} contains an unnamed joint")
        if name in mapped:
            raise ObservationError(f"{context} contains duplicate joint {name}")
        names.append(name)
        mapped[name] = entry
    if not names:
        raise ObservationError(f"{context} contains no named joint states")
    return mapped


def _read_expected(
    mapping: Mapping[str, Mapping[str, Any]],
    expected_names: Sequence[str],
    context: str,
) -> np.ndarray:
    missing = [name for name in expected_names if name not in mapping]
    if missing:
        raise ObservationError(f"{context} missing required joints: {missing}")
    return np.asarray(
        [_finite_position(mapping[name], name) for name in expected_names],
        dtype=np.float32,
    )


class StrictRobotStateReader:
    """Read all required joints without zero fills or ambiguous ordering."""

    def __init__(self, robot: Any) -> None:
        self._robot = robot

    def read(self) -> np.ndarray:
        try:
            body_payload = self._robot.get_joint_states()
        except Exception as exc:
            raise ObservationError(f"get_joint_states failed: {exc}") from exc
        if not isinstance(body_payload, Mapping):
            raise ObservationError("get_joint_states returned a non-mapping payload")
        body_entries = body_payload.get("states")
        if not isinstance(body_entries, Sequence) or isinstance(body_entries, (str, bytes)):
            raise ObservationError("get_joint_states payload has no states sequence")
        body_map = _named_entries(body_entries, context="body state")
        left_arm = _read_expected(body_map, LEFT_ARM_JOINT_NAMES, "left arm")
        right_arm = _read_expected(body_map, RIGHT_ARM_JOINT_NAMES, "right arm")

        try:
            end_payload = self._robot.get_end_state()
        except Exception as exc:
            raise ObservationError(f"get_end_state failed: {exc}") from exc
        if not isinstance(end_payload, Mapping):
            raise ObservationError("get_end_state returned a non-mapping payload")
        left_hand = self._read_hand(
            end_payload.get("left_end_state"),
            LEFT_HAND_JOINT_NAMES,
            "left hand",
        )
        right_hand = self._read_hand(
            end_payload.get("right_end_state"),
            RIGHT_HAND_JOINT_NAMES,
            "right hand",
        )
        state = np.concatenate((left_arm, right_arm, left_hand, right_hand))
        if state.shape != (ACTION_DIM,) or not np.all(np.isfinite(state)):
            raise ObservationError(
                f"assembled G2 policy state is invalid: shape={state.shape}"
            )
        return state.astype(np.float32, copy=False)

    @staticmethod
    def _read_hand(
        side_payload: Any,
        expected_names: Sequence[str],
        context: str,
    ) -> np.ndarray:
        if not isinstance(side_payload, Mapping):
            raise ObservationError(f"{context} payload is missing")
        entries = side_payload.get("end_states")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise ObservationError(f"{context} has no end_states sequence")
        parallel_names = side_payload.get("names")
        if parallel_names is not None and (
            not isinstance(parallel_names, Sequence)
            or isinstance(parallel_names, (str, bytes))
        ):
            raise ObservationError(f"{context} names is not a sequence")
        mapping = _named_entries(
            entries,
            parallel_names=parallel_names,
            context=context,
        )
        return _read_expected(mapping, expected_names, context)
