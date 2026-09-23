"""Engineering reader -> normalizer -> frozen IK check; labels stand in for predictions."""
import argparse
import json
from pathlib import Path
import numpy as np
from .data import Release
from .execution import ActionDecoder
from f1_data.reader import decode_eef


def run(release_path):
    release = Release(release_path, engineering=True)
    decoder = ActionDecoder(release.normalizer)
    records = []
    for ep in release.episodes:
        for t in np.unique(ep.starts[[0, len(ep.starts)//2, -1]]):
            raw = ep.sample(t)
            normalized = release.normalizer(raw)['action']
            physical = release.normalizer.denormalize_action(normalized)
            expected = decode_eef(physical, raw['anchor'])
            result = decoder.decode(normalized, raw['state'][:14])
            record = {'episode': ep.episode, 'start': int(t), 'success': result['success']}
            if result['success']:
                position, rotation = [], []
                for step, q in enumerate(result['joint_targets_deg']):
                    for arm in (0, 1):
                        actual = decoder.solver.model.fk(arm, q[arm*7:arm*7+7])
                        target = expected[step, arm]
                        position.append(np.linalg.norm(actual[:3, 3] - target[:3, 3])*1000)
                        rotation.append(np.rad2deg(np.arccos(np.clip((np.trace(actual[:3,:3] @ target[:3,:3].T)-1)/2, -1, 1))))
                record.update(max_position_mm=float(max(position)), max_rotation_deg=float(max(rotation)))
                np.testing.assert_allclose(result['gripper_targets'], physical[:20, 18:20])
                np.testing.assert_allclose(result['base_velocity_targets'], physical[:20, 20:23])
            else:
                record.update({k: result[k] for k in ('reason', 'step', 'arm') if k in result})
                assert result['joint_targets_deg'] is None
                assert 'base_velocity_targets' not in result and 'gripper_targets' not in result
            records.append(record)
    raw = release.episodes[0].sample(release.episodes[0].starts[0])
    normalized = release.normalizer(raw)['action']
    invalid = normalized.copy(); invalid[0, 3:9] = 0
    rejected = decoder.decode(invalid, raw['state'][:14])
    assert not rejected['success'] and rejected['joint_targets_deg'] is None
    assert 'base_velocity_targets' not in rejected
    invalid = normalized.copy(); invalid[0, 0] = np.nan
    assert decoder.decode(invalid, raw['state'][:14])['reason'] == 'nonfinite_action'
    return {'scope': 'label roundtrip through the model-output API; not policy inference',
            'ik_version': 'f1_ikfk_demo_v1', 'training_started': False, 'robot_commands_sent': 0,
            'prefixes': len(records), 'accepted': sum(r['success'] for r in records),
            'invalid_rotation_and_nonfinite_rejected': True, 'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = run(args.release)
    with args.output.open('x') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({k:v for k,v in report.items() if k != 'records'}, indent=2))


if __name__ == '__main__':
    main()
