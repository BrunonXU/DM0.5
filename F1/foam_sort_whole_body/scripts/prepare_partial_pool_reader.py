#!/usr/bin/env python3
"""Build FK/reader inputs for partial episodes, retaining reviewed controls.

Does not grant training approval or run robot actions. Existing 28-source
reference packs override automatic partial proposals for the same identities.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import numpy as np
from build_arm_guided_joint_proposals import TASK, sha, write
sys.path.insert(0,str(TASK/'robot'))
from kinematics import ArmModel,LOW_DEG,HIGH_DEG
sys.path.insert(0,str(TASK/'models/DM0.5'))
from f1_data.reader import Episode


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--pool',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();pool=args.pool.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    audit=json.loads((pool/'independent_validation.json').read_text());assert audit['status']=='source_masks_and_h50_passed'
    ref=TASK/'results/demo_training_release_20260917_v1'
    controls={x['episode']:x for x in json.loads((ref/'selection.json').read_text())['records'] if x['status']=='offline_prepared'}
    reference_hashes={x['episode']:x['source_sha256'] for x in json.loads((ref/'minmax_reader_audit/report.json').read_text())['episodes']}
    cfg=TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg'
    model=ArmModel(cfg)
    template=json.loads((TASK/'results/kinematics_validation_20260916/pose_cache/all_data_3_derived_episode_000057/pose_cache.json').read_text())
    records=[]
    for row in audit['cases']:
        ep=row['episode'];name=ep.replace('/','_')
        if not row['starts'] and ep not in controls:
            records.append(dict(episode=ep,split=row['split'],status='no_h50_windows_pending_local_repair',valid_starts=0));continue
        origin=ref if ep in controls else pool
        source=origin/'cases'/name/'composed/episode.npz'
        expected=reference_hashes[ep] if ep in controls else row['source_npz_sha256']
        assert sha(source)==expected
        dest=out/'cases'/name/'composed';dest.mkdir(parents=True)
        shutil.copy2(source,dest/'episode.npz');shutil.copy2(source.with_suffix('.json'),dest/'episode.json')
        cache=out/'pose_cache'/name;cache.mkdir(parents=True)
        if ep in controls:
            for suffix in ['.npz','.json']:shutil.copy2(ref/'pose_cache'/name/('pose_cache'+suffix),cache/('pose_cache'+suffix))
        else:
            with np.load(source,allow_pickle=False) as z:data={k:z[k] for k in z.files}
            n=len(data['time_ns']);mapped=np.clip(data['action'][:,:14].reshape(n,2,7),LOW_DEG,HIGH_DEG)
            poses=dict(time_ns=data['time_ns'],raw_command_joints=data['action'][:,:14].astype(float),mapped_command_joints=mapped.reshape(n,14))
            for kind,key,mask in [('feedback','state','state_valid'),('command','action','action_candidate_valid')]:
                values=data[key][:,:14].reshape(n,2,7) if kind=='feedback' else mapped
                valid=data[mask][:,:14].reshape(n,2,7).all(2)&np.isfinite(values).all(2)
                pose=np.full((n,2,4,4),np.nan)
                for arm in range(2):
                    ix=np.flatnonzero(valid[:,arm])
                    unique,inverse=np.unique(values[ix,arm],axis=0,return_inverse=True)
                    if len(unique):pose[ix,arm]=np.array([model.fk(arm,q) for q in unique])[inverse]
                poses[kind+'_pose']=pose;poses[kind+'_valid']=valid
            np.savez_compressed(cache/'pose_cache.npz',**poses)
            pm={k:template[k] for k in ['kinematics_contract','execution_mapping','pose_unit','arm_order']}
            pm.update(schema='f1_local_verified_dh_fk_cache_v1',source_npz_sha256=sha(dest/'episode.npz'),
                      pose_cache_sha256=sha(cache/'pose_cache.npz'),training_approved=False,
                      implementation=str(TASK/'robot/kinematics.py'),implementation_sha256=sha(TASK/'robot/kinematics.py'),
                      config_sha256=sha(cfg),parity_evidence=str(TASK/'results/continuous_ik_20260916/README.md'))
            write(cache/'pose_cache.json',pm)
        episode=Episode(dest/'episode.npz',cache/'pose_cache.npz',allow_candidates=True)
        assert len(episode.starts)==int(episode.data['chunk_start_candidate_valid'].sum())
        assert len(episode.starts)>0 and episode.split==row['split']
        # First/middle/last real samples exercise EEF layout; full audit follows.
        for i in np.unique(episode.starts[[0,len(episode.starts)//2,-1]]):episode.sample(int(i))
        records.append(dict(episode=ep,split=episode.split,status='offline_prepared',valid_starts=len(episode.starts),
                            source_sha256=sha(source),provenance=str(source),
                            preserved_reference=ep in controls))
        write(out/'progress.json',dict(examined=len(records),scope=len(audit['cases']),prepared=sum(x['valid_starts']>0 for x in records)))
        if len(records)%25==0:print(json.dumps(dict(examined=len(records),scope=len(audit['cases']))),flush=True)
    report=dict(schema='f1_partial_pool_reader_preparation_v1',training_approved=False,
                accepted=sum(x['valid_starts']>0 for x in records),total_starts=sum(x['valid_starts'] for x in records),
                by_split={s:sum(x['valid_starts']>0 and x['split']==s for x in records) for s in ['train','validation']},
                records=records,script_sha256=sha(__file__),parent_audit_sha256=sha(pool/'independent_validation.json'))
    write(out/'selection.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='records'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
