import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import pytest
from f1_toys.rlt_prediction_index import source_predictions


def row(request=1):
    return dict(model_id='m',policy_epoch=3,request_id=request,observation_id=9,
                observation_monotonic_ns=10,success=True,prediction_replay=dict(z_rl=[1]))


def test_partial_append_is_pending_then_exact_source_is_retained(tmp_path):
    path=tmp_path/'log.jsonl';r=row();line=json.dumps(r)+'\n'
    path.write_text(line[:-2])
    with pytest.raises(ValueError,match='not all available'):source_predictions(path,[r])
    path.write_text(line+json.dumps(row(2))[:-2])
    values=list(source_predictions(path,[r]).values())
    assert len(values)==1 and values[0]['prediction_replay']==r['prediction_replay']
    import hashlib
    assert values[0]['_source_line_sha256']==hashlib.sha256(line.encode()).hexdigest()


def test_conflicting_identity_cannot_replace_original(tmp_path):
    path=tmp_path/'log.jsonl';r=row();changed=dict(r,prediction_replay=dict(z_rl=[2]))
    path.write_text(json.dumps(r)+'\n'+json.dumps(changed)+'\n')
    with pytest.raises(ValueError,match='Conflicting'):source_predictions(path,[r])


def test_service_rotation_keeps_both_original_reply_sources(tmp_path):
    old,new=tmp_path/'old.jsonl',tmp_path/'new.jsonl'
    old.write_text(json.dumps(row(1))+'\n')
    with pytest.raises(ValueError,match='not all available'):
        source_predictions([old,new],[row(1),row(2)])
    new.write_text(json.dumps(row(2))+'\n')
    assert len(source_predictions([old,new],[row(1),row(2)]))==2
    new.write_text(json.dumps(dict(row(1),prediction_replay={'z_rl':[2]}))+'\n')
    with pytest.raises(ValueError,match='Conflicting'):
        source_predictions([old,new],[row(1)])
