import json

import numpy as np
import pytest

from g2teleop.hand_synergy import (
    ChangedOnlyHandScheduler,
    DualHandCommandState,
    DualHandSynergy,
    HandSynergyError,
)


def config(tmp_path):
    path = tmp_path / "fit.json"
    section = {
        "open_g0": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "closed_g1": [1, 1, 1, 0, 1, 1, 0, 1, 0, 1],
        "thumb_clearance_c1": [0, 0, 0],
    }
    path.write_text(json.dumps({"status": "fit", "powered_allowed": False,
                                "left": section, "right": section}))
    return path


def test_g_and_c_mapping(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    target = fit.target("left", 0.5, 1.0)
    assert np.allclose(target[:3], 0)
    assert np.allclose(target[[4, 5, 7, 9]], 0.5)
    assert np.allclose(target[[3, 6, 8]], 0)


def test_other_hand_holds_and_near_zero_rejected(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    measured = np.full(20, 0.5)
    state = DualHandCommandState(fit, measured)
    before = state.command.copy()
    after = state.step(side="left", enabled=True, grip_velocity=1, thumb_clearance=False, dt=0.01)
    assert np.array_equal(after[10:], before[10:])
    assert np.max(np.abs(after[:10] - before[:10])) <= 0.003000001
    with pytest.raises(HandSynergyError, match="near-zero"):
        DualHandCommandState(fit, np.zeros(20))


def test_analog_grip_target_moves_toward_trigger_without_jump(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    state = DualHandCommandState(fit, np.full(20, 0.5))
    before = state.grip["left"]
    state.step(
        side="left", enabled=True, grip_velocity=0.0, grip_target=1.0,
        thumb_clearance=False, dt=0.1,
    )
    assert state.grip["left"] == pytest.approx(before + 0.03)


def test_changed_only_scheduler_samples_at_5hz_and_stops_resending(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    state = DualHandCommandState(fit, np.full(20, 0.5))
    scheduler = ChangedOnlyHandScheduler(state, nominal_hz=5.0)

    first = scheduler.step(
        now=10.0,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=1.0,
        thumb_clearance=False,
    )
    assert first.should_send
    assert first.sampled
    assert first.intent_changed
    assert first.intent_id == 1

    early = scheduler.step(
        now=10.1,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=1.0,
        thumb_clearance=False,
    )
    assert not early.should_send
    assert not early.sampled
    np.testing.assert_array_equal(early.command, first.command)

    second = scheduler.step(
        now=10.2,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=1.0,
        thumb_clearance=False,
    )
    assert second.should_send
    assert second.intent_id == 1
    assert np.max(np.abs(second.command - first.command)) <= 0.060000001

    now = 10.4
    decision = second
    for _ in range(40):
        decision = scheduler.step(
            now=now,
            side="left",
            enabled=True,
            grip_velocity=0.0,
            grip_target=1.0,
            thumb_clearance=False,
        )
        now += 0.2
    assert not decision.should_send
    assert decision.reason == "sampled_but_unchanged"


def test_changed_intent_sends_immediately_but_disabled_state_never_advances(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    state = DualHandCommandState(fit, np.full(20, 0.5))
    scheduler = ChangedOnlyHandScheduler(state, nominal_hz=5.0)
    held = state.command.copy()

    disabled = scheduler.step(
        now=1.0,
        side="left",
        enabled=False,
        grip_velocity=0.0,
        grip_target=1.0,
        thumb_clearance=False,
    )
    assert not disabled.should_send
    np.testing.assert_array_equal(disabled.command, held)

    close = scheduler.step(
        now=2.0,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=1.0,
        thumb_clearance=False,
    )
    assert close.should_send and close.intent_id == 1

    # A new RT target is recorded immediately but cannot violate the 5 Hz
    # move_ee_pos call spacing.
    opened = scheduler.step(
        now=2.05,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=0.0,
        thumb_clearance=False,
    )
    assert not opened.should_send
    assert opened.intent_changed
    assert opened.intent_id == 2
    opened_at_slot = scheduler.step(
        now=2.2,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=0.0,
        thumb_clearance=False,
    )
    assert opened_at_slot.should_send
    assert opened_at_slot.intent_id == 2


def test_arming_without_first_grip_intent_does_not_project_measured_hand(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    measured = np.linspace(0.10, 0.90, 20)
    state = DualHandCommandState(fit, measured)
    scheduler = ChangedOnlyHandScheduler(state, nominal_hz=5.0)

    decision = scheduler.step(
        now=1.0,
        side="left",
        enabled=True,
        grip_velocity=0.0,
        grip_target=None,
        thumb_clearance=False,
    )
    assert not decision.should_send
    assert not decision.sampled
    assert decision.intent_id == 0
    assert decision.reason == "no_hand_intent_hold"
    np.testing.assert_array_equal(decision.command, measured)


def test_100hz_caller_never_produces_o10_calls_faster_than_5hz(tmp_path):
    fit = DualHandSynergy(config(tmp_path))
    state = DualHandCommandState(fit, np.full(20, 0.5))
    scheduler = ChangedOnlyHandScheduler(state, nominal_hz=5.0)
    send_times = []
    for tick in range(1000):
        decision = scheduler.step(
            now=tick * 0.01,
            side="left",
            enabled=True,
            grip_velocity=0.0,
            grip_target=1.0,
            thumb_clearance=False,
        )
        if decision.should_send:
            send_times.append(tick * 0.01)

    assert send_times
    assert np.min(np.diff(send_times)) >= 0.2 - 1e-12
    # The persistent target remains present for ten seconds, but GDK calls end
    # once the generated 20D command reaches it.
    assert send_times[-1] < 5.0
