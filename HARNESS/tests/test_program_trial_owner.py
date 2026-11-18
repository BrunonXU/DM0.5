"""Real owner mailbox and host selection, no control outputs."""
from types import SimpleNamespace
import pytest
from f1_harness.contracts import Task,SkillIntent,HarnessError,digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.program_trials import owner_program_trial
from f1_harness.task_runner import _freeze_program_trial
from test_program_trials import observed


def owner(tmp_path):
    engine=TaskEngine(EvidenceStore(tmp_path),mode='fake',clock=lambda:10)
    engine.store.event('run_identity',synthetic=True,scenario_id=None,
                       scene_manifest_sha256=None,heldout=False)
    engine.program_trials_enabled=True
    session=engine.open_session()['session_id']
    task=Task(task_id='run',object_id='block',hole_id='hole',calibration_id='cal')
    engine.prepare_task(session,task.model_dump())
    engine.recording_identity={'owner_run_id':'run'}
    engine.hardware_ready=True
    engine.local_start(physical_event=True,feedback_healthy=True)
    engine.update_observation(observed('initial').model_copy(update={'stationary':True}))
    intent=SkillIntent(request_id='first',session_id=session,owner_instance_id=engine.instance,
        task_id=task.task_id,task_hash=digest(task),epoch=engine.epoch,
        observation_id='initial',scene_version='initial',calibration_id='cal',skill='approach_grasp')
    return engine,intent


def rows(engine):
    import json
    return [json.loads(line) for line in (engine.store.root/'trace.jsonl').read_text().splitlines()]


def test_first_action_freezes_actual_owner_scene_without_extra_request(tmp_path):
    engine,intent=owner(tmp_path)
    assert engine.submit_skill(intent.model_dump())['lifecycle']=='accepted'
    trial=owner_program_trial(rows(engine))
    assert trial['mode']=='discovery' and trial['candidate'] is None
    assert trial['observation']==engine.observation.model_dump(mode='json')
    assert trial['scope']['owner_instance_id']==engine.instance
    assert not trial['generalization_proven']


def test_registration_is_once_and_rejects_changed_observation_or_late_label(tmp_path):
    engine,intent=owner(tmp_path)
    with pytest.raises(HarnessError,match='observation_changed'):
        engine.register_program_trial(engine.session,'old')
    first=engine.register_program_trial(engine.session,'initial')
    assert engine.register_program_trial(engine.session,'initial')==first
    assert sum(r['kind']=='program_trial_frozen' for r in rows(engine))==1
    with pytest.raises(HarnessError,match='already_frozen'):
        engine.register_program_trial(engine.session,'initial',candidate={'claim':'success'})
    engine.submit_skill(intent.model_dump())
    engine.program_trial=None # Simulate no prior registration; existing accepted action remains.
    with pytest.raises(HarnessError,match='precede_first_action'):
        engine.register_program_trial(engine.session,'initial')


def test_learning_metadata_absence_does_not_veto_valid_motion(tmp_path):
    engine,intent=owner(tmp_path)
    engine.update_observation(engine.observation.model_copy(update={'frames':()}))
    result=engine.submit_skill(intent.model_dump())
    assert result['lifecycle']=='accepted'
    assert engine.program_trial is None
    assert any(r['kind']=='program_trial_unavailable' for r in rows(engine))


def test_trial_never_restores_authority_after_stop(tmp_path):
    engine,_=owner(tmp_path)
    engine.revoke('operator_stop')
    with pytest.raises(HarnessError,match='current_authority'):
        engine.register_program_trial(engine.session,'initial')
    assert engine.program_trial is None


@pytest.mark.parametrize('mismatch',[False,True])
@pytest.mark.parametrize('partial',[False,True])
def test_host_uses_pending_candidate_only_after_owner_ack(tmp_path,monkeypatch,mismatch,partial):
    import f1_harness.program_experience as programs
    candidate={'id':'c','trial_candidate':{'candidate_id':'c'}}
    monkeypatch.setattr(programs,'pending_agent_programs',lambda *a,**k:[candidate])
    monkeypatch.setattr(programs,'validated_agent_programs',lambda *a,**k:[])
    called=[]
    def call(method,**kwargs):
        called.append((method,kwargs))
        return {'registered':True,'trial':{'candidate':None if mismatch else kwargs['candidate']}}
    backend=SimpleNamespace(session='session',call=call)
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    selected=_freeze_program_trial(backend,task,{},tmp_path,dict(geometry_available=not partial,local_environment_available=partial,
        observation_id='current'),EvidenceStore(tmp_path/'host'),supported=True)
    assert selected==(None if mismatch else candidate)
    assert called[0][0]=='register_program_trial'
    assert called[0][1]['observation_id']=='current'


def test_unavailable_initial_geometry_keeps_normal_exploration(tmp_path):
    backend=SimpleNamespace(call=lambda *a,**k:pytest.fail('must not register invented scene'))
    assert _freeze_program_trial(backend,None,None,tmp_path,{'geometry_available':False},
        EvidenceStore(tmp_path/'host'),supported=True) is None
