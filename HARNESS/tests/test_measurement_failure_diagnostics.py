"""Real per-view causes survive fallback, RPC readiness and the PyRUA ledger."""
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.conditions import (observation_condition, object_measurement_recoverable,
    with_observation_details, compact_observation_condition)
from f1_harness.live_scene import LiveSceneObserver
from test_full_development_scene import full_scene, sample
from test_scene_pipeline import fixture

QUALITY='insufficient_upright_face_support'
METRICS=dict(input_points=3790,inlier_points=836,inlier_fraction_of_mask_points=836/3790,
    observed_planar_spans_m=[.02042,.01217],accepted=False,diagnostic_only=True,
    failed_checks=[QUALITY,'observed_face_is_too_narrow'])


def test_primary_geometry_failure_survives_missing_secondary_and_next_frame(monkeypatch):
    import f1_harness.scene_pipeline as module
    pipeline,obs=full_scene();pipeline.config['object_reacquisition_camera']='head'
    measured=module.object_from_mask;fail=[True]
    def measure(**kwargs):
        if fail[0]:
            kwargs['diagnostics']['surface_selection']=deepcopy(METRICS)
            raise ValueError(QUALITY)
        return measured(**kwargs)
    monkeypatch.setattr(module,'object_from_mask',measure)
    try:
        frame=sample(obs,0)
        with pytest.raises(ValueError,match='^'+QUALITY+'$') as caught:
            pipeline.process(frame,base_eef=np.eye(4))
        assert observation_condition(str(caught.value))['state']=='waiting'
        attempts=pipeline.last_timing['object_geometry_attempts']
        assert [a['camera'] for a in attempts]==['right','head']
        assert attempts[0]['mask_available'] is True and attempts[0]['stage']=='geometry'
        assert attempts[0]['metrics']['surface_selection']==METRICS
        assert attempts[1]['reason']=='object_mask_missing:camera=head:object=block'
        assert attempts[1]['metrics']=={} and attempts[1]['mask_available'] is None
        assert all(a['observation_id']==frame.observation_id for a in attempts)
        assert attempts[0]['frames'][0]['source_ns']==frame.frames[0].source_ns
        fail[0]=False
        scene=pipeline.process(sample(obs,1),base_eef=np.eye(4))
        assert scene.geometry_camera=='right'
        assert 'object_geometry_attempts' not in pipeline.last_timing
    finally:pipeline.close()


@pytest.mark.parametrize('head_error',[
    'camera_calibration_identity_changed','secondary_camera_pairing_unavailable',
    'unexpected:insufficient_upright_face_support',
    'mask_view=head:mask_uncertain_requires_new_prompt:object=hole',
    'mask_view=right:mask_uncertain_requires_new_prompt:object=block',
])
def test_primary_quality_never_masks_secondary_identity_or_unrelated_entity(monkeypatch,head_error):
    import f1_harness.scene_pipeline as module
    pipeline,obs=full_scene();pipeline.config['object_reacquisition_camera']='head'
    def rejected(**kwargs):raise ValueError(QUALITY)
    monkeypatch.setattr(module,'object_from_mask',rejected)
    original=pipeline.segment
    def segment(rgb,view):
        if view=='head':raise ValueError(head_error)
        return original(rgb,view)
    pipeline.segment=segment
    try:
        with pytest.raises(ValueError) as caught:pipeline.process(sample(obs,0),base_eef=np.eye(4))
        assert str(caught.value)==head_error
        assert [a['reason'] for a in pipeline.last_timing['object_geometry_attempts']]==[QUALITY,head_error]
    finally:pipeline.close()


@pytest.mark.parametrize('reason',[QUALITY,'insufficient valid depth','surface is not a reliable planar block face'])
def test_exact_geometry_failures_are_waiting_but_embedded_words_are_not(reason):
    assert object_measurement_recoverable(reason)
    assert observation_condition(reason)['state']=='waiting'
    assert not object_measurement_recoverable('unknown:'+reason)
    assert not object_measurement_recoverable(reason+'_identity_changed')
    assert observation_condition('unknown:'+reason)['state']=='needs_attention'


