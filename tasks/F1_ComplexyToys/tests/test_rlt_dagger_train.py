"""Expert supervision, immutable terminal targets, and bounded head selection."""
from pathlib import Path
import sys

import numpy as np
import pytest

torch = pytest.importorskip("torch")
TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
sys.path.insert(0, str(TASK / "scripts"))
from f1_toys.rlt_stage2 import actor_loss, bc_targets
from rlt_dagger_train import (action_metrics, discounted_rewards, evaluate,
                              monitor_state, monitor_update, normalization,
                              sample_indices, validate_replay)


GRIPPER = {"physical_open": 0, "physical_closed": 100,
           "normalized_open": -1, "normalized_closed": 1}


class TwinQ(torch.nn.Module):
    def __init__(self, size):
        super().__init__()
        self.linear = torch.nn.Linear(size, 2, bias=False)
        with torch.no_grad():
            self.linear.weight.zero_()
            self.linear.weight[:, 0] = 1

    def forward(self, state, action):
        return self.linear(action.flatten(1))


class SmallPolicy(torch.nn.Module):
    chunk_len = 2
    step_action_dim = 10

    def __init__(self):
        super().__init__()
        self.action = torch.nn.Parameter(torch.zeros(20))
        self.q_head = TwinQ(20)

    def _get_ref_chunk(self, obs):
        return obs["ref_chunk"][:, :2].flatten(1)

    def _critic_state(self, obs):
        return obs["z_rl"]

    def sac_forward(self, obs, **kwargs):
        self.last_reference = obs["ref_chunk"].clone()
        return self.action.expand(len(obs["z_rl"]), -1), None, None

    def sac_q_forward(self, obs, action):
        return self.q_head(self._critic_state(obs), action)


def observation(count=1):
    return {"z_rl": torch.zeros(count, 3), "proprio": torch.zeros(count, 2),
            "ref_chunk": torch.ones(count, 3, 10)}


def config():
    return {"model": {"chunk_len": 2, "action_dim": 10}, "gamma_per_step": 0.9,
            "warmup_steps": 300, "patience": 3, "batch_size": 32}


def replay():
    records = [dict(source_run="train", source_group="train-session", segment_id="training",
                    split="train", terminated=True, phase="release"),
               dict(source_run="heldout", source_group="independent-session", segment_id="heldout",
                    split="validation", terminated=True, phase="hold")]
    data = observation(4)
    data.update(actions=torch.zeros(2, 2, 10), intervene_flags=torch.ones(2, 2, dtype=torch.bool),
                current=torch.tensor([0, 1]), next=torch.tensor([2, 3]),
                rewards=torch.tensor([[0., 1.], [0., 0.]]),
                terminated=torch.tensor([True, True]), truncated=torch.tensor([False, False]),
                discount=torch.full((2,), .81))
    return data, records


def test_human_bc_replaces_target_without_replacing_current_b_reference():
    model = SmallPolicy()
    obs = observation()
    human = -torch.ones(1, 2, 10)
    mask = torch.tensor([[True, False]])
    loss, metrics = actor_loss(model, obs, q_weight=0, bc_weight=1,
                              reference_dropout_prob=0.5, human_action=human, human_mask=mask)
    loss.backward()
    assert torch.equal(model.last_reference, obs["ref_chunk"])
    assert (model.action.grad[:10] > 0).all()
    assert (model.action.grad[10:] < 0).all()
    assert metrics["actor/human_fraction"] == .5
    assert all(parameter.grad is None for parameter in model.q_head.parameters())


def test_default_bc_still_matches_legacy_reference_only():
    model, obs = SmallPolicy(), observation()
    loss, _ = actor_loss(model, obs, q_weight=0, bc_weight=5, reference_dropout_prob=0)
    assert float(loss) == 5.0
    target, flags = bc_targets(model, obs)
    assert torch.equal(target, obs["ref_chunk"][:, :2])
    assert not flags.any()
    with pytest.raises(ValueError, match="together"):
        bc_targets(model, obs, human_action=torch.zeros_like(target))


def test_mask_cannot_silently_broadcast_across_time():
    model, obs = SmallPolicy(), observation()
    with pytest.raises(ValueError, match="Intervention mask"):
        bc_targets(model, obs, torch.zeros(1, 2, 10), torch.ones(1, dtype=torch.bool))


def test_source_sessions_cannot_be_split_and_real_expert_validation_required():
    data, records = replay()
    validate_replay(data, records, config())
    records[1]["source_group"] = "train-session"
    with pytest.raises(ValueError, match="source_group"):
        validate_replay(data, records, config())
    records[1]["source_group"] = "independent-session"
    data["intervene_flags"][1] = False
    with pytest.raises(ValueError, match="validation"):
        validate_replay(data, records, config())


