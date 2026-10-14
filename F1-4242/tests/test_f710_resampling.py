"""Fixed-grid F710 provenance survives the common candidate/release path."""
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAGGER = ROOT.parent / 'DAgger'
if not (DAGGER / 'F1/src/f1dagger').is_dir():
    pytest.skip('requires the DAgger development checkout', allow_module_level=True)
for rel in ('F1/src', 'G2/src', 'shared/src', 'F1/tests', 'G2/tests'):
    sys.path.insert(0, str(DAGGER / rel))

from test_f1_recording import raw_episode
from recording.unified.adapters import f710
from recording.unified.writer import export, review_template


def bundle(tmp_path):
    raw, _ = raw_episode(tmp_path, ['human_arm']*60, synthetic=True,
                         timestamps_ns=[i*34_100_000 for i in range(60)])
    return f710(raw, DAGGER)


def test_adapter_retains_causal_grid_provenance(tmp_path):
    data = bundle(tmp_path)
    assert len(data.segments) == 1
    assert any(r['observation_reused'] for r in data.segments[0])
    for row in data.segments[0]:
        assert row['time_ns']-row['source_observation_monotonic_ns'] == row['observation_age_ns']
        assert row['command_monotonic_ns'] <= row['time_ns']
        assert row['source_row'] >= 0
    assert data.metadata['selection']['training_ready'] is False


@pytest.mark.parametrize('changed', ['missing', 'wrong', 'different_segments'])
def test_old_or_changed_segment_review_cannot_approve_new_selection(tmp_path, changed):
    data = bundle(tmp_path)
    review = dict(review_template(data), decision='accepted', reviewer='test', task='test', accepted_segments=[0])
    if changed == 'missing':
        review.pop('selection_sha256')
    elif changed == 'wrong':
        review['selection_sha256'] = 'old'
    else:
        data.segments[0].pop()
    with pytest.raises(ValueError, match='another selection'):
        export(data, tmp_path/'must-not-publish', None, review=review)
    assert not (tmp_path/'must-not-publish').exists()


@pytest.mark.skipif(not os.environ.get('F1_TEST_LEROBOT_ROOT'), reason='requires pinned LeRobot environment')
def test_held_frames_and_50_step_actions_roundtrip_with_official_reader(tmp_path):
    import numpy as np
    data = bundle(tmp_path)
    out = tmp_path/'candidate'
    report = export(data, out, os.environ['F1_TEST_LEROBOT_ROOT'])
    assert report['reader_roundtrip_verified']
    assert not report['training_ready'] and not report['human_review_accepted']
    rows = [json.loads(line) for line in (out/'f1_source.jsonl').read_text().splitlines()]
    assert any(r['observation_reused'] for r in rows)
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    reader = LeRobotDataset(repo_id='local/f1_upper16', root=out, video_backend='pyav',
                             delta_timestamps={'action': [i/30 for i in range(50)]})
    sample = reader[0]
    np.testing.assert_allclose(sample['action'], [r['action'] for r in rows[:50]])
    assert not sample['action_is_pad'].any()
    tail = reader[len(rows)-1]
    assert tail['action_is_pad'][1:].all()
