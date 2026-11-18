"""The physical V27 empty grasp is a regression input, never simulated success."""
from concurrent.futures import Future
import json
from pathlib import Path

import numpy as np
import pytest

from f1_harness.contracts import Frame
from f1_harness.grasp_tracking import revalidate_nonfollowing
from f1_harness.host import fake_host
from f1_harness.owner_bridge import OwnerBridge
from test_full_development_scene import full_scene, sample
from test_grasp_tracking import measured
from test_owner_bridge import install_running


BOX = dict(min=[-.015, -.015, 0.], max=[.015, .015, .06])


def test_real_v27_miss_survives_noisy_pose_and_reconciles_new_stable_measurements():
    record = json.loads((Path(__file__).parent/'fixtures/v27_miss_measurements.json').read_text())
    pipe, observation = full_scene()
    pipe.config.update(tracking_tolerance_m=.005, gripper_stationary_delta=.5,
                       search_object_box=BOX)
    pipe.last_timing={}
    outcomes = {}
    try:
        for row in record['samples']:
            evidence = row['measurements']
            values = list(observation.state16)
            values[7:14] = row['joints']; values[15] = row['gripper']
            obs = observation.model_copy(update=dict(observation_id=row['observation_id'],
                frames=tuple(Frame.model_validate(f) for f in row['frames']),
                state16=tuple(values), state_received_ns=evidence['state_received_ns'],
                received_ns=evidence['received_ns']))
            result = pipe._primary_evidence(obs,np.asarray(row['object_pose']),np.asarray(row['eef_pose']),
                evidence['source_ns'],camera=evidence['camera'])
            suffix = row['observation_id'].rsplit(':',1)[1]
            outcomes[suffix] = result[-1]
            if suffix == '350':
                assert result[-1] is None
                assert pipe.not_in_hand_reference is not None
                assert pipe.not_in_hand_reference['suspended']
        assert outcomes['349'] is False
        assert outcomes['350'] is None
        assert outcomes['351'] is None
        assert outcomes['353'] is False
        assert pipe.last_timing['primary_evidence']['reason']=='measured_nonfollowing_revalidated'
        assert True not in outcomes.values()
    finally:
        pipe.close()


@pytest.mark.parametrize('change',['object','camera','scope','gripper','duplicate','moving_object'])
def test_historical_miss_cannot_turn_unrelated_or_unknown_samples_into_empty(change):
    origin = measured(0, eef_z=.1, object_z=0.)
    reference = {key:origin[key] for key in ('obj','gripper','camera_identity','time','state_ns','source_stamps')}
    rows = [measured(i,eef_z=.1,object_z=0.,relative_x=.01) for i in (1,2,3)]
    if change=='object':
        for r in rows:r['obj'][0,3]=.15
    if change=='camera':rows[-1]['camera_identity']=('different',)
    if change=='scope':rows[-1]['scope_identity']=('different',)
    if change=='gripper':rows[-1]['gripper']=18.
    if change=='duplicate':
        for r in rows:r['source_stamps']=(200_000_000,)*2
    if change=='moving_object':
        for i,r in enumerate(rows):r['obj'][0,3]=i*.015
    result=revalidate_nonfollowing(reference,rows,object_box=BOX,
        translation_tolerance=.005,gripper_tolerance=.5)
    assert not result['restored']


def test_regrasp_request_preserves_empty_proof_until_physical_gripper_change():
    pipe,_=full_scene()
    try:
        context=dict(owner_instance_id='owner',session_id='session',task_id='task',
                     task_hash='hash',task_generation=1,epoch=1,phase='grasp',request_id='old')
        pipe.last_timing={};pipe._context(context,1)
        reference={'proof':'independently measured miss'}
        pipe.not_in_hand_reference=reference
        pipe._context(dict(context,phase='regrasp',request_id='new'),2)
        assert pipe.not_in_hand_reference is reference
        # Scope replacement still discards evidence; starting a request does not.
        pipe._context(dict(context,epoch=2,phase='regrasp',request_id='newer'),3)
        assert pipe.not_in_hand_reference is None
    finally:pipe.close()


