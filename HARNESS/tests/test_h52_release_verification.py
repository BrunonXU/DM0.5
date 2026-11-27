"""H-52: release post-motion verification based on measured gripper CAN open state."""
import numpy as np
import pytest

from f1_harness.contracts import (
    Frame, Mode, Observation, Pairing, SceneObject, SkillIntent, Task, digest,
)
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.observation import require_geometry_identity
from f1_harness.owner_bridge import OwnerBridge
from f1_harness.skill_services import VerifyService


def _task():
    return Task(
        task_id='t-h52', shape='square', object_id='square-1', hole_id='square-hole-1',
        calibration_id='cal-h52', goal='placed', admission_profile='development_coarse',
        allowed_skills=('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
                        'transfer_above_hole', 'align', 'insert', 'release',
                        'verify_placed', 'withdraw_reset'),
    )


def _fixture():
    return dict(release_opening=0.0, gripper_tolerance=5.0)


def _obs(*, now_ns, gripper_val, objects=(), geometry_scope='coarse_object_verification'):
    frames = (
        Frame(camera='right', stream='rgb', frame_id='right_color', sequence=10,
              source_ns=now_ns - 10_000_000, received_ns=now_ns - 5_000_000,
              clock_domain='test', width=640, height=480, encoding='rgb8',
              blob_sha256='1' * 64),
        Frame(camera='right', stream='depth', frame_id='right_depth', sequence=10,
              source_ns=now_ns - 10_000_000, received_ns=now_ns - 5_000_000,
              clock_domain='test', width=640, height=480, encoding='16UC1',
              blob_sha256='2' * 64),
    )
    pairings = (Pairing(camera='right', rgb_sequence=10, depth_sequence=10,
                        skew_ns=0, quality='source_time_bounded'),)
    state16 = tuple([0.0] * 15 + [float(gripper_val)])
    return Observation(
        observation_id='obs-h52', acquisition_instance='acq-1', clock_domain='test',
        received_ns=now_ns, state_received_ns=now_ns,
        state16=state16, state_valid_mask=(True,) * 16,
        frames=frames, pairings=pairings, objects=tuple(objects),
        stationary=True, calibration_id='cal-h52',
        geometry_scope=geometry_scope, geometry_camera='right',
        scene_version='v-h52',
    )


def _intent(task, obs, req_id='req-rel'):
    return SkillIntent(
        request_id=req_id, session_id='s1', owner_instance_id='o1',
        task_id=task.task_id, task_hash=digest(task),
        observation_id=obs.observation_id, epoch=1,
        scene_version=obs.scene_version, calibration_id=task.calibration_id,
        skill='release', parameters={}, budget_ms=10_000,
    )


def test_h52_release_verification_admits_occluded_object_only_scope_without_hole():
    task = _task()
    now = 20_000_000_000
    obs = _obs(now_ns=now, gripper_val=0.5, objects=())
    # Post-motion verification does NOT require hole_obj.
    cams = require_geometry_identity(
        obs, goal=task.goal, admission_profile=task.admission_profile,
        skill='release', verification=True, hole_id=task.hole_id,
    )
    assert cams == ('right',)
    # Pre-motion planning STILL requires hole_obj.
    with pytest.raises(ValueError, match='aperture_measurement_required'):
        require_geometry_identity(
            obs, goal=task.goal, admission_profile=task.admission_profile,
            skill='release', verification=False, hole_id=task.hole_id,
        )


def test_h52_release_can_open_and_block_invisible_succeeds():
    task = _task()
    now = 20_000_000_000
    obs = _obs(now_ns=now, gripper_val=1.2, objects=())
    vs = VerifyService(task, clock=lambda: now + 10_000_000, fixture=_fixture())
    vs.dispatch('update_scene', dict(observation=obs.model_dump()))
    res = vs.dispatch('verify_skill', dict(intent=_intent(task, obs).model_dump(), after_ns=now - 50_000_000))
    assert res['outcome'] == 'success'
    assert res['reason'] == 'gripper_can_open_release_postcondition'
    assert res['evidence_ids']


def test_h52_release_can_open_and_block_visible_unheld_succeeds():
    task = _task()
    now = 20_000_000_000
    cube = SceneObject(
        object_id='square-1', shape='square', pose=tuple(np.eye(4).ravel()),
        frame_id='base', planning_margin_m=0.015, visible=True, held=False,
        stationary=True, evidence_ids=('1' * 64, '2' * 64),
    )
    obs = _obs(now_ns=now, gripper_val=0.0, objects=(cube,))
    vs = VerifyService(task, clock=lambda: now + 10_000_000, fixture=_fixture())
    vs.dispatch('update_scene', dict(observation=obs.model_dump()))
    res = vs.dispatch('verify_skill', dict(intent=_intent(task, obs).model_dump(), after_ns=now - 50_000_000))
    assert res['outcome'] == 'success'
    assert res['reason'] == 'gripper_can_open_release_postcondition'


def test_h52_release_can_not_open_fails_even_if_block_invisible():
    task = _task()
    now = 20_000_000_000
    obs = _obs(now_ns=now, gripper_val=52.0, objects=())
    vs = VerifyService(task, clock=lambda: now + 10_000_000, fixture=_fixture())
    vs.dispatch('update_scene', dict(observation=obs.model_dump()))
    res = vs.dispatch('verify_skill', dict(intent=_intent(task, obs).model_dump(), after_ns=now - 50_000_000))
    assert res['outcome'] == 'failure'
    assert res['reason'] == 'gripper_can_not_open'


def test_h52_release_incomplete_publication_never_succeeds(tmp_path):
    task = _task()
    now = 20_000_000_000
    obs = _obs(now_ns=now, gripper_val=0.0, objects=())
    vs = VerifyService(task, clock=lambda: now + 10_000_000, fixture=_fixture())
    vs.dispatch('update_scene', dict(observation=obs.model_dump()))
    res = vs.dispatch('verify_skill', dict(
        intent=_intent(task, obs).model_dump(),
        after_ns=now - 50_000_000,
        publication_complete=False,
    ))
    assert res['outcome'] != 'success'
    assert res['reason'] == 'release_publication_incomplete'

    # Also verify OwnerBridge revokes on publication_complete=False before verification can ever run.
    eng = TaskEngine(EvidenceStore(tmp_path / 'store'), mode=Mode.FAKE, clock=lambda: now)
    sess = eng.open_session()['session_id']
    eng.prepare_task(sess, task.model_dump())
    eng.local_start(physical_event=True, feedback_healthy=True)
    bridge = OwnerBridge(
        eng, numerical_planner=lambda *a, **k: None, result_verifier=lambda *a, **k: None,
        feedback_age_ns=200_000_000, reference_tolerance_deg=0.5, clock=lambda: now,
    )
    bridge.after_publication(publication_id='p1', measured16=(0.0,) * 16, publication_complete=False, now_ns=now)
    assert eng.authorized is False
    assert (eng.authority_revocation or {}).get('reason') == 'owner_publication_incomplete'
    bridge.close()
