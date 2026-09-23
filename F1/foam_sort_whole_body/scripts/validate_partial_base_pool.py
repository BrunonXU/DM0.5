#!/usr/bin/env python3
"""Independent full-pool invariants and actual H50 event coverage audit."""
import argparse
import json
from pathlib import Path
from collections import Counter
import numpy as np
from build_arm_guided_joint_proposals import TASK, sha, write


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True);args=ap.parse_args()
    root=args.root.resolve();summary=json.loads((root/'summary.json').read_text())
    assert not (root/'independent_validation.json').exists()
    masks={r['episode']:r for r in map(json.loads,(TASK/'results/demo_772_selection/mask_manifest.jsonl').read_text().splitlines())}
    rows=[]
    for r in summary['results']:
        ep=r['episode'];m=masks[ep];source=Path(m['aligned_npz_path'])
        assert sha(source)==m['aligned_npz_sha256']
        path=root/'cases'/ep.replace('/','_')/'composed/episode.npz';assert sha(path)==r['npz_sha256']
        with np.load(source,allow_pickle=False) as z:old={k:z[k] for k in z.files}
        with np.load(path,allow_pickle=False) as z:new={k:z[k] for k in z.files}
        for k in ('time_ns','state','state_valid','camera_source_indices','camera_log_age_s','camera_timestamp_candidate_valid','observation_candidate_valid'):
            assert np.array_equal(old[k],new[k],equal_nan=True),(ep,k)
        assert np.array_equal(old['action'][:,:16],new['action'][:,:16],equal_nan=True)
        for k in ('action_candidate_valid','chunk_start_candidate_valid'):
            assert not (new[k]&~old[k]).any(),(ep,k)
        assert not new['training_approved'] and not new['action_candidate_valid'][m['tail_mask_start_index']:].any()
        present=new['base_candidate_present'];assert np.isnan(new['action'][~present,16:]).all()
        assert np.isfinite(new['action'][present,16:]).all()
        starts=np.flatnonzero(new['chunk_start_candidate_valid']);cov=np.zeros(len(present),bool)
        for i in starts:
            assert i+50<=len(present) and old['chunk_start_candidate_valid'][i]
            assert new['observation_candidate_valid'][i] and new['camera_timestamp_candidate_valid'][i].all()
            assert new['camera_source_indices'][i].min()>=0
            assert new['state_valid'][i:i+50].all() and new['action_candidate_valid'][i:i+50].all()
            assert np.isfinite(new['action'][i:i+50]).all()
            cov[i:i+50]=True
        assert len(starts)==r['valid_starts']
        events={}
        for col,hand in [(14,'left'),(15,'right')]:
            diff=np.r_[0,np.diff(old['action'][:,col])]
            for sign,kind in [(1,'closing'),(-1,'opening')]:
                ix=np.flatnonzero((diff*sign>1)&(np.arange(len(present))<m['tail_mask_start_index']))
                selected=ix[cov[ix]]
                assert not len(selected) or (np.abs(new['action'][selected,16:])<=1e-7).all()
                events[hand+'_'+kind]=dict(total=len(ix),covered=len(selected))
        rows.append(dict(episode=ep,split=r['split'],starts=len(starts),events=events,
                         stages=len(r['motion_stages']),transition_starts=r['transition_starts'],
                         source_npz_sha256=sha(path)))
    totals={k:dict(sources_with_covered_changes=sum(r['events'][k]['covered']>0 for r in rows),
                   total_change_frames=sum(r['events'][k]['total'] for r in rows),
                   covered_change_frames=sum(r['events'][k]['covered'] for r in rows))
            for k in ('left_closing','left_opening','right_closing','right_opening')}
    report=dict(status='source_masks_and_h50_passed',scope=len(rows),training_approved=False,
                starts=sum(r['starts'] for r in rows),sources_with_windows=sum(r['starts']>0 for r in rows),
                by_split=dict(Counter(r['split'] for r in rows if r['starts'])),event_coverage=totals,
                zero_window_sources=[r['episode'] for r in rows if not r['starts']],
                both_left_event_types_covered=sum(r['events']['left_closing']['covered']>0 and r['events']['left_opening']['covered']>0 for r in rows),
                cases=rows,limits='Gripper changes are not semantic grasp success; does not run EEF reader or original image decode',
                script_sha256=sha(__file__),summary_sha256=sha(root/'summary.json'))
    write(root/'independent_validation.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases','zero_window_sources')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
