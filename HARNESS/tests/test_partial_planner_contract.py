"""Recorded partial data through real host/Responses/PyRUA code, no hardware I/O.

The saved input is method-level native context computation, not owner admission.
World-move RPC receipts below are labelled test doubles, never physical success.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.contracts import Observation,Task,digest
from f1_harness.planner_geometry import pack_geometry

INPUT=Path(__file__).resolve().parents[1]/'artifacts/base-repair-20261117/partial-host-contract-input.json'
OBJECT_INPUT=Path(__file__).resolve().parents[1]/'artifacts/base-repair-v29-20261117/object-only-host-contract-input.json'


def recorded():
    if not INPUT.is_file():pytest.skip('Recorded V29 partial host acceptance fixture is local-only')
    value=json.loads(INPUT.read_text());Observation.model_validate(value['observation'])
    assert value['observation']['objects'][0]['pose'] is None
    assert value['observation']['objects'][0]['held'] is None
    assert value['observation']['local_collision_evidence']
    return value


def object_only_contract(source):
    # A labelled protocol variant, not a newly measured pose or physical proof.
    source=deepcopy(source);pose=np.eye(4).ravel().tolist()
    source['observation'].update(geometry_scope='coarse_object_verification')
    source['observation']['objects'][0]['pose']=pose
    source['geometry'].update(geometry_scope='coarse_object_verification',object_pose=pose)
    source['geometry']['local_environment']['pose_available']=True
    return source


def recorded_object_only():
    if not OBJECT_INPUT.is_file():pytest.skip('Recorded V29 object-only host acceptance fixture is local-only')
    value=json.loads(OBJECT_INPUT.read_text());Observation.model_validate(value['observation'])
    assert value['observation']['objects'][0]['pose'] is not None
    assert value['observation']['fixture'] is None
    assert value['observation']['local_collision_evidence']
    return value


def partial_condition(source):
    return dict(state='ready',code='ready',scope='observation',recovery='none',
        geometry_scope=source['observation']['geometry_scope'],geometry_status='partial',
        details=dict(diagnostic_only=True,reason='partial_object_geometry',
            observation_id=source['observation']['observation_id'],
            calibration_id=source['observation']['calibration_id'],
            attempts=deepcopy(source['measurement_attempts'])))


def test_depth_grids_are_removed_before_recursive_planner_geometry_copy():
    raw=deepcopy(recorded()['geometry'])
    raw['local_collision_evidence']={'private':'depth proof only'}
    raw['local_environment']['free_space_views']=[{'min_depth_m':[.123]*5000}]
    raw['local_environment']['fine_depth_zlib_base64']='private_compressed_depth'
    result=pack_geometry(raw);text=json.dumps(result)
    assert not any(name in text for name in ('min_depth_m','free_space_views','local_collision_evidence',
                                           'fine_depth_zlib_base64','private_compressed_depth'))
    assert result['current_tcp_pose']==raw['current_tcp_pose']
    assert result['local_environment']['possible_target_occupancy_aabb']==raw['local_environment']['possible_target_occupancy_aabb']
    assert result['local_environment']['unknown_space_is_free'] is False
    assert 'free_space_views' in raw['local_environment']
    assert len(text.encode())<10000


def make_remote(monkeypatch,*,object_only=False):
    from test_remote_readiness import combined_backend
    source=recorded_object_only() if object_only else recorded()
    obs=Observation.model_validate(source['observation'])
    backend=combined_backend(monkeypatch,[True]);backend.task=Task.model_validate(source['task'])
    backend.task_deadline=30.;backend.geometry_calls=[]
    original=backend.call;status=original('status')
    status['observation_readiness']=partial_condition(source)
    # Observation acquisition, authority and collision admission are tested in
    # their owner suites. Here the exact recorded data enters the host boundary.
    backend._await_observation=lambda **kwargs:(obs,deepcopy(status))
    def call(method,**kwargs):
        if method=='geometry_context':
            backend.geometry_calls.append(kwargs)
            return deepcopy(source['geometry'])
        return original(method,**kwargs)
    backend.call=call
    return backend,source


@pytest.mark.parametrize('field',['observation_id','calibration_id'])
def test_partial_observe_does_not_merge_another_snapshot_geometry(monkeypatch,field):
    from f1_harness.contracts import HarnessError
    backend,source=make_remote(monkeypatch)
    source['geometry'][field]='different-snapshot'
    with pytest.raises(HarnessError,match='geometry_context_observation_changed'):
        backend.execute_tool('observe',{})
    assert not backend.submissions


@pytest.mark.parametrize('code,tool',[
    ('r = robo.observe()','observe'),('s = robo.state()','observe'),
    ('r = robo.geometry()','geometry_context')])
@pytest.mark.parametrize('object_only',[False,True])
def test_recorded_partial_observe_and_geometry_survive_pyrua_assignment(monkeypatch,tmp_path,code,tool,object_only):
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    backend,source=make_remote(monkeypatch,object_only=object_only);robot=F1Robot(backend)
    arm=ArmToolkit(robot,arm='cells',workspace=tmp_path,images='on-demand')
    feedback=arm.execute_tool('python',{'code':code})
    assert not feedback.images and not feedback.result.get('exception')
    ledger=next(c for c in feedback.result['primitive_calls'] if c['tool']==tool)
    geometry=ledger['geometry']
    assert geometry['current_tcp_pose']==source['geometry']['current_tcp_pose']
    assert geometry['object_pose']==source['observation']['objects'][0]['pose']
    assert geometry['local_environment']['pose_available'] is object_only
    assert geometry['available_skills']==['reobserve','move_tcp']
    assert geometry['cartesian_motion']['target_frames']==['world']
    if object_only:
        assert geometry['local_environment']['environment_model']['current_fixture_available'] is False
        assert geometry['local_environment']['environment_model']['unregistered_obstacles_supported'] is False
    assert geometry['perception_boundary']['shape_label_is_independent_classification'] is False
    assert geometry['perception_boundary']['unknown_held_is_not_empty'] is True
    encoded=json.dumps(feedback.result)
    assert all(k not in encoded for k in ('min_depth_m','free_space_views','local_collision_evidence'))
    assert not backend.submissions
    if tool=='observe':
        state=robot._last_state
        actual_pose=state.objects[0]['pose']
        assert (None if actual_pose is None else list(actual_pose))==source['observation']['objects'][0]['pose']
        assert state.objects[0]['held'] is None
        assert state.geometry_scope==source['observation']['geometry_scope']
        assert state.geometry['current_tcp_pose']==source['geometry']['current_tcp_pose']
        assert list(state.quality_warnings)==ledger['quality_warnings']
        assert ledger['quality_warnings']==source['observation']['quality_warnings'][:8]
        assert len(backend.geometry_calls)==1
        if not object_only:
            diagnostic=ledger['condition']['details']['attempts'][0]
            assert diagnostic['reason']=='insufficient_upright_face_support'
            assert diagnostic['metrics']==source['measurement_attempts'][0]['metrics']
        # Even explicit print receives a bounded public observation envelope.
        returned=backend.execute_tool('observe',{})
        assert 'local_collision_evidence' not in returned['observation']


@pytest.mark.parametrize('object_only',[False,True])
def test_real_first_request_has_partial_bounds_tcp_and_no_default_grasp(monkeypatch,tmp_path,object_only):
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    from f1_harness.evidence import EvidenceStore
    from f1_harness.planner import ResponsesPlanner
    from f1_harness.task_runner import run_task
    from test_planner_bootstrap_observation import Backend
    source=recorded_object_only() if object_only else recorded()
    backend=Backend();backend.task=Task.model_validate(source['task'])
    for state in (backend.status,backend.prepared_authority):state['task_hash']=digest(backend.task)
    backend.status['perception_context'].update(task_id=backend.task.task_id,task_hash=digest(backend.task))
    def execute(name,args,**kwargs):
        backend.calls.append(name)
        assert name=='observe','Partial bootstrap must not preview or execute a default grasp'
        observation={k:v for k,v in source['observation'].items() if k!='local_collision_evidence'}
        return dict(observation=observation,geometry=pack_geometry(source['geometry']),
            status=deepcopy(backend.status),condition=partial_condition(source))
    backend.execute_tool=execute
    monkeypatch.setattr('f1_harness.task_runner.EvidenceRemoteBackend',lambda *a:backend)
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path/'cells',images='on-demand')
    arm.start=lambda:arm;arm.close=lambda:None
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm',lambda *a,**kw:arm)
    requests=[]
    class NoNetworkPlanner(ResponsesPlanner):
        def __init__(self,credentials,store):self.store=store
        def prepare_connection(self):pass
        def close(self):pass
        def request(self,**kwargs):
            requests.append(kwargs)
            return dict(output=[dict(type='function_call',name='finish',call_id='partial-inspection',
                arguments=json.dumps(dict(status='stuck',summary='offline partial contract only')))])
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner',NoNetworkPlanner)
    path=tmp_path/'task.json';path.write_text(backend.task.model_dump_json())
    args=SimpleNamespace(task=path,gateway='unused',image_gateway='unused',authorization_source='web',
        start_timeout=1,planner='astra',experience_snapshot=None)
    store=EvidenceStore(tmp_path/'planner');run_task(args,store,None)
    assert len(requests)==1
    encoded=json.dumps(requests[0]['input'])
    assert 'Current geometry is partial' in encoded
    assert 'current_tcp_pose' in encoded and 'possible_target_occupancy_aabb' in encoded
    assert 'unknown_space_is_free' in encoded and 'source_observation_id' in encoded
    if not object_only:
        assert 'insufficient_upright_face_support' in encoded and 'inlier_points' in encoded
    assert 'shape_label_is_independent_classification' in encoded
    assert all(k not in encoded for k in ('min_depth_m','free_space_views','local_collision_evidence','input_image'))
    assert 'inspect_plan' not in backend.calls and 'execute_skill' not in backend.calls
    rows=[json.loads(l) for l in (store.root/'trace.jsonl').read_text().splitlines()]
    initial=next(r['summary'] for r in rows if r['kind']=='planner_initial_observation')
    assert not initial['geometry_available'] and initial['local_environment_available']
    assert initial['objects'][0]['held'] is None and initial['objects'][0]['pose']==source['observation']['objects'][0]['pose']
    assert initial['object_pose_available'] is object_only
    if object_only:
        assert 'target pose is measured, but the environment is incomplete' in encoded
        assert 'its object pose and grasp pose are unavailable' not in encoded


@pytest.mark.parametrize('object_only',[False,True])
def test_world_parameters_and_request_receipt_survive_real_host_and_pyrua(monkeypatch,tmp_path,object_only):
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    backend,source=make_remote(monkeypatch,object_only=object_only)
    target=np.array(source['geometry']['current_tcp_pose']).reshape(4,4);target[0,3]+=.002
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path,images='on-demand')
    feedback=arm.execute_tool('python',{'code':'r = robo.move_to('+repr(target.ravel().tolist())+', frame="world")'})
    assert not feedback.result.get('exception') and not feedback.images
    assert len(backend.submissions)==1
    intent=backend.submissions[0]
    assert intent['skill']=='move_tcp' and intent['parameters']['target_frame']=='world'
    assert list(intent['parameters']['target_tcp_pose'])==target.ravel().tolist()
    assert not intent['parameters']['support_contact']
    result=next(r for r in feedback.result['primitive_calls'] if r['tool']=='execute_skill')
    assert result['request_id']==intent['request_id'] and result['outcome']=='success'
    assert not backend.solved()  # A mocked world-motion receipt never completes placement.


@pytest.mark.parametrize('code,tool',[
    ('p = robo.inspect_plan("move_tcp", target_tcp_pose='+repr(np.eye(4).ravel().tolist())+', target_frame="world")','inspect_plan'),
    ('r = robo.move_to('+repr(np.eye(4).ravel().tolist())+', frame="world")','execute_skill')])
def test_unknown_space_numbers_survive_unprinted_pyrua_feedback(monkeypatch,tmp_path,code,tool):
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    from f1_harness.contracts import HarnessError
    from f1_harness.owner_bridge import numerical_failure_detail
    backend,source=make_remote(monkeypatch)
    metrics=dict(cell_min=[.1,.2,.3],cell_max=[.2,.3,.4],possible_target_overlap=True,
        views=[dict(camera='right',reason='depth_pixels_unknown',unknown_pixels=7,
            min_depth_m=[9]*1000,fine_depth_zlib_base64='private-grid')],
        cells_tested=19,unknown_space_is_free=False,evidence_observation_id=source['observation']['observation_id'])
    detail=numerical_failure_detail(HarnessError('local_observation_space_unknown:'+json.dumps(
        dict(diagnostic=dict(code='local_observation_space_unknown',metrics=metrics)))))
    backend.execute_tool=lambda name,args,**kw:dict(planned=False,outcome='failure',no_motion=True,
        request_id='offline-rejected-request',reason='local_observation_space_unknown',
        planning_diagnostic=detail,geometry=source['geometry'])
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path,images='on-demand')
    feedback=arm.execute_tool('python',{'code':code})
    assert not feedback.result.get('exception') and not feedback.images
    ledger=next(row for row in feedback.result['primitive_calls'] if row['tool']==tool)
    actual=ledger['planning_diagnostic']['metrics']
    assert actual['cell_min']==metrics['cell_min'] and actual['cell_max']==metrics['cell_max']
    assert actual['views'][0]['unknown_pixels']==7 and actual['unknown_space_is_free'] is False
    assert actual['evidence_observation_id']==source['observation']['observation_id']
    assert not any(key in json.dumps(ledger) for key in ('min_depth_m','fine_depth_zlib_base64','private-grid'))


def test_original_partial_can_freeze_discovery_metadata_without_an_object_pose():
    from f1_harness.program_trials import build_program_trial,validate_program_trial
    source=recorded();task=Task.model_validate(source['task']);obs=Observation.model_validate(source['observation'])
    scope=dict(owner_instance_id='offline-owner',session_id='offline-session',task_id=task.task_id,
        task_hash=digest(task),epoch=1,calibration_id=task.calibration_id)
    trial=build_program_trial(obs,task,scope=scope,owner_run_id='offline-contract-test')
    assert validate_program_trial(trial)==trial
    assert trial['mode']=='discovery' and not trial['heldout'] and not trial['generalization_proven']
    assert trial['observation']==obs.model_dump(mode='json')
    assert trial['observation']['objects'][0]['held'] is None
    assert trial['observation']['objects'][0]['pose'] is None


def test_partial_independent_candidate_is_registered_by_owner_before_any_action(monkeypatch,tmp_path):
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.task_runner import _freeze_program_trial
    import f1_harness.program_experience as programs
    source=recorded();task=Task.model_validate(source['task']);obs=Observation.model_validate(source['observation'])
    # Owner protocol fixture only: original observed data enters an in-memory
    # engine, with no bridge, transport, publisher, or program execution.
    engine=TaskEngine(EvidenceStore(tmp_path/'owner'),mode='hardware',clock=lambda:obs.received_ns)
    engine.host_boot_id=obs.host_boot_id
    session=engine.open_session()['session_id'];engine.prepare_task(session,task.model_dump())
    engine.program_trials_enabled=True;engine.recording_identity={'owner_run_id':'partial-validation-fixture'}
    engine.hardware_ready=True;engine.local_start(physical_event=True,feedback_healthy=True)
    engine.update_observation(obs)
    descriptor=dict(candidate_id='a'*64,source_sha256='b'*64,discovery_run='previous-fixture',
        discovery_scene='previous-scene',discovery_scenario='previous-scenario',discovery_source_sha256='c'*64,
        discovery_scope=dict(owner_instance_id='previous-owner',session_id='previous-session',task_id=task.task_id,
            task_hash=digest(task),epoch=1,calibration_id=task.calibration_id))
    candidate=dict(id='a'*64,trial_candidate=descriptor)
    monkeypatch.setattr(programs,'pending_agent_programs',lambda *a,**k:[candidate])
    monkeypatch.setattr(programs,'validated_agent_programs',lambda *a,**k:[])
    backend=SimpleNamespace(session=session,call=lambda method,**kwargs:getattr(engine,method)(**kwargs))
    selected=_freeze_program_trial(backend,task,{},tmp_path,dict(geometry_available=False,
        local_environment_available=True,observation_id=obs.observation_id),EvidenceStore(tmp_path/'host'),supported=True)
    assert selected==candidate
    trial=engine.program_trial
    assert trial['mode']=='validation' and trial['candidate']==descriptor
    assert trial['observation']==obs.model_dump(mode='json')
    assert not engine.task_requests and not engine.request_publications
    assert not engine.completed_skills and trial['generalization_proven'] is False
