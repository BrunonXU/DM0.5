"""Current decision evidence from trusted ledger entries, never model claims.

No new observation, motion, authorization or dispatch is performed here. Historical
failures are resolved only by a successful receipt for that operation/skill.
"""


def operation(call):
    tool = call.get('tool')
    name = (call.get('kwargs') or {}).get('name')
    return ('preview' if tool in ('inspect_plan', 'preview_skill') else tool,
            name or ('approach_grasp' if tool == 'inspect_plan' else None))


class PlannerDecision:
    def __init__(self):
        self.completed = []
        self.blockers = {}
        self.resolved = []
        self.execution_requested = False
        self.last_skill = None
        self.latest = None

    def update(self, result):
        for call in result.get('primitive_calls', []):
            key = operation(call)
            failed = (call.get('ok') is False or call.get('planned') is False or
                      call.get('outcome') in ('failure','pending_review','interrupted'))
            fact = {k: call[k] for k in ('tool','request_id','observation_id','outcome',
                'reason','error','no_motion','planned','current_scope','cancel_stage',
                'resume_request_id','verification_request_id','observation_retry_reason','continuation_submitted','missing_entities',
                'planning_diagnostic','scene_entities') if k in call}
            fact['skill'] = key[1]
            if not fact.get('reason') and call.get('planned') is False:
                diagnostic = call.get('planning_diagnostic') or {}
                missing = call.get('missing_entities') or (diagnostic.get('metrics') or {}).get('missing_entities') or []
                entities = ', '.join(str(row.get('object_id')) for row in missing if isinstance(row,dict))
                fact['reason'] = str(diagnostic.get('code') or 'preview_not_planned') + (': '+entities if entities else '')
            self.latest = fact
            if call.get('tool') == 'execute_skill':
                self.execution_requested = True
                self.last_skill = key[1]
            if failed:
                self.blockers.pop(key, None)
                self.blockers[key] = fact
            elif call.get('ok') is True:
                success = (call.get('outcome') == 'success' or
                    call.get('planned') is True and call.get('current_scope') is True)
                if success:
                    resolved_keys = [key]
                    if call.get('tool') == 'execute_skill':
                        resolved_keys.append(('preview', key[1]))
                        if key[1] not in self.completed:
                            self.completed.append(key[1])
                    for old_key in resolved_keys:
                        old = self.blockers.pop(old_key, None)
                        if old:
                            self.resolved.append(dict(old, resolved_by=fact))
                            self.resolved = self.resolved[-3:]
        blocked = list(self.blockers.values())
        if result.get('exception') or result.get('error'):
            decision = 'inspect_program_error_and_original_receipts'
        elif result.get('images'):
            decision = 'interpret_requested_visual_evidence'
        elif any(b.get('resume_request_id') and b.get('continuation_submitted') is False
                 and b.get('observation_retry_reason')==
                     'observation_viewpoint_required:local_environment_only_supports_world_observation_motion'
                 for b in blocked):
            decision = 'relocalize_target_from_a_new_view_then_resume_original_checkpoint'
        elif blocked:
            decision = 'resolve_current_blocker_from_original_receipts'
        elif self.latest and self.latest.get('planned') is True and self.latest.get('current_scope') is True:
            decision = ('continue_explicit_program_from_current_stage' if self.execution_requested
                        else 'execute_guarded_program_no_preview_confirmation_needed')
        elif self.last_skill:
            decision = 'continue_from_completed_steps_without_replay'
        else:
            decision = 'obtain_missing_task_evidence'
        return dict(schema='f1-decision-context-v1', source='trusted_primitive_calls',
            completed_skills=list(self.completed), last_requested_skill=self.last_skill,
            execution_requested=self.execution_requested, latest_receipt=self.latest,
            unresolved_blockers=blocked, resolved_failures=list(self.resolved),
            next_decision=decision, motion_authorized=False,
            cached_state_is_not_current_evidence=True)
