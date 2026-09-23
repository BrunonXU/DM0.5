#!/usr/bin/env python3
"""Repair short stationary evidence holes using additional original-image anchors.

No interpolation across measured motion, camera/pose failures or missing images.
Outputs a new proposal version and records every accepted/rejected bridge.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
from build_arm_guided_joint_proposals import TASK, build, sha, write
from refine_arm_guided_base_boundaries import ROIS, _analyze_roi
from audit_base_phase_cache import held_interval


def eligible_holes(pairs, maximum_s=.35):
    out=[];i=0
    while i<len(pairs):
        if pairs[i]['status']!='unknown':i+=1;continue
        j=i+1
        while j<len(pairs) and pairs[j]['status']=='unknown':j+=1
        group=pairs[i:j]
        if i>0 and j<len(pairs) and pairs[i-1]['status']==pairs[j]['status']=='stationary_support':
            context=pairs[i-1:j+1]
            if (group[-1]['end_s']-group[0]['start_s']<=maximum_s
                and all(p['pose_ok'] and p['fresh'] and not p['repeated'] and .04<p['dt_s']<.18 for p in context)
                and all(abs(a['end_s']-b['start_s'])<1e-8 for a,b in zip(context,context[1:]))):
                out.append((i,j))
        i=j
    return out


def anchored_pairs(record):
    pairs=[dict(p) for p in record['pairs']];holes=eligible_holes(pairs);reports=[]
    if not holes:return pairs,reports
    cache=Path(record['cache_path'])
    if not cache.is_absolute():cache=TASK/cache
    assert sha(cache)==record['cache_sha256']
    meta=json.loads(cache.with_suffix('.json').read_text());origin=meta['source_start_ns']
    with np.load(cache,allow_pickle=False) as z:frames={k:z[k] for k in z.files}
    times=(frames['head_log_ns']-origin)/1e9
    for i,j in holes:
        left=pairs[i-1]['start_s'];right=pairs[j]['end_s']
        a=int(np.argmin(abs(times-left)));b=int(np.argmin(abs(times-right)))
        assert abs(times[a]-left)<1e-8 and abs(times[b]-right)<1e-8
        coverage,q=held_interval(frames['hal_log_ns'],frames['head_body_q'],int(frames['head_log_ns'][a]),int(frames['head_log_ns'][b]))
        span=np.ptp(q,axis=0) if len(q) else np.full(8,np.inf)
        pose_ok=bool(coverage['covered'] and max(span[:3])<=.3 and max(span[3:])<=.2)
        rois={}
        for name,boxes in ROIS.items():
            rois[name]=[_analyze_roi(frames['head_gray'][a,y0:y1,x0:x1].astype(float),
                        frames['head_gray'][b,y0:y1,x0:x1].astype(float),k,list(box))
                        for k,box in enumerate(boxes) for x0,y0,x1,y1 in [box]]
        # Fixed total-displacement bound, not a rate increased with gap duration.
        low={name:sum(r['valid'] and r['zero_shift_ncc']>=.96 and r['displacement_px']<=.2 for r in rs)
             for name,rs in rois.items()}
        # Seven stationary patches across all three groups can support the
        # same conclusion when a hand occludes one of the outer patches.
        balanced=low['outer']>=2 and low['floor']>=2 and low['central']>=3
        accepted=bool(pose_ok and ((low['outer']>=3 and low['central']>=3) or balanced))
        report=dict(start_s=pairs[i]['start_s'],end_s=pairs[j-1]['end_s'],
                    anchor_times_s=[left,right],head_source_indices=frames['head_source_indices'][[a,b]].tolist(),
                    stationary_roi_counts=low,pose_ok=pose_ok,accepted=accepted,rois=rois,
                    provenance='short_unknown_bracketed_by_static_and_longer_baseline_image_agreement')
        reports.append(report)
        if accepted:
            for k in range(i,j):pairs[k]['status']='stationary_support';pairs[k]['static_bridge']=True
    return pairs,reports


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--refinement',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    masks_path=TASK/'results/demo_772_selection/mask_manifest.jsonl';selection_path=TASK/'results/demo_772_selection/candidates.jsonl'
    masks={r['episode']:r for r in map(json.loads,masks_path.read_text().splitlines())}
    selected={r['episode']:r for r in map(json.loads,selection_path.read_text().splitlines())}
    reports=[]
    for path in sorted((args.refinement/'cases').glob('*.json')):
        case=json.loads(path.read_text());ep=case['episode'];m=masks[ep];row=selected[ep]
        assert case['parent_split']==m['parent_split']==row['parent_split']
        for name,expected in case['inputs'].items():assert sha(name)==expected
        assert sha(m['aligned_npz_path'])==m['aligned_npz_sha256']
        with np.load(m['aligned_npz_path'],allow_pickle=False) as z:parent={k:z[k] for k in z.files}
        t=(parent['time_ns']-row['source_start_ns'])/1e9
        pp=args.refinement/'pairs'/path.name;pr=json.loads(pp.read_text());assert pr['cache_sha256']==case['cache_sha256']
        # Keep the same boundary admission rules; no attempt to salvage unstable routes.
        original,old_report=build(case,pr['pairs'],parent,t,m['tail_mask_start_s'])
        bridges=[];data=original;report=old_report
        if original is not None:
            pairs,bridges=anchored_pairs(pr)
            data,report=build(case,pairs,parent,t,m['tail_mask_start_s'])
        report.update(episode=ep,parent_split=row['parent_split'],training_approved=False,
            inputs={str(path.resolve()):sha(path),str(pp.resolve()):sha(pp),m['aligned_npz_path']:m['aligned_npz_sha256']},
            bridge_checks=bridges,previous_h50_starts=old_report.get('proposed_h50_starts',0))
        if data is not None:
            out=args.output/ep.replace('/','_');out.mkdir();np.savez_compressed(out/'proposal.npz',**data)
            report['npz_sha256']=sha(out/'proposal.npz');write(out/'report.json',report)
        reports.append(report)
    summary=dict(scope=len(reports),materialized=sum(r['status']=='joint_timing_proposal' for r in reports),
        h50_starts=sum(r.get('proposed_h50_starts',0) for r in reports),
        accepted_static_bridges=sum(sum(b['accepted'] for b in r['bridge_checks']) for r in reports),
        training_approved=False,results=reports,script_sha256=sha(__file__),
        generator_sha256=sha(TASK/'scripts/build_arm_guided_joint_proposals.py'),
        registration_sha256=sha(TASK/'scripts/head_background_motion.py'),
        bridge_policy=dict(max_unknown_s=.35,max_endpoint_displacement_px=.2,min_zero_ncc=.96,
                           motion_pairs_bridgeable=False,parent_masks_relaxed=False),
        mask_registry_sha256=sha(masks_path),selection_sha256=sha(selection_path))
    write(args.output/'summary.json',summary)
    (args.output/'method_snapshot.py').write_bytes(Path(__file__).read_bytes())
    print(json.dumps({k:v for k,v in summary.items() if k!='results'},indent=2))

if __name__=='__main__':main()