def scoped_details(context,reason=QUALITY):
    return dict(diagnostic_only=True,reason=reason,observation_id='recorded-frame-9',calibration_id='cal',
        scope={k:context[k] for k in ('owner_instance_id','session_id','task_hash','epoch')},
        attempts=[dict(camera='right',object_id='block',mask_available=True,stage='geometry',reason=reason,
            observation_id='recorded-frame-9',metrics={'surface_selection':deepcopy(METRICS)},
            frames=[dict(stream='rgb',source_ns=100,received_ns=200,blob_sha256='a'*64)]),
            dict(camera='head',object_id='block',mask_available=None,stage='segmentation_or_view',
                 observation_id='recorded-frame-9',reason='object_mask_missing:camera=head:object=block',
                 frames=[],metrics={})])


def test_live_failure_keeps_matching_details_and_recorder_snapshot():
    observer=LiveSceneObserver.__new__(LiveSceneObserver)
    context=dict(owner_instance_id='owner',session_id='session',task_hash='hash',epoch=1)
    details=scoped_details(context);captured=[]
    observer.stop=threading.Event();observer.config={'poll_seconds':.01};observer.lock=threading.Lock()
    observer.latest=object();observer.error=None;observer.raw=SimpleNamespace(store=SimpleNamespace(blob=lambda _:b''))
    observer.pipeline=SimpleNamespace(reject_measurement=lambda _:None,segment=None)
    observer.owner=SimpleNamespace(call=lambda *a,**kw:None)
    observer.store=SimpleNamespace(event=lambda *a,**kw:observer.stop.set())
    observer.failure_recorder=SimpleNamespace(offer=lambda *a:captured.append(a),status=lambda:{})
    def fail():
        observer.failure_input=dict(context=context,observation=dict(observation_id='recorded-frame-9',calibration_id='cal'))
        observer.last_timing={'pipeline':{'object_geometry_attempts':deepcopy(details['attempts'])}}
        raise ValueError(QUALITY)
    observer._sample=fail
    observer._run()
    readiness=observer.readiness()
    assert readiness['state']=='waiting' and readiness['code']==QUALITY
    assert readiness['details']['attempts'][0]['metrics']['surface_selection']==METRICS
    assert captured[0][1]['measurement_diagnostics']==readiness['details']
    readiness['details']['attempts'][0]['metrics'].clear()
    assert observer.readiness()['details']['attempts'][0]['metrics']
    # A subsequent hard error before geometry cannot inherit the old fit.
    observer.stop.clear()
    def unknown():
        observer.failure_input=None;observer.last_timing={'pipeline':{}}
        raise ValueError('camera_identity_changed')
    observer._sample=unknown;observer._run()
    assert observer.readiness()['state']=='needs_attention'
    assert 'details' not in observer.readiness()


def test_live_success_clears_old_failure_details(monkeypatch):
    from test_phase_live_scene import observer
    scene,_=observer(monkeypatch);scene.error_details={'old':True}
    scene._sample()
    assert scene.error_details is None and 'details' not in scene.readiness()


@pytest.mark.parametrize('fault',['epoch','reason','source','size','malformed_scope','nonfinite'])
def test_stale_or_unbounded_details_cannot_join_a_current_missing_observation(fault):
    context=dict(owner_instance_id='owner',session_id='session',task_hash='hash',epoch=1)
    condition=observation_condition(QUALITY);details=scoped_details(context)
    status=dict(perception_context=context,observation_readiness=dict(condition,details=details))
    if fault=='epoch':details['scope']['epoch']=0
    elif fault=='reason':details['reason']='another failure'
    elif fault=='source':status['observation_readiness']['code']='scene_not_ready'
    elif fault=='size':details['extra']='x'*13000
    elif fault=='malformed_scope':details['scope']=['owner']
    elif fault=='nonfinite':details['attempts'][0]['metrics']['nan']=float('nan')
    assert 'details' not in with_observation_details(condition,status)


