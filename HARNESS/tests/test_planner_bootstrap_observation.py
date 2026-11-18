"""A missing scene may reach image inspection, never motion or replacement authority."""
from copy import deepcopy
from types import SimpleNamespace
import io

import pytest
from PIL import Image
from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot
from f1_harness.conditions import observation_condition
from f1_harness.contracts import HarnessError, Task, Frame, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.remote import ObservationWaitEnded
from f1_harness.task_runner import EvidenceRemoteBackend, _initial_planner_observation, run_task


MISSING='object_not_visible_in_either_calibrated_view'
SAVED_REASON='observation_readiness_timeout:observation_unavailable:RpcError:observe_with_status: '+MISSING


class Store:
    def __init__(self):self.events=[]
    def event(self,kind,**values):self.events.append(dict(kind=kind,**values))


class Backend:
    def __init__(self,error=None):
        self.task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
        self.instance='owner';self.session='session';self.pending=None
        self.calls=[];self.images=[];self.error=error;self.progress=[]
        self.prepared_authority=dict(task_hash=digest(self.task),authorized=False,epoch=0,task_generation=1)
        self.status=dict(owner_instance_id=self.instance,task_hash=digest(self.task),epoch=1,
            authorized=True,hold_requested=False,hardware_ready=True,
            perception_context=dict(owner_instance_id=self.instance,session_id=self.session,
                task_id=self.task.task_id,task_hash=digest(self.task),task_generation=1,epoch=1),
            observation_readiness=observation_condition(MISSING))

    def execute_tool(self,name,args,*,prompt_handoff=False):
        self.calls.append(name)
        assert name=='observe','Bootstrap must not submit an action'
        assert prompt_handoff,'Bootstrap must allow image prompting without polling unseeded masks'
        if self.error:raise self.error
        return dict(observation=dict(observation_id='real-id',calibration_id='cal',scene_version='scene',
            stationary=True,objects=[dict(object_id='block')]))

    def call(self,method,**kwargs):
        self.calls.append(method)
        if method=='status':return deepcopy(self.status)
        if method=='capabilities':return dict(mode='hardware',hardware_ready=True)
        if method=='execution_events':return dict(events=[],next=kwargs['after'])
        if method=='finish_task':return dict(outcome='pending_review',synthetic=True)
        raise AssertionError(method)

    def _progress(self,state,**detail):self.progress.append(dict(state=state,**detail))
    def heartbeat(self):pass
    def close_images(self):pass
    def cancel(self):pass
    def solved(self):return False
    def env_steps(self):return 0
    def artifact(self,name,step=-1):
        assert name in ('head.png','right.png') and step==-1
        self.images.append(name)
        image=Image.new('RGB',(32,32),'white');stream=io.BytesIO();image.save(stream,format='PNG')
        return stream.getvalue()


@pytest.mark.parametrize('reason',[
    SAVED_REASON,
    'observation_prompt_required:observation_unavailable:RpcError:observe_with_status: '
    'mask_view=right:mask_uncertain_requires_new_prompt:object=square-1',
])
def test_saved_b312_missing_geometry_becomes_diagnostic_not_fake_observation(reason):
    backend=Backend(ObservationWaitEnded(reason));store=Store()
    result=_initial_planner_observation(backend,deepcopy(backend.status),store)
    assert result['geometry_available'] is False and result['reason']==reason
    assert result['condition']['state']=='waiting'
    assert result['recovery']=='localize_current_entities_then_reacquire' and not result['motion_evidence']
    assert backend.calls==['observe','status']
    assert not {'objects','observation_id','received_ns','scene_version'} & result.keys()
    assert store.events[-1]['kind']=='planner_initial_observation_unavailable'


def test_valid_initial_scene_is_preserved_and_does_not_add_status_roundtrip():
    backend=Backend();store=Store()
    result=_initial_planner_observation(backend,backend.status,store)
    assert result['geometry_available'] and result['observation_id']=='real-id'
    assert result['objects']==[dict(object_id='block')]
    assert backend.calls==['observe']
    assert store.events[-1]['kind']=='planner_initial_observation'


def test_quality_warning_survives_full_bootstrap_without_downgrading_geometry():
    backend=Backend();execute=backend.execute_tool
    def observed(*args,**kwargs):
        value=execute(*args,**kwargs)
        value['observation']['quality_warnings']=['planar_support_fraction_below_reference']
        return value
    backend.execute_tool=observed
    result=_initial_planner_observation(backend,backend.status,Store())
    assert result['geometry_available'] is True
    assert result['quality_warnings']==['planar_support_fraction_below_reference']
    assert backend.calls==['observe']


def test_missing_scene_carries_prompt_catalog_without_claiming_detection():
    backend=Backend(ObservationWaitEnded(SAVED_REASON));store=Store()
    catalog={'head':[dict(object_id='hole',role='target_aperture')]}
    backend.status['observation_readiness']['search_entities']=catalog
    result=_initial_planner_observation(backend,deepcopy(backend.status),store)
    assert result['search_entities']==catalog and not result['geometry_available']
    assert 'objects' not in result and backend.calls==['observe','status']


