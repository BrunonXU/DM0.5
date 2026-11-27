"""Regression tests for loaded align/insert revalidation in held-object frame."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.contracts import SkillIntent, Task, digest
from f1_harness.revalidation import RevalidationSceneChanged, revalidate_candidate
from test_full_development_skills import pose, scene
from test_plan_revalidation import Numerics, observation, service


class AlignNumerics(Numerics):
    def call(self, method, **kw):
        if method == 'endpoint_ik':
            return [dict(converged=True, min_margin_deg=15.0) for _ in kw['goals']]
        return super().call(method, **kw)


def _loaded_align_scenario(clock, *, attachment, hole_offset=(0.0, 0.0, 0.0)):
    task = Task(task_id='task', object_id='block', hole_id='hole', calibration_id='coarse',
                admission_profile='development_coarse')
    serv = service(clock, task)
    serv.numerics = AlignNumerics()
    serv.fixture.update(
        transfer_policy='registered_transport',
        effective_clearance_m=0.002,
        object_planning_margin_m=0.0003,
        object_local_box=dict(min=[-0.015, -0.015, 0.0], max=[0.015, 0.015, 0.06]),
    )

    def make_obs(name, att, *, hole_shift=(0.0, 0.0, 0.0)):
        obs = observation(clock[0], name=name)
        base_eef = scene().base_eef.copy()
        obj_pose = base_eef @ att
        hole_pose = np.array(obs.objects[1].pose).reshape(4, 4).copy()
        hole_pose[:3, 3] += np.array(hole_shift)
        obj = obs.objects[0].model_copy(update=dict(
            held=True,
            pose=tuple(obj_pose.ravel()),
            eef_object_pose=tuple(att.ravel()),
        ))
        hole = obs.objects[1].model_copy(update=dict(pose=tuple(hole_pose.ravel())))
        return obs.model_copy(update=dict(objects=(obj, hole)))

    original = make_obs('orig', attachment)
    intent = SkillIntent(
        request_id='align-req', session_id='session', owner_instance_id='owner',
        task_id=task.task_id, task_hash=digest(task), epoch=1,
        observation_id=original.observation_id, scene_version=original.scene_version,
        calibration_id=task.calibration_id, skill='align',
    )
    held = list(original.state16)
    full = [0.0] * 22
    candidate = serv.dispatch('plan_skill', dict(
        intent=intent.model_dump(), observation=original.model_dump(), full22=full, held16=held,
    ))
    after = clock[0]
    clock[0] += 100_000_000
    return serv, task, intent, original, candidate, full, held, after, make_obs


def test_align_revalidation_uses_attached_object_frame_not_wrist_lever_arm():
    """Stationary <1 deg in-hand rotation noise shifts Link7 >3 mm at 29 cm lever arm,
    but moves the held block <0.6 mm — align revalidation must accept it and sweep all segments."""
    clock = [1_000_000_000]
    att0 = pose(0.0, 0.0, -0.29)
    serv, task, intent, original, candidate, full, held, after, make_obs = _loaded_align_scenario(
        clock, attachment=att0,
    )
    # 0.9 deg stationary yaw noise + 0.2 mm translation noise -> ~4.5 mm wrist lever-arm shift,
    # while every corner of the 30x30x60 mm block moves < 0.6 mm (<= 1.70 mm tolerance).
    att1 = att0.copy()
    att1[:3, :3] = att1[:3, :3] @ Rotation.from_euler('z', 0.9, degrees=True).as_matrix()
    att1[0, 3] += 0.0002
    latest = make_obs('fresh', att1)
    proof = revalidate_candidate(
        serv, intent=intent.model_dump(), proposal=candidate,
        original_observation=original.model_dump(), observation=latest.model_dump(),
        full22=full, held16=held, after_ns=after,
    )
    assert proof['geometry_checks']['goal_reference'] == 'attached_object'
    assert proof['geometry_checks']['swept_segments'] > 0


def test_align_revalidation_still_rejects_true_block_shift_or_tilt():
    """Real hole/block shift > 1.70 mm or tilt moving block corners > 1.70 mm must still fail."""
    clock = [1_000_000_000]
    att0 = pose(0.0, 0.0, -0.29)
    serv, task, intent, original, candidate, full, held, after, make_obs = _loaded_align_scenario(
        clock, attachment=att0,
    )
    # 2.5 mm real hole shift -> exceeds 1.70 mm remaining clearance -> MUST reject
    latest_shifted = make_obs('shifted', att0, hole_shift=(0.0025, 0.0, 0.0))
    with pytest.raises(RevalidationSceneChanged, match='revalidation_goal_changed|revalidation_endpoint_outside_remaining_clearance'):
        revalidate_candidate(
            serv, intent=intent.model_dump(), proposal=candidate,
            original_observation=original.model_dump(), observation=latest_shifted.model_dump(),
            full22=full, held16=held, after_ns=after,
        )
    # 2.5 deg pitch tilt on 60 mm tall block -> top corner moves ~2.6 mm > 1.70 mm -> MUST reject
    att_tilted = att0.copy()
    att_tilted[:3, :3] = att_tilted[:3, :3] @ Rotation.from_euler('y', 2.5, degrees=True).as_matrix()
    latest_tilted = make_obs('tilted', att_tilted)
    with pytest.raises(RevalidationSceneChanged, match='revalidation_endpoint_outside_remaining_clearance'):
        revalidate_candidate(
            serv, intent=intent.model_dump(), proposal=candidate,
            original_observation=original.model_dump(), observation=latest_tilted.model_dump(),
            full22=full, held16=held, after_ns=after,
        )
