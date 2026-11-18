import json
from pathlib import Path
import queue

import pytest

from f1_harness.contracts import HarnessError, SkillResult
from f1_harness.evidence import AsyncOwnerJournal, EvidenceStore
from f1_harness.failure_corpus import record_failures
from f1_harness.learning_review import review_failures
from test_unpublished_plan import make_bridge


def rejected_intent(engine):
    engine.needs_correction=True
    return engine.pending.model_copy(update=dict(request_id='insert-rejected',skill='insert'))


def diagnostics(path):
    return [r for r in map(json.loads,(path/'trace.jsonl').read_text().splitlines())
            if r['kind']=='skill_admission_rejected']


@pytest.mark.parametrize('operation',['prepare_skill'])
def test_rejection_records_context_without_mutating_execution(tmp_path,operation):
    e,b,*_=make_bridge(tmp_path)
    try:
        intent=rejected_intent(e)
        before=(e.pending,e.running,dict(e.results),e.task_requests,e.corrections,e.authorized)
        for _ in range(2):
            with pytest.raises(HarnessError,match='failed_insertion_requires_retreat_and_alignment'):
                getattr(e,operation)(intent.model_dump())
        assert before==(e.pending,e.running,e.results,e.task_requests,e.corrections,e.authorized)
        rows=diagnostics(tmp_path);assert len(rows)==1
        assert rows[0]['state']['needs_correction'] is True
        assert rows[0]['no_new_motion'] and not rows[0]['request_had_publication']
        assert e.admission_diagnostics.status()['duplicates']==1
        e.phase='changed-context'
        with pytest.raises(HarnessError):getattr(e,operation)(intent.model_dump())
        assert len(diagnostics(tmp_path))==2
    finally:b.close()


@pytest.mark.parametrize('fault',['writer_raises','writer_drops'])
def test_diagnostics_failure_never_replaces_original_error(tmp_path,fault):
    e,b,*_=make_bridge(tmp_path)
    try:
        intent=rejected_intent(e)
        def writer(*args,**kwargs):
            if fault=='writer_raises':raise OSError('optional writer failed')
        e.store.diagnostic_event=writer
        result=e.submit_skill(intent.model_dump())
        assert result['lifecycle']=='rejected' and result['no_motion']
        assert result['reason']=='failed_insertion_requires_retreat_and_alignment'
        assert e.admission_diagnostics.dropped==1
        assert e.pending.request_id=='one-request' and e.authorized
    finally:b.close()


def test_oversized_request_keeps_original_schema_rejection(tmp_path):
    from pydantic import ValidationError
    e,b,*_=make_bridge(tmp_path)
    try:
        intent=rejected_intent(e).model_copy(update=dict(request_id='x'*129))
        with pytest.raises(ValidationError):e.submit_skill(intent.model_dump())
        assert not diagnostics(tmp_path) and e.task_requests==1
        # Direct diagnostic input is bounded too, independently of the schema.
        e.admission_diagnostics.record(e,intent,'failed_insertion_requires_retreat_and_alignment','submit')
        assert e.admission_diagnostics.dropped==1
    finally:b.close()


@pytest.mark.parametrize('field,value',[('session_id','other'),('epoch',999),('task_hash','other'),
                                      ('owner_instance_id','other'),('calibration_id','other')])
def test_foreign_context_is_not_attributed(tmp_path,field,value):
    e,b,*_=make_bridge(tmp_path)
    try:
        intent=rejected_intent(e).model_copy(update={field:value})
        with pytest.raises(HarnessError):e.prepare_skill(intent.model_dump())
        assert not diagnostics(tmp_path)
    finally:b.close()


def test_cap_and_prior_publication_are_explicit(tmp_path):
    e,b,*_=make_bridge(tmp_path)
    try:
        intent=rejected_intent(e)
        e.request_publications[intent.request_id]={'publication_id':'earlier-motion'}
        for index in range(70):
            trial=intent if index==0 else intent.model_copy(update=dict(request_id=str(index)))
            with pytest.raises(HarnessError):e.prepare_skill(trial.model_dump())
        rows=diagnostics(tmp_path)
        assert len(rows)==64 and rows[0]['request_had_publication'] is True
        assert rows[0]['no_new_motion'] is True
        assert e.admission_diagnostics.dropped==6
        assert e.task_requests==1
    finally:b.close()


def bare_journal():
    journal=object.__new__(AsyncOwnerJournal)
    journal.queue=queue.Queue(maxsize=1);journal.diagnostic_queue=queue.Queue(maxsize=1)
    journal.closed=False;journal.error=None
    return journal


def test_optional_queue_cannot_overflow_required_queue():
    journal=bare_journal()
    assert journal.diagnostic_event('optional',state={'needs_correction':True})
    assert journal.diagnostic_event('optional') is None
    assert journal.error is None and journal.queue.empty()
    assert journal.event('required')
    with pytest.raises(RuntimeError,match='owner_journal_overflow'):journal.event('required')