@pytest.mark.parametrize('error',[
    HarnessError('observation_readiness_timeout:owner_status_unavailable'),
    TimeoutError('transport'),ConnectionError('transport'),
    ObservationWaitEnded('observation_needs_attention:calibration_changed'),
    ObservationWaitEnded('observation_readiness_timeout:observation_unavailable:RpcError:observe: camera_identity_changed'),
    ObservationWaitEnded('observation_readiness_timeout:FileNotFoundError'),
    ObservationWaitEnded('observation_readiness_timeout:owner_perception_context_changed'),
    ObservationWaitEnded('observation_readiness_timeout:observation_unavailable:RpcError:other_method: '+MISSING),
])
def test_transport_identity_authority_and_unknown_errors_remain_fatal(error):
    backend=Backend(error);store=Store()
    with pytest.raises(type(error)) as caught:_initial_planner_observation(backend,backend.status,store)
    assert caught.value is error and not store.events
    assert backend.calls==['observe']


@pytest.mark.parametrize('field,value',[
    ('authorized',False),('hold_requested',True),('hardware_ready',False),
    ('owner_instance_id','replaced'),('task_hash','replaced'),('epoch',2),('task_generation',2),
    ('session_id','replaced'),('context_epoch',2),('context_task_id','replaced'),
    ('context_task_generation',2),
])
def test_waiting_perception_does_not_cross_changed_owner_scope(field,value):
    backend=Backend(ObservationWaitEnded(SAVED_REASON));authorized=deepcopy(backend.status)
    if field=='session_id':backend.status['perception_context']['session_id']=value
    elif field.startswith('context_'):backend.status['perception_context'][field.removeprefix('context_')]=value
    else:backend.status[field]=value
    with pytest.raises(HarnessError,match='planner_bootstrap_authority_changed'):
        _initial_planner_observation(backend,authorized,Store())


def test_new_identity_fault_after_missing_sample_remains_fatal():
    backend=Backend(ObservationWaitEnded(SAVED_REASON));authorized=deepcopy(backend.status)
    backend.status['observation_readiness']=observation_condition('camera_identity_changed')
    with pytest.raises(HarnessError,match='observation_identity_changed'):
        _initial_planner_observation(backend,authorized,Store())


@pytest.mark.parametrize('stop_before_status',[True,False])
def test_stop_during_bootstrap_prevents_recovery_turn(stop_before_status):
    backend=Backend(ObservationWaitEnded(SAVED_REASON));checks=[]
    def cancelled():
        checks.append(1);return stop_before_status or len(checks)>1
    with pytest.raises(HarnessError,match='planner_bootstrap_authority_ended'):
        _initial_planner_observation(backend,backend.status,Store(),cancelled=cancelled)


def test_actual_task_runner_reaches_model_and_pyrua_image_with_no_geometry(tmp_path,monkeypatch):
    backend=Backend(ObservationWaitEnded(SAVED_REASON))
    monkeypatch.setattr('f1_harness.task_runner.EvidenceRemoteBackend',lambda *a:backend)
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path/'cells',images='on-demand')
    arm.start=lambda:arm;arm.close=lambda:None
    monkeypatch.setattr('f1_harness.task_runner.SandboxedArm',lambda *a,**kw:arm)
    seen=[]
    class OfflinePlanner:
        def __init__(self,*a):pass
        def prepare_connection(self):pass
        def close(self):pass
        def run(self,arm,*,task_text,snapshot_text,continue_run,complete_run=None):
            assert continue_run()
            assert 'Initial geometry is unavailable' in task_text
            assert SAVED_REASON in task_text
            image=arm.execute_tool('python',dict(code='robo.show("right")'))
            assert image.images and not image.result.get('exception')
            seen.append(task_text)
            return dict(outcome='pending_review',reason='synthetic image inspection only')
    monkeypatch.setattr('f1_harness.planner.ResponsesPlanner',OfflinePlanner)
    path=tmp_path/'task.json';path.write_text(backend.task.model_dump_json())
    args=SimpleNamespace(task=path,gateway='unused',image_gateway='unused',authorization_source='web',
        start_timeout=1,planner='astra',experience_snapshot=None)
    result=run_task(args,EvidenceStore(tmp_path/'evidence'),None)
    assert backend.progress[-1]['state']=='completed'
    assert backend.progress[-1]['outcome']=='pending_review'
    assert seen and backend.images==['right.png']
    assert result['planner_declaration']['outcome']=='pending_review'
    assert 'execute_skill' not in backend.calls


@pytest.mark.parametrize('camera,roi,size',[
    ('head',[307,28,1229,720],[922,692]),('right',None,[960,540])])
def test_image_metadata_binds_actual_raw_frame_crop_and_resize_without_geometry(tmp_path,camera,roi,size):
    store=EvidenceStore(tmp_path);blob=store.put_blob(bytes(1280*720*3))
    frame=Frame(camera=camera,stream='rgb',frame_id=camera,sequence=3,source_ns=123,received_ns=456,
        clock_domain='camera',width=1280,height=720,encoding='rgb8',blob_sha256=blob)
    backend=EvidenceRemoteBackend.__new__(EvidenceRemoteBackend)
    backend.evidence_store=store;backend.archive_blob=lambda sha:None;calls=[]
    def call(method):
        calls.append(method);assert method=='camera_observation'
        return dict(observation_id='raw-frame-only',frames=[frame.model_dump()])
    backend.call=call
    data=backend.artifact(camera+'.png')
    meta=backend.last_viewed_images[camera]
    assert list(Image.open(io.BytesIO(data)).size)==size
    assert meta['frame']==frame.model_dump() and meta['observation_id']=='raw-frame-only'
    assert meta['roi']==roi and meta['model_image_size']==size and meta['source_image_size']==[1280,720]
    assert calls==['camera_observation'] and not {'objects','scene_version'} & meta.keys()