def test_reward_target_uses_exact_position_not_always_last_frame():
    data, _ = replay()
    data["rewards"] = torch.tensor([[1., 0.], [0., 1.]])
    torch.testing.assert_close(discounted_rewards(data, config()), torch.tensor([[1.], [.9]]))


def test_terminal_error_is_fixed_and_counterfactual_q_is_separate():
    data, records = replay()
    model = SmallPolicy()
    data["actions"][:, 0, 0] = .25
    with torch.no_grad():
        model.action[0] = .75
    metrics, report = evaluate(model, model.q_head, data, records, config(),
                               {"mean": [0, 0, 0], "std": [1, 1, 1]}, GRIPPER, "bc")
    assert metrics["validation/terminal_failure_mae"] == pytest.approx(.25)
    assert metrics["train/terminal_success_mae"] == pytest.approx(.65)
    terminal = report["terminals"][1]
    assert terminal["fixed_target"] == 0
    assert terminal["actual_q"] == [.25, .25]
    assert terminal["actor_q"] == [.75, .75]
    assert terminal["reference_q"] == [1, 1]
    assert metrics["validation/terminal_success_count"] == 0


def test_hold_false_open_uses_expert_and_explicit_physical_encoding():
    expert = torch.zeros(1, 2, 10)
    expert[..., 9] = 1
    prediction = expert.clone()
    prediction[0, 0, 9] = -1
    # Opposite reference does not alter expert holding labels.
    reference = -torch.ones_like(expert)
    values = action_metrics(prediction, expert, reference, torch.ones(1, 2, dtype=torch.bool),
                            torch.ones(3), GRIPPER)
    assert values["expert_hold_steps"] == 2
    assert values["expert_false_open_rate"] == .5
    assert values["expert_gripper_physical_mae"] == 50
    with pytest.raises(ValueError, match="Gripper encoding"):
        normalization({"action_norm_stats": {"mean": [0, 0, 0], "std": [1, 1, 1]}}, {})


def metric(score, terminal=.2, bound=0):
    return {"validation/expert_episode_macro_mse": score,
            "validation/terminal_macro_mae": terminal, "all/q_bound_violation_max": bound}


def test_bc_patience_respects_minimum_and_does_not_invent_percent_threshold():
    state, cfg = monitor_state(), config()
    monitor_update(state, metric(1), phase="bc", step=0, cfg=cfg, baseline=metric(1))
    for step in (50, 100, 150, 200, 250):
        _, reason = monitor_update(state, metric(1.1), phase="bc", step=step, cfg=cfg, baseline=metric(1))
        assert reason is None
    _, reason = monitor_update(state, metric(1.1), phase="bc", step=300, cfg=cfg, baseline=metric(1))
    assert reason == "expert_validation_patience"
    # A real sub-percent improvement resets patience; there is no 1% rule.
    improved, _ = monitor_update(state, metric(.999), phase="bc", step=350, cfg=cfg, baseline=metric(1))
    assert improved and state["nonimproving"] == 0


def test_q_stops_only_after_two_bad_checks_against_fixed_bc_baseline():
    state, cfg, base = monitor_state(), config(), metric(.1, terminal=.1)
    monitor_update(state, base, phase="q", step=0, cfg=cfg, baseline=base)
    assert monitor_update(state, metric(.2, .2), phase="q", step=50, cfg=cfg, baseline=base)[1] is None
    assert monitor_update(state, metric(.2, .2), phase="q", step=100, cfg=cfg, baseline=base)[1] == "q_fixed_diagnostics_degraded_twice"
    state = monitor_state()
    monitor_update(state, base, phase="q", step=0, cfg=cfg, baseline=base)
    # Better expert fit does not excuse increasing theoretically invalid Q values.
    assert monitor_update(state, metric(.05, .05, .2), phase="q", step=50, cfg=cfg, baseline=base)[1] is None
    assert monitor_update(state, metric(.04, .04, .2), phase="q", step=100, cfg=cfg, baseline=base)[1] == "q_fixed_diagnostics_degraded_twice"
    assert state["best_step"] == 0


def test_sampler_never_uses_validation_or_loses_rng_reproducibility():
    _, records = replay()
    first = sample_indices(records, np.random.default_rng(12), config())
    second = sample_indices(records, np.random.default_rng(12), config())
    assert first == second and set(first) == {0}