@pytest.mark.parametrize('bad',[float('nan'),object(),'x'*8192])
def test_invalid_optional_payload_is_dropped_without_poisoning_journal(bad):
    journal=bare_journal()
    assert journal.diagnostic_event('optional',payload=bad) is None
    assert journal.error is None and journal.diagnostic_queue.empty()
    assert journal.event('required')


def test_optional_journal_flush_and_close_preserve_rows(tmp_path):
    journal=AsyncOwnerJournal(EvidenceStore(tmp_path))
    try:
        state={'needs_correction':True}
        first=journal.diagnostic_event('optional',state=state)
        state['needs_correction']=False
        main=journal.event('required');journal.flush()
        rows=list(map(json.loads,(tmp_path/'trace.jsonl').read_text().splitlines()))
        assert {r['event_id'] for r in rows}=={first,main}
        assert next(r for r in rows if r['event_id']==first)['state']['needs_correction'] is True
        last=journal.diagnostic_event('optional')
    finally:journal.close()
    assert last in {r['event_id'] for r in map(json.loads,(tmp_path/'trace.jsonl').read_text().splitlines())}


def test_recovered_alignment_state_bug_becomes_engineering_issue_not_motion_program(tmp_path):
    # Inject the old stale flag only after the current engine has correctly
    # completed alignment; this is a fault reproduction, not a historical run.
    run=tmp_path/'synthetic-run';trace=run/'collected/owner/harness'
    e,b,*_=make_bridge(trace)
    try:
        original=e.pending
        e.pending=original.model_copy(update=dict(skill='align'))
        e.take_pending()
        e.complete(original.request_id,SkillResult(request_id=original.request_id,lifecycle='completed',
            outcome='success',evidence_ids=('test-fixture-alignment-proof',)))
        assert not e.needs_correction and 'align' in e.completed_skills
        e.needs_correction=True
        intent=original.model_copy(update=dict(skill='insert',request_id='fault-injected-insert'))
        result=e.submit_skill(intent.model_dump())
        assert result['lifecycle']=='rejected' and result['no_motion']
        assert result['reason']=='failed_insertion_requires_retreat_and_alignment'
        record=record_failures(run,tmp_path/'memory')
        facts=json.loads(Path(record['path']).read_text())
        fact=next(i for i in facts['incidents'] if i['kind']=='skill_admission_rejected')
        assert fact['preceding_result']['skill']=='align'
        assert fact['preceding_result']['outcome']=='success' and not fact['new_motion_inferred']
        review=review_failures(record,tmp_path/'memory')
        assert review['candidate_count']==0 and review['engineering_issue_count']==1
        assert Path(review['path']).with_suffix('.md').is_file()
        assert review_failures(record,tmp_path/'memory')==review
        assert not e.request_publications
    finally:b.close()


@pytest.mark.parametrize('ordering',['delayed','missing','equal','new_task'])
def test_optional_append_order_is_not_used_as_causal_order(tmp_path,ordering):
    run=tmp_path/'run';path=run/'collected/owner/harness/trace.jsonl';path.parent.mkdir(parents=True)
    early=dict(kind='skill_result',monotonic_ns=10,skill='align',event_id='early',
               result=dict(request_id='align',outcome='success'))
    late=dict(kind='skill_result',monotonic_ns=30,skill='reobserve',event_id='late',
              result=dict(request_id='later',outcome='success'))
    refused=dict(kind='skill_admission_rejected',monotonic_ns=20,
        schema='f1-admission-rejection-v1',operation='submit',request_id='insert',skill='insert',
        reason='failed_insertion_requires_retreat_and_alignment',no_new_motion=True,
        new_request_queued=False,request_had_publication=False,state={'needs_correction':True})
    rows=[early,late,refused]
    if ordering=='missing':early.pop('monotonic_ns')
    if ordering=='equal':early['monotonic_ns']=20
    if ordering=='new_task':rows.insert(1,dict(kind='task_prepared',monotonic_ns=15))
    path.write_text('\n'.join(map(json.dumps,rows)))
    record=record_failures(run,tmp_path/'memory')
    fact=json.loads(Path(record['path']).read_text())['incidents'][0]
    if ordering=='delayed':assert fact['preceding_result']['event_id']=='early'
    else:assert fact['preceding_result'] is None


@pytest.mark.parametrize('rich',['valid','invalid','missing'])
def test_legacy_receipt_deduplicates_only_valid_diagnostic(tmp_path,rich):
    run=tmp_path/'run';path=run/'collected/owner/harness/trace.jsonl';path.parent.mkdir(parents=True)
    legacy=dict(kind='skill_not_admitted',monotonic_ns=1,request_id='r',reason='stale_observation')
    detailed=dict(legacy,kind='skill_admission_rejected',schema='f1-admission-rejection-v1',
        operation='submit',no_new_motion=True,new_request_queued=False,request_had_publication=False,
        state={'needs_correction':False})
    if rich=='invalid':detailed['state']=None
    rows=[legacy]+([] if rich=='missing' else [detailed])
    path.write_text('\n'.join(map(json.dumps,rows)))
    record=record_failures(run,tmp_path/'memory')
    assert record['incident_count']==(2 if rich=='invalid' else 1)
    review=review_failures(record,tmp_path/'memory')
    assert review['candidate_count']==0
