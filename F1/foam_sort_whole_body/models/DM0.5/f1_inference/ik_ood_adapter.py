"""Temporary OOD variant: 5mm position tolerance, all other IK options pinned."""
import argparse
from copy import deepcopy
from dataclasses import replace

import numpy as np

from f1_training.execution import frozen_solver
from f1_inference.native_adapter import create_app

RECIPE = 'f1_ikfk_demo_pos5mm_v1'
OPTIONS = {'position_tolerance_mm': 5.0, 'rotation_tolerance_deg': 0.001}


def ood_solver(*, options=None):
    solver = frozen_solver()
    solver.options = replace(solver.options, **(OPTIONS if options is None else options))
    return solver


def decode_response(response, solver, *, recipe=RECIPE, options=OPTIONS):
    result = deepcopy(response)
    if response.get('status') != 'ready' and not (response.get('status') == 'rejected' and response.get('reason') == 'ik_rejected'):
        raise ValueError('OOD adapter only decodes ready or checked IK-rejected model output')
    if response.get('ik_recipe_version') != 'f1_ikfk_demo_v1':
        raise ValueError('Unexpected upstream IK version')
    if any(getattr(solver.options, k) != v for k, v in options.items()):
        raise ValueError('OOD solver options differ from declared recipe')
    physical = np.asarray(response['diagnostics']['physical_action23'], float)
    if physical.shape != (50, 23) or not np.isfinite(physical).all():
        raise ValueError('Expected finite physical H50/23')
    solved = solver.solve_eef_prefix(physical, response['anchor_joints14_deg'],
                                     response['reference_joints14_deg'], execute_steps=20)
    for key in ('joint_targets_deg', 'gripper_targets', 'base_velocity_targets', 'reason', 'ik'):
        result.pop(key, None)
    result.update(ik_recipe_version=recipe, ik_options=dict(options), execute_steps=20,
                  execution_mode='sync_raw_h20_v1')
    result['diagnostics']['ik_details'] = solved.get('diagnostics', [])
    if not solved['success']:
        result.update(status='rejected', reason=solved.get('reason', 'ik_rejected'),
                      ik={k: solved[k] for k in ('arm', 'step') if k in solved})
        if solved.get('diagnostics'):
            result['ik']['failure'] = solved['diagnostics'][-1]
        return result
    result['status'] = 'ready'
    for key, width in [('joint_targets_deg', 14), ('gripper_targets', 2), ('base_velocity_targets', 3)]:
        values = np.asarray(solved[key], float)
        if values.shape != (20, width) or not np.isfinite(values).all():
            raise ValueError('Invalid decoded prefix')
        result[key] = values.tolist()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=7899)
    args = parser.parse_args()
    create_app(ood_solver(), response_decoder=decode_response, health_fields={
        'execution_mode': 'sync_raw_h20_v1', 'execute_steps': 20,
        'ik_recipe_version': RECIPE, 'ik_options': OPTIONS,
        'adapter': 'temporary_ood_position_5mm; unchanged_model_and_other_ik_options',
    }).run(host='127.0.0.1', port=args.port, threaded=True, use_reloader=False)
