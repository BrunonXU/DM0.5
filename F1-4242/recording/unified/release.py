"""Immutable training release: source-session splits and train-only statistics."""
import json
from pathlib import Path
import numpy as np
from .contract import atomic_json, confined, sha256, NAMES, CONTRACT


def build(spec, output):
    output=Path(output);confined(output.parent,output.name)
    if output.exists():raise FileExistsError(output)
    datasets=spec.get('datasets',[])
    if not datasets:raise ValueError('no datasets')
    sessions={};seen=set();seen_observations={};manifest=[];train=[]
    for entry in datasets:
        root=Path(entry['path']).resolve(strict=True);split=entry['split']
        if split not in ('train','validation','test'):raise ValueError('invalid split')
        audit=json.loads(confined(root,'f1_audit.json').read_text())
        files=json.loads(confined(root,'f1_files.json').read_text())['files']
        required={'f1_audit.json','f1_source.jsonl','f1_review.json','f1_contract.json','meta/info.json'}
        if not required.issubset(files):raise ValueError('incomplete release manifest')
        for rel,digest in files.items():
            if sha256(confined(root,rel))!=digest:raise ValueError('dataset changed: '+rel)
        if not audit.get('training_ready') or not audit.get('reader_roundtrip_verified') or audit['source'].get('synthetic'):
            raise ValueError('only human-reviewed, verified, non-synthetic datasets may be released')
        count=0
        for line in confined(root,'f1_source.jsonl').open():
            row=json.loads(line);session=row['session_id']
            if session in sessions and sessions[session]!=split:raise ValueError('source-session leakage between splits')
            sessions[session]=split
            key=(session,row['time_ns'])
            if key in seen:raise ValueError('duplicate source row across datasets')
            source_key=(session,row.get('source_observation_monotonic_ns',row['time_ns']))
            if source_key in seen_observations and seen_observations[source_key]!=root:
                raise ValueError('duplicate source observation across datasets')
            seen_observations[source_key]=root
            seen.add(key);count+=1
            if split=='train':train.append(row)
        if count!=audit['frames']:raise ValueError('provenance count differs')
        manifest.append(dict(path=str(root),split=split,frames=count,manifest_sha256=sha256(root/'f1_files.json')))
    if not train:raise ValueError('train split is empty')
    stats={}
    for key in ('state','action'):
        a=np.asarray([r[key] for r in train],dtype=np.float64)
        stats[key]=dict(mean=a.mean(0).tolist(),std=a.std(0).tolist(),min=a.min(0).tolist(),max=a.max(0).tolist(),count=len(a))
    result=dict(schema='f1_training_release_v1',contract=CONTRACT,names=NAMES,datasets=manifest,
        session_splits=sessions,statistics_basis='train_only',statistics=stats,
        model_preprocessing='model-specific resizing/normalization belongs to training adapter',
        uploaded=False)
    output.parent.mkdir(parents=True,exist_ok=True)
    # A release is a new manifest; never rewrite datasets or their review.
    with output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    return result


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--spec',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();build(json.loads(Path(a.spec).read_text()),a.output)

if __name__=='__main__':main()
