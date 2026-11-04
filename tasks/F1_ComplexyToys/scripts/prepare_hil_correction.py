"""Review-bound raw correction -> real F1 reader input; never starts training.

The baseline statistics are used only for reader acceptance, not fitted on a
held-out correction. A future mixed training release must fit train-only stats.
"""
import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image

TASK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TASK/'src'), str(TASK/'DataPrehandle')]
from build_training_index import Config, build_index
from f1_toys.eef import encode_actions, solver
from f1_toys.processing import active_cameras, load_contract, sha256


def prepare(raw, review_path, baseline, output, *, contract_path=None):
    raw, output = raw.resolve(), output.resolve()
    review = json.loads(review_path.read_text())
    raw_manifest = json.loads((raw/'journal/manifest.json').read_text())
    if raw_manifest.get('binding', {}).get('collection_purpose') == 'evaluation':
        raise ValueError('Evaluation episodes cannot enter SFT')
    synthetic = raw_manifest.get('binding', {}).get('synthetic') is True
    if review.get('dataset_role') not in ('original_demo', 'new_demo', 'correction'):
        raise ValueError('Explicit dataset_role required')
    if not review.get('source_group'):
        raise ValueError('Original session/scene source_group required')
    if review.get('split') not in ('train', 'validation'):
        raise ValueError('Explicit prospective episode split required')
    accepted = review['accepted_time_ranges_ns']
    if 'expert_intervals' in review:
        approved = [[s['start_ns'], s['end_ns']] for s in review['expert_intervals']]
        if not review.get('reviewer') or accepted != approved:
            raise ValueError('SFT ranges must match the independently approved expert intervals')
        local = raw/'local_segments/placement-1.json'
        if not local.exists() or sha256(local) != review.get('source_receipt_sha256'):
            raise ValueError('Expert SFT approval is not bound to the local source')
    for a, b in review.get('environment_assistance_ranges_ns', []):
        if any(a <= y and b >= x for x, y in accepted):
            raise ValueError('Environment assistance overlaps expert approval')
    partial = output.with_name(output.name+'.partial')
    if output.exists() or partial.exists() or raw == output or raw in output.parents:
        raise ValueError('Use a new output outside immutable raw data')
    cfg = load_contract(baseline/'contract.json')
    baseline_manifest = json.loads((baseline/'manifest.json').read_text())
    for name in ('contract.json', 'norm_stats.json'):
        if sha256(baseline/name) != baseline_manifest['files_sha256'][name]:
            raise ValueError('Baseline identity changed')
    if contract_path is not None:
        replacement = load_contract(contract_path)
        for key in ('schema', 'fps', 'horizon', 'kinematics_sha256', 'prompt'):
            if replacement[key] != cfg[key]:
                raise ValueError('Reader input migration cannot change action/task semantics: '+key)
        cfg = replacement
    if raw_manifest.get('schema_version') == 4 and cfg.get('input_version') != 'right_only_v1':
        raise ValueError('Missing-left-gripper recordings require the right-only model contract')
    partial.mkdir(parents=True)
    home = review.get('home_review')
    report = build_index(raw, partial/'index', mode='reviewed', review_path=review_path,
        config=Config(required_cameras=active_cameras(cfg), allow_partial_windows=True), home_review=home)
    rows = [json.loads(s) for s in (partial/'index/anchors.jsonl').read_text().splitlines()]
    indexed_count = len(rows)
    if review.get('anchor_time_ranges_ns') is not None:
        # Anchor eligibility is independent of the approved label timeline:
        # never truncate valid future labels at the end of the anchor range.
        ranges = review['anchor_time_ranges_ns']
        if not ranges or any(len(r) != 2 or r[0] > r[1] for r in ranges):
            raise ValueError('Invalid anchor eligibility ranges')
        rows = [r for r in rows if any(a <= r['anchor_monotonic_ns'] <= b for a,b in ranges)]
    if not rows:
        raise ValueError('No reviewed valid published-action windows')
    model = solver().model
    if model.config_sha256 != cfg['kinematics_sha256']:
        raise ValueError('Native kinematics changed')
    # Overlapping H50 windows repeatedly encode identical recorded targets.
    # Cache exact joint tuples only; never quantize or approximate kinematics.
    fk, cache = model.fk, {}
    def cached_fk(arm, q):
        key = (arm, tuple(q))
        if key not in cache:
            cache[key] = fk(arm, q)
        return cache[key]
    model.fk = cached_fk
    states, actions, joints, masks, samples = [], [], [], [], []
    for row in rows:
        images = {}
        for frame in row['frames']:
            if frame['camera'] not in active_cameras(cfg):
                continue
            name = f"images/{frame['camera']}/{frame['payload_sha256']}.jpg"
            images[frame['camera']] = name
            target = partial/name
            if not target.exists():
                source = (raw/'payload'/frame['path']).resolve()
                if (raw/'payload').resolve() not in source.parents:
                    raise ValueError('Payload escapes raw root')
                with source.open('rb') as stream:
                    stream.seek(frame['payload_offset']); data = stream.read(frame['payload_bytes'])
                if hashlib.sha256(data).hexdigest() != frame['payload_sha256']:
                    raise ValueError('Image hash mismatch')
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    expected = tuple(cfg['head_resolution']) if frame['camera'] == 'head' else (640,480)
                    if image.mode != 'RGB' or image.size != expected:
                        raise ValueError('Source image geometry changed')
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        state = np.asarray(row['state'], np.float32)
        submitted = np.asarray([a['action'] for a in row['action_window']], np.float32)
        length = len(submitted); mask = np.arange(50) < length
        encoded, padded = np.zeros((50,10), np.float32), np.zeros((50,16), np.float32)
        encoded[:length] = encode_actions(model, state, submitted); padded[:length] = submitted
        states.append(state); actions.append(encoded); joints.append(padded); masks.append(mask)
        phases = [p['stage'] for p in review.get('phase_ranges', [])
                  if p['start_ns'] <= row['anchor_monotonic_ns'] <= p['end_ns']]
        if len(phases) != 1:
            raise ValueError('Every selected anchor needs exactly one reviewed phase')
        samples.append(dict(episode_id=row['episode_id'], observation_id=row['observation_id'],
            state_valid_mask=row['state_valid_mask'],
            action_valid_masks=[a['action_valid_mask'] for a in row['action_window']],
            anchor_monotonic_ns=row['anchor_monotonic_ns'], images=images,
            action_grid_ns=[a['grid_monotonic_ns'] for a in row['action_window']],
            action_control_sequences=[a['control_stream_sequence'] for a in row['action_window']],
            action_sources=[a['source'] for a in row['action_window']], valid_action_steps=length,
            supervision_phase=phases[0], policy_epoch=row['policy_epoch'],
            intervention_id=row['intervention_id'], truncation_reason=row.get('truncation_reason')))
    hold = review.get('terminal_hold_range_ns')
    terminal = None
    if hold is not None:
        if hold[1]-hold[0] < 2_000_000_000 or review.get('terminal_stable_confirmed') is not True:
            raise ValueError('Terminal hold needs >=2s real feedback/video stability review')
        times = sorted({t for row in samples for t, source in zip(row['action_grid_ns'],row['action_sources'])
                        if hold[0] <= t <= hold[1] and source == 'human_hold'})
        if not times or times[-1]-times[0] < 1_966_666_666 or any(b-a > 50_000_000 for a,b in zip(times,times[1:])):
            raise ValueError('Reviewed terminal hold did not survive continuous valid indexing')
        terminal = dict(real_label_steps=len(times), first_ns=times[0], last_ns=times[-1], padding_used=False)
    # A new, explicitly scoped contract; never overwrite the frozen V1 config.
    cfg = dict(cfg, prompt_status='RTC HIL reader acceptance only; no training authorized',
               supervision_authorization='Reviewed submitted actions; no automatic policy relabelling',
               home_supervision=dict(review_sha256=sha256(review_path), require_feedback_completion=True))
    for name, value in [('contract.json',cfg),('review.json',review),
                        ('norm_stats.json',json.loads((baseline/'norm_stats.json').read_text()))]:
        (partial/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    np.savez_compressed(partial/'arrays.npz',states=np.stack(states),actions=np.stack(actions),
                        submitted=np.stack(joints),action_time_mask=np.stack(masks))
    (partial/'samples.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in samples))
    from prepare_formal import validation_indices
    (partial/'eval_indices.json').write_text(json.dumps(validation_indices('validation',len(samples))))
    manifest = dict(schema='f1_toys_eef_dataset_v3',smoke_only=synthetic,synthetic=synthetic,formal_training_ready=False,
        human_success_reviewed=not synthetic and review.get('task_success') is True,
        human_correction_reviewed=not synthetic,
        split='reader_acceptance_only',prospective_split=review['split'], source_group=review['source_group'],
        dataset_role=review['dataset_role'],source_manifest_sha256=report['source_manifest_sha256'],
        source_episodes=[dict(episode=report['source_episode_id'],manifest_sha256=report['source_manifest_sha256'])],
        samples=len(samples),valid_length_counts=dict(Counter(map(len,[r['action_grid_ns'] for r in samples]))),
        phase_counts=dict(Counter(r['supervision_phase'] for r in samples)),
        source_counts=dict(Counter(s for r in samples for s in r['action_sources'])),terminal_hold=terminal,
        statistics_scope='frozen baseline train-only; reader acceptance, refit future mixed train release',
        contract_sha256=sha256(partial/'contract.json'),norm_stats_sha256=sha256(partial/'norm_stats.json'),
        files_sha256={str(p.relative_to(partial)):sha256(p) for p in partial.rglob('*') if p.is_file()})
    (partial/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    from f1_toys.dataset import F1Dataset
    reader = F1Dataset(partial, training=False, allow_smoke_only=synthetic)
    for i in range(len(reader)):
        obs, action, mask = reader[i]
        assert mask.sum()==samples[i]['valid_action_steps'] and np.isfinite(action).all()
        from f1_toys.processing import CAMERA_KEYS
        assert (action[~mask]==0).all()
        assert obs['image_mask'] == {key: c in active_cameras(cfg) for c,key in CAMERA_KEYS.items()}
    receipt = dict(reader_samples=len(reader),rlinf_reader_verified=True,training_started=False,
        human_review_accepted=not synthetic,synthetic=synthetic,
        review_scope='synthetic_fixture_only' if synthetic else 'reviewed_real_recording',
        terminal_hold=terminal,source_manifest_sha256=report['source_manifest_sha256'],
        manifest_sha256=sha256(partial/'manifest.json'))
    receipt['indexed_windows_before_anchor_filter'] = indexed_count
    receipt['anchor_filtered_windows'] = indexed_count-len(samples)
    (partial/'reader-audit.json').write_text(json.dumps(receipt,indent=2)+'\n')
    os.replace(partial,output)
    return receipt


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('raw','review','baseline','output'):p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(prepare(a.raw,a.review,a.baseline,a.output)))
