"""Each failed run produces evidence-bound program candidates, never auto-promotion."""
import json
from pathlib import Path
from .contracts import digest
from .evidence import EvidenceStore
from .recovery_programs import registered_program,validate_program


TITLES={
    'verify_completed_motion':'动作完成但视觉证据不足：补验证，不能重放运动',
    'resume_known_progress':'已知运动停滞：保持确认后从实测状态继续',
    'insert_reobserve_realign':'插入失败：撤离、重看、对齐后再插入',
}


def classify(incident):
    if incident.get('kind') in ('skill_admission_rejected','skill_not_admitted'):
        return None  # A refused request is not a failed physical insertion.
    reason=incident.get('reason') or ''
    if (reason=='fresh_post_motion_scene_missing' or reason.startswith('post_motion_verification_unavailable:')
            or incident.get('kind')=='harness_verification_escalated'):
        return 'verify_completed_motion'
    if reason in ('motion_stalled','harness_motion_checkpoint'):
        return 'resume_known_progress'
    if incident.get('skill')=='insert' and incident.get('outcome')=='failure':
        return 'insert_reobserve_realign'
    return None


def review_failures(failure_record,root):
    if failure_record is None:return None
    record=json.loads(Path(failure_record['path']).read_text())
    if digest(record)!=failure_record['id'] or record.get('schema')!='f1-failure-facts-v1':
        raise ValueError('failure_record_identity_changed')
    store=EvidenceStore(Path(root)/'learning')
    grouped={};engineering=[]
    for incident in record['incidents']:
        name=classify(incident)
        if name:grouped.setdefault(name,[]).append(incident)
        else:
            issue=dict(skill=incident.get('skill'),reason=incident.get('reason'),
                request_id=incident.get('request_id'),count=incident.get('count',1))
            if incident.get('kind') in ('skill_admission_rejected','skill_not_admitted'):
                issue.update(category='admission_orchestration',operation=incident.get('operation'),
                    state=incident.get('state'),preceding_result=incident.get('preceding_result'),
                    causal_status=incident.get('causal_status'),
                    request_had_publication=incident.get('request_had_publication'),
                    new_motion_inferred=False,review_focus='phase_recovery_and_admission_state',
                    execution_changed=False)
            engineering.append(issue)
    candidates=[]
    for name,facts in sorted(grouped.items()):
        program=registered_program(name)
        value=dict(schema='f1-program-lesson-candidate-v1',program=program,
            program_sha256=validate_program(program),title=TITLES[name],
            source_sha256=record['source_sha256'],source_run=record['run'],failure_record=failure_record['id'],
            incidents=facts,status='candidate_not_validated',execution_enabled=False,
            cause_status='observed_control_flow_not_physical_cause',
            validation_required=['registered-program counterexamples and cancellation',
                'independent scenario with actual trigger and successful recovery sequence',
                'bound owner evidence and complete task outcome before promotion'])
        key=digest(value);filename=key+'.candidate.json'
        if not (store.root/filename).exists():store.write_json(filename,value)
        # Human prose is a view of the immutable candidate, never compiler input.
        md=store.root/(key+'.md')
        if not md.exists():md.write_text('# '+TITLES[name]+'\n\n'
            '这是本次失败触发的策略候选，尚未独立验证，也未进入执行快照。运行结果只说明发生了这些事件，不证明策略能解决现场原因。\n\n'
            '策略结构保存在同名 JSON；Markdown 用于审阅。执行只使用登记程序及 owner 的当前 checkpoint，不从文字生成关节命令。\n\n'
            f'- 程序：`{name}`\n- 发现运行：`{record["run"]}`\n'
            f'- 原件摘要：`{record["source_sha256"]}`\n- 独立验证：待完成\n')
        candidates.append(dict(id=key,program=name,path=str(store.root/filename)))
    # Unknown reasons become explicit engineering work, never a guessed recovery.
    review=dict(schema='f1-learning-review-v1',source_sha256=record['source_sha256'],
        failure_record=failure_record['id'],candidates=candidates,engineering_issues=engineering,
        promoted=False,execution_changed=False)
    key=digest(review)
    if not (store.root/(key+'.review.json')).exists():store.write_json(key+'.review.json',review)
    if any(i.get('category')=='admission_orchestration' for i in engineering):
        md=store.root/(key+'.review.md')
        if not md.exists():
            md.write_text('# 请求准入与编排异常审阅\n\n'
                '记录的是被拒绝的请求，不代表机器人执行失败或已经发生碰撞。应核对阶段与恢复状态，不能让模型通过经验文字忽略拒绝。\n\n'
                '同名JSON保留原件摘要、前一技能结果及拒绝时状态；相邻事件只说明顺序，不单独证明原因。本审阅不改变执行快照或控制代码。\n\n'
                f'- 工程问题：{sum(i.get("category")=="admission_orchestration" for i in engineering)}项\n'
                f'- 原件摘要：`{record["source_sha256"]}`\n'
                '- 后续：复现状态转换，修复并独立验证；不自动生成撤离、重复插入或放宽门槛的程序。\n')
    return dict(id=key,path=str(store.root/(key+'.review.json')),candidate_count=len(candidates),
                engineering_issue_count=len(engineering),promoted=False)


