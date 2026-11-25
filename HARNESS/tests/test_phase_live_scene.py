from types import SimpleNamespace
import threading
from itertools import chain, count

import numpy as np
import pytest

from f1_harness.live_scene import LiveSceneObserver, SceneNotAdvanced
from f1_harness.scene_pipeline import SecondaryCameraUnavailable
from test_scene_pipeline import fixture


def observer(monkeypatch,*,change_context=False,slow=False):
    _,obs=fixture()
    scene=LiveSceneObserver.__new__(LiveSceneObserver)
    ctx=dict(owner_instance_id='owner',session_id='session',task_hash='task',task_generation=1,
             epoch=1,phase='observe',authorized=False,request_id=None)
    statuses=iter([ctx,{**ctx,'epoch':2} if change_context else ctx])
    stamps=[None]*7+[1]*7+[None,1]
    feedback=dict(host_boot_id=obs.host_boot_id,stamps16=stamps,full22=[0.]*22,measured16=[0.]*16)
    scene.ingested=[]
    def owner_call(method, **kwargs):
        if method=='status':return {'perception_context':next(statuses)}
        if method=='ingest_live_scene':scene.ingested.append(kwargs);return {}
        return feedback
    scene.owner=SimpleNamespace(call=owner_call)
    scene.raw=SimpleNamespace(observe=lambda:obs,store=SimpleNamespace(blob=lambda sha:b'frame'))
    scene.config=dict(feedback_age_ms=200,full22_reference=[0.]*22,reference_tolerance_deg=.1,
                      frame_state_skew_ms=50,max_age_ms=500,archive_budget_bytes=10000)
    scene.numerics=SimpleNamespace(call=lambda *args,**kwargs:np.eye(4))
    scene.frames=SimpleNamespace(native_eef=lambda value:value)
    # Production ScenePipeline always exposes its segmentation provider. The
    # readiness contract reads it even during a discarded phase-racing frame.
    scene.pipeline=SimpleNamespace(process=lambda value,**kwargs:value,segment=None)
    verified=[];scene.verifier=SimpleNamespace(call=lambda *args,**kwargs:verified.append(kwargs))
    scene.store=SimpleNamespace(put_blob=lambda value:None)
    scene.archived=set();scene.archive_bytes=0;scene.lock=threading.Lock();scene.latest=None;scene.error=None
    times=chain([1],count(600_000_001)) if slow else count(1)
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:next(times))
    return scene,verified


def test_unused_left_feedback_can_be_missing_without_fabrication(monkeypatch):
    scene,verified=observer(monkeypatch)
    scene._sample()
    assert verified and not any(scene.latest.state_valid_mask[:7])
    assert all(scene.latest.state_valid_mask[7:14])
    assert scene.ingested[0]['observation']['observation_id']==scene.latest.observation_id


def test_scene_requests_history_at_image_time_not_latest_feedback(monkeypatch):
    scene,_=observer(monkeypatch)
    call=scene.owner.call
    requested=[]
    def recording(method,**kwargs):
        if method=='measured_feedback':requested.append(kwargs)
        return call(method,**kwargs)
    scene.owner.call=recording
    scene._sample()
    assert requested==[{'target_received_ns':1}]


def test_real_wrist_pair_arriving_during_owner_call_keeps_its_capture_times(monkeypatch):
    scene, verified = observer(monkeypatch)
    raw = scene.raw.observe()
    # V47 99ce snapshot 42f8ed71 stores these image, acquisition and matched
    # joint timestamps. The RPC return time below is simulated.
    frame_times = {'rgb': 31009578242167, 'depth': 31009575276278}
    acquired_ns = 31009583289249
    state_ns = 31009584786712
    frames = tuple(f.model_copy(update={'camera': 'right', 'received_ns': frame_times[f.stream]})
                   for f in raw.frames)
    raw = raw.model_copy(update={'frames': frames, 'received_ns': acquired_ns,
                                 'pairings': (raw.pairings[0].model_copy(update={'camera': 'right'}),)})
    scene.raw.observe = lambda: raw
    clock = [acquired_ns]
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns', lambda: clock[0])
    call = scene.owner.call
    def receive(method, **kwargs):
        feedback = call(method, **kwargs)
        if method == 'measured_feedback':
            assert kwargs == {'target_received_ns': 31009576759222}
            clock[0] = state_ns + 1_000_000
            return dict(feedback, stamps16=[state_ns] * 16, matched_state_received_ns=state_ns, **kwargs)
        return feedback
    scene.owner.call = receive
    scene._sample()
    assert verified and scene.ingested
    assert scene.latest.state_received_ns == state_ns
    assert scene.latest.received_ns == acquired_ns
    assert {f.stream: f.received_ns for f in scene.latest.frames} == frame_times


