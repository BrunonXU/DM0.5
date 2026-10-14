from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


pytest.importorskip("ruckig")


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts/15_replay_ruckig_otg.py"
    spec = importlib.util.spec_from_file_location("replay_ruckig_otg", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_preserves_limits_and_reduces_one_tick_reversals() -> None:
    module = _module()
    samples = []
    q = [0.0] * 7
    for sequence in range(120):
        velocity = 0.2 if sequence < 40 else (-0.2 if sequence < 80 else 0.0)
        q = q.copy()
        q[0] += velocity * 0.01
        samples.append(
            {
                "event": "f710_control_sample",
                "enabled": True,
                "ik_solved_this_tick": True,
                "active_side": "left",
                "control_sequence": sequence,
                "ik_q_ref_rad": q,
                "ik_dq_ref_rad_s": [velocity] + [0.0] * 6,
                "joint_lower_margin_rad": [2.0 + value for value in q],
                "joint_upper_margin_rad": [2.0 - value for value in q],
                "arm_command_rad": q + [0.0] * 7,
            }
        )
    report = module.replay(
        samples,
        "phase",
        control_period_s=0.01,
        max_velocity_rad_s=0.35,
        max_acceleration_rad_s2=2.0,
        max_jerk_rad_s3=20.0,
    )
    assert report["samples"] == 120
    assert report["segments"] == 1
    assert report["max_output_velocity_rad_s"] <= 0.35 + 1e-6
    assert report["max_output_acceleration_rad_s2"] <= 2.0 + 1e-6
    assert report["max_output_jerk_rad_s3"] <= 20.0 + 1e-5
