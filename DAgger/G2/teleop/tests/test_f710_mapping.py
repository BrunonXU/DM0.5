from __future__ import annotations

from g2teleop.f710_mapping import F710Mapper, GamepadSnapshot


def test_requires_neutral_side_and_fresh_rb_edge() -> None:
    mapper = F710Mapper()
    assert not mapper.map(GamepadSnapshot(rb=True)).deadman
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    enabled = mapper.map(GamepadSnapshot(rb=True, left_y=-1.0))
    assert enabled.deadman
    assert enabled.active_side == "left"
    assert enabled.mode == "combined"
    assert enabled.axes[0] == 1.0


def test_selection_is_ignored_while_deadman_is_held() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(rb=True))
    result = mapper.map(GamepadSnapshot(rb=True, dpad_x=1))
    assert result.active_side == "left"
    assert result.warning == "select_side_before_deadman"


def test_selection_attempted_during_rb_must_be_released_and_pressed_again() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(rb=True))
    ignored = mapper.map(GamepadSnapshot(rb=True, dpad_x=1))
    assert ignored.active_side == "left"
    still_left = mapper.map(GamepadSnapshot(rb=False, dpad_x=1))
    assert still_left.active_side == "left"
    mapper.map(GamepadSnapshot())
    applied = mapper.map(GamepadSnapshot(dpad_x=1))
    assert applied.active_side == "right"


def test_dpad_and_rb_same_sample_never_arms_or_reuses_old_side() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    simultaneous = mapper.map(GamepadSnapshot(dpad_x=-1, rb=True))
    assert not simultaneous.deadman
    assert simultaneous.warning == "release_dpad_then_press_deadman"
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    assert mapper.map(GamepadSnapshot(rb=True)).deadman


def test_translation_stick_left_means_positive_base_y() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    enabled = mapper.map(GamepadSnapshot(rb=True, left_x=-1.0))
    assert enabled.deadman
    assert enabled.axes == (0.0, 1.0, 0.0)


def test_disconnect_forgets_side_and_requires_rearming() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=1))
    mapper.map(GamepadSnapshot())
    assert mapper.map(GamepadSnapshot(rb=True)).deadman
    disconnected = mapper.map(GamepadSnapshot(connected=False))
    assert not disconnected.deadman
    assert disconnected.active_side is None
    assert not mapper.map(GamepadSnapshot(rb=True)).deadman


def test_disconnect_resets_speed_and_forgets_old_hand_target() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    assert mapper.map(GamepadSnapshot(a=True)).speed_scale == 2.0
    mapper.map(GamepadSnapshot(a=False, right_trigger=1.0))
    mapper.map(GamepadSnapshot(connected=False))
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    selected = mapper.map(GamepadSnapshot())
    assert selected.speed_scale == 1.0
    assert selected.grip_target is None


def test_trigger_and_speed_boost_are_disabled_without_rb() -> None:
    mapper = F710Mapper()
    result = mapper.map(GamepadSnapshot(right_trigger=1.0, lb=True))
    assert result.grip_velocity == 0.0
    assert not result.thumb_clearance
    assert result.grip_target is None
    assert result.speed_scale == 1.0


def test_combined_correction_layout() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    enabled = mapper.map(GamepadSnapshot(
        rb=True,
        left_x=-1.0,
        left_y=-1.0,
        right_x=1.0,
        right_y=-1.0,
        lb=True,
        right_trigger=0.75,
        a=True,
        b=True,
    ))
    assert enabled.mode == "combined"
    assert enabled.axes == (1.0, 1.0, 1.0)
    assert enabled.angular_axes == (1.0, -1.0, -1.0)
    assert enabled.grip_target == 1.0
    assert not enabled.thumb_clearance
    assert enabled.speed_scale == 2.0


def test_a_click_toggles_persistent_speed_and_defaults_low() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    assert mapper.map(GamepadSnapshot(rb=True)).speed_scale == 1.0
    assert mapper.map(GamepadSnapshot(rb=True, a=True)).speed_scale == 2.0
    assert mapper.map(GamepadSnapshot(rb=True, a=False)).speed_scale == 2.0
    assert mapper.map(GamepadSnapshot(rb=True, a=True)).speed_scale == 1.0


