#!/usr/bin/env python3
"""Recheck previously unresolved boundaries using adjacent cached evidence.

The candidate component/search, maximum bracket and sensitivity tests do not
change. Only evidence immediately outside a search edge becomes visible.
"""
import argparse
import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from refine_arm_guided_base_boundaries import TASK, PRIOR, ROLES, SETTINGS, locate, classify, sha, write


def one(job):
    path,out=job;case=json.loads(path.read_text());ep=case['episode']
    pairpath=path.parents[1]/'pairs'/path.name
    pairs=json.loads(pairpath.read_text())['pairs']
    draftpath=PRIOR/'autonomous_queue'/path.name
    phases={p['role']:p['observed_support']for p in json.loads(draftpath.read_text())['phases']}
    statuses={rate:[classify(p,rate)for p in pairs]for rate in SETTINGS['motion_sensitivity_px_s']}
    repaired=[]
    for b in case['boundaries']:
        if b['primary']['status']=='bounded_visual_proposal' and b['stable_under_pixel_thresholds']:continue
        tests={str(rate):locate(pairs,s,ROLES[b['role']],b['boundary'],b['search_s'],phases[b['role']],context_s=.5)
               for rate,s in statuses.items()}
        bounded=[r for r in tests.values()if r['status']=='bounded_visual_proposal']
        stable=len(bounded)==3 and max(r['estimate_s']for r in bounded)-min(r['estimate_s']for r in bounded)<=.3
        if stable:
            repaired.append(dict(role=b['role'],boundary=b['boundary'],old_status=b['primary'],new_status=tests['2.5']))
            b.update(primary=tests['2.5'],sensitivity=tests,stable_under_pixel_thresholds=True,context_evidence_margin_s=.5)
    if repaired:
        case['inputs'].update({str(path.resolve()):sha(path),str(pairpath.resolve()):sha(pairpath)})
        write(out/'cases'/path.name,case)
        write(out/'pairs'/path.name,json.loads(pairpath.read_text()))
    return dict(episode=ep,repaired=repaired)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--workers',type=int,default=4)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    roots=[TASK/'results/arm_guided_dense_20260917_v3',TASK/'results/arm_guided_full_pool_20260917/refinement']
    paths=[p for root in roots for p in sorted((root/'cases').glob('*.json'))]
    rows=[]
    with ProcessPoolExecutor(max_workers=args.workers)as pool:
        for row in pool.map(one,[(p,args.output.resolve())for p in paths]):
            rows.append(row)
            write(args.output/'progress.json',dict(complete=len(rows),total=len(paths),improved=sum(bool(r['repaired'])for r in rows)))
    summary=dict(scope=len(rows),improved_sources=sum(bool(r['repaired'])for r in rows),
        repaired_boundaries=sum(len(r['repaired'])for r in rows),training_approved=False,results=rows,
        locator_sha256=sha(TASK/'scripts/refine_arm_guided_base_boundaries.py'))
    write(args.output/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items()if k!='results'}))


if __name__=='__main__':main()
