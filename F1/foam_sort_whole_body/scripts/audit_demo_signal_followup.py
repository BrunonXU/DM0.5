#!/usr/bin/env python3
"""Explain demo numeric diagnostics without changing source data or masks."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np

from audit_demo_signals import ARM_NAMES, TASK, digest, read, require, stats, write


def one(row, prior):
    ep = row['episode']
    ap = Path(row['aligned_npz_path'])
    sp = Path(row['signal_npz_path'])
    sm = read(sp.with_suffix('.json'))
    require(digest(ap) == row['aligned_npz_sha256'] and digest(sp) == row['signal_npz_sha256'], 'Source digest mismatch')
    origin = row['source_start_ns']
    with np.load(ap, allow_pickle=False) as a, np.load(sp, allow_pickle=False) as s:
        q, hal = a['action'][:, :14], a['state'][:, :14]
        t = (a['time_ns'] - origin) / 1e9
        valid = a['action_candidate_valid'][:, :14].all(axis=1) & a['state_valid'][:, :14].all(axis=1)
        unchanged = (np.abs(np.diff(q, axis=0)).max(axis=1) <= 1e-6) & valid[1:] & valid[:-1]
        edges = np.flatnonzero(np.diff(np.r_[False, unchanged, False]))
        suspects = []
        for first, last in zip(edges[::2], edges[1::2]):
            if t[last] - t[first] < 1:
                continue
            target = q[first]
            initial_error = np.abs(target - hal[first])
            error_increase = np.abs(target - hal[last]) - initial_error
            span = np.ptp(hal[first:last + 1], axis=0)
            columns = np.flatnonzero((error_increase > 5) & (span > 5))
            if not len(columns):
                continue
            raw_ids = (s['lead_t_ns'] >= a['time_ns'][first]) & (s['lead_t_ns'] <= a['time_ns'][last])
            raw_span = np.ptp(s['lead_q'][raw_ids], axis=0) if raw_ids.any() else np.full(14, np.nan)
            growing = (np.abs(target - hal[first:last + 1]) - initial_error)[:, columns].max(axis=1) > 2
            divergence = first + int(np.flatnonzero(growing)[0])
            horizon = row['chunk_horizon']
            chunk_indices = np.flatnonzero(a['chunk_start_candidate_valid'])
            crossing = chunk_indices[(chunk_indices <= last) & (chunk_indices + horizon - 1 >= first)]
            crossing_divergence = chunk_indices[(chunk_indices <= last) & (chunk_indices + horizon - 1 >= divergence)]
            candidate = {'held_start_frame': int(first), 'held_last_frame': int(last),
                'held_start_s': float(t[first]), 'held_last_s': float(t[last]), 'held_duration_s': float(t[last] - t[first]),
                'continues_to_last_numeric_arm_frame': bool(last == np.flatnonzero(valid)[-1]),
                'raw_lead_sample_count': int(raw_ids.sum()), 'raw_lead_max_span_deg': float(raw_span.max()),
                'raw_lead_exact_constant': bool((raw_span == 0).all()),
                'held_lead_target_deg': target.tolist(), 'hal_at_held_start_deg': hal[first].tolist(),
                'hal_at_held_end_deg': hal[last].tolist(),
                'hal_span_deg': span.tolist(), 'target_error_increase_deg': error_increase.tolist(),
                'diverging_joints': [ARM_NAMES[j] for j in columns],
                'first_gt_2deg_extra_error_frame': int(divergence), 'first_gt_2deg_extra_error_s': float(t[divergence]),
                'existing_chunk_starts_inside_held_run': int(a['chunk_start_candidate_valid'][first:last + 1].sum()),
                'existing_joint_valid_frames_inside_held_run': int(a['joint_frame_candidate_valid'][first:last + 1].sum()),
                'existing_joint_valid_frames_after_divergence': int(a['joint_frame_candidate_valid'][divergence:last + 1].sum()),
                'existing_chunks_intersect_held_run': len(crossing),
                'existing_chunk_starts_before_held_run_crossing_boundary': int((crossing < first).sum()),
                'existing_chunks_intersect_after_divergence': len(crossing_divergence),
                'existing_chunk_starts_before_divergence_crossing_boundary': int((crossing_divergence < divergence).sum()),
                'proposed_training_mask_written': False,
                'interpretation': 'Feedback departs from unchanged ordinary lead target. Consistent with separate reset control path; not execution-version proof. Held-run start and >2deg detector are review bounds, not certified reset onset.'}
            suspects.append(candidate)

        # JSON zero is the first head-camera log sample; this is independently
        # compared to the offset inferred from all stored base velocity values.
        head_offset = (sm['topics']['/camera/head/color/image_raw/compressed']['first_ns'] - origin) / 1e9
        inferred = prior['base']['smoothstep_derivative_inference']['best_constant_time_offset_s']
        require(abs(head_offset - inferred) < 1e-7, 'JSON/base time origin does not equal first head log sample')
        b = prior['base']
        final_v = s['base_v'][-1]
        final_speed = float(np.linalg.norm(final_v[:2]))
        base_t = (s['base_t_ns'] - origin) / 1e9
        end = b['detected_windows_json']['return_end_s'] + head_offset
        moving = np.linalg.norm(s['base_v'][:, :2], axis=1) > 1e-9
        last_nonzero = int(np.flatnonzero(moving)[-1]) if moving.any() else None
        tail = {'last_sample_velocity_mps': final_v.tolist(), 'last_sample_speed_mps': final_speed,
                'last_base_sample_s': float(base_t[-1]), 'recording_duration_s': row['source_duration_ns'] / 1e9,
                'last_nonzero_sample_s': float(base_t[last_nonzero]) if last_nonzero is not None else None,
                'time_after_last_nonzero_until_record_stop_s': float(row['source_duration_ns'] / 1e9 - base_t[last_nonzero]) if last_nonzero is not None else None,
                'return_end_json_head_relative_s': b['detected_windows_json']['return_end_s'],
                'return_end_final_log_relative_s': end,
                'template_end_minus_last_base_sample_s': float(end - base_t[-1]),
                'template_end_minus_record_stop_s': float(end - row['source_duration_ns'] / 1e9),
                'has_recorded_zero_after_last_motion': bool(last_nonzero is not None and last_nonzero < len(base_t) - 1),
                'interpretation': 'Finite sampled weak-curve endpoint, not proof of incomplete physical return. Do not repeat last nonzero action beyond recording.'}
        jumps = []
        for change in prior['streams']['lead']['changes']['top']:
            if change['step'] <= 10:
                continue
            first = int(np.searchsorted(t, change['before_s']))
            last = int(np.searchsorted(t, change['after_s']))
            first, last = min(first, len(t)-1), min(last, len(t)-1)
            lo, hi = max(first-2, 0), min(last+3, len(t))
            nearby = range(lo, hi)
            starts = np.flatnonzero(a['chunk_start_candidate_valid'])
            # The transition in the aligned label stream is the last grid
            # before the new raw command and first grid at/after it. A chunk
            # starting at the latter has not supervised the jump itself.
            pre = last - 1
            post = last
            transition_valid = pre >= 0 and bool(a['action_candidate_valid'][pre, :14].all()) and bool(a['action_candidate_valid'][post, :14].all())
            crossing = starts[(starts <= pre) & (starts + row['chunk_horizon'] - 1 >= post)] if transition_valid else np.array([], dtype=np.int64)
            jumps.append({**change, 'joint': ARM_NAMES[change['column']],
                'candidate_chunks_spanning_valid_pre_and_post_transition_grid': len(crossing),
                'pre_transition_grid_s': float(t[pre]) if pre >= 0 else None,
                'post_transition_grid_s': float(t[post]),
                'pre_and_post_arm_actions_valid': bool(transition_valid),
                'first_valid_chunk_start_s': float(t[starts[0]]) if len(starts) else None,
                'nearby_grid': [{'time_s': float(t[i]), 'state_valid': bool(a['state_valid'][i].all()),
                    'action_valid': bool(a['action_candidate_valid'][i].all()),
                    'camera_valid': a['camera_timestamp_candidate_valid'][i].tolist(),
                    'chunk_start_valid': bool(a['chunk_start_candidate_valid'][i])} for i in nearby]})
        return {'episode': ep, 'split': row['parent_split'], 'terminal_lead_constant_hal_divergence': suspects,
                'json_time_origin': {'first_head_log_offset_s': head_offset, 'fitted_base_offset_s': inferred,
                    'difference_s': float(inferred-head_offset), 'mapping': 'final_log_relative_s = json_window_s + first_head_log_offset_s',
                    'base_labels_already_contain_offset': True, 'apply_extra_label_shift': False},
                'weak_base_endpoint': tail, 'startup_lead_jumps': jumps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=TASK/'results/demo_788_check')
    args = parser.parse_args()
    out = args.root/'signals'
    output = out/'episodes_followup.jsonl'
    require(not output.exists(), 'Followup output exists; preserve completed audit')
    frozen = (args.root/'candidates.jsonl').read_bytes()
    rows = [json.loads(line) for line in frozen.splitlines() if line.strip()]
    prior_bytes = (out/'episodes.jsonl').read_bytes()
    prior = {r['episode']: r for r in map(json.loads, prior_bytes.splitlines())}
    require(len(rows) == len(prior) == 788, 'Expected pinned 788-source audit')
    results = []
    with output.open('w') as f:
        for row in rows:
            result = one(row, prior[row['episode']])
            results.append(result)
            f.write(json.dumps(result, ensure_ascii=False, allow_nan=False)+'\n')
    require((args.root/'candidates.jsonl').read_bytes() == frozen, 'Candidates changed during followup')
    runs = [s for r in results for s in r['terminal_lead_constant_hal_divergence']]
    tail = [r for r in results if r['weak_base_endpoint']['last_sample_speed_mps'] > .01]
    summary = {'kind': 'numeric_followup_not_training_acceptance', 'candidate_count': len(results),
        'candidate_sha256': hashlib.sha256(frozen).hexdigest(), 'prior_episodes_sha256': hashlib.sha256(prior_bytes).hexdigest(),
        'script_sha256': digest(Path(__file__)),
        'terminal_frozen_lead_diverging_hal_source_count': sum(bool(r['terminal_lead_constant_hal_divergence']) for r in results),
        'terminal_frozen_lead_diverging_hal_run_count': len(runs),
        'runs_reconfirmed_exact_constant_in_float64_source': sum(s['raw_lead_exact_constant'] for s in runs),
        'runs_continue_to_last_arm_valid_frame': sum(s['continues_to_last_numeric_arm_frame'] for s in runs),
        'run_duration_s': stats([s['held_duration_s'] for s in runs]),
        'feedback_max_span_deg': stats([max(s['hal_span_deg']) for s in runs]),
        'error_growth_max_deg': stats([max(s['target_error_increase_deg']) for s in runs]),
        'existing_chunk_starts_inside_held_runs': sum(s['existing_chunk_starts_inside_held_run'] for s in runs),
        'existing_joint_valid_frames_inside_held_runs': sum(s['existing_joint_valid_frames_inside_held_run'] for s in runs),
        'existing_joint_valid_frames_after_divergence': sum(s['existing_joint_valid_frames_after_divergence'] for s in runs),
        'existing_chunks_intersect_held_runs': sum(s['existing_chunks_intersect_held_run'] for s in runs),
        'existing_chunk_starts_before_held_runs_crossing_boundary': sum(s['existing_chunk_starts_before_held_run_crossing_boundary'] for s in runs),
        'existing_chunks_intersect_after_divergence': sum(s['existing_chunks_intersect_after_divergence'] for s in runs),
        'count_interpretation': 'One run per source in this audit; no double counting. Held interval begins before detected reset divergence, so intersecting counts are diagnostic upper bounds, not a frozen cleaning decision.',
        'json_origin_equals_first_head_log_source_count': sum(abs(r['json_time_origin']['difference_s']) < 1e-7 for r in results),
        'json_origin_max_abs_error_s': max(abs(r['json_time_origin']['difference_s']) for r in results),
        'tail_gt_0_01mps_count': len(tail),
        'tail_gt_0_01mps_speed_mps': stats([r['weak_base_endpoint']['last_sample_speed_mps'] for r in tail]),
        'tail_gt_0_01mps_seconds_from_last_sample_to_template_end': stats([r['weak_base_endpoint']['template_end_minus_last_base_sample_s'] for r in tail]),
        'tail_gt_0_01mps_seconds_from_last_nonzero_to_record_stop': stats([r['weak_base_endpoint']['time_after_last_nonzero_until_record_stop_s'] for r in tail]),
        'tail_gt_0_01mps_template_end_minus_record_stop_s': stats([r['weak_base_endpoint']['template_end_minus_record_stop_s'] for r in tail]),
        'tail_gt_0_01mps_sources_with_zero_after_motion': sum(r['weak_base_endpoint']['has_recorded_zero_after_last_motion'] for r in tail),
        'startup_lead_jumps': [{'episode': r['episode'], **s} for r in results for s in r['startup_lead_jumps']],
        'training_masks_changed': False, 'training_exclusions_added': [],
        'terminal_pattern_recommendation': 'Review separate reset onset and mask the unsupported terminal motion, recomputing no-crossing future chunk masks. Do not drop all 788 sources, force HAL into action labels, or relabel task success automatically.',
        'base_endpoint_recommendation': 'Keep no-padding tail masks. Residual weak speed is a small sampled endpoint, not reason for whole-source rejection.',
        'base_origin_recommendation': 'For displaying JSON time windows add first head log offset. Stored base_v already uses this origin; do not shift labels again.'}
    write(out/'followup_summary.json', summary)
    with (out/'tail_reset_candidates.jsonl').open('w') as f:
        for r in results:
            for s in r['terminal_lead_constant_hal_divergence']:
                f.write(json.dumps({'episode': r['episode'], 'split': r['split'],
                    'tail_start_frame_candidate': s['held_start_frame'],
                    'tail_stop_frame_exclusive': s['held_last_frame'] + 1,
                    'tail_start_s_candidate': s['held_start_s'], 'tail_last_s': s['held_last_s'],
                    'boundary_status': 'conservative_held_lead_run_not_certified_reset_onset',
                    'already_decided_training_exclusion': False, **s}, ensure_ascii=False, allow_nan=False)+'\n')
    primary = read(out/'summary.json')
    primary['followup'] = {'script_sha256': digest(Path(__file__)), 'summary': 'followup_summary.json',
        'primary_issue': 'All 788 terminal held-lead runs have measured joint feedback departing from targets; current technical masks still admit them.',
        'tail_reset_candidates': 'tail_reset_candidates.jsonl',
        'json_time_origin_resolved': 'All 788 inferred offsets equal first head camera log_time minus MCAP start; stored base_v already contains offset, do not shift again.',
        'tail_nonzero_speed_resolved': '31 endpoints >0.01m/s are small (<=0.02733m/s) sampled deceleration tails, not automatic source rejection.'}
    write(out/'summary.json', primary)
    lines = ['# 数值复核追加：全批末端复位与时间原点', '',
        f"**{summary['terminal_frozen_lead_diverging_hal_source_count']}/788份末端有共同模式：双臂lead目标完全不变，但HAL反馈持续离开这个目标。**",
        f"已回查原始提取float64信号，{summary['runs_reconfirmed_exact_constant_in_float64_source']}段双臂14维lead严格恒定，并非float32量化误判；{summary['runs_continue_to_last_arm_valid_frame']}段一直持续到最后有效手臂帧。",
        f"恒定段时长中位{summary['run_duration_s']['p50']:.3f}秒、范围{summary['run_duration_s']['min']:.3f}–{summary['run_duration_s']['max']:.3f}秒；HAL最大变化至少{summary['feedback_max_span_deg']['min']:.2f}°。",
        '检测规则：lead14维连续不变≥1秒，同时至少一个HAL关节变化>5°、相对固定lead目标的误差增大>5°。这不是普通向目标收敛的跟随滞后，符合另一条控制路径复位但普通lead未记录新目标的数值表现；仍不能据此证明采集程序版本。', '',
        f"现有数值/相机有效性规则仍允许{summary['existing_chunk_starts_inside_held_runs']:,}个起点直接落在这些恒定段内，共{summary['existing_chunks_intersect_held_runs']:,}个50步chunk触及它们。恒定段起点可能早于复位真正开始，因此该数值是诊断范围，不是已经确定的删除量。",
        '**不建议丢掉全部788条，也不能把这些末端原样视为普通示教。** 下一版应定位复位起点、只屏蔽无命令监督的尾段，同时阻止未来动作chunk跨界；原录像不用手工分循环裁剪。不能用HAL反馈冒充缺失的执行命令。', '',
        '## 底盘JSON的时间原点已解释', '',
        f"788/788的模板拟合偏移等于头相机首条log_time减整包MCAP起点，最大差{summary['json_origin_max_abs_error_s']:.3g}秒。JSON检测窗口的0是首张头图，显示到整包时间轴时需加这个偏移。base_v已经按此原点生成，不能再整体平移一次。",
        '这证明的是标签内部时间关系，仍需视频确认运动窗口；不能据此宣布标签真实。', '',
        '## 31条非零末速度是微小采样尾端', '',
        f"最后base样本>0.01m/s的31条，速度{summary['tail_gt_0_01mps_speed_mps']['min']:.5f}–{summary['tail_gt_0_01mps_speed_mps']['max']:.5f}m/s，中位{summary['tail_gt_0_01mps_speed_mps']['p50']:.5f}m/s。",
        f"距模板返回结束只差{summary['tail_gt_0_01mps_seconds_from_last_sample_to_template_end']['min']:.5f}–{summary['tail_gt_0_01mps_seconds_from_last_sample_to_template_end']['max']:.5f}秒，符合最后采样没正好落在零速点；不能自动认定未完成返回或整条丢弃。维持尾部mask、不重复末帧非零速度作为未来监督。", '',
        '## 两条启动目标跳变', '']
    for r in summary['startup_lead_jumps']:
        lines.append(f"- `{r['episode']}` {r['before_s']:.3f}–{r['after_s']:.3f}s，{r['joint']}变化{r['step']:.3f}°；同时包含有效跳变前后格点的chunk候选数为{r['candidate_chunks_spanning_valid_pre_and_post_transition_grid']}，首个有效起点{r['first_valid_chunk_start_s']:.3f}s。" +
                     ("首有效chunk已在跳后，不能算跨跳变监督。" if r['first_valid_chunk_start_s'] > r['after_s'] else "该突变仍可出现在现有监督chunk内，需处理启动片段。"))
    lines += ['', '证据：[逐条追加数据](episodes_followup.jsonl)、[汇总](followup_summary.json)。本次没有修改任何mask、排除更多原录制、修正标签或启动训练。']
    (out/'followup_findings.md').write_text('\n'.join(lines)+'\n')
    primary_report = out/'findings.md'
    text = primary_report.read_text()
    header, rest = text.split('\n', 1)
    # Re-generating an explicitly preserved prior followup must not duplicate
    # the derived introduction in the primary findings file.
    marker = '本次逐条读取 '
    require(marker in rest, 'Primary numeric findings text changed unexpectedly')
    rest = '\n' + rest[rest.index(marker):]
    primary_report.write_text(header+'\n\n**首要新增问题：788/788尾部lead双臂目标严格恒定，HAL反馈却离开该目标；现有技术mask仍放行这些片段。** 需要定位独立复位并加尾段mask，不要整批丢弃。完整原始数值与候选边界见[末端复位追加核查](followup_findings.md)及[逐条候选](tail_reset_candidates.jsonl)。\n\n'+
        '底盘JSON的时间0已确认是首张头图：788/788固定原点差均与首头图log偏移完全一致，已存base_v包含该偏移，不能再次平移。31条非零末速度均不超过0.02733m/s，属于采样减速尾端，不是整条拒收理由。\n'+rest)
    print(json.dumps({k:summary[k] for k in ('candidate_count','terminal_frozen_lead_diverging_hal_source_count',
        'runs_reconfirmed_exact_constant_in_float64_source','existing_chunks_intersect_held_runs',
        'existing_chunks_intersect_after_divergence','json_origin_equals_first_head_log_source_count','tail_gt_0_01mps_count')},ensure_ascii=False))


if __name__ == '__main__':
    main()
