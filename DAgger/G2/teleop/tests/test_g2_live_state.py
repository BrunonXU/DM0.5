from __future__ import annotations

import numpy as np
import pytest

from g2teleop.g2_live_state import (
    LEFT_ARM_JOINT_NAMES,
    RIGHT_ARM_JOINT_NAMES,
    WAIST_JOINT_NAMES,
    LiveStateError,
    parse_body_kinematics,
)


def _payload() -> dict[str, object]:
    names = WAIST_JOINT_NAMES + LEFT_ARM_JOINT_NAMES + RIGHT_ARM_JOINT_NAMES
    return {
        "states": [
            {"name": name, "motor_position": index * 0.01}
            for index, name in enumerate(names)
        ]
    }


def test_parse_body_kinematics_freezes_name_order() -> None:
    payload = _payload()
    payload["states"] = list(reversed(payload["states"]))
    snapshot = parse_body_kinematics(
        payload, sequence=3, sampled_at_monotonic=10.0
    )
    np.testing.assert_allclose(snapshot.waist, np.arange(5) * 0.01)
    np.testing.assert_allclose(snapshot.arms, np.arange(5, 19) * 0.01)
    assert snapshot.sequence == 3
    assert snapshot.age_seconds(10.02) == pytest.approx(0.02)


def test_parse_body_kinematics_rejects_missing_and_duplicate() -> None:
    payload = _payload()
    payload["states"].pop()
    with pytest.raises(LiveStateError, match="missing required"):
        parse_body_kinematics(payload, sequence=0, sampled_at_monotonic=1.0)

    payload = _payload()
    payload["states"].append(dict(payload["states"][0]))
    with pytest.raises(LiveStateError, match="duplicate"):
        parse_body_kinematics(payload, sequence=0, sampled_at_monotonic=1.0)

