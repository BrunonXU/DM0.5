"""Verified parameter preferences; notes never execute Python or alter geometry."""
import yaml

from .contracts import MotionParameters, HarnessError


def validated_parameters(value):
    if not isinstance(value,dict) or len(value)>5:
        raise HarnessError('invalid_parameter_experience')
    if 'move_tcp' in value:
        raise HarnessError('scene_specific_tcp_experience_not_supported')
    return {skill:MotionParameters.model_validate(params).for_skill(skill).supplied()
            for skill,params in value.items() if params}


def validates_execution(candidate,rows,requests):
    parameters=validated_parameters(candidate['motion_parameters'])
    accepted=[r['intent'] for r in rows if r.get('kind')=='skill_accepted']
    intents={i['request_id']:i for i in accepted}
    if len(intents)!=len(accepted):return False
    used={intents[r]['skill']:intents[r] for r in requests if r in intents}
    if any(skill not in used or used[skill].get('calibration_id')!=candidate['calibration_id']
           or MotionParameters.model_validate(used[skill].get('parameters',{})).supplied()!=params
           for skill,params in parameters.items()):return False
    # A useful grasp recipe must lead to completed placement and safe return,
    # not merely a collision-free planned prefix.
    scope=('owner_instance_id','session_id','task_hash','epoch','calibration_id')
    selected=[used[k] for k in parameters]
    if not selected or any(any(i.get(k)!=selected[0].get(k) for k in scope) for i in selected):return False
    if any(not any(r.get('kind')=='harness_skill_first_publication' and r.get('request_id')==i['request_id']
                   for r in rows) for i in selected):return False
    placement=next((index for index,r in reversed(list(enumerate(rows)))
                    if r.get('kind')=='skill_result' and r.get('skill')=='verify_placed'
                    and r['result'].get('outcome')=='success'),None)
    if placement is None:return False
    for row in rows[placement+1:]:
        if row.get('kind')!='skill_result' or row.get('skill')!='withdraw_reset':continue
        result=row['result'];intent=intents.get(result['request_id'])
        if (intent and all(intent.get(k)==selected[0].get(k) for k in scope)
                and result.get('outcome')=='success' and result.get('evidence_ids')
                and not result.get('assisted') and result.get('synthetic')==candidate['synthetic']):return True
    return False


def select_parameters(snapshot,task):
    selected={};sources=[]
    for name,text in (snapshot or {}).get('files',{}).items():
        parts=text.split('---',2)
        if len(parts)!=3 or parts[0].strip():continue
        try:
            meta=yaml.safe_load(parts[1])
            if not isinstance(meta,dict) or meta.get('confidence')!='verified':continue
            if (meta.get('verifier')!='f1-parameter-verifier-v1' or not meta.get('review_sha256')
                    or meta.get('calibration_id')!=task.calibration_id
                    or meta.get('parameter_schema')!='f1-task-relative-motion-v1'):continue
            values=validated_parameters(meta['motion_parameters'])
            if set(values)-set(task.allowed_skills):continue
        except (ValueError,KeyError,TypeError,yaml.YAMLError,HarnessError):continue
        # Conflicting lessons are not silently chosen by filename ordering.
        for skill,params in values.items():
            if skill in selected and selected[skill]!=params:selected[skill]=None
            else:selected.setdefault(skill,params)
        sources.append(dict(id=meta.get('id'),file=name))
    return {k:v for k,v in selected.items() if v is not None},sources


def library_preferences(snapshot,task):
    values,_=select_parameters(snapshot,task)
    return 'MOTION_PREFERENCES = '+repr(values)+'\n'


def review_run_parameters(run,root,rows):
    """Quarantine successful parameter programs; independent review is separate."""
    from pathlib import Path
    from .contracts import digest
    from .memory import ExperienceStore
    from .evidence import EvidenceStore
    results=[r for r in rows if r.get('kind')=='skill_result']
    placed=next((r for r in reversed(results) if r.get('skill')=='verify_placed'),None)
    returned=next((r for r in reversed(results) if r.get('skill')=='withdraw_reset'),None)
    if not all(r and r['result'].get('outcome')=='success' and r['result'].get('evidence_ids')
               and not r['result'].get('assisted') and not r['result'].get('synthetic')
               for r in (placed,returned)):return None
    if not any(r.get('kind')=='owner_placement_verified'
               and r.get('request_id')==placed['result']['request_id'] for r in rows):return None
    successes={r['result']['request_id'] for r in results if r['result'].get('outcome')=='success'}
    intents=[r['intent'] for r in rows if r.get('kind')=='skill_accepted'
             and r['intent']['request_id'] in successes and any(v is not None for v in r['intent'].get('parameters',{}).values())]
    if not intents:return None
    values=validated_parameters({i['skill']:i['parameters'] for i in intents if i['skill']!='move_tcp'})
    # A contiguous successful sequence including all parameterized skills is
    # required by the existing independent verifier on a different scene.
    skills=[]
    for r in results:
        if r['result'].get('outcome')=='success' and r['skill'] not in skills:skills.append(r['skill'])
        if r is placed:break
    values={k:v for k,v in values.items() if k in skills}
    if not values:return None
    key=ExperienceStore(Path(root)).propose(title='单块任务参数化动作经验',
        applies_when='Matching calibration and square task; prefer parameters, replan on rejection',
        skills=skills,discovery_run=str(Path(run).resolve()),
        discovery_scene=digest([r for r in rows if r.get('kind')=='task_prepared']),
        motion_parameters=values,calibration_id=intents[0]['calibration_id'])
    path=Path(run)/'parameter-experience-review.json'
    value=dict(candidate_id=key,motion_parameters=values,promoted=False)
    if not path.exists():EvidenceStore(Path(run)).write_json(path.name,value)
    return value