def test_y_requests_global_home_only_with_rb_released() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    requested = mapper.map(GamepadSnapshot(y=True))
    assert requested.home_reset
    assert not requested.deadman
    assert requested.grip_target is None

    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    blocked = mapper.map(GamepadSnapshot(rb=True, y=True))
    assert not blocked.home_reset
    assert blocked.warning == "release_deadman_before_home_reset"


def test_right_stick_uses_natural_non_flight_signs() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    up_right = mapper.map(GamepadSnapshot(rb=True, right_x=1.0, right_y=-1.0))
    assert up_right.angular_axes == (0.0, -1.0, -1.0)


def test_rt_click_toggles_binary_grip_target_per_side() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())

    armed = mapper.map(GamepadSnapshot(rb=True))
    assert armed.grip_target is None
    closed = mapper.map(GamepadSnapshot(rb=True, right_trigger=0.8))
    assert closed.grip_target == 1.0
    # A held trigger is one click, not a stream of alternating edges.
    assert mapper.map(GamepadSnapshot(rb=True, right_trigger=1.0)).grip_target == 1.0
    # Values in the hysteresis band do not re-arm the edge detector.
    assert mapper.map(GamepadSnapshot(rb=True, right_trigger=0.5)).grip_target == 1.0
    mapper.map(GamepadSnapshot(rb=True, right_trigger=0.0))
    opened = mapper.map(GamepadSnapshot(rb=True, right_trigger=0.8))
    assert opened.grip_target == 0.0


def test_rt_toggle_is_independent_for_each_hand_and_does_not_require_rb() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    mapper.map(GamepadSnapshot(dpad_x=-1))
    mapper.map(GamepadSnapshot())
    closed_without_rb = mapper.map(GamepadSnapshot(right_trigger=1.0))
    assert not closed_without_rb.deadman
    assert closed_without_rb.active_side == "left"
    assert closed_without_rb.grip_target == 1.0
    mapper.map(GamepadSnapshot(right_trigger=0.0))
    assert mapper.map(GamepadSnapshot(right_trigger=1.0)).grip_target == 0.0

    mapper.map(GamepadSnapshot(rb=False, right_trigger=0.0))
    mapper.map(GamepadSnapshot(dpad_x=1))
    mapper.map(GamepadSnapshot())
    right_closed = mapper.map(GamepadSnapshot(right_trigger=1.0))
    assert not right_closed.deadman
    assert right_closed.active_side == "right"
    assert right_closed.grip_target == 1.0


def test_rt_without_selected_side_is_inert_and_warns() -> None:
    mapper = F710Mapper()
    result = mapper.map(GamepadSnapshot(right_trigger=1.0))
    assert not result.deadman
    assert result.grip_target is None
    assert result.warning == "hand_intent_without_active_side"


def test_start_generates_persistent_monotonic_toggle_id() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    first = mapper.map(GamepadSnapshot(start=True))
    held = mapper.map(GamepadSnapshot(start=True))
    released = mapper.map(GamepadSnapshot(start=False))
    second = mapper.map(GamepadSnapshot(start=True))

    assert first.hil_toggle_id == 1
    assert first.hil_button_pressed is True
    assert held.hil_toggle_id == 1
    assert released.hil_toggle_id == 1
    assert second.hil_toggle_id == 2


def test_start_must_be_released_after_controller_reconnect() -> None:
    mapper = F710Mapper()
    mapper.map(GamepadSnapshot())
    assert mapper.map(GamepadSnapshot(start=True)).hil_toggle_id == 1
    disconnected = mapper.map(GamepadSnapshot(connected=False))
    assert disconnected.hil_toggle_id == 1
    # A physically held START cannot create a transition as SDL reconnects.
    assert mapper.map(GamepadSnapshot(start=True)).hil_toggle_id == 1
    mapper.map(GamepadSnapshot(start=False))
    assert mapper.map(GamepadSnapshot(start=True)).hil_toggle_id == 2
