#!/usr/bin/env python3
"""Use measured arm activity to bracket base phases, without rewriting labels.

The task prior is soft: operation/hold transitions locate a SEARCH interval,
not an equality between arm and chassis transition times. HAL and lead remain
separate. Reset feedback may locate task end, but never becomes a lead target.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import numpy as np

TASK=Path(__file__).resolve().parents[1]
WINDOW=6  # current plus 6 preceding 30Hz samples: 0.2 seconds
DWELL=6
THRESHOLDS=(.25,.5,1.)  # range in degrees, sensitivity diagnostics, not limits
ANCHORS=(('back_out','start_s','motion_to_hold'),
         ('back_out','end_s','hold_to_motion'),
         ('right_out','start_s','motion_to_hold'),
         ('forward_out','end_s','hold_to_motion'),
         ('back_return','start_s','motion_to_hold'),
         ('left_return','end_s','hold_to_motion'))


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def activity(t,q,valid):
    span=np.full(len(t),np.nan)
    if len(t)<=WINDOW:return span
    windows=np.lib.stride_tricks.sliding_window_view(q,WINDOW+1,axis=0)
    good=np.lib.stride_tricks.sliding_window_view(valid,WINDOW+1,axis=0).all(axis=(1,2))
    good&=np.isfinite(windows).all(axis=(1,2))
    gaps=np.lib.stride_tricks.sliding_window_view(np.diff(t),WINDOW).max(axis=1)
    good&=(gaps<=.1)&(t[WINDOW:]-t[:-WINDOW]<=.21)&(t[WINDOW:]-t[:-WINDOW]>.19)
    values=np.ptp(windows,axis=2).max(axis=1)
    span[WINDOW:]=np.where(good,values,np.nan)
    return span


def transitions(t,span,threshold):
    active=span>threshold;finite=np.isfinite(span);events=[]
    for i in np.flatnonzero(np.diff(active.astype(np.int8))!=0)+1:
        if i<DWELL or i+DWELL>len(t) or not finite[i-DWELL:i+DWELL].all():continue
        left,right=active[i-DWELL:i],active[i:i+DWELL]
        if right.all() and not left.any():kind='hold_to_motion'
        elif left.all() and not right.any():kind='motion_to_hold'
        else:continue
        events.append(dict(kind=kind,time_s=float(t[i]),index=int(i),
                           detector_window_s=[float(t[max(0,i-WINDOW)]),float(t[i])],
                           confirmation_time_s=float(t[i+DWELL-1]),
                           threshold_range_deg=threshold))
    return events


def select_anchor(events,role,boundary,kind,support):
    reference=support[boundary]
    # Hold should precede travel; operation should follow arrival. Preserve
    # limited overlap for review rather than declaring concurrency impossible.
    lo,hi=(reference-6,reference+.5) if kind=='motion_to_hold' else (reference-1,reference+4)
    candidates=[e for e in events if e['kind']==kind and lo<=e['time_s']<=hi]
    if not candidates:return dict(role=role,boundary=boundary,status='no_arm_anchor',support_time_s=reference)
    e=min(candidates,key=lambda e:abs(e['time_s']-reference))
    a,b=sorted([e['time_s'],reference])
    return dict(role=role,boundary=boundary,status='soft_arm_anchor',support_time_s=reference,
                arm_event=e,alternatives=candidates,search_interval_s=[max(0,a-.5),b+.5],
                arm_minus_visual_support_s=e['time_s']-reference,
                interpretation='search prior only; image verification decides chassis transition; not simultaneous by definition')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    selection=TASK/'results/demo_772_selection/candidates.jsonl';maskfile=TASK/'results/demo_772_selection/mask_manifest.jsonl'
    rows=list(map(json.loads,selection.read_text().splitlines()));masks={r['episode']:r for r in map(json.loads,maskfile.read_text().splitlines())}
    results=[];controls=[]
    write(out/'config.json',dict(script_sha256=sha(__file__),selection_sha256=sha(selection),mask_sha256=sha(maskfile),
      window_frames=WINDOW+1,dwell_frames=DWELL,thresholds_deg=list(THRESHOLDS),primary_threshold_deg=.5,
      threshold_meaning='0.2s maximum joint range for sensitivity, not base stop labels or robot motion limits',
      scope='whole_body_joint_data_no_modality_split',training_approved=False))
    (out/'method_snapshot.py').write_bytes(Path(__file__).read_bytes())
    for row in rows:
        ep=row['episode'];stem=ep.replace('/','_');m=masks[ep]
        assert sha(m['aligned_npz_path'])==m['aligned_npz_sha256']
        draftfile=TASK/'results/base_relabel_v3_autonomous/autonomous_queue'/f'{stem}.json'
        draft=json.loads(draftfile.read_text())
        with np.load(m['aligned_npz_path'],allow_pickle=False) as z:
            t=(z['time_ns']-row['source_start_ns'])/1e9
            feedback_span=activity(t,z['state'][:,:14],z['state_valid'][:,:14])
            command_span=activity(t,z['action'][:,:14],z['action_candidate_valid'][:,:14])
            hal={str(th):transitions(t,feedback_span,th) for th in THRESHOLDS}
            lead={str(th):transitions(t,command_span,th) for th in THRESHOLDS}
            for events in hal.values():
                for event in events:
                    event['inside_parent_joint_supervision']=bool(z['action_candidate_valid'][event['index']].all())
            phases={p['role']:p['observed_support'] for p in draft['phases']}
            anchors=[select_anchor(hal['0.5'],role,boundary,kind,phases[role]) for role,boundary,kind in ANCHORS if role in phases]
            for anchor in anchors:
                if anchor['status']!='soft_arm_anchor':continue
                e=anchor['arm_event'];matches=[]
                for th in ('.25','1.0'):
                    key='0.25' if th=='.25' else th
                    near=[x for x in hal[key] if x['kind']==e['kind'] and abs(x['time_s']-e['time_s'])<=.6]
                    matches.append(bool(near))
                anchor['stable_across_thresholds_within_0_6s']=all(matches)
            result=dict(episode=ep,parent_split=row['parent_split'],tail_mask_start_s=m['tail_mask_start_s'],
              feedback_events=hal,lead_events=lead,anchors=anchors,training_approved=False,
              source_sha256={m['aligned_npz_path']:m['aligned_npz_sha256'],str(draftfile):sha(draftfile)},
              limits=['arm holding does not prove chassis moving; arm motion does not prove chassis stopped',
                      'causal 0.2s range and dwell introduce detection delay; preserve detector window',
                      'feedback-only reset events are anchors, never repaired action labels'])
            write(out/'cases'/f'{stem}.json',result)
        resolved=[a for a in anchors if a['status']=='soft_arm_anchor']
        results.append(dict(episode=ep,parent_split=row['parent_split'],available_arm_anchors=len(resolved),
            stable_arm_anchors=sum(a['stable_across_thresholds_within_0_6s'] for a in resolved),
            primary_anchor_roles=[a['role']+'.'+a['boundary'] for a in resolved],
            missing=[a['role']+'.'+a['boundary'] for a in anchors if a['status']=='no_arm_anchor']))
        ref=TASK/'results/base_relabel_v3_autonomous/cases'/stem/'phase_review.json'
        if ref.exists():
            review=json.loads(ref.read_text());segments={s['id']:s for s in review['segments']}
            # 003 has a final adjustment; compare the semantic end of the
            # return, not the end of the main lateral subsegment.
            if 'left_return_adjustment' in segments:segments['left_return']=segments['left_return_adjustment']
            for a in resolved:
                if a['role'] not in segments:continue
                reference=segments[a['role']][a['boundary']];delta=a['arm_event']['time_s']-reference
                controls.append(dict(episode=ep,anchor=a['role']+'.'+a['boundary'],
                  visual_boundary_s=reference,arm_event_s=a['arm_event']['time_s'],arm_minus_base_s=delta,
                  inside_search_interval=a['search_interval_s'][0]<=reference<=a['search_interval_s'][1],
                  stable_across_thresholds=a['stable_across_thresholds_within_0_6s'],reference_sha256=sha(ref)))
    (out/'index.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in results))
    write(out/'control_comparison.json',dict(comparisons=controls,training_approved=False,
      interpretation='independent earlier visual timing compared with arm-search priors; not arm/base time equality'))
    summary=dict(processed=len(results),sources_with_any_arm_anchor=sum(r['available_arm_anchors']>0 for r in results),
      sources_with_all_six_arm_anchors=sum(r['available_arm_anchors']==6 for r in results),
      total_arm_anchors=sum(r['available_arm_anchors'] for r in results),
      threshold_stable_anchors=sum(r['stable_arm_anchors'] for r in results),
      per_role_found=dict(Counter(k for r in results for k in r['primary_anchor_roles'])),
      controls_compared=len(controls),controls_inside_search_interval=sum(r['inside_search_interval'] for r in controls),
      controls_outside_search=[r for r in controls if not r['inside_search_interval']],
      source_data_changed=False,training_approved=False)
    write(out/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
