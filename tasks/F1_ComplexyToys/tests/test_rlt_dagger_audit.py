"""Probe semantics and expert-reference metrics for zero-update candidate audit."""
from pathlib import Path
import sys

import numpy as np
import pytest

torch = pytest.importorskip("torch")
TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
sys.path.insert(0, str(TASK / "scripts"))
from audit_rlt_dagger import choose_probes, command_differences, reference_expert_baseline


def record(index, grips, *, terminated=False):
    return dict(segment_id="segment", source_run="source", split="validation",
        decision_ns=index*100, action_grid_ns=[index*100+i*10 for i in range(len(grips))],
        submitted16=[[0.0]*15+[grip] for grip in grips], terminated=terminated, truncated=False)


def test_probe_release_requires_observed_closed_then_open_command():
    records = [record(0, [100, 100]), record(1, [100, 0]), record(2, [0, 0], terminated=True)]
    probes, report = choose_probes(records)
    assert [p["index"] for p in probes] == [0, 1, 2]
    assert probes[1]["roles"] == ["recorded_opening_neighborhood"]
    assert report[0]["recorded_opening"]["first_open_command_ns"] == 110
    assert report[0]["last_is_true_terminal"]


def test_no_release_does_not_invent_one_or_turn_last_record_into_terminal():
    probes, report = choose_probes([record(i, [100, 100]) for i in range(3)])
    assert report[0]["recorded_opening"] is None
    assert probes[1]["roles"] == ["middle_no_observed_close_to_open_crossing"]
    assert not report[0]["last_is_true_terminal"]
    _, already_open = choose_probes([record(i, [0, 0]) for i in range(3)])
    assert already_open[0]["recorded_opening"] is None


def test_physical_command_error_uses_frozen_xyz_scale_and_gripper_semantics():
    candidate = np.zeros((2, 10)); target = np.zeros_like(candidate)
    candidate[:, :3] = [1, 2, 3]; candidate[:, 9] = 2
    metrics = command_differences(candidate, target, {"std": [.001, .002, .003]})
    assert metrics["xyz_axis_mae_mm"] == pytest.approx(14/3)
    assert metrics["xyz_l2_mean_mm"] == pytest.approx(np.sqrt(98))
    assert metrics["gripper_command_points_mae"] == 100


def test_b_reference_baseline_uses_expert_masks_and_episode_macro():
    reference = torch.zeros(3, 2, 10)
    data = dict(actions=torch.ones_like(reference),
                intervene_flags=torch.tensor([[True, True], [True, True], [False, False]]))
    data["actions"][2] = 1000
    records = [dict(source_run="train", split="train"),
               dict(source_run="expert_validation", split="validation"),
               dict(source_run="autonomous_validation", split="validation")]
    gripper = dict(physical_open=0, physical_closed=100, normalized_open=-1, normalized_closed=1)
    result = reference_expert_baseline(reference, data, records, {"std": [.001]*3}, gripper)
    held = result["validation"]
    assert held["metrics"]["expert_mse"] == 1
    assert held["metrics"]["expert_episode_macro_mse"] == 1
    assert held["metrics"]["expert_xyz_mm_mae"] == pytest.approx(1)
    assert len(held["expert_episodes"]) == 1
