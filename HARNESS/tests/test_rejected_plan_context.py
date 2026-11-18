"""A rejected action keeps its receipt and exposes bounded current numbers."""
from copy import deepcopy
import json

import pytest
from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.contracts import SkillIntent,digest
from f1_harness.evidence import EvidenceStore
from f1_harness.planner_geometry import recovery_geometry
from test_planner_skill_feedback import result,Backend
from test_remote_readiness import backend


def geometry():
    return dict(schema='f1-geometric-tools-v1',observation_id='measured',calibration_id='cal',
        world_frame='native',current_tcp_pose=[1,0,0,.8,0,1,0,-.3,0,0,1,.6,0,0,0,1],
        object_pose=[1,0,0,.84,0,1,0,-.34,0,0,1,.54,0,0,0,1],object_held=None,
        support=dict(gap_m=-.0013066,contact_tolerance_m=.001,contact_measured=False,
            support_observed=False,relation='model_conflict',source='registered_calibration'),
        available_skills=['move_tcp','reobserve'],cartesian_motion=dict(target_frames=['world']),
        local_environment=dict(unknown_space_is_free=False,observed_surface_aabb=dict(
            min=[.8,-.4,.5],max=[.9,-.3,.6]),free_space_views=[{'fine_depth_zlib_base64':'secret-grid'*10000}],
            local_collision_evidence_ref={'sha256':'local-only-reference'}),
        grasp_suggestions={'candidates':[{'pose':'large'}]*10000})


def test_failure_geometry_is_bounded_and_survives_unprinted_cell(tmp_path):
    context=dict(state='available',geometry=recovery_geometry(geometry()),motion_authorized=False)
    payload=result(recovery_context=context)
    arm=ArmToolkit(F1Robot(Backend(payload)),arm='cells',workspace=tmp_path,images=False)
    output=arm.execute_tool('python',{'code':'r=robo.skill("grasp")'}).result
    call=output['primitive_calls'][0];seen=call['recovery_context']['geometry']
    assert not output.get('exception') and output['stdout']==''
    assert call['no_motion'] and call['outcome']=='pending_review'
    assert seen['current_tcp_pose']==geometry()['current_tcp_pose']
    assert seen['object_held'] is None and seen['support']['gap_m']==-.0013066
    encoded=json.dumps(call['recovery_context'])
    assert len(encoded)<6000 and 'secret-grid' not in encoded and 'grasp_suggestions' not in encoded
    assert 'local-only-reference' not in encoded and 'local_collision_evidence_ref' not in encoded
    assert not seen['motion_authorized']


@pytest.mark.parametrize('fault',[None,'timeout','schema','scope','revoked'])
def test_known_rejection_context_never_replaces_owner_receipt_or_replays(monkeypatch,tmp_path,fault):
    b=backend(monkeypatch,[True]);original=b.call;queries=[];statuses=0
    b.evidence_store=EvidenceStore(tmp_path/'backend')
    def call(method,**kwargs):
        nonlocal statuses
        queries.append(method)
        if method=='geometry_context':
            if fault=='timeout':raise TimeoutError('geometry_context timed out')
            return {'schema':'bad','observation_id':'obs','calibration_id':'cal'} if fault=='schema' else geometry()
        value=original(method,**kwargs)
        if method=='status':
            statuses+=1
            if fault=='scope' and statuses==2:value['epoch']=2
            if fault=='revoked':value['authorized']=False
        return value
    b.call=call
    intent=SkillIntent(request_id='request',session_id=b.session,owner_instance_id=b.instance,
        task_id=b.task.task_id,task_hash=digest(b.task),epoch=1,observation_id='obs',scene_version='scene',
        calibration_id='cal',skill='grasp')
    receipt=result();before=deepcopy(receipt)
    value=b._rejected_plan_context(receipt,intent,deadline=10.)
    assert receipt==before and {k:value[k] for k in before}==before
    context=value['recovery_context']
    assert context['state']==('available' if fault is None else 'unavailable')
    assert not context['motion_authorized']
    assert not b.submissions and set(queries)<= {'status','geometry_context'}
    assert queries.count('geometry_context')<=1
    if fault:assert context['failure_class']=='context_query_failed'


@pytest.mark.parametrize('no_motion',[False,None])
def test_unknown_or_published_action_has_no_automatic_geometry_query(monkeypatch,no_motion):
    b=backend(monkeypatch,[True]);receipt=result(no_motion=no_motion)
    assert b._rejected_plan_context(receipt,None,deadline=10.) is receipt
    assert not b.calls


def test_execute_path_attaches_once_after_terminal_plan_rejection(monkeypatch,tmp_path):
    b=backend(monkeypatch,[True]);original=b.call;queries=[]
    b.evidence_store=EvidenceStore(tmp_path/'backend')
    def call(method,**kwargs):
        queries.append(method)
        if method=='skill_result':return result(request_id=kwargs['request_id'])
        if method=='geometry_context':return geometry()
        return original(method,**kwargs)
    b.call=call
    value=b.execute_tool('execute_skill',{'name':'grasp'})
    assert value['recovery_context']['state']=='available'
    assert len(b.submissions)==1 and queries.count('geometry_context')==1


def test_scoped_backend_keeps_depth_with_owner_and_out_of_model_observation():
    from types import SimpleNamespace
    from f1_harness.adapters import ScopedBackend
    from test_local_environment_motion import partial_setup
    _,observation,_,_=partial_setup()
    assert observation.local_collision_evidence is not None
    received=[]
    engine=SimpleNamespace(update_observation=received.append,status=lambda:dict(authorized=False))
    backend=ScopedBackend(engine,SimpleNamespace(observe=lambda:observation),session_id='local')
    value=backend.execute_tool('observe',{})
    assert received==[observation]
    assert value['observation']['observation_id']==observation.observation_id
    assert 'local_collision_evidence' not in value['observation']
    assert 'local_collision_evidence_ref' not in value['observation']
