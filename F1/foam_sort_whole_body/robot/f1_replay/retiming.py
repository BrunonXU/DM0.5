"""Explicit replay-only joint timeline retiming; never rewrites training sources."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
from .bundle import ReplayBundle, ReplayFrame, decode_json
from .runtime import ReplayError


def apply_retiming(bundle, plan_path):
    raw = Path(plan_path).read_bytes()
    plan = decode_json(raw)
    if (plan.get('schema') != 'f1_replay_retiming_v1'
            or plan.get('scope') != 'replay_diagnostic_not_training'
            or plan.get('parent_manifest_sha256') != bundle.sha256
            or plan.get('episode') != bundle.manifest['source']['episode']):
        raise ReplayError('重定时计划与源包身份或用途不匹配')
    if bundle.manifest['status'] != 'replay_ready':
        raise ReplayError('重定时不能批准尚未核对的源包')
    hz = plan.get('hz')
    if type(hz) is not int or hz != 30:
        raise ReplayError('当前重定时只支持已核对的30Hz源')
    limits = plan.get('limits', {})
    for k in ['acceleration', 'deceleration', 'speed']:
        v = limits.get(k)
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            raise ReplayError('无效底盘动态限制')
    n = len(bundle.frames)
    for i, f in enumerate(bundle.frames):
        if abs(f.time_ns - round(i*1e9/hz)) > 2:
            raise ReplayError('源不是连续30Hz，拒绝跨缺口插补')
    if abs(bundle.duration_ns - round(n*1e9/hz)) > 2:
        raise ReplayError('源尾边界不是完整采样周期')
    phases = plan.get('phases')
    if not isinstance(phases, list) or not phases:
        raise ReplayError('缺少具名移动阶段')
    segments, cursor, ids = [], 0, set()
    for p in phases:
        a, b, count = [p.get(k) for k in ['start_index', 'stop_index_exclusive', 'output_intervals']]
        if (any(type(x) is not int for x in [a,b,count]) or not cursor <= a < b <= n
                or count < b-a or count > 30000 or not p.get('id') or p['id'] in ids):
            raise ReplayError('阶段倒序、重叠、加速、重复或长度无效')
        ids.add(p['id'])
        delta = p.get('delta_xy_m')
        if (not isinstance(delta,list) or len(delta)!=2
                or any(type(x) not in (int,float) or not math.isfinite(x) for x in delta)
                or sum(x != 0 for x in delta)!=1):
            raise ReplayError('仅支持无旋转单轴阶段')
        d = abs(sum(delta)); T = count/hz
        if (1.5*d/T > limits['speed'] + 1e-10
                or 6*d/T**2 > min(limits['acceleration'],limits['deceleration']) + 1e-10):
            raise ReplayError('重定时阶段仍超过动态限制')
        # The reviewed source must support the signed distance/direction; do not
        # permit an unrelated plan to invent a leg or rescale distance unnoticed.
        integral = [sum(f.action[16+j]/hz for f in bundle.frames[a:b]) for j in range(2)]
        if any(abs(x-y)>0.002 for x,y in zip(integral,delta)):
            raise ReplayError('阶段距离与已核对源命令积分不符')
        if a > cursor: segments.append((cursor,a,a-cursor,None))
        segments.append((a,b,count,p));cursor=b
    if cursor < n:segments.append((cursor,n,n-cursor,None))
    if sum(s[2] for s in segments)>30000:raise ReplayError('计划超出诊断回放长度')
    def interpolate(index):
        low=min(int(math.floor(index)),n-1);high=min(low+1,n-1);u=index-low
        left,right=bundle.frames[low],bundle.frames[high]
        state=tuple(x+(y-x)*u for x,y in zip(left.state,right.state))
        action=tuple(x+(y-x)*u for x,y in zip(left.action[:16],right.action[:16]))
        source=round(left.source_time_ns+(right.source_time_ns-left.source_time_ns)*u)
        return state, action, source
    def progress(u): return u*u*(3-2*u)
    frames, mapping = [], []
    for a,b,count,p in segments:
        first=len(frames)
        for j in range(count):
            index=a+j*(b-a)/count
            state, upper, source = interpolate(index)
            if p:
                # Exact interval-average velocity preserves commanded distance
                # under zero-order hold, including first and last samples.
                scale=(progress((j+1)/count)-progress(j/count))*hz
                base=(p['delta_xy_m'][0]*scale,p['delta_xy_m'][1]*scale,0.0)
            else:
                if any(abs(x)>1e-9 for x in bundle.frames[int(index)].action[16:]):
                    raise ReplayError('未声明区间存在底盘运动；拒绝补零')
                base=(0.0,0.0,0.0)
            k=len(frames)
            frames.append(ReplayFrame(round(k*1e9/hz),source,state,tuple(upper)+base))
        mapping.append({'id':p['id'] if p else 'source_stationary',
                        'source_start_index':a,'source_stop_index_exclusive':b,
                        'output_start_index':first,'output_stop_index_exclusive':len(frames),
                        'duration_s':count/hz,'distance_xy_m':p['delta_xy_m'] if p else [0,0]})
    metadata={'kind':'f1_retimed_joint_replay','status':bundle.manifest['status'],
              'scope':plan['scope'],'training_approved':False,
              'source':copy.deepcopy(bundle.manifest['source']),
              'parent_manifest_sha256':bundle.sha256,'parent_manifest':bundle.manifest,
              'execution_contract':copy.deepcopy(bundle.manifest.get('execution_contract',{})),
              'duration_ns':round(len(frames)*1e9/hz),'frames':{'count':len(frames)},
              'retiming':{'plan_sha256':hashlib.sha256(raw).hexdigest(),'plan':plan,'mapping':mapping,
                          'source_time_ns':'interpolated_original_recording_time',
                          'time_ns':'synthetic_execution_time_not_original_log_time',
                          'upper_body':'piecewise_linear_joint_and_gripper_interpolation_with_shared_time_map',
                          'base':'interval_average_derivative_of_reviewed_distance_smoothstep'}}
    identity=hashlib.sha256(json.dumps(metadata,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return ReplayBundle(frames,metadata['duration_ns'],metadata,identity,bundle.path)
