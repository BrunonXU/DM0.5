"""Real local child processes; fake computation only, no robot connection."""
from pathlib import Path
import sys,time,threading
from copy import deepcopy
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from audit_workers import AuditPool,AuditCancelled,AuditWorkerFailed,pack_job,unpack_job
from f1_harness.skills import SkillGeometry


def job(index=0):
    scene=SkillGeometry(base_eef=np.eye(4),base_object=np.eye(4),base_hole=np.eye(4),eef_object=None,
        object_grasp_eef=np.eye(4),approach_clearance_m=.1,transfer_clearance_m=.1,insertion_depth_m=.01,
        attached_radius_m=.02,uncertainty_m=.001,effective_clearance_m=.01,obstacles=(),grasp_opening=100.,release_opening=0.)
    return [0.]*22,scene,[0.]*16,[([float(index)]*16,100+index,1.)]


def pool(tmp_path,mode='normal',workers=2,**extra):
    return AuditPool(dict(test_mode=mode,**extra),tmp_path/'workers',workers=workers,
        _worker_command=[sys.executable,str(Path(__file__).parent/'fixtures/audit_worker_fake.py')])


def assert_reaped(p,children):
    assert not p.children and not p.ready
    assert all(c['process'].poll() is not None for c in children)
    assert all(r['returncode'] is not None for r in p.last_cleanup)


def test_warm_reuse_and_ordered_complete_results(tmp_path,monkeypatch):
    monkeypatch.setenv('TEST_PRIVATE_CREDENTIAL','fake-canary')
    p=pool(tmp_path).start();children=list(p.children);pids={c['process'].pid for c in children}
    try:
        for size in (7,3):
            rows=p.map([job(i) for i in range(size)],context_binding='a'*64)
            assert [r['block_index'] for r in rows]==list(range(size))
            assert all(r['samples']==1 and r['failures']==[] and not r['private_credential_present'] for r in rows)
            assert {c['process'].pid for c in p.children}==pids
    finally:p.close()
    assert_reaped(p,children)


@pytest.mark.parametrize('mode',['crash','bad_binding','bad_asset','partial','native_error','partial_frame'])
def test_bad_or_partial_reply_reaps_every_worker(tmp_path,mode):
    p=pool(tmp_path,mode).start();children=list(p.children)
    with pytest.raises((AuditWorkerFailed,TimeoutError,ConnectionError,OSError)):
        p.map([job(0),job(1)],context_binding='a'*64,timeout_s=2.)
    assert_reaped(p,children)


def test_cancellation_interrupts_native_hang_even_if_term_ignored(tmp_path):
    p=pool(tmp_path,'ignore_term').start();children=list(p.children);cancel=threading.Event()
    timer=threading.Timer(.15,cancel.set);timer.start();start=time.monotonic()
    try:
        with pytest.raises(AuditCancelled):p.map([job()],context_binding='a'*64,cancelled=cancel.is_set)
    finally:timer.cancel()
    assert time.monotonic()-start<3.
    assert_reaped(p,children)
    assert any(r['returncode']==-9 for r in p.last_cleanup)


def test_deadline_and_context_change_cannot_return_partial_batch(tmp_path):
    p=pool(tmp_path,'hang').start();children=list(p.children)
    with pytest.raises(TimeoutError):p.map([job()],context_binding='a'*64,timeout_s=.15)
    assert_reaped(p,children)
    p=pool(tmp_path/'next','hang').start();children=list(p.children);current=threading.Event();current.set()
    timer=threading.Timer(.15,current.clear);timer.start()
    try:
        with pytest.raises(AuditCancelled):p.map([job()],context_binding='a'*64,current=current.is_set)
    finally:timer.cancel()
    assert_reaped(p,children)


def test_explicit_restart_uses_new_generation_and_new_process(tmp_path):
    p=pool(tmp_path,'hang_once',workers=1,marker=str(tmp_path/'entered'))
    p.start();old_generation=p.generation;old_children=list(p.children)
    with pytest.raises(TimeoutError):p.map([job()],context_binding='a'*64,timeout_s=.15)
    assert_reaped(p,old_children)
    with pytest.raises(AuditWorkerFailed,match='not_ready'):p.map([job()],context_binding='b'*64)
    p.start();new_children=list(p.children)
    try:
        assert p.generation!=old_generation
        assert p.map([job()],context_binding='b'*64)[0]['samples']==1
    finally:p.close()
    assert_reaped(p,new_children)


def test_duplicate_old_reply_is_rejected_on_next_batch(tmp_path):
    p=pool(tmp_path,'duplicate',workers=1).start();children=list(p.children)
    p.map([job()],context_binding='a'*64)
    with pytest.raises(AuditWorkerFailed,match='binding_mismatch'):
        p.map([job()],context_binding='a'*64)
    assert_reaped(p,children)


