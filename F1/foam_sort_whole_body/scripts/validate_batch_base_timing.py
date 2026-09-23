#!/usr/bin/env python3
"""Independent artifact/whole-window audit. Does not certify scene semantics."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run',type=Path,required=True);args=ap.parse_args()
    root=args.run.resolve();task=Path(__file__).resolve().parents[1]
    config=json.loads((root/'run_config.json').read_text())
    assert digest(root/'method_snapshot.py')==config['script_sha256']
    selection=task/'results/demo_772_selection/candidates.jsonl';maskpath=task/'results/demo_772_selection/mask_manifest.jsonl'
    assert digest(selection)==config['selection_sha256'];assert digest(maskpath)==config['mask_sha256']
    masks={r['episode']:r for r in map(json.loads,maskpath.read_text().splitlines())}
    checked=[];failures=[]
    rows=[json.loads(l) for l in (root/'index.jsonl').read_text().splitlines()]
    assert len(rows)==len({r['episode'] for r in rows})==config['scope']
    for row in rows:
        ep=row['episode'];report=json.loads(Path(row['path']).read_text());npz=Path(row['path']).with_name('proposal.npz')
        try:
            assert digest(npz)==report['proposal_sha256']
            for p,h in report['source_sha256'].items():assert digest(p)==h,p
            m=masks[ep];assert report['parent_split']==m['parent_split']
            assert digest(m['aligned_npz_path'])==m['aligned_npz_sha256']
            with np.load(m['aligned_npz_path'],allow_pickle=False) as z: parent={k:z[k] for k in z.files}
            with np.load(npz,allow_pickle=False) as z: proposal={k:z[k] for k in z.files}
            assert np.array_equal(parent['time_ns'],proposal['time_ns'])
            assert not bool(proposal['training_approved'])
            velocity=proposal['base_velocity_proposal'];support=proposal['automatic_support'];joint=proposal['proposed_joint_valid'];chunks=proposal['proposed_chunk_start']
            assert velocity.shape==(len(joint),3)
            assert np.isfinite(velocity[support]).all() and np.isnan(velocity[~support]).all()
            assert not joint[~support].any()
            assert not joint[m['tail_mask_start_index']:].any()
            assert not np.any(joint&~parent['action_candidate_valid'].all(1))
            assert not np.any(joint&~parent['state_valid'].all(1))
            assert not np.any(chunks&~parent['chunk_start_candidate_valid'])
            transitions=0
            for i in np.flatnonzero(chunks):
                assert i+50<=len(joint) and joint[i:i+50].all()
                assert parent['observation_candidate_valid'][i]
                values=velocity[i:i+50];moving=np.any(values!=0,axis=1)
                transitions+=bool(moving.any() and (~moving).any())
            checked.append(dict(episode=ep,checked_h50_starts=int(chunks.sum()),
                                windows_with_both_stationary_and_motion=transitions))
        except Exception as e:failures.append(dict(episode=ep,error=f'{type(e).__name__}: {e}'))
    result=dict(scope=len(rows),checked=len(checked),failures=failures,
                h50_starts_checked=sum(r['checked_h50_starts'] for r in checked),
                windows_with_both_stationary_and_motion=sum(r['windows_with_both_stationary_and_motion'] for r in checked),
                training_approved=False,formal_reader_verified=False,
                limitation='structural checks and preserved inputs; no statement that pixels prove physical stops',
                records=checked,validator_sha256=digest(__file__))
    (root/'independent_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='records'},ensure_ascii=False,indent=2))
    if failures:raise SystemExit(1)

if __name__=='__main__':main()
