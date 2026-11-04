import copy
import json
import pytest

from scripts.import_rlt_training_reviews import build_projection, digest, encoded


def cache_fixture(*, manual=False, split='train'):
    episode = 'f1_hil_20261029T032541Z_c5a0c2bc'
    binding = dict(execution_mode='powered', purpose='policy_hil', rlt={'control_mode': 'rlt_hil_supervised'})
    terminal = dict(observation_id=17, available_monotonic_ns=12_000_000_000)
    local = dict(episode_id=episode, segment_id=episode + ':placement:1', event='rlt_local_closed',
                 objective='approach_place_release_v1', status='failure' if manual else 'success',
                 reward=0.0 if manual else 1.0, terminated=True, truncated=False, assisted=True,
                 started_ns=2_000_000_000, ended_ns=15_000_000_000,
                 terminal_observation=terminal, termination_reason='operator_handoff_without_success' if manual else 'human_confirmed')
    streams = {name: dict(bytes=100, sha256='a' * 64) for name in ('observations', 'policy_chunks', 'control', 'events')}
    receipt = dict(schema='f1_local_journal_prefix_v1', episode_id=episode, binding=binding,
                   streams=streams, local_result=local, drop_count=0, training_ready=False)
    receipt_sha = digest(encoded(receipt))
    review = None
    result = copy.deepcopy(local)
    files = {'source-receipt.json': digest(encoded(receipt, indent=2)), 'replay.pt': 'b' * 64, 'transitions.jsonl': 'c' * 64}
    if manual:
        review = dict(episode_id=episode, source_receipt_sha256=receipt_sha, objective='approach_place_release_v1',
                      approved=True, reviewer='onsite_operator', status='success', assisted=True,
                      placement_release_confirmed=True, terminal_observation=terminal)
        result.update(status='success', reward=1.0)
        files['review.json'] = digest(encoded(review, indent=2))
    m = dict(schema='f1_local_replay_cache_v1', learning_scope='local_replay', synthetic=False,
             objective='approach_place_release_v1', episode_id=episode, segment_id=local['segment_id'],
             source_receipt_sha256=receipt_sha, recipe_sha256='d' * 64, adapter_sha256={'adapter.py': 'e' * 64},
             review_sha256=digest(encoded(review, indent=2)) if review else None,
             local_result=result, split=split, transition_count=13, retained_seconds=8.5, files_sha256=files)
    key = digest((receipt_sha + m['recipe_sha256'] + str(m['review_sha256']) + json.dumps(m['adapter_sha256'], sort_keys=True)).encode())
    root = '/cache/' + key
    item = dict(path=root + '/manifest.json', manifest=m, source_receipt=receipt, review=review, mtime=1793245920.0)
    row = dict(id='1' * 24, run_id=episode, path='/robot/raw/' + episode, capture_mode='rlt', binding=binding,
               metadata_sha256='f' * 64, source_signature='0' * 64,
               files=[dict(relative_path='journal/' + name + '.jsonl', size_bytes=200) for name in streams])
    config = dict(f1=dict(seed_caches=[root], data_policy='/selected/policy.json'))
    policy = dict(schema='f1_rlt_data_policy_v1', default='quarantine',
                  episodes={episode: dict(split=split, reason='previously reviewed', source_receipt_sha256=receipt_sha)})
    return dict(episodes=[row]), [item], config, policy


@pytest.mark.parametrize('manual', [False, True])
def test_projected_review_preserves_explicit_source_and_awaits_remote_verification(manual):
    catalog, snapshot, config, policy = cache_fixture(manual=manual)
    before = copy.deepcopy((catalog, snapshot, config, policy))
    output = build_projection(catalog, snapshot, config, policy)
    row, = output['reviews']
    review = row['review']
    assert review['confirmation_kind'] == ('manual_confirmation' if manual else 'live_success')
    assert review['outcome'] == 'assisted_success'
    assert review['transition_count'] == 13 and review['retained_seconds'] == 8.5
    assert review['metadata_sha256'] == catalog['episodes'][0]['metadata_sha256']
    assert review['source_signature'] == catalog['episodes'][0]['source_signature']
    assert review['requires_remote_prefix_verification'] and not review['requires_remote_policy_verification']
    assert not output['save_permitted'] and not output['gradient_updates_verified']
    assert row['verification']['streams'] == snapshot[0]['source_receipt']['streams']
    assert (catalog, snapshot, config, policy) == before


def test_old_cache_for_same_source_is_ignored_unless_explicitly_selected():
    catalog, snapshot, config, policy = cache_fixture()
    old = copy.deepcopy(snapshot[0])
    old['path'] = '/old-cache/' + '9' * 64 + '/manifest.json'
    old['manifest']['transition_count'] = 99999
    output = build_projection(catalog, snapshot + [old], config, policy)
    assert output['ignored_cache_count'] == 1 and output['totals'] == {'train': 13, 'validation': 0}
    config['f1']['seed_caches'].append(old['path'].rsplit('/', 1)[0])
    with pytest.raises(ValueError, match='duplicate a source'):
        build_projection(catalog, snapshot + [old], config, policy)


def test_missing_policy_keeps_required_remote_gate_and_validation_stays_separate():
    catalog, snapshot, config, _ = cache_fixture(split='validation')
    output = build_projection(catalog, snapshot, config)
    assert output['totals'] == {'train': 0, 'validation': 13}
    assert output['reviews'][0]['review']['requires_remote_policy_verification']
    assert not output['policy_content_verified']


@pytest.mark.parametrize('mutation', ['synthetic', 'receipt', 'cached_receipt', 'review', 'result',
                                      'catalog_source', 'launch', 'prefix_size', 'split', 'key'])
def test_identity_and_admission_mismatches_rejected(mutation):
    catalog, snapshot, config, policy = cache_fixture(manual=True)
    item, = snapshot
    if mutation == 'synthetic':
        item['manifest']['synthetic'] = True
    elif mutation == 'receipt':
        item['source_receipt']['drop_count'] = 1
    elif mutation == 'cached_receipt':
        item['manifest']['files_sha256']['source-receipt.json'] = '1' * 64
    elif mutation == 'review':
        item['review']['approved'] = False
    elif mutation == 'result':
        item['manifest']['local_result']['terminal_observation']['observation_id'] = 999
    elif mutation == 'catalog_source':
        catalog['episodes'][0]['metadata_sha256'] = 'bad'
    elif mutation == 'launch':
        catalog['episodes'][0]['binding'] = {'execution_mode': 'shadow'}
    elif mutation == 'prefix_size':
        catalog['episodes'][0]['files'][0]['size_bytes'] = 1
    elif mutation == 'split':
        policy['episodes'][item['manifest']['episode_id']]['split'] = 'exclude'
    elif mutation == 'key':
        item['manifest']['recipe_sha256'] = '1' * 64
    with pytest.raises(ValueError):
        build_projection(catalog, snapshot, config, policy)


def test_missing_selected_cache_and_no_implicit_discovery():
    catalog, snapshot, config, policy = cache_fixture()
    with pytest.raises(ValueError, match='missing from replay snapshot'):
        build_projection(catalog, [], config, policy)
    config['f1']['seed_caches'] = []
    with pytest.raises(ValueError, match='explicit seed_caches'):
        build_projection(catalog, snapshot, config, policy)


def test_import_output_is_not_directly_saveable_by_review_writer():
    from data_rlt_assessment import _review_error
    catalog, snapshot, config, policy = cache_fixture()
    review = build_projection(catalog, snapshot, config, policy)['reviews'][0]['review']
    error = _review_error(review, catalog['episodes'][0])
    assert error and '尚未完成核验' in error[1]
