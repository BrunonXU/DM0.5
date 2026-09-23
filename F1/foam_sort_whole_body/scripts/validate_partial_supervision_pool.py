#!/usr/bin/env python3
"""Independently verify preservation and missing-base semantics on real packs."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np
TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'models/DM0.5'))
from f1_data.reader import Episode, PARTIAL_BASE_CONTRACT, sha256
from f1_data.state_tokens import state_tokens


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--stop-profile',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();root=args.root.resolve()
    assert not args.output.exists(), 'Keep prior diagnostic evidence'
    profile=json.loads(args.stop_profile.read_text())
    linear=float(profile['base_stopped_linear_m_s']);angular=float(profile['base_stopped_angular_rad_s'])
    assert linear>0 and angular>0
    selection=json.loads((root/'selection.json').read_text());rows=[]
    for record in selection['records']:
        stem=record['episode'].replace('/','_')
        ep=Episode(root/'cases'/stem/'composed/episode.npz',root/'pose_cache'/stem/'pose_cache.npz',allow_candidates=True)
        assert ep.contract==PARTIAL_BASE_CONTRACT
        origin=ep.meta['upper_body_window_parent'];assert sha256(origin['path'])==origin['sha256']
        with np.load(origin['path'],allow_pickle=False)as z:p={k:z[k]for k in z.files}
        d=ep.data
        for k in ['time_ns','state','state_valid','camera_source_indices','camera_timestamp_candidate_valid','observation_candidate_valid']:
            assert np.array_equal(d[k],p[k],equal_nan=True),(ep.episode,k)
        assert np.array_equal(d['action'][:,:16],p['action'][:,:16],equal_nan=True)
        assert np.array_equal(d['action_candidate_valid'][:,:16],p['action_candidate_valid'][:,:16])
        assert np.array_equal(ep.starts,np.flatnonzero(p['chunk_start_candidate_valid']))
        future=d['action_candidate_valid'][:,:16].all(1)&d['state_valid'].all(1)&np.isfinite(d['action'][:,:16]).all(1)
        invalid=np.r_[0,np.cumsum(~future)]
        assert ((invalid[ep.starts+50]-invalid[ep.starts])==0).all()
        assert not (d['action_candidate_valid'][:,16:]&~np.isfinite(d['action'][:,16:])).any()
        mask=d['base_candidate_present']&d['base_timing_reviewed']&d['action_candidate_valid'][:,16:].all(1)
        assert np.array_equal(mask,ep.base_valid)
        probes=[ep.starts[0],ep.starts[-1]]
        for known in [True,False]:
            indices=ep.starts[mask[ep.starts]==known]
            if len(indices):probes.append(indices[len(indices)//2])
        for t in probes:
            sample=ep.sample(t);assert sample['action_mask'][:,:20].all()
            assert np.array_equal(sample['action_mask'][:,20:23],np.repeat(mask[t:t+50,None],3,axis=1))
            assert np.array_equal(sample['state_mask'][16:],np.repeat(mask[t],3))
            assert np.isfinite(sample['state']).all()and np.isfinite(sample['action']).all()
            if not mask[t]:assert state_tokens(sample['state'],sample['state_mask'])[-3:]==['unknown']*3
        gripchange=np.r_[False,(np.abs(np.diff(d['action'][:,14:16],axis=0))>1).any(1)]
        velocity=np.nan_to_num(d['action'][:,16:])
        nonzero=gripchange&mask&(np.abs(velocity).max(1)>1e-7)
        conflict=gripchange&mask&((np.linalg.norm(velocity[:,:2],axis=1)>linear)|(abs(velocity[:,2])>angular))
        rows.append(dict(episode=ep.episode,starts=len(ep.starts),original_upper_windows_preserved=True,
            exact_nonzero_gripper_frames=np.flatnonzero(nonzero).tolist(),
            nonzero_linear_speeds_m_s=np.linalg.norm(velocity[nonzero,:2],axis=1).tolist(),
            known_base_gripper_conflict_frames=np.flatnonzero(conflict).tolist(),source_sha256=ep.source_sha256))
    report=dict(status='passed'if not any(r['known_base_gripper_conflict_frames']for r in rows)else 'conflicts_found',
        schema='f1_partial_supervision_independent_validation_v2',training_approved=False,
        stop_policy=dict(path=str(args.stop_profile.resolve()),sha256=sha256(args.stop_profile),
            linear_m_s=linear,angular_rad_s=angular,
            note='Existing deployed replay stop tolerance, not a newly fitted threshold. Exact nonzero diagnostics retained; no label or mask changes. This checks gripper events, not all arm motion.'),
        episodes=rows,total_starts=sum(r['starts']for r in rows),all_original_upper_windows_preserved=True,
        script_sha256=sha256(__file__),selection_sha256=sha256(root/'selection.json'))
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items()if k!='episodes'}))
    if report['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
