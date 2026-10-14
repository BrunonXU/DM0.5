from __future__ import annotations

from g2dagger.hil_supervisor import HILState, HILSupervisor


def test_only_start_toggle_enters_hil_and_rb_never_does() -> None:
    supervisor = HILSupervisor()
    anchored = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    assert anchored.state == HILState.POLICY.value

    rb_only = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=True,
        hand_motion_pending=False,
    )
    assert rb_only.state == HILState.POLICY.value
    assert rb_only.control_source == "policy"

    entered = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=1, rb_deadman=True,
        hand_motion_pending=False,
    )
    assert entered.transition == "policy_to_hil"
    assert entered.human_arm_active is True
    assert entered.intervention_id == 1


def test_rb_release_and_side_switch_pause_do_not_exit_hil() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=1, rb_deadman=True,
        hand_motion_pending=False,
    )
    released = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=1, rb_deadman=False,
        hand_motion_pending=False,
    )
    assert released.state == HILState.HIL_LATCHED.value
    assert released.control_source == "human_hold"


def test_exit_requires_released_rb_and_completed_hand_motion() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=1, rb_deadman=False,
        hand_motion_pending=False,
    )
    rb_blocked = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=2, rb_deadman=True,
        hand_motion_pending=False,
    )
    assert rb_blocked.transition == "hil_exit_rejected"
    assert rb_blocked.state == HILState.HIL_LATCHED.value

    hand_blocked = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=3, rb_deadman=False,
        hand_motion_pending=True,
    )
    assert hand_blocked.reason == "wait_for_human_hand_motion"

    handoff = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=4, rb_deadman=False,
        hand_motion_pending=False,
    )
    assert handoff.state == HILState.HANDOFF_HOLD.value
    assert handoff.fresh_policy_required is True


def test_handoff_accepts_only_current_epoch_policy_chunk() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=1, rb_deadman=False,
        hand_motion_pending=False,
    )
    handoff = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=2, rb_deadman=False,
        hand_motion_pending=False,
    )
    stale = supervisor.accept_fresh_policy_chunk(
        policy_epoch=handoff.policy_epoch - 1, request_id=9
    )
    assert stale.state == HILState.HANDOFF_HOLD.value
    assert stale.reason == "stale_policy_epoch"

    assert supervisor.register_fresh_policy_request(
        policy_epoch=handoff.policy_epoch, request_id=10, observation_id=3,
        observation_monotonic_ns=supervisor.handoff_monotonic_ns + 1,
        request_monotonic_ns=supervisor.handoff_monotonic_ns + 2)
    resumed = supervisor.accept_fresh_policy_chunk(
        policy_epoch=handoff.policy_epoch, request_id=10
    )
    assert resumed.state == HILState.POLICY.value
    assert resumed.transition == "fresh_policy_to_policy"


def test_transport_loss_never_resumes_policy_automatically() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False,
    )
    held = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=0, rb_deadman=False,
        hand_motion_pending=False, transport_fresh=False,
    )
    assert held.state == HILState.SAFE_HOLD.value

    reconnected = supervisor.observe_operator(
        session_id="s2", hil_toggle_id=7, rb_deadman=False,
        hand_motion_pending=False,
    )
    assert reconnected.state == HILState.SAFE_HOLD.value
    assert reconnected.transition == "operator_session_anchored"

    explicit = supervisor.observe_operator(
        session_id="s2", hil_toggle_id=8, rb_deadman=False,
        hand_motion_pending=False,
    )
    assert explicit.state == HILState.HIL_LATCHED.value


def test_toggle_gap_fails_to_safe_hold() -> None:
    supervisor = HILSupervisor()
    supervisor.observe_operator(
        session_id="s1", hil_toggle_id=2, rb_deadman=False,
        hand_motion_pending=False,
    )
    result = supervisor.observe_operator(
        session_id="s1", hil_toggle_id=4, rb_deadman=False,
        hand_motion_pending=False,
    )
    assert result.state == HILState.SAFE_HOLD.value
    assert result.reason == "hil_toggle_id_gap"
