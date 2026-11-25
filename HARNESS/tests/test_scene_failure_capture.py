import hashlib
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
import numpy as np

from f1_harness.evidence import EvidenceStore, FrameCache
from f1_harness.scene_failures import SceneFailureRecorder


def inputs(cache):
    rgb=cache.put_blob(b'actual saved rgb');depth=cache.put_blob(b'actual saved depth')
    info=cache.put_blob(b'intrinsics')
    return dict(context=dict(owner_instance_id='owner',session_id='session',task_hash='hash',
        task_id='web-'+'a'*32,epoch=4,phase='approach_grasp',authorized=True),
        observation=dict(received_ns=100,frames=[dict(camera='right',stream='rgb',source_ns=90,
            received_ns=95,blob_sha256=rgb,camera_info_sha256=info),
            dict(camera='right',stream='depth',source_ns=91,received_ns=96,blob_sha256=depth,camera_info_sha256=info)]),
        feedback=dict(stamps16=[97]*16),search_before={'prompts':{'right':{'block':[1,2,3,4]}}})


def saved(store):
    return [json.loads(p.read_text()) for p in (store.root/'scene-failures').glob('*.json')]


def test_snapshot_pins_bytes_and_original_timestamps_even_after_cache_eviction(tmp_path):
    cache=FrameCache();snap=inputs(cache);store=EvidenceStore(tmp_path)
    writer=SceneFailureRecorder(store)
    assert writer.offer('target_missing',snap,cache.blob)
    cache.data.clear();snap['observation']['received_ns']=999
    snap['search_before']['prompts']['right']['block'][0]=99
    assert not writer.close()['writer_alive']
    record=saved(store)[0]
    assert record['complete'] and record['observation']['received_ns']==100
    assert record['search_before']['prompts']['right']['block']==[1,2,3,4]
    assert all(hashlib.sha256(writer.archive.blob(sha)).hexdigest()==sha for sha in record['persisted_blobs'])
    assert not list((store.root/'blobs').iterdir())


def test_disk_failure_is_diagnostic_and_never_raises_to_caller(tmp_path):
    cache=FrameCache();snap=inputs(cache);store=EvidenceStore(tmp_path)
    writer=SceneFailureRecorder(store)
    writer.archive.put_blob=lambda data:(_ for _ in ()).throw(OSError('disk unavailable'))
    assert writer.offer('target_missing',snap,cache.blob)
    writer.close()
    record=saved(store)[0]
    assert not record['complete'] and len(record['failed_writes'])==3
    assert record['persisted_blobs']==[]


def test_cache_miss_keeps_metadata_without_fabricating_blobs(tmp_path):
    cache=FrameCache();snap=inputs(cache);store=EvidenceStore(tmp_path)
    cache.data.clear()
    writer=SceneFailureRecorder(store)
    assert writer.offer('target_missing',snap,cache.blob)
    writer.close()
    record=saved(store)[0]
    assert not record['complete'] and len(record['missing_at_capture'])==3
    assert not record['persisted_blobs']


def test_blocked_writer_has_one_queued_snapshot_and_offer_does_not_wait(tmp_path):
    cache=FrameCache();snap=inputs(cache);store=EvidenceStore(tmp_path)
    writer=SceneFailureRecorder(store)
    entered=threading.Event();release=threading.Event();original=writer.archive.put_blob
    def blocked(data):
        entered.set();assert release.wait(3)
        return original(data)
    writer.archive.put_blob=blocked
    try:
        assert writer.offer('first',snap,cache.blob)
        assert entered.wait(1)
        # The same content can still be archived by the normal scene lane.
        sha=store.put_blob(b'actual saved rgb')
        assert store.blob(sha)==b'actual saved rgb'
        assert writer.offer('second',snap,cache.blob)
        # A third diagnostic is dropped, not blocked behind disk IO.
        result=[];done=threading.Event()
        def attempt():result.append(writer.offer('third',snap,cache.blob));done.set()
        thread=threading.Thread(target=attempt);thread.start()
        assert done.wait(.5) and result==[False]
        thread.join()
    finally:release.set();writer.close()
    assert len(saved(store))==2 and writer.status()['skipped_busy']==1


def test_budget_repeat_limit_and_unbound_scope_are_advisory(tmp_path):
    cache=FrameCache();snap=inputs(cache);store=EvidenceStore(tmp_path)
    writer=SceneFailureRecorder(store,max_samples=1,per_reason=1)
    assert not writer.offer('none',{'context':{}},cache.blob)
    assert writer.offer('same',snap,cache.blob)
    assert not writer.offer('same',snap,cache.blob)
    writer.close();assert len(saved(store))==1
    small=SceneFailureRecorder(EvidenceStore(tmp_path/'small'),max_bytes=10)
    assert not small.offer('budget',snap,cache.blob)
    assert small.close()['skipped_budget']==1


