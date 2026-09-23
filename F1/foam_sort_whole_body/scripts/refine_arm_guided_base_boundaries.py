#!/usr/bin/env python3
"""Dense background evidence inside arm-guided phase searches.

No source, existing label or training manifest is changed. Arm events bound
searches; they never directly become chassis start/stop times. Pixel thresholds
scale with actual frame interval. Motion/gap/pose failures remain explicit.
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
from head_background_motion import _analyze_roi,ROI_FRACTIONS
from audit_base_phase_cache import held_interval

TASK=Path(__file__).resolve().parents[1]
PRIOR=TASK/'results/base_relabel_v3_autonomous'
PILOT=TASK/'results/batch_base_timing_20260917_v2'
ARMS=TASK/'results/arm_base_anchors_20260917_v1'
ROLES={'back_out':'image_up','right_out':'image_left','forward_out':'image_down',
       'back_return':'image_up','left_return':'image_right'}
ROIS={'floor':[(170,155,260,225),(210,170,300,245),(140,175,230,245)],
      'outer':[(20,35,100,115),(170,30,250,110),(280,35,360,115),(20,155,95,225),(350,170,440,245)],
      'central':[(round(a*480),round(b*270),round(c*480),round(d*270)) for a,b,c,d in ROI_FRACTIONS]}
SETTINGS=dict(motion_px_s=2.5,motion_sensitivity_px_s=[1.5,2.5,4.0],static_px_s=1.5,
              min_shifted_ncc=.7,static_zero_ncc=.96,minimum_run_s=.16,
              max_boundary_bracket_s=.5,max_unobserved_bridge_s=.35,
              threshold_scope='diagnostic_pixel_rates_not_physical_base_speeds',training_approved=False)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n');temp.replace(path)


def direction(x,y):
    if abs(x)>4*abs(y):return 'image_right' if x>0 else 'image_left'
    if abs(y)>4*abs(x):return 'image_down' if y>0 else 'image_up'
    return 'oblique'


def classify(pair,rate=2.5):
    if not pair['pose_ok'] or not pair['fresh'] or not .04<pair['dt_s']<.18 or pair['repeated']:
        return 'unknown'
    ds=[];dt=pair['dt_s']
    for name,rs in pair['rois'].items():
        moving=[r for r in rs if r['valid'] and r['shifted_ncc']>=.7 and r['displacement_px']>=max(.15,rate*dt)]
        if len(moving)>=(3 if name=='central' else 2):
            vec=np.array([[r['dx_px'],r['dy_px']] for r in moving]);median=np.median(vec,axis=0)
            norm=np.linalg.norm(median)
            if norm>0 and np.min((vec@median)/(np.linalg.norm(vec,axis=1)*norm))>.94:
                d=direction(*median)
                if d!='oblique':ds.append(d)
    if len(set(ds))>1:return 'conflict'
    if ds:return ds[0]
    # Widely separated static regions can outvote an isolated moving hand.
    # A coherent moving REGION GROUP above already vetoes stationary status.
    def low(rs):return sum(r['valid'] and r['zero_shift_ncc']>=.96 and
                           r['displacement_px']<max(.08,1.5*dt) for r in rs)
    if low(pair['rois']['outer'])>=3 and low(pair['rois']['central'])>=3:return 'stationary_support'
    return 'unknown'


def runs(pairs,statuses):
    result=[]
    for i,(p,status) in enumerate(zip(pairs,statuses)):
        if result and result[-1]['status']==status and abs(result[-1]['end_s']-p['start_s'])<1e-8:
            result[-1]['end_s']=p['end_s'];result[-1]['indices'].append(i)
        else:result.append(dict(start_s=p['start_s'],end_s=p['end_s'],status=status,indices=[i]))
    return result


def locate(pairs,statuses,direction_,boundary,search,support=None,*,context_s=0.):
    """Bracket onset/offset using observed neighbours, not arm event equality."""
    if not 0 <= context_s <= SETTINGS['max_boundary_bracket_s']:
        raise ValueError('context may not exceed the existing visual bracket limit')
    groups=runs(pairs,statuses);lo,hi=search
    candidates=[g for g in groups if g['status']==direction_ and
                 g['end_s']-g['start_s']>=SETTINGS['minimum_run_s'] and
                 min(g['end_s'],hi)>max(g['start_s'],lo)]
    if not candidates:return dict(status='unresolved',reason='no_sustained_direction_in_search')
    # A new burst after an observed stop belongs to another adjustment. Do not
    # extend the current leg simply by choosing the last similar direction.
    if support is not None and boundary=='end_s':
        overlaps=[(max(0,min(g['end_s'],support['end_s'])-max(g['start_s'],support['start_s'])),g) for g in candidates]
        maximum=max(n for n,g in overlaps)
        if maximum<=0:return dict(status='unresolved',reason='no_component_matches_original_motion_support')
        g=max(overlaps,key=lambda x:x[0])[1]
    else:
        g=min(candidates,key=lambda x:x['start_s']) if boundary=='start_s' else max(candidates,key=lambda x:x['end_s'])
    if boundary=='start_s':
        first=g['indices'][0]
        earlier=[i for i in range(first) if statuses[i] not in ('unknown','conflict',direction_) and pairs[i]['end_s']>=lo-context_s]
        if not earlier:return dict(status='unresolved',reason='no_observed_preceding_state',motion_group=g)
        j=earlier[-1];a,b=pairs[j]['end_s'],pairs[first]['end_s']
        between=range(j+1,first)
    else:
        last=g['indices'][-1]
        later=[i for i in range(last+1,len(pairs)) if statuses[i] not in ('unknown','conflict',direction_) and pairs[i]['start_s']<=hi+context_s]
        if not later:return dict(status='unresolved',reason='no_observed_following_state',motion_group=g)
        j=later[0];a,b=pairs[last]['start_s'],pairs[j]['start_s'];between=range(last+1,j)
    if any(statuses[i]=='conflict' for i in between):return dict(status='unresolved',reason='conflicting_background_inside_bracket',envelope_s=[a,b])
    if b<a or b-a>SETTINGS['max_boundary_bracket_s']:
        return dict(status='unresolved',reason='boundary_bracket_too_wide',envelope_s=[a,b])
    # A hole in the decoded timeline cannot be mistaken for contiguous evidence.
    sub=pairs[min(g['indices'][0],j):max(g['indices'][-1],j)+1]
    if any(y['start_s']-x['end_s']>.01 for x,y in zip(sub,sub[1:])):
        return dict(status='unresolved',reason='cache_coverage_gap')
    return dict(status='bounded_visual_proposal',envelope_s=[a,b],estimate_s=(a+b)/2,
                width_s=b-a,motion_group=g)


def make_pairs(ep,out,cache_root):
    cache=(cache_root/'sources'/ep).with_suffix('.npz');meta=json.loads(cache.with_suffix('.json').read_text())
    assert meta['episode']==ep and meta['status']=='complete'
    assert sha(cache)==meta['npz_sha256'];pairs=[]
    with np.load(cache,allow_pickle=False) as archive:z={k:archive[k] for k in archive.files}
    for i in range(1,len(z['head_log_ns'])):
        ns0,ns1=map(int,z['head_log_ns'][i-1:i+1]);dt=(ns1-ns0)/1e9
        t0=(ns0-meta['source_start_ns'])/1e9;t1=(ns1-meta['source_start_ns'])/1e9
        coverage,q=held_interval(z['hal_log_ns'],z['head_body_q'],ns0,ns1)
        span=np.ptp(q,axis=0) if len(q) else np.full(8,np.inf)
        pose_ok=coverage['covered'] and max(span[:3])<=.3 and max(span[3:])<=.2
        fresh=bool(np.all((z['head_age_s'][i-1:i+1]>=0)&(z['head_age_s'][i-1:i+1]<=.1)))
        repeated=bool(z['head_jpeg_sha256'][i-1]==z['head_jpeg_sha256'][i] or ns1==ns0)
        roi={}
        if pose_ok and fresh and .04<dt<.18 and not repeated:
            for name,bounds in ROIS.items():
                roi[name]=[_analyze_roi(z['head_gray'][i-1,y0:y1,x0:x1].astype(float),
                                      z['head_gray'][i,y0:y1,x0:x1].astype(float),j,list(box))
                           for j,box in enumerate(bounds) for x0,y0,x1,y1 in [box]]
        else:roi={k:[] for k in ROIS}
        p=dict(start_s=t0,end_s=t1,dt_s=dt,pose_ok=bool(pose_ok),fresh=fresh,repeated=repeated,
               head_source_indices=z['head_source_indices'][i-1:i+1].tolist(),rois=roi)
        p['status']=classify(p);pairs.append(p)
    write(out/'pairs'/f'{ep.replace("/","_")}.json',dict(episode=ep,cache_path=str(cache),cache_sha256=meta['npz_sha256'],pairs=pairs))
    return pairs,meta


def one(ep,out,reuse_pairs=None,cache_root=PILOT/'dense_boundary_pilot'):
    if reuse_pairs is None:
        pairs,meta=make_pairs(ep,out,cache_root)
    else:
        source=reuse_pairs/'pairs'/f'{ep.replace("/","_")}.json'
        old=json.loads(source.read_text());cache=(cache_root/'sources'/ep).with_suffix('.npz')
        meta=json.loads(cache.with_suffix('.json').read_text())
        assert old['episode']==ep and old['cache_sha256']==meta['npz_sha256']==sha(cache)
        pairs=old['pairs']
        write(out/'pairs'/source.name,old|dict(reused_evidence_sha256=sha(source),reused_evidence_path=str(source)))
    draftpath=PRIOR/'autonomous_queue'/f'{ep.replace("/","_")}.json';draft=json.loads(draftpath.read_text())
    armpath=ARMS/'cases'/f'{ep.replace("/","_")}.json';arms=json.loads(armpath.read_text())
    anchors={(a['role'],a['boundary']):a for a in arms['anchors'] if a['status']=='soft_arm_anchor'}
    proposal=[]
    all_status={rate:[classify(p,rate) for p in pairs] for rate in SETTINGS['motion_sensitivity_px_s']}
    for phase in draft['phases']:
        role=phase['role'];support=phase['observed_support']
        for boundary in ('start_s','end_s'):
            arm=anchors.get((role,boundary));value=support[boundary]
            search=[max(0,value-1.5),min(meta['source_duration_ns']/1e9,value+1.5)]
            if arm:search=[min(search[0],arm['search_interval_s'][0]),max(search[1],arm['search_interval_s'][1])]
            tests={str(rate):locate(pairs,statuses,ROLES[role],boundary,search,support) for rate,statuses in all_status.items()}
            primary=tests['2.5'];bounded=[p for p in tests.values() if p['status']=='bounded_visual_proposal']
            stable=len(bounded)==3 and max(p['estimate_s'] for p in bounded)-min(p['estimate_s'] for p in bounded)<=.3
            proposal.append(dict(role=role,boundary=boundary,search_s=search,arm_guided=arm is not None,
                arm_event_s=arm['arm_event']['time_s'] if arm else None,primary=primary,
                sensitivity=tests,stable_under_pixel_thresholds=stable,training_approved=False))
    report=dict(episode=ep,parent_split=meta['parent_split'],boundaries=proposal,
      pair_counts=dict(Counter(p['status'] for p in pairs)),cache_sha256=meta['npz_sha256'],
      inputs={str(draftpath):sha(draftpath),str(armpath):sha(armpath)},training_approved=False)
    write(out/'cases'/f'{ep.replace("/","_")}.json',report)
    return dict(episode=ep,pairs=len(pairs),bounded=sum(p['primary']['status']=='bounded_visual_proposal' for p in proposal),
                boundaries=len(proposal),stable=sum(p['stable_under_pixel_thresholds'] for p in proposal))


def compare(out,results):
    records=[]
    for r in results:
        ep=r['episode'];stem=ep.replace('/','_');ref=PRIOR/'cases'/stem/'phase_review.json'
        if not ref.exists():continue
        truth=json.loads(ref.read_text());segments={s['id']:s for s in truth['segments']}
        if 'left_return_adjustment' in segments:
            segments['left_return']=dict(segments['left_return_adjustment'],start_s=segments['left_return_main']['start_s'])
        report=json.loads((out/'cases'/f'{stem}.json').read_text())
        for b in report['boundaries']:
            if b['role'] not in segments:continue
            s=segments[b['role']];reference=s[b['boundary']];p=b['primary'];radius=s['evidence'].get('boundary_estimate_radius_s',.5)
            result=dict(episode=ep,role=b['role'],boundary=b['boundary'],reference_s=reference,reference_radius_s=radius,
                        status=p['status'],stable=b['stable_under_pixel_thresholds'])
            if p['status']=='bounded_visual_proposal':
                result.update(estimate_s=p['estimate_s'],absolute_error_s=abs(p['estimate_s']-reference),
                              envelopes_overlap=p['envelope_s'][0]<=reference+radius and p['envelope_s'][1]>=reference-radius)
            records.append(result)
    write(out/'control_comparison.json',dict(records=records,reference_meaning='earlier visual estimates, not instrumented ground truth',training_approved=False))
    return records


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--workers',type=int,default=2)
    ap.add_argument('--controls-only',action='store_true');ap.add_argument('--reuse-pairs',type=Path)
    ap.add_argument('--candidates',type=Path,default=PILOT/'boundary_pilot_candidates.jsonl')
    ap.add_argument('--cache',type=Path,default=PILOT/'dense_boundary_pilot')
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    if args.reuse_pairs:
        cfg=json.loads((args.reuse_pairs/'config.json').read_text())
        assert cfg['registration_sha256']==sha(Path(__file__).with_name('head_background_motion.py'))
        assert {k:[list(x) for x in v] for k,v in ROIS.items()}==cfg['rois']
    rows=[json.loads(l) for l in args.candidates.read_text().splitlines()]
    assert len(rows)==len({r['episode'] for r in rows}), 'duplicate source'
    if args.controls_only:rows=[r for r in rows if (PRIOR/'cases'/r['episode'].replace('/','_')/'phase_review.json').exists()]
    write(out/'config.json',dict(settings=SETTINGS,rois=ROIS,script_sha256=sha(__file__),registration_sha256=sha(Path(__file__).with_name('head_background_motion.py')),scope=[r['episode'] for r in rows],candidate_sha256=sha(args.candidates),cache_root=str(args.cache.resolve())))
    (out/'method_snapshot.py').write_bytes(Path(__file__).read_bytes());results=[];errors=[];start=time.monotonic()
    def safe(row):
        try:return one(row['episode'],out,args.reuse_pairs,args.cache),None
        except Exception as e:return None,dict(episode=row['episode'],error=repr(e))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result,error in pool.map(safe,rows):
            if error:errors.append(error)
            else:results.append(result)
            progress=dict(completed=len(results),requested=len(rows),errors=errors,elapsed_s=time.monotonic()-start)
            write(out/'progress.json',progress);print(json.dumps(progress),flush=True)
    controls=compare(out,results);bounded=[r for r in controls if r['status']=='bounded_visual_proposal']
    summary=dict(processed=len(results),errors=errors,total_boundaries=sum(r['boundaries'] for r in results),
      bounded=sum(r['bounded'] for r in results),threshold_stable=sum(r['stable'] for r in results),
      control_boundaries=len(controls),control_bounded=len(bounded),
      control_envelopes_overlap=sum(r['envelopes_overlap'] for r in bounded),
      control_max_absolute_error_s=max((r['absolute_error_s'] for r in bounded),default=None),
      training_approved=False,labels_changed=False,results=results)
    write(out/'summary.json',summary);print(json.dumps(summary,indent=2))
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
