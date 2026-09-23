#!/usr/bin/env python3
"""Freeze an event-complete demo subset, preserving source clocks and splits.

Offline source/FK preparation only; no robot commands and no training approval.
Base amplitude is reduced within each original phase to the explicit execution
bounds. This is a demo weak-label policy, not measured velocity recovery.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import numpy as np
from build_arm_guided_joint_proposals import TASK, sha, write
from compose_base_candidate import compose_arrays
sys.path.insert(0,str(TASK/'robot'))
from kinematics import ArmModel, LOW_DEG, HIGH_DEG
sys.path.insert(0,str(TASK/'models/DM0.5'))
from f1_data.reader import Episode, fit_statistics

CONTROLS=['all_data_3_derived/episode_000057','all_data_2_derived/episode_000003',
          'all_data_1_derived/episode_000020','all_data_1_derived/episode_000096',
          'all_data_derived/episode_000093','all_data_derived/episode_000192']


def arrays(p):
    with np.load(p,allow_pickle=False) as z:return {k:z[k].copy() for k in z.files}


def event_gate(data,stages,t):
    stage={m['role']:m for m in stages};n=len(t)
    cov=np.zeros(n,bool)
    for i in np.flatnonzero(data['chunk_start_candidate_valid']):cov[i:i+50]=True
    d=np.diff(data['action'][:,14],prepend=data['action'][0,14])
    events={'grasp':np.flatnonzero((t<stage['back_out']['start_s'])&(d>1)),
            'release':np.flatnonzero((t>stage['forward_out']['end_s'])&(t<stage['back_return']['start_s'])&(d< -1))}
    report={};passed=True
    for name,ix in events.items():
        complete=bool(len(ix) and cov[ix].all())
        stopped=bool(len(ix) and np.isfinite(data['action'][ix,16:]).all() and np.max(abs(data['action'][ix,16:]))<=1e-7)
        # Check intervening frames as well as command changes, including gradual opening.
        if len(ix):
            span=slice(int(ix[0]),int(ix[-1])+1)
            complete=complete and bool(cov[span].all())
            stopped=stopped and bool(np.isfinite(data['action'][span,16:]).all() and np.max(abs(data['action'][span,16:]))<=1e-7)
        report[name]=dict(indices=ix.tolist(),times_s=t[ix].tolist(),covered=complete,base_zero=stopped)
        passed=passed and complete and stopped
    movements={m['role']:int(np.sum(cov&(t>=m['start_s'])&(t<m['end_s']))) for m in stages}
    passed=passed and all(movements.values())
    report.update(movement_supervised_frames=movements,passed=bool(passed),
                  limits='command events locate grasp/release; not independent proof of physical material success')
    return report


def scale_base(data,stages,t,acceleration,speed):
    result={k:v.copy() for k,v in data.items()};records=[]
    for m in stages:
        begin,end=m['start_s'],m['end_s'];duration=end-begin
        take=(t>=begin)&(t<=end)&np.isfinite(data['action'][:,16:]).all(1)
        v=data['action'][take,16:]
        if not len(v):continue
        # Smoothstep derivative analytic peak; original stages are single-axis.
        axis=m['axis'];distance=abs(m['distance_prior_m'])
        peak_a=6*distance/duration**2;peak_v=1.5*distance/duration
        scale=min(1.,acceleration/peak_a,speed/peak_v)
        if scale<1:scale*=1-1e-6  # Float32 export margin, not a controller tuning change.
        result['action'][take,16:]=v*scale
        records.append(dict(role=m['role'],start_s=begin,end_s=end,scale=scale,
                            prior_distance_m=m['distance_prior_m'],new_prior_distance_m=m['distance_prior_m']*scale,
                            analytic_peak_acceleration=peak_a*scale,analytic_peak_speed=peak_v*scale))
    a=result['action'][:,16:];finite=np.isfinite(a).all(1)
    pair=finite[1:]&finite[:-1]
    diff=np.linalg.norm(np.diff(a,axis=0)[pair],axis=1)/np.diff(t)[pair]
    if len(diff):assert np.max(diff)<=acceleration+1e-5,('discrete acceleration exceeds bound',float(np.max(diff)))
    assert np.array_equal(result['action'][:,:16],data['action'][:,:16],equal_nan=True)
    return result,dict(stages=records,max_discrete_acceleration=float(max(diff,default=0)),
                      acceleration_limit=acceleration,speed_limit=speed,
                      interpretation='amplitude_limited_demo_weak_labels_not_measured_motion',
                      source_clock_unchanged=True,controller_parameters_changed=False)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--proposal-dirs',type=Path,nargs='+',required=True)
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--base-acceleration-limit',type=float,required=True)
    ap.add_argument('--base-speed-limit',type=float,required=True);args=ap.parse_args()
    assert args.base_acceleration_limit>0 and args.base_speed_limit>0
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    masks={r['episode']:r for r in map(json.loads,(TASK/'results/demo_772_selection/mask_manifest.jsonl').read_text().splitlines())}
    rows={r['episode']:r for r in map(json.loads,(TASK/'results/demo_772_selection/candidates.jsonl').read_text().splitlines())}
    sources={};selection=[]
    for root in args.proposal_dirs:
        summary=json.loads((root/'summary.json').read_text())
        for r in summary['results']:
            if r['status']!='joint_timing_proposal':continue
            ep=r['episode'];p=Path(masks[ep]['aligned_npz_path']);assert sha(p)==masks[ep]['aligned_npz_sha256']
            pp=root/ep.replace('/','_')/'proposal.npz';assert sha(pp)==r['npz_sha256']
            parent=arrays(p);proposal=arrays(pp);v=proposal['base_velocity_proposal'];present=np.isfinite(v).all(1)
            data=compose_arrays(parent,dict(time_ns=proposal['time_ns'],base_candidate_v=v,
                                base_candidate_present=present,base_timing_reviewed=present),50)
            assert np.array_equal(data['chunk_start_candidate_valid'],proposal['proposed_chunk_start'])
            sources[ep]=(data,json.loads(p.with_suffix('.json').read_text()),r['motion_stages'],
                         dict(kind='automated_timing_with_image_verified_short_gaps',source=str(p.resolve()),source_sha256=sha(p),
                              proposal=str(pp.resolve()),proposal_sha256=sha(pp),report=str((pp.parent/'report.json').resolve()),report_sha256=sha(pp.parent/'report.json')))
    for ep in CONTROLS:
        folder=TASK/'results/base_relabel_v3_autonomous/cases'/ep.replace('/','_');p=folder/'composed/episode.npz'
        phases=json.loads((folder/'phase_review.json').read_text())
        stages=[dict(role=s['id'],start_s=s['start_s'],end_s=s['end_s'],axis=0 if s['dx_m'] else 1,
                     distance_prior_m=s['dx_m'] or s['dy_m']) for s in phases['segments'] if s['kind']=='motion']
        sources[ep]=(arrays(p),json.loads(p.with_suffix('.json').read_text()),stages,
                     dict(kind='existing_direct_visual_review',source=str(p),source_sha256=sha(p),
                          phase_review=str(folder/'phase_review.json'),phase_sha256=sha(folder/'phase_review.json')))
    config=TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg'
    model=ArmModel(config)
    template=json.loads((TASK/'results/kinematics_validation_20260916/pose_cache/all_data_3_derived_episode_000057/pose_cache.json').read_text())
    accepted=[]
    for ep,(data,meta,stages,provenance) in sorted(sources.items()):
        t=(data['time_ns']-rows[ep]['source_start_ns'])/1e9;gate=event_gate(data,stages,t)
        entry=dict(episode=ep,split=meta['split'],gate=gate,provenance=provenance)
        if not gate['passed']:entry['status']='excluded_event_coverage_or_stop_conflict';selection.append(entry);continue
        data,dynamics=scale_base(data,stages,t,args.base_acceleration_limit,args.base_speed_limit)
        n=len(t);name=ep.replace('/','_');dest=out/'cases'/name/'composed';dest.mkdir(parents=True)
        data['training_approved']=np.array(False)
        assert not data['action_candidate_valid'][masks[ep]['tail_mask_start_index']:].any()
        meta.update(version='demo_event_complete_20260917_v1',cleaning_version='demo_event_complete_20260917_v1',
                    training_approved=False,provenance=provenance,event_coverage=gate,base_dynamics=dynamics,
                    base_label_origin='posthoc_timing_and_amplitude_limited_route_prior',
                    chunk_candidate_starts=int(data['chunk_start_candidate_valid'].sum()))
        np.savez_compressed(dest/'episode.npz',**data);write(dest/'episode.json',meta)
        poses=dict(time_ns=data['time_ns'],raw_command_joints=data['action'][:,:14].astype(float),
                   mapped_command_joints=np.clip(data['action'][:,:14].reshape(n,2,7),LOW_DEG,HIGH_DEG).reshape(n,14))
        for kind,key,mask in [('feedback','state','state_valid'),('command','action','action_candidate_valid')]:
            values=data[key][:,:14].reshape(n,2,7)
            if kind=='command':values=np.clip(values,LOW_DEG,HIGH_DEG)
            valid=data[mask][:,:14].reshape(n,2,7).all(2)&np.isfinite(values).all(2)
            pose=np.full((n,2,4,4),np.nan)
            for i,arm in np.argwhere(valid):pose[i,arm]=model.fk(int(arm),values[i,arm])
            poses[kind+'_pose']=pose;poses[kind+'_valid']=valid
        cache=out/'pose_cache'/name;cache.mkdir(parents=True);np.savez_compressed(cache/'pose_cache.npz',**poses)
        pm={k:template[k] for k in ['kinematics_contract','execution_mapping','pose_unit','arm_order']}
        pm.update(schema='f1_local_verified_dh_fk_cache_v1',source_npz_sha256=sha(dest/'episode.npz'),
                  pose_cache_sha256=sha(cache/'pose_cache.npz'),training_approved=False,
                  implementation=str(TASK/'robot/kinematics.py'),implementation_sha256=sha(TASK/'robot/kinematics.py'),
                  config_sha256=sha(config),parity_evidence=str(TASK/'results/continuous_ik_20260916/README.md'))
        write(cache/'pose_cache.json',pm)
        episode=Episode(dest/'episode.npz',cache/'pose_cache.npz',allow_candidates=True)
        assert np.array_equal(episode.starts,np.flatnonzero(data['chunk_start_candidate_valid']))
        entry.update(status='offline_prepared',valid_starts=len(episode.starts),dynamics=dynamics)
        accepted.append(episode);selection.append(entry)
        write(out/'progress.json',dict(prepared=len(accepted),examined=len(selection),scope=len(sources)))
    stats=fit_statistics([e for e in accepted if e.split=='train']);write(out/'normalization.json',stats)
    write(out/'selection.json',dict(schema='f1_demo_event_complete_selection_v1',training_approved=False,
          accepted=len(accepted),by_split={s:sum(e.split==s for e in accepted) for s in ['train','validation']},
          total_starts=sum(len(e.starts) for e in accepted),records=selection,
          unmaterialized_sources_not_admitted=sorted(set(rows)-set(sources)),
          script_sha256=sha(__file__),controls_excluded=['058 uncertain release','135 unknown middle segment']))
    print(json.dumps(dict(prepared=len(accepted),starts=sum(len(e.starts) for e in accepted),training_approved=False)))

if __name__=='__main__':main()
