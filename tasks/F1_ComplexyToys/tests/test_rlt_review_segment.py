from pathlib import Path
import sys
import json
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from rlt_review_segment import prepare, publish, sft_review
from test_rlt_priority import raw_episode


def test_uncertain_is_not_failure_and_review_history_preserves_raw(tmp_path):
    raw, receipt, _, _ = raw_episode(tmp_path)
    before = {str(p):p.read_bytes() for p in raw.rglob('*') if p.is_file()}
    decision = dict(reviewer='operator', reason='outcome uncertain', status='pending_review')
    first = publish(raw, decision, tmp_path/'reviews')
    assert first['review']['expert_intervals'] == []
    second = publish(raw, dict(decision, reason='reviewed again'), tmp_path/'reviews')
    assert second['review']['supersedes_sha256'] == first['revision']
    assert len(list((tmp_path/'reviews/history/e').glob('*.json'))) == 2
    assert before == {str(p):p.read_bytes() for p in raw.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='explicit confirmation'):
        prepare(raw, dict(decision, status='failure'))


def test_review_requires_real_local_endpoint_and_explicit_success(tmp_path):
    raw, receipt, streams, _ = raw_episode(tmp_path)
    decision = dict(reviewer='operator', reason='video reviewed', status='success')
    with pytest.raises(ValueError, match='confirmation'):prepare(raw, decision)
    result = receipt['local_result']
    decision.update(placement_release_confirmed=True,
                    terminal_observation_id=result['terminal_observation']['observation_id'])
    assert prepare(raw,decision)['status']=='success'
    decision['terminal_observation_id']=streams['observations'][0]['observation_id']
    with pytest.raises(ValueError, match='outside original'):prepare(raw,decision)


def test_same_approved_human_range_drives_sft_and_rejects_policy_range(tmp_path):
    raw, receipt, streams, _=raw_episode(tmp_path)
    (raw/'journal/manifest.json').write_text(json.dumps(dict(binding={},episode_id='e')))
    times=[r['monotonic_ns'] for r in streams['control']]
    decision=dict(reviewer='operator',reason='corrected placement',status='pending_review',
                  expert_intervals=[dict(start_ns=times[7],end_ns=times[11])])
    local=prepare(raw,decision)
    sft=sft_review(raw,local,source_group='scene1',split='train')
    assert sft['accepted_time_ranges_ns']==[[times[7],times[11]]]
    assert sft['accepted_interventions']==[1] and sft['human_review_accepted']
    decision['expert_intervals'][0]['start_ns']=times[2]
    with pytest.raises(ValueError,match='nonhuman'):prepare(raw,decision)
    decision['environment_assistance_ranges_ns']=[[times[7],times[8]]]
    with pytest.raises(ValueError,match='External object reset'):prepare(raw,decision)