def test_preprocessing_state_is_copied_not_returned_as_live_prompts():
    from test_mask_tracking_transaction import provider
    masks=provider();snapshot=masks.tracking_snapshot()
    snapshot['default']['prompts']['hole'][0]=100
    assert masks.states['default']['prompts']['hole'][0]==40


def test_observer_failure_enqueues_exact_attempt_without_making_a_scene_valid(tmp_path,monkeypatch):
    from test_phase_live_scene import observer
    scene,verified=observer(monkeypatch)
    scene.failure_recorder=SceneFailureRecorder(EvidenceStore(tmp_path))
    call=scene.owner.call
    def owner(method,**kwargs):
        value=call(method,**kwargs)
        if method=='status':value['perception_context'].update(task_id='web-'+'a'*32,authorized=True)
        return value
    scene.owner.call=owner
    scene.pipeline.process=lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('mask_missing'))
    scene.pipeline.invalidate=lambda:None
    scene.stop=threading.Event();scene.store.event=lambda *args,**kwargs:scene.stop.set()
    scene._run();scene.failure_recorder.close()
    rows=saved(scene.failure_recorder.store)
    assert len(rows)==1 and rows[0]['reason']=='mask_missing'
    assert rows[0]['feedback']['stamps16'][7]==1
    assert rows[0]['observation']['received_ns']==1
    assert scene.latest is None and not verified


def test_diagnostic_search_snapshot_error_does_not_reject_good_scene(monkeypatch):
    from types import SimpleNamespace
    from test_phase_live_scene import observer
    scene,verified=observer(monkeypatch)
    scene.pipeline.segment=SimpleNamespace(tracking_snapshot=lambda:(_ for _ in ()).throw(RuntimeError('diagnostic error')))
    scene._sample()
    assert verified and scene.latest is not None
    assert scene.failure_input['search_snapshot_error']=='RuntimeError'


def test_unstarted_task_does_not_consume_failure_capture_budget(tmp_path):
    cache=FrameCache();snap=inputs(cache);snap['context']['authorized']=False
    writer=SceneFailureRecorder(EvidenceStore(tmp_path))
    assert not writer.offer('warmup_fault',snap,cache.blob)
    assert writer.status().get('accepted',0)==0
    snap['context']['authorized']=True
    assert writer.offer('real_task_fault',snap,cache.blob)
    writer.close()


