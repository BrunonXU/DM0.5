"""Local fixture images and RPC test phase switching; never physical placement."""
from types import SimpleNamespace
import threading
import numpy as np
import pytest
from f1_harness.live_scene import LiveSceneObserver, SceneNotAdvanced
from f1_harness.ipc import UnixClient, UnixServer
from test_full_development_scene import sample
from test_return_observation import prepared


def live(monkeypatch):
    pipeline,original=prepared(monkeypatch)
    state=dict(now=1_000_000_001,stamp=750_000_001,context=dict(owner_instance_id='owner',
        session_id='session',task_id='task',task_hash='task',task_generation=1,epoch=1,
        authorized=True,placement_verified=True,phase='withdraw_reset',request_id=None),
        faults=('duplicate_source_stamp:right',),feedback_lag=0,gripper=0.,ingested=[],verified=[],queries=[])
    scene=LiveSceneObserver.__new__(LiveSceneObserver)
    scene.config=dict(geometry=pipeline.config,feedback_age_ms=200,full22_reference=[0.]*22,
        reference_tolerance_deg=.1,frame_state_skew_ms=50,max_age_ms=500,archive_budget_bytes=100000)
    def observe():
        value=sample(original,(state['stamp']-1)//100_000_000)
        frames=tuple(f.model_copy(update=dict(received_ns=state['stamp'],source_ns=state['stamp'])) for f in value.frames)
        return value.model_copy(update=dict(received_ns=state['stamp'],frames=frames,faults=state['faults']))
    def dispatch(method,kwargs):
        if method=='status':return dict(perception_context=state['context'].copy())
        if method=='ingest_live_scene':state['ingested'].append(kwargs);return {}
        if method=='invalidate_live_scene':return {}
        assert method=='measured_feedback'
        target=kwargs.get('target_received_ns');state['queries'].append(target)
        stamp=target if target is not None else state['now']-state['feedback_lag']
        q=[0.]*15+[state['gripper']]
        return dict(host_boot_id=original.host_boot_id,stamps16=[None]*7+[stamp]*7+[None,stamp],
                    full22=[0.]*22,measured16=q)
    scene.owner=SimpleNamespace(call=lambda method,**kwargs:dispatch(method,kwargs))
    scene.raw=SimpleNamespace(observe=observe,store=SimpleNamespace(blob=lambda sha:b'test-archive'))
    scene.numerics=SimpleNamespace(call=lambda *a,**k:np.eye(4))
    scene.frames=SimpleNamespace(native_eef=lambda p:p)
    scene.pipeline=pipeline
    scene.verifier=SimpleNamespace(call=lambda method,**kwargs:state['verified'].append(kwargs))
    scene.store=SimpleNamespace(put_blob=lambda b:None,event=lambda *a,**k:None)
    scene.archived=set();scene.archive_bytes=0;scene.lock=threading.Lock();scene.latest=None;scene.error=None
    monkeypatch.setattr('f1_harness.live_scene.time.monotonic_ns',lambda:state['now'])
    return scene,state,dispatch


def test_fixed_head_history_and_current_arm_feedback_keep_their_own_timestamps(monkeypatch):
    scene,state,_=live(monkeypatch)
    try:
        for i in range(3):
            state['now']+=100_000_000;state['stamp']+=100_000_000
            scene._sample()
        result=scene.latest
        assert result.stationary and result.geometry_scope=='coarse_fixture_return'
        assert state['queries'][-2:]==[state['stamp'],None]
        assert result.state_received_ns==state['now']
        assert all(f.received_ns==state['stamp'] for f in result.frames)
        assert result.received_ns==state['stamp']
        assert all(result.state_valid_mask[7:14]) and result.state_valid_mask[15]
    finally:scene.pipeline.close()


def test_duplicate_poll_with_unused_wrist_fault_does_not_erase_return_stationary_history(monkeypatch):
    scene,state,_=live(monkeypatch)
    try:
        for i in range(3):
            state['now']+=100_000_000;state['stamp']+=100_000_000
            scene._sample()
            original=scene.latest
            # A faster polling loop repeatedly sees the same head image while
            # the unused wrist has a sampling fault. This must not reprocess it.
            for _ in range(4):
                state['now']+=1_000_000
                with pytest.raises(SceneNotAdvanced):scene._sample()
                assert scene.latest is original
                assert len(scene.pipeline.return_history)==i+1
        assert scene.latest.stationary and len(state['ingested'])==3
    finally:scene.pipeline.close()


def test_primary_camera_switch_does_not_compare_unrelated_source_stamps(monkeypatch):
    scene,state,_=live(monkeypatch)
    scene.last_processed_frames=tuple(('right',f.stream,f.sequence,state['stamp']) for f in scene.raw.observe().frames if f.camera=='right')
    try:
        scene._sample()
        assert state['ingested'] and scene.latest.geometry_camera=='head'
    finally:scene.pipeline.close()


@pytest.mark.parametrize('fault',['stale_current_feedback','closed_gripper','wrong_head_identity'])
def test_return_does_not_borrow_old_arm_or_bad_camera_state(monkeypatch,fault):
    scene,state,_=live(monkeypatch)
    if fault=='stale_current_feedback':state['feedback_lag']=201_000_000
    if fault=='closed_gripper':state['gripper']=100.
    if fault=='wrong_head_identity':
        observe=scene.raw.observe
        scene.raw.observe=lambda:observe().model_copy(update=dict(frames=tuple(
            f.model_copy(update={'camera_info_sha256':'f'*64}) if f.camera=='head' else f
            for f in observe().frames)))
    try:
        with pytest.raises(ValueError):scene._sample()
        assert not state['ingested'] and not state['verified']
    finally:scene.pipeline.close()


def test_head_only_return_contract_crosses_existing_unix_rpc(monkeypatch,tmp_path):
    scene,state,dispatch=live(monkeypatch)
    server=UnixServer(tmp_path/'owner.sock',dispatch).start()
    scene.owner=UnixClient(tmp_path/'owner.sock',timeout=1)
    try:
        for _ in range(3):
            state['now']+=100_000_000;state['stamp']+=100_000_000
            scene._sample()
        assert scene.latest.stationary
        assert len(state['verified'])==len(state['ingested'])==3
        assert all(row['context']['placement_verified'] for row in state['ingested'])
        assert all(row['observation']['geometry_scope']=='coarse_fixture_return' for row in state['ingested'])
    finally:server.close();scene.pipeline.close()
