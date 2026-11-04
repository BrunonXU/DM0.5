"""Real-data admission contracts and fail-closed critic promotion checks."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
torch=pytest.importorskip('torch')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_critic_calibration import critic_report,load_terminals,combine_training_suffixes
from f1_toys.rlt_online import sha


def data(rewards):
    n=len(rewards)
    return dict(obs={'state':torch.zeros(n,2)},actions=torch.zeros(n,16,10),
        action_time_mask=torch.ones(n,16,dtype=torch.bool),elapsed_steps=torch.ones(n)*16,
        terminated=torch.ones(n,dtype=torch.bool),truncated=torch.zeros(n,dtype=torch.bool),
        reward=torch.tensor(rewards,dtype=torch.float))


def test_critic_gate_uses_both_heads_and_actual_outcomes():
    learner=SimpleNamespace(model=SimpleNamespace(_critic_state=lambda x:x['state']),
        critic=lambda *a:torch.tensor([[.9,.95],[.1,.2]]))
    assert critic_report(learner,data([1,0]))['passed']
    learner.critic=lambda *a:torch.tensor([[.9,.95],[.1,.4]])
    assert not critic_report(learner,data([1,0]))['passed']
    learner.critic=lambda *a:torch.tensor([[.9,float('nan')],[.1,.1]])
    assert not critic_report(learner,data([1,0]))['passed']
    assert not critic_report(learner,data([]))['passed']


def anchor(root):
    root.mkdir()
    for split in ('train','validation'):torch.save(data([0]),root/(split+'.pt'))
    manifest=dict(schema='f1_rlt_critic_terminals_v1',objective='approach_place_release_v1',
        feature_identity={'encoder':'fixed'},provenance={s:[{'episode':s}] for s in ('train','validation')},
        files_sha256={s+'.pt':sha(root/(s+'.pt')) for s in ('train','validation')})
    (root/'manifest.json').write_text(json.dumps(manifest))
    return manifest


def test_anchor_hashes_and_episode_split_are_mandatory(tmp_path):
    root=tmp_path/'anchor';meta=anchor(root)
    load_terminals(root,{'encoder':'fixed'})
    meta['provenance']['validation'][0]['episode']='train'
    (root/'manifest.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError,match='leakage'):load_terminals(root,{'encoder':'fixed'})
    meta['provenance']['validation'][0]['episode']='validation'
    (root/'manifest.json').write_text(json.dumps(meta))
    torch.save(data([1]),root/'train.pt')
    with pytest.raises(ValueError,match='changed'):load_terminals(root,{'encoder':'fixed'})


def test_suffixes_cannot_smuggle_validation_into_training(tmp_path):
    root=tmp_path/'anchor';anchor(root)
    suffix=tmp_path/'suffix';suffix.mkdir()
    meta=dict(schema='f1_local_replay_cache_v1',objective='approach_place_release_v1',
        learning_scope='critic_only_terminal_suffixes',split='train',feature_identity={'encoder':'fixed'},
        episode_id='validation',files_sha256={})
    (suffix/'manifest.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError,match='leakage'):
        combine_training_suffixes(root,[suffix],tmp_path/'combined',{'encoder':'fixed'})
    assert not (tmp_path/'combined').exists()
