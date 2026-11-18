"""Small reviewed configuration surface; no arbitrary providers or executable imports."""
from pathlib import Path
import tomllib


def load_config(path:Path):
    value=tomllib.loads(path.read_text())
    planner=value['planner']
    expected=dict(provider='openai',runtime='responses',wire_api='responses',
                  base_url='https://api.openai.com/v1',model='gpt-6.2-astra',reasoning_effort='high')
    if value.get('schema_version')!=1 or any(planner.get(k)!=v for k,v in expected.items()):
        raise ValueError('planner_configuration_differs_from_frozen_contract')
    if value['execution'].get('allow_robot_actions') or value['execution'].get('allow_training'):
        raise ValueError('planner_config_cannot_grant_robot_or_training_authority')
    if value['execution'].get('mode') not in ('offline','hardware'):
        raise ValueError('unknown_execution_transport')
    if value['memory'].get('automatic_promotion'):
        raise ValueError('independent_experience_validation_required')
    return value
