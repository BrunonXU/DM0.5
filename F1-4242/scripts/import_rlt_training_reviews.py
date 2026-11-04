#!/usr/bin/env python3
"""Project explicitly selected replay receipts into pending, source-bound UI reviews.

This offline importer never trains, contacts another machine or writes recording
sidecars. Its output MUST pass robot journal-prefix verification before importing
through save_training_review. A cache proves reviewed replay preparation, not
that an optimizer has consumed its transitions.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re


OBJECTIVE = 'approach_place_release_v1'
STREAMS = {'observations', 'policy_chunks', 'control', 'events'}
MAX_JSON_BYTES = 16 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value, **kwargs):
    return json.dumps(value, allow_nan=False, **kwargs).encode()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def cache_root(value):
    check(isinstance(value, str), 'cache path must be a string')
    p = PurePosixPath(value)
    check(p.is_absolute() and '..' not in p.parts and str(p) == value, 'noncanonical cache path')
    return value


def read_json(path):
    with Path(path).open('rb') as handle:
        raw = handle.read(MAX_JSON_BYTES + 1)
    check(len(raw) <= MAX_JSON_BYTES, 'input JSON exceeds size limit')
    return json.loads(raw), digest(raw)


def policy_split(policy, receipt, receipt_sha):
    """Mirror f1_toys.rlt_data_policy.select_receipt without importing trainers."""
    check(isinstance(policy, dict) and policy.get('schema') == 'f1_rlt_data_policy_v1'
          and policy.get('default') == 'quarantine' and isinstance(policy.get('episodes'), dict),
          'unsupported data selection policy')
    entry = policy['episodes'].get(receipt['episode_id'])
    if entry is not None:
        check(entry.get('source_receipt_sha256') == receipt_sha and entry.get('reason'),
              'data policy receipt identity/reason mismatch')
        return entry.get('split')
    future, binding = policy.get('future'), receipt['binding']
    if not future:
        return 'quarantine'
    match = re.fullmatch(r'f1_hil_(\d{8}T\d{6}Z)_[0-9a-f]+', receipt['episode_id'])
    if (not match or match[1] < future['not_before_utc'] or
            binding.get('recording_boundary') != 'f1_rlt_local_v2' or
            binding.get('execution_mode') != 'powered' or binding.get('robot') != 'F1-09' or
            binding.get('base_model_version') != future['base_model_version'] or
            binding.get('rlt', {}).get('encoder_manifest_sha256') != future['encoder_manifest_sha256']):
        return 'quarantine'
    modulus = future['validation_modulus']
    check(type(modulus) is int and modulus >= 2, 'invalid policy validation modulus')
    return 'validation' if int(digest(receipt['episode_id'].encode())[:16], 16) % modulus == 0 else 'train'


def project_record(item, recording, policy=None):
    m, receipt, review = item['manifest'], item['source_receipt'], item.get('review')
    check(m.get('schema') == 'f1_local_replay_cache_v1' and m.get('synthetic') is False
          and m.get('learning_scope') == 'local_replay' and m.get('objective') == OBJECTIVE,
          'not a real local replay cache')
    episode = m.get('episode_id')
    check(episode == recording.get('run_id') == receipt.get('episode_id'), 'source episode mismatch')
    check(recording.get('capture_mode') == 'rlt', 'selected cache is not an RLT recording')
    check(valid_sha(recording.get('metadata_sha256')) and valid_sha(recording.get('source_signature')),
          'catalog source binding missing')
    check(receipt.get('schema') == 'f1_local_journal_prefix_v1' and receipt.get('drop_count') == 0
          and receipt.get('binding') == recording.get('binding')
          and receipt['binding'].get('synthetic', False) is False
          and receipt['binding'].get('execution_mode') == 'powered', 'receipt launch binding mismatch')
    # F1Recorder.checkpoint_record uses json.dump(..., allow_nan=False), preserving
    # insertion order. The copied cache uses rlt_online.atomic_json(indent=2).
    source_sha = digest(encoded(receipt))
    check(source_sha == m.get('source_receipt_sha256'), 'original source receipt SHA mismatch')
    files = m.get('files_sha256', {})
    check(digest(encoded(receipt, indent=2)) == files.get('source-receipt.json'),
          'cached receipt SHA mismatch')
    check(all(valid_sha(files.get(name)) for name in ('replay.pt', 'transitions.jsonl')),
          'cache file identities missing')
    streams = receipt.get('streams')
    check(isinstance(streams, dict) and set(streams) == STREAMS, 'unexpected receipt streams')
    for name, value in streams.items():
        check(isinstance(value, dict) and type(value.get('bytes')) is int and value['bytes'] > 0
              and valid_sha(value.get('sha256')), 'invalid journal prefix identity')
        source = next((f for f in recording.get('files', []) if f.get('relative_path') == 'journal/' + name + '.jsonl'), None)
        check(source is not None and source.get('size_bytes', 0) >= value['bytes'],
              'receipt prefix exceeds catalog journal')
    raw_result = receipt.get('local_result', {})
    result = m.get('local_result', {})
    check(raw_result.get('episode_id') == episode and result.get('episode_id') == episode
          and raw_result.get('segment_id') == m.get('segment_id') == result.get('segment_id')
          and raw_result.get('event') == 'rlt_local_closed' and raw_result.get('objective') == OBJECTIVE,
          'local segment identity mismatch')
    expected = dict(raw_result)
    if review is None:
        check(m.get('review_sha256') is None and 'review.json' not in files, 'missing source outcome review')
    else:
        check(isinstance(review, dict) and review.get('source_receipt_sha256') == source_sha
              and review.get('episode_id', episode) == episode and review.get('objective') == OBJECTIVE
              and review.get('approved') is True and bool(str(review.get('reviewer') or '').strip())
              and review.get('status') in ('success', 'failure'), 'outcome review is not source-bound/approved')
        check(review['status'] != 'success' or review.get('placement_release_confirmed') is True,
              'success review lacks placement/release confirmation')
        check(digest(encoded(review, indent=2)) == files.get('review.json'), 'cached outcome review SHA mismatch')
        # Existing human outcome reviews were saved either with atomic_json or
        # as UTF-8 indent=2 plus newline; require one exact original-byte digest.
        approved_digests = {digest(encoded(review, indent=2)),
                            digest(encoded(review, ensure_ascii=False, indent=2) + b'\n')}
        check(m.get('review_sha256') in approved_digests, 'original outcome review SHA mismatch')
        if 'assisted' in review:
            check(review['assisted'] == raw_result.get('assisted'), 'assistance evidence conflict')
        expected.update(status=review['status'], reward=float(review['status'] == 'success'),
                        terminated=True, truncated=False, terminal_observation=review['terminal_observation'])
    check(result == expected, 'cache local result differs from receipt and approved review')
    check(result.get('status') in ('success', 'failure') and result.get('terminated') is True
          and result.get('truncated') is False and result.get('reward') == float(result['status'] == 'success'),
          'cache has no confirmed local terminal outcome')
    check(type(result.get('assisted')) is bool, 'missing assistance evidence')
    terminal = result.get('terminal_observation', {})
    check(type(terminal.get('observation_id')) is int and terminal['observation_id'] >= 0
          and type(terminal.get('available_monotonic_ns')) is int
          and type(result.get('started_ns')) is int and type(result.get('ended_ns')) is int
          and result['started_ns'] <= terminal['available_monotonic_ns'] <= result['ended_ns'],
          'invalid local terminal observation')
    count, retained, split = m.get('transition_count'), m.get('retained_seconds'), m.get('split')
    check(type(count) is int and count > 0 and isinstance(retained, (int, float))
          and not isinstance(retained, bool) and math.isfinite(retained) and retained > 0
          and split in ('train', 'validation'), 'invalid replay counts or split')
    check(retained <= (terminal['available_monotonic_ns'] - result['started_ns']) / 1e9 + 1,
          'retained duration exceeds reviewed local segment')
    if policy is not None:
        check(policy_split(policy, receipt, source_sha) == split, 'cache split conflicts with selection policy')
    check(valid_sha(m.get('recipe_sha256')) and isinstance(m.get('adapter_sha256'), dict)
          and m['adapter_sha256'] and all(valid_sha(v) for v in m['adapter_sha256'].values()),
          'cache recipe/adapter identities missing')
    key = digest((source_sha + m['recipe_sha256'] + str(m.get('review_sha256')) +
                  json.dumps(m['adapter_sha256'], sort_keys=True)).encode())
    check(PurePosixPath(item['path']).parent.name == key, 'cache identity differs from selected path')
    is_success = result['status'] == 'success'
    outcome = ('assisted_success' if result['assisted'] else 'autonomous_success') if is_success else 'failure'
    confirmation = 'manual_confirmation' if is_success and review else ('live_success' if is_success else 'reviewed_failure')
    check(not (is_success and review is None) or result.get('termination_reason') == 'human_confirmed',
          'success has neither live UP evidence nor approved manual review')
    label = {'manual_confirmation': '人工补确认成功（原始标记保留）',
             'live_success': '现场↑成功标记', 'reviewed_failure': '已审核的局部失败'}[confirmation]
    if is_success and review and review.get('confirmation_source') == 'visual_audit':
        # Existing UI schema has no visual-audit enum. Do not misrepresent an
        # image audit as an operator confirmation; keep the explicit label.
        confirmation, label = 'none', '视觉复核成功（非操作者补确认）'
    check(isinstance(item.get('mtime'), (int, float)) and math.isfinite(item['mtime']), 'cache snapshot time missing')
    snapshot_time = datetime.fromtimestamp(item['mtime'], timezone.utc).isoformat()
    refs = ['selected replay cache: ' + item['path'], 'receipt SHA256: ' + source_sha]
    if review:
        refs += [str(ref) for ref in review.get('evidence_refs', [])]
    notes = ('人工补确认成功，保留原始结束标记。' if confirmation == 'manual_confirmation' else label + '。')
    notes += f'已审核局部{count}个转移，保留{retained:.2f}秒；' + ('开发验证留出。' if split == 'validation' else '可用于训练回放。')
    notes += '这是审核与准入快照，不代表学习器已完成梯度更新。'
    projected = dict(schema='f1_rlt_training_review_v1', episode_id=episode,
                     metadata_sha256=recording['metadata_sha256'], source_signature=recording['source_signature'],
                     approved=True, reviewed_at=snapshot_time, split=split, outcome=outcome,
                     transition_count=count, retained_seconds=retained,
                     confirmation_kind=confirmation, confirmation_label=label,
                     source_label='已审核回放缓存快照', notes_summary=notes,
                     excluded_summary='局部审核终点之后及质量切片不纳入本回执；完整录制仍保留。',
                     evidence_refs=refs[:64], terminal_monotonic_ns=terminal['available_monotonic_ns'],
                     terminal_observation_id=terminal['observation_id'], source_receipt_sha256=source_sha,
                     source_stream_hashes=streams, replay_cache_path=str(PurePosixPath(item['path']).parent),
                     cache_manifest_sha256=digest(encoded(m, indent=2)), gradient_updates_verified=False,
                     requires_remote_prefix_verification=True, requires_remote_policy_verification=policy is None)
    return dict(id=recording['id'], episode_id=episode, recording_path=recording['path'], review=projected,
                verification=dict(requires_remote_prefix_verification=True, streams=streams,
                                  terminal_observation=terminal, original_local_result=raw_result,
                                  metadata_sha256=recording['metadata_sha256'],
                                  source_signature=recording['source_signature'],
                                  requires_remote_policy_verification=policy is None))


def build_projection(catalog, snapshot, config, policy=None):
    check(isinstance(catalog, dict) and isinstance(catalog.get('episodes'), list), 'invalid catalog snapshot')
    check(isinstance(snapshot, list) and len(snapshot) <= 10000, 'invalid replay snapshot')
    f1 = config.get('f1', {})
    selected = f1.get('seed_caches')
    check(isinstance(selected, list) and selected and len(selected) <= 1000, 'explicit seed_caches required')
    selected = [cache_root(p) for p in selected]
    check(len(set(selected)) == len(selected), 'duplicate selected cache')
    check(isinstance(f1.get('data_policy'), str) and f1['data_policy'], 'selection policy identity required')
    items = {}
    for item in snapshot:
        check(isinstance(item, dict) and isinstance(item.get('path'), str), 'invalid snapshot cache item')
        path = cache_root(item['path'])
        check(PurePosixPath(path).name == 'manifest.json', 'snapshot path is not a cache manifest')
        root = str(PurePosixPath(path).parent)
        if root not in selected:
            continue
        check(root not in items, 'duplicate selected snapshot cache')
        items[root] = item
    check(set(items) == set(selected), 'selected cache missing from replay snapshot')
    by_episode = {}
    for row in catalog['episodes']:
        if row.get('run_id'):
            check(row['run_id'] not in by_episode, 'ambiguous catalog episode')
            by_episode[row['run_id']] = row
    reviews, used_episodes = [], set()
    for root in selected:
        item = items[root]
        episode = item.get('manifest', {}).get('episode_id')
        check(episode in by_episode, 'selected cache source missing from catalog')
        check(episode not in used_episodes, 'selected caches duplicate a source episode')
        used_episodes.add(episode)
        reviews.append(project_record(item, by_episode[episode], policy))
    return dict(schema='f1_rlt_training_review_import_v1', created_at=datetime.now(timezone.utc).isoformat(),
                projection_only=True, save_permitted=False, gradient_updates_verified=False,
                selected_cache_paths=selected, ignored_cache_count=len(snapshot) - len(items),
                data_policy_path=f1['data_policy'], policy_content_verified=policy is not None,
                requires_remote_prefix_verification=True, reviews=reviews,
                totals={split: sum(row['review']['transition_count'] for row in reviews if row['review']['split'] == split)
                        for split in ('train', 'validation')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('catalog', 'replay-snapshot', 'selection-config', 'output'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--data-policy', type=Path, help='Explicit local copy of the selected policy; never fetched implicitly')
    args = parser.parse_args()
    catalog, catalog_sha = read_json(args.catalog)
    snapshot, snapshot_sha = read_json(args.replay_snapshot)
    config, config_sha = read_json(args.selection_config)
    policy, policy_sha = read_json(args.data_policy) if args.data_policy else (None, None)
    expected = config.get('f1', {}).get('data_policy_sha256')
    check(not expected or policy_sha == expected, 'selected data policy file SHA mismatch')
    result = build_projection(catalog, snapshot, config, policy)
    result['inputs_sha256'] = dict(catalog=catalog_sha, replay_snapshot=snapshot_sha,
                                   selection_config=config_sha, data_policy=policy_sha)
    # An existing output is not silently replaced by a new admission snapshot.
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
    print(json.dumps(dict(output=str(args.output), review_count=len(result['reviews']),
                          totals=result['totals'], save_permitted=False)))


if __name__ == '__main__':
    main()
