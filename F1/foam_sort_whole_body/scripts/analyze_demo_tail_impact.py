#!/usr/bin/env python3
"""Quantify terminal-arm masking tradeoffs for 778 demo sources; no masks written."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from audit_demo_signals import ARM_NAMES, TASK, digest, read, require, stats, write

EXTRA_EXCLUDED = {'all_data_2_derived/episode_000256'} | {
    f'all_data_1_derived/episode_{i:06d}' for i in range(9)}
EXAMPLES = {'all_data_derived/episode_000192': 4,
            'all_data_derived/episode_000093': 7}


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def remaining_template_path(segments, cut, origin_offset):
    remaining = 0.0
    per_segment = []
    for i, (a, b, x, y, xx, yy, label) in enumerate(segments):
        u = float(np.clip((cut-origin_offset-a)/(b-a), 0, 1))
        distance = float(np.hypot(xx-x, yy-y))
        rest = distance * (1-3*u*u+2*u*u*u)
        remaining += rest
        per_segment.append({'segment': i, 'label': label, 'remaining_weak_path_m': rest})
    return remaining, sum(x['remaining_weak_path_m'] for x in per_segment[3:]), per_segment


def analyze(row, tail, previous):
    ep = row['episode']
    require(digest(Path(row['aligned_npz_path'])) == row['aligned_npz_sha256'], 'Aligned SHA changed')
    require(digest(Path(row['signal_npz_path'])) == row['signal_npz_sha256'], 'Signal SHA changed')
    source = Path(row['raw_path'])
    weak = read(source.parent/'estimated_move_state.json')
    offset = previous['json_time_origin']['first_head_log_offset_s']
    duration = row['source_duration_ns']/1e9
    held = tail['held_start_s']
    divergence = tail['first_gt_2deg_extra_error_s']
    return_begin = weak['detected_windows']['return_start_s']+offset
    return_end = weak['detected_windows']['return_end_s']+offset
    windows = {}
    with np.load(row['aligned_npz_path'], allow_pickle=False) as a, np.load(row['signal_npz_path'], allow_pickle=False) as s:
        origin = row['source_start_ns']
        t = (a['time_ns']-origin)/1e9
        bt = (s['base_t_ns']-origin)/1e9
        speed = np.linalg.norm(s['base_v'][:, :2], axis=1)
        moving = speed > 1e-9
        last_moving_index = int(np.flatnonzero(moving)[-1])
        last_motion = float(bt[last_moving_index])
        first_zero_after_last_motion = float(bt[last_moving_index+1]) if last_moving_index+1<len(bt) else None
        cut_frame = tail['held_start_frame']
        valid_prefix = a['joint_frame_candidate_valid'][:cut_frame]
        first_valid = int(np.flatnonzero(valid_prefix)[0])
        zero_at_end = (np.linalg.norm(a['action'][:cut_frame, 16:18], axis=1)<=1e-9) & a['action_candidate_valid'][:cut_frame, 16:19].all(axis=1)
        last_nonzero_grid = int(np.flatnonzero(~zero_at_end)[-1]) if (~zero_at_end).any() else -1
        zero_grid_start = last_nonzero_grid+1
        retained_prefix = {'first_joint_valid_s': float(t[first_valid]),
            'last_retained_grid_s': float(t[cut_frame-1]), 'stop_s_exclusive': held,
            'interior_joint_invalid_frames': int((~valid_prefix[first_valid:]).sum()),
            'retained_zero_base_tail_frames': int(cut_frame-zero_grid_start),
            'retained_zero_base_tail_duration_grid_s': float((cut_frame-zero_grid_start)/30),
            'first_recorded_zero_after_last_base_motion_s': first_zero_after_last_motion,
            'zero_base_time_before_cut_s': max(0.0, held-first_zero_after_last_motion) if first_zero_after_last_motion is not None else 0.0,
            'base_weak_peak_mps': float(speed.max()), 'weak_window_method': weak['detected_windows']['method'],
            'visual_task_success_verified': False, 'robot_replay_approved': False}
        for label, cut, frame in [('held_run_start', held, tail['held_start_frame']),
                                 ('extra_error_gt_2deg', divergence, tail['first_gt_2deg_extra_error_frame'])]:
            lost_path, lost_return, segments = remaining_template_path(weak['motion_segments'], cut, offset)
            # Segment geometry and fitted smoothstep describe synthetic labels
            # only. Distances are never presented as measured robot travel.
            start_ids = np.flatnonzero(a['chunk_start_candidate_valid'])
            crossing = start_ids[(start_ids <= tail['held_last_frame']) &
                (start_ids+row['chunk_horizon']-1 >= frame)]
            tail_grid = t >= cut
            av = a['action_candidate_valid'][:, 16:19].all(axis=1)
            aligned_moving = np.linalg.norm(a['action'][:, 16:18], axis=1)>1e-9
            idx = max(int(np.searchsorted(bt, cut, side='right')-1), 0)
            windows[label] = {'cut_frame_candidate': frame, 'cut_s_candidate': cut,
                'last_nonzero_weak_base_s': last_motion,
                'seconds_until_last_nonzero_weak_base': last_motion-cut,
                'cuts_off_nonzero_base_samples': bool(np.any(moving & (bt>=cut))),
                'return_phase_at_cut': 'before_return_starts' if cut<return_begin else
                    'return_in_progress' if cut<return_end else 'return_already_ended',
                'return_end_minus_cut_s': return_end-cut,
                'latest_recorded_base_at_cut_mps': s['base_v'][idx].tolist(),
                'removed_nonzero_weak_base_source_samples': int(np.sum(moving & (bt>=cut))),
                'removed_nonzero_weak_base_aligned_frames': int(np.sum(tail_grid & av & aligned_moving)),
                'removed_joint_valid_frames': int(np.sum(tail_grid & a['joint_frame_candidate_valid'])),
                'candidate_chunks_intersect_removed_tail': len(crossing),
                'remaining_candidate_chunks_if_joint_tail_masked': len(start_ids)-len(crossing),
                'discarded_template_path_m_not_measured': lost_path,
                'discarded_return_template_path_m_not_measured': lost_return,
                'discarded_return_template_fraction': lost_return/2.6,
                'remaining_weak_path_by_segment': segments}
        examples = None
        if ep in EXAMPLES:
            j = EXAMPLES[ep]
            query = sorted(set([float(held), float(divergence), float(return_begin), float(return_end),
                      *[float(x) for x in np.arange(np.floor(held*2)/2, duration, .5)]]))
            samples = []
            for second in query:
                i = min(int(np.searchsorted(t, second)), len(t)-1)
                samples.append({'time_s': float(t[i]), 'joint': ARM_NAMES[j],
                    'lead_target_deg': float(a['action'][i, j]), 'hal_feedback_deg': float(a['state'][i, j]),
                    'recorded_base_weak_mps': a['action'][i, 16:19].tolist()})
            raw_ids = s['lead_t_ns']>=int(a['time_ns'][tail['held_start_frame']])
            examples = {'highlight_joint': ARM_NAMES[j], 'held_target_full_deg': a['action'][tail['held_start_frame'], :14].tolist(),
                'start_hal_full_deg': a['state'][tail['held_start_frame'], :14].tolist(),
                'last_hal_full_deg': a['state'][-1, :14].tolist(),
                'raw_lead_tail_peak_to_peak_deg': np.ptp(s['lead_q'][raw_ids], axis=0).tolist(),
                'timeline': samples}
        last_hal = a['state'][-1, :14].tolist()
    return {'episode': ep, 'split': row['parent_split'], 'duration_s': duration,
        'held_start_s': held, 'held_last_s': tail['held_last_s'], 'held_duration_s': tail['held_duration_s'],
        'divergence_gt_2deg_s': divergence,
        'return_begin_log_relative_s': return_begin, 'return_end_log_relative_s': return_end,
        'cuts': windows, 'retained_prefix': retained_prefix, 'last_arm_feedback_deg': last_hal, 'example': examples,
        'mask_written': False, 'proven_real_base_return_end': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-root', type=Path, default=TASK/'results/demo_788_check')
    parser.add_argument('--output', type=Path, default=TASK/'results/demo_778_tail/analysis')
    parser.add_argument('--extend-existing', action='store_true', help='Preserve and extend this tool\'s own earlier analysis')
    args = parser.parse_args()
    if args.output.exists():
        require(args.extend_existing, 'Output exists; preserve completed analysis')
        existing = read(args.output/'summary.json')
        require(existing.get('candidate_count')==778 and existing.get('mask_written') is False and
                existing.get('scope','').startswith('778 remaining sources after user-authorized'),
                'Existing output not recognized as this tool\'s own analysis')
        backup = args.output/'initial_analysis_preserved'
        require(not backup.exists(), 'Prior analysis backup exists; do not overwrite it')
        backup.mkdir()
        for name in ('summary.json', 'episodes.jsonl', 'findings.md'):
            (backup/name).write_bytes((args.output/name).read_bytes())
    cpath = args.previous_root/'candidates.jsonl'
    tpath = args.previous_root/'signals/tail_reset_candidates.jsonl'
    fpath = args.previous_root/'signals/episodes_followup.jsonl'
    cbytes = cpath.read_bytes()
    rows = jsonl(cpath)
    require(len(rows)==788 and EXTRA_EXCLUDED <= {r['episode'] for r in rows}, 'Expected approved 788 baseline/exclusions')
    selected = [r for r in rows if r['episode'] not in EXTRA_EXCLUDED]
    require(len(selected)==778, 'Expected 778 remaining sources')
    new_manifest = args.output.parent/'candidates.jsonl'
    frozen_new_sha = None
    if new_manifest.exists():
        new_rows = {r['episode']:r for r in jsonl(new_manifest)}
        require(set(new_rows)=={r['episode'] for r in selected}, 'Frozen 778 manifest selection differs')
        for row in selected:
            require(all(new_rows[row['episode']][k]==row[k] for k in
                ('aligned_npz_sha256','signal_npz_sha256','parent_split')), 'Frozen 778 source changed')
        frozen_new_sha=digest(new_manifest)
    tails = {r['episode']:r for r in jsonl(tpath)}
    previous = {r['episode']:r for r in jsonl(fpath)}
    results = [analyze(r, tails[r['episode']], previous[r['episode']]) for r in selected]
    require(cpath.read_bytes()==cbytes, 'Candidate input changed during analysis')
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': '778 remaining sources after user-authorized 000256 + nine head-clock exclusions; analysis only, no new training manifest or mask',
        'candidate_count': len(results), 'split_counts': dict(Counter(r['split'] for r in results)),
        'input_hashes': {'candidate_sha256': hashlib.sha256(cbytes).hexdigest(),
            'tail_candidates_sha256': digest(tpath), 'signal_followup_sha256': digest(fpath),
            'frozen_778_candidate_sha256': frozen_new_sha},
        'extra_excluded': sorted(EXTRA_EXCLUDED), 'script_sha256': digest(Path(__file__)),
        'held_duration_s': stats([r['held_duration_s'] for r in results]),
        'held_to_detected_divergence_s': stats([r['divergence_gt_2deg_s']-r['held_start_s'] for r in results]),
        'cuts': {}, 'examples': {r['episode']:r for r in results if r['example'] is not None},
        'mask_written': False, 'candidate_manifest_changed': False,
        'interpretation': 'lead is recorded ordinary teleop command candidate, HAL is measured joint feedback. A constant target with feedback departing toward a common pose is compatible with separate reset control; no command-ACK or acquisition-version proof.',
        'recommendation': ['Do not subtract a fixed 3.8 seconds: it is a median, and terminal intervals overlap stored base return in most sources.',
            'Whole-action terminal masking creates partial-task trajectories for most sources; do not advertise them as complete-return replay.',
            'Review actual reset onset and retained base return with images and command semantics; per-dimension arm loss masking can retain base labels only after appropriate loader/loss/replay support and weak-label checks.',
            'Never silently replace missing ordinary commands with HAL feedback or guess a reset command. A derived feedback-trajectory target is a separate action contract requiring explicit validation.',
            'Offline image/state/action replay is suitable for diagnosing this now; hardware replay readiness additionally requires verified executable targets, mapping, timing and onsite user operation.']}
    for key in ('held_run_start', 'extra_error_gt_2deg'):
        values = [r['cuts'][key] for r in results]
        hit = [v for v in values if v['cuts_off_nonzero_base_samples']]
        summary['cuts'][key] = {'sources_cutting_nonzero_base_samples': len(hit),
            'sources_after_base_motion_samples': len(values)-len(hit),
            'return_phase_at_cut_counts': dict(Counter(v['return_phase_at_cut'] for v in values)),
            'seconds_until_last_nonzero_base_all': stats([v['seconds_until_last_nonzero_weak_base'] for v in values]),
            'seconds_until_last_nonzero_base_overlapping_only': stats([v['seconds_until_last_nonzero_weak_base'] for v in hit]),
            'discarded_return_template_fraction_all': stats([v['discarded_return_template_fraction'] for v in values]),
            'discarded_return_template_fraction_overlapping_only': stats([v['discarded_return_template_fraction'] for v in hit]),
            'discarded_weak_return_path_m_total_not_measured': sum(v['discarded_return_template_path_m_not_measured'] for v in values),
            'removed_nonzero_weak_base_aligned_frames': sum(v['removed_nonzero_weak_base_aligned_frames'] for v in values),
            'removed_joint_valid_frames': sum(v['removed_joint_valid_frames'] for v in values),
            'candidate_chunks_intersect_removed_tail': sum(v['candidate_chunks_intersect_removed_tail'] for v in values),
            'remaining_candidate_chunks_if_joint_tail_masked': sum(v['remaining_candidate_chunks_if_joint_tail_masked'] for v in values)}
    endpoints = np.array([r['last_arm_feedback_deg'] for r in results])
    median = np.median(endpoints, axis=0)
    summary['arm_endpoints'] = {'per_joint_median_deg': median.tolist(), 'per_joint_min_deg': endpoints.min(axis=0).tolist(),
        'per_joint_max_deg': endpoints.max(axis=0).tolist(),
        'per_source_max_deviation_from_population_median_deg': stats(np.abs(endpoints-median).max(axis=1)),
        'meaning': 'Descriptive measured endpoint cluster, not a command target or certified home-pose parameter.'}
    complete = [r for r in results if not r['cuts']['held_run_start']['cuts_off_nonzero_base_samples']]
    summary['base_stopped_before_held_tail'] = {'source_count': len(complete),
        'split_counts': dict(Counter(r['split'] for r in complete)),
        'sources_by_split': {split:[r['episode'] for r in complete if r['split']==split] for split in sorted({r['split'] for r in complete})},
        'zero_base_time_before_cut_s': stats([r['retained_prefix']['zero_base_time_before_cut_s'] for r in complete]),
        'retained_zero_base_tail_duration_grid_s': stats([r['retained_prefix']['retained_zero_base_tail_duration_grid_s'] for r in complete]),
        'sources_with_continuous_joint_valid_prefix': sum(r['retained_prefix']['interior_joint_invalid_frames']==0 for r in complete),
        'meaning': 'Complete stored weak base template retained; does not establish visual task completion or true physical return.'}
    recommended_ids = [f'all_data_3_derived/episode_{i:06d}' for i in (83,66,2)]
    recommended = [r for ep in recommended_ids for r in complete if r['episode']==ep]
    require(len(recommended)==3 and all(r['split']=='train' and
        r['retained_prefix']['interior_joint_invalid_frames']==0 and
        r['retained_prefix']['retained_zero_base_tail_duration_grid_s']>=.1 for r in recommended),
        'Recommended prefix no longer satisfies numeric criteria')
    summary['offline_replay_numeric_candidates'] = [
        {'episode':r['episode'], 'split':r['split'], **r['retained_prefix']} for r in recommended]
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output/'episodes.jsonl').open('w') as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n')
    write(args.output/'summary.json', summary)
    with (args.output/'base_stopped_before_tail.jsonl').open('w') as f:
        for r in complete:
            f.write(json.dumps({'episode':r['episode'],'split':r['split'],
                'held_start_s':r['held_start_s'],'return_end_log_relative_s':r['return_end_log_relative_s'],
                **r['retained_prefix']},ensure_ascii=False,allow_nan=False)+'\n')
    write(args.output/'offline_replay_candidates.json',summary['offline_replay_numeric_candidates'])
    a,b = summary['cuts']['held_run_start'],summary['cuts']['extra_error_gt_2deg']
    lines = ['# 778份候选：末端手臂回位与底盘返程重叠', '',
        '**不能统一删掉最后3.8秒后就宣告全循环replay已就绪。** 3.8秒是末端lead恒定段时长的中位数，不是录制协议规定的尾长，更不等于底盘静止时间。', '',
        f"本次按用户新增排除000256及all_data_1的000000–000008，只读分析剩778份；没有改candidate或写训练mask。末端恒定段范围{summary['held_duration_s']['min']:.3f}–{summary['held_duration_s']['max']:.3f}秒，中位{summary['held_duration_s']['p50']:.3f}秒。", '',
        '| 假设的逻辑尾段起点 | 会截到仍非零底盘返程的录制 | 截到的返程剩余时间中位数 | 返程模板路径损失中位数（仅重叠来源） |',
        '| --- | ---: | ---: | ---: |',
        f"| lead开始恒定 | {a['sources_cutting_nonzero_base_samples']}/778 | {a['seconds_until_last_nonzero_base_overlapping_only']['p50']:.3f}s | {a['discarded_return_template_fraction_overlapping_only']['p50']*100:.1f}% |",
        f"| HAL离开固定目标已增加2°的诊断点 | {b['sources_cutting_nonzero_base_samples']}/778 | {b['seconds_until_last_nonzero_base_overlapping_only']['p50']:.3f}s | {b['discarded_return_template_fraction_overlapping_only']['p50']*100:.1f}% |", '',
        '这里的底盘距离、返程阶段与剩余时长全来自**后补标签**，不是实测移动。2°也是定位诊断点，不能作为未经核实的物理复位触发时间。', '',
        '## 末端在做什么', '',
        '数值上，普通lead目标保持上一组双臂位置，HAL却把双臂带回各录制十分相近的收拢姿态；这与独立复位路径执行动作、普通lead缓存没跟着更新的表现一致。lead表示命令候选，HAL表示机器人反馈，不能把二者当同一个字段。画面与候选代码可以支持“回位”的解释，但录制没保存这条实际命令路径，不能据此伪造命令。', '']
    for ep, r in summary['examples'].items():
        ex=r['example'];j=ARM_NAMES.index(ex['highlight_joint'])
        lines += [f"- `{ep}`：恒定尾段{r['held_start_s']:.3f}–{r['held_last_s']:.3f}s。{ex['highlight_joint']}的lead保持{ex['held_target_full_deg'][j]:.3f}°，HAL从{ex['start_hal_full_deg'][j]:.3f}°到{ex['last_hal_full_deg'][j]:.3f}°；后补返程结束在{r['return_end_log_relative_s']:.3f}s，与手臂尾段重叠。"]
    lines += ['', '## 可采用的处理方向', '',
        '- 原始录制完整保留。全维mask若从保守恒定点开始，明确标记为部分任务片段，不当完整返回循环。',
        '- 优先用当前离线图像/状态/动作replay定位真正回位边界，核实底盘标签；这是检查工具，不要求先宣布数据可训练。',
        '- 若要保留同时发生的底盘返程，需要支持逐动作维mask及对应reader/loss语义，仅屏蔽缺失普通命令监督的双臂维；仍需明确外部复位产生的状态对端到端策略的影响。',
        '- 若希望模型完整学习手臂回位，需要取得或验证实际复位目标来源；将HAL改成动作目标属于新的轨迹标签定义，不能悄悄替代原示教命令。',
        '- 真机replay还需要真实可执行动作、接口映射、节拍与现场启动条件；单纯删除尾部不能满足这些条件。', '',
        f"标签返程已经结束的70份包含64训练、6验证，具体名单和零速停留见[70份名单](base_stopped_before_tail.jsonl)。其保留部分已记录零底盘速度的末端停留，范围{summary['base_stopped_before_held_tail']['zero_base_time_before_cut_s']['min']:.3f}–{summary['base_stopped_before_held_tail']['zero_base_time_before_cut_s']['max']:.3f}秒，中位{summary['base_stopped_before_held_tail']['zero_base_time_before_cut_s']['p50']:.3f}秒。这仍不代表视觉已确认实际返回。",
        '首轮数值离线replay候选：[000083、000066、000002](offline_replay_candidates.json)，均为all_data_3训练分组、保留前段技术连续且末端已有零速；未获真机执行验收。', '',
        '逐条重叠、模板损失、chunk影响、两条详细时间线见[episodes.jsonl](episodes.jsonl)与[summary.json](summary.json)。本分析不改变最终方案或验收状态。']
    (args.output/'findings.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'sources':len(results), 'cuts':summary['cuts'], 'held_duration_s':summary['held_duration_s']},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