def test_new_measured_following_supersedes_old_empty_fact():
    pipe, observation=full_scene()
    pipe.config.update(tracking_tolerance_m=.005,gripper_stationary_delta=.5,search_object_box=BOX)
    pipe.last_timing={}
    try:
        for i in range(6):
            row=measured(i,eef_z=i*.02,object_z=0. if i<3 else (i-2)*.02)
            obs=sample(observation,i)
            q=list(obs.state16);q[7]=i*2.;q[15]=100.
            obs=obs.model_copy(update={'state16':tuple(q)})
            frames=[f for f in obs.frames if f.camera=='right']
            result=pipe._primary_evidence(obs,row['obj'],row['eef'],
                tuple(f.source_ns for f in frames),camera='right')
            if i==2:
                assert result[-1] is False and pipe.not_in_hand_reference is not None
        assert result[-1] is True
        assert pipe.not_in_hand_reference is None
    finally:pipe.close()


@pytest.mark.parametrize('motion',['static','carried'])
def test_cross_view_uses_object_volume_and_current_hand_motion(motion):
    pipe, obs = full_scene()
    pipe.config.update(search_object_box=BOX,tracking_tolerance_m=.005)
    pipe.object_camera='right';pipe.last_timing={}
    original=np.eye(4);original[:3,3]=[.837605,-.336949,.543696]
    pipe.search_hint=dict(pose=original.copy(),eef_object=original.copy(),received_ns=1)
    current=np.eye(4)
    try:
        wrong=original.copy();wrong[:3,3]=[.790164,-.369835,.682218]
        with pytest.raises(ValueError,match='object_identity_unresolved'):
            pipe._associate_object_view(wrong,'head',obs,current)
        np.testing.assert_array_equal(pipe.search_hint['pose'],original)
        assert pipe.object_camera=='right'
        right=original.copy()
        if motion=='carried':
            current[0,3]=.2;right[0,3]+=.2
            pipe.in_hand_reference=original.copy()
            # Zero is fully open even when the registered release is 18.
            pipe.config['gripper_open_range']=[17.5,18.5]
            states=list(obs.state16);states[15]=0.
            opened=obs.model_copy(update={'state16':tuple(states)})
            with pytest.raises(ValueError,match='object_identity_unresolved'):
                pipe._associate_object_view(right,'head',opened,current)
            states=list(obs.state16);states[15]=100.
            obs=obs.model_copy(update={'state16':tuple(states)})
        pipe._associate_object_view(right,'head',obs,current)
        assert pipe.last_timing['object_identity']['status']=='associated'
    finally:pipe.close()


@pytest.mark.parametrize('published',[False,True])
def test_plan_failure_receipt_reports_actual_publication_state(tmp_path,published):
    from test_unpublished_plan import make_bridge
    engine,bridge,clock,_,poll,_=make_bridge(tmp_path)
    try:
        assert poll() is None
        if published:engine.record_skill_publication('one-request',publication_id='published',now_ns=clock[0])
        bridge.future.set_exception(RuntimeError('regrasp_requires_confirmed_empty_gripper_or_registered_station'))
        assert poll() is None
        result=engine.results['one-request']
        assert result.no_motion is (not published)
        assert result.reason=='numerical_plan_rejected:regrasp_requires_confirmed_empty_gripper_or_registered_station'
    finally:bridge.close()


def test_empty_evidence_missing_before_any_regrasp_publication_preserves_physical_budget(tmp_path):
    from test_unpublished_plan import make_bridge
    engine,bridge,_,_,poll,_=make_bridge(tmp_path)
    engine.pending=engine.pending.model_copy(update={'skill':'regrasp'})
    engine.completed_skills={'approach_grasp'}
    engine.active_grasp_selection={'provenance':'previous-completed-approach'}
    try:
        poll()
        assert engine.regrasps==1 and engine.active_grasp_selection is None
        bridge.future.set_exception(RuntimeError('regrasp_requires_confirmed_empty_gripper_or_registered_station'))
        poll()
        assert engine.results['one-request'].no_motion
        assert engine.regrasps==0 and 'regrasp' not in engine.attempted_motion
        assert engine.completed_skills=={'approach_grasp'}
        assert engine.active_grasp_selection=={'provenance':'previous-completed-approach'}
        assert not engine.request_publications
    finally:bridge.close()
