from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_supervision import approved_steps
from f1_toys.rlt_transition_ledger import new_credits
from f1_toys.rlt_task_evaluation import preregister, preregister_paired, summarize, paired


def row():
    return dict(episode_id='e', segment_id='p', decision_ns=10, next_decision_ns=30,
                command_sequences=[1,2], action_time_mask=[True,True],
                intervene_flags=[True,True], action_grid_ns=[20,30])


def test_expert_review_does_not_follow_success_or_human_flag():
    r = row()
    assert approved_steps(r, {'status':'success'}) == [False,False]
    assert approved_steps(r, {'reviewer':'operator', 'expert_intervals':[{'start_ns':20,'end_ns':30}]}) == [False,True]
    with pytest.raises(ValueError):
        approved_steps(r, {'expert_intervals':[{'start_ns':20,'end_ns':30}]})


def test_budget_relabel_no_credit_new_tail_once():
    first = row(); added, ids = new_credits(set(), [first])
    assert len(added) == 1
    changed = dict(first, reward=1, expert_approved=[True, True])
    assert not new_credits(ids, [changed])[0]
    tail = dict(first, decision_ns=30, next_decision_ns=50, command_sequences=[3,4])
    added, all_ids = new_credits(ids, [changed,tail])
    assert len(added) == 1 and not new_credits(all_ids, [changed,tail])[0]


def test_task_report_excludes_assisted_success_and_retains_infra_denominator():
    plan = preregister('test', {s:s+'-hash' for s in ('b1250','bc400','rlt')})
    rows = [dict(p, reviewed=True, outcome='success', human_intervened=p['strategy']=='rlt',
                 infrastructure_interrupted=False) for p in plan['attempts']]
    rows[0]['infrastructure_interrupted'] = True
    report = summarize(plan, rows)
    assert sum(v['started'] for v in report['strategies'].values()) == 12
    assert sum(v['policy_evaluable'] for v in report['strategies'].values()) == 11
    assert report['strategies']['rlt']['autonomous_successes'] == 0
    assert not report['improvement_claim']
    with pytest.raises(ValueError, match='Duplicate'): summarize(plan, rows+[rows[0]])
    rows[1]['model_identity'] = 'latest_changed'
    with pytest.raises(ValueError, match='weights'): summarize(plan, rows)


def test_exact_paired_test_does_not_treat_ties_as_wins():
    result = paired({'a':True,'b':True}, {'a':False,'b':True})
    assert result == dict(wins=1,losses=0,ties=1,pairs=2,exact_two_sided_mcnemar_p=1.)


def test_confirmatory_cannot_claim_from_partial_or_infra_excluded_pairs():
    plan=preregister_paired('fixed', {'bc400':'a','rlt':'b'}, 'bc400')
    rows=[dict(p,reviewed=True,outcome='success' if p['strategy']=='rlt' else 'failure',
               human_intervened=False,infrastructure_interrupted=False) for p in plan['attempts']]
    assert summarize(plan,rows)['improvement_claim']
    assert not summarize(plan,rows[:20])['improvement_claim']
    rows[0]['infrastructure_interrupted']=True
    assert not summarize(plan,rows)['improvement_claim']
