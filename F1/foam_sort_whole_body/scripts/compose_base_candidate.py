#!/usr/bin/env python3
"""Compose an audited base sidecar with the current conservative 19D pack.

This step never relaxes a parent mask or approves training. Proposed velocity
values may be previewed; only reviewed, present, finite base samples can retain
parent validity. Original arm/gripper, state, camera and time arrays are kept.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile

import numpy as np

TASK = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_npz(path, expected_sha):
    content = Path(path).read_bytes()
    require(digest(content) == expected_sha, f"SHA mismatch: {path}")
    with np.load(io.BytesIO(content), allow_pickle=False) as z:
        return {key: z[key].copy() for key in z.files}


def compose_arrays(parent, base, horizon):
    time = parent['time_ns']
    n = len(time)
    require(time.dtype == np.dtype('int64') and time.ndim == 1 and np.all(np.diff(time) > 0), 'invalid parent time')
    require(np.array_equal(time, base['time_ns']), 'sidecar and parent times differ')
    require(parent['state'].shape == (n, 21) and parent['action'].shape == (n, 19), 'expected state21/action19')
    require(parent['action_candidate_valid'].dtype == bool and parent['action_candidate_valid'].shape == (n, 19), 'invalid parent action mask')
    require(base['base_candidate_v'].shape == (n, 3), 'expected base3')
    require(type(horizon) is int and 0 < horizon <= n, 'invalid horizon')
    for name in ('base_candidate_present', 'base_timing_reviewed'):
        require(base[name].shape == (n,) and base[name].dtype == bool, f'invalid {name}')
    present = base['base_candidate_present']
    reviewed = base['base_timing_reviewed']
    finite = np.isfinite(base['base_candidate_v']).all(axis=1)
    require(not np.any(reviewed & ~present), 'reviewed base sample is absent')
    require(not np.any(present & ~finite), 'present base sample is nonfinite')
    require(np.isnan(base['base_candidate_v'][~present]).all(), 'unknown base must be NaN')
    result = {key: value.copy() for key, value in parent.items()}
    result['action'][:, 16:19] = base['base_candidate_v']
    result['action_candidate_valid'][:, 16:19] &= (present & reviewed & finite)[:, None]
    future = result['state_valid'].all(axis=1) & result['action_candidate_valid'].all(axis=1)
    result['future_signal_action_candidate_valid'] = future
    result['joint_frame_candidate_valid'] = result['observation_candidate_valid'] & result['action_candidate_valid'].all(axis=1)
    bad = np.r_[0, np.cumsum(~future)]
    chunks = np.zeros(n, dtype=bool)
    chunks[:n-horizon+1] = result['observation_candidate_valid'][:n-horizon+1] & (bad[horizon:]-bad[:-horizon] == 0)
    # Retain any prior semantic/chunk restrictions in addition to recomputation.
    result['chunk_start_candidate_valid'] = chunks & parent['chunk_start_candidate_valid']
    result['base_candidate_present'] = present.copy()
    result['base_timing_reviewed'] = reviewed.copy()
    result['training_approved'] = np.array(False)
    return result


def build(import_dir, output, cleaning_version='base_relabel_v2_conservative_composed_candidate'):
    require(isinstance(cleaning_version, str) and bool(cleaning_version.strip()), 'explicit nonempty cleaning version required')
    import_dir, output = Path(import_dir).resolve(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'use a new output directory')
    imported = json.loads((import_dir/'manifest.json').read_text())
    require(imported['schema'] == 'f1_base_repair_import_manifest_v1' and imported['status'] == 'complete', 'incomplete base import')
    for name, expected in imported['artifacts'].items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts, 'invalid artifact path')
        content = (import_dir/name).read_bytes()
        require(digest(content) == expected['sha256'] and len(content) == expected['size_bytes'], f'changed import artifact: {name}')
    binding = json.loads((import_dir/'repair_binding.json').read_text())
    ep = binding['episode']
    paths = {'selection': TASK/'results/demo_772_selection/candidates.jsonl', 'masks': TASK/'results/demo_772_selection/mask_manifest.jsonl'}
    registries, identities = {}, {}
    for name, path in paths.items():
        content = path.read_bytes()
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        require(len(rows) == len({row['episode'] for row in rows}), 'duplicate registry episode')
        registries[name] = {row['episode']: row for row in rows}
        identities[name] = {'path': str(path), 'sha256': digest(content)}
    require(ep in registries['selection'] and ep in registries['masks'], 'source is excluded or has no current mask')
    source, mask = registries['selection'][ep], registries['masks'][ep]
    require(source['aligned_npz_sha256'] == mask['parent_aligned_npz_sha256'], 'current mask has a different technical parent')
    require(source['source_start_ns'] == int(binding['source_origin_ns']) and source['source_duration_ns'] == int(binding['source_duration_ns']), 'source time identity mismatch')
    require(source['source_relative_path'] == binding['source_relative_path'] and source['parent_split'] == binding['parent_split'] == mask['parent_split'], 'source/split identity mismatch')
    require(source['aligned_npz_sha256'] == binding['source_products']['original_aligned_npz']['sha256'], 'sidecar technical source mismatch')
    parent = read_npz(mask['aligned_npz_path'], mask['aligned_npz_sha256'])
    meta_path = Path(mask['aligned_npz_path']).with_suffix('.json')
    meta_bytes = meta_path.read_bytes()
    require(digest(meta_bytes) == mask['aligned_metadata_sha256'], 'current metadata SHA mismatch')
    meta = json.loads(meta_bytes)
    require(meta['episode'] == ep and meta['tail_mask_start_index'] == mask['tail_mask_start_index'], 'current metadata identity mismatch')
    sidecar = import_dir/'candidate/base_relabel.npz'
    base = read_npz(sidecar, imported['artifacts']['candidate/base_relabel.npz']['sha256'])
    result = compose_arrays(parent, base, int(source['chunk_horizon']))
    require(not result['action_candidate_valid'][mask['tail_mask_start_index']:].any(), 'parent tail restriction changed')
    report = {'schema':'f1_composed_base_candidate_v1', 'episode':ep, 'training_approved':False,
              'frames':len(result['time_ns']), 'state_dim':21, 'action_dim':19,
              'base_present_frames':int(result['base_candidate_present'].sum()),
              'base_timing_reviewed_frames':int(result['base_timing_reviewed'].sum()),
              'joint_numeric_valid_frames':int(result['joint_frame_candidate_valid'].sum()),
              'chunk_starts':int(result['chunk_start_candidate_valid'].sum()),
              'tail_mask_start_index':mask['tail_mask_start_index'], 'tail_mask_start_s':mask['tail_mask_start_s'],
              'parent_masks_relaxed':False, 'arm_gripper_values_changed':False,
              'camera_time_state_changed':False, 'formal_training_reader_verified':False,
              'robot_replay_verified':False, 'limits':'Timing-reviewed weak base candidate; unknown gaps retained; not a complete-task training acceptance.',
              'source':{'registries':identities,'current_mask_entry':mask,'base_import_manifest':{'path':str(import_dir/'manifest.json'),'sha256':digest((import_dir/'manifest.json').read_bytes())},'script_sha256':digest(Path(__file__).read_bytes())}}
    meta.update({'parent_cleaning_version':meta.get('cleaning_version', meta.get('version')),
                 'cleaning_version':cleaning_version, 'version':cleaning_version, 'training_approved':False,
                 'composition':report, 'base_label_origin':'posthoc_route_prior_with_explicit_visual_timing'})
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='.base-compose-', dir=output.parent))
    try:
        np.savez_compressed(tmp/'episode.npz', **result)
        for name, obj in [('episode.json',meta),('report.json',report)]:
            (tmp/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        manifest = {'schema':'f1_composed_candidate_manifest_v1','episode':ep,'training_approved':False,
                    'artifacts':{p.name:{'sha256':digest(p.read_bytes()),'size_bytes':p.stat().st_size} for p in tmp.iterdir()}}
        (tmp/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        require(not output.exists(), 'output appeared during composition')
        tmp.rename(output)
    finally:
        if tmp.exists(): shutil.rmtree(tmp)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cleaning-version', default='base_relabel_v2_conservative_composed_candidate')
    args = parser.parse_args()
    report = build(args.import_dir, args.output, args.cleaning_version)
    print(json.dumps({k:report[k] for k in ('episode','frames','base_present_frames','base_timing_reviewed_frames','joint_numeric_valid_frames','chunk_starts','training_approved')},ensure_ascii=False,indent=2))
