"""H-51 item 3: align fast-fail when all candidates have insertion_preview == predicted_blocked."""
import json
import numpy as np
import pytest

from f1_harness.contracts import (
    Frame, HarnessError, Observation, Pairing, SceneObject, SkillIntent, Task, digest,
)
from f1_harness.skill_services import CANDIDATE_FAILURES, PlanService


def _make_align_service(monkeypatch, tmp_path):
    now = 20_000_000_000
    task = Task(
        task_id='t-h51', shape='square', object_id='square-1', hole_id='square-hole-1',
        calibration_id='cal-h51', goal='placed', admission_profile='development_coarse',
        allowed_skills=('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
                        'transfer_above_hole', 'align', 'insert', 'release',
                        'verify_placed', 'withdraw_reset'),
    )
    hole_pose = np.eye(4); hole_pose[2, 3] = 0.50
    cube_pose = np.eye(4); cube_pose[2, 3] = 0.55
    fixture_cfg = dict(
        native_frame='base',
        frame_chain=dict(
            accepted=True, synthetic=False, evidence_ids=['e1'], holdout_hash='0' * 64,
            native_frame='base', ik_world_frame='ik_world',
            native_eef_frame='eef', ik_eef_frame='ik_eef',
            T_ik_world_native=np.eye(4).ravel().tolist(),
            T_ik_eef_native_eef=np.eye(4).ravel().tolist(),
        ),
        approach_clearance_m=0.05, transfer_clearance_m=0.04, insertion_depth_m=0.012,
        attached_radius_m=0.04, effective_clearance_m=0.02, planning_margin_m=0.015,
        object_planning_margin_m=0.001,
        grasp_opening=55.0, release_opening=0.0, gripper_tolerance=5.0,
        joint_tolerance_deg=1.0, settle_ms=50,
        object_grasp_eef=np.eye(4).ravel().tolist(),
        eef_tip_reference=np.eye(4).ravel().tolist(),
        tool_local_boxes=[dict(min=[-0.02, -0.02, -0.02], max=[0.02, 0.02, 0.02])],
        object_local_box=dict(min=[-0.015, -0.015, 0.0], max=[0.015, 0.015, 0.06]),
        fixture_reference_hole=hole_pose.ravel().tolist(),
        obstacles=[dict(role='bucket', name='wall_x_low', pose=np.eye(4).ravel().tolist(),
                        min=[-0.2, -0.2, 0.0], max=[-0.1, 0.2, 0.5])],
        transfer_policy='registered_transport',
        return_route=dict(
            schema='f1-empty-tool-return-v1',
            home_right_deg=[0.0] * 7,
            right_axis_ik_world=[0.0, -1.0, 0.0],
            source='test', source_sha256='0' * 64,
        ),
    )
    service = PlanService(task, fixture_cfg, tmp_path / 'unused.sock', clock=lambda: now)
    calls = []

    class FakeNum:
        def call(self, method, **kwargs):
            calls.append(method)
            if method == 'forward_pose':
                pose = np.eye(4); pose[2, 3] = 0.60
                return pose.tolist()
            if method == 'endpoint_ik':
                return dict(results=[dict(converged=True, min_margin_deg=25.0) for _ in kwargs['goals']])
            if method == 'solve_path':
                return [list(kwargs['held16'])]
            raise HarnessError('unexpected:' + method)

    service.numerics = FakeNum()
    frames = (
        Frame(camera='head', stream='rgb', frame_id='h_rgb', sequence=1,
              source_ns=now - 5_000_000, received_ns=now, clock_domain='test',
              width=640, height=480, encoding='rgb8', blob_sha256='a' * 64),
        Frame(camera='head', stream='depth', frame_id='h_dep', sequence=1,
              source_ns=now - 5_000_000, received_ns=now, clock_domain='test',
              width=640, height=480, encoding='16UC1', blob_sha256='b' * 64),
        Frame(camera='right', stream='rgb', frame_id='r_rgb', sequence=1,
              source_ns=now - 5_000_000, received_ns=now, clock_domain='test',
              width=640, height=480, encoding='rgb8', blob_sha256='c' * 64),
        Frame(camera='right', stream='depth', frame_id='r_dep', sequence=1,
              source_ns=now - 5_000_000, received_ns=now, clock_domain='test',
              width=640, height=480, encoding='16UC1', blob_sha256='d' * 64),
    )
    pairings = (
        Pairing(camera='head', rgb_sequence=1, depth_sequence=1, skew_ns=0, quality='source_time_bounded'),
        Pairing(camera='right', rgb_sequence=1, depth_sequence=1, skew_ns=0, quality='source_time_bounded'),
    )
    cube = SceneObject(
        object_id='square-1', shape='square', pose=tuple(cube_pose.ravel()),
        eef_object_pose=tuple(np.eye(4).ravel()),
        frame_id='base', planning_margin_m=0.015, visible=True, held=True,
        stationary=True, evidence_ids=('c' * 64, 'd' * 64),
    )
    hole = SceneObject(
        object_id='square-hole-1', shape='square', pose=tuple(hole_pose.ravel()),
        frame_id='base', planning_margin_m=0.015, visible=True,
        stationary=True, evidence_ids=('a' * 64, 'b' * 64),
    )
    obs = Observation(
        observation_id='obs-h51', acquisition_instance='acq-1', clock_domain='test',
        received_ns=now, state_received_ns=now,
        state16=(0.0,) * 16, state_valid_mask=(True,) * 16,
        frames=frames, pairings=pairings, objects=(cube, hole),
        stationary=True, calibration_id='cal-h51',
        geometry_scope='coarse_static_task', geometry_camera='right',
        scene_version='sv-h51',
    )
    intent = SkillIntent(
        request_id='req-h51-align', session_id='s1', owner_instance_id='o1',
        task_id=task.task_id, task_hash=digest(task),
        observation_id=obs.observation_id, epoch=1,
        scene_version=obs.scene_version, calibration_id=task.calibration_id,
        skill='align', parameters={}, budget_ms=15_000,
    )
    return service, intent, obs, calls


