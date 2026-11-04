#!/usr/bin/env python3
"""Build a new right-only V1/correction collection without rewriting originals."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TASK/'src'), str(TASK/'scripts'), str(TASK/'DataPrehandle')]
from f1_toys.processing import load_contract, sha256
from f1_toys.dataset import F1Dataset
from prepare_formal import fit_statistics
from prepare_hil_correction import prepare
from review_home import review as review_home


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)+'\n'
    if path.exists() and path.read_text() != text:
        raise ValueError('Refusing changed prepared evidence: '+str(path))
    path.write_text(text)


def correction_review(raw, source, group, confirmation_sha):
    manifest = json.loads((raw/'journal/manifest.json').read_text())
    if manifest['binding'].get('process_run_id') != group:
        raise ValueError('Correction session differs from the approved split group')
    if manifest['episode_id'] != source['episode']:
        raise ValueError('Correction episode identity differs from the inventory')
    home = review_home(raw, source['manifest_sha256'])
    accepted = [h for h in home['decisions'] if h['accepted']]
    if len(accepted) != 1 or len(home['decisions']) != 1:
        raise ValueError('Expected exactly one independently completed V1 Home')
    h = accepted[0]
    controls = [json.loads(l) for l in (raw/'journal/control.jsonl').open()]
    human = [c for c in controls if c['source'].startswith('human_')
             and c['hil_state']=='hil_latched' and c['publication_complete'] is True]
    active = [c for c in human if c['source']!='human_hold' and c['monotonic_ns']<h['start_ns']]
    start, end = active[0]['monotonic_ns'], human[-1]['monotonic_ns']
    hold_start = h['end_ns']+1
    assert end-hold_start >= 2_000_000_000
    assert all(c['submitted'][15] <= 1 and c['measured'][15] <= 1
               for c in human if c['monotonic_ns']>=hold_start)
    # Phase boundaries describe actual gripper commands, not visual insertion truth.
    opening = next(c['monotonic_ns'] for c in active if c['submitted'][15]<95)
    opened = next(c['monotonic_ns'] for c in human
                  if opening <= c['monotonic_ns'] < h['start_ns'] and c['submitted'][15]<=1)
    assert start < opening <= opened < h['start_ns'] < hold_start < end
    phases = [('alignment_insertion',start,opening-1),('release',opening,opened),
              ('withdrawal',opened+1,h['start_ns']-1),('native_home',h['start_ns'],h['end_ns']),
              ('terminal_hold',hold_start,end)]
    return dict(contract='f1_upper_16d_v1',manifest_sha256=source['manifest_sha256'],
        human_review_accepted=True,reviewer='User confirmed nine successful all-gamepad corrections; recipe authorized 2026-10-21',
        user_confirmation_sha256=confirmation_sha,task_success=True,
        accepted_interventions=sorted({c['intervention_id'] for c in human}),
        accepted_time_ranges_ns=[[start,end]],
        anchor_time_ranges_ns=[[start,min(end,h['end_ns']+3_000_000_000)]],
        dataset_role='correction',source_group=group,split='train',
        environment_assistance_ranges_ns=[],home_review=home,
        terminal_hold_range_ns=[hold_start,end],terminal_stable_confirmed=True,
        phase_ranges=[dict(stage=s,start_ns=a,end_ns=b) for s,a,b in phases],
        phase_boundary_basis='Published gripper command onset/open and completed Home events; not visual depth annotation')


def link_image(source, target, expected):
    if target.exists():
        if sha256(target) != expected:raise ValueError('Image identity collision')
        return
    if sha256(source) != expected:raise ValueError('Source image changed')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:os.link(source,target)
    except OSError:shutil.copyfile(source,target)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('raw-root','evidence','baseline','heldout-correction','contract','work','output'):
        p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--reviews-only',action='store_true')
    a=p.parse_args()
    cfg=load_contract(a.contract)
    inventory=json.loads((a.evidence/'batch-inventory.json').read_text())
    confirmation=json.loads((a.evidence/'user-success-confirmation.json').read_text())
    assert set(confirmation['scope'])=={r['episode'] for r in inventory['episodes']}
    assert cfg['input_version']=='right_only_v1'
    group='f1_hil_20261021T093218Z_1eb32b6e'
    corrections=[]; reviews=[]
    for source in inventory['episodes']:
        raw=a.raw_root/source['episode']
        review=correction_review(raw,source,group,sha256(a.evidence/'user-success-confirmation.json'))
        review_path=a.work/'reviews'/(source['episode']+'.json')
        write(review_path,review);reviews.append(review)
        if a.reviews_only:continue
        out=a.work/'corrections'/source['episode']
        if not out.exists():
            out.parent.mkdir(parents=True,exist_ok=True)
            print(json.dumps(dict(preparing=source['episode'])),flush=True)
            prepare(raw,review_path,a.baseline/'train',out,contract_path=a.contract)
        else:
            assert json.loads((out/'review.json').read_text())==review
            old=load_contract(out/'contract.json')
            assert old['input_version']==cfg['input_version'] and old['active_cameras']==cfg['active_cameras']
        corrections.append(out)
    if a.reviews_only:
        print(json.dumps(dict(reviews=len(reviews),training_started=False)));return
    partial=a.output.with_name(a.output.name+'.partial')
    if a.output.exists() or partial.exists():raise FileExistsError(a.output)
    partial.mkdir(parents=True)
    shutil.copytree(a.work/'reviews',partial/'reviews')
    shutil.copy2(a.evidence/'user-success-confirmation.json',partial/'user-confirmation.json')
    shutil.copy2(a.baseline/'home-review.json',partial/'v1-home-review.json')
    shutil.copy2(a.heldout_correction/'review.json',partial/'heldout-correction-review.json')
    cfg['home_supervision']['review_files_sha256']={p.name:sha256(p) for p in
        [*(partial/'reviews').glob('*.json'),partial/'v1-home-review.json',partial/'heldout-correction-review.json']}
    sampling=dict(method='source_episode_anchor_v1',counts={'original_demo':96,'correction':32},global_batch=128)
    loaded={}; splits={}; source_groups={}; identities=set()
    for split in ('train','validation'):
        sources=[(a.baseline/split,'original_demo')]+([(x,'correction') for x in corrections]
            if split=='train' else [(a.heldout_correction,'correction')])
        dest=partial/split;dest.mkdir()
        records=[];arrays={k:[] for k in ('states','actions','submitted','action_time_mask')}
        entries=[];eval_indices=[];provenance=[]
        for root,role in sources:
            m=json.loads((root/'manifest.json').read_text())
            for name,digest in m['files_sha256'].items():
                if sha256(root/name)!=digest:raise ValueError('Source package changed: '+str(root/name))
            source_cfg=load_contract(root/'contract.json')
            for key in ('fps','horizon','prompt','action_semantics','kinematics_sha256'):
                assert source_cfg[key]==cfg[key],key
            offset=len(records)
            rows=[json.loads(line) for line in (root/'samples.jsonl').open()]
            for r in rows:
                r=dict(r,dataset_role=role)
                r['images']={c:r['images'][c] for c in cfg['active_cameras']}
                assert set(r['action_sources'])<={'human_arm','human_hand','human_arm_and_hand','human_hold','home'}
                for rel in r['images'].values():link_image(root/rel,dest/rel,m['files_sha256'][rel])
                records.append(r)
            with np.load(root/'arrays.npz',allow_pickle=False) as data:
                for k in arrays:arrays[k].append(data[k])
            if split=='validation':
                eval_indices.extend(offset+i for i in json.loads((root/'eval_indices.json').read_text()))
            for source in m['source_episodes']:
                ep=source['episode'];digest=source['manifest_sha256']
                assert ep not in identities and digest not in identities
                identities.update((ep,digest))
                g=m.get('source_group') or source.get('source_group') or ep
                assert g not in source_groups or source_groups[g]==split
                source_groups[g]=split
                entries.append(dict(source,dataset_role=role,source_group=g))
            provenance.append(dict(package=str(root),manifest_sha256=sha256(root/'manifest.json')))
        arrays={k:np.concatenate(v) for k,v in arrays.items()}
        assert all(np.isfinite(v).all() for v in arrays.values())
        if split=='train':stats=fit_statistics(arrays['states'],arrays['actions'],records,arrays['action_time_mask'])
        write(dest/'contract.json',cfg);write(dest/'norm_stats.json',stats);write(dest/'eval_indices.json',eval_indices)
        (dest/'samples.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        np.savez_compressed(dest/'arrays.npz',**arrays)
        loaded[split]=(dest,records,arrays,entries,provenance)
        splits[split]=entries
    write(partial/'split.json',dict(schema='v1_dagger_split_v1',train=splits['train'],validation=splits['validation'],
        frozen_v1_split_sha256=sha256(a.baseline/'split.json'),standard_layout_only=True))
    reports={}
    for split,(dest,rows,arrays,entries,provenance) in loaded.items():
        m=dict(schema='f1_toys_eef_dataset_v3',smoke_only=False,synthetic=False,split=split,
            formal_training_ready=False,human_success_reviewed=True,source_episodes=entries,
            source_package_provenance=provenance,sampling=sampling,samples=len(rows),windows=len(rows),
            partial_windows=int((~arrays['action_time_mask'].all(1)).sum()),
            valid_action_steps=int(arrays['action_time_mask'].sum()),
            dataset_role_counts=dict(Counter(r['dataset_role'] for r in rows)),
            phase_counts=dict(Counter(r['supervision_phase'] for r in rows)),
            input_version=cfg['input_version'],v1_anchor_policy='Retain frozen conservative V1 anchors; no temporal re-pairing',
            correction_anchor_policy='Two required model cameras; first active human to Home completion plus 3s; labels uncapped',
            contract_sha256=sha256(dest/'contract.json'),norm_stats_sha256=sha256(dest/'norm_stats.json'),
            split_sha256=sha256(partial/'split.json'),
            files_sha256={str(f.relative_to(dest)):sha256(f) for f in dest.rglob('*') if f.is_file()})
        write(dest/'manifest.json',m)
        ds=F1Dataset(dest)
        for i in range(len(ds)):
            obs,action,mask=ds[i]
            assert mask.sum()==rows[i]['valid_action_steps'] and np.isfinite(action).all()
            assert (action[~mask]==0).all() and not obs['image_mask']['left_wrist_0_rgb']
            assert not obs['state'][[0,1,2,3,4,5,6,14,*range(16,32)]].any()
        m['formal_training_ready']=True
        m['data_validation']='All actual reader samples passed; model/distributed preflight separate'
        (dest/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
        reports[split]={k:v for k,v in m.items() if k!='files_sha256'}
        print(json.dumps(dict(split=split,samples=len(ds),phases=m['phase_counts'])),flush=True)
    write(partial/'manifest.json',dict(schema='f1_toys_collection_v1',splits=reports,
        recipe='V1_DAgger_FullSFT',sampling=sampling,builder_sha256=sha256(Path(__file__)),
        files_sha256={str(f.relative_to(partial)):sha256(f) for f in partial.rglob('*') if f.is_file()}))
    os.replace(partial,a.output)
    print(json.dumps(dict(result='PASS',output=str(a.output),splits=reports)),flush=True)


if __name__=='__main__':main()
