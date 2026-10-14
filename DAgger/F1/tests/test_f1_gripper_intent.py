"""F1 RT intent: new clicks act once; takeover and unknown-side latches stay held."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from f1dagger.teleop import GripperIntent
from g2dagger.f710_mapping import F710Mapper, GamepadSnapshot


def intent(side, target):
    return SimpleNamespace(active_side=side, grip_target=target)


@pytest.mark.parametrize('side,index', [('left', 0), ('right', 1)])
@pytest.mark.parametrize('baseline_at_takeover', [False, True])
def test_first_rt_after_explicit_none_baseline_is_not_swallowed(side, index, baseline_at_takeover):
    grip = GripperIntent()
    initial = np.r_[np.zeros(14), 43., 66.]
    grip.anchor(initial, intent(side, None) if baseline_at_takeover else None)
    if not baseline_at_takeover:
        np.testing.assert_equal(grip.update(intent(side, None)), initial[14:])
    expected = initial[14:].copy()
    expected[index] = 100.
    np.testing.assert_equal(grip.update(intent(side, 1.)), expected)
    np.testing.assert_equal(grip.update(intent(side, 1.)), expected)
    expected[index] = 0.
    np.testing.assert_equal(grip.update(intent(side, 0.)), expected)


def test_real_mapper_home_clears_side_then_first_new_rt_works():
    mapper, grip = F710Mapper(), GripperIntent()
    neutral = GamepadSnapshot()
    grip.anchor(np.r_[np.zeros(14), 43., 66.], mapper.map(neutral))
    def sample(**fields):
        mapped = mapper.map(replace(neutral, **fields))
        return mapped, grip.update(mapped)
    sample(dpad_x=-1)
    sample()
    _, output = sample(right_trigger=1.)
    assert list(output) == [100., 66.]
    sample()
    home = mapper.map(replace(neutral, y=True))
    assert home.home_reset and home.active_side is None and home.grip_target is None
    # ControlLoop re-anchors after completed Home while preserving commanded grips.
    grip.anchor(np.r_[np.zeros(14), output], home)
    sample()
    sample(dpad_x=1)
    sample()
    mapped, after = sample(right_trigger=1.)
    assert not mapped.deadman  # RT is independent of the arm deadman.
    assert list(after) == [100., 100.]
    sample()
    _, after = sample(right_trigger=1.)
    assert list(after) == [100., 0.]


def test_unknown_side_first_non_none_is_only_baseline_not_replay():
    grip = GripperIntent()
    grip.anchor(np.r_[np.zeros(14), 43., 66.], intent('left', None))
    assert list(grip.update(intent('right', 1.))) == [43., 66.]
    assert list(grip.update(intent('right', 1.))) == [43., 66.]
    assert list(grip.update(intent('right', 0.))) == [43., 0.]


def test_switch_back_keeps_per_side_baselines_and_retakeover_suppresses_old_latches():
    grip = GripperIntent()
    grip.anchor(np.r_[np.zeros(14), 43., 66.], intent('left', None))
    assert list(grip.update(intent('left', 1.))) == [100., 66.]
    grip.update(intent('right', None))
    assert list(grip.update(intent('right', 1.))) == [100., 100.]
    assert list(grip.update(intent('left', 1.))) == [100., 100.]
    assert list(grip.update(intent('left', 0.))) == [0., 100.]
    # During pause/policy, other code may issue new physical grip targets.
    grip.anchor(np.r_[np.zeros(14), 21., 37.], intent('right', 1.))
    assert list(grip.update(intent('right', 1.))) == [21., 37.]
    assert list(grip.update(intent('left', 0.))) == [21., 37.]
    assert list(grip.update(intent('left', 0.))) == [21., 37.]
    assert list(grip.update(intent('left', 1.))) == [100., 37.]
    assert list(grip.update(intent('right', 0.))) == [100., 0.]


def test_rt_pressed_before_side_selection_cannot_command_a_gripper():
    mapper, grip = F710Mapper(), GripperIntent()
    mapper.map(GamepadSnapshot())
    grip.anchor(np.r_[np.zeros(14), 43., 66.], None)
    mapped = mapper.map(GamepadSnapshot(right_trigger=1.))
    assert mapped.warning == 'hand_intent_without_active_side'
    assert list(grip.update(mapped)) == [43., 66.]
    mapped = mapper.map(GamepadSnapshot(right_trigger=1., dpad_x=-1))
    assert mapped.grip_target is None
    assert list(grip.update(mapped)) == [43., 66.]
    grip.update(mapper.map(GamepadSnapshot()))
    assert list(grip.update(mapper.map(GamepadSnapshot(right_trigger=1.)))) == [100., 66.]


def test_field_right_rt_sequence_762_does_not_wait_for_another_click():
    # f1_hil_20261008T092037Z_54565d3b: right None -> 1 at packet 762,
    # then 93 consecutive grip_target=1 packets while published grips stayed 0.
    grip = GripperIntent()
    grip.anchor(np.zeros(16), None)
    assert list(grip.update(intent('right', None))) == [0., 0.]
    published_candidates = [grip.update(SimpleNamespace(active_side='right', grip_target=1.,
                                                      sequence=seq))
                            for seq in range(762, 762 + 93)]
    np.testing.assert_equal(published_candidates, np.tile([0., 100.], (93, 1)))
