import json

from f1_harness.evidence import EvidenceStore
from f1_harness.evidence_pack import inspect_pair
from test_evidence_pack import fixture


def test_post_task_manual_reset_does_not_change_completed_autonomy(tmp_path):
    planner,owner=fixture(tmp_path)
    rows=[dict(event_id='end',kind='authority_revoked',reason='task_finished',epoch=2),
          dict(event_id='reset',kind='manual_intervention'),
          dict(event_id='reset-revoke',kind='authority_revoked',reason='manual_takeover',epoch=3)]
    with (owner/'harness/trace.jsonl').open('a') as stream:
        for row in rows:stream.write(json.dumps(row)+'\n')
    store=EvidenceStore(planner)
    for row in rows:store.event('owner_event',record=row,owner_instance_id='instance')
    result=inspect_pair(planner,owner)
    assert result['integrity_complete'] and result['autonomous_success']
    assert not result['assisted']