@pytest.mark.parametrize('tool,code',[('observe','r = robo.observe()'),('inspect_plan','r = robo.inspect_plan("approach_grasp")')])
def test_actual_pyrua_assignment_exposes_numeric_view_failures_without_images(monkeypatch,tmp_path,tool,code):
    from rpent.utils.rpc.rpc_client import RpcError
    from pyrualean.arms import ArmToolkit
    from f1_harness.adapters import F1Robot
    from test_remote_readiness import combined_backend
    backend=combined_backend(monkeypatch,[RpcError('observe_with_status',QUALITY)])
    backend.task_deadline=.15;original=backend.call
    def call(method,**kwargs):
        result=original(method,**kwargs)
        if method=='status':
            result['perception_context'].update({k:result[k] for k in ('owner_instance_id','task_hash','epoch')})
            result['observation_readiness']=dict(observation_condition(QUALITY),
                details=scoped_details(result['perception_context']))
        return result
    backend.call=call
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path,images='on-demand')
    feedback=arm.execute_tool('python',dict(code=code))
    assert not feedback.images and not feedback.result.get('exception')
    ledger=next(c for c in feedback.result['primitive_calls'] if c['tool']==tool)
    details=ledger['condition']['details']
    assert details['attempts'][0]['metrics']['surface_selection']==METRICS
    assert details['attempts'][0]['source_ns']=={'rgb':100}
    assert 'blob_sha256' not in json.dumps(details)
    assert not backend.submissions
    assert len(json.dumps(ledger['condition'],ensure_ascii=False).encode())<4000


def test_large_future_metrics_keep_causes_without_unbounded_ledger():
    context=dict(owner_instance_id='owner',session_id='session',task_hash='hash',epoch=1)
    details=scoped_details(context);details['attempts'][0]['metrics']['future_array']=['x'*100]*100
    value=compact_observation_condition(dict(observation_condition(QUALITY),details=details))
    assert len(json.dumps(value).encode())<4000
    assert [a['camera'] for a in value['details']['attempts']]==['right','head']
    assert value['details']['attempts'][0]['reason']==QUALITY
    assert 'metrics_omitted' in value['details']


def test_real_first_responses_input_contains_view_numbers_without_image(monkeypatch,tmp_path):
    from f1_harness.adapters import F1Robot
    from f1_harness.evidence import EvidenceStore
    from f1_harness.planner import ResponsesPlanner
    from f1_harness.remote import ObservationWaitEnded
    from f1_harness.task_runner import run_task
    from pyrualean.arms import ArmToolkit
    from test_planner_bootstrap_observation import Backend
    backend=Backend(ObservationWaitEnded('observation_readiness_timeout:observation_unavailable:'
        'RpcError:observe_with_status: '+QUALITY))
    backend.status['observation_readiness']=dict(observation_condition(QUALITY),
        details=scoped_details(backend.status['perception_context']))
    requests=[]
    monkeypatch.setattr('f1_harness.task_runner.EvidenceRemoteBackend',lambda *a:backend)
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path/'cells',images='on-demand')
    arm.start=lambda:arm;arm.close=lambda:None
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm',lambda *a,**kw:arm)
    class NoNetworkPlanner(ResponsesPlanner):
        def __init__(self,credentials,store):self.store=store
        def prepare_connection(self):pass
        def close(self):pass
        def request(self,**kwargs):
            requests.append(kwargs)
            return dict(output=[dict(type='function_call',name='finish',call_id='diagnostic',
                arguments=json.dumps(dict(status='stuck',summary='offline diagnostic acceptance')))])
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner',NoNetworkPlanner)
    path=tmp_path/'task.json';path.write_text(backend.task.model_dump_json())
    args=SimpleNamespace(task=path,gateway='unused',image_gateway='unused',authorization_source='web',
        start_timeout=1,planner='astra',experience_snapshot=None)
    run_task(args,EvidenceStore(tmp_path/'planner'),None)
    assert len(requests)==1
    encoded=json.dumps(requests[0]['input'],ensure_ascii=False)
    assert QUALITY in encoded and 'object_mask_missing:camera=head:object=block' in encoded
    assert '3790' in encoded and '836' in encoded and '0.02042' in encoded
    assert 'input_image' not in encoded and not backend.images
    assert 'execute_skill' not in backend.calls


