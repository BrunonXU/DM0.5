#!/usr/bin/env python3
"""Batch temporal proposals from immutable evidence; never a training release.

All 19 action dimensions share the proposed exclusion mask. Unknown intervals
are NaN, not stationary. Separate draft keys intentionally prevent the formal
reader from consuming an unreviewed reconstruction as an approved episode.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import numpy as np

TASK = Path(__file__).resolve().parents[1]
OLD = TASK / 'results/base_relabel_v3_autonomous'
DIRECTIONS = {'image_up', 'image_down', 'image_left', 'image_right'}
ROLE = {'back_out': ('image_up', 0, -.5), 'right_out': ('image_left', 1, -1.6),
        'forward_out': ('image_down', 0, .5), 'back_return': ('image_up', 0, -.5),
        'left_return': ('image_right', 1, 1.6)}
SETTINGS = dict(schema='f1_batch_base_timing_proposal_v1', static_ncc=.96,
    static_max_px=.75, motion_ncc=.7, motion_min_px=1.25,
    event_delta=5., event_step_epsilon=.05, event_merge_gap_s=.25, max_pair_gap_s=.7,
    max_boundary_envelope_s=1.5, boundary_guard_s=.10,
    amplitude_source='user_confirmed_route_prior_not_measured_velocity',
    scope='automatic_joint_proposal_not_semantic_acceptance', training_approved=False)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    tmp.replace(path)


def records(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def direction(x,y):
    if abs(x)>4*abs(y): return 'image_right' if x>0 else 'image_left'
    if abs(y)>4*abs(x): return 'image_down' if y>0 else 'image_up'
    return 'oblique'


def classify_pair(inner, outer, central):
    """All coordinates are image motion; never pixel-to-metre conversion."""
    assert inner['t0']==outer['t0']==central['start_s']
    assert inner['t1']==outer['t1']==central['end_s']
    result=dict(start_s=inner['t0'], end_s=inner['t1'], status='unknown', reasons=[])
    if (not inner['pose_ok'] or not outer['pose_ok'] or
        not .1 < inner['t1']-inner['t0'] < SETTINGS['max_pair_gap_s'] or
        max(central['previous_causal_age_s'],central['current_causal_age_s'])>.1 or
        central['repeated_source_frame'] or central['repeated_jpeg'] or central['repeated_gray']):
        result['reasons']=['pose_age_gap_or_repeated_image']; return result
    motion=central['motion']
    dirs=[p['direction'] for p in (inner,outer) if p['direction'] in DIRECTIONS]
    if motion['trusted_motion'] and motion['dx_px'] is not None:
        d=direction(motion['dx_px'],motion['dy_px'])
        if d in DIRECTIONS: dirs.append(d)
    if len(set(dirs))>1:
        result['reasons']=['background_direction_conflict']; return result
    if dirs:
        result['status']=dirs[0]; result['support_region_groups']=len(dirs); return result
    rois=[inner['rois'],outer['rois'],motion['rois']]
    def lows(rs):
        return sum(r['valid'] and r['zero_shift_ncc']>=SETTINGS['static_ncc'] and
                   r['displacement_px']<SETTINGS['static_max_px'] for r in rs)
    moving=any(r['valid'] and r['shifted_ncc']>=SETTINGS['motion_ncc'] and
               r['displacement_px']>=SETTINGS['motion_min_px'] for rs in rois for r in rs)
    # Stationary is an explicit high-correlation hypothesis across separated
    # regions, not the complement of detected motion. Foreground remains a risk.
    if not moving and lows(outer['rois'])>=3 and lows(motion['rois'])>=3:
        result['status']='stationary_support'
    else: result['reasons']=['insufficient_background_evidence']
    return result


def merge_pairs(pairs):
    groups=[]
    for p in pairs:
        if p['status']=='unknown': continue
        if groups and groups[-1]['status']==p['status'] and abs(groups[-1]['end_s']-p['start_s'])<1e-8:
            groups[-1]['end_s']=p['end_s']; groups[-1]['pairs']+=1
        else: groups.append(dict(start_s=p['start_s'],end_s=p['end_s'],status=p['status'],pairs=1))
    return groups


def gripper_events(t, a, valid):
    events=[]
    for col,name in [(14,'left'),(15,'right')]:
        good=valid[1:,col]&valid[:-1,col]&np.isfinite(a[1:,col])&np.isfinite(a[:-1,col])
        good&=(np.diff(t)<=.1)
        delta=np.diff(a[:,col])
        for sign,kind in [(1,'close_command'),(-1,'open_command')]:
            # A slow 0->100 closure may have NO individual jump over 5.
            # Aggregate monotonic command changes; never join across a dropout
            # or a meaningful opposite-direction transition.
            ids=np.flatnonzero(good&(sign*delta>SETTINGS['event_step_epsilon']))+1
            runs=[]
            for i in ids:
                prior=runs[-1][-1] if runs else None
                can_join=(prior is not None and t[i]-t[prior]<=SETTINGS['event_merge_gap_s']
                          and good[prior:i].all()
                          and np.all(sign*delta[prior:i]>=-SETTINGS['event_step_epsilon']))
                if can_join: runs[-1].append(int(i))
                else:runs.append([int(i)])
            for run in runs:
                change=float(sign*(a[run[-1],col]-a[run[0]-1,col]))
                if change<SETTINGS['event_delta']:continue
                events.append(dict(hand=name,kind=kind,start_s=float(t[run[0]]),end_s=float(t[run[-1]]),
                                   indices=run,command_change=change,
                                   interpretation='command_change_not_object_contact'))
    return sorted(events,key=lambda e:e['start_s'])


def proposal(times, parent, pairs, draft, tail_s, events):
    """Mask entire 19D rows and H50 spans; do not split supervision by modality."""
    n=len(times); v=np.full((n,3),np.nan,dtype=np.float32); support=np.zeros(n,bool)
    groups=merge_pairs(pairs); stages=[]; issues=[]
    for g in groups:
        if g['status']=='stationary_support':
            take=(times>=g['start_s']+SETTINGS['boundary_guard_s'])&(times<g['end_s']-SETTINGS['boundary_guard_s'])
            v[take]=0;support[take]=True
    phases=draft['phases']
    for phase in phases:
        role=phase['role']; seed=phase['observed_support']; d,axis,dist=ROLE[role]
        # Pick the current contiguous directional component that intersects the
        # old support. Never bridge unknown gaps merely to recover route length.
        matches=[g for g in groups if g['status']==d and min(g['end_s'],seed['end_s'])>max(g['start_s'],seed['start_s'])]
        if not matches:
            issues.append(role+':no_directional_component');continue
        g=max(matches,key=lambda g:g['end_s']-g['start_s'])
        before=[p for p in pairs if p['end_s']<=g['start_s']+1e-8 and p['status'] not in ('unknown',d)]
        after=[p for p in pairs if p['start_s']>=g['end_s']-1e-8 and p['status'] not in ('unknown',d)]
        if not before or not after:
            issues.append(role+':unbounded_start_or_stop');continue
        previous,next_=before[-1],after[0]
        own=[p for p in pairs if p['status']==d and p['start_s']>=g['start_s']-1e-8 and p['end_s']<=g['end_s']+1e-8]
        begin=[previous['end_s'],own[0]['end_s']]
        end=[own[-1]['start_s'],next_['start_s']]
        begin[0]=min(begin);end[1]=max(end)
        if max(begin[1]-begin[0],end[1]-end[0])>SETTINGS['max_boundary_envelope_s']:
            issues.append(role+':wide_boundary_envelope');continue
        a,b=np.mean(begin),np.mean(end)
        if not a<b or begin[1]>=end[0]:
            issues.append(role+':no_resolved_motion_interior');continue
        take=(times>=begin[1])&(times<end[0])
        # Retain ONLY explicit same-direction support; do not impute a blank.
        agrees=np.zeros(n,bool)
        for p in own: agrees|=(times>=p['start_s'])&(times<p['end_s'])
        take&=agrees
        u=(times[take]-a)/(b-a)
        v[take]=0;v[take,axis]=dist*6*u*(1-u)/(b-a);support[take]=True
        stages.append(dict(role=role,start_envelope_s=begin,end_envelope_s=end,
            proposed_start_s=float(a),proposed_end_s=float(b),distance_prior_m=dist,
            direction=d,axis=axis,resolved_interior_frames=int(take.sum()),
            timing_status='automatic_proposal',physically_measured=False))
    # Exclude command transition neighbourhoods unless explicit stationary
    # evidence covers them. This quarantines ambiguous hand/base overlap rather
    # than pretending gripper events prove that the chassis stopped.
    event_results=[]
    for e in events:
        guard=(times>=e['start_s']-.5)&(times<=e['end_s']+.5)&(times<tail_s)
        moving=support&np.any(np.nan_to_num(v)!=0,axis=1)
        conflict=bool(np.any(guard&moving))
        if conflict:
            support[guard]=False;v[guard]=np.nan
        event_results.append(e|dict(proposed_motion_overlap=conflict,
                                     event_neighbourhood_frames=int(guard.sum())))
    support&=(times<tail_s)
    v[~support]=np.nan
    # One mask for ALL dimensions, preserving every prior technical exclusion.
    joint=parent['action_candidate_valid'].all(axis=1)&parent['state_valid'].all(axis=1)&support
    chunks=chunk_mask(parent,joint)
    return v,support,joint,chunks,stages,issues,event_results,groups


def chunk_mask(parent,joint,horizon=50):
    n=len(joint); result=np.zeros(n,bool)
    if n>=horizon:
        bad=np.r_[0,np.cumsum(~joint)]
        result[:n-horizon+1]=(bad[horizon:]-bad[:-horizon]==0)&parent['observation_candidate_valid'][:n-horizon+1]
    return result&parent['chunk_start_candidate_valid']


def process(item):
    row,mask,index,out=item;ep=row['episode'];stem=ep.replace('/','_');case=out/'cases'/stem
    case.mkdir(parents=True,exist_ok=False)
    draft_path=Path(index['draft_path']);assert sha(draft_path)==index['draft_sha256']
    draft=json.loads(draft_path.read_text())
    inner=OLD/('floor' if index['status']=='assistant_visual_candidate' else 'batch/floor')/(stem+'.json')
    outer=OLD/'outer/floor'/(stem+'.json')
    central=(TASK/'results/demo_775_phase_review/base_phase_screen/sources'/ep).with_suffix('.pairs.jsonl')
    a=json.loads(inner.read_text());b=json.loads(outer.read_text());c=records(central)
    central_metadata=central.with_name(central.name.replace('.pairs.jsonl','.json'))
    audit=json.loads(central_metadata.read_text())
    assert sha(central)==audit['pairs_sha256']
    assert a['episode']==b['episode']==ep and a['cache_sha256']==b['cache_sha256']
    cache=Path(a['cache']);assert sha(cache)==a['cache_sha256']
    assert a['cache_sha256']==audit['input_hashes']['cache_npz_sha256']
    cachemeta=json.loads(cache.with_suffix('.json').read_text())
    assert cachemeta['source_start_ns']==row['source_start_ns']
    assert len(a['pairs'])==len(b['pairs'])==len(c)
    with np.load(mask['aligned_npz_path'],allow_pickle=False) as z: parent={k:z[k] for k in z.files}
    assert sha(mask['aligned_npz_path'])==mask['aligned_npz_sha256']
    times=(parent['time_ns']-int(row['source_start_ns']))/1e9
    assert np.all(np.diff(parent['time_ns'])>0)
    tail=mask['tail_mask_start_s']
    pairs=[classify_pair(x,y,z) for x,y,z in zip(a['pairs'],b['pairs'],c) if x['t1']<tail]
    events=gripper_events(times,parent['action'],parent['action_candidate_valid'])
    v,support,joint,chunks,stages,issues,events,groups=proposal(times,parent,pairs,draft,tail,events)
    unresolved_frames=parent['action_candidate_valid'].all(axis=1)&~support
    # Old labels only provide a disagreement count, never ground truth for the repair.
    old=parent['action'][:,16:19]; stationary=support&np.all(v==0,axis=1)
    old_nonzero_stop=stationary&np.any(np.nan_to_num(old)!=0,axis=1)
    proposed_moving=support&np.any(np.nan_to_num(v)!=0,axis=1)
    old_zero_move=proposed_moving&np.all(old==0,axis=1)
    coarse_motion=[]
    for e in events:
        hits=[p for p in pairs if p['status'] in DIRECTIONS and min(e['end_s']+.1,p['end_s'])>max(e['start_s']-.1,p['start_s'])]
        if hits:coarse_motion.append(dict(event=e,pairs=hits,meaning='coarse_visual_overlap_needs_boundary_check_not_proven_mislabel'))
    np.savez_compressed(case/'proposal.npz',time_ns=parent['time_ns'],base_velocity_proposal=v,
        automatic_support=support,proposed_joint_valid=joint,proposed_chunk_start=chunks,
        unresolved_joint_frames=unresolved_frames,training_approved=np.array(False))
    hashes={str(p):sha(p) for p in (inner,outer,central,central_metadata,draft_path,Path(mask['aligned_npz_path']))}
    report=dict(episode=ep,parent_split=row['parent_split'],previously_reviewed=index['status']=='assistant_visual_candidate',
        frames=len(times),tail_mask_start_s=tail,stages=stages,issues=issues,old_triage_flags=draft['flags'],
        pair_counts=dict(Counter(p['status'] for p in pairs)),groups=groups,events=events,
        event_visual_overlap=coarse_motion,proposed_joint_frames=int(joint.sum()),
        proposed_h50_starts=int(chunks.sum()),unknown_joint_frames=int(unresolved_frames.sum()),
        old_nonzero_in_stationary_support_frames=int(old_nonzero_stop.sum()),
        old_zero_in_motion_support_frames=int(old_zero_move.sum()),
        status='automatic_proposal_only',training_approved=False,
        invariants=dict(raw_changed=False,upper_body_changed=False,split_changed=False,
                        masks_relaxed=False,modality_grouping=False,time_axis_changed=False),
        source_sha256=hashes,cache_sha256=a['cache_sha256'],proposal_sha256=sha(case/'proposal.npz'),
        limits=['2Hz head cache cannot establish exact contact/stop boundaries',
                'fixed ROI may lock on foreground; automatic support is not observed chassis odometry',
                'NaN gaps and all19 exclusion are required; formal reader must not consume this draft',
                'five selected roles do not prove absence of another cycle or full-task success'])
    write(case/'report.json',report)
    return {k:report[k] for k in ('episode','parent_split','previously_reviewed','proposed_joint_frames','proposed_h50_starts',
       'unknown_joint_frames','old_nonzero_in_stationary_support_frames','old_zero_in_motion_support_frames','issues','pair_counts')}|dict(
       event_visual_overlap_count=len(coarse_motion),stage_count=len(stages),path=str(case/'report.json'),status=report['status'])


def controls(out,results):
    reports=[]
    for row in results:
        if not row['previously_reviewed']:continue
        stem=row['episode'].replace('/','_');ref=OLD/'cases'/stem/'phase_review.json';review=json.loads(ref.read_text())
        with np.load(out/'cases'/stem/'proposal.npz') as z: t=(z['time_ns']-int(review['source_origin_ns']))/1e9;v=z['base_velocity_proposal'];support=z['automatic_support']
        known=np.zeros(len(t),bool);wanted=np.zeros((len(t),3))
        for s in review['segments']:
            radius=s['evidence'].get('boundary_estimate_radius_s',.5)
            use=(t>=s['start_s']+radius)&(t<s['end_s']-radius)
            known|=use;wanted[use]=np.sign([s['dx_m'],s['dy_m'],s['dyaw_rad']])
        bad=support&known&np.any(np.sign(np.nan_to_num(v))!=wanted,axis=1)
        conflict_times=t[bad]
        reports.append(dict(episode=row['episode'],compared_frames=int((support&known).sum()),
            direction_or_stop_conflict_frames=int(bad.sum()),conflict_times_s=conflict_times.tolist(),
            reference_sha256=sha(ref),reference='existing_visual_estimates_with_boundary_uncertainty_excluded'))
    result=dict(reports=reports,comparison_frames=sum(r['compared_frames'] for r in reports),
       conflict_frames=sum(r['direction_or_stop_conflict_frames'] for r in reports),
       training_approved=False,limits='8 existing cases are regression controls, not statistical proof for 764 unseen sources')
    write(out/'control_comparison.json',result);return result


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--workers',type=int,default=2);ap.add_argument('--limit',type=int);args=ap.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    selection=TASK/'results/demo_772_selection/candidates.jsonl';maskfile=TASK/'results/demo_772_selection/mask_manifest.jsonl';indexfile=OLD/'pool_index.jsonl'
    masks={r['episode']:r for r in records(maskfile)};indices={r['episode']:r for r in records(indexfile)};rows=records(selection)
    if args.limit: rows=rows[:args.limit]
    identity=dict(settings=SETTINGS,script_sha256=sha(Path(__file__)),selection_sha256=sha(selection),mask_sha256=sha(maskfile),index_sha256=sha(indexfile),scope=len(rows))
    write(out/'run_config.json',identity);results=[];failures=[];started=time.monotonic()
    (out/'method_snapshot.py').write_bytes(Path(__file__).read_bytes())
    def safe(row):
        try:return process((row,masks[row['episode']],indices[row['episode']],out)),None
        except Exception as e:return None,dict(episode=row['episode'],error=f'{type(e).__name__}: {e}')
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i,(result,error) in enumerate(pool.map(safe,rows),1):
            if error:failures.append(error)
            else:results.append(result)
            if i%25==0 or i==len(rows):
                progress=dict(processed=i,total=len(rows),successful=len(results),failures=failures,elapsed_s=round(time.monotonic()-started,2),status='running')
                write(out/'progress.json',progress);print(json.dumps(progress),flush=True)
    control=controls(out,results)
    (out/'index.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in results))
    remaining=[r for r in results if not r['previously_reviewed']]
    summary=dict(scope=len(rows),processed=len(results),previously_reviewed_controls=len(results)-len(remaining),new_sources_processed=len(remaining),
       failures=failures,new_sources_with_proposed_h50=sum(r['proposed_h50_starts']>0 for r in remaining),
       proposed_h50_starts=sum(r['proposed_h50_starts'] for r in remaining),
       new_sources_with_visual_event_overlap=sum(r['event_visual_overlap_count']>0 for r in remaining),
       new_sources_all_five_motion_interiors=sum(r['stage_count']==5 for r in remaining),
       new_stage_counts=dict(Counter(r['stage_count'] for r in remaining)),
       unresolved_issues=dict(Counter(s.split(':')[-1] for r in remaining for s in r['issues'])),
       old_nonzero_in_stationary_support_frames=sum(r['old_nonzero_in_stationary_support_frames'] for r in remaining),
       old_zero_in_motion_support_frames=sum(r['old_zero_in_motion_support_frames'] for r in remaining),
       control_comparison_frames=control['comparison_frames'],control_conflict_frames=control['conflict_frames'],
       training_approved=False,formal_training_manifest_changed=False,elapsed_s=round(time.monotonic()-started,2))
    write(out/'summary.json',summary);write(out/'progress.json',dict(status='complete' if not failures else 'complete_with_errors',**summary))
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    if failures:raise SystemExit(1)

if __name__=='__main__':main()
