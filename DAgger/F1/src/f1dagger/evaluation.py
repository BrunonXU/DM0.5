"""Prospective, immutable evaluation identity; never grants movement authority."""
import hashlib
import json


def validate_evaluation(value, base, actor):
    if value is None:
        return None
    if (value.get('schema') != 'f1_local_evaluation_v1'
            or value.get('strategy') not in ('b1250', 'bc400', 'rlt')
            or any(not isinstance(value.get(k), str) or not value[k].strip()
                   for k in ('batch_id', 'attempt_id', 'start_group', 'base_version', 'actor_version'))
            or value.get('budget_seconds') != 30
            or value['base_version'] != base.model_version
            or value['actor_version'] != actor.rlt_identity['actor_version']):
        raise ValueError('Evaluation identity must pre-register the exact base/actor and local protocol')
    # Even B1250 comparisons retain a bound actor transport, but never select it.
    if actor.config.get('allow_boundary_updates'):
        raise ValueError('Evaluation requires boundary updates disabled before connecting')
    if value['strategy'] == 'bc400' and (actor.rlt_identity['checkpoint_step'] != 400
                                       or actor.rlt_identity['q_guidance_updates'] != 0):
        raise ValueError('BC400 evaluation must use the original BC400 actor')
    result = dict(value)
    result['protocol_sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return result
