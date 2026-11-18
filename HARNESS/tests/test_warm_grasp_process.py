"""Cross-run worker reuse must not reuse candidates or late results."""
import sys,threading
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import warm_grasp_process as warm
from f1_harness.contracts import Task,digest,HarnessError


@pytest.fixture
def setup(monkeypatch,tmp_path):
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    class Prepared:
        def __init__(self,service,*_):self.prepared=None;self.service=service
        def dispatch(self,method,kwargs):
            if method=='prepare_grasp':self.prepared=kwargs['key'];return dict(prepared=True)
            value,self.prepared=self.prepared,None
            if value!=kwargs['key']:raise HarnessError('prepared_candidate_missing')
            return dict(proposal={'key':value},receipt={})
    monkeypatch.setattr(warm,'PreparedGraspService',Prepared)
    service=warm.LeasedPreparedService(SimpleNamespace(task=task,fixture={},clock=lambda:0,numerics=None),None)
    entered=threading.Event();release=threading.Event();delay=[False];killed=[]
    def init(self,**_):self.cleanup=None;self.startup_s=.1
    def dispatch(self,method,kwargs):
        if delay[0] and method=='lease_call':entered.set();assert release.wait(2)
        if self.cleanup is not None:raise ConnectionError('closed')
        return service.dispatch(method,kwargs)
    def close(self):self.cleanup={'reaped':True};killed.append(True);release.set()
    monkeypatch.setattr(warm.PlanningProcess,'__init__',init)
    monkeypatch.setattr(warm.PlanningProcess,'dispatch',dispatch)
    monkeypatch.setattr(warm.PlanningProcess,'close',close)
    source=tmp_path/'frozen.py';source.write_text('source')
    process=warm.WarmGraspProcess(identity_files=[source])
    scope=dict(task_id=task.task_id,task_hash=digest(task),calibration_id=task.calibration_id,
               epoch=1,owner_instance_id='owner1',session_id='run1')
    return SimpleNamespace(process=process,scope=scope,service=service,entered=entered,release=release,
                           delay=delay,killed=killed,source=source)


def test_idle_close_reuses_process_but_not_prepared_candidate(setup):
    p=setup.process;lease=p.acquire(setup.scope)
    lease.dispatch('prepare_grasp',dict(parent_intent=setup.scope,key='draft'))
    lease.close()
    assert p.cleanup is None and setup.service.worker.prepared is None
    new_scope=dict(setup.scope,epoch=2,session_id='run2',owner_instance_id='owner2')
    next_lease=p.acquire(new_scope)
    with pytest.raises(HarnessError,match='candidate_missing'):
        next_lease.dispatch('adopt_grasp',dict(intent=new_scope,key='draft'))
    with pytest.raises(RuntimeError,match='lease_closed'):
        lease.dispatch('prepare_grasp',dict(parent_intent=setup.scope,key='old'))
    next_lease.dispatch('prepare_grasp',dict(parent_intent=new_scope,key='new'))
    assert next_lease.dispatch('adopt_grasp',dict(intent=new_scope,key='new'))['proposal']=={'key':'new'}
    next_lease.close();p.close();assert p.cleanup['reaped']


def test_cannot_begin_another_run_while_current_lease_active(setup):
    setup.process.acquire(setup.scope)
    with pytest.raises(RuntimeError,match='run_active'):setup.process.acquire(setup.scope)
    setup.process.close()


def test_worker_rejects_previous_scope_even_with_new_lease_token(setup):
    old=setup.process.acquire(setup.scope);old.close()
    fresh=setup.process.acquire(dict(setup.scope,session_id='new'))
    with pytest.raises(HarnessError,match='scope_mismatch'):
        fresh.dispatch('prepare_grasp',dict(parent_intent=setup.scope,key='old'))
    assert setup.service.worker.prepared is None
    fresh.close();setup.process.close()


def test_cancel_inflight_kills_worker_and_never_returns_late_result(setup):
    lease=setup.process.acquire(setup.scope);setup.delay[0]=True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(lease.dispatch,'prepare_grasp',dict(parent_intent=setup.scope,key='draft'))
        assert setup.entered.wait(1)
        lease.close()
        with pytest.raises((ConnectionError,RuntimeError)):future.result(timeout=1)
    assert setup.process.cleanup['reaped']
    with pytest.raises(RuntimeError,match='process_closed'):setup.process.acquire(setup.scope)


@pytest.mark.parametrize('when',['idle','busy'])
def test_identity_change_discards_worker(setup,when):
    if when=='idle':
        setup.source.write_text('changed')
        with pytest.raises(RuntimeError,match='source_changed'):setup.process.acquire(setup.scope)
    else:
        lease=setup.process.acquire(setup.scope);setup.delay[0]=True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(lease.dispatch,'prepare_grasp',dict(parent_intent=setup.scope,key='draft'))
            assert setup.entered.wait(1);setup.source.write_text('changed');setup.release.set()
            with pytest.raises(RuntimeError,match='source_changed'):future.result(timeout=1)
    assert setup.process.cleanup['reaped']


def test_fixed_task_cannot_be_silently_reconfigured(setup):
    with pytest.raises(HarnessError,match='fixed_task_mismatch'):
        setup.process.acquire(dict(setup.scope,task_hash='different'))
    assert setup.process.cleanup['reaped']


