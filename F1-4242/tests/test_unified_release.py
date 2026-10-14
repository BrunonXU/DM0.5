import json
import pytest
from recording.unified.contract import sha256
from recording.unified.release import build


def dataset(tmp_path,name,session,offset=0):
    root=tmp_path/name;root.mkdir()
    (root/'meta').mkdir();(root/'meta/info.json').write_text('{}')
    (root/'f1_contract.json').write_text('{}');(root/'f1_review.json').write_text('{}')
    (root/'f1_audit.json').write_text(json.dumps(dict(training_ready=True,reader_roundtrip_verified=True,source={},frames=2)))
    rows=[dict(session_id=session,time_ns=offset+i,state=[offset+i]*16,action=[offset+i+1]*16) for i in range(2)]
    (root/'f1_source.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    files={str(p.relative_to(root)):sha256(p) for p in root.rglob('*') if p.is_file()}
    (root/'f1_files.json').write_text(json.dumps(dict(files=files)))
    return str(root)


def test_release_train_only_statistics_and_session_split(tmp_path):
    a=dataset(tmp_path,'a','s1');b=dataset(tmp_path,'b','s2',100)
    spec=dict(datasets=[dict(path=a,split='train'),dict(path=b,split='validation')])
    out=tmp_path/'release.json';r=build(spec,out)
    assert r['statistics']['state']['mean']==[.5]*16
    with pytest.raises(FileExistsError):build(spec,out)


def test_session_leakage_and_source_changes_refused(tmp_path):
    a=dataset(tmp_path,'a','same');b=dataset(tmp_path,'b','same',100)
    spec=dict(datasets=[dict(path=a,split='train'),dict(path=b,split='test')])
    with pytest.raises(ValueError,match='leakage'):build(spec,tmp_path/'bad.json')
    from pathlib import Path
    (Path(a)/'f1_source.jsonl').write_text('changed')
    with pytest.raises(ValueError,match='changed'):build(dict(datasets=[dict(path=a,split='train')]),tmp_path/'changed.json')


def test_held_observation_allowed_once_but_not_in_two_rephased_exports(tmp_path):
    from pathlib import Path
    a=dataset(tmp_path,'a','same',10);b=dataset(tmp_path,'b','same',20)
    for directory in (a,b):
        root=Path(directory)
        rows=[json.loads(line) for line in (root/'f1_source.jsonl').read_text().splitlines()]
        for row in rows:row['source_observation_monotonic_ns']=1
        (root/'f1_source.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        files=json.loads((root/'f1_files.json').read_text())
        files['files']['f1_source.jsonl']=sha256(root/'f1_source.jsonl')
        (root/'f1_files.json').write_text(json.dumps(files))
    build(dict(datasets=[dict(path=a,split='train')]),tmp_path/'ok.json')
    with pytest.raises(ValueError,match='duplicate source observation'):
        build(dict(datasets=[dict(path=a,split='train'),dict(path=b,split='train')]),tmp_path/'bad.json')
