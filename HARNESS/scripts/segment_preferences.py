"""Quarantined offline ordering hints, reusing existing planning scope identity.

A hint never removes candidates or supplies a previous collision verdict. Code,
recipe or scope mismatch falls back to the registered order, not a task failure.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path
import re

from f1_harness.contracts import digest


@lru_cache(maxsize=1)
def maintenance_identity():
    root=Path(__file__).parent
    return digest({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
        ('segment_preferences.py','segment_candidates.py','segment_preview.py','output_branch.py','sweep_blocks.py','audit_workers.py','audit_worker_launch.py','audit_assets.py','audit_asset_watch.py',
         '../src/f1_harness/command_forecast.py')})


def preference_scope(context, inputs, stages, speeds):
    planning=context.get('planning_scope')
    if not isinstance(planning,dict):return None
    end=next(i for i in inputs['trajectory']['stops'] if i>=inputs['start_index'])
    return dict(schema='f1-offline-segment-preference-scope-v1',planning=deepcopy(planning),
        control_profile_sha256=context['control_profile_sha256'],numerical_asset_id=context.get('numerical_asset_id'),
        control_source_sha256=context.get('control_source_sha256'),
        maintenance_source_sha256=maintenance_identity(),skill=inputs['skill'],
        start_index=inputs['start_index'],end_index=end,
        stages=[dict(start_index=s['start_index'],end_index=s['end_index'],semantic_stage=s.get('semantic_stage'))
            for s in stages if s['start_index']<=end and s['end_index']>inputs['start_index']],
        registered_speeds=sorted(float(v) for v in speeds),
        conditional_hold_samples=inputs['post_boundary_samples'])


def propose_preference(recommendation, discovery_run):
    """Create content-addressed JSON from a complete current conditional audit."""
    r=recommendation;scope=r.get('preference_scope');attempts=r.get('attempts')
    if (r.get('schema')!='f1-offline-segment-recommendation-v1' or not isinstance(scope,dict)
            or r.get('execution_authorized') is not False or r.get('whole_skill_verified') is not False
            or not isinstance(discovery_run,str) or not discovery_run
            or not isinstance(attempts,list) or not attempts):
        raise ValueError('preference_discovery_incomplete')
    speed=r.get('recommended_reference_speed');audit=attempts[-1].get('audit')
    if (speed not in scope['registered_speeds'] or attempts[-1]['reference_speed']!=speed
            or attempts[-1]['outcome']!='conditional_segment_clear'
            or not isinstance(audit,list) or len(audit)!=1
            or audit[0].get('complete') is not True or audit[0].get('passed') is not True
            or audit[0].get('failures')!=[] or type(audit[0].get('samples')) is not int or audit[0]['samples']<=0):
        raise ValueError('preference_requires_complete_conditional_audit')
    value=dict(schema='f1-offline-segment-preference-v1',scope=deepcopy(scope),
        preferred_reference_speed=speed,discovery_run=discovery_run,
        recommendation_sha256=digest(r),source_binding=r['binding'],
        synthetic=True,execution_enabled=False,promoted=False,real_experience_eligible=False,
        operation='prefer_registered_reference_candidate',fresh_audit_required=True,
        removes_candidates=False)
    return dict(value,id=digest(value))


def preferred_order(preferences, scope, registered):
    baseline=list(registered)
    fallback=lambda reason:(baseline,dict(applied=False,reason=reason))
    if not preferences:return fallback('no_preference')
    if scope is None:return fallback('scope_unavailable')
    if not isinstance(preferences,(list,tuple)):return fallback('invalid_preference_collection')
    matches=[]
    for value in preferences:
        try:
            payload={k:v for k,v in value.items() if k!='id'}
            if (value.get('schema')!='f1-offline-segment-preference-v1' or value['id']!=digest(payload)
                    or value.get('synthetic') is not True or value.get('execution_enabled') is not False
                    or value.get('promoted') is not False or value.get('real_experience_eligible') is not False
                    or value.get('fresh_audit_required') is not True or value.get('removes_candidates') is not False
                    or value.get('operation')!='prefer_registered_reference_candidate'
                    or value.get('scope')!=scope):continue
            if (not isinstance(value.get('discovery_run'),str) or not value['discovery_run']
                    or any(not isinstance(value.get(k),str) or not re.fullmatch('[a-f0-9]{64}',value[k])
                        for k in ('recommendation_sha256','source_binding'))):continue
            speed=value['preferred_reference_speed']
            if type(speed) not in (int,float) or speed not in baseline:continue
            matches.append(value)
        except (AttributeError,KeyError,TypeError,ValueError):continue
    if not matches:return fallback('no_valid_matching_preference')
    if len({m['preferred_reference_speed'] for m in matches})!=1:return fallback('conflicting_preferences')
    selected=matches[0];speed=selected['preferred_reference_speed']
    return [speed]+[v for v in baseline if v!=speed],dict(applied=True,preference_id=selected['id'],
        discovery_run=selected['discovery_run'],fresh_audit_required=True,removes_candidates=False)


def render_preference(value):
    return ('# 离线段候选排序建议\n\n'
        '此建议来自独立保存的条件输出审计，只调整下一次尝试顺序，所有候选仍保留。每次推荐都必须重新检查当前状态和场景，不复用旧碰撞判定。\n\n'
        '它没有进入机器人执行配置、没有晋升真实经验；候选不适用时继续其余登记方案。代码、配方或阶段身份不匹配则忽略建议。\n\n'
        f'- 技能：`{value["scope"]["skill"]}`，段起点：`{value["scope"]["start_index"]}`。\n'
        f'- 优先参考速度：`{value["preferred_reference_speed"]}`，不是控制输出限速。\n'
        f'- 发现运行：`{value["discovery_run"]}`。\n'
        f'- 建议身份：`{value["id"]}`。\n'
        '- JSON是机器建议；本MD不进入编译器，也不能授权运动。\n')