def learning_snapshot(root):
    """Deduplicate recurrence by original owner trace, never by repeated review calls."""
    store=EvidenceStore(Path(root)/'learning');groups={}
    for path in sorted(store.root.glob('*.candidate.json')):
        value=json.loads(path.read_text())
        if (path.name!=digest(value)+'.candidate.json'
                or value.get('schema')!='f1-program-lesson-candidate-v1'
                or value.get('program_sha256')!=validate_program(value.get('program'))
                or value.get('execution_enabled') is not False):
            raise ValueError('learning_candidate_identity_changed')
        name=value['program']['id'];group=groups.setdefault(name,dict(program=name,sources=set(),candidates=[]))
        group['sources'].add(value['source_sha256']);group['candidates'].append(path.stem.removesuffix('.candidate'))
    rows=[dict(program=name,independent_source_count=len(group['sources']),
               sources=sorted(group['sources']),candidate_ids=group['candidates']) for name,group in sorted(groups.items())]
    value=dict(schema='f1-learning-diagnostic-snapshot-v1',strategies=rows,executable=False,
        note='Source count is recurrence, not successful validation or performance improvement.')
    key=digest(value)
    if not (store.root/(key+'.snapshot.json')).exists():store.write_json(key+'.snapshot.json',value)
    return dict(id=key,path=str(store.root/(key+'.snapshot.json')),strategy_count=len(rows),executable=False)


def register_candidates(review_record, root):
    """Move evidence-bound lessons to verifier input, never to the live corpus."""
    from .memory import ExperienceStore
    from .contracts import HarnessError, SKILLS
    if review_record is None:
        return None
    review = json.loads(Path(review_record['path']).read_text())
    if digest(review) != review_record['id']:
        raise ValueError('learning_review_identity_changed')
    memory = ExperienceStore(Path(root))
    registered = []
    unresolved = []
    for row in review['candidates']:
        path = Path(row['path'])
        lesson = json.loads(path.read_text())
        if digest(lesson) != row['id'] or path.name != row['id'] + '.candidate.json':
            raise ValueError('learning_candidate_identity_changed')
        for skill in sorted({r.get('skill') for r in lesson['incidents']} - {None}):
            if skill not in SKILLS:
                unresolved.append(dict(lesson_id=row['id'], skill=skill, reason='unknown_skill'))
                continue
            try:
                key = memory.propose_program(path, skill=skill)
            except HarnessError as exc:
                unresolved.append(dict(lesson_id=row['id'], skill=skill, reason=str(exc)))
            else:
                registered.append(dict(lesson_id=row['id'], skill=skill, candidate_id=key))
    value = dict(schema='f1-program-review-registration-v1', learning_review=review_record['id'],
                 registered=registered, unresolved=unresolved, promoted=False)
    store = EvidenceStore(Path(root) / 'learning')
    key = digest(value)
    path = store.root / (key + '.registration.json')
    if not path.exists():
        store.write_json(path.name, value)
    return dict(id=key, path=str(path), candidate_count=len(registered),
                unresolved_count=len(unresolved), promoted=False)