def test_h51_align_fast_fails_when_all_candidates_insertion_preview_blocked(monkeypatch, tmp_path):
    assert 'insertion_preview_blocked_all_candidates' in CANDIDATE_FAILURES
    service, intent, obs, calls = _make_align_service(monkeypatch, tmp_path)

    def all_blocked(scene, variants, ranking, **kw):
        rows = [
            dict(variant=v, insertion_preview=dict(
                status='predicted_blocked',
                diagnostic=dict(code='scene_sweep_blocked', metrics=dict(obstacle_name=f'wall_{v}')),
            ))
            for v in variants
        ]
        return list(variants), rows

    monkeypatch.setattr('f1_harness.transport.rank_alignment_feeds', all_blocked)
    with pytest.raises(HarnessError) as exc_info:
        service.dispatch('plan_skill', dict(
            intent=intent.model_dump(), observation=obs.model_dump(),
            full22=[0.0] * 22, held16=[0.0] * 16,
        ))
    msg = str(exc_info.value)
    assert msg.startswith('insertion_preview_blocked_all_candidates:')
    payload = json.loads(msg.split(':', 1)[1])
    assert payload['diagnostic']['code'] == 'insertion_preview_blocked_all_candidates'
    assert len(payload['candidates']) == 4
    assert [c['diagnostic']['metrics']['obstacle_name'] for c in payload['candidates']] == [
        f'wall_{v}' for v in payload['diagnostic']['metrics']['blocked_variants']
    ]
    # Must NOT have attempted solve_path (no motion planned)!
    assert 'solve_path' not in calls


def test_h51_align_plans_normally_if_at_least_one_candidate_is_predicted_clear(monkeypatch, tmp_path):
    service, intent, obs, calls = _make_align_service(monkeypatch, tmp_path)

    def one_clear(scene, variants, ranking, **kw):
        rows = []
        for idx, v in enumerate(variants):
            if idx == 0:
                rows.append(dict(variant=v, insertion_preview=dict(status='predicted_clear')))
            else:
                rows.append(dict(variant=v, insertion_preview=dict(
                    status='predicted_blocked',
                    diagnostic=dict(code='scene_sweep_blocked', metrics=dict(obstacle_name='wall_x_low')),
                )))
        return list(variants), rows

    monkeypatch.setattr('f1_harness.transport.rank_alignment_feeds', one_clear)
    monkeypatch.setattr('f1_harness.transport.propose_alignment',
                        lambda scene, variant: dict(kind='motion', skill='align',
                                                    stages=[dict(goals=[np.eye(4).tolist()], gripper=None, eef_object=None)]))
    prop = service.dispatch('plan_skill', dict(
        intent=intent.model_dump(), observation=obs.model_dump(),
        full22=[0.0] * 22, held16=[0.0] * 16,
    ))
    assert prop['request_id'] == intent.request_id
    assert 'solve_path' in calls
