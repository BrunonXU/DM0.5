"""Inspectable geometric baseline; pending evidence is a result, not a Python crash."""

APPROACH_ONLY = '''
result = robo.skill("approach_grasp")
'''

from .recovery_programs import compile_library

SINGLE_BLOCK = compile_library(2) + '''
def run_baseline():
    result = motion("approach_grasp")
    if result["outcome"] != "success": return result
    grasp = motion("grasp")
    if grasp["outcome"] == "success":
        grasp = skill("verify_grasp")
    if grasp["outcome"] == "failure":
        if grasp.get("reason") == "motion_stalled": return grasp
        result = skill("reobserve")
        if result["outcome"] != "success": return result
        result = skill("regrasp")
        if result["outcome"] != "success": return result
        grasp = skill("verify_grasp")
    if grasp["outcome"] != "success": return grasp
    for name in ("transfer_above_hole", "inspect_in_hand"):
        result = motion(name)
        if result["outcome"] != "success": return result
    inserted = skill("align")
    if inserted["outcome"] == "success":
        inserted = skill("insert")
    for attempt in range(2):
        if inserted["outcome"] == "success": break
        if inserted["outcome"] != "failure": return inserted
        for name in INSERTION_RECOVERY:
            result = motion(name)
            if result["outcome"] != "success": return result
        inserted = skill("insert")
    if inserted["outcome"] != "success": return inserted
    result = skill("release")
    if result["outcome"] != "success": return result
    return skill("verify_placed")
result = run_baseline()
'''


def baseline_experience(snapshot):
    """Compile only recognized, verified skill sequences; note text is never code."""
    import yaml
    from .contracts import HarnessError
    from .recovery_programs import validate_program
    selected=[]
    for name,text in (snapshot or {}).get('files',{}).items():
        parts=text.split('---',2)
        if len(parts)!=3 or parts[0].strip():continue
        try:metadata=yaml.safe_load(parts[1])
        except yaml.YAMLError:continue
        if not isinstance(metadata,dict) or metadata.get('confidence')!='verified':continue
        if 'program' in metadata:
            try:
                key=validate_program(metadata['program'])
            except HarnessError:
                continue
            if (metadata.get('program_sha256')!=key or metadata.get('verifier')!='f1-program-verifier-v3'
                    or not metadata.get('review_sha256')):
                continue
            selected.append(dict(id=metadata.get('id'),strategy=metadata['program']['id'],
                                 program_sha256=key,file=name))
            continue
        if metadata.get('skills')==['retreat','reobserve','align','insert']:
            selected.append(dict(id=metadata.get('id'),strategy='insertion_reobserve',file=name))
    return selected


def single_block_program(task, *, experience=None):
    if task.goal == 'approach_grasp': return APPROACH_ONLY
    code=SINGLE_BLOCK.replace('range(2)', 'range(' + str(task.local_corrections) + ')')
    from .parameter_experience import library_preferences
    code=code.replace('MOTION_PREFERENCES = {}\n',library_preferences(experience,task))
    if 'withdraw_reset' in task.allowed_skills:
        code=code.replace('    return skill("verify_placed")',
            '    placed = skill("verify_placed")\n'
            '    if placed["outcome"] != "success": return placed\n'
            '    return motion("withdraw_reset")')
    selected=baseline_experience(experience)
    if selected:
        # Current registered strategies match the built-in recovery program.
        # Track its provenance without pretending this changes the default action.
        code='EXPERIENCE_STRATEGY_IDS = '+repr([r['id'] for r in selected])+'\n'+code
    return code
