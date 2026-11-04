"""Immutable priority transfer preserves journal transitions and image bytes."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_priority import build_bundle, digest, validate_bundle, read_frame
from f1_toys.rlt_local_replay import build_transitions, read_prefix
from test_rlt_local_replay import fixture
from test_rlt_decision_replay import decision_fixture


def raw_episode(tmp_path):
    run = tmp_path/'raw'/'e'
    (run/'journal').mkdir(parents=True)
    (run/'payload').mkdir()
    (run/'local_segments').mkdir()
    streams, result = fixture(assisted=True)
    streams.update(events=[result], policy_chunks=[])
    receipt = dict(schema='f1_local_journal_prefix_v1',drop_count=0,episode_id='e',
                   binding={'execution_mode':'powered','recording_boundary':'f1_rlt_local_v2'},
                   local_result=result,streams={})
    for name, rows in streams.items():
        for i, row in enumerate(rows):row.update(episode_id='e',stream_sequence=i)
        data=b''.join(json.dumps(r).encode()+b'\n' for r in rows)
        (run/'journal'/(name+'.jsonl')).write_bytes(data)
        receipt['streams'][name]={'bytes':len(data),'sha256':digest(data)}
    (run/'local_segments/placement-1.json').write_text(json.dumps(receipt))
    index=[];pack=b'original pack prefix not requested'
    for obs in streams['observations']:
        blob=('synthetic-image-'+str(obs['observation_id'])).encode()
        frame=dict(camera='head',path='head.pack',payload_offset=len(pack),payload_bytes=len(blob),payload_sha256=digest(blob))
        index.append(dict(observation_id=obs['observation_id'],state16=obs['state16'],available_monotonic_ns=obs['available_monotonic_ns'],frames=[frame]))
        pack+=blob
    (run/'payload/head.pack').write_bytes(pack)
    (run/'payload/frames.jsonl').write_bytes(b''.join(json.dumps(r).encode()+b'\n' for r in index)+b'{"uncommitted":')
    return run,receipt,streams,index


def test_priority_preserves_transitions_and_original_image_bytes(tmp_path):
    raw,receipt,streams,index=raw_episode(tmp_path)
    before={str(p):p.read_bytes() for p in raw.rglob('*') if p.is_file()}
    bundle=build_bundle(raw,tmp_path/'priority');m=validate_bundle(bundle)
    assert m['archive_complete'] is False
    assert build_transitions(read_prefix(bundle,receipt),receipt['local_result']) == build_transitions(streams,receipt['local_result'])
    assert len(m['observation_ids']) < len(index)
    assert not (bundle/'payload/head.pack').exists()
    for row in index:
        if row['observation_id'] in m['observation_ids']:
            for frame in row['frames']:
                assert read_frame(bundle/'payload',frame)==read_frame(raw/'payload',frame)
    assert before == {str(p):p.read_bytes() for p in raw.rglob('*') if p.is_file()}
    assert build_bundle(raw,tmp_path/'priority')==bundle


@pytest.mark.parametrize('fault',['image','receipt','partial','path'])
def test_incomplete_or_corrupt_bundle_is_rejected(tmp_path,fault):
    raw,_,_,_=raw_episode(tmp_path);bundle=build_bundle(raw,tmp_path/'priority')
    if fault=='image':next((bundle/'payload/blobs').iterdir()).write_bytes(b'corrupt')
    if fault=='receipt':(bundle/'local_segments/placement-1.json').write_text('{}')
    if fault=='partial':next((bundle/'payload/blobs').iterdir()).unlink()
    if fault=='path':
        p=bundle/'learning-bundle.json';m=json.loads(p.read_text());m['files_sha256']['../escaped']='a'*64;p.write_text(json.dumps(m))
    with pytest.raises((ValueError,FileNotFoundError)):validate_bundle(bundle)


def test_missing_original_image_never_commits_bundle(tmp_path):
    raw,_,_,_=raw_episode(tmp_path);(raw/'payload/head.pack').write_bytes(b'')
    with pytest.raises(ValueError,match='not yet complete or corrupt'):build_bundle(raw,tmp_path/'priority')
    assert not list((tmp_path/'priority').iterdir())


def test_priority_includes_original_actor_query_outside_legacy_grid(tmp_path):
    raw, receipt, _, _ = raw_episode(tmp_path)
    streams, result, _ = decision_fixture()
    streams['events'].append(result)
    receipt['local_result']=result
    obs = streams['observations'][1]
    for plan in (streams['events'][0]['plan'], streams['policy_chunks'][0]):
        plan.update(observation_id=obs['observation_id'],
                    observation_monotonic_ns=obs['available_monotonic_ns'])
    for name, rows in streams.items():
        for i, row in enumerate(rows):row.update(episode_id='e', stream_sequence=i)
        data=b''.join(json.dumps(r).encode()+b'\n' for r in rows)
        (raw/'journal'/(name+'.jsonl')).write_bytes(data)
        receipt['streams'][name]={'bytes':len(data),'sha256':digest(data)}
    (raw/'local_segments/placement-1.json').write_text(json.dumps(receipt))
    old = {r[k] for r in build_transitions(streams,result) for k in ('observation_id','next_observation_id')}
    assert obs['observation_id'] not in old
    manifest=validate_bundle(build_bundle(raw,tmp_path/'priority'))
    assert obs['observation_id'] in manifest['observation_ids']
    assert old <= set(manifest['observation_ids'])
