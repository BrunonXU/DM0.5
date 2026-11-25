"""Independent review: physical openings invalidate command-filled grasp history."""
import numpy as np
import pytest

from f1_harness.contracts import MeasuredGripperFeedback
from test_full_development_scene import sample
from test_gripper_contact import configured, packet


def measured_observation(raw, index, *, opened_at=()):
    value = packet(end=index * 100_000_000)
    for row in value['samples']:
        # Real passive CAN uses a persistent source and monotonic sequence.
        # Repeated snapshots preserve both identity and original measurement.
        stamp = row['received_ns']
        row['sequence'] = stamp // 10_000_000 + 1
        if stamp in opened_at:
            row.update(raw_position=255, closure_percent=0.,
                       data_hex=bytes([0, 0, 255, 0, 2, 0, 0, 0]).hex())
    value['latest'] = value['samples'][-1]
    value['received_count'] = value['latest']['sequence']
    observation = sample(raw, index)
    state = list(observation.state16)
    state[15] = 100.  # Installed ROS field can still echo the close command.
    return observation.model_copy(update=dict(state16=tuple(state), host_boot_id='boot',
        measured_gripper=MeasuredGripperFeedback.model_validate(value)))


def target_held(observation):
    return next(obj.held for obj in observation.objects if obj.object_id == 'block')


@pytest.mark.parametrize('case', ['visible_open', 'mask_gap_open', 'between_images_open_close'])
def test_physical_opening_cannot_restore_old_visual_attachment(case):
    pipeline, raw = configured()
    context = dict(pipeline.previous_context, task_id='task', epoch=1)
    try:
        for index, z in [(6, 0.), (7, .02), (8, .04)]:
            eef = np.eye(4)
            eef[2, 3] = z
            value = pipeline.process(measured_observation(raw, index),
                                     base_eef=eef, context=context)
        assert target_held(value) is True

        opening = (900_000_000,) if case != 'between_images_open_close' else (820_000_000, 830_000_000)
        observation = measured_observation(raw, 9, opened_at=opening)
        if case == 'mask_gap_open':
            original = pipeline.segment
            def bad_mask(*args, **kwargs):
                raise ValueError('mask_unavailable')
            pipeline.segment = bad_mask
            with pytest.raises(ValueError, match='mask_unavailable'):
                pipeline.process(observation, base_eef=eef, context=context)
            pipeline.segment = original
        else:
            value = pipeline.process(observation, base_eef=eef, context=context)
            assert target_held(value) is not True
        assert pipeline.in_hand_reference is None
        assert pipeline.in_hand_quarantine is None

        # Reclosing with stationary geometry is not a new measured grasp.
        for index in (10, 11, 12):
            value = pipeline.process(measured_observation(raw, index, opened_at=opening),
                                     base_eef=eef, context=context)
            assert target_held(value) is not True
    finally:
        pipeline.close()


@pytest.mark.parametrize('case', ['closed', 'missing', 'stopped', 'stale'])
def test_return_cannot_use_open_command_without_current_physical_opening(monkeypatch, case):
    from test_return_observation import prepared
    pipeline, raw = prepared(monkeypatch)
    pipeline.config.update(measured_gripper_contact=True, measured_gripper_max_age_ns=200_000_000)
    observation = measured_observation(raw, 10)
    state = list(observation.state16)
    state[15] = 18.
    feedback = observation.measured_gripper
    if case == 'missing':
        feedback = None
    elif case == 'stopped':
        feedback = feedback.model_copy(update={'running': False})
    elif case == 'stale':
        feedback = measured_observation(raw, 6, opened_at=(600_000_000,)).measured_gripper
    observation = observation.model_copy(update={'state16': tuple(state), 'measured_gripper': feedback})
    context = dict(authorized=True, placement_verified=True, phase='withdraw_reset', epoch=1)
    try:
        with pytest.raises(ValueError, match='return_gripper_not_open'):
            pipeline.process(observation, base_eef=np.eye(4), context=context)
    finally:
        pipeline.close()


def test_return_binds_gripper_to_current_head_instead_of_unused_old_wrist(monkeypatch):
    from test_return_observation import prepared
    pipeline, raw = prepared(monkeypatch)
    pipeline.config.update(measured_gripper_contact=True, measured_gripper_max_age_ns=200_000_000)
    opening = tuple(range(900_000_000, 1_000_000_001, 10_000_000))
    observation = measured_observation(raw, 10, opened_at=opening)
    observation = observation.model_copy(update={'frames': tuple(
        frame.model_copy(update={'received_ns': 800_000_001}) if frame.camera == 'right' else frame
        for frame in observation.frames)})
    context = dict(authorized=True, placement_verified=True, phase='withdraw_reset', epoch=1)
    try:
        value = pipeline.process(observation, base_eef=np.eye(4), context=context)
        assert value.geometry_scope == 'coarse_fixture_return'
        assert {frame.camera for frame in value.frames} == {'head'}
    finally:
        pipeline.close()


def test_reacquired_head_object_uses_its_own_image_time_for_physical_opening():
    pipeline, raw = configured()
    pipeline.last_timing = {}
    try:
        for index, z in [(6, 0.), (7, .02), (8, .04)]:
            pose = np.eye(4)
            pose[2, 3] = z
            held = pipeline._primary_evidence(measured_observation(raw, index), pose, pose,
                                             (index * 100_000_000,) * 2, camera='head')[-1]
        assert held is True
        observation = measured_observation(raw, 9, opened_at=(900_000_000,))
        observation = observation.model_copy(update={'frames': tuple(
            frame.model_copy(update={'received_ns': 800_000_001}) if frame.camera == 'right' else frame
            for frame in observation.frames)})
        held = pipeline._primary_evidence(observation, pose, pose,
                                         (900_000_000,) * 2, camera='head')[-1]
        assert held is not True
    finally:
        pipeline.close()


def test_open_command_cannot_override_current_measured_closed_fingers():
    pipeline, raw = configured()
    context = dict(authorized=True, task_id='task', epoch=1)
    try:
        for index in (6, 7, 8, 9):
            observation = measured_observation(raw, index)
            state = list(observation.state16)
            state[15] = 18.  # Opening was requested; hardware is still at 72.55.
            observation = observation.model_copy(update={'state16': tuple(state)})
            value = pipeline.process(observation, base_eef=np.eye(4), context=context)
            assert target_held(value) is not False
        observation = measured_observation(raw, 10, opened_at=(1_000_000_000,))
        state = list(observation.state16)
        state[15] = 18.
        value = pipeline.process(observation.model_copy(update={'state16': tuple(state)}),
                                 base_eef=np.eye(4), context=context)
        assert target_held(value) is False  # A current actual opening is evidence.
    finally:
        pipeline.close()