@pytest.mark.parametrize(('current_lag_ms', 'reason'), [
    (3, 'camera_sample_feedback_aged:right'),
    (201, 'feedback_expired:current'),
    (-1, 'feedback_expired:current'),
])
def test_old_camera_pair_is_waitable_only_with_valid_current_joint_feedback(monkeypatch,current_lag_ms,reason):
    scene, verified, targets = two_camera_scene(monkeypatch)
    clock = 1_300_000_000
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns', lambda: clock)
    call = scene.owner.call
    queries = []
    def measured(method, **kwargs):
        if method != 'measured_feedback':
            return call(method, **kwargs)
        target = kwargs.get('target_received_ns')
        queries.append(target)
        if target is None:
            stamp = clock - current_lag_ms * 1_000_000
            return dict(host_boot_id=scene.raw.observe().host_boot_id, full22=[0.]*22,
                        stamps16=[stamp]*16, measured16=[0.]*16)
        return dict(call(method, **kwargs), target_received_ns=target, matched_state_received_ns=target)
    scene.owner.call = measured
    with pytest.raises(ValueError, match=reason):
        scene._sample()
    assert queries == [950_000_000, None] and targets == [950_000_000]
    assert not verified and not scene.ingested


def test_polling_same_frame_keeps_old_scene_age_and_does_not_reingest(monkeypatch):
    scene,verified=observer(monkeypatch)
    scene._sample()
    original=scene.latest
    scene.owner.call=lambda method,**kwargs:{'perception_context':{}}
    with pytest.raises(SceneNotAdvanced):scene._sample()
    assert scene.latest is original and len(verified)==1 and len(scene.ingested)==1
    assert scene.latest.received_ns==original.received_ns


def test_owner_transition_during_processing_does_not_reach_verifier(monkeypatch):
    scene,verified=observer(monkeypatch,change_context=True)
    with pytest.raises(ValueError,match='context_changed'):scene._sample()
    assert not verified and scene.latest is None


@pytest.mark.parametrize('change,preserve',[
    ({'phase':'verify_grasp','request_id':'verify-request'},True),
    ({'phase':'withdraw_reset','placement_verified':True},True),
    ({'authorized':False},False),
    ({'epoch':2},False),
    ({'task_generation':2},False),
])
def test_run_discards_racing_scene_but_preserves_history_only_for_normal_progress(monkeypatch,change,preserve):
    scene,verified=observer(monkeypatch)
    before=dict(owner_instance_id='owner',session_id='session',task_id='task',task_hash='hash',
                task_generation=1,epoch=1,authorized=True,phase='grasp',request_id='grasp-request')
    statuses=iter([before,{**before,**change}])
    _,obs=fixture()
    feedback=dict(host_boot_id=obs.host_boot_id,stamps16=[None]*7+[1]*7+[None,1],
                  full22=[0.]*22,measured16=[0.]*16)
    scene.owner=SimpleNamespace(call=lambda method,**kwargs:{'perception_context':next(statuses)} if method=='status' else feedback)
    measured_reference=np.eye(4)
    scene.pipeline.in_hand_reference=measured_reference
    scene.pipeline.hole_last_visible_ns=1
    def invalidate():
        scene.pipeline.in_hand_reference=None;scene.pipeline.hole_last_visible_ns=None
    scene.pipeline.invalidate=invalidate
    # _run handles the real exception path, clearing public latest in both cases.
    scene.stop=threading.Event();scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene.latest=obs
    scene._run()
    assert not verified and scene.latest is None
    if preserve:
        assert scene.pipeline.in_hand_reference is measured_reference
        assert scene.pipeline.hole_last_visible_ns==1
        assert scene.error=='owner_perception_stage_advanced'
    else:
        assert scene.pipeline.in_hand_reference is None and scene.pipeline.hole_last_visible_ns is None
        assert scene.error=='owner_perception_context_changed'


