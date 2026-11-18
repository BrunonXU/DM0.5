"""Verify registered recovery operations against owner request lineage, not prose."""
from .recovery_programs import validate_program

SCOPE = ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'epoch', 'calibration_id')


def successful(row, synthetic):
    result = row.get('result', {})
    return (result.get('outcome') == 'success' and result.get('synthetic') is synthetic
            and result.get('assisted') is False and bool(result.get('evidence_ids')))


def same_scope(a, b):
    return all(a.get(k) is not None and a.get(k) != '' and a.get(k) == b.get(k) for k in SCOPE)


def verify_program(candidate, rows, placement, *, synthetic):
    """Return the actual successful operation and its parent, or no match.

    Events are inspected in journal order. A final unrelated successful placement
    cannot validate a retry that never happened, a repeated normal skill call,
    a continuation from another task, or a replay after revocation.
    """
    program = candidate['program']
    validate_program(program)
    accepted = {}
    results = {}
    for i, row in enumerate(rows):
        if row.get('kind') == 'skill_accepted':
            intent = row.get('intent', {})
            request = intent.get('request_id')
            if not request or request in accepted:
                return None
            accepted[request] = (i, intent)
        elif row.get('kind') == 'skill_result':
            request = row.get('result', {}).get('request_id')
            if not request or request in results:
                return None
            results[request] = (i, row)
    placement_id = placement['result']['request_id']
    if placement_id not in accepted:
        return None
    placement_scope = accepted[placement_id][1]
    tasks = [r for r in rows if r.get('kind') == 'task_prepared'
             and r.get('task_hash') == placement_scope.get('task_hash')]
    if len(tasks) != 1:
        return None
    completion = placement
    if 'withdraw_reset' in tasks[0].get('task', {}).get('allowed_skills', []):
        resets = [r for r in rows[rows.index(placement) + 1:] if r.get('kind') == 'skill_result'
                  and r.get('skill') == 'withdraw_reset' and successful(r, synthetic)]
        if not resets:
            return None
        completion = resets[-1]
        reset_id = completion['result']['request_id']
        if (reset_id not in accepted or not same_scope(accepted[reset_id][1], placement_scope)
                or not any(r.get('kind') == 'harness_measured_waypoint' and r.get('request_id') == reset_id
                           for r in rows[:rows.index(completion)])):
            return None
    rows = rows[:rows.index(completion) + 1]
    # Accepted/results maps were built from the full original trace. Do not use
    # later operations to validate an already completed task.
    results = {q: item for q, item in results.items() if item[0] < len(rows)}

    def uninterrupted(start, end):
        return not any(r.get('kind') in ('authority_revoked', 'web_authorization', 'task_prepared')
                       for r in rows[start + 1:end])

    def scoped(request):
        return request in accepted and same_scope(accepted[request][1], placement_scope)

    actions = [(i, r) for i, r in enumerate(rows) if r.get('kind') == 'skill_result']
    if program['operation'] == 'registered_skill_sequence':
        for n in range(len(actions) - len(program['skills']) + 1):
            sequence = actions[n:n + len(program['skills'])]
            if [r.get('skill') for _, r in sequence] != program['skills']:
                continue
            ids = [r['result']['request_id'] for _, r in sequence]
            if not all(scoped(q) and successful(r, synthetic) and accepted[q][0] < i
                       for q, (i, r) in zip(ids, sequence)):
                continue
            # The failed insertion must directly precede this recovery sequence.
            if n == 0:
                continue
            trigger_index, trigger = actions[n - 1]
            parent = trigger['result']['request_id']
            if (trigger.get('skill') == 'insert' and trigger['result'].get('outcome') == 'failure'
                    and scoped(parent) and accepted[ids[0]][0] > trigger_index
                    and uninterrupted(accepted[parent][0], len(rows))):
                return dict(program_id=program['id'], parent_request_id=parent, request_ids=ids)
        return None

    for request, (result_index, row) in results.items():
        if row.get('skill') != candidate['skills'][0] or not successful(row, synthetic) or not scoped(request):
            continue
        new_index, intent = accepted[request]
        parent = intent.get(program['request_field'])
        if not parent or parent == request or not scoped(parent) or parent not in results:
            continue
        parent_index, parent_intent = accepted[parent]
        failed_index, failed = results[parent]
        if (not parent_index < failed_index < new_index < result_index
                or parent_intent.get('skill') != intent.get('skill')
                or not uninterrupted(parent_index, len(rows))):
            continue
        # No different action is allowed to invalidate the saved checkpoint.
        if any(r.get('kind') == 'skill_accepted' for r in rows[failed_index + 1:new_index]):
            continue
        result = failed['result']
        if result.get('synthetic') is not synthetic or result.get('assisted') is not False:
            continue
        if program['operation'] == 'recheck_same_skill':
            if (result.get('outcome') != 'pending_review' or result.get('verification_request_id') != parent
                    or parent_intent.get('verification_request_id') or intent.get('resume_request_id')):
                continue
            escalation = [r for r in rows[parent_index:failed_index] if
                r.get('kind') == 'harness_verification_escalated' and r.get('request_id') == parent
                and r.get('motion_replayed') is False and isinstance(r.get('after_ns'), int)]
            continuation = [r for r in rows[new_index:result_index] if
                r.get('kind') == 'harness_verification_continued' and r.get('request_id') == request
                and r.get('motion_request_id') == parent and r.get('motion_replayed') is False
                and r.get('skill') == intent['skill'] and r.get('epoch') == intent['epoch']
                and r.get('task_hash') == intent['task_hash']]
            measured = [r for r in rows[parent_index:failed_index]
                        if r.get('kind') == 'harness_measured_waypoint' and r.get('request_id') == parent
                        and isinstance(r.get('measured_stamp_ns'), int)]
            if not (measured and any(a['after_ns'] == b.get('after_ns')
                    and 0 <= measured[-1]['measured_stamp_ns'] <= a['after_ns']
                    for a in escalation for b in continuation)):
                continue
            if any(r.get('request_id') == request and r.get('kind') in
                   ('harness_skill_first_publication', 'harness_measured_waypoint') for r in rows):
                continue
        else:
            if (result.get('outcome') != 'failure' or result.get('reason') != 'motion_stalled'
                    or result.get('resume_request_id') != parent or intent.get('verification_request_id')):
                continue
            checkpoints = [(i, r) for i, r in enumerate(rows[failed_index + 1:new_index], failed_index + 1)
                           if r.get('kind') == 'harness_motion_checkpoint' and r.get('request_id') == parent
                           and r.get('resumable') is True and r.get('skill') == intent['skill']
                           and r.get('task_hash') == intent['task_hash'] and r.get('epoch') == intent['epoch']
                           and (intent['skill'] != 'grasp' or r.get('preclosure') is True)]
            if not any(any(r.get('kind') == 'hold_acknowledged' and r.get('stage') == 'measured_stationary'
                           for r in rows[i + 1:new_index]) for i, _ in checkpoints):
                continue
            # A normal success or an accepted-but-unpublished candidate is not a
            # successful physical continuation. Both attempts need owner output.
            if not all(any(r.get('kind') == 'harness_skill_first_publication' and r.get('request_id') == q
                           for r in rows[a:b]) for q, a, b in
                       ((parent, parent_index, failed_index), (request, new_index, result_index))):
                continue
        return dict(program_id=program['id'], parent_request_id=parent, request_ids=[request])
    return None
