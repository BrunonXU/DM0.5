from __future__ import annotations

import time

import numpy as np
import pytest

from g2teleop.g2_live_state import KinematicSnapshot
from g2teleop.live_ik_shadow import LiveIKShadowEngine
from g2teleop.protocol import IntentPacket


def _full_model(pin):
    model = pin.Model()
    axes = (pin.JointModelRZ, pin.JointModelRY, pin.JointModelRX)
    parent = 0
    parent_frame = 0
    for index in range(1, 6):
        parent = model.addJoint(
            parent,
            axes[(index - 1) % 3](),
            pin.SE3.Identity(),
            f"idx0{index}_body_joint{index}",
        )
        model.appendBodyToJoint(parent, pin.Inertia.Random(), pin.SE3.Identity())
        parent_frame = model.addJointFrame(parent, parent_frame)
    waist_parent = parent
    waist_parent_frame = parent_frame
    for side, prefix in (("l", "idx2"), ("r", "idx6")):
        parent = waist_parent
        parent_frame = waist_parent_frame
        for index in range(1, 8):
            parent = model.addJoint(
                parent,
                axes[(index - 1) % 3](),
                pin.SE3(np.eye(3), np.array([0.08, 0.0, 0.0])),
                f"{prefix}{index}_arm_{side}_joint{index}",
            )
            model.appendBodyToJoint(parent, pin.Inertia.Random(), pin.SE3.Identity())
            parent_frame = model.addJointFrame(parent, parent_frame)
        model.addFrame(
            pin.Frame(
                f"hand_{side}_base_link",
                parent,
                parent_frame,
                pin.SE3(np.eye(3), np.array([0.1, 0.0, 0.0])),
                pin.FrameType.OP_FRAME,
            )
        )
    model.lowerPositionLimit[:] = -3.0
    model.upperPositionLimit[:] = 3.0
    model.velocityLimit[:] = 1.0
    return model


def _packet(sequence: int, deadman: bool, axes=(0.0, 0.0, 0.0)) -> IntentPacket:
    return IntentPacket(
        session_id="test",
        sequence=sequence,
        sent_at_unix_ns=time.time_ns(),
        deadman=deadman,
        active_side="left",
        mode="translation",
        axes=axes,
        grip_velocity=0.0,
        thumb_clearance=False,
    )


def test_live_shadow_initializes_then_computes_virtual_candidate() -> None:
    pin = pytest.importorskip("pinocchio")
    pytest.importorskip("pink")
    model = _full_model(pin)
    joints = {
        "left": [f"idx2{i}_arm_l_joint{i}" for i in range(1, 8)],
        "right": [f"idx6{i}_arm_r_joint{i}" for i in range(1, 8)],
    }
    engine = LiveIKShadowEngine(pin, model, joints)
    arm_q = np.array([0.2, -0.4, 0.3, -0.5, 0.2, 0.4, -0.1])
    snapshot = KinematicSnapshot(0, 1.0, np.zeros(5), np.r_[arm_q, arm_q])
    initialized = engine.process(_packet(0, True), snapshot, now=1.01)
    assert initialized.state == "SHADOW_ACTIVE"
    assert initialized.reason == "takeover_initialized"

    moved = engine.process(
        _packet(1, True, axes=(0.2, 0.0, 0.0)), snapshot, now=1.03
    )
    assert moved.state == "SHADOW_ACTIVE"
    assert moved.candidate_arms14 is not None
    assert np.max(np.abs(moved.candidate_arms14[:7] - arm_q)) > 0.0
    np.testing.assert_allclose(moved.candidate_arms14[7:], arm_q)


def test_live_shadow_holds_on_stale_state() -> None:
    pin = pytest.importorskip("pinocchio")
    pytest.importorskip("pink")
    model = _full_model(pin)
    joints = {
        "left": [f"idx2{i}_arm_l_joint{i}" for i in range(1, 8)],
        "right": [f"idx6{i}_arm_r_joint{i}" for i in range(1, 8)],
    }
    engine = LiveIKShadowEngine(pin, model, joints)
    snapshot = KinematicSnapshot(0, 1.0, np.zeros(5), np.zeros(14))
    result = engine.process(_packet(0, True), snapshot, now=1.2)
    assert result.state == "HOLD"
    assert result.reason == "stale_state"
