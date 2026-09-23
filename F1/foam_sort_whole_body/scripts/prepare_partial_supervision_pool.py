#!/usr/bin/env python3
"""Preserve valid arm windows independently of missing posthoc chassis labels."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import numpy as np
from build_arm_guided_joint_proposals import TASK, sha, write
sys.path.insert(0,str(TASK/'models/DM0.5'))
sys.path.insert(0,str(TASK/'robot'))
from f1_data.reader import Episode, PARTIAL_BASE_CONTRACT
from kinematics import ArmModel, LOW_DEG, HIGH_DEG

OLD=TASK/'results/full_pool_reader_20260917_v1'


def prepare(job):
    row,out=job;ep=row['episode'];stem=ep.replace('/','_')
    source=OLD/'cases'/stem/'composed/episode.npz'
    if not source.exists():source=TASK/'results/partial_phase_pool_20260917_v1/cases'/stem/'composed/episode.npz'
    meta=json.loads(source.with_suffix('.json').read_text())
    with np.load(source,allow_pickle=False)as z:data={k:z[k]for k in z.files}
    assert sha(row['aligned_npz_path'])==row['aligned_npz_sha256']
    with np.load(row['aligned_npz_path'],allow_pickle=False)as z:parent={k:z[k]for k in z.files}
    for k in ['time_ns','state','state_valid','camera_source_indices','camera_timestamp_candidate_valid','observation_candidate_valid']:
        assert np.array_equal(data[k],parent[k],equal_nan=True),(ep,k)
    assert np.array_equal(data['action'][:,:16],parent['action'][:,:16],equal_nan=True)
    assert np.array_equal(data['action_candidate_valid'][:,:16],parent['action_candidate_valid'][:,:16])
    # Retain original conservative body/clock/arm/semantic restrictions. The
    # strict all-dimension mask stays intact for legacy inspection/replay.
    data['upper_body_chunk_start_candidate_valid']=parent['chunk_start_candidate_valid'].copy()
    dest=out/'cases'/stem/'composed';dest.mkdir(parents=True)
    np.savez_compressed(dest/'episode.npz',**data)
    meta.update(reader_contract=PARTIAL_BASE_CONTRACT,training_approved=False,
        upper_body_window_parent=dict(path=row['aligned_npz_path'],sha256=row['aligned_npz_sha256']),
        supervision_policy='unknown chassis dimensions are masked; original arm/clock/reset restrictions retained',
        missing_state_encoding='state19 tokens: unknown for missing chassis; numeric zeros are containers only')
    write(dest/'episode.json',meta)
    cache=out/'pose_cache'/stem;cache.mkdir(parents=True)
    oldcache=OLD/'pose_cache'/stem
    if oldcache.exists():
        pm=json.loads((oldcache/'pose_cache.json').read_text())
        assert pm['source_npz_sha256']==sha(source)
        assert pm['pose_cache_sha256']==sha(oldcache/'pose_cache.npz')
        shutil.copy2(oldcache/'pose_cache.npz',cache/'pose_cache.npz')
        pm.update(source_npz_sha256=sha(dest/'episode.npz'),source_rebinding_evidence=dict(
            previous_source=str(source),previous_source_sha256=sha(source),
            feedback_commands_and_masks_unchanged=True),training_approved=False)
    else:
        cfg=TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg'
        model=ArmModel(cfg);n=len(data['time_ns']);mapped=np.clip(data['action'][:,:14].reshape(n,2,7),LOW_DEG,HIGH_DEG)
        poses=dict(time_ns=data['time_ns'],raw_command_joints=data['action'][:,:14].astype(float),mapped_command_joints=mapped.reshape(n,14))
        for kind,key,mask in [('feedback','state','state_valid'),('command','action','action_candidate_valid')]:
            values=data[key][:,:14].reshape(n,2,7)if kind=='feedback'else mapped
            valid=data[mask][:,:14].reshape(n,2,7).all(2)&np.isfinite(values).all(2)
            pose=np.full((n,2,4,4),np.nan)
            for arm in range(2):
                ix=np.flatnonzero(valid[:,arm]);unique,inverse=np.unique(values[ix,arm],axis=0,return_inverse=True)
                if len(unique):pose[ix,arm]=np.array([model.fk(arm,q)for q in unique])[inverse]
            poses[kind+'_pose']=pose;poses[kind+'_valid']=valid
        np.savez_compressed(cache/'pose_cache.npz',**poses)
        template=json.loads((OLD/'pose_cache/all_data_3_derived_episode_000057/pose_cache.json').read_text())
        pm={k:template[k]for k in ['kinematics_contract','execution_mapping','pose_unit','arm_order']}
        pm.update(source_npz_sha256=sha(dest/'episode.npz'),pose_cache_sha256=sha(cache/'pose_cache.npz'),
            training_approved=False,implementation_sha256=sha(TASK/'robot/kinematics.py'),config_sha256=sha(cfg))
    write(cache/'pose_cache.json',pm)
    reader=Episode(dest/'episode.npz',cache/'pose_cache.npz',allow_candidates=True)
    assert np.array_equal(reader.starts,np.flatnonzero(parent['chunk_start_candidate_valid'])),ep
    counts={}
    for arm,sl in [('left',slice(0,7)),('right',slice(7,14))]:
        active=np.ptp(np.lib.stride_tricks.sliding_window_view(data['action'][:,sl],50,axis=0),axis=-1).max(1)>5
        counts[arm]=int(active[reader.starts].sum())
    for i in np.unique(reader.starts[[0,len(reader.starts)//2,-1]]):
        s=reader.sample(i);assert s['action_mask'][:,:20].all()
    return dict(episode=ep,split=reader.split,status='offline_prepared',valid_starts=len(reader.starts),
        source_sha256=sha(dest/'episode.npz'),provenance=str(source),provenance_sha256=sha(source),
        known_base_current_starts=int(reader.base_valid[reader.starts].sum()),arm_transition_starts=counts,
        original_upper_body_windows_preserved=True)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--workers',type=int,default=4)
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    rows=list(map(json.loads,(TASK/'results/demo_772_selection/mask_manifest.jsonl').read_text().splitlines()));results=[]
    with ProcessPoolExecutor(max_workers=args.workers)as pool:
        for r in pool.map(prepare,[(r,out)for r in rows]):
            results.append(r);write(out/'progress.json',dict(complete=len(results),total=len(rows)))
    summary=dict(schema='f1_partial_base_supervision_pool_v2',training_approved=False,accepted=len(results),
        total_starts=sum(r['valid_starts']for r in results),by_split={s:sum(r['split']==s for r in results)for s in ['train','validation']},
        known_base_current_starts=sum(r['known_base_current_starts']for r in results),
        arm_transition_starts={arm:sum(r['arm_transition_starts'][arm]for r in results)for arm in ['left','right']},
        records=results,script_sha256=sha(__file__))
    write(out/'selection.json',summary)
    print(json.dumps({k:v for k,v in summary.items()if k!='records'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
