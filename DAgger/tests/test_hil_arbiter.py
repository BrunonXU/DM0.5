from __future__ import annotations

import numpy as np
import pytest

from g2dagger.hil_arbiter import HILActionArbiter
from g2dagger.hil_supervisor import HILSupervisor


def _actions():
    return np.zeros(34), np.ones(34), np.full(34, 2.0)


def test_policy_state_selects_policy_and_records_all_sources() -> None:
    supervisor = HILSupervisor()
    update = supervisor.observe_operator(
        session_id="s", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    hold, policy, human = _actions()
    selected = HILActionArbiter().select(
        update, hold_action=hold, policy_action=policy, human_action=human,
        policy_request_id=5, policy_action_index=7,
    )
    np.testing.assert_allclose(selected.command, policy)
    assert selected.control_source == "policy"
    assert selected.intervention_mask is False
    assert selected.telemetry["policy_proposal_34d"] == policy.tolist()
    assert selected.telemetry["human_candidate_34d"] == human.tolist()


def test_hil_arm_selects_full_human_command() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    update = supervisor.observe_operator(
        session_id="s", hil_toggle_id=1, rb_deadman=True,
        hand_motion_pending=False,
    )
    hold, policy, human = _actions()
    selected = HILActionArbiter().select(
        update, hold_action=hold, policy_action=policy, human_action=human,
        discarded_policy_suffix_steps=23,
    )
    np.testing.assert_allclose(selected.command, human)
    assert selected.intervention_mask is True
    assert selected.human_arm_mask is True
    assert selected.telemetry["discarded_policy_suffix_steps"] == 23


def test_hil_pause_and_handoff_select_hold_not_old_policy() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    paused = supervisor.observe_operator(
        session_id="s", hil_toggle_id=1, rb_deadman=False,
        hand_motion_pending=False,
    )
    hold, policy, human = _actions()
    selected = HILActionArbiter().select(
        paused, hold_action=hold, policy_action=policy, human_action=human,
    )
    np.testing.assert_allclose(selected.command, hold)
    assert selected.control_source == "human_hold"

    handoff = supervisor.observe_operator(
        session_id="s", hil_toggle_id=2, rb_deadman=False,
        hand_motion_pending=False,
    )
    selected = HILActionArbiter().select(
        handoff, hold_action=hold, policy_action=policy, human_action=human,
    )
    np.testing.assert_allclose(selected.command, hold)
    assert selected.telemetry["fresh_policy_required"] is True


def test_missing_action_for_active_owner_is_rejected() -> None:
    supervisor = HILSupervisor()
    update = supervisor.observe_operator(
        session_id="s", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    with pytest.raises(ValueError, match="requires policy_action"):
        HILActionArbiter().select(update, hold_action=np.zeros(34))
