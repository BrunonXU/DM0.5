#!/usr/bin/env python3
"""Create an explicit, evidence-bound mask revision without overwriting inputs.

Only configured episodes get new arrays. Other manifest entries keep their
existing paths and hashes. Verification rereads every manifest pack and checks
all future windows independently of the prefix-sum implementation used to build.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

MUTABLE = {'action_candidate_valid', 'future_signal_action_candidate_valid',
           'joint_frame_candidate_valid', 'chunk_start_candidate_valid', 'training_approved'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_write(path, value):
    with path.open('x') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def arrays(path):
    with np.load(path, allow_pickle=False) as source:
        return {key: source[key] for key in source.files}


def referenced_path(config_path, value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def digest_bound_file(config_path, spec):
    path = referenced_path(config_path, spec['path'])
    require(sha(path) == spec['sha256'], 'Input digest mismatch: ' + str(path))
    return path


def counts(pack):
    future = pack['future_signal_action_candidate_valid']
    return {'frames_retained': len(future),
            'joint_valid_frames': int(pack['joint_frame_candidate_valid'].sum()),
            'chunk_starts': int(pack['chunk_start_candidate_valid'].sum()),
            'future_valid_frames': int(future.sum()),
            'action_valid_cells': int(pack['action_candidate_valid'].sum()),
            'continuous_windows': int(np.count_nonzero(np.diff(np.r_[False, future, False].astype(int)) == 1))}


def derive(parent, start, stop, horizon):
    result = {key: value.copy() for key, value in parent.items()}
    result['action_candidate_valid'][start:stop, :] = False
    future = result['state_valid'].all(axis=1) & result['action_candidate_valid'].all(axis=1)
    result['future_signal_action_candidate_valid'] = future
    result['joint_frame_candidate_valid'] = future & result['observation_candidate_valid']
    n = len(future)
    starts = np.zeros(n, dtype=bool)
    if n >= horizon:
        bad_prefix = np.r_[0, np.cumsum(~future, dtype=np.int64)]
        starts[:n - horizon + 1] = bad_prefix[horizon:] - bad_prefix[:-horizon] == 0
    result['chunk_start_candidate_valid'] = starts & result['observation_candidate_valid']
    result['training_approved'] = np.array(False)
    return result


def verify_pack(record, parent_record, candidate, revision, config_hash):
    ep = record['episode']
    parent_path, path = Path(parent_record['aligned_npz_path']), Path(record['aligned_npz_path'])
    for item in (record, parent_record):
        source = Path(item['aligned_npz_path'])
        require(sha(source) == item['aligned_npz_sha256'], ep + ': pack hash mismatch')
        require(sha(source.with_suffix('.json')) == item['aligned_metadata_sha256'], ep + ': metadata hash mismatch')
    parent, result = arrays(parent_path), arrays(path)
    require(set(parent) == set(result), ep + ': array keys changed')
    require(record['parent_split'] == parent_record['parent_split'] == candidate['parent_split'], ep + ': split changed')
    n, horizon = len(result['time_ns']), candidate['chunk_horizon']
    require(result['action'].shape == (n, 19) and result['state'].shape == (n, 21), ep + ': incorrect action/state shape')
    require(str(result['source_relative_path']) == candidate['source_relative_path'], ep + ': source identity changed')
    require(record['tail_mask_start_index'] == parent_record['tail_mask_start_index'], ep + ': inherited tail boundary changed')
    require(record['tail_mask_start_s'] == parent_record['tail_mask_start_s'], ep + ': inherited tail time changed')
    require(not bool(result['training_approved']) and not record['training_approved'], ep + ': unexpected training approval')
    require(not record['complete_cycle_verified'], ep + ': unexpected complete-cycle approval')
    for key in parent:
        if key not in MUTABLE:
            require(parent[key].tobytes() == result[key].tobytes(), ep + ': non-mask array changed: ' + key)
        else:
            require(not np.any(result[key] & ~parent[key]), ep + ': previously invalid data promoted: ' + key)
    expected_action = parent['action_candidate_valid'].copy()
    if revision is not None:
        lo, hi = revision['start_frame'], revision['stop_frame_exclusive']
        expected_action[lo:hi] = False
        require(path != parent_path, ep + ': revision overwrote its parent')
        require(record['mask_revision_config_sha256'] == config_hash, ep + ': missing config binding')
        require(record['parent_manifest_record'] == parent_record, ep + ': old manifest lineage changed')
        metadata = json.loads(path.with_suffix('.json').read_text())
        require(metadata['parent_metadata'] == json.loads(parent_path.with_suffix('.json').read_text()), ep + ': old metadata lineage changed')
    else:
        require(record == parent_record, ep + ': unrelated manifest record changed')
        require(path == parent_path, ep + ': unrelated pack duplicated')
    require(np.array_equal(expected_action, result['action_candidate_valid']), ep + ': unexpected action mask edits')
    valid = result['state_valid'].all(axis=1) & expected_action.all(axis=1)
    observations = result['state_valid'].all(axis=1) & result['camera_timestamp_candidate_valid'].all(axis=1)
    require(np.array_equal(observations, result['observation_candidate_valid']), ep + ': observation mask wrong')
    require(np.array_equal(valid, result['future_signal_action_candidate_valid']), ep + ': future mask wrong')
    require(np.array_equal(valid & observations, result['joint_frame_candidate_valid']), ep + ': joint mask wrong')
    # Independent window enumeration instead of the production prefix-sum rule.
    expected_chunks = np.zeros(n, dtype=bool)
    if n >= horizon:
        expected_chunks[:n - horizon + 1] = np.lib.stride_tricks.sliding_window_view(valid, horizon).all(axis=1)
    expected_chunks &= observations
    require(np.array_equal(expected_chunks, result['chunk_start_candidate_valid']), ep + ': future window check failed')
    starts = np.flatnonzero(expected_chunks)
    require(np.all(starts + horizon <= record['tail_mask_start_index']), ep + ': chunk reaches inherited tail')
    before, after = counts(parent), counts(result)
    removed = np.flatnonzero(parent['chunk_start_candidate_valid'] & ~expected_chunks).tolist()
    removed_joint = np.flatnonzero(parent['joint_frame_candidate_valid'] & ~result['joint_frame_candidate_valid']).tolist()
    removed_future = np.flatnonzero(parent['future_signal_action_candidate_valid'] & ~valid).tolist()
    require(after['joint_valid_frames'] == record['joint_valid_frames_after'], ep + ': joint count mismatch')
    require(after['chunk_starts'] == record['chunk_starts_after'], ep + ': chunk count mismatch')
    require(after['frames_retained'] == record['frames_retained'], ep + ': frame retention mismatch')
    assertions = revision.get('validation_assertions', {}) if revision else {}
    if 'removed_chunk_start_frames' in assertions:
        require(removed == assertions['removed_chunk_start_frames'], ep + ': unexpected chunk removals')
    if 'removed_joint_valid_frames' in assertions:
        require(len(removed_joint) == assertions['removed_joint_valid_frames'], ep + ': unexpected joint-frame removals')
    if 'transition_pre_frame' in assertions:
        pre, post = assertions['transition_pre_frame'], assertions['transition_post_frame']
        require(not np.any((starts <= pre) & (starts + horizon - 1 >= post)), ep + ': a valid chunk still spans the target transition')
    return {'episode': ep, 'verified': True, 'changed': revision is not None,
            'before': before, 'after': after, 'removed_chunk_start_frames': removed,
            'removed_joint_valid_frame_indices': removed_joint,
            'removed_future_valid_frame_indices': removed_future,
            'values_times_images_and_nonmask_arrays_byte_identical': True,
            'no_previously_invalid_mask_entry_promoted': True,
            'all_retained_future_windows_independently_verified': True,
            'original_pack_and_metadata_hashes_unchanged': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config_path, output = args.config.resolve(), args.output.resolve()
    require(not output.exists(), 'Refusing to overwrite output: ' + str(output))
    config_bytes = config_path.read_bytes()
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    cfg = json.loads(config_bytes)
    require(cfg['mask_scope'] == 'full_19D_action_supervision', 'Only explicit full 19D masking is supported')
    candidate_path = digest_bound_file(config_path, cfg['candidates'])
    parent_path = digest_bound_file(config_path, cfg['parent_mask_manifest'])
    candidate_rows, parent_rows = read_rows(candidate_path), read_rows(parent_path)
    candidates = {r['episode']: r for r in candidate_rows}
    parents = {r['episode']: r for r in parent_rows}
    require(len(candidates) == len(candidate_rows) == len(parents) == len(parent_rows) == cfg['expected_source_count'], 'Unexpected source count/duplicates')
    require(set(candidates) == set(parents), 'Candidate/parent source sets differ')
    require(dict(Counter(r['parent_split'] for r in candidate_rows)) == cfg['expected_split_counts'], 'Unexpected source split')
    revisions = {r['episode']: r for r in cfg['revisions']}
    require(len(revisions) == len(cfg['revisions']) and revisions, 'Expected nonempty unique episode revisions')
    require(not set(revisions) - set(candidates), 'Revision episode absent from input selection')
    evidence_paths = {}
    for ep, rev in revisions.items():
        require(not Path(ep).is_absolute() and '..' not in Path(ep).parts, 'Unsafe episode path')
        require(type(rev['start_frame']) is int and type(rev['stop_frame_exclusive']) is int, 'Boundaries must be integer frame indices')
        require(0 <= rev['start_frame'] < rev['stop_frame_exclusive'] <= candidates[ep]['frames'], 'Invalid interval bounds')
        evidence_paths[ep] = digest_bound_file(config_path, rev['evidence'])
    # Verify every inherited pack before creating the new manifest.
    for record in parent_rows:
        path = Path(record['aligned_npz_path'])
        require(sha(path) == record['aligned_npz_sha256'], 'Parent array hash changed: ' + record['episode'])
        require(sha(path.with_suffix('.json')) == record['aligned_metadata_sha256'], 'Parent metadata hash changed: ' + record['episode'])
    output.mkdir(parents=True, exist_ok=False)
    with (output / 'config_snapshot.json').open('xb') as stream:
        stream.write(config_bytes)
    (output / 'evidence').mkdir()
    for ep, evidence_path in evidence_paths.items():
        destination = output / 'evidence' / (ep.replace('/', '_') + evidence_path.suffix)
        with destination.open('xb') as stream:
            stream.write(evidence_path.read_bytes())
    manifest = []
    for parent in parent_rows:
        ep = parent['episode']
        if ep not in revisions:
            manifest.append(parent)
            continue
        rev, candidate = revisions[ep], candidates[ep]
        source = Path(parent['aligned_npz_path'])
        parent_meta = json.loads(source.with_suffix('.json').read_text())
        old = arrays(source)
        start, stop = rev['start_frame'], rev['stop_frame_exclusive']
        new = derive(old, start, stop, candidate['chunk_horizon'])
        before, after = counts(old), counts(new)
        target = (output / 'aligned' / ep).with_suffix('.npz')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            np.savez_compressed(stream, **new)
        interval = {'start_frame': start, 'stop_frame_exclusive': stop,
                    'start_s': float((old['time_ns'][start] - candidate['source_start_ns']) / 1e9),
                    'stop_s_exclusive': float((old['time_ns'][stop] - candidate['source_start_ns']) / 1e9) if stop < len(old['time_ns']) else candidate['source_duration_ns'] / 1e9,
                    'reason': rev['reason'], 'evidence': rev['evidence']}
        metadata = {**parent_meta,
                    'cleaning_version': cfg['version'], 'parent_cleaning_version': parent['cleaning_version'],
                    'parent_metadata': parent_meta,
                    'parent_aligned_npz_sha256': parent['aligned_npz_sha256'],
                    'parent_aligned_metadata_sha256': parent['aligned_metadata_sha256'],
                    'parent_aligned_npz_path': str(source),
                    'original_candidate_aligned_npz_sha256': candidate['aligned_npz_sha256'],
                    'candidate_selection_sha256': cfg['candidates']['sha256'],
                    'parent_mask_manifest_sha256': cfg['parent_mask_manifest']['sha256'],
                    'mask_revision_config_sha256': config_hash,
                    'mask_revision_intervals': [interval],
                    'parent_counts': before, 'current_counts': after,
                    'joint_frame_candidate_valid_frames': after['joint_valid_frames'],
                    'chunk_candidate_starts': after['chunk_starts'],
                    'continuous_windows': after['continuous_windows'],
                    'training_approved': False, 'complete_cycle_verified': False,
                    'note': 'Explicit startup supervision mask layered on the inherited conservative tail mask. Values, timestamps and camera arrays are preserved. Candidate only; execution and complete task success are not verified.'}
        json_write(target.with_suffix('.json'), metadata)
        record = {**parent, 'cleaning_version': cfg['version'],
                  'parent_cleaning_version': parent['cleaning_version'],
                  'parent_manifest_record': parent,
                  'parent_aligned_npz_path': str(source),
                  'parent_aligned_npz_sha256': parent['aligned_npz_sha256'],
                  'parent_aligned_metadata_sha256': parent['aligned_metadata_sha256'],
                  'original_candidate_aligned_npz_sha256': candidate['aligned_npz_sha256'],
                  'aligned_npz_path': str(target), 'aligned_npz_sha256': sha(target),
                  'aligned_metadata_sha256': sha(target.with_suffix('.json')),
                  'mask_revision_config_sha256': config_hash, 'mask_revision_intervals': [interval],
                  'joint_valid_frames_before': before['joint_valid_frames'],
                  'joint_valid_frames_after': after['joint_valid_frames'],
                  'chunk_starts_before': before['chunk_starts'], 'chunk_starts_after': after['chunk_starts'],
                  'training_approved': False, 'complete_cycle_verified': False}
        manifest.append(record)
    manifest_path = output / 'manifest.jsonl'
    with manifest_path.open('x') as stream:
        for record in manifest:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')
    # Reopen serialized output, all parent packs and all current metadata.
    validated = []
    with (output / 'validation_episodes.jsonl').open('x') as stream:
        for record in read_rows(manifest_path):
            ep = record['episode']
            result = verify_pack(record, parents[ep], candidates[ep], revisions.get(ep), config_hash)
            validated.append(result)
            stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
            if len(validated) % 100 == 0:
                print(f'Independently verified {len(validated)}/{len(manifest)} packs', flush=True)
    require(config_path.read_bytes() == config_bytes, 'Config changed during build')
    digest_bound_file(config_path, cfg['candidates'])
    digest_bound_file(config_path, cfg['parent_mask_manifest'])
    for rev in revisions.values():
        digest_bound_file(config_path, rev['evidence'])
    validation = {'status': 'passed', 'sources': len(validated),
                  'manifest_sha256': sha(manifest_path),
                  'config_snapshot_sha256': sha(output / 'config_snapshot.json'),
                  'all_parent_and_current_pack_and_metadata_hashes_verified': True,
                  'all_nonmask_arrays_byte_identical': True, 'no_invalid_mask_entry_promoted': True,
                  'all_chunks_independently_verified_by_window_enumeration': True,
                  'source_selection_and_split_unchanged': True,
                  'exact_configured_removed_chunk_start_indices_verified': True,
                  'changed_episodes': [r for r in validated if r['changed']]}
    json_write(output / 'validation.json', validation)
    summary = {'version': cfg['version'], 'kind': 'candidate_supervision_mask_revision_not_training_acceptance',
               'created_at': datetime.now(timezone.utc).isoformat(), 'script_sha256': sha(Path(__file__)),
               'config_sha256': config_hash, 'candidates': cfg['candidates'],
               'parent_mask_manifest': cfg['parent_mask_manifest'],
               'manifest_path': str(manifest_path), 'manifest_sha256': sha(manifest_path),
               'source_count': len(manifest), 'split_counts': cfg['expected_split_counts'],
               'new_array_pack_count': len(revisions), 'unchanged_referenced_pack_count': len(manifest) - len(revisions),
               'totals_before': {key: sum(r['before'][key] for r in validated) for key in validated[0]['before']},
               'totals_after': {key: sum(r['after'][key] for r in validated) for key in validated[0]['after']},
               'removed_joint_valid_frames': sum(len(r['removed_joint_valid_frame_indices']) for r in validated),
               'removed_chunk_starts': sum(len(r['removed_chunk_start_frames']) for r in validated),
               'changed_episodes': list(revisions), 'validation_status': 'passed',
               'raw_and_previous_packs_modified': False, 'source_selection_modified': False,
               'training_approved': False, 'complete_cycle_verified': False,
               'robot_execution_verified': False,
               'limits': 'Startup masking isolates the recorded old-target prefix; it does not establish the acquisition version, execution mapping, visual alignment or task outcome. Inherited weak-base-label review and conservative tail boundaries remain in force.'}
    json_write(output / 'summary.json', summary)
    (output / 'report.md').write_text('\n'.join([
        '# 启动监督mask候选修订', '',
        f"版本 `{cfg['version']}`：{len(manifest)}份来源和原分组保持，新增{len(revisions)}个派生数组包，其余{len(manifest) - len(revisions)}个沿用原路径/哈希。",
        f"联合有效帧 {summary['totals_before']['joint_valid_frames']} → {summary['totals_after']['joint_valid_frames']}；50步chunk {summary['totals_before']['chunk_starts']} → {summary['totals_after']['chunk_starts']}。",
        '按显式配置区间屏蔽完整19D监督；原数值、时间、图像和已知尾段边界保留。旧文件与旧清单没有覆盖。', '',
        '全部当前/父版包及metadata哈希、所有非mask数组和未放宽有效性已验证，独立逐窗口重算全部50步chunk通过。配置指定的跨启动跳变chunk已精确撤销，未删除额外起点。', '',
        '这是数据侧候选隔离，不是采集发布程序修复、训练放行、真机执行或任务成功验收。', '',
        '证据：[新清单](manifest.jsonl)、[汇总](summary.json)、[独立验证](validation.json)、[逐条验证](validation_episodes.jsonl)、[配置快照](config_snapshot.json)。',
    ]) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
