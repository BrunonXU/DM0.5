#!/usr/bin/env python3
"""Summarize native-solver diagnostics without treating proposed joints as motion."""
import argparse
import json
from pathlib import Path

import numpy as np

from audit_native_kinematics import LOW, HIGH
from kinematics_sdk_worker import sha256


def summarize(root):
    result = {'scope':'returned numeric IK solutions, not executed robot motion', 'modes':{}}
    for mode in ['baseline','controller_limits','fixed_reference']:
        folder = root if mode == 'baseline' else root/mode
        groups, cases, last = {}, {}, {}
        with (folder/'responses.jsonl').open() as src:
            header = json.loads(next(src))
            if header['requests_sha256'] != sha256(folder/'requests.jsonl'):
                raise RuntimeError('Request identity mismatch')
            if mode == 'controller_limits':
                pnva = np.array(header['contract']['effective_pnva'])
                np.testing.assert_array_equal(pnva[:,:,0],np.broadcast_to(HIGH,(2,7)))
                np.testing.assert_array_equal(pnva[:,:,1],np.broadcast_to(LOW,(2,7)))
            for line in src:
                r = json.loads(line)
                if 'ik' not in r or ':chunk:' not in r['id']:
                    continue
                case,_,start,k = r['id'].split(':'); k=int(k)
                arm,ik = r['arm'],r['ik']
                q,ref = np.array(ik['joints']),np.array(ik['reference'])
                excess = float(max(0,np.max(np.maximum(LOW-q,q-HIGH))))
                ctl_bad = excess > 1e-5
                bad = not ik['success'] or ik['out_range'] or ik['joint_exceeded'] or ctl_bad
                chain=(case,start,arm)
                previous = last.get(chain)
                adjacent = float(np.max(abs(q-previous[0]))) if k and previous is not None else None
                previous_admissible = previous is not None and previous[1]
                last[chain] = (q, not bad)
                for horizon in (20,50):
                    if k>=horizon:
                        continue
                    key=f'{"left" if arm==0 else "right"}_first{horizon}'
                    g=groups.setdefault(key,{'calls':0,'sdk_failed':0,'sdk_joint_exceeded':0,
                        'controller_joint_exceeded':0,'any_limit_or_solver_failure':0,
                        'max_controller_excess_deg':0,'max_subsequent_reference_distance_deg':0,
                        'max_adjacent_returned_change_deg':0,'max_admissible_adjacent_change_deg':0})
                    g['calls']+=1;g['sdk_failed']+=not ik['success'];g['sdk_joint_exceeded']+=ik['joint_exceeded']
                    g['controller_joint_exceeded']+=ctl_bad;g['any_limit_or_solver_failure']+=bad
                    g['max_controller_excess_deg']=max(g['max_controller_excess_deg'],excess)
                    if k:
                        g['max_subsequent_reference_distance_deg']=max(g['max_subsequent_reference_distance_deg'],float(abs(q-ref).max()))
                    if adjacent is not None:
                        g['max_adjacent_returned_change_deg']=max(g['max_adjacent_returned_change_deg'],adjacent)
                        if not bad and previous_admissible:
                            g['max_admissible_adjacent_change_deg']=max(g['max_admissible_adjacent_change_deg'],adjacent)
                    c=cases.setdefault(f'{case}_{key}',{'calls':0,'failures':0})
                    c['calls']+=1;c['failures']+=bad
        result['modes'][mode]={'groups':groups,'cases':cases,'contract':header['contract'],
                              'responses_sha256':sha256(folder/'responses.jsonl')}
    (root/'diagnostic_comparison.json').write_text(json.dumps(result,indent=2)+'\n')
    for mode,data in result['modes'].items():
        print(mode,json.dumps(data['groups']))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    summarize(parser.parse_args().root.resolve())
