from __future__ import annotations

from g2teleop import ActiveSide, IpadSample, InputMapper


def sample(*, a3: float = 0.0, **buttons: bool) -> IpadSample:
    return IpadSample(
        captured_at_monotonic=1.0,
        position_m=(0.1, 0.2, 0.3),
        quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
        analogs={"a3": a3},
        buttons=buttons,
    )


def test_requires_side_before_deadman_enables_motion() -> None:
    mapper = InputMapper()
    intent = mapper.map(sample(b1=True))
    assert not intent.motion_enabled
    assert intent.warning == "deadman_without_active_side"


def test_select_left_only_while_deadman_released() -> None:
    mapper = InputMapper()
    selected = mapper.map(sample(b2=True))
    assert selected.active_side is ActiveSide.LEFT
    moving = mapper.map(sample(a3=0.7, b1=True, b3=True, b4=True))
    assert moving.active_side is ActiveSide.LEFT
    assert moving.motion_enabled
    assert moving.grip_velocity_raw == 0.7
    assert moving.thumb_clearance
    assert moving.warning == "side_change_while_deadman_held_ignored"


def test_simultaneous_side_selection_is_ignored() -> None:
    mapper = InputMapper()
    mapper.map(sample(b2=True))
    intent = mapper.map(sample(b2=True, b3=True))
    assert intent.active_side is ActiveSide.LEFT
    assert intent.warning == "simultaneous_left_right_selection_ignored"


def test_deadband_and_release_force_zero_hand_intent() -> None:
    mapper = InputMapper()
    mapper.map(sample(b3=True))
    neutral = mapper.map(sample(a3=0.05, b1=True, b4=True))
    assert neutral.active_side is ActiveSide.RIGHT
    assert neutral.grip_velocity_raw == 0.0
    assert neutral.thumb_clearance
    released = mapper.map(sample(a3=0.8, b4=True))
    assert not released.motion_enabled
    assert released.grip_velocity_raw == 0.0
    assert not released.thumb_clearance

