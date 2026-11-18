import json
from f1_harness.failure_corpus import record_failures


def test_wait_without_terminal_result_is_retained_not_promoted(tmp_path):
    run=tmp_path/'run';path=run/'collected/owner/harness/trace.jsonl'
    path.parent.mkdir(parents=True)
    events=[dict(kind='harness_verification_wait',request_id='approach',
                 reason='fresh_post_motion_scene_missing',monotonic_ns=t) for t in (10,20,30)]
    path.write_text('\n'.join(json.dumps(e) for e in events))
    record=record_failures(run,tmp_path/'memory')
    assert record==record_failures(run,tmp_path/'memory')
    from pathlib import Path
    value=json.loads(Path(record['path']).read_text())
    assert value['executable'] is False
    assert value['incidents'][0]['count']==3
    assert not (tmp_path/'memory/corpus').exists()
