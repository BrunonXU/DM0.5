"""Synthetic journals test lineage, not successful physical recovery."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from f1_harness.contracts import HarnessError, digest
from f1_harness.recovery_programs import registered_program
from f1_harness.program_verifier import verify_program
from f1_harness.memory import ExperienceStore, read_snapshot
from f1_harness.programs import baseline_experience


def journal(program='verify_completed_motion', *, returning=False):
    scope = dict(owner_instance_id='owner', session_id='session', task_id='task',
                 task_hash='task-hash', epoch=1, calibration_id='cal')
    def accepted(q, skill, **extra):
        return dict(kind='skill_accepted', intent=dict(scope, request_id=q, skill=skill, **extra))
    def result(q, skill, outcome='success', **extra):
        return dict(kind='skill_result', skill=skill, result=dict(request_id=q, outcome=outcome,
            synthetic=True, assisted=False, evidence_ids=['evidence'], **extra))
    rows = [dict(kind='run_identity', scenario_id='heldout', synthetic=True),
            dict(kind='task_prepared', task_hash='task-hash',
                 task=dict(allowed_skills=['grasp','verify_placed'] + (['withdraw_reset'] if returning else []))),
            accepted('original', 'grasp'),
            dict(kind='harness_skill_first_publication', request_id='original')]
    if program == 'verify_completed_motion':
        rows += [dict(kind='harness_measured_waypoint', request_id='original', measured_stamp_ns=10),
                 dict(kind='harness_verification_escalated', request_id='original', after_ns=11, motion_replayed=False),
                 result('original', 'grasp', 'pending_review', verification_request_id='original'),
                 accepted('recovered', 'grasp', verification_request_id='original'),
                 dict(kind='harness_verification_continued', request_id='recovered', motion_request_id='original',
                      after_ns=11, motion_replayed=False, skill='grasp', epoch=1, task_hash='task-hash')]
    else:
        rows += [result('original', 'grasp', 'failure', reason='motion_stalled',
                        cancel_stage='cancel_requested', resume_request_id='original'),
                 dict(kind='harness_motion_checkpoint', request_id='original', skill='grasp',
                      epoch=1, task_hash='task-hash', resumable=True, preclosure=True),
                 dict(kind='hold_acknowledged', stage='measured_stationary'),
                 accepted('recovered', 'grasp', resume_request_id='original'),
                 dict(kind='harness_skill_first_publication', request_id='recovered')]
    rows += [result('recovered','grasp'), accepted('placed','verify_placed'), result('placed','verify_placed')]
    if returning:
        rows += [accepted('reset','withdraw_reset'), dict(kind='harness_measured_waypoint',request_id='reset'),
                 result('reset','withdraw_reset')]
    return rows


def evaluate(rows, program):
    placement = next(r for r in rows if r.get('kind') == 'skill_result' and r.get('skill') == 'verify_placed')
    candidate = dict(program=registered_program(program), skills=['grasp'])
    return verify_program(candidate, rows, placement, synthetic=True)


@pytest.mark.parametrize('program',['verify_completed_motion','resume_known_progress'])
@pytest.mark.parametrize('mutation',['none','other_epoch','other_session','other_owner','other_task',
    'ordinary_success','no_parent','revoked','failed_recovery','assisted','no_evidence','reordered',
    'unrelated_action','no_task','duplicate_request'])
def test_program_requires_actual_bound_recovery_not_merely_successful_placement(program,mutation):
    rows=journal(program)
    accepted=next(r for r in rows if r.get('intent',{}).get('request_id')=='recovered')
    recovered=next(r for r in rows if r.get('result',{}).get('request_id')=='recovered')
    field=registered_program(program)['request_field']
    if mutation.startswith('other_'):
        key={'other_epoch':'epoch','other_session':'session_id','other_owner':'owner_instance_id','other_task':'task_hash'}[mutation]
        accepted['intent'][key]='different'
    if mutation=='ordinary_success':accepted['intent'].pop(field)
    if mutation=='no_parent':accepted['intent'][field]='unrecorded'
    if mutation=='revoked':rows.insert(rows.index(accepted),dict(kind='authority_revoked'))
    if mutation=='failed_recovery':recovered['result']['outcome']='failure'
    if mutation=='assisted':recovered['result']['assisted']=True
    if mutation=='no_evidence':recovered['result']['evidence_ids']=[]
    if mutation=='reordered':rows.remove(recovered);rows.insert(rows.index(accepted),recovered)
    if mutation=='unrelated_action':
        extra=deepcopy(accepted);extra['intent'].update(request_id='different',skill='retreat')
        rows.insert(rows.index(accepted),extra)
    if mutation=='no_task':rows=[r for r in rows if r['kind']!='task_prepared']
    if mutation=='duplicate_request':rows.insert(rows.index(accepted),deepcopy(accepted))
    assert bool(evaluate(rows,program)) is (mutation=='none')


@pytest.mark.parametrize('mutation',['wrong_after','replayed','no_arrival','new_motion','wrong_skill'])
def test_recheck_is_evidence_only_and_bound_to_completed_motion(mutation):
    rows=journal()
    continued=next(r for r in rows if r['kind']=='harness_verification_continued')
    if mutation=='wrong_after':continued['after_ns']=12
    if mutation=='replayed':continued['motion_replayed']=True
    if mutation=='no_arrival':rows=[r for r in rows if r['kind']!='harness_measured_waypoint']
    if mutation=='new_motion':rows.insert(rows.index(continued),dict(kind='harness_skill_first_publication',request_id='recovered'))
    if mutation=='wrong_skill':continued['skill']='insert'
    assert evaluate(rows,'verify_completed_motion') is None


@pytest.mark.parametrize('mutation',['not_preclosure','not_resumable','no_hold','hold_before_checkpoint','unpublished'])
def test_resume_requires_owner_checkpoint_and_hold_after_stall(mutation):
    rows=journal('resume_known_progress')
    checkpoint=next(r for r in rows if r['kind']=='harness_motion_checkpoint')
    hold=next(r for r in rows if r['kind']=='hold_acknowledged')
    if mutation=='not_preclosure':checkpoint['preclosure']=False
    if mutation=='not_resumable':checkpoint['resumable']=False
    if mutation=='no_hold':hold['stage']='owner_hold_committed'
    if mutation=='hold_before_checkpoint':rows.remove(hold);rows.insert(rows.index(checkpoint),hold)
    if mutation=='unpublished':rows=[r for r in rows if not (r['kind']=='harness_skill_first_publication' and r['request_id']=='recovered')]
    assert evaluate(rows,'resume_known_progress') is None


def test_full_cycle_lesson_requires_return_and_preserves_placement_only_legacy():
    rows=journal(returning=True)
    assert evaluate(rows,'verify_completed_motion')
    rows[-1]['result']['outcome']='failure'
    assert evaluate(rows,'verify_completed_motion') is None
    assert evaluate(journal(),'verify_completed_motion')


def propose(memory,root,program='verify_completed_motion'):
    from f1_harness.failure_corpus import record_failures
    from f1_harness.learning_review import review_failures
    source=root/'discovery/collected/owner/harness/trace.jsonl'
    source.parent.mkdir(parents=True)
    rows=journal(program)
    # Discovery contains a failed checkpoint, not a recovered success.
    rows=rows[:next(i for i,r in enumerate(rows) if r.get('intent',{}).get('request_id')=='recovered')]
    source.write_text('\n'.join(map(json.dumps,rows)))
    review=review_failures(record_failures(root/'discovery',memory.root),memory.root)
    lesson=json.loads(Path(review['path']).read_text())['candidates'][0]['path']
    return memory.propose_program(lesson,skill='grasp')


@pytest.mark.parametrize('program',['verify_completed_motion','resume_known_progress'])
def test_formal_lesson_to_independent_verifier_to_rpent_snapshot(tmp_path,program):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=propose(memory,tmp_path,program)
    assert memory.snapshot()['count']==0
    run=tmp_path/'validation';run.mkdir()
    (run/'trace.jsonl').write_text('\n'.join(map(json.dumps,journal(program))))
    receipt=memory.verify(key,validation_run=run,scenario_id='heldout')
    assert receipt['accepted'] and receipt['verifier']=='f1-program-verifier-v3'
    assert receipt['program_match']['parent_request_id']=='original'
    assert memory.verify(key,validation_run=run,scenario_id='heldout')==receipt
    snapshot=read_snapshot(Path(memory.promote(key)['path']),synthetic=True)
    assert baseline_experience(snapshot)[0]['strategy']==program
    assert baseline_experience(snapshot)[0]['id']==key


def test_changed_trace_or_old_review_cannot_promote_formal_program(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=propose(memory,tmp_path)
    run=tmp_path/'validation';run.mkdir();p=run/'trace.jsonl'
    p.write_text('\n'.join(map(json.dumps,journal())))
    memory.verify(key,validation_run=run,scenario_id='heldout')
    p.write_text(p.read_text()+'\n{}')
    with pytest.raises(HarnessError,match='trace_changed'):memory.promote(key)


def test_copy_of_discovery_is_not_an_independent_validation(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True);key=propose(memory,tmp_path)
    run=tmp_path/'copy';run.mkdir()
    (run/'trace.jsonl').write_bytes((tmp_path/'discovery/collected/owner/harness/trace.jsonl').read_bytes())
    with pytest.raises(HarnessError,match='independent'):memory.verify(key,validation_run=run,scenario_id='different')


@pytest.mark.parametrize('break_trigger',[False,True])
def test_insertion_program_requires_contiguous_recovery_after_failed_insertion(break_trigger):
    rows=journal();scope=deepcopy(rows[2]['intent'])
    rows=rows[:2]
    for q,skill,outcome in [('failed','insert','success' if break_trigger else 'failure'),
                            ('back','retreat','success'),('look','reobserve','success'),
                            ('align','align','success'),('insert','insert','success'),
                            ('placed','verify_placed','success')]:
        rows.extend([dict(kind='skill_accepted',intent=dict(scope,request_id=q,skill=skill)),
                     dict(kind='skill_result',skill=skill,result=dict(request_id=q,outcome=outcome,
                          synthetic=True,assisted=False,evidence_ids=['evidence']))])
    program=registered_program('insert_reobserve_realign')
    result=verify_program(dict(program=program,skills=program['skills']),rows,rows[-1],synthetic=True)
    assert bool(result) is (not break_trigger)


def test_real_lesson_does_not_promote_from_development_success_without_heldout_identity(tmp_path):
    memory=ExperienceStore(tmp_path/'memory')
    synthetic=ExperienceStore(tmp_path/'synthetic',synthetic=True)
    key=propose(synthetic,tmp_path)
    # Test-only fixture converts synthetic fields to exercise the real-mode gate;
    # no physical run or held-out result is asserted by this test.
    value=synthetic._candidate(key);value['synthetic']=False;key=digest(value)
    memory.records.write_json(key+'.candidate.json',value)
    rows=journal()
    for row in rows:
        if row['kind']=='run_identity':row['synthetic']=False
        if row['kind']=='skill_result':row['result']['synthetic']=False
    rows.append(dict(kind='owner_placement_verified',request_id='placed'))
    run=tmp_path/'development';run.mkdir()
    (run/'trace.jsonl').write_text('\n'.join(map(json.dumps,rows)))
    assert not memory.verify(key,validation_run=run,scenario_id='heldout')['accepted']
    with pytest.raises(HarnessError,match='not_verified'):memory.promote(key)


def test_formal_review_tampering_is_recomputed_before_promotion(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True);key=propose(memory,tmp_path)
    run=tmp_path/'validation';run.mkdir();rows=journal()
    next(r for r in rows if r.get('intent',{}).get('request_id')=='recovered')['intent'].pop('verification_request_id')
    (run/'trace.jsonl').write_text('\n'.join(map(json.dumps,rows)))
    receipt=memory.verify(key,validation_run=run,scenario_id='heldout')
    assert not receipt['accepted']
    receipt.update(accepted=True,sequence_request_ids=['recovered'])
    (memory.records.root/(key+'.review.json')).write_text(json.dumps(receipt))
    with pytest.raises(HarnessError,match='validation_changed'):memory.promote(key)
