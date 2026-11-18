"""A supported release may change only the gripper, even if other motion is clear."""
from copy import deepcopy
from dataclasses import replace

import pytest

from f1_harness.contracts import HarnessError, digest
from f1_harness.revalidation import revalidate_candidate
from test_full_development_skills import scene
from test_plan_revalidation import scenario


def supported(scenario):
    service, args, clock, original, latest, intent = scenario
    service.task = service.task.model_copy(update={
        'allowed_skills': (*service.task.allowed_skills, 'release_supported')})
    intent = intent.model_copy(update={'skill': 'release_supported', 'task_hash': digest(service.task)})
    measured = dict(held=True, relation='geometric_contact_candidate', stationary=True,
                    reason='support_load_unresolved_while_held_or_moving')
    geometry = replace(scene(), support_measurement=measured, effective_clearance_m=.003)
    service._scene = lambda *a, **kw: geometry
    held = list(args['held16']); held[15] = geometry.grasp_opening
    args.update(intent=intent.model_dump(), held16=held)
    return service, args, geometry, intent


@pytest.mark.parametrize('coordinate,value', [(7, .01), (15, 17.)])
def test_supported_release_rejects_numeric_arm_motion_or_wrong_opening(scenario, coordinate, value):
    service, args, geometry, intent = supported(scenario)
    native = service.numerics.call
    def broken(method, **kw):
        result = native(method, **kw)
        if method == 'solve_path':
            result[0][coordinate] = value
        return result
    service.numerics.call = broken
    with pytest.raises(HarnessError, match='numerical_supported_release_must_hold_arm'):
        service._solve_candidate(intent, geometry, args['held16'], args['full22'], 0)


@pytest.mark.parametrize('coordinate,value', [(7, .01), (15, 17.)])
def test_supported_release_proof_rejects_collision_free_tampered_command(scenario, coordinate, value):
    service, args, geometry, intent = supported(scenario)
    candidate = service._solve_candidate(intent, geometry, args['held16'], args['full22'], 0)
    args['proposal'] = candidate
    assert revalidate_candidate(service, **args)['geometry_checks']['stages'] == 1
    broken = deepcopy(candidate)
    targets = [list(q) for q in broken['targets']]
    targets[0][coordinate] = value
    broken['targets'] = targets
    # Model a collision-free null-space displacement: geometric endpoint checks
    # alone would miss this forbidden arm change during release.
    service.numerics.poses[targets[0][7]] = geometry.base_eef.copy()
    args['proposal'] = broken
    with pytest.raises(HarnessError, match='numerical_supported_release_must_hold_arm'):
        revalidate_candidate(service, **args)
