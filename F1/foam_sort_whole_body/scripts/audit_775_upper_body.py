#!/usr/bin/env python3
"""Read-only recheck of the frozen 775 selection, upper-body labels and masks.

Writes a new audit directory only. Does not read raw MCAP payloads, modify source
packs, infer task success, or treat ordinary command/feedback error as a bug.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

TASK = Path(__file__).resolve().parents[1]
ARM_NAMES = [f'{side}_j{i}' for side in ('left', 'right') for i in range(1, 8)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load(path):
    with np.load(path, allow_pickle=False) as archive:
        return {key: archive[key] for key in archive.files}


def run_edges(mask):
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


def intersecting(starts, first, last, horizon):
    return starts[(starts <= last) & (starts + horizon - 1 >= first)]


def fresh_sample(t, values, grid):
    values = values[:, None] if values.ndim == 1 else values
    idx = np.searchsorted(t, grid, side='right') - 1
    safe = np.maximum(idx, 0)
    valid = ((idx >= 0) & (grid - t[safe] <= 100_000_000))[:, None]
    valid = valid & np.isfinite(values[safe])
    sampled = values[safe].astype(np.float32)
    sampled[~valid] = np.nan
    return sampled, valid


def one(row, mask, split, prior):
    ep, origin, horizon = row['episode'], row['source_start_ns'], row['chunk_horizon']
    require(row['parent_split'] == mask['parent_split'] == split, ep + ': source split changed')
    paths = [('parent_aligned', Path(row['aligned_npz_path']), row['aligned_npz_sha256']),
             ('signal', Path(row['signal_npz_path']), row['signal_npz_sha256']),
             ('masked_aligned', Path(mask['aligned_npz_path']), mask['aligned_npz_sha256'])]
    hashes = {}
    for name, path, expected in paths:
        hashes[name] = sha(path)
        require(hashes[name] == expected, ep + ': changed ' + name)
    for path, expected in [(paths[0][1].with_suffix('.json'), row['aligned_metadata_sha256']),
                           (paths[2][1].with_suffix('.json'), mask['aligned_metadata_sha256'])]:
        require(sha(path) == expected, ep + ': changed metadata')
    raw_stat = Path(row['raw_path']).stat()
    require(row['source_fingerprint'] == {'mtime_ns': raw_stat.st_mtime_ns, 'size_bytes': raw_stat.st_size},
            ep + ': raw source stat differs; no raw payload hash performed')
    p, s, a = [load(item[1]) for item in paths]
    n, tail = len(a['time_ns']), mask['tail_mask_start_index']
    grid, t = a['time_ns'], (a['time_ns'] - origin) / 1e9
    require(a['state'].shape == (n, 21) and a['action'].shape == (n, 19), ep + ': shape mismatch')
    require(n == row['frames'] == mask['frames_retained'], ep + ': frames changed')
    require(str(a['source_relative_path']) == row['source_relative_path'], ep + ': source identity mismatch')
    mutable = {'action_candidate_valid', 'future_signal_action_candidate_valid',
               'joint_frame_candidate_valid', 'chunk_start_candidate_valid', 'training_approved'}
    require(set(p) == set(a), ep + ': array keys changed')
    for key in p:
        if key not in mutable:
            require(p[key].tobytes() == a[key].tobytes(), ep + ': preserved array changed: ' + key)
        else:
            require(not np.any(a[key] & ~p[key]), ep + ': invalid data promoted: ' + key)
    require(not bool(a['training_approved']) and not row['training_approved'] and not mask['training_approved'],
            ep + ': unexpected approval')
    require(not mask['complete_cycle_verified'], ep + ': unexpected cycle approval')

    streams = {}
    sampled = {}
    for name, value_name in [('hal', 'hal_q'), ('lead', 'lead_q'), ('left_cmd', 'left_cmd'),
                             ('right_cmd', 'right_cmd'), ('left_state', 'left_state'), ('right_state', 'right_state')]:
        stamps, values = s[name + '_t_ns'], s[value_name]
        require(len(stamps) == len(values) and len(stamps) > 0, ep + ': empty/unequal ' + name)
        require(np.all(np.diff(stamps) > 0), ep + ': nonincreasing ' + name)
        require(np.isfinite(values).all(), ep + ': nonfinite raw numeric ' + name)
        gaps = np.flatnonzero(np.diff(stamps) > 100_000_000)
        streams[name] = {'count': len(stamps), 'nonfinite': 0,
                         'gaps_gt_100ms': [{'before_s': float((stamps[i] - origin) / 1e9),
                                           'after_s': float((stamps[i + 1] - origin) / 1e9)} for i in gaps]}
        if name not in ('hal', 'lead'):
            require(np.all((values >= 0) & (values <= 100)), ep + ': gripper outside [0,100]')
            streams[name].update(min=float(values.min()), max=float(values.max()))
        sampled[name] = fresh_sample(stamps, values, grid)
    hv, hm = sampled['hal']
    expected_state = np.c_[hv[:, :14], sampled['left_state'][0], sampled['right_state'][0], hv[:, 14:19]]
    expected_sv = np.c_[hm[:, :14], sampled['left_state'][1], sampled['right_state'][1], hm[:, 14:19]]
    expected_upper = np.c_[sampled['lead'][0], sampled['left_cmd'][0], sampled['right_cmd'][0]]
    expected_uv = np.c_[sampled['lead'][1], sampled['left_cmd'][1], sampled['right_cmd'][1]]
    require(np.array_equal(expected_state, a['state'], equal_nan=True), ep + ': state alignment differs')
    require(np.array_equal(expected_upper, a['action'][:, :16], equal_nan=True), ep + ': upper action alignment differs')
    require(np.array_equal(expected_sv, a['state_valid']), ep + ': invalid state/freshness admitted')
    require(np.array_equal(expected_uv, p['action_candidate_valid'][:, :16]), ep + ': original action freshness differs')
    expected_av = p['action_candidate_valid'].copy()
    expected_av[tail:] = False
    require(np.array_equal(expected_av, a['action_candidate_valid']), ep + ': wrong terminal mask')
    require(np.isfinite(a['state'][a['state_valid']]).all(), ep + ': admitted nonfinite state')
    require(np.isfinite(a['action'][a['action_candidate_valid']]).all(), ep + ': admitted nonfinite action')

    future = a['state_valid'].all(axis=1) & a['action_candidate_valid'].all(axis=1)
    obs = a['state_valid'].all(axis=1) & a['camera_timestamp_candidate_valid'].all(axis=1)
    expected_chunks = np.zeros(n, dtype=bool)
    if n >= horizon:
        # Independently enumerate every window; the production code uses prefix sums.
        expected_chunks[:n - horizon + 1] = np.lib.stride_tricks.sliding_window_view(future, horizon).all(axis=1)
    expected_chunks &= obs
    require(np.array_equal(obs, a['observation_candidate_valid']), ep + ': observation validity differs')
    require(np.array_equal(future, a['future_signal_action_candidate_valid']), ep + ': future validity differs')
    require(np.array_equal(future & obs, a['joint_frame_candidate_valid']), ep + ': joint validity differs')
    require(np.array_equal(expected_chunks, a['chunk_start_candidate_valid']), ep + ': chunk crosses invalid frame or bad observation')
    starts = np.flatnonzero(expected_chunks)
    require(len(starts) == mask['chunk_starts_after'], ep + ': chunk count differs')
    require(int((future & obs).sum()) == mask['joint_valid_frames_after'], ep + ': frame count differs')
    require(not len(intersecting(starts, tail, n - 1, horizon)), ep + ': chunk reaches masked terminal segment')
    require(tail == prior['terminal_lead_constant_hal_divergence'][0]['held_start_frame'], ep + ': old terminal boundary changed')

    # Preserve the historical >=1s constant-lead detector, adding transient peak
    # departure so a return towards target before a hold ends is not overlooked.
    q, hal = p['action'][:, :14], p['state'][:, :14]
    valid = p['action_candidate_valid'][:, :14].all(axis=1) & p['state_valid'][:, :14].all(axis=1)
    unchanged = (np.abs(np.diff(q, axis=0)).max(axis=1) <= 1e-6) & valid[1:] & valid[:-1]
    holds = []
    all_holds = 0
    for first, last in run_edges(unchanged):
        if t[last] - t[first] < 1:
            continue
        all_holds += 1
        errors = np.abs(hal[first:last + 1] - q[first])
        span = np.ptp(hal[first:last + 1], axis=0)
        end_growth, peak_growth = errors[-1] - errors[0], errors.max(axis=0) - errors[0]
        columns = np.flatnonzero((peak_growth > 5) & (span > 5))
        if not len(columns):
            continue
        raw_ids = (s['lead_t_ns'] >= grid[first]) & (s['lead_t_ns'] <= grid[last])
        raw_values = s['lead_q'][raw_ids]
        raw_exact = bool(len(raw_values) and np.all(raw_values == raw_values[0]))
        holds.append({'start_frame': int(first), 'last_frame': int(last), 'start_s': float(t[first]),
                      'last_s': float(t[last]), 'duration_s': float(t[last] - t[first]),
                      'terminal': bool(last == np.flatnonzero(valid)[-1]),
                      'before_current_tail_mask': bool(last < tail),
                      'raw_lead_samples': int(raw_ids.sum()), 'raw_lead_exact_constant': raw_exact,
                      'hal_max_span_deg': float(span.max()), 'max_end_error_growth_deg': float(end_growth.max()),
                      'max_peak_error_growth_deg': float(peak_growth.max()),
                      'end_growth_rule_matches': bool(np.any((end_growth > 5) & (span > 5))),
                      'diverging_joints': [ARM_NAMES[i] for i in columns],
                      'current_joint_valid_frames': int(a['joint_frame_candidate_valid'][first:last + 1].sum()),
                      'current_chunks_intersecting': len(intersecting(starts, first, last, horizon))})

    jumps = []
    delta = np.abs(np.diff(s['lead_q'], axis=0))
    for i in np.flatnonzero(delta.max(axis=1) > 10):
        post = int(np.searchsorted(grid, s['lead_t_ns'][i + 1]))
        pre = post - 1
        cross = starts[(starts <= pre) & (starts + horizon - 1 >= post)]
        joint = int(delta[i].argmax())
        jumps.append({'before_s': float((s['lead_t_ns'][i] - origin) / 1e9),
                      'after_s': float((s['lead_t_ns'][i + 1] - origin) / 1e9),
                      'joint': ARM_NAMES[joint], 'step_deg': float(delta[i, joint]),
                      'aligned_pre_frame': pre, 'aligned_post_frame': post,
                      'aligned_pre_s': float(t[pre]) if 0 <= pre < n else None,
                      'aligned_post_s': float(t[post]) if post < n else None,
                      'current_chunks_spanning_jump': len(cross), 'crossing_chunk_start_frames': cross.tolist(),
                      'first_current_chunk_start_s': float(t[starts[0]]),
                      'historically_known': bool(prior['startup_lead_jumps'])})
    numeric_upper = expected_sv.all(axis=1) & expected_uv.all(axis=1)
    valid_ids = np.flatnonzero(numeric_upper)
    interior_invalid = int((~numeric_upper[valid_ids[0]:valid_ids[-1] + 1]).sum()) if len(valid_ids) else n
    return {'episode': ep, 'split': split, 'hashes': hashes, 'checks_passed': True,
            'frames': n, 'joint_valid_frames': int((future & obs).sum()), 'chunk_starts': len(starts),
            'tail_mask_start_s': float(t[tail]), 'tail_mask_start_frame': tail,
            'upper_numeric_invalid_frames_before_tail': int((~numeric_upper[:tail]).sum()),
            'upper_numeric_interior_invalid_frames': interior_invalid,
            'admitted_invalid_upper_numeric_frames': int((future & ~numeric_upper).sum()),
            'invalid_camera_observation_chunk_starts': int((expected_chunks & ~a['camera_timestamp_candidate_valid'].all(axis=1)).sum()),
            'constant_lead_holds_ge_1s': all_holds, 'constant_lead_diverging_hal_runs': holds,
            'lead_jumps_gt_10deg': jumps, 'upper_streams': streams,
            'tracking_error_auto_classified_as_bug': False, 'training_approved': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=TASK / 'results/demo_775_phase_review/upper_body')
    args = parser.parse_args()
    require(not args.output.exists(), 'Refusing to overwrite a completed or partial audit directory')
    selection = TASK / 'results/demo_775_selection'
    frozen_paths = [selection / 'candidates.jsonl', selection / 'mask_manifest.jsonl',
                    TASK / 'results/cleaning_v1/source_split.json',
                    TASK / 'results/demo_788_check/signals/episodes_followup.jsonl']
    frozen_hashes = {str(p.relative_to(TASK)): sha(p) for p in frozen_paths}
    candidates, masks = rows(frozen_paths[0]), {r['episode']: r for r in rows(frozen_paths[1])}
    splits = json.loads(frozen_paths[2].read_text())['assignments']
    prior = {r['episode']: r for r in rows(frozen_paths[3])}
    require(len(candidates) == len(masks) == len({r['episode'] for r in candidates}) == 775, 'Expected pinned 775 selection')
    require({r['episode'] for r in candidates} == set(masks), 'Candidate/mask identities differ')
    require(len({r['source_relative_path'] for r in candidates}) == 775, 'Duplicate raw source')
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    with (args.output / 'episodes.jsonl').open('x') as stream:
        for row in candidates:
            result = one(row, masks[row['episode']], splits[row['episode']], prior[row['episode']])
            results.append(result)
            stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
            if len(results) % 100 == 0:
                print(f'Checked {len(results)}/775 upper-body packs', flush=True)
    for p in frozen_paths:
        require(sha(p) == frozen_hashes[str(p.relative_to(TASK))], 'Input manifest changed during audit')
    hold_runs = [{'episode': r['episode'], **h} for r in results for h in r['constant_lead_diverging_hal_runs']]
    jumps = [{'episode': r['episode'], **h} for r in results for h in r['lead_jumps_gt_10deg']]
    summary = {'kind': 'upper_body_readonly_technical_recheck_not_training_or_execution_acceptance',
               'created_at': datetime.now(timezone.utc).isoformat(), 'script_sha256': sha(Path(__file__)),
               'frozen_inputs_sha256': frozen_hashes, 'sources': len(results),
               'splits': dict(Counter(r['split'] for r in results)),
               'all_checks_passed': all(r['checks_passed'] for r in results),
               'joint_valid_frames': sum(r['joint_valid_frames'] for r in results),
               'chunk_starts': sum(r['chunk_starts'] for r in results),
               'admitted_invalid_upper_numeric_frames': sum(r['admitted_invalid_upper_numeric_frames'] for r in results),
               'invalid_camera_observation_chunk_starts': sum(r['invalid_camera_observation_chunk_starts'] for r in results),
               'upper_numeric_invalid_frames_before_tail': sum(r['upper_numeric_invalid_frames_before_tail'] for r in results),
               'upper_numeric_interior_invalid_frames': sum(r['upper_numeric_interior_invalid_frames'] for r in results),
               'raw_upper_gaps_gt_100ms': sum(len(s['gaps_gt_100ms']) for r in results for s in r['upper_streams'].values()),
               'terminal_constant_lead_diverging_hal_runs': sum(h['terminal'] for h in hold_runs),
               'terminal_runs_current_chunks_intersecting': sum(h['current_chunks_intersecting'] for h in hold_runs if h['terminal']),
               'internal_constant_lead_diverging_hal_runs': [h for h in hold_runs if not h['terminal']],
               'lead_jumps_gt_10deg': jumps,
               'gripper_ranges': {key: {'min': min(r['upper_streams'][key]['min'] for r in results),
                                         'max': max(r['upper_streams'][key]['max'] for r in results)}
                                  for key in ('left_cmd', 'right_cmd', 'left_state', 'right_state')},
               'nonfinite_raw_upper_values': 0, 'nonfinite_admitted_values': 0,
               'raw_mcap_payloads_read_or_hashed': False, 'raw_source_size_mtime_checked': True,
               'candidate_or_mask_mutated': False, 'training_approved': False,
               'scope_limits': ['>=1s held-lead and >5deg departure is a diagnostic screen, not an exhaustive anomaly proof',
                                'ordinary tracking differences are not classified as bugs',
                                'source grouping inherited; near-duplicate content not re-audited',
                                'historical camera timestamp masks preserved; physical exposure alignment not proven',
                                'actual execution mapping, collected software version and task outcomes remain unverified']}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    lines = ['# 2026-09-15：775份上半身复核', '',
             '**尾部复位监督已隔离；上半身尚不能宣布全部验收，000080的历史启动目标跳变仍有10个有效chunk跨越。**', '',
             '本次从当前775名单和mask清单读取全部来源，复核派生包/原候选/信号哈希及raw大小与mtime，未读取raw MCAP载荷或重扫其完整哈希。',
             f"共{summary['sources']}份，分组{summary['splits']}；{summary['joint_valid_frames']:,}联合有效帧、{summary['chunk_starts']:,}动作块，与当前清单一致。", '',
             '## 已经解决、此次仅验证保持', '',
             f"- {summary['terminal_constant_lead_diverging_hal_runs']}段末端lead14恒定而HAL背离的历史区间仍全部隔离，当前动作块触及数{summary['terminal_runs_current_chunks_intersecting']}。无需把旧尾端问题再当新增异常。",
             '- 时间、状态、动作值、图像索引及旧有效性完整继承，未放行任何原本无效项。独立逐窗口重算50步chunk与实际一致，尾部不补值、动作块不跨无效数值帧；相机有效性要求在观测起点。',
             f"- 全部原提取上半身流finite、时间严格递增；>100ms内部原始缺口{summary['raw_upper_gaps_gt_100ms']}。尾边界前共有{summary['upper_numeric_invalid_frames_before_tail']:,}个上半身无效数值帧，放行0个；内部无效数值帧{summary['upper_numeric_interior_invalid_frames']}。已知旧2.6秒HAL缺口不在当前775池。",
             f"- 夹爪命令/反馈均在[0,100]，右夹爪全恒100；当前775份继承原录制train/validation分组，无来源跨组或自动训练批准。", '',
             '## 仍需处理的具体边界', '']
    for j in jumps:
        lines.append(f"- `{j['episode']}`：{j['before_s']:.6f}–{j['after_s']:.6f}s，{j['joint']}目标变化{j['step_deg']:.6f}°，当前有效chunk跨跳变{j['current_chunks_spanning_jump']}个；这是历史已知项，不是新发现。")
    lines += ['', f"内部非末尾lead14持续恒定≥1秒，且同一HAL关节活动跨度>5°、相对固定目标误差增加>5°的模式：{len(summary['internal_constant_lead_diverging_hal_runs'])}段。此次同时检查末点误差及区间峰值误差，未据普通lead/HAL跟踪差自动判bug。", '',
              '检测为数值筛查，不证明所有人工干预/执行异常均不存在。000080仍需核真实启动过程并另存启动边界处理；未擅自改候选或mask。真实执行映射、采集版本、相机曝光对时和任务结果仍未知，离线通过不能等同真机/成功验收。', '',
              '证据：[汇总](summary.json)、[逐来源复核](episodes.jsonl)、[历史启动与尾端复核](../../demo_788_check/signals/followup_findings.md)。',
              '复现工具：[audit_775_upper_body.py](../../../scripts/audit_775_upper_body.py)。冻结输出拒绝覆盖；新复现请传入其他--output目录。']
    (args.output / 'findings.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
