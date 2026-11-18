"""Phase scheduling: a cloud reply is never a gate while the VLA still moves."""
from types import SimpleNamespace
import pytest
from f1_harness.hybrid_runner import run_hybrid,placement_tail_context


def scenario(monkeypatch, *, release=False, revoked=False, unknown=False):
    ticks=[0];calls=[];phase=['base'];events=[]
    monkeypatch.setattr('f1_harness.hybrid_runner.time.sleep',lambda _:ticks.__setitem__(0,ticks[0]+1))
    class Backend:
        session='session'
        def call(self,method,**kw):
            if method=='status':
                return dict(authorized=not (revoked and ticks[0]),epoch=1,
                    hybrid=dict(phase=phase[0],opportunity=dict(identity=['owner','session',1,1],
                        inside=True,closed=not (release and ticks[0]),eligible=True)))
            assert method=='hybrid_control';calls.append(kw['operation'])
            if kw['operation']=='recover':phase[0]='local'
            if kw['operation']=='acquire':
                if unknown:raise TimeoutError('unknown acquire result')
                if not release and not revoked:phase[0]='local'
            return dict(accepted=True)
    planner=SimpleNamespace(request=lambda **kw:pytest.fail('cloud call before handoff'))
    return lambda:run_hybrid(Backend(),planner,SimpleNamespace(event=lambda k,**v:events.append((k,v))),
        cancelled=lambda:False,clock=lambda:ticks[0]),calls,events


def test_handoff_precedes_model_or_geometry_work(monkeypatch):
    run,calls,events=scenario(monkeypatch)
    assert run()['outcome']=='handoff'
    assert calls==['claim','acquire']
    assert events[-1][1]['base_continues'] is False
    assert events[-1][1]['placement_verified'] is False


@pytest.mark.parametrize('kw,reason',[({'release':True},'handoff_window_changed'),
                                     ({'revoked':True},'owner_authority_ended')])
def test_released_window_yields_but_revoked_authority_stops(monkeypatch,kw,reason):
    run,calls,_=scenario(monkeypatch,**kw)
    assert run()['reason']==reason
    assert calls==(['claim','acquire','recover'] if kw.get('release') else ['claim','acquire'])


def test_unknown_acquire_is_not_replayed(monkeypatch):
    run,calls,_=scenario(monkeypatch,unknown=True)
    with pytest.raises(TimeoutError):run()
    assert calls==['claim','acquire']


def test_local_tail_uses_registered_program_and_retains_checkpoint():
    calls=[];receipt=dict(value='pending',primitive_calls=[dict(request_id='original',outcome='pending_review')])
    def execute(tool,args):calls.append((tool,args));return SimpleNamespace(result=receipt)
    assert placement_tail_context(SimpleNamespace(execute_tool=execute),SimpleNamespace(event=lambda *a,**kw:None)) is receipt
    assert len(calls)==1 and 'hybrid_tail_result = continue_external_task()' in calls[0][1]['code']

@pytest.mark.parametrize('reason',[None,'vla_grasp_not_attached','vla_attachment_lost'])
def test_outside_region_handoff_reserves_shared_recovery_budget(monkeypatch,reason):
    t=[0];calls=[];phase=['base']
    monkeypatch.setattr('f1_harness.hybrid_runner.time.sleep',lambda _:t.__setitem__(0,t[0]+1))
    class Backend:
        session='s';task_deadline=300
        def call(self,method,**kw):
            if method=='status':return dict(authorized=True,epoch=1,hybrid=dict(phase=phase[0],
                recovery_reason=reason,opportunity=dict(identity=['o','s',1,0],eligible=False,inside=False)))
            calls.append(kw['operation']);phase[0]='local';return dict(accepted=True)
    result=run_hybrid(Backend(),None,SimpleNamespace(event=lambda *a,**kw:None),cancelled=lambda:False,clock=lambda:t[0])
    assert result['outcome']=='handoff' and t[0]<300
    assert result['reason']==(reason or 'vla_segment_budget_yield')
    assert calls==['recover']
