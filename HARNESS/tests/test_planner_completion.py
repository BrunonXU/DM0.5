import json
from types import SimpleNamespace

import pytest
from pyrualean.arms import ArmToolkit

from f1_harness.adapters import F1Robot,ScopedBackend
from f1_harness.contracts import Task,SKILLS,Mode
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.fake import FakePlant
from f1_harness.planner import ResponsesPlanner
from f1_harness.programs import single_block_program
from f1_harness.task_runner import _finish_completed_task


@pytest.mark.parametrize('failures,automatic',[
    ({},True),({'withdraw_reset':1},False),({'verify_placed':1},False),
    ({'grasp':1,'regrasp':1},False)])
def test_actual_owner_completion_avoids_only_redundant_model_round(tmp_path,failures,automatic):
    engine=TaskEngine(EvidenceStore(tmp_path/'owner'),mode=Mode.FAKE,lease_ms=2000)
    session=engine.open_session()['session_id']
    task=Task(task_id='cycle',object_id='square',hole_id='hole',calibration_id='fake',allowed_skills=SKILLS)
    engine.prepare_task(session,task.model_dump())
    plant=FakePlant(engine,failures);plant.start()
    backend=ScopedBackend(engine,plant,session_id=session,plant=plant)
    arm=ArmToolkit(F1Robot(backend),arm='cells',workspace=tmp_path/'worker',images=False)
    planner=object.__new__(ResponsesPlanner);planner.store=EvidenceStore(tmp_path/'planner')
    calls=[];finishes=[]
    def request(**kwargs):
        calls.append(kwargs)
        name='python' if len(calls)==1 else 'finish'
        arguments=({'code':single_block_program(task)} if name=='python' else {'status':'stuck','summary':'incomplete'})
        return dict(output=[dict(type='function_call',name=name,call_id=str(len(calls)),arguments=json.dumps(arguments))])
    def finish_call(method,**kwargs):
        finishes.append(method)
        return engine.finish_task(**kwargs)
    def complete():
        host=SimpleNamespace(pending=None,session=session,solved=backend.solved,
            placement_verified='verify_placed' in engine.completed_skills,call=finish_call)
        return _finish_completed_task(host,task)
    planner.request=request
    result=planner.run(arm,task_text='complete the task',complete_run=complete)
    assert len(calls)==(1 if automatic else 2)
    assert finishes==(['finish_task'] if automatic else [])
    assert result['status']==('success' if automatic else 'stuck')
    assert engine.authorized is (not automatic)


def test_stdout_success_does_not_replace_trusted_completion(tmp_path):
    planner=object.__new__(ResponsesPlanner);planner.store=EvidenceStore(tmp_path)
    calls=[]
    class Arm:
        def get_tools_spec(self):return [dict(name=n,description=n,input_schema={}) for n in ('python','finish')]
        def execute_tool(self,name,args):
            return SimpleNamespace(result=dict(stdout='all done',status='success',episode_finished=True),images=[])
    def request(**kwargs):
        calls.append(kwargs)
        return dict(output=[dict(type='function_call',name='python' if len(calls)==1 else 'finish',
            call_id=str(len(calls)),arguments='{}')])
    planner.request=request
    planner.run(Arm(),task_text='task',complete_run=lambda:None)
    assert len(calls)==2


def test_stop_after_tool_precedes_automatic_finish(tmp_path):
    planner=object.__new__(ResponsesPlanner);planner.store=EvidenceStore(tmp_path)
    running=[True];completed=[]
    class Arm:
        def get_tools_spec(self):return [dict(name=n,description=n,input_schema={}) for n in ('python','finish')]
        def execute_tool(self,name,args):
            running[0]=False
            return SimpleNamespace(result={'stdout':'done'},images=[])
    planner.request=lambda **kw:dict(output=[dict(type='function_call',name='python',call_id='1',arguments='{}')])
    result=planner.run(Arm(),task_text='task',continue_run=lambda:running[0],complete_run=lambda:completed.append(True))
    assert result['outcome']=='interrupted' and not completed


@pytest.mark.parametrize('goal,pending,verified,solved',[
    ('approach_grasp',None,True,True),('placed','active',True,True),
    ('placed',None,False,True),('placed',None,True,False)])
def test_partial_or_pending_task_never_calls_owner_finish(goal,pending,verified,solved):
    backend=SimpleNamespace(pending=pending,placement_verified=verified,solved=lambda:solved)
    assert _finish_completed_task(backend,SimpleNamespace(goal=goal)) is None


@pytest.mark.parametrize('pending,session,expected',[
    (None,'session',[]),(None,'other',['finish_task']),
    ('request','session',['cancel_skill','finish_task'])])
def test_cleanup_reuses_only_received_same_session_finish(pending,session,expected):
    from f1_harness.remote import RemoteTaskBackend
    backend=object.__new__(RemoteTaskBackend)
    backend.session='session';backend.pending=None;backend._closed=False
    backend.defer_session_close=True;calls=[]
    def call(method,**kwargs):
        calls.append(method)
        return {'outcome':'pending_review'}
    backend.rpc=SimpleNamespace(call=call)
    receipt=backend.call('finish_task',session_id='session')
    calls.clear();backend.session=session;backend.pending=pending
    assert backend.cancel()==receipt
    assert calls==expected
    assert backend._closed


def test_unknown_finish_is_not_cached_as_confirmed_cleanup():
    from f1_harness.remote import RemoteTaskBackend
    backend=object.__new__(RemoteTaskBackend)
    backend.session='session';backend.pending=None;backend._closed=False
    backend.defer_session_close=True;calls=[]
    def call(method,**kwargs):
        calls.append(method)
        if len(calls)==1:raise TimeoutError('receipt unavailable')
        return {'outcome':'pending_review'}
    backend.rpc=SimpleNamespace(call=call)
    with pytest.raises(TimeoutError):backend.call('finish_task',session_id='session')
    assert backend.cancel()=={'outcome':'pending_review'}
    assert calls==['finish_task','finish_task']


def test_preparing_another_task_invalidates_previous_finish():
    from f1_harness.remote import RemoteTaskBackend
    backend=object.__new__(RemoteTaskBackend)
    backend.session='session';backend.pending=None;backend._closed=False
    backend.defer_session_close=True;calls=[]
    def call(method,**kwargs):
        calls.append(method)
        return {'outcome':'pending_review'}
    backend.rpc=SimpleNamespace(call=call)
    backend.call('finish_task',session_id='session')
    backend.call('prepare_task',session_id='session',task={})
    backend.cancel()
    assert calls==['finish_task','prepare_task','finish_task']
