"""Read-only, source-timed episode diagnostics. No ROS or command publishing.

Derivatives are local cubic least-squares estimates on 11 actual timestamps;
five boundary frames on either side are unknown, never padded with zeros.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pyarrow.parquet as pq

from pico_f1.lerobot_v3 import CAMERA_KEYS
from pico_f1.session_pose import SESSION_JOINT_NAMES
from pico_f1.alignment_archive import read_extras, validate_coverage

FEATURES = list(SESSION_JOINT_NAMES) + ['base_vx_m_s', 'base_vy_m_s', 'base_yaw_rate_rad_s']
STAGES = ['1 抓取手机（待确认）', '2 移动到放置点（待确认）',
          '3 放置手机（待确认）', '4 返回并复位（待确认）']


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError('path escapes dataset')
    return path


def episode_paths(root, row, info):
    data = contained(root, info['data_path'].format(chunk_index=int(row['data/chunk_index']),
                                                   file_index=int(row['data/file_index'])))
    videos = []
    for key in CAMERA_KEYS:
        videos.append(contained(root, info['video_path'].format(video_key=key,
            chunk_index=int(row[f'videos/{key}/chunk_index']),
            file_index=int(row[f'videos/{key}/file_index']))))
    return data, videos


def polynomial_derivatives(t, values, window=11):
    values = np.asarray(values, dtype=float)
    if values.ndim == 1:
        values = values[:, None]
    out = [np.full_like(values, np.nan) for _ in range(4)]
    half = window // 2
    for i in range(half, len(t) - half):
        sl = slice(i-half, i+half+1)
        x = t[sl] - t[i]
        if np.any(np.diff(t[sl]) <= 0) or np.max(np.diff(t[sl])) > .1:
            continue
        y = values[sl]
        good = np.all(np.isfinite(y), axis=0)
        if not good.any():
            continue
        scale = max(abs(x).max(), 1e-9)
        coeff = np.linalg.lstsq(np.vander(x/scale, 4, increasing=True), y[:, good], rcond=None)[0]
        for order in range(4):
            out[order][i, good] = coeff[order] * math.factorial(order) / scale**order
    return out


def infer_stages(t, base):
    """Two sustained motion bouts imply four candidate phases, NOT task labels."""
    active = (np.linalg.norm(base[:, :2], axis=1) > .04) | (np.abs(base[:, 2]) > .08)
    # Join turn/translate pauses <=2.5 s, retaining sustained manipulation stops.
    starts = np.flatnonzero(active & ~np.r_[False, active[:-1]])
    ends = np.flatnonzero(active & ~np.r_[active[1:], False])
    runs = []
    for start, end in zip(starts, ends):
        if runs and t[start] - t[runs[-1][1]] <= 2.5:
            runs[-1][1] = int(end)
        else:
            runs.append([int(start), int(end)])
    runs = [r for r in runs if t[r[1]] - t[r[0]] >= .6]
    if len(runs) < 2:
        return [], '不足两段持续底盘运动，无法可靠推断四阶段；请人工标注'
    chosen = sorted(sorted(runs, key=lambda r: t[r[1]]-t[r[0]], reverse=True)[:2])
    boundaries = [0., float(t[chosen[0][0]]), float(t[chosen[0][1]]),
                  float(t[chosen[1][0]]), float(t[-1])]
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        return [], '阶段边界退化，需人工标注'
    return [dict(name=STAGES[i], start=boundaries[i], end=boundaries[i+1], inferred=True)
            for i in range(4)], '底盘阈值 + 2.5秒内停顿合并 + 两段最长运动启发式；不证明抓取或放置成功'


def video_header(path):
    if not path.is_file():
        return dict(status='missing', detail='视频文件不存在')
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,avg_frame_rate,nb_frames,duration,codec_name',
            '-of', 'json', str(path)], capture_output=True, text=True, timeout=12, check=True)
        stream = json.loads(result.stdout)['streams'][0]
        numerator, denominator = stream.get('avg_frame_rate', '0/1').split('/')
        return dict(status='container_checked', width=stream['width'], height=stream['height'],
                    fps=float(numerator)/float(denominator),
                    frames=int(stream['nb_frames']) if stream.get('nb_frames', 'N/A') != 'N/A' else None,
                    codec=stream.get('codec_name'), duration=float(stream.get('duration', 0)),
                    detail='容器元信息检查；未逐帧解码，不能证明无损坏帧')
    except Exception as exc:
        return dict(status='unavailable', detail=str(exc)[:240])


def analyze_episode(root, row, info, probe=True):
    root = Path(root).resolve()
    episode = int(row['episode_index'])
    data_path, video_paths = episode_paths(root, row, info)
    if info['features']['observation.state']['names'] != FEATURES or info['features']['action']['names'] != FEATURES:
        raise ValueError('27D feature names/order changed; refusing silent remap')
    table = pq.read_table(data_path, filters=[('episode_index', '=', episode)])
    a = table.to_pydict()
    q, cmd = np.asarray(a['observation.state'], float), np.asarray(a['action'], float)
    n = len(q)
    if n < 2 or q.shape != (n, 27) or cmd.shape != q.shape:
        raise ValueError('insufficient frames or incompatible feature shape')
    source = np.asarray(a['source.timestamp_ns'], np.int64)
    t = (source - source[0]).astype(float) / 1e9
    dt = np.diff(t)
    problems, notes = [], []
    if n != int(row['length']): problems.append('Parquet 行数与 episode length 不一致')
    if not np.array_equal(a['frame_index'], np.arange(n)): problems.append('frame_index 不连续/重复')
    if not np.array_equal(a['index'], np.arange(int(row['dataset_from_index']), int(row['dataset_to_index']))):
        problems.append('全局 index 与元数据范围不一致')
    if not np.all(np.isfinite(q)) or not np.all(np.isfinite(cmd)): problems.append('state/action 有缺失或非有限数')
    if np.any(dt <= 0): problems.append('源时间戳重复或倒退')
    nominal = np.asarray(a['timestamp'], float)
    fps = float(info['fps'])
    if not np.allclose(nominal, np.arange(n)/fps, atol=2e-4): problems.append('LeRobot 时间轴不符合声明 fps')
    effective = (n-1)/t[-1] if t[-1] > 0 else None
    if effective is None or abs(effective/fps - 1) > .05: problems.append('源采样率偏离声明 fps 超过 5%')
    cams = np.asarray(a['source.camera_timestamp_ns'], np.int64)
    integrity = np.asarray(a['source.camera_integrity_timestamp_ns'], np.int64)
    skew = np.ptp(cams, axis=1) / 1e6
    if np.max(skew) > 50: problems.append('三相机源时间差超过 50 ms')
    ages = {}
    for col in ('state', 'action'):
        age = np.asarray(a[f'source.{col}_max_age_ns'], np.int64) / 1e6
        ages[col] = dict(max_ms=float(age.max()), p95_ms=float(np.percentile(age, 95)))
        if np.any(age < 0) or np.max(age) > 50: problems.append(f'{col} 对齐年龄不在 0–50 ms')
    extras = []
    try:
        extras = read_extras(root/'alignment'/f'episode-{episode:06d}')
        validate_coverage(integrity, extras, 55_000_000)
    except Exception as exc:
        problems.append('源帧覆盖/补充帧校验: ' + str(exc))
    cameras = []
    for i, (key, path) in enumerate(zip(CAMERA_KEYS, video_paths)):
        meta = video_header(path) if probe else dict(status='not_checked', detail='未检查视频')
        same_file = float(row[f'videos/{key}/from_timestamp']) == 0 and abs(float(row[f'videos/{key}/to_timestamp']) - n/fps) < .01
        if meta['status'] == 'missing': problems.append(key + ' 视频缺失')
        if meta['status'] == 'unavailable': problems.append(key + ' 视频容器检查未完成')
        if meta.get('fps') is not None and abs(meta['fps']-fps) > .05: problems.append(key + ' 视频 fps 不一致')
        if same_file and meta.get('frames') is not None and meta['frames'] != n: problems.append(key + ' 视频/动作帧数不一致')
        if meta.get('duration', 0) and float(row[f'videos/{key}/to_timestamp']) > meta['duration'] + 2/fps:
            problems.append(key + ' 视频时长不足以覆盖 episode')
        selected_gaps = np.diff(integrity[:, i])/1e6
        cameras.append(dict(key=key, **meta, expected_episode_frames=n,
            frame_count_check='容器帧数比对' if same_file and meta.get('frames') is not None else '未验证：容器无独立episode帧数',
            max_selected_gap_ms=float(selected_gaps.max()),
            duplicate_or_reverse=int(np.sum(selected_gaps <= 0)),
            extras=sum(e['camera'] == key for e in extras),
            video_offset_s=float(row[f'videos/{key}/from_timestamp'])))
    derivatives = polynomial_derivatives(t, q) if np.all(dt > 0) else [np.full_like(q, np.nan)]*4
    base_derivatives = derivatives
    # Local root displacement is velocity integration, NOT measured world pose.
    root_pose = np.zeros((n, 3))
    if np.all(dt > 0) and np.all(np.isfinite(q[:, 24:])):
        for i in range(1, n):
            v = (q[i-1, 24:] + q[i, 24:])*.5
            yaw = root_pose[i-1, 2] + .5*v[2]*dt[i-1]
            c, s = np.cos(yaw), np.sin(yaw)
            root_pose[i] = root_pose[i-1] + dt[i-1]*np.array([c*v[0]-s*v[1], s*v[0]+c*v[1], v[2]])
    else:
        root_pose[:] = np.nan
    stages, stage_note = infer_stages(t, q[:, 24:]) if np.all(dt > 0) else ([], '时间轴无效')
    metrics = []
    for i, name in enumerate(FEATURES):
        orders = [q[:, i], derivatives[1][:, i], derivatives[2][:, i], derivatives[3][:, i]]
        if i >= 24:
            orders = [root_pose[:, i-24], q[:, i], base_derivatives[1][:, i], base_derivatives[2][:, i]]
        valid = [v[np.isfinite(v)] for v in orders]
        metrics.append(dict(name=name, position_span=float(np.ptp(valid[0])) if len(valid[0]) else None,
            velocity_p95=float(np.percentile(abs(valid[1]), 95)) if len(valid[1]) else None,
            acceleration_p95=float(np.percentile(abs(valid[2]), 95)) if len(valid[2]) else None,
            jerk_p95=float(np.percentile(abs(valid[3]), 95)) if len(valid[3]) else None,
            jerk_peak=float(np.max(abs(valid[3]))) if len(valid[3]) else None,
            tracking_rmse=float(np.sqrt(np.mean((q[:, i]-cmd[:, i])**2))),
            units='deg' if i < 22 else ('%' if i < 24 else ('rad' if i == 26 else 'm'))))
    curves = []
    # Keep all samples: no hidden downsampling of peaks or phase boundaries.
    for k in range(n):
        curves.append(dict(t=float(t[k]), q=q[k], command=cmd[k],
            v=derivatives[1][k], a=derivatives[2][k], j=derivatives[3][k],
            root=root_pose[k], skew_ms=float(skew[k])))
    summary = dict(episode=episode, frames=n, duration_s=float(t[-1]), declared_fps=fps,
        effective_fps=effective, source_start_ns=int(source[0]),
        captured_at=datetime.fromtimestamp(int(source[0])/1e9, timezone.utc).isoformat(),
        quality='问题' if problems else '表格/同步检查通过', issues=problems,
        video_audit=('容器检查完成' if all(c['status'] == 'container_checked' for c in cameras)
                     else '部分或全部视频未检查'), stage_status='待确认' if stages else '无法分段',
        linear_path_m=float(np.trapezoid(np.linalg.norm(q[:, 24:26], axis=1), t)),
        yaw_travel_rad=float(np.trapezoid(np.abs(q[:, 26]), t)),
        arm_tracking_rmse_deg=float(np.sqrt(np.mean((q[:, :14]-cmd[:, :14])**2))),
        idle_ratio=float(np.mean((np.linalg.norm(q[:, 24:26], axis=1) < .04) & (np.abs(q[:, 26]) < .08))),
        max_camera_skew_ms=float(skew.max()), max_source_gap_ms=float(dt.max()*1000),
        return_xy_estimate_m=float(np.linalg.norm(root_pose[-1, :2])),
        arm_return_rmse_deg=float(np.sqrt(np.mean((q[-1, :14]-q[0, :14])**2))))
    result = clean(dict(summary=summary, features=FEATURES, curves=curves, metrics=metrics,
        stages=stages, stage_note=stage_note, cameras=cameras, ages=ages,
        provenance=dict(data=str(data_path.relative_to(root)), grain='one source-timed frame',
            derivative='11-frame local cubic least squares; 5-frame edges unknown',
            root='body-frame velocity integrated in episode-start frame; drift accumulates',
            limits='无物体真值/成功标签；无全局关节根位置实测；jerk 是滤波估计而非电机内环值'), notes=notes))
    result['phase_metrics'] = phase_statistics(result['curves'], result['stages'])
    return result


def phase_statistics(curves, stages):
    result = []
    for k, stage in enumerate(stages):
        part = [r for r in curves if stage['start'] <= r['t'] and
                (r['t'] < stage['end'] or k == len(stages)-1 and r['t'] == stage['end'])]
        if not part:
            continue
        t = np.array([r['t'] for r in part])
        q = np.array([r['q'] for r in part], float)
        cmd = np.array([r['command'] for r in part], float)
        values = dict(stage=stage['name'], start=stage['start'], end=stage['end'],
            duration_s=stage['end']-stage['start'], frames=len(part),
            arm_rmse_deg=float(np.sqrt(np.mean((q[:, :14]-cmd[:, :14])**2))),
            linear_path_m=float(np.trapezoid(np.linalg.norm(q[:, 24:26], axis=1), t)))
        for field, name in [('v','arm_velocity_p95_deg_s'),('a','arm_acceleration_p95_deg_s2'),('j','arm_jerk_p95_deg_s3')]:
            a = np.abs(np.array([r[field][:14] for r in part], float))
            valid = a[np.all(np.isfinite(a), axis=1)]
            values[name] = float(np.percentile(np.max(valid, axis=1),95)) if len(valid) else None
        result.append(values)
    return clean(result)


def population_statistics(rows):
    good = [r for r in rows if r.get('duration_s') is not None]
    results = {}
    for key in ('duration_s', 'linear_path_m', 'yaw_travel_rad', 'arm_tracking_rmse_deg'):
        members = [(r['episode'], r.get(key)) for r in good if r.get(key) is not None and math.isfinite(r[key])]
        v = np.array([x[1] for x in members])
        if not len(v): continue
        q1, median, q3, p95 = np.percentile(v, [25, 50, 75, 95])
        fence = q3 + 1.5*(q3-q1)
        results[key] = dict(n=len(v), median=median, p95=p95, upper_fence=fence,
            tail_candidates=[eid for eid, value in members if value > fence] if len(v) >= 20 and q3 > q1 else [],
            assessment='IQR 高尾候选，不是坏数据' if len(v) >= 20 and q3 > q1 else '样本不足20条或IQR退化，仅描述分布')
    return clean(results)