def test_partial_success_keeps_same_frame_fit_diagnostics_without_a_new_wait_state(monkeypatch):
    from test_phase_live_scene import observer
    scene,_=observer(monkeypatch);raw=scene.raw.observe()
    def partial(value,**kwargs):
        details=scoped_details(kwargs['context'])
        for attempt in details['attempts']:attempt['observation_id']=value.observation_id
        scene.pipeline.last_timing={'object_geometry_attempts':details['attempts']}
        return value.model_copy(update={'geometry_scope':'coarse_local_environment'})
    scene.pipeline.process=partial
    scene._sample()
    readiness=scene.readiness()
    assert readiness['state']=='ready' and readiness['geometry_status']=='partial'
    assert readiness['details']['observation_id']==raw.observation_id
    assert readiness['details']['attempts'][0]['metrics']['surface_selection']==METRICS
    first=compact_observation_condition(readiness)
    assert compact_observation_condition(first)==first


@pytest.mark.parametrize('contaminated_source',[False,True])
@pytest.mark.parametrize('secondary_fault',[None,'duplicate_source_stamp:head'])
def test_real_partial_pipeline_survives_unpaired_secondary_in_live_ingestion(monkeypatch,tmp_path,contaminated_source,secondary_fault):
    from test_phase_live_scene import observer
    from test_partial_scene import partial_fixture
    from test_full_development_scene import sample
    from f1_harness.observation import require_geometry_identity
    scene,verified=observer(monkeypatch)
    from f1_harness.evidence import EvidenceStore
    from f1_harness.scene_evidence import bind_store
    scene.store=EvidenceStore(tmp_path/'gateway')
    scene.scene_evidence_store=bind_store(tmp_path,scene.store)
    scene.config['archive_budget_bytes']=16*1024*1024
    pipeline,original=partial_fixture(monkeypatch)
    raw=sample(original,0)
    raw=raw.model_copy(update={'pairings':tuple(p for p in raw.pairings if p.camera=='right')})
    if secondary_fault:raw=raw.model_copy(update={'faults':(secondary_fault,)})
    scene.raw.observe=lambda:raw
    scene.pipeline=pipeline
    scene.config['geometry']=pipeline.config
    if contaminated_source:
        process=pipeline.process
        def contaminated(*args,**kwargs):
            value=process(*args,**kwargs)
            evidence=value.local_collision_evidence.model_copy(update={'source_frames':raw.frames})
            return value.model_copy(update={'local_collision_evidence':evidence})
        pipeline.process=contaminated
    try:
        if contaminated_source:
            from f1_harness.scene_pipeline import SecondaryCameraUnavailable
            with pytest.raises(SecondaryCameraUnavailable,match='secondary_camera_pairing_unavailable'):
                scene._sample()
            assert not verified and not scene.ingested
            return
        scene._sample()
        assert verified and len(scene.ingested)==1
        assert scene.latest.geometry_scope=='coarse_local_environment'
        assert not scene.latest.faults and {f.camera for f in scene.latest.frames}=={'right'}
        assert {v.camera for v in scene.latest.local_collision_evidence.free_space_views}=={'right'}
        assert scene.latest.objects[0].pose is None and scene.latest.objects[0].held is None
        assert require_geometry_identity(scene.latest,admission_profile='development_coarse',
            goal='placed',skill='reobserve')==('right',)
        condition=scene.readiness()
        assert condition['state']=='ready' and condition['geometry_status']=='partial'
        assert condition['details']['attempts'][0]['reason']=='observed_face_is_too_narrow'
    finally:pipeline.close()


def test_object_only_local_readiness_does_not_hide_available_observation_motion(monkeypatch):
    from test_phase_live_scene import observer
    from test_partial_planner_contract import recorded,object_only_contract
    from f1_harness.contracts import Observation
    scene,_=observer(monkeypatch)
    value=object_only_contract(recorded())['observation']
    scene.latest=Observation.model_validate(value);scene.error=None
    result=scene.readiness()
    assert result['state']=='ready' and result['geometry_status']=='partial'
    assert result['geometry_scope']=='coarse_object_verification'
    # Existing in-hand observation without local space still cannot move.
    scene.latest=scene.latest.model_copy(update={'local_collision_evidence':None})
    result=scene.readiness()
    assert result['state']=='waiting' and result['code']=='required_camera_pending:head'
    assert 'geometry_status' not in result and 'move_tcp' not in result['available_skills']