def test_pending_request_during_processing_keeps_same_measured_scene(monkeypatch):
    scene,verified=observer(monkeypatch)
    original=scene.owner.call
    status_calls=[]
    def owner_call(method,**kwargs):
        value=original(method,**kwargs)
        if method=='status':
            status_calls.append(True)
            context=dict(value['perception_context'],action_published=False)
            if len(status_calls)==2:
                context.update(request_id='new-request',requested_skill='move_tcp')
            return {'perception_context':context}
        return value
    scene.owner.call=owner_call
    scene._sample()
    assert verified and len(scene.ingested)==1
    assert scene.ingested[0]['context']['requested_skill']=='move_tcp'
    assert scene.latest.received_ns==1


def test_processing_time_does_not_refresh_old_observation(monkeypatch):
    scene,verified=observer(monkeypatch,slow=True)
    with pytest.raises(ValueError,match='latency_exceeds'):scene._sample()
    assert not verified and scene.latest is None


def test_slow_evidence_archive_does_not_publish_scene(monkeypatch):
    scene,verified=observer(monkeypatch)
    times=iter([1,2,600_000_001])
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:next(times))
    with pytest.raises(ValueError,match='archive_exceeds'):scene._sample()
    assert not verified and scene.latest is None


def test_snapshot_timestamp_does_not_rejuvenate_old_frames(monkeypatch):
    scene,_=observer(monkeypatch)
    _,obs=fixture()
    scene.latest=obs.model_copy(update={'received_ns':600_000_001})
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:600_000_002)
    with pytest.raises(ValueError,match='scene_expired'):scene.observe()


def two_camera_scene(monkeypatch,*,head_shift=0,wrist_delta=0):
    scene,verified=observer(monkeypatch)
    _,obs=fixture()
    frames=tuple(f.model_copy(update={'camera':camera,'received_ns':stamp,'source_ns':stamp})
                 for camera,stamp in [('head',800_000_000),('right',950_000_000)] for f in obs.frames)
    obs=obs.model_copy(update={'received_ns':1_000_000_000,'frames':frames})
    scene.raw.observe=lambda:obs
    scene.config['geometry']=dict(camera='right',hole_camera={'camera':'head'})
    call=scene.owner.call
    targets=[]
    def owner_call(method,**kwargs):
        if method!='measured_feedback':return call(method,**kwargs)
        target=kwargs['target_received_ns'];targets.append(target)
        actual=target+(wrist_delta if target==950_000_000 else 0)
        full22=[0.]*22
        if target==800_000_000:full22[14]=head_shift
        return dict(host_boot_id=obs.host_boot_id,stamps16=[None]*7+[actual]*7+[None,actual],
                    full22=full22,measured16=[0.]*16)
    scene.owner.call=owner_call
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:1_000_000_000)
    return scene,verified,targets


def test_fixed_head_and_wrist_use_their_own_real_feedback_history(monkeypatch):
    scene,verified,targets=two_camera_scene(monkeypatch)
    scene._sample()
    assert targets==[950_000_000,800_000_000]
    assert verified and scene.latest.state_received_ns==950_000_000
    assert scene.last_timing['cross_camera_span_ms']==150
    assert min(f.received_ns for f in scene.latest.frames)==800_000_000


def test_head_reference_is_checked_at_head_frame_time_not_wrist_time(monkeypatch):
    scene,verified,_=two_camera_scene(monkeypatch,head_shift=1)
    with pytest.raises(ValueError,match='camera_body_reference_changed:head'):scene._sample()
    assert not verified


def test_independent_head_does_not_relax_wrist_feedback_skew(monkeypatch):
    scene,verified,_=two_camera_scene(monkeypatch,wrist_delta=-51_000_000)
    with pytest.raises(ValueError,match='frame_feedback_skew'):scene._sample()
    assert not verified


