import hashlib
import json
import threading

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
