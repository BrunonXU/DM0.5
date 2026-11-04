"""Online trial contracts: diagnostic gates, seeded sampling and demo lineage."""
import copy
import json
import os
from pathlib import Path
import sys

import pytest
torch = pytest.importorskip('torch')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_evaluation import supervised_online_checks
from f1_toys.rlt_sampling import sampling_contract, sample_actor
from f1_toys.rlt_stage2 import make_policy
from f1_toys.rlt_online import sha
from f1_toys.rlt_demo_seed import load_demo_seed


def test_trial_diagnostics_do_not_require_expert_imitation_or_perfect_q():
    report = dict(checks={'human_fit': False, 'current_critic_calibrated': False},
        candidate=dict(finite=True, human_mse=10., release_steps=1, hold_steps=1),
        critic={'checks': {'finite': True}}, kinematics={'passed': True})
    updated = supervised_online_checks(report)
    assert updated['passed'] and not updated['diagnostic_checks']['human_fit']
    assert updated['scope'].startswith('supervised_trial')
    assert 'diagnostic_checks' not in report
    report['kinematics']['passed'] = False
    assert not supervised_online_checks(report)['passed']
    report['kinematics']['passed'] = True
    report['candidate']['human_mse'] = float('nan')
    assert not supervised_online_checks(report)['passed']


def test_sampling_is_reproducible_isolated_and_nonzero():
    core = os.environ.get('F1_RLT_CORE', str(Path(__file__).resolve().parents[3]/'RLinf'))
    model = make_policy(core, dict(z_dim=4, proprio_dim=2, action_dim=10,
        chunk_len=2, ref_chunk_len=3, fixed_std=.002))
    obs = dict(z_rl=torch.zeros(1, 4), proprio=torch.zeros(1, 2), ref_chunk=torch.zeros(1, 3, 10))
    contract = dict(schema='f1_rlt_sampling_v1', mode='fixed_gaussian', fixed_std=.002)
    rng = torch.get_rng_state().clone()
    a, seed = sample_actor(model, obs, contract, seed=8)
    b, _ = sample_actor(model, obs, contract, seed=8)
    c, _ = sample_actor(model, obs, contract, seed=9)
    assert seed == 8 and torch.equal(a, b) and not torch.equal(a, c)
    assert torch.equal(torch.get_rng_state(), rng)
    deterministic, no_seed = sample_actor(model, obs, None)
    assert no_seed is None
    torch.testing.assert_close(deterministic, model.sac_forward(obs, deterministic=True)[0], rtol=0, atol=0)
    with pytest.raises(ValueError, match='scale'):
        sample_actor(model, obs, dict(contract, fixed_std=.1), seed=8)


def test_sampling_contract_rejects_unknown_mode_and_scale():
    head = dict(model_config={'fixed_std': .002})
    assert sampling_contract(head) is None
    head['actor_sampling'] = dict(schema='f1_rlt_sampling_v1', mode='fixed_gaussian', fixed_std=.002)
    assert sampling_contract(head)['mode'] == 'fixed_gaussian'
    for key, value in [('fixed_std', float('nan')), ('fixed_std', .1), ('mode', 'anything')]:
        changed = copy.deepcopy(head); changed['actor_sampling'][key] = value
        with pytest.raises(ValueError): sampling_contract(changed)


def seed_fixture(tmp_path):
    root, audit = tmp_path/'seed', tmp_path/'audit'
    root.mkdir(); audit.mkdir()
    rows = []
    for split in ('train', 'validation'):
        part = dict(actions=torch.zeros(1,16,10), action_time_mask=torch.ones(1,16,dtype=torch.bool),
            intervene_flags=torch.ones(1,16,dtype=torch.bool), terminated=torch.ones(1,dtype=torch.bool),
            reward=torch.ones(1))
        torch.save(part, root/(split+'.pt'))
        rows.append(dict(split=split, source_run=split, source_group=split, terminated=True, rewards=[1.]))
    (audit/'transitions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (audit/'audit.json').write_text(json.dumps(dict(files_sha256={'transitions.jsonl':sha(audit/'transitions.jsonl')})))
    manifest = dict(schema='f1_rlt_reviewed_demo_seed_tensors_v1', objective='approach_place_release_v1',
        capture_mode='DAgger', learning_use='RLT_demo_seed', feature_identity={'frozen':'same'},
        audit_sha256=sha(audit/'audit.json'), counts={'train':1,'validation':1},
        files_sha256={s+'.pt':sha(root/(s+'.pt')) for s in ('train','validation')})
    (root/'manifest.json').write_text(json.dumps(manifest))
    return dict(root=str(root),audit_root=str(audit),manifest_sha256=sha(root/'manifest.json'))


def test_demo_source_bound_and_not_relabelled(tmp_path):
    config = seed_fixture(tmp_path)
    data, records, manifest = load_demo_seed(config, {'frozen':'same'})
    assert len(data['train']['actions']) == 1 and manifest['capture_mode'] == 'DAgger'
    with pytest.raises(ValueError, match='identity'):
        load_demo_seed(config, {'frozen':'different'})
    (Path(config['root'])/'train.pt').write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='checksum'):
        load_demo_seed(config, {'frozen':'same'})
