#!/usr/bin/env python3
"""Freeze already audited demo data; never train or issue robot commands.

New approval copies differ only in training_approved and provenance metadata.
Original sources, masks, statistics values and caches stay intact.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import numpy as np
TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'models/DM0.5'))
from f1_data.reader import Episode, sha256, PARTIAL_BASE_CONTRACT
from f1_training.data import describe_release, Release
from f1_training.validation_protocol import build_recipe_selection
from f1_training.evaluation import validate_selection


def read(p):return json.loads(Path(p).read_text())
def write(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x')as f:json.dump(v,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--image-root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--decision',type=Path,required=True);args=ap.parse_args()
    root=args.root.resolve();out=args.output.resolve();image_root=args.image_root.resolve()
    assert not out.exists(), 'Use a new output; never overwrite frozen or failed artifacts'
    selection=read(root/'selection.json');numeric=read(root/'numeric_audit/report.json')
    images=read(root/'image_readback/report.json');independent=read(root/'demo_validation.json')
    decision=read(args.decision);replay=read(decision['replay_evidence']['path'])
    assert sha256(decision['replay_evidence']['path'])==decision['replay_evidence']['sha256']
    assert decision['demo_semantic_acceptance'] is True and decision['no_further_replay_required'] is True
    assert sha256(root/'selection.json')==decision['selection_sha256']==independent['selection_sha256']
    assert independent['status']=='passed' and independent['all_original_upper_windows_preserved']
    assert images['status']=='passed' and images['statistics_sha256']==sha256(root/'numeric_audit/normalization.json')
    assert {r['sample']for r in replay['runs']}=={'020','096','093'}
    for run in replay['runs']:
        assert run['source_values_and_timestamps_match'] and run['new_partial_source_actions_identical']
        assert run['gripper_change_stop_passed'] and run['report']['source_timeline_complete']
        assert run['report']['stop']['zero_velocity_feedback_confirmed'] and not run['report']['stop']['errors']
        assert run['report']['recording']['exit_code']==0 and run['report']['recording']['metadata_present']
        directory=Path(decision['replay_evidence']['path']).parent/run['sample']/run['run']
        assert sha256(directory/'events.jsonl')==run['events_sha256']
        assert sha256(directory/'report.json')==run['report_sha256']
    nr={r['episode']:r for r in numeric['episodes']};ir={r['episode']:r for r in images['episodes']}
    vr={r['episode']:r for r in independent['episodes']}
    assert len(nr)==len(ir)==len(vr)==selection['accepted']==772
    assert numeric['total_starts']==images['total_starts_read']==selection['total_starts']==778892
    assert images['total_image_reads']==3*selection['total_starts']
    assert len({r['episode']for r in selection['records']})==772
    stats=read(root/'numeric_audit/normalization.json')
    assert stats['schema']==PARTIAL_BASE_CONTRACT and stats['source_split']=='train'
    assert stats['state']['range_policy']['arms']=='train_minmax'
    # Validate engineering binding before any approval copy is created.
    parents=[]
    for row in selection['records']:
        name=row['episode'];stem=name.replace('/','_')
        ep=Episode(root/'cases'/stem/'composed/episode.npz',root/'pose_cache'/stem/'pose_cache.npz',allow_candidates=True)
        assert ep.source_sha256==row['source_sha256']==nr[name]['source_sha256']==ir[name]['source_sha256']==vr[name]['source_sha256']
        assert ep.split==row['split']==nr[name]['split']==ir[name]['split']
        assert len(ep.starts)==row['valid_starts']==nr[name]['valid_starts']==ir[name]['starts_read']
        assert sha256(ep.pose_cache)==nr[name]['pose_cache_sha256']
        assert ir[name]['status']=='passed' and sha256(image_root/stem/'manifest.json')==ir[name]['manifest_sha256']
        assert nr[name]['max_action_affine_roundtrip_abs']<1e-4
        assert nr[name]['max_eef_position_roundtrip_abs_m']<1e-6
        assert nr[name]['max_eef_rotation_matrix_roundtrip_abs']<1e-5
        if ep.split=='train':assert not any(nr[name]['state_clipped_fraction'][:14])
        parents.append(ep)
    expected=sorted((ep.episode,ep.source_sha256,sha256(ep.pose_cache),len(ep.starts))for ep in parents if ep.split=='train')
    assert expected==sorted((r['episode'],r['sha256'],r['pose_sha256'],r['valid_starts'])for r in stats['source_packs'])
    out.mkdir(parents=True)
    evidence_paths=[args.decision,Path(decision['replay_evidence']['path']),root/'selection.json',root/'demo_validation.json',root/'numeric_audit/report.json',root/'numeric_audit/normalization.json',root/'image_readback/report.json']
    checks={str(p.resolve()):sha256(p)for p in evidence_paths}
    approved=[];mapping=[]
    for ep in parents:
        stem=ep.episode.replace('/','_');dest=out/'cases'/stem/'composed';dest.mkdir(parents=True)
        data=dict(ep.data);data['training_approved']=np.array(True)
        np.savez_compressed(dest/'episode.npz',**data)
        meta=dict(ep.meta,training_approved=True,approval_scope='demo_partial_supervision_not_precise_trajectory',
                  audited_parent=dict(path=str(ep.source),sha256=ep.source_sha256))
        write(dest/'episode.json',meta)
        cache=out/'pose_cache'/stem;cache.mkdir(parents=True)
        shutil.copy2(ep.pose_cache,cache/'pose_cache.npz')
        write(cache/'pose_cache.json',dict(ep.pose_meta,source_npz_sha256=sha256(dest/'episode.npz'),training_approved=True))
        folder=out/'images'/stem;folder.mkdir(parents=True)
        image_meta=read(image_root/stem/'manifest.json')
        assert image_meta['source_npz_sha256']==ep.source_sha256
        for camera in ('head','left','right'):(folder/camera).symlink_to(image_root/stem/camera,target_is_directory=True)
        write(folder/'manifest.json',dict(image_meta,source_npz_sha256=sha256(dest/'episode.npz'),training_approved=True,
             audited_parent_manifest_sha256=sha256(image_root/stem/'manifest.json')))
        frozen=Episode(dest/'episode.npz',cache/'pose_cache.npz')
        for k,v in ep.data.items():
            if k!='training_approved':assert np.array_equal(v,frozen.data[k],equal_nan=v.dtype.kind in 'fc'),(ep.episode,k)
        assert np.array_equal(frozen.starts,ep.starts)
        approved.append(frozen);mapping.append(dict(episode=ep.episode,parent_sha256=ep.source_sha256,frozen_sha256=frozen.source_sha256,numeric_fields_unchanged=True))
    stats['training_approved']=True
    stats['source_packs']=[dict(episode=ep.episode,sha256=ep.source_sha256,pose_sha256=sha256(ep.pose_cache),valid_starts=len(ep.starts))for ep in approved if ep.split=='train']
    stats['approval_rebinding']=dict(parent_statistics_sha256=sha256(root/'numeric_audit/normalization.json'),numeric_values_and_selected_samples_unchanged=True)
    write(out/'normalization.json',stats)
    doc=describe_release(approved,out/'normalization.json',out/'images')
    doc.update(training_approved=True,release_state='frozen_demo_partial_supervision',acceptance_evidence=checks,
       limitations=decision['limitations'],no_robot_motion_or_model_training=True)
    # Use the existing recipe to bind all validation sources and fixed windows.
    from types import SimpleNamespace
    proxy=SimpleNamespace(for_split=lambda split:[ep for ep in approved if ep.split==split])
    doc['evaluation']=build_recipe_selection(proxy,'pass_a')
    write(out/'release.json',doc)
    bound=Release(out/'release.json');validate_selection(bound)
    # Snapshot task-side execution contract so later agent edits cannot silently
    # change the frozen reader semantics. Training launch must check these hashes.
    code=[]
    for directory in ('models/DM0.5/f1_data','models/DM0.5/f1_training'):
        for p in sorted((TASK/directory).glob('*.py')):
            target=out/'code_snapshot'/p.relative_to(TASK);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
            code.append(dict(path=str(p),sha256=sha256(p),snapshot=str(target)))
    write(out/'code_manifest.json',code)
    shutil.copy2(TASK/'models/DM0.5/experiment.yaml',out/'experiment.yaml')
    receipt=dict(status='frozen_demo_training_data',training_approved=True,episodes=len(approved),
        by_split={s:len(bound.for_split(s))for s in ('train','validation')},total_starts=sum(len(e.starts)for e in approved),
        release=str(out/'release.json'),release_sha256=sha256(out/'release.json'),evidence=checks,source_approval_mapping=mapping,
        code_manifest_sha256=sha256(out/'code_manifest.json'),model_training_started=False,robot_motion_sent=False,
        scope='Demo data/reader freeze; not closed-loop learned-policy success or precise chassis calibration')
    write(out/'freeze_receipt.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items()if k not in ('source_approval_mapping','evidence')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
