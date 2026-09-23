#!/usr/bin/env python3
"""Bind completed offline checks; keep formal training pending onsite acceptance."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np
TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'models/DM0.5'))
from f1_data.reader import Episode, sha256
from f1_training.data import describe_release, Release


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--numeric-audit',default='reader_audit');ap.add_argument('--suffix',default='')
    args=ap.parse_args();root=args.root.resolve()
    assert all(c.isalnum() or c=='_' for c in args.suffix)
    release_name='engineering_release'+args.suffix+'.json';acceptance_name='offline_acceptance'+args.suffix+'.json'
    assert not (root/release_name).exists()
    readback=json.loads((root/'image_readback/report.json').read_text());assert readback['status']=='passed'
    selection=json.loads((root/'selection.json').read_text());episodes=[];checks=[]
    for row in selection['records']:
        if row['status']!='offline_prepared':continue
        folder=row['episode'].replace('/','_');source=root/'cases'/folder/'composed/episode.npz'
        ep=Episode(source,root/'pose_cache'/folder/'pose_cache.npz',allow_candidates=True)
        original=Path(row['provenance']['source']);assert sha256(original)==row['provenance']['source_sha256']
        with np.load(original,allow_pickle=False) as z:
            for key in ['time_ns','state','state_valid','camera_source_indices','camera_log_age_s','camera_timestamp_candidate_valid']:
                assert np.array_equal(ep.data[key],z[key],equal_nan=True),(ep.episode,key)
            assert np.array_equal(ep.data['action'][:,:16],z['action'][:,:16],equal_nan=True)
            assert not (ep.data['action_candidate_valid']&~z['action_candidate_valid']).any()
            assert not (ep.data['chunk_start_candidate_valid']&~z['chunk_start_candidate_valid']).any()
        coverage=np.zeros(len(ep.data['time_ns']),bool)
        for i in ep.starts:coverage[i:i+50]=True
        for event in ['grasp','release']:
            indices=row['gate'][event]['indices'];assert indices and coverage[indices].all()
            assert np.max(np.abs(ep.data['action'][indices,16:]))<=1e-7
        assert ep.split==row['split']
        episodes.append(ep);checks.append(dict(episode=ep.episode,split=ep.split,valid_starts=len(ep.starts),
            arms_grippers_state_camera_clock_unchanged=True,parent_masks_not_relaxed=True,
            grasp_release_covered_by_actual_reader=True,grasp_release_base_zero=True))
    numeric=json.loads((root/args.numeric_audit/'report.json').read_text())
    assert numeric['total_starts']==sum(len(e.starts) for e in episodes)
    assert {(e.episode,e.source_sha256,len(e.starts)) for e in episodes}=={(e['episode'],e['source_sha256'],e['valid_starts']) for e in numeric['episodes']}
    document=describe_release(episodes,root/args.numeric_audit/'normalization.json',root/'reader_audit/images')
    document['release_state']='offline_verified_pending_onsite_replay_and_final_acceptance'
    document['acceptance_evidence']=str(root/acceptance_name)
    path=root/release_name;path.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    bound=Release(path,engineering=True);assert len(bound.episodes)==selection['accepted']
    report=dict(status='offline_reader_passed',training_approved=False,onsite_replay='pending',
                episodes=checks,total_starts=sum(len(e.starts) for e in episodes),
                release_sha256=sha256(path),readback_report_sha256=sha256(root/'image_readback/report.json'),
                numerical_reader_report_sha256=sha256(root/args.numeric_audit/'report.json'),
                scope='Source identity, masks, events, all selected original images and numerical reader; not physical task success')
    (root/acceptance_name).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='episodes'},indent=2))

if __name__=='__main__':main()