def test_diagnostic_directory_failure_does_not_prevent_observer_start(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from f1_harness.live_scene import LiveSceneObserver
    monkeypatch.setattr('f1_harness.frames.FrameChain.load',lambda cfg:SimpleNamespace(native_frame='world'))
    monkeypatch.setattr('f1_harness.live_scene.CalibratedScenePipeline',lambda *args:SimpleNamespace())
    monkeypatch.setattr('f1_harness.scene_failures.SceneFailureRecorder',
        lambda *args:(_ for _ in ()).throw(OSError('diagnostic directory unavailable')))
    monkeypatch.setattr('f1_harness.live_scene.threading.Thread.start',lambda self:None)
    raw=SimpleNamespace(store=SimpleNamespace(blob=lambda sha:b''))
    config=dict(geometry={'native_frame':'world'},owner_socket=tmp_path/'owner',
                numerical_socket=tmp_path/'numeric',verifier_socket=tmp_path/'verify')
    scene=LiveSceneObserver(raw,config,None,EvidenceStore(tmp_path))
    assert scene.failure_recorder is None and scene.failure_recorder_error=='OSError'
    assert scene.error=='scene_not_ready'  # Normal observation lifecycle still starts.


def test_early_failures_reserve_existing_budget_for_transport(tmp_path):
    cache=FrameCache();snap=inputs(cache);store=EvidenceStore(tmp_path)
    writer=SceneFailureRecorder(store)
    try:
        for i in range(4):
            assert writer.offer('early-'+str(i),snap,cache.blob)
            writer.queue.join()
        assert not writer.offer('one-more-early',snap,cache.blob)
        snap['context']['phase']='transfer_above_hole'
        assert writer.offer('early-0',snap,cache.blob)
        writer.queue.join()
        assert writer.status()['accepted']==5
    finally:writer.close()
    assert any(r['context']['phase']=='transfer_above_hole' for r in saved(store))


def test_post_transport_partial_keeps_one_synchronized_diagnostic(tmp_path,monkeypatch):
    from f1_harness.contracts import SceneObject
    from test_phase_live_scene import observer
    live,verified=observer(monkeypatch)
    cache=FrameCache();original=live.raw.observe();frames=[]
    for camera in ('head','right','left'):
        for f in original.frames:
            if camera=='left' and f.stream=='depth':continue
            frames.append(f.model_copy(update=dict(camera=camera,frame_id=camera+'_wrist_color_optical_frame',
                blob_sha256=cache.put_blob((camera+f.stream).encode()),camera_info_sha256=cache.put_blob(f.stream.encode()))))
    pairs=tuple(original.pairings[0].model_copy(update={'camera':c}) for c in ('head','right'))
    raw=original.model_copy(update=dict(host_boot_id='boot',frames=tuple(frames),pairings=pairs))
    live.raw.observe=lambda:raw
    live.raw.store=cache
    view=dict(rgb_info_sha256=cache.put_blob(b'rgb'),depth_info_sha256=cache.put_blob(b'depth'))
    live.config['geometry']=dict(view,camera='right',hole_camera=dict(view,camera='head'),object_id='block',hole_id='hole',calibration_id='test-cal',
        cross_view_aperture=dict(max_pair_skew_ns=50_000_000))
    live.head_model=SimpleNamespace(pose=lambda full:np.eye(4))
    original_call=live.owner.call
    def call(method,**kwargs):
        value=original_call(method,**kwargs)
        if method=='status':
            value['perception_context'].update(authorized=True,phase='inspect_in_hand',task_id='field')
            value['completed_skills']=['transfer_above_hole']
        if method=='measured_feedback':value['host_boot_id']='boot'
        return value
    live.owner.call=call
    def process(obs,**kw):
        wrist=tuple(f for f in obs.frames if f.camera=='right')
        target=SceneObject(object_id='block',shape='square',held=True,pose=tuple(np.eye(4).ravel()),
            eef_object_pose=tuple(np.eye(4).ravel()),evidence_ids=tuple(f.blob_sha256 for f in wrist))
        return obs.model_copy(update=dict(objects=(target,),frames=wrist,pairings=(pairs[1],),
            calibration_id='test-cal',geometry_scope='coarse_object_verification',quality_warnings=('fixture_unavailable:aperture_not_detected',)))
    live.pipeline.process=process
    writer=live.failure_recorder=SceneFailureRecorder(EvidenceStore(tmp_path))
    try:
        early=inputs(cache)
        for i in range(7):
            if i==4:early['context']['phase']='inspect_in_hand'
            assert writer.offer('ordinary'+str(i),early,cache.blob)
            writer.queue.join()
        assert not writer.offer('other',early,cache.blob)
        live._sample()
        assert len(live.ingested)==len(verified)==1 and {f.camera for f in live.latest.frames}=={'right'}
        writer.queue.join()
        assert not writer.offer_held_aperture(live.failure_input,live.latest,cache.blob,now_ns=4,max_age_ns=500_000_000)
    finally:writer.close()
    records=saved(writer.store);assert len(records)==8
    matched=[r for r in records if 'held_aperture' in r]
    assert len(matched)==1
    row=matched[0]
    assert row['complete'] and row['diagnostic_only'] and row['reason']=='fixture_unavailable:aperture_not_detected'
    assert {f['camera'] for f in row['held_aperture']['owner_frames']}=={'right'}
    assert row['held_aperture']['target']['held'] is True and len(row['observation']['frames'])==5
    assert row['base_eef']==row['head_camera_pose']==np.eye(4).tolist() and row['held_aperture']['matched_window_ns']==0
    assert all(writer.archive.blob(f['blob_sha256'])==cache.blob(f['blob_sha256']) for f in row['observation']['frames'])

    other=SceneFailureRecorder(EvidenceStore(tmp_path/'controls'))
    try:
        def offer(snapshot=None,scene=None,lookup=None,now=4):
            return other.offer_held_aperture(snapshot or live.failure_input,scene or live.latest,
                lookup or cache.blob,now_ns=now,max_age_ns=500_000_000)
        for fault in ('missing_left','wrong_skew','wrong_image_id','missing_fk','feedback_epoch','calibration','missing_camera_time'):
            sample=deepcopy(live.failure_input)
            if fault=='missing_left':sample['observation']['frames']=sample['observation']['frames'][:-1]
            if fault=='wrong_skew':sample['observation']['frames'][-1]['received_ns']+=51_000_000
            if fault=='wrong_image_id':sample['observation']['observation_id']='another'
            if fault=='missing_fk':sample['base_eef']=None
            if fault=='feedback_epoch':sample['camera_feedback']['head']['host_boot_id']='old'
            if fault=='calibration':sample['observation']['frames'][0]['camera_info_sha256']='9'*64
            if fault=='missing_camera_time':sample['observation']['frames'][0]['source_ns']=None
            assert not offer(sample),fault
        assert not offer(now=600_000_000)
        obj=live.latest.objects[0].model_copy(update=dict(held=None))
        assert not offer(scene=live.latest.model_copy(update={'objects':(obj,)}))
        assert not offer(lookup=lambda sha:None)
        assert other.status().get('accepted',0)==0
        assert offer(lookup=lambda sha:b'wrong contents')
        other.queue.join()
        assert other.status()['held_aperture_write_failures']==1 and other.status()['reserved_bytes']==0
        assert not saved(other.store)
        assert offer()
    finally:other.close()
