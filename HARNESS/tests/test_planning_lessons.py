import json
from copy import deepcopy

import pytest

from f1_harness.contracts import Task,SkillIntent,digest,HarnessError
from f1_harness.planning_lessons import review_candidate_orders
from test_plan_revalidation import service,observation


@pytest.fixture
def planning(monkeypatch):
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    serv=service([1_000_000_000],task);obs=observation(1_000_000_000)
    # Production fixture is JSON; the generic numeric test helper uses arrays.
    serv.fixture=json.loads(json.dumps(serv.fixture,default=lambda value:value.tolist()))
    intent=SkillIntent(request_id='req',session_id='session',owner_instance_id='owner',task_id='task',task_hash=digest(task),
        epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,calibration_id='coarse',skill='approach_grasp')
    calls=[]
    def solve(i,s,h,f,v,*,deadline_ns=None): 
        calls.append(v)
        if v<2:raise HarnessError('scene_sweep_blocked')
        return dict(request_id=i.request_id,epoch=i.epoch,calibration_id=i.calibration_id,
                    candidate_variant=v,geometry_trace=[{}])
    monkeypatch.setattr(serv,'_solve_candidate',solve)
    kwargs=dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=[0]*22,held16=list(obs.state16))
    return serv,kwargs,calls


def source(tmp_path,planning,*,synthetic=False):
    serv,kwargs,calls=planning;proposal=serv.dispatch('plan_skill',kwargs)
    rows=[dict(kind='harness_planning_started',request_id='req',intent=kwargs['intent'],observation=kwargs['observation']),
          dict(kind='harness_plan_waiting_fresh_revalidation',event_id='event',request_id='req',candidate=proposal)]
    if synthetic:rows[0]['simulation']=True
    run=tmp_path/'run';p=run/'collected/owner/harness/trace.jsonl';p.parent.mkdir(parents=True)
    p.write_text('\n'.join(map(json.dumps,rows)))
    return run,p,rows


def candidate(review):
    record=json.loads(open(review['path']).read());return json.loads(open(record['candidates'][0]['path']).read())


def test_successful_plan_keeps_rejected_attempts_and_immutable_scope(planning):
    serv,kwargs,calls=planning;result=serv.dispatch('plan_skill',kwargs)
    audit=result['geometry_trace'][0]['candidate_evaluation']
    assert calls==[0,1,2]
    assert [a['outcome'] for a in audit['attempts']]==['rejected','rejected','planned']
    assert audit['attempts'][0]['diagnostic']['code']=='scene_sweep_blocked'
    assert audit['scope']['fixture_sha256']==digest(serv.fixture)
    assert audit['scope']['task_hash']==digest(serv.task)
    assert audit['registered_variants']==[0,1,2,3] and audit['motion_success_inferred'] is False


def test_order_proposal_is_quarantined_preserves_all_candidates_and_deduplicates(tmp_path,planning):
    run,p,rows=source(tmp_path,planning,synthetic=True)
    p.write_text('\n'.join(map(json.dumps,rows+[rows[-1]])))
    root=tmp_path/'memory';report=review_candidate_orders(run,root)
    assert report['candidate_count']==1 and report['invalid_count']==0
    value=candidate(report)
    assert value['candidate_order']==[2,0,1,3] and value['synthetic']
    assert not value['execution_enabled'] and not value['motion_success_inferred']
    assert not value['limits_modified'] and value['cost_is_counterfactual_not_measured_speedup']
    assert value['avoided_attempt_cost_observed_ns']==sum(a['elapsed_ns'] for a in value['evaluation']['attempts'][:-1])
    assert review_candidate_orders(run,root)==report
    assert len(list((root/'planning-candidates').glob('*.md')))==1
    assert not list(root.glob('*.snapshot.json')) and not (root/'published').exists()


@pytest.mark.parametrize('change',['request','epoch','task','calibration','object_skill','fixture','variant','attempt_duplicate','attempt_negative','registered','accept_early','missing_rejection'])
def test_unbound_or_malformed_diagnostics_cannot_produce_order(tmp_path,planning,change):
    run,p,rows=source(tmp_path,planning);e=rows[-1]['candidate']['geometry_trace'][0]['candidate_evaluation']
    if change=='request':e['request_id']='another'
    elif change=='epoch':e['epoch']+=1
    elif change=='task':e['scope']['task_hash']='b'*64
    elif change=='calibration':e['scope']['calibration_id']='other'
    elif change=='object_skill':e['scope']['skill']='insert'
    elif change=='fixture':e['scope']['fixture_sha256']='missing'
    elif change=='variant':e['selected_variant']=3
    elif change=='attempt_duplicate':e['attempts'][1]['variant']=0
    elif change=='attempt_negative':e['attempts'][0]['elapsed_ns']=-1
    elif change=='registered':e['registered_variants']=[2]
    elif change=='accept_early':e['attempts'][0]['outcome']='planned'
    else:e['attempts'][0].pop('diagnostic')
    p.write_text('\n'.join(map(json.dumps,rows)))
    report=review_candidate_orders(run,tmp_path/'memory')
    assert report['candidate_count']==0 and report['invalid_count']==1


def test_no_rejected_attempt_means_no_new_lesson(tmp_path,planning):
    planning[0].fixture['candidate_order']={'approach_grasp':[2,0,1,3]}
    run,p,rows=source(tmp_path,planning)
    result=review_candidate_orders(run,tmp_path/'memory')
    assert result['candidate_count']==0 and result['invalid_count']==0


def test_diagnostic_source_read_error_cannot_reject_valid_plan(planning,monkeypatch):
    serv,kwargs,calls=planning
    monkeypatch.setattr('f1_harness.planning_lessons.planning_scope',lambda *a:(_ for _ in ()).throw(OSError('unavailable')))
    result=serv.dispatch('plan_skill',kwargs)
    assert result['candidate_variant']==2 and result['geometry_trace'][0]['candidate_evaluation_error']=='OSError'


def test_old_trace_without_attempts_stays_uninvented(tmp_path,planning):
    run,p,rows=source(tmp_path,planning);rows[-1]['candidate']['geometry_trace'][0].pop('candidate_evaluation')
    p.write_text('\n'.join(map(json.dumps,rows)))
    result=review_candidate_orders(run,tmp_path/'memory')
    assert result['candidate_count']==0 and result['invalid_count']==0


def test_normal_postrun_review_picks_up_planning_failures_without_promoting(tmp_path,planning):
    from f1_harness.run_review import summarize_run
    run,p,rows=source(tmp_path,planning,synthetic=True);root=tmp_path/'memory'
    report=summarize_run(run,root)
    assert report['planning_order_review']['candidate_count']==1
    assert not report['promoted'] and not report['measured_placement_success']
    assert summarize_run(run,root)==report
    original=set((root/'planning-candidates').iterdir())
    rows[-1]['candidate']['geometry_trace'][0]['candidate_evaluation']['attempts'][0]['elapsed_ns']+=1
    p.write_text('\n'.join(map(json.dumps,rows)))
    with pytest.raises(ValueError,match='reviewed_owner_trace_changed'):summarize_run(run,root)
    assert set((root/'planning-candidates').iterdir())==original
