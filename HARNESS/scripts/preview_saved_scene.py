"""Native geometry/preview on a saved scene; never creates an owner or publisher."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from f1_harness.contracts import Observation, Task, SkillIntent, MotionParameters, digest
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from f1_harness.owner_bridge import numerical_failure_detail


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--site', type=Path, required=True)
    p.add_argument('--scene', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--request', type=Path)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    root = Path(__file__).resolve().parents[1]
    source = json.loads(a.scene.read_text())[0]
    obs = Observation.model_validate(source['observation'])
    task = Task.model_validate_json((a.site / 'task.local.json').read_text())
    fixture = json.loads((a.site / 'plan.local.json').read_text())['fixture']
    spec = json.loads((a.site / 'numerics.local.json').read_text())['spec']
    spec['urdf'] = str(root.parent / 'F1-4242/web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf')
    spec['gate']['config']['_infra_root'] = str(root.parent / 'F1-4242')
    spec['gate']['runtime'] = str(a.output.parent / 'native-model')
    spec['gate']['full22'] = source['full22']
    numeric = NumericalDispatcher(spec)
    service = PlanService(task, fixture, a.output.parent / 'unused-preview.sock',
                          clock=lambda: obs.received_ns)
    service.numerics = SimpleNamespace(call=lambda method, **kw: numeric.dispatch(method, kw))
    kw = dict(observation=obs.model_dump(), full22=source['full22'], held16=list(obs.state16))
    if a.request is None:
        result = service.dispatch('geometry_context', kw)
    else:
        request = json.loads(a.request.read_text())
        skill = request['name']
        if skill not in ('approach_grasp', 'move_tcp'):
            raise ValueError('unsupported_offline_preview_skill')
        params = MotionParameters.model_validate(request.get('parameters') or {}).for_skill(skill)
        intent = SkillIntent(request_id=a.request.stem, session_id='offline-preview',
            owner_instance_id='no-owner', task_id=task.task_id, task_hash=digest(task), epoch=1,
            observation_id=obs.observation_id, scene_version=obs.scene_version,
            calibration_id=task.calibration_id, skill=skill, parameters=params, budget_ms=300000)
        try:
            proposal = service.dispatch('plan_skill', dict(kw, intent=intent.model_dump()))
            result = dict(planned=True, proposal=proposal)
        except Exception as exc:
            result = dict(planned=False, planning_diagnostic=numerical_failure_detail(exc))
    with a.output.open('x') as f:
        json.dump(result, f, indent=2, allow_nan=False)
    print(json.dumps(dict(output=str(a.output), planned=result.get('planned'))))


if __name__ == '__main__':
    main()