@pytest.mark.parametrize(('current_fault','expected_reason'), [
    (None, 'camera_sample_feedback_unpaired:right'),
    ('stale', 'feedback_expired:current'),
    ('clock', 'feedback_clock_mismatch'),
])
def test_bad_wrist_pair_keeps_diagnostics_and_requires_live_control_feedback(
        monkeypatch, current_fault, expected_reason):
    scene, verified, _ = two_camera_scene(monkeypatch,wrist_delta=-51_000_000)
    original_call = scene.owner.call
    invalidations = []
    def owner_call(method, **kwargs):
        if method == 'invalidate_live_scene':
            invalidations.append(kwargs['reason'])
            return {}
        if method == 'measured_feedback':
            target = kwargs.get('target_received_ns')
            if target is not None:
                feedback = original_call(method, **kwargs)
                return dict(feedback, target_received_ns=target, matched_state_received_ns=min(feedback['stamps16'][7:14]))
            stamp = 799_000_000 if current_fault == 'stale' else 999_000_000
            return dict(host_boot_id='other-boot' if current_fault=='clock' else scene.raw.observe().host_boot_id,
                        full22=[0.]*22, measured16=[0.]*16, stamps16=[stamp]*16)
        return original_call(method, **kwargs)
    scene.owner.call=owner_call
    reference=object()
    scene.pipeline.in_hand_reference=reference
    scene.pipeline.invalidate=lambda:setattr(scene.pipeline,'in_hand_reference',None)
    scene.stop=threading.Event()
    scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run()
    assert invalidations==[expected_reason] and not verified and not scene.ingested
    assert (scene.pipeline.in_hand_reference is reference)==(current_fault is None)
    if not current_fault:
        assert scene.error.startswith('frame_feedback_skew:')
        assert '51.0' in scene.error


def test_new_head_only_does_not_clear_primary_temporal_history(monkeypatch):
    scene,verified,_=two_camera_scene(monkeypatch)
    scene._sample()
    original=scene.raw.observe()
    frames=tuple(f.model_copy(update={'sequence':2,'received_ns':f.received_ns+10,'source_ns':f.source_ns+10})
                 if f.camera=='head' else f for f in original.frames)
    scene.raw.observe=lambda:original.model_copy(update={'frames':frames})
    scene.owner.call=lambda method,**kwargs:{'perception_context':{}}
    with pytest.raises(SceneNotAdvanced):scene._sample()
    assert len(verified)==1


@pytest.mark.parametrize('reason,preserve',[
    ('perception_latency_exceeds_observation_budget',True),
    ('evidence_archive_exceeds_observation_budget',True),
    ('verification_transport_exceeds_observation_budget',True),
    ('capture_faults:duplicate_source_stamp:head',False),
    ('feedback_expired:right',False),
])
def test_processing_expiry_retains_original_history_but_unpublishes_scene(monkeypatch,reason,preserve):
    scene,_=observer(monkeypatch)
    original=object();scene.pipeline.in_hand_reference=original
    scene.pipeline.invalidate=lambda:setattr(scene.pipeline,'in_hand_reference',None)
    calls=[];scene.owner.call=lambda method,**kwargs:calls.append((method,kwargs))
    def sample():raise ValueError(reason)
    scene._sample=sample;scene.latest=object()
    scene.stop=threading.Event();scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run()
    assert scene.latest is None and scene.error==reason
    assert calls==[('invalidate_live_scene',{'reason':reason})]
    assert (scene.pipeline.in_hand_reference is original)==preserve


def test_head_timing_miss_still_processes_primary_but_never_publishes_full_scene(monkeypatch):
    scene,verified,_=two_camera_scene(monkeypatch)
    owner=scene.owner.call;processed=[]
    def call(method,**kwargs):
        value=owner(method,**kwargs)
        if method=='measured_feedback' and kwargs['target_received_ns']==800_000_000:
            value['stamps16']=[None]*7+[700_000_000]*7+[None,700_000_000]
        return value
    scene.owner.call=call
    def process(obs,**kwargs):
        processed.append(kwargs)
        assert obs.state_received_ns==950_000_000
        raise kwargs['secondary_error']
    scene.pipeline.process=process
    with pytest.raises(SecondaryCameraUnavailable,match='frame_feedback_skew'):scene._sample()
    assert processed and not verified and not scene.ingested


