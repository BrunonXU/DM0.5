"""Materialize local replay from immutable prefix receipts and frozen features."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import uuid

import numpy as np
import torch

from .rlt_local_replay import read_prefix, build_transitions, OBJECTIVE
from .rlt_online import atomic_json, sha
from .eef import solver, encode_actions, normalize_actions
from .rlt_priority import effective_result, read_frame, validate_bundle
from .rlt_action_contract import action_joints, SUBMITTED, REFERENCE, DECISION


def replay_adapter_identity():
    return {name: sha(Path(__file__).with_name(name)) for name in
            ('rlt_local_pipeline.py', 'rlt_local_replay.py', 'rlt_human_replay.py', 'rlt_priority.py',
             'rlt_action_contract.py', 'rlt_decision_replay.py', 'rlt_prefix.py', 'rlt_supervision.py', 'eef.py')}


def complete_lines(path):
    """A live append-only index can end in an incomplete, uncommitted line."""
    data = Path(path).read_bytes()
    return [json.loads(line) for line in data[:data.rfind(b'\n')+1].splitlines()]


class FrozenFeatureClient:
    def __init__(self, endpoint, identity, *, timeout=10.):
        self.endpoint, self.identity, self.timeout = endpoint, identity, timeout
        self.session = uuid.uuid4().hex

    def __call__(self, observation, payload, payload_root):
        from websockets.sync.client import connect
        frames, blobs = [], []
        root = Path(payload_root).resolve()
        for frame in payload['frames']:
            blob = read_frame(root, frame)
            blobs.append(blob)
            frames.append(dict(camera=frame['camera'], encoding=frame.get('media_type', 'image/jpeg'),
                               width=frame['width'], height=frame['height'], length=len(blob),
                               host_received_monotonic_ns=frame['host_received_monotonic_ns']))
        meta = dict(contract='f1_upper_16d_v1', protocol='f1_eef_rlt_sync_v1', session_id=self.session,
                    request_id=observation['observation_id'], observation_id=observation['observation_id'],
                    policy_epoch=observation['policy_epoch'], observation_monotonic_ns=observation['capture_started_monotonic_ns'],
                    state16=observation['state16'], last_submitted16=observation['state16'],
                    state_timestamps_ns=observation['state_timestamps_ns'],
                    chunk_steps=16, prediction_horizon=16, reference_horizon=50, execute_steps=16, frequency_hz=30.,
                    control_mode='rlt_hil_supervised', model_id=self.identity['model_id'],
                    model_version=self.identity['model_version'], frames=frames)
        if 'state_valid_mask' in observation:
            meta.update(state_valid_mask=observation['state_valid_mask'],
                        absent_state_semantics=observation['absent_state_semantics'],
                        state_timestamps_ns=[0 if value is None else value for value in observation['state_timestamps_ns']])
        raw = json.dumps(meta, allow_nan=False).encode()
        with connect(self.endpoint, open_timeout=self.timeout, close_timeout=.2,
                     max_size=4*1024**2, compression=None, proxy=None) as connection:
            connection.send(struct.pack('!I', len(raw))+raw+b''.join(blobs))
            result = json.loads(connection.recv(timeout=self.timeout))
        if (result.get('success') is not True or result.get('feature_only') is not True
                or result.get('observation_id') != observation['observation_id']
                or result.get('model_version') != self.identity['model_version']
                or any(result.get(k) != self.identity['rlt'][k]
                       for k in ('base_model_version', 'encoder_manifest_sha256'))):
            raise ValueError('Frozen feature request rejected: ' + str(result.get('error', result.get('reason'))))
        return result['features']


def prepare_local_cache(run, output_root, cfg, feature_provider, *, predictions=None):
    run = Path(run)
    bundle = None
    if (run/'learning-bundle.json').exists():
        bundle = validate_bundle(run)
    receipt_path = run/'local_segments/placement-1.json'
    receipt = json.loads(receipt_path.read_text())
    if receipt.get('binding', {}).get('collection_purpose') == 'evaluation':
        raise ValueError('Evaluation episodes cannot enter replay')
    source_digest = sha(receipt_path)
    split = 'validation' if receipt['episode_id'] in cfg.get('validation_episodes', []) else 'train'
    if cfg.get('data_policy'):
        from .rlt_data_policy import load_policy, episode_selection
        split = episode_selection(load_policy(cfg['data_policy'], cfg['data_policy_sha256']), receipt_path)
        if split not in ('train', 'validation'):
            raise ValueError('Episode is excluded or requires review')
    review = None
    review_digest = None
    if cfg.get('reviews_root'):
        review_path = Path(cfg['reviews_root'])/(receipt['episode_id']+'.json')
        if review_path.exists():
            review_digest = sha(review_path)
            review = json.loads(review_path.read_text())
            effective_result(receipt, source_digest, review)
    if bundle is not None and bundle['review_sha256'] != review_digest:
        raise ValueError('Learning bundle review revision differs from current approved review')
    recipe_digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    adapter = replay_adapter_identity()
    key = hashlib.sha256((source_digest+recipe_digest+str(review_digest)+json.dumps(adapter, sort_keys=True)).encode()).hexdigest()
    output = Path(output_root)/key
    if (output/'manifest.json').exists():
        return output
    expected = cfg['feature_identity']['rlt']
    binding = receipt['binding']
    if (binding.get('base_model_version') != expected['base_model_version']
            or binding.get('rlt', {}).get('encoder_manifest_sha256') != expected['encoder_manifest_sha256']):
        raise ValueError('Recorded frozen VLA/encoder differs from replay recipe')
    streams = read_prefix(run, receipt)
    result = effective_result(receipt, source_digest, review)
    alignment = None
    if cfg.get('action_representation') == DECISION:
        from .rlt_decision_replay import build_decision_transitions
        if predictions is None:
            raise ValueError('Decision replay requires source-bound server predictions')
        records, alignment = build_decision_transitions(streams,result,predictions,gamma=cfg['gamma_per_step'])
    else:
        records = build_transitions(streams, result, gamma=cfg['gamma_per_step'])
    if cfg.get('terminal_suffixes_only', False):
        from .rlt_local_replay import terminal_suffixes
        records = terminal_suffixes(streams, result, gamma=cfg['gamma_per_step'])
    from .rlt_supervision import approved_steps, CONTRACT
    for record in records:
        record['expert_approved'] = approved_steps(record, review)
    payload_root = run/'payload' if (run/'payload').is_dir() else run/'payload.partial'
    payload = {row['observation_id']: row for row in complete_lines(payload_root/'frames.jsonl')}
    observations = {row['observation_id']: row for row in streams['observations']}
    wanted = sorted({r[k] for r in records for k in ('observation_id', 'next_observation_id')})
    feature_map = {}
    original_features = {}
    original_prefixes = {}
    if alignment is not None:
        for prediction in predictions.values():
            oid=prediction['observation_id']
            if oid not in observations or prediction['observation_monotonic_ns'] != observations[oid]['capture_started_monotonic_ns']:
                continue
            if any(prediction.get('rlt',{}).get(k)!=expected[k] for k in
                   ('base_model_version','encoder_manifest_sha256')):
                raise ValueError('Original actor features belong to another frozen VLA/encoder')
            replay=prediction['prediction_replay']
            original_features[oid]=dict(z_rl=replay['z_rl'],proprio=replay['proprio'],
                                        ref_chunk=[a[:10] for a in replay['reference_normalized_actions32']])
            if replay.get('prefix_contract') == 'known_prefix_detached_v1':
                original_prefixes[oid] = (replay['committed_actions'], replay['committed_mask'], replay['decision_steps'])
    # Features already completed during a previous busy-server retry are reused
    # only under the receipt+recipe identity. They contain no robot commands.
    partial = output.with_name(output.name+'.partial')
    partial.mkdir(parents=True, exist_ok=True)
    for observation_id in wanted:
        cached = partial/f'feature-{observation_id}.json'
        if observation_id in original_features:
            feature_map[observation_id]=original_features[observation_id]
        elif cached.exists():
            feature_map[observation_id] = json.loads(cached.read_text())
        else:
            observation, images = observations[observation_id], payload[observation_id]
            if images['state16'] != observation['state16'] or images['available_monotonic_ns'] != observation['available_monotonic_ns']:
                raise ValueError('Payload differs from journal observation')
            feature_map[observation_id] = feature_provider(observation, images, payload_root)
            atomic_json(cached, feature_map[observation_id])
    shape = dict(z_rl=(2048,), proprio=(8,), ref_chunk=(50, 10))
    for feature in feature_map.values():
        if set(feature) != set(shape) or any(np.shape(feature[k]) != expected or not np.isfinite(feature[k]).all() for k, expected in shape.items()):
            raise ValueError('Frozen feature contract mismatch')
    model = solver().model
    actions = np.zeros((len(records), 16, 10), dtype=np.float32)
    for i, record in enumerate(records):
        if 'normalized_actions10' in record:
            actual=np.asarray(record['normalized_actions10'],dtype=np.float32)
            actions[i,:len(actual)]=actual
        else:
            contract=cfg.get('action_representation',SUBMITTED)
            actual = encode_actions(model, record['state16'], action_joints(record, REFERENCE if contract==DECISION else contract))
            actions[i, :len(actual)] = normalize_actions(actual, cfg['action_norm_stats'])[..., :10]
    data = dict(
        obs={k: torch.tensor(np.asarray([feature_map[r['observation_id']][k] for r in records]), dtype=torch.float32) for k in shape},
        next_obs={k: torch.tensor(np.asarray([feature_map[r['next_observation_id']][k] for r in records]), dtype=torch.float32) for k in shape},
        actions=torch.from_numpy(actions))
    for name, dtype in (('action_time_mask', torch.bool), ('intervene_flags', torch.bool), ('expert_approved', torch.bool),
                        ('terminated', torch.bool), ('truncated', torch.bool), ('reward', torch.float32),
                        ('discount', torch.float32), ('elapsed_steps', torch.float32)):
        data[name] = torch.tensor([r[name] for r in records], dtype=dtype)
    if alignment is not None:
        data['execution_fraction']=torch.tensor([r['execution_fraction'] for r in records],dtype=torch.float32)
    if original_prefixes:
        from .rlt_prefix import add_empty_prefix, with_prefix, planned_mask
        for field, obs_key in [('observation_id','obs'),('next_observation_id','next_obs')]:
            data[obs_key] = add_empty_prefix(data[obs_key])
            for i,r in enumerate(records):
                if r[field] in original_prefixes:
                    action, mask, steps = original_prefixes[r[field]]
                    data[obs_key]['committed_actions'][i] = torch.tensor(action)
                    data[obs_key]['committed_mask'][i] = torch.tensor(mask,dtype=torch.bool)
                    if type(steps) is not int:
                        raise ValueError('RTC decision horizon must be an integer')
                    data[obs_key]['decision_steps'][i] = steps
            with_prefix(data['actions'],data[obs_key])
            planned_mask(data[obs_key],data['action_time_mask'])
    torch.save(data, partial/'replay.pt')
    (partial/'transitions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    atomic_json(partial/'source-receipt.json', receipt)
    if review is not None:
        atomic_json(partial/'review.json', review)
    if alignment is not None:
        atomic_json(partial/'alignment.json', alignment)
        # Keep the original proposals alongside this derived cache. New online
        # episodes must remain reproducible after the service log rotates.
        used={r.get('server_prediction_sha256') for r in records if r['decision_kind']=='actor_request'}
        originals=[p for p in predictions.values() if p.get('_source_line_sha256') in used]
        (partial/'predictions.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in originals))
    atomic_json(partial/'manifest.json', dict(schema='f1_local_replay_cache_v1', objective=OBJECTIVE,
        supervision_contract=CONTRACT,
        learning_scope='critic_only_terminal_suffixes' if cfg.get('terminal_suffixes_only', False) else 'local_replay',
        source_receipt_sha256=source_digest, recipe_sha256=recipe_digest,
        adapter_sha256=adapter,
        action_representation=cfg.get('action_representation', SUBMITTED),
        decision_alignment=alignment,
        review_sha256=review_digest,
        local_result=result,
        episode_id=receipt['episode_id'], segment_id=receipt['local_result']['segment_id'],
        synthetic=bool(receipt['binding'].get('synthetic', False)),
        feature_identity=cfg['feature_identity'], transition_count=len(records),
        observation_run_count=len({r['observation_run'] for r in records}),
        quality_cut_count=sum(r['truncated'] for r in records),
        retained_seconds=sum(r['elapsed_steps']/30 for r in records),
        split=split,
        files_sha256={name: sha(partial/name) for name in ('replay.pt', 'transitions.jsonl', 'source-receipt.json', *(['review.json'] if review else []), *(['alignment.json','predictions.jsonl'] if alignment else []))}))
    os.replace(partial, output)
    return output
