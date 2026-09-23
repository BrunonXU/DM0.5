#!/usr/bin/env python3
"""Prepare isolated SDK requests and collect source-bound FK caches/IK evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from kinematics_sdk_worker import sha256

TASK = Path(__file__).resolve().parents[1]
LOW = np.array([-178, -94, -178, -145, -178, -60, -90], dtype=np.float64)
HIGH = np.array([178, 120, 178, 60, 178, 60, 90], dtype=np.float64)


def prepare(output):
    output.mkdir(parents=True, exist_ok=True)
    sources = sorted((TASK / 'results/base_relabel_v3_autonomous/cases').glob('*/composed/episode.npz'))
    if not sources:
        raise RuntimeError('No candidate sources found')
    entries = []
    count = 0
    with (output / 'requests.jsonl').open('x') as dst:
        def emit(req):
            nonlocal count
            dst.write(json.dumps(req, allow_nan=False, separators=(',', ':'))+'\n')
            count += 1
        for case, source in enumerate(sources):
            with np.load(source, allow_pickle=False) as z:
                state, action = z['state'].astype(np.float64), z['action'].astype(np.float64)
                mapped = np.clip(action[:, :14].reshape(-1, 2, 7), LOW, HIGH)
                sv = z['state_valid'][:, :14].reshape(-1, 2, 7).all(-1) & np.isfinite(state[:, :14].reshape(-1, 2, 7)).all(-1)
                av = z['action_candidate_valid'][:, :14].reshape(-1, 2, 7).all(-1) & np.isfinite(mapped).all(-1)
                entries.append({'case':case, 'case_name':source.parents[1].name,
                    'source_path':str(source), 'source_npz_sha256':sha256(source), 'frames':len(state),
                    'source_training_approved':bool(z['training_approved']),
                    'feedback_valid_per_arm':sv.sum(0).tolist(), 'command_valid_per_arm':av.sum(0).tolist()})
                for arm in (0, 1):
                    for i in np.flatnonzero(sv[:, arm]):
                        emit({'id':f'{case}:feedback:{i}', 'arm':arm, 'joints':state[i, arm*7:arm*7+7].tolist()})
                    for i in np.flatnonzero(av[:, arm]):
                        q = mapped[i, arm].tolist()
                        emit({'id':f'{case}:command:{i}', 'arm':arm, 'joints':q, 'reference':q})
                    # H50 blocks cover continuous valid arm targets, independently of chassis labels.
                    valid_starts = [i for i in range(len(state)-49) if sv[i,arm] and av[i:i+50,arm].all()]
                    selected = []
                    for i in valid_starts:
                        if not selected or i >= selected[-1]+50:
                            selected.append(i)
                    if valid_starts and valid_starts[-1] not in selected:
                        selected.append(valid_starts[-1])
                    for start in selected:
                        anchor = state[start, arm*7:arm*7+7].tolist()
                        chain = f'{case}:{arm}:{start}'
                        for k in range(50):
                            emit({'id':f'{case}:chunk:{start}:{k}', 'arm':arm,
                                  'joints':mapped[start+k,arm].tolist(), 'reference':anchor,
                                  'anchor_joints':anchor, 'chain':chain})
    manifest = {'schema':'f1_native_kinematics_requests_v1', 'sources':entries, 'requests':count,
                'execution_mapping':{'id':'controller_arm_clip_20260916','lower_deg':LOW.tolist(),'upper_deg':HIGH.tolist()},
                'source_script_sha256':sha256(__file__), 'requests_sha256':sha256(output/'requests.jsonl'),
                'training_approved':False, 'scope':'arm kinematics only; base label validity not assessed'}
    (output/'request_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'sources':len(entries),'requests':count,'path':str(output)}))


def stats(values):
    a = np.asarray(values, dtype=np.float64)
    if not len(a):
        return {'count':0}
    return {'count':len(a),'median':float(np.median(a)),'p95':float(np.quantile(a,.95)),
            'p99':float(np.quantile(a,.99)),'max':float(np.max(a))}


def collect(output):
    manifest = json.loads((output/'request_manifest.json').read_text())
    data = []
    for entry in manifest['sources']:
        if sha256(entry['source_path']) != entry['source_npz_sha256']:
            raise RuntimeError('Source changed during audit')
        with np.load(entry['source_path'],allow_pickle=False) as z:
            data.append({'time_ns':z['time_ns'].copy(),
                'feedback_pose':np.full((entry['frames'],2,4,4),np.nan),
                'command_pose':np.full((entry['frames'],2,4,4),np.nan),
                'feedback_valid':np.zeros((entry['frames'],2),bool),
                'command_valid':np.zeros((entry['frames'],2),bool),
                'raw_command_joints':z['action'][:,:14].astype(np.float64),
                'mapped_command_joints':np.clip(z['action'][:,:14].reshape(-1,2,7),LOW,HIGH).reshape(-1,14)})
    groups = {}
    bad = []
    seen = set()
    with (output/'responses.jsonl').open() as src:
        header = json.loads(next(src))
        if header['requests_sha256'] != manifest['requests_sha256']:
            raise RuntimeError('Worker used different requests')
        for line in src:
            row = json.loads(line)
            unique = (row['id'],row['arm'])
            if unique in seen:
                raise RuntimeError(f'Duplicate response {unique}')
            seen.add(unique)
            tokens = row['id'].split(':')
            case, kind, i, arm = int(tokens[0]),tokens[1],int(tokens[2]),row['arm']
            pose = np.asarray(row['pose'])
            if kind in ('feedback','command'):
                data[case][kind+'_pose'][i,arm] = pose
                data[case][kind+'_valid'][i,arm] = True
            if 'ik' not in row:
                continue
            ik = row['ik']
            key = f'{kind}_{"left" if arm==0 else "right"}'
            group = groups.setdefault(key,{'calls':0,'sdk_failed':0,'out_range':0,'sdk_joint_exceeded':0,
                'controller_joint_exceeded':0,'singular':0,'position_mm':[],'rotation_deg':[],
                'latency_us':[],'encoding_position_mm':[],'encoding_rotation_deg':[],
                'first_step_deg':[],'subsequent_step_deg':[],'pose_tolerance_failed':0})
            group['calls'] += 1
            group['sdk_failed'] += not ik['success']
            group['out_range'] += ik['out_range']
            group['sdk_joint_exceeded'] += ik['joint_exceeded']
            group['singular'] += any(ik['singular'])
            q = np.asarray(ik['joints'])
            exceeded = bool(np.any(q < LOW-1e-5) or np.any(q > HIGH+1e-5))
            group['controller_joint_exceeded'] += exceeded
            group['latency_us'].append(ik['latency_us'])
            group['encoding_position_mm'].append(row['encoding_error'][0])
            group['encoding_rotation_deg'].append(row['encoding_error'][1])
            tolerance_failed = False
            if 'pose_error' in ik:
                group['position_mm'].append(ik['pose_error'][0])
                group['rotation_deg'].append(ik['pose_error'][1])
                tolerance_failed = ik['pose_error'][0] > .1 or ik['pose_error'][1] > .1
            group['pose_tolerance_failed'] += tolerance_failed
            if kind == 'chunk' and ik['success']:
                step = float(np.max(np.abs(q-np.asarray(ik['reference']))))
                group['first_step_deg' if tokens[3]=='0' else 'subsequent_step_deg'].append(step)
            if not ik['success'] or ik['out_range'] or ik['joint_exceeded'] or exceeded or tolerance_failed:
                bad.append(row)
    if len(seen) != manifest['requests']:
        raise RuntimeError('Missing responses')
    # Exact identity coverage check, not just matching counts.
    with (output/'requests.jsonl').open() as src:
        expected = {(r['id'],r['arm']) for r in map(json.loads,src)}
    if seen != expected:
        raise RuntimeError('Response identities mismatch')
    cache_entries = []
    for entry, arrays in zip(manifest['sources'],data):
        folder = output/'pose_cache'/entry['case_name']
        folder.mkdir(parents=True,exist_ok=True)
        cache = folder/'pose_cache.npz'
        if cache.exists():
            raise FileExistsError(cache)
        np.savez_compressed(cache,**arrays)
        metadata = {**entry, 'schema':'f1_native_fk_pose_cache_v1','pose_unit':'m',
            'arm_order':['left','right'],'execution_mapping':manifest['execution_mapping'],
            'kinematics_contract':header['contract'],'kinematics_version':'native_sdk_20260916',
            'pose_cache_sha256':sha256(cache),'training_approved':False,
            'validation_scope':'FK numeric cache; IK and physical task readiness reported separately'}
        (folder/'pose_cache.json').write_text(json.dumps(metadata,indent=2)+'\n')
        cache_entries.append({'source_path':entry['source_path'],'cache_path':str(cache),'metadata_path':str(folder/'pose_cache.json')})
    for group in groups.values():
        for key,value in list(group.items()):
            if isinstance(value,list):
                group[key] = stats(value)
    report = {'schema':'f1_native_kinematics_audit_v1','sources':len(data),'responses':len(seen),
        'kinematics_contract':header['contract'],'groups':groups,'failure_records':len(bad),
        'numerical_diagnostic_tolerance':{'position_mm':.1,'rotation_deg':.1,'joint_limit_deg':1e-5},
        'tolerance_scope':'numerical roundtrip only, not physical collision or task acceptance',
        'training_approved':False,'robot_commands_sent':0,
        'request_manifest_sha256':sha256(output/'request_manifest.json'),
        'responses_sha256':sha256(output/'responses.jsonl'),'cache_entries':cache_entries}
    (output/'failures.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in bad))
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'responses':len(seen),'failures':len(bad),'groups':groups}))


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['prepare','collect'])
    p.add_argument('--output',type=Path,default=TASK/'results/kinematics_validation_20260916')
    args=p.parse_args()
    {'prepare':prepare,'collect':collect}[args.mode](args.output.resolve())