@pytest.mark.parametrize('mode',['bad_ready','init_hang'])
def test_failed_initialization_leaves_no_live_worker(tmp_path,mode):
    p=pool(tmp_path,mode)
    with pytest.raises((AuditWorkerFailed,TimeoutError)):
        p.start(timeout_s=1.)
    assert not p.children and not p.ready and p.last_cleanup
    assert all(r['returncode'] is not None for r in p.last_cleanup)


def test_json_geometry_roundtrip_preserves_numeric_values_and_rejects_unknown_class():
    import json
    original=job();encoded=pack_job(original);decoded=unpack_job(json.loads(json.dumps(encoded)))
    np.testing.assert_array_equal(decoded[1].base_eef,original[1].base_eef)
    assert decoded[0]==original[0] and decoded[2]==original[2]
    assert decoded[3]==[list(x) for x in original[3]]
    bad=deepcopy(encoded);bad['scene_type']='UntrustedFactory'
    with pytest.raises(ValueError,match='unsupported'):unpack_job(bad)


def test_duplicate_start_preserves_existing_warm_workers(tmp_path):
    p=pool(tmp_path).start();children=list(p.children);generation=p.generation
    try:
        with pytest.raises(AuditWorkerFailed,match='explicit_close'):p.start()
        assert p.ready and p.generation==generation
        assert [c['process'].pid for c in p.children]==[c['process'].pid for c in children]
        assert p.map([job()],context_binding='a'*64)[0]['samples']==1
    finally:p.close()


def test_busy_request_does_not_queue_or_kill_active_batch(tmp_path):
    p=pool(tmp_path,'hang_once',workers=1,marker=str(tmp_path/'entered')).start()
    cancel=threading.Event();errors=[]
    def run():
        try:p.map([job()],context_binding='a'*64,cancelled=cancel.is_set)
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    try:
        deadline=time.monotonic()+2
        while not (tmp_path/'entered').exists() and time.monotonic()<deadline:time.sleep(.01)
        assert (tmp_path/'entered').exists()
        with pytest.raises(AuditWorkerFailed,match='busy'):p.map([job()],context_binding='b'*64)
        assert p.ready and all(c['process'].poll() is None for c in p.children)
    finally:
        cancel.set();thread.join(timeout=3)
    assert not thread.is_alive() and len(errors)==1 and isinstance(errors[0],AuditCancelled)
    assert not p.ready and not p.children


def test_cancellation_during_initialization_reaps_without_ready(tmp_path):
    p=pool(tmp_path,'init_hang');cancel=threading.Event();timer=threading.Timer(.15,cancel.set);timer.start()
    try:
        with pytest.raises(AuditCancelled):p.start(cancelled=cancel.is_set)
    finally:timer.cancel()
    assert p.last_cleanup and all(r['returncode'] is not None for r in p.last_cleanup)
    assert not p.ready and not p.children


def test_prefix_rejection_requires_requested_mode_and_reuses_workers(tmp_path):
    p=pool(tmp_path,'rejected_prefix').start();long=job();long[3].extend(long[3]*2)
    try:
        for _ in range(2):
            value=p.map([long],context_binding='a'*64,stop_on_failure=True)[0]
            assert value['samples']==1 and value['stopped_on_first_failure']
        children=list(p.children)
        with pytest.raises(AuditWorkerFailed,match='incomplete'):
            p.map([long],context_binding='a'*64)
        assert_reaped(p,children)
    finally:p.close()


def test_warmup_from_short_lived_thread_does_not_create_workers(tmp_path):
    p=pool(tmp_path);errors=[]
    def start():
        try:p.start()
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=start);thread.start();thread.join(timeout=3)
    assert len(errors)==1 and isinstance(errors[0],AuditWorkerFailed)
    assert str(errors[0])=='audit_pool_start_requires_main_thread'
    assert not p.children and not p.ready
    # Rejected startup did not poison a subsequent main-thread warmup.
    p.start()
    try:assert p.map([job()],context_binding='a'*64)[0]['samples']==1
    finally:p.close()


@pytest.mark.parametrize('phase',['initialize','idle','busy','stopped'])
def test_parent_sigkill_terminates_and_reaps_workers(tmp_path,phase):
    import subprocess,json
    driver=Path(__file__).parent/'fixtures/audit_parent_crash.py'
    result=subprocess.run([sys.executable,str(driver),'reaper',phase,str(tmp_path)],
        capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    evidence=json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence['phase']==phase and evidence['parent_returncode']==-9
    assert len(evidence['reaped'])==2 and all(v['signal']==9 for v in evidence['reaped'])


def test_parent_identity_race_prevents_target_exec(tmp_path):
    import subprocess,os
    target=tmp_path/'should-not-run'
    launcher=Path(__file__).parents[1]/'scripts/audit_worker_launch.py'
    result=subprocess.run([sys.executable,str(launcher),str(os.getpid()+1),'--',sys.executable,
        '-c','from pathlib import Path; import sys; Path(sys.argv[1]).touch()',str(target)],
        capture_output=True,text=True,timeout=5)
    assert result.returncode!=0 and 'parent_changed_before_launch' in result.stderr
    assert not target.exists()
