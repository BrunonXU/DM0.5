from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import pytest

from f1dagger.recording import F1Recorder, make_sink, audit, read_rows
from f1dagger.review import build_review
from f1dagger.payload_archive import read_payload
from g2dagger.recording_sinks import EpisodeVideoSink
from test_f1_policy import observation


def episode(tmp_path, storage, *, shadow=False, missing=False):
    run = tmp_path/'raw'; run.mkdir()
    recorder = F1Recorder(run/'journal', episode_id='review-test', task_prompt='pick </script><script>alert(1)</script>',
                          binding={'execution_mode': 'shadow' if shadow else 'powered'})
    sink = make_sink(run/'payload', 'hil-record') if storage == 'pack' else EpisodeVideoSink(run/'payload')
    for i, offset in enumerate([0, 100_000_000, 350_000_000]):
        obs = observation(i, 10**17+offset)
        if not (missing and i == 2): assert sink.submit(obs)
        assert recorder.record_timestamped_observation(obs, hil_state='hil_latched', intervention_id=1, policy_epoch=2)
        # First observation deliberately precedes any command. The next two
        # have distinct actions, never fabricated from all-zero feedback.
        if i:
            t = obs.available_monotonic_ns-1
            target = np.zeros(16); target[0] = i*7
            pubs = [] if shadow else [{'topic_key': k, 'monotonic_ns': t-1, 'publisher_gid': 'aa'*16}
                                     for k in ['joint_command', 'left_gripper_command', 'right_gripper_command']]
            assert recorder.control(now=t, state='hil_latched', intervention=1, epoch=2,
                selected=target, submitted=None if shadow else target, source='human_arm' if i == 1 else 'home',
                measured=np.zeros(16), sequence=i, execution_mode='shadow' if shadow else 'powered',
                publication_complete=not shadow, publications=pubs,
                action_source_timestamps_ns=[] if shadow else [t-1]*16)
    assert sink.close(); assert recorder.close(); assert audit(run)['valid']
    return run


def tree_hash(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('storage', ['pack', 'legacy'])
def test_review_extracts_original_bytes_and_receipt_timeline_without_approval(tmp_path, storage):
    run = episode(tmp_path, storage); original = tree_hash(run); out = tmp_path/'review'
    result = build_review(run, out)
    assert result['images'] == 9 and not result['training_ready'] and result['accepted_interventions'] == []
    assert tree_hash(run) == original and not (tmp_path/'review.partial').exists()
    data = json.loads((out/'review_data.json').read_text()); rows = data['observations']
    assert [r['elapsed_ms'] for r in rows] == [0, 100, 350]
    assert rows[0]['reference_monotonic_ns'] == str(10**17+1)  # Beyond JavaScript exact integer range.
    assert rows[0]['control'] is None and rows[0]['action16'] is None
    assert rows[1]['action16'][0] == 7 and rows[1]['state16'][0] == 0
    assert rows[2]['control']['source'] == 'home'
    archived = read_rows(run/'payload/frames.jsonl')
    for row, raw in zip(rows, archived):
        for frame, stored in zip(row['frames'], raw['frames']):
            assert (out/frame['extracted_path']).read_bytes() == read_payload(run/'payload', stored)
    html = (out/'index.html').read_text()
    assert 'pick </script><script>alert(1)</script>' not in html
    embedded = re.search(r'<script id="review-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert json.loads(embedded) == data
    assert (out/'control.jsonl').read_bytes() == (run/'journal/control.jsonl').read_bytes()


def test_shadow_and_missing_payload_are_explicit(tmp_path):
    run = episode(tmp_path, 'pack', shadow=True, missing=True)
    build_review(run, tmp_path/'review')
    data = json.loads((tmp_path/'review/review_data.json').read_text())
    assert any('SHADOW' in n for n in data['notices'])
    assert any('未保存图片' in n for n in data['notices'])
    assert all(r['action16'] is None for r in data['observations'])
    assert all(f['extracted_path'] is None for f in data['observations'][-1]['frames'])
    assert data['image_count'] == 6 and data['accepted_interventions'] == []


def test_review_refuses_raw_output_and_existing_destinations(tmp_path):
    run = episode(tmp_path, 'pack')
    with pytest.raises(ValueError, match='immutable'): build_review(run, run/'review')
    out = tmp_path/'review'; out.mkdir()
    with pytest.raises(FileExistsError): build_review(run, out)
    partial = tmp_path/'other.partial'; partial.mkdir()
    with pytest.raises(FileExistsError): build_review(run, tmp_path/'other')


def test_invalid_raw_never_creates_review(tmp_path):
    run = episode(tmp_path, 'pack')
    with (run/'payload/head.pack').open('ab') as handle: handle.write(b'bad')
    with pytest.raises(ValueError, match='audit failed'): build_review(run, tmp_path/'out')
    assert not (tmp_path/'out').exists() and not (tmp_path/'out.partial').exists()


def test_extraction_failure_keeps_partial_and_source_unchanged(tmp_path, monkeypatch):
    run = episode(tmp_path, 'pack'); before = tree_hash(run)
    def fail(*_args): raise OSError('synthetic output failure')
    monkeypatch.setattr('f1dagger.review.read_payload', fail)
    with pytest.raises(OSError): build_review(run, tmp_path/'out')
    assert not (tmp_path/'out').exists() and (tmp_path/'out.partial').is_dir()
    assert before == tree_hash(run)