def test_scope_change_during_primary_only_update_clears_history(monkeypatch):
    scene,verified=observer(monkeypatch,change_context=True)
    reference=object();scene.pipeline.in_hand_reference=reference
    scene.pipeline.hole_last_visible_ns=1
    def process(*args,**kwargs):raise SecondaryCameraUnavailable('capture_faults:duplicate_source_stamp:head')
    scene.pipeline.process=process
    scene.pipeline.invalidate=lambda:setattr(scene.pipeline,'in_hand_reference',None)
    scene.pipeline.invalidate_secondary=lambda:setattr(scene.pipeline,'hole_last_visible_ns',None)
    scene.stop=threading.Event();scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run()
    assert scene.error=='owner_perception_context_changed'
    assert scene.pipeline.in_hand_reference is None and not verified and not scene.ingested


def test_same_capture_new_epoch_cannot_keep_prior_primary_evidence(monkeypatch):
    scene,verified,_=two_camera_scene(monkeypatch)
    scene._sample();captured=scene.raw.observe()
    scene.last_processed_frames=tuple((f.camera,f.stream,f.sequence,f.source_ns) for f in captured.frames if f.camera=='right')
    scene.raw.observe=lambda:captured.model_copy(update={'faults':('duplicate_source_stamp:head',)})
    scene.pipeline.previous_context={'epoch':1,'phase':'observe'}
    scene.owner.call=lambda *args,**kwargs:{'perception_context':{'epoch':2,'phase':'observe'}}
    scene.pipeline.in_hand_reference=object()
    scene.pipeline.invalidate=lambda:setattr(scene.pipeline,'in_hand_reference',None)
    scene.stop=threading.Event();scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run()
    assert scene.error=='owner_perception_context_changed' and scene.pipeline.in_hand_reference is None
    assert len(verified)==1


def test_typed_secondary_miss_removes_full_scene_but_preserves_primary(monkeypatch):
    scene,verified=observer(monkeypatch)
    reference=object();scene.pipeline.in_hand_reference=reference;scene.pipeline.hole_last_visible_ns=1
    scene.pipeline.invalidate=lambda:setattr(scene.pipeline,'in_hand_reference',None)
    scene.pipeline.invalidate_secondary=lambda:setattr(scene.pipeline,'hole_last_visible_ns',None)
    def process(*args,**kwargs):raise SecondaryCameraUnavailable('capture_faults:duplicate_source_stamp:head')
    scene.pipeline.process=process
    scene.latest=object();scene.stop=threading.Event();scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run()
    assert scene.latest is None and scene.error=='capture_faults:duplicate_source_stamp:head'
    assert scene.pipeline.in_hand_reference is reference and scene.pipeline.hole_last_visible_ns is None
    assert not verified and not scene.ingested


@pytest.mark.parametrize('changed',['clock','reference'])
def test_head_identity_fault_cannot_hide_behind_duplicate_capture(monkeypatch,changed):
    scene,verified,_=two_camera_scene(monkeypatch)
    original=scene.raw.observe();owner=scene.owner.call
    scene.raw.observe=lambda:original.model_copy(update={'faults':('duplicate_source_stamp:head',)})
    processed=[];scene.pipeline.process=lambda *args,**kwargs:processed.append(kwargs)
    def call(method,**kwargs):
        value=owner(method,**kwargs)
        if method=='measured_feedback' and kwargs['target_received_ns']==800_000_000:
            if changed=='clock':value['host_boot_id']='other-boot'
            else:value['full22'][14]=10
        return value
    scene.owner.call=call
    with pytest.raises(ValueError) as error:scene._sample()
    assert not isinstance(error.value,SecondaryCameraUnavailable)
    assert not processed and not verified and not scene.ingested


@pytest.mark.parametrize(('history','future','reason'),[
    (True,False,'camera_sample_feedback_aged:right'),
    (False,False,'feedback_expired:right'),
    (True,True,'feedback_expired:right'),
])
def test_old_feedback_is_waitable_only_for_an_identified_image_time_sample(monkeypatch,history,future,reason):
    scene,_=observer(monkeypatch)
    raw=scene.raw.observe()
    now=300_000_001
    target=1
    stamps=[1]*16
    if future:stamps[8]=now+1
    feedback=dict(full22=scene.config['full22_reference'],stamps16=stamps,host_boot_id=raw.host_boot_id)
    if history:feedback.update(matched_state_received_ns=1,target_received_ns=target)
    with pytest.raises(ValueError,match=reason):
        scene._validate_feedback(raw,feedback,now,camera='right',age_ms=200,target_received_ns=target)
