"""Head preference is efficient when visible, but can recover in the wrist."""
import numpy as np
import pytest

from test_full_development_scene import full_scene, sample
from test_scene_pipeline import fixture


def setup(*, head_object=False, right_object=True):
    pipeline, obs = full_scene(); original, _ = fixture(); calls = []
    pipeline.config.update(pregrasp_object_camera='head', object_reacquisition_camera='head')
    def masks(rgb, view):
        calls.append(view)
        value = original.segment(rgb)
        if view == 'head':
            return value if head_object else {'hole': value['hole']}
        if not right_object:
            raise ValueError('mask_view=right:mask_uncertain_requires_new_prompt:object=block')
        return {'block': value['block']}
    pipeline.segment = masks
    return pipeline, obs, calls


def test_preferred_head_missing_object_falls_back_to_current_wrist_and_keeps_hole():
    pipeline, obs, calls = setup()
    try:
        scene = pipeline.process(sample(obs, 0), base_eef=np.eye(4), context={'phase': 'observe'})
        assert scene.geometry_camera == 'right'
        assert scene.geometry_scope == 'coarse_static_task'
        assert {obj.object_id for obj in scene.objects} == {'block', 'hole'}
        assert calls == ['head', 'right']
        assert pipeline.last_timing['right_pixel_work_skipped'] is False
        assert pipeline.last_timing['object_reacquisition']['camera'] == 'right'
    finally:
        pipeline.close()


def test_good_preferred_head_still_skips_wrist_work():
    pipeline, obs, calls = setup(head_object=True)
    try:
        scene = pipeline.process(sample(obs, 0), base_eef=np.eye(4), context={'phase': 'observe'})
        assert scene.geometry_camera == 'head' and calls == ['head']
        assert pipeline.last_timing['right_pixel_work_skipped'] is True
    finally:
        pipeline.close()


@pytest.mark.parametrize('reason', [
    'insufficient_upright_face_support', 'surface_points_missing_or_invalid',
    'insufficient valid depth', 'surface is not a reliable planar block face',
])
def test_known_head_geometry_quality_fault_can_use_independent_wrist(monkeypatch, reason):
    import f1_harness.scene_pipeline as module
    pipeline, obs, calls = setup(head_object=True)
    measure = module.object_from_mask; count = 0
    def first_rejected(**kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise ValueError(reason)
        return measure(**kwargs)
    monkeypatch.setattr(module, 'object_from_mask', first_rejected)
    try:
        scene = pipeline.process(sample(obs, 0), base_eef=np.eye(4), context={'phase': 'observe'})
        assert scene.geometry_camera == 'right' and calls == ['head', 'right']
        assert pipeline.last_timing['preferred_head_rejected'] == reason
    finally:
        pipeline.close()


@pytest.mark.parametrize('reason', [
    'camera_calibration_identity_changed', 'secondary_camera_pairing_unavailable',
    'new_consistent_source_frames_required:camera=head', 'unexpected_geometry_failure',
])
def test_head_identity_time_or_unknown_error_never_falls_back(monkeypatch, reason):
    import f1_harness.scene_pipeline as module
    pipeline, obs, calls = setup(head_object=True)
    def rejected(**kwargs):
        raise ValueError(reason)
    monkeypatch.setattr(module, 'object_from_mask', rejected)
    try:
        with pytest.raises(ValueError, match=reason):
            pipeline.process(sample(obs, 0), base_eef=np.eye(4), context={'phase': 'observe'})
        assert calls == ['head']
    finally:
        pipeline.close()


def test_both_missing_report_each_view_reason_without_recursive_retry():
    pipeline, obs, calls = setup(right_object=False)
    try:
        with pytest.raises(ValueError) as failure:
            pipeline.process(sample(obs, 0), base_eef=np.eye(4), context={'phase': 'observe'})
        assert str(failure.value)=='mask_view=right:mask_uncertain_requires_new_prompt:object=block'
        attempts=pipeline.last_timing['object_geometry_attempts']
        assert [a['camera'] for a in attempts]==['head','right']
        assert attempts[0]['reason']=='object_mask_missing:camera=head:object=block'
        assert attempts[1]['reason']==str(failure.value)
        assert calls == ['head', 'right']
    finally:
        pipeline.close()


def test_missing_required_head_fixture_is_not_misreported_as_object_fallback():
    pipeline, obs, calls = setup()
    def missing_hole(rgb, view):
        calls.append(view)
        raise ValueError('mask_view=head:mask_uncertain_requires_new_prompt:object=hole')
    pipeline.segment = missing_hole
    try:
        with pytest.raises(ValueError, match='object=hole'):
            pipeline.process(sample(obs, 0), base_eef=np.eye(4), context={'phase': 'observe'})
        assert calls == ['head']
    finally:
        pipeline.close()


def test_object_only_scene_retains_actual_head_source_when_hole_missing():
    pipeline,obs,calls=setup(head_object=True);original=pipeline.segment
    pipeline.segment=lambda rgb,view:{k:v for k,v in original(rgb,view).items() if k=='block'}
    try:
        scene=pipeline.process(sample(obs,0),base_eef=np.eye(4),context={'phase':'observe'})
        assert scene.geometry_scope=='coarse_object_verification'
        assert scene.geometry_camera=='head'
        assert {f.camera for f in scene.frames}=={'head'}
        assert {p.camera for p in scene.pairings}=={'head'}
    finally:pipeline.close()
