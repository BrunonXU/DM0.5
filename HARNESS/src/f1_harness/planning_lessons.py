"""Evidence-bound candidate ordering proposals; never automatic control edits."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from .contracts import digest
from .evidence import EvidenceStore
from .skills import candidate_variants


@lru_cache(maxsize=1)
def runtime_source_identity():
    # Once per immutable planner process, outside the control loop. This also
    # binds solver/geometry adapters rather than only the ranking implementation.
    package=Path(__file__).parent
    return digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.glob('*.py'))})


def planning_scope(task,fixture,skill):
    return dict(task_hash=digest(task),calibration_id=task.calibration_id,skill=skill,
        fixture_sha256=digest(fixture),runtime_source_sha256=runtime_source_identity())


def validate_evaluation(evaluation,candidate,start):
    """Bind diagnostics to the original request; planning is not action success."""
    if evaluation.get('schema')!='f1-candidate-evaluation-v1' or evaluation.get('motion_success_inferred') is not False:
        raise ValueError('candidate_evaluation_schema')
    intent=start['intent'];scope=evaluation['scope'];skill=intent['skill']
    if (evaluation['request_id']!=intent['request_id'] or candidate['request_id']!=intent['request_id']
            or evaluation['epoch']!=intent['epoch'] or candidate['epoch']!=intent['epoch']
            or evaluation['observation_id']!=start['observation']['observation_id']
            or scope['task_hash']!=intent['task_hash'] or scope['calibration_id']!=intent['calibration_id']
            or scope['skill']!=skill or candidate['calibration_id']!=intent['calibration_id']):
        raise ValueError('candidate_evaluation_scope_mismatch')
    if any(not isinstance(scope[k],str) or not re.fullmatch('[a-f0-9]{64}',scope[k])
           for k in ('fixture_sha256','runtime_source_sha256','task_hash')):
        raise ValueError('candidate_evaluation_identity_missing')
    registered=list(candidate_variants(skill));attempts=evaluation['attempts']
    if (evaluation['registered_variants']!=registered or not isinstance(attempts,list)
            or not 1<=len(attempts)<=len(registered)):
        raise ValueError('candidate_evaluation_attempts_invalid')
    seen=set()
    for index,attempt in enumerate(attempts):
        variant=attempt['variant'];elapsed=attempt['elapsed_ns']
        if (type(variant) is not int or variant not in registered or variant in seen
                or type(elapsed) is not int or elapsed<0
                or attempt['outcome']!=('planned' if index==len(attempts)-1 else 'rejected')):
            raise ValueError('candidate_evaluation_attempts_invalid')
        if attempt['outcome']=='rejected' and not isinstance(attempt.get('diagnostic',{}).get('code'),str):
            raise ValueError('candidate_evaluation_rejection_missing')
        seen.add(variant)
    if (evaluation['selected_variant']!=attempts[-1]['variant']
            or candidate['candidate_variant']!=attempts[-1]['variant']):
        raise ValueError('candidate_evaluation_selection_mismatch')
    return registered


def review_candidate_orders(run,root):
    """Generate quarantined JSON+MD proposals, including in otherwise successful runs.

    Rejected candidates disappear from a simple final-outcome-only review. Their
    cost can inform a new ordering, but not a wider geometric/limit exception.
    """
    run=Path(run).resolve();source=run/'collected/owner/harness/trace.jsonl'
    if not source.is_file():return None
    raw=source.read_bytes();source_hash=hashlib.sha256(raw).hexdigest()
    rows=[json.loads(line) for line in raw.splitlines() if line.strip()]
    starts={r['request_id']:r for r in rows if r.get('kind')=='harness_planning_started'}
    synthetic=any(r.get('simulation') is True or r.get('result',{}).get('synthetic') is True for r in rows)
    store=EvidenceStore(Path(root)/'planning-candidates');candidates=[];invalid=[];seen=set()
    for row in rows:
        if row.get('kind')!='harness_plan_waiting_fresh_revalidation':continue
        candidate=row.get('candidate',{});trace=candidate.get('geometry_trace') or []
        evaluation=trace[0].get('candidate_evaluation') if trace else None
        if evaluation is None:continue  # Historical logs have no rejected-attempt evidence.
        try:
            start=starts[row['request_id']]
            registered=validate_evaluation(evaluation,candidate,start)
            if row['request_id']!=evaluation['request_id']:raise ValueError('candidate_event_request_mismatch')
        except (KeyError,ValueError,TypeError,IndexError) as exc:
            invalid.append(dict(event_id=row.get('event_id'),reason=str(exc)[:200]));continue
        identity=digest(evaluation)
        if identity in seen:continue
        seen.add(identity)
        attempts=evaluation['attempts'];rejected=attempts[:-1]
        if not rejected or len(registered)<2:continue
        selected=evaluation['selected_variant']
        order=[selected]+[v for v in registered if v!=selected]
        value=dict(schema='f1-planning-order-candidate-v1',scope=evaluation['scope'],
            operation='prefer_registered_candidates',candidate_order=order,
            source_run=str(run),source_sha256=source_hash,source_event_id=row.get('event_id'),
            evaluation=evaluation,synthetic=synthetic,execution_enabled=False,
            status='candidate_not_validated',motion_success_inferred=False,
            avoided_attempt_cost_observed_ns=sum(a['elapsed_ns'] for a in rejected),
            cost_is_counterfactual_not_measured_speedup=True,
            retained_variants=registered,limits_modified=False,
            validation_required=['independent task/scene evidence under identical configuration and code scope',
                'actual application of the preferred order with all original geometric/native checks',
                'complete task outcome, cancellation and fallback evidence',
                'matched comparison of planning cost; no regression in success',
                'explicit reviewed configuration publication; no arbitrary program execution'])
        key=digest(value);path=store.root/(key+'.candidate.json')
        if not path.exists():store.write_json(path.name,value)
        elif json.loads(path.read_text())!=value:raise ValueError('planning_candidate_identity_changed')
        md=store.root/(key+'.md')
        if not md.exists():
            md.write_text('# 规划候选顺序建议\n\n'
                '本次规划先拒绝部分候选，再得到可行计划。建议先尝试这次可行的候选，其余候选保留；这不是运动成功或现场提速证明。\n\n'
                '同名JSON保存请求、原件、配置与代码身份。该建议未独立验证，不进入执行快照，不修改限位、碰撞、控制代码或训练权重。\n\n'
                f'- 技能：`{value["scope"]["skill"]}`\n- 建议顺序：`{order}`\n'
                f'- 发现运行：`{run}`\n- 原件摘要：`{source_hash}`\n'
                '- 下一步：在独立场景核对实际采用、完整任务结果和规划成本，再发布已审核配置。\n')
        candidates.append(dict(id=key,path=str(path),skill=value['scope']['skill']))
    review=dict(schema='f1-planning-order-review-v1',source_run=str(run),source_sha256=source_hash,
        candidates=candidates,invalid_evaluations=invalid,execution_changed=False,promoted=False)
    key=digest(review);path=store.root/(key+'.review.json')
    if not path.exists():store.write_json(path.name,review)
    return dict(id=key,path=str(path),candidate_count=len(candidates),invalid_count=len(invalid),execution_changed=False)
