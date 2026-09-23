#!/usr/bin/env python3
"""Materialize complete, stable phase proposals; keep unknown gaps and all19 masks.

Artifacts deliberately use proposal-only keys. They are NOT approved episode
packs. Existing reader/train manifests cannot consume them accidentally.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np

TASK=Path(__file__).resolve().parents[1]
ROUTE={'back_out':(0,-.5),'right_out':(1,-1.6),'forward_out':(0,.5),
       'back_return':(0,-.5),'left_return':(1,1.6)}


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def build(case,pairs,parent,t,tail):
    keys={(b['role'],b['boundary']):b for b in case['boundaries']}
    if any((r,k) not in keys or keys[r,k]['primary']['status']!='bounded_visual_proposal'
           or not keys[r,k]['stable_under_pixel_thresholds'] for r in ROUTE for k in ('start_s','end_s')):
        return None,dict(status='deferred',reason='missing_or_unstable_boundary')
    moves=[]
    for role,(axis,distance) in ROUTE.items():
        begin,end=[keys[role,k]['primary']['estimate_s'] for k in ('start_s','end_s')]
        if not 0<=begin<end<=tail:return None,dict(status='deferred',reason='invalid_phase_or_after_parent_tail')
        moves.append(dict(role=role,start_s=begin,end_s=end,axis=axis,distance_prior_m=distance))
    ordered=sorted(moves,key=lambda m:m['start_s'])
    if any(a['end_s']>b['start_s'] for a,b in zip(ordered,ordered[1:])):
        return None,dict(status='deferred',reason='overlapping_phases')
    v=np.full((len(t),3),np.nan,dtype=np.float32);static=np.zeros(len(t),bool)
    for p in pairs:
        if p['status']=='stationary_support':static|=(t>=p['start_s'])&(t<p['end_s'])
    v[static]=0
    contradictions=[]
    for m in moves:
        begin,end=m['start_s'],m['end_s']
        interior=(t>begin+.5)&(t<end-.5)
        if np.any(static&interior):
            contradictions.append(dict(role=m['role'],stationary_interior_frames=int((static&interior).sum())))
        take=(t>=begin)&(t<=end);u=(t[take]-begin)/(end-begin)
        v[take]=0;v[take,m['axis']]=m['distance_prior_m']*6*u*(1-u)/(end-begin)
        # Complete only the observed transition envelope, not every blank
        # between stages. These values remain explicit weak point estimates.
        for which,value in [('start_s',begin),('end_s',end)]:
            envelope=keys[m['role'],which]['primary']['envelope_s']
            if which=='start_s':
                neighbours=[p for p in pairs if p['status']=='stationary_support' and envelope[0]-.01<=p['end_s']<=value]
                if neighbours:
                    a=max(p['end_s'] for p in neighbours);v[(t>=a)&(t<value)]=0
            else:
                neighbours=[p for p in pairs if p['status']=='stationary_support' and value<=p['start_s']<=envelope[1]+.01]
                if neighbours:
                    b=min(p['start_s'] for p in neighbours);v[(t>value)&(t<b)]=0
    if contradictions:return None,dict(status='deferred',reason='stationary_evidence_inside_motion',conflicts=contradictions)
    v[t>=tail]=np.nan
    finite=np.isfinite(v).all(1)
    valid=finite&parent['state_valid'].all(1)&parent['action_candidate_valid'].all(1)
    chunks=np.zeros(len(t),bool)
    for i in np.flatnonzero(parent['chunk_start_candidate_valid']):
        if i+50<=len(t) and parent['observation_candidate_valid'][i] and valid[i:i+50].all():chunks[i]=True
    moving=np.any(np.nan_to_num(v)!=0,axis=1)
    transition=[int(i) for i in np.flatnonzero(chunks) if moving[i:i+50].any() and (~moving[i:i+50]).any()]
    report=dict(status='joint_timing_proposal',motion_stages=moves,proposed_joint_frames=int(valid.sum()),
       proposed_h50_starts=int(chunks.sum()),stationary_motion_transition_h50=len(transition),
       transition_start_indices=transition,unknown_frames=int((~finite).sum()),
       max_abs_velocity=np.max(np.abs(v[finite]),axis=0).tolist() if finite.any() else None,
       limits='movement interiors use route continuity prior; automatic background timing is not physical ground truth',
       training_approved=False,arm_gripper_values_changed=False,parent_mask_relaxed=False)
    return dict(time_ns=parent['time_ns'],base_velocity_proposal=v,proposed_joint_valid=valid,
                proposed_chunk_start=chunks,training_approved=np.array(False)),report


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--refinement',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    maskpath=TASK/'results/demo_772_selection/mask_manifest.jsonl';selpath=TASK/'results/demo_772_selection/candidates.jsonl'
    masks={r['episode']:r for r in map(json.loads,maskpath.read_text().splitlines())}
    rows={r['episode']:r for r in map(json.loads,selpath.read_text().splitlines())};results=[]
    for p in sorted((args.refinement/'cases').glob('*.json')):
        case=json.loads(p.read_text());ep=case['episode'];m=masks[ep];row=rows[ep]
        assert case['parent_split']==m['parent_split']==row['parent_split']
        for name,expected in case['inputs'].items():assert sha(name)==expected
        arm_record=json.loads((TASK/'results/arm_base_anchors_20260917_v1/cases'/p.name).read_text())
        assert arm_record['source_sha256'].get(m['aligned_npz_path'])==m['aligned_npz_sha256']
        assert sha(m['aligned_npz_path'])==m['aligned_npz_sha256']
        pairpath=args.refinement/'pairs'/p.name;pairrecord=json.loads(pairpath.read_text());pairs=pairrecord['pairs']
        assert pairrecord['cache_sha256']==case['cache_sha256']
        with np.load(m['aligned_npz_path']) as z:parent={k:z[k] for k in z.files}
        t=(parent['time_ns']-row['source_start_ns'])/1e9
        data,report=build(case,pairs,parent,t,m['tail_mask_start_s'])
        report.update(episode=ep,parent_split=m['parent_split'],training_approved=False,
          inputs={str(p.resolve()):sha(p),str(pairpath.resolve()):sha(pairpath),m['aligned_npz_path']:m['aligned_npz_sha256']})
        if data:
            dest=out/ep.replace('/','_');dest.mkdir()
            np.savez_compressed(dest/'proposal.npz',**data);report['npz_sha256']=sha(dest/'proposal.npz')
            # Independent direct checks, not a second call to generator.
            with np.load(dest/'proposal.npz') as z:
                assert np.array_equal(z['time_ns'],parent['time_ns'])
                assert np.isnan(z['base_velocity_proposal'][t>=m['tail_mask_start_s']]).all()
                for i in np.flatnonzero(z['proposed_chunk_start']):
                    assert z['proposed_joint_valid'][i:i+50].all()
                    assert np.isfinite(z['base_velocity_proposal'][i:i+50]).all()
                    assert parent['action_candidate_valid'][i:i+50].all()
            write(dest/'report.json',report)
        results.append(report)
    summary=dict(scope=len(results),materialized=sum(r['status']=='joint_timing_proposal' for r in results),
       h50_starts=sum(r.get('proposed_h50_starts',0) for r in results),
       transition_h50_starts=sum(r.get('stationary_motion_transition_h50',0) for r in results),
       training_approved=False,formal_manifest_changed=False,results=results,
       script_sha256=sha(__file__),mask_registry_sha256=sha(maskpath),selection_sha256=sha(selpath))
    write(out/'summary.json',summary);(out/'method_snapshot.py').write_bytes(Path(__file__).read_bytes())
    print(json.dumps({k:v for k,v in summary.items() if k!='results'},indent=2))
    for r in results:
        if r['status']=='joint_timing_proposal' or r.get('reason')!='missing_or_unstable_boundary':print(r['episode'],r['status'],r.get('reason'),r.get('stationary_motion_transition_h50'))

if __name__=='__main__':main()
