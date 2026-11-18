from pathlib import Path
import json
import pytest
from f1_harness.failure_corpus import record_failures
from f1_harness.learning_review import review_failures,learning_snapshot


def source(root,name,request='move'):
    run=root/name;p=run/'collected/owner/harness/trace.jsonl';p.parent.mkdir(parents=True)
    rows=[dict(kind='skill_accepted',monotonic_ns=0,intent={'request_id':request,'skill':'approach_grasp'}),
          dict(kind='harness_verification_wait',request_id=request,reason='fresh_post_motion_scene_missing',after_ns=0,monotonic_ns=1),
          dict(kind='harness_verification_wait',request_id=request,reason='fresh_post_motion_scene_missing',after_ns=0,monotonic_ns=2),
          dict(kind='skill_result',skill='grasp',monotonic_ns=3,result=dict(request_id='g',outcome='failure',reason='unknown_physical_fault'))]
    p.write_text('\n'.join(map(json.dumps,rows)))
    return run


def test_each_failure_becomes_bound_program_candidate_or_engineering_issue_not_live_memory(tmp_path):
    root=tmp_path/'memory';run=source(tmp_path,'run')
    facts=record_failures(run,root);review=review_failures(facts,root)
    report=json.loads(Path(review['path']).read_text())
    assert report['promoted'] is False and report['execution_changed'] is False
    assert len(report['candidates'])==1 and len(report['engineering_issues'])==1
    candidate=json.loads(Path(report['candidates'][0]['path']).read_text())
    assert candidate['incidents'][0]['count']==2 and candidate['incidents'][0]['skill']=='approach_grasp'
    assert candidate['program']['operation']=='recheck_same_skill'
    assert candidate['execution_enabled'] is False
    assert not (root/'corpus').exists()
    assert review_failures(facts,root)==review
    one=learning_snapshot(root)
    assert json.loads(Path(one['path']).read_text())['strategies'][0]['independent_source_count']==1
    run2=source(tmp_path,'run2','different-request')
    review_failures(record_failures(run2,root),root)
    two=learning_snapshot(root)
    assert json.loads(Path(two['path']).read_text())['strategies'][0]['independent_source_count']==2


def test_mutated_candidate_is_not_consumed_as_diagnostic_or_executable_snapshot(tmp_path):
    facts=record_failures(source(tmp_path,'run'),tmp_path/'memory')
    review=review_failures(facts,tmp_path/'memory')
    p=Path(json.loads(Path(review['path']).read_text())['candidates'][0]['path'])
    value=json.loads(p.read_text());value['program']['operation']='publish';p.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='identity_changed'):learning_snapshot(tmp_path/'memory')


def test_run_review_creates_quarantined_lessons_and_reuses_unchanged_owner_source(tmp_path):
    from f1_harness.run_review import summarize_run
    run=source(tmp_path,'run');memory=tmp_path/'memory'
    # No successful placement in this owner trace; no execution corpus promotion.
    result=summarize_run(run,memory)
    assert result['learning_review']['candidate_count']==1
    assert result['learning_review']['diagnostic_snapshot']['executable'] is False
    assert result['candidate_ids']==[] and not result['promoted']
    assert summarize_run(run,memory)==result
    assert len(list((memory/'learning').glob('*.candidate.json')))==1
