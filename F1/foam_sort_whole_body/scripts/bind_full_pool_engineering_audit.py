#!/usr/bin/env python3
"""Bind completed reader checks, without approving incomplete task supervision."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'models/DM0.5'))
from f1_data.reader import Episode, sha256
from f1_training.data import Release, describe_release


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, required=True)
    args = ap.parse_args(); root = args.root.resolve()
    output = root/'engineering_release.json'
    assert not output.exists(), 'Use a new artifact; do not overwrite a binding'
    numeric_path = root/'reader_audit/report.json'
    images_path = root/'image_readback/report.json'
    statistics_path = root/'reader_audit/normalization.json'
    selection_path = root/'selection.json'
    numeric = json.loads(numeric_path.read_text())
    images = json.loads(images_path.read_text())
    selection = json.loads(selection_path.read_text())
    stats = json.loads(statistics_path.read_text())
    assert images['status'] == 'passed'
    assert images['statistics_sha256'] == sha256(statistics_path)
    assert stats['state']['range_policy']['arms'] == 'train_minmax'
    nr = {r['episode']:r for r in numeric['episodes']}
    ir = {r['episode']:r for r in images['episodes']}
    assert len(nr) == len(numeric['episodes']) == len(ir) == len(images['episodes']) == selection['accepted']
    episodes = []
    for row in selection['records']:
        if row['status'] != 'offline_prepared': continue
        stem = row['episode'].replace('/', '_')
        source = root/'cases'/stem/'composed/episode.npz'
        ep = Episode(source, root/'pose_cache'/stem/'pose_cache.npz', allow_candidates=True)
        assert ep.source_sha256 == sha256(row['provenance']) == row['source_sha256']
        assert ep.split == row['split'] == nr[ep.episode]['split'] == ir[ep.episode]['split']
        assert len(ep.starts) == row['valid_starts'] == nr[ep.episode]['valid_starts'] == ir[ep.episode]['starts_read']
        assert ep.source_sha256 == nr[ep.episode]['source_sha256'] == ir[ep.episode]['source_sha256']
        assert sha256(ep.pose_cache) == nr[ep.episode]['pose_cache_sha256']
        assert sha256(root/'reader_audit/images'/stem/'manifest.json') == ir[ep.episode]['manifest_sha256']
        assert ir[ep.episode]['status'] == 'passed'
        assert np.isfinite(nr[ep.episode]['state_dequantization_max_abs_physical_units']).all()
        episodes.append(ep)
    total = sum(len(ep.starts) for ep in episodes)
    assert total == numeric['total_starts'] == images['total_starts_read'] == selection['total_starts']
    assert images['total_image_reads'] == 3*total
    document = describe_release(episodes, statistics_path, root/'reader_audit/images')
    document['release_state'] = 'engineering_verified_pending_transition_coverage_resolution'
    document['limitations'] = ['Partial episodes; source counts do not imply complete cycles',
        'Substantial arm-transition attrition from unknown base labels remains unresolved',
        'Base labels are reconstructed weak supervision, not measured velocity',
        'No loss changes, model training, or new hardware acceptance']
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2)+'\n')
    bound = Release(output, engineering=True)
    assert len(bound.episodes) == len(episodes)
    report = dict(schema='f1_full_pool_engineering_binding_v1', status='engineering_passed', training_approved=False,
        episodes=len(episodes), by_split={s:sum(ep.split==s for ep in episodes)for s in ('train','validation')},
        total_starts=total, total_image_reads=images['total_image_reads'], unique_images=images['total_unique_images'],
        report_sha256={str(p):sha256(p) for p in [numeric_path,images_path,statistics_path,selection_path,output]},
        arm_state_range='train_minmax', validation_used_for_statistics=False,
        quantitative_errors={k:max(r[k]for r in numeric['episodes']) for k in
            ['max_action_affine_roundtrip_abs','max_eef_position_roundtrip_abs_m','max_eef_rotation_matrix_roundtrip_abs']},
        limitations=document['limitations'])
    # Per-dimension clipping counts avoid misrepresenting the union of clipped starts.
    report['arm_state_clipped_counts_per_joint_by_split'] = {s:np.sum([
        np.rint(np.array(r['state_clipped_fraction'][:14])*r['valid_starts']).astype(int)
        for r in numeric['episodes'] if r['split']==s],axis=0).tolist()for s in ['train','validation']}
    (root/'engineering_binding_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
