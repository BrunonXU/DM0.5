"""First real development increment cannot silently become a precision task."""
from dataclasses import replace

import numpy as np
import pytest

from f1_harness.contracts import Task, digest
from f1_harness.geometry import tool_sweep_clear
from f1_harness.observation import ObservationBuffer, require_geometry
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.skills import SkillGeometry, propose
from test_observation import frame
from test_scene_pipeline import fixture


def test_development_scope_cannot_authorize_other_skills():
    args=dict(task_id='a',object_id='block',hole_id='hole',calibration_id='coarse',goal='approach_grasp',
              allowed_skills=('approach_grasp','reobserve'),local_corrections=0,regrasp_limit=0)
    task=Task(**args)
    assert digest(task)!=digest(Task(**(args|{'goal':'placed'})))
    for update in ({'allowed_skills':('approach_grasp','grasp')},{'regrasp_limit':1},{'local_corrections':1}):
        with pytest.raises(ValueError):Task(**(args|update))


def test_repeated_source_frame_is_discarded_then_new_pair_rechecked():
    buffer=ObservationBuffer(clock=lambda:200)
    buffer.add(frame('rgb'));buffer.add(frame('depth'))
    buffer.add(frame('rgb',sequence=2,received_ns=110,blob_sha256='c'*64))
    assert buffer.snapshot().faults
    assert buffer.snapshot().frames[0].sequence==1
    buffer.add(frame('rgb',sequence=3,source_ns=200,received_ns=200))
    buffer.add(frame('depth',sequence=2,source_ns=200,received_ns=200))
    obs=buffer.snapshot()
    assert not obs.faults and obs.pairings[0].quality=='receipt_only'
    # A late frame is rejected; only progress beyond the accepted high-water mark recovers.
    buffer.add(frame('rgb',sequence=4,source_ns=150,received_ns=210))
    buffer.add(frame('rgb',sequence=5,source_ns=300,received_ns=220))
    assert not buffer.snapshot().faults
    assert next(f for f in buffer.snapshot().frames if f.stream=='rgb').source_ns!=150
    assert buffer.diagnostic_events()[-1]['reason']=='source_time_reversed'


def test_receipt_geometry_requires_static_measured_samples_and_exact_goal():
    old,obs=fixture()
    cfg=old.config|dict(goal='approach_grasp',planning_margin_m=.015,stationary_window_ns=200_000_000,
                       reference_gripper_closure=0,reference_gripper_tolerance=.1)
    pipeline=CalibratedScenePipeline(cfg,old.segment,old.blob)
    for index in range(3):
        stamp=1+index*100_000_000
        frames=tuple(f.model_copy(update=dict(source_ns=stamp,received_ns=stamp,sequence=index+1)) for f in obs.frames)
        pair=obs.pairings[0].model_copy(update=dict(quality='receipt_only',rgb_sequence=index+1,depth_sequence=index+1))
        value=pipeline.process(obs.model_copy(update=dict(received_ns=stamp,state_received_ns=stamp,
                                                          frames=frames,pairings=(pair,))),base_eef=np.eye(4))
        assert value.stationary == (index==2)
    assert value.objects[0].inserted is None and value.objects[0].uncertainty_m is None
    assert value.objects[0].planning_margin_m==.015
    assert value.pairings[0].quality=='receipt_only'
    require_geometry(value,now_ns=stamp,max_age_ns=500_000_000,goal='approach_grasp')
    with pytest.raises(ValueError,match='coarse_geometry'):
        require_geometry(value,now_ns=stamp,max_age_ns=500_000_000)
    with pytest.raises(ValueError,match='stale'):
        require_geometry(value,now_ns=stamp+500_000_001,max_age_ns=500_000_000,goal='approach_grasp')


def test_right_camera_uses_measured_eef_and_disallows_identity_guess():
    old,obs=fixture()
    cfg=old.config|dict(camera='right',base_camera=None,eef_camera=np.eye(4).tolist(),
                       goal='approach_grasp',planning_margin_m=.015,stationary_window_ns=200_000_000,
                       reference_gripper_closure=0,reference_gripper_tolerance=.1)
    pipeline=CalibratedScenePipeline(cfg,old.segment,old.blob)
    right=obs.model_copy(update=dict(frames=tuple(f.model_copy(update={'camera':'right'}) for f in obs.frames),
                                    pairings=tuple(p.model_copy(update={'camera':'right','quality':'receipt_only'}) for p in obs.pairings)))
    eef=np.eye(4);eef[0,3]=.4
    value=pipeline.process(right,base_eef=eef)
    expected=old.process(obs,base_eef=np.eye(4)).objects[0]
    assert np.isclose(value.objects[0].pose[3]-expected.pose[3],.4)
    assert value.geometry_camera=='right'


def approach_scene():
    eef=np.eye(4);eef[:3,3]=[0,0,1.]
    obj=np.eye(4);obj[:3,3]=[.1,0,.6]
    tip=np.eye(4);tip[2,3]=-.2
    return SkillGeometry(eef,obj,None,None,None,.12,None,None,.06,.015,None,(),None,None,
                         goal='approach_grasp',eef_tip_reference=tip,maximum_approach_translation_m=.02)


def test_approach_clamps_increment_and_never_commands_gripper():
    scene=approach_scene();proposal=propose('approach_grasp',scene)
    stage=proposal['stages'][0];goal=np.array(stage['goals'][0])
    assert stage['gripper'] is None and proposal['partial_approach']
    assert np.allclose(goal[:3,:3],scene.base_eef[:3,:3])
    assert np.isclose(np.linalg.norm(goal[:3,3]-scene.base_eef[:3,3]),.02)
    for forbidden in ('grasp','insert','release','transfer_above_hole'):
        with pytest.raises(RuntimeError,match='noncontact_scope'):propose(forbidden,scene)
    with pytest.raises(RuntimeError):propose('approach_grasp',replace(scene,maximum_approach_translation_m=.031))


def test_environment_check_includes_tip_not_only_eef():
    scene=approach_scene()
    table=[dict(min=[-.3,-.3,.78],max=[.3,.3,.79])]
    assert not tool_sweep_clear(scene.base_eef,scene.base_eef,scene.eef_tip_reference,.06,table,margin_m=.015)
    with pytest.raises(RuntimeError,match='sweep_blocked'):
        propose('approach_grasp',replace(scene,obstacles=tuple(table)))