def test_old_end_message_cannot_close_new_run(setup):
    old=setup.process.acquire(setup.scope);old.close()
    lease=setup.process.acquire(dict(setup.scope,epoch=2))
    with pytest.raises(HarnessError,match='lease_expired'):
        setup.service.dispatch('end_run',dict(serial=old.serial,token=old.token))
    assert setup.service.active['token']==lease.token
    lease.close();setup.process.close()


def test_clock_mapping_cannot_change_during_active_lease(setup):
    first=lambda:{'origin_ns':10,'start_wall_ns':20}
    second=lambda:{'origin_ns':30,'start_wall_ns':40}
    setup.process.clock_mapping=first
    lease=setup.process.acquire(setup.scope,clock_mapping=first)
    with pytest.raises(RuntimeError,match='run_active'):
        setup.process.acquire(setup.scope,clock_mapping=second)
    assert setup.process.clock_mapping is first
    lease.close()
    assert setup.process.clock_mapping is warm.idle_clock_mapping
    new=setup.process.acquire(setup.scope,clock_mapping=second)
    assert setup.process.clock_mapping is second
    new.close();setup.process.close()


def test_web_task_name_and_recovery_budget_get_fresh_plan_context(setup,monkeypatch,tmp_path):
    calls=[]
    def service(task,fixture,socket,*,clock):
        calls.append((task,fixture))
        return SimpleNamespace(task=task,fixture=fixture,clock=clock,numerics=None)
    monkeypatch.setattr('f1_harness.skill_services.PlanService',service)
    old=setup.process.acquire(setup.scope)
    old.dispatch('prepare_grasp',dict(parent_intent=setup.scope,key='old'));old.close()
    task=setup.service.task.model_copy(update=dict(task_id='web-new',local_corrections=0))
    scope=dict(setup.scope,task_id=task.task_id,task_hash=digest(task),session_id='new')
    record=tmp_path/'current-run.json';record.write_text('{}')
    lease=setup.process.acquire(scope,task=task,fixture={},identity_files=[record])
    assert len(calls)==1 and calls[0][0].task_id=='web-new' and calls[0][0].local_corrections==0
    assert setup.service.worker.prepared is None
    with pytest.raises(HarnessError,match='scope_mismatch'):
        lease.dispatch('prepare_grasp',dict(parent_intent=setup.scope,key='stale'))
    lease.dispatch('prepare_grasp',dict(parent_intent=scope,key='new'))
    assert lease.dispatch('adopt_grasp',dict(intent=scope,key='new'))['proposal']=={'key':'new'}
    record.write_text('{"changed":true}')
    with pytest.raises(RuntimeError,match='source_changed'):
        lease.dispatch('prepare_grasp',dict(parent_intent=scope,key='again'))
    assert setup.process.cleanup['reaped']


def test_fresh_context_must_pass_constructor_even_when_recipe_matches(setup,monkeypatch,tmp_path):
    def reject(*_,**__):raise ValueError('current_frame_not_validated')
    monkeypatch.setattr('f1_harness.skill_services.PlanService',reject)
    record=tmp_path/'current-run.json';record.write_text('{}')
    task=setup.service.task.model_copy(update=dict(task_id='web-new'))
    scope=dict(setup.scope,task_id=task.task_id,task_hash=digest(task))
    with pytest.raises(ValueError,match='current_frame_not_validated'):
        setup.process.acquire(scope,task=task,fixture={},identity_files=[record])
    assert setup.process.cleanup['reaped'] and setup.service.active is None


def test_moving_recipe_change_is_not_hidden_as_web_run_identity(setup,tmp_path):
    record=tmp_path/'current-run.json';record.write_text('{}')
    with pytest.raises(HarnessError,match='recipe_changed'):
        setup.process.acquire(setup.scope,task=setup.service.task,fixture={'grasp_depth':.1},identity_files=[record])
    assert setup.process.cleanup['reaped']


def test_idle_worker_does_not_retain_finished_replay_globals(setup):
    import gc,weakref
    class Marker:pass
    marker=Marker();reference=weakref.ref(marker)
    namespace={'payload':marker}
    exec('def mapping(): return {"origin_ns": 1, "start_wall_ns": 2}',namespace)
    lease=setup.process.acquire(setup.scope,clock_mapping=namespace['mapping'])
    del namespace,marker
    gc.collect();assert reference() is not None  # active callback legitimately retains it
    lease.close();gc.collect()
    assert reference() is None
    setup.process.close()


def test_default_lease_restores_template_after_explicit_web_context(setup,monkeypatch,tmp_path):
    def service(task,fixture,socket,*,clock):return SimpleNamespace(task=task,fixture=fixture,clock=clock,numerics=None)
    monkeypatch.setattr('f1_harness.skill_services.PlanService',service)
    task=setup.service.task.model_copy(update=dict(task_id='web-new',local_corrections=0))
    scope=dict(setup.scope,task_id=task.task_id,task_hash=digest(task),session_id='new')
    record=tmp_path/'current-run.json';record.write_text('{}')
    explicit=setup.process.acquire(scope,task=task,fixture={},identity_files=[record]);explicit.close()
    default=setup.process.acquire(setup.scope)
    assert setup.service.worker.service is setup.service.template
    assert setup.service.worker.service.task.task_id==setup.scope['task_id']
    default.close();setup.process.close()
