"""Bound model deliberation from structured tool receipts, not generated prose.

This stops further model calls; it never declares a grasp, authorizes a motion,
or changes an owner's cancellation/arrival contracts. Budgets are per task.
"""
import json
import hashlib
from .planner_decision import PlannerDecision


class PlannerProgress:
    def __init__(self, max_idle_turns=3, max_candidate_turns=2):
        self.max_idle_turns = max_idle_turns
        self.max_candidate_turns = max_candidate_turns
        self.idle_turns = 0
        self.completed = set()
        self.completed_requests = set()
        self.evidence = set()
        self.candidates = set()
        self.candidate_turns = 0
        self.visual_refresh_used = False
        self.last_failure = None
        self.failed_strategies = {}
        self.decision = PlannerDecision()

    def update(self, result):
        progress = []
        calls = result.get('primitive_calls', [])
        for call in calls:
            tool = call.get('tool')
            args = call.get('kwargs') or {}
            if call.get('ok') is False or call.get('planned') is False or call.get('outcome') in ('failure', 'pending_review', 'interrupted'):
                self.last_failure = {key: call[key] for key in
                    ('tool', 'outcome', 'reason', 'error', 'cancel_stage',
                     'verification_request_id', 'resume_request_id', 'planning_diagnostic', 'missing_entities') if key in call}
                self.last_failure['skill'] = args.get('name')
                if not self.last_failure.get('reason') and call.get('planned') is False:
                    diagnostic=call.get('planning_diagnostic') or {}
                    missing=call.get('missing_entities') or (diagnostic.get('metrics') or {}).get('missing_entities') or []
                    entities=', '.join(str(row.get('object_id')) for row in missing if isinstance(row,dict))
                    self.last_failure['reason']=str(diagnostic.get('code') or 'preview_not_planned')+(
                        ': '+entities if entities else '')
                if tool in ('execute_skill','preview_skill','inspect_plan'):
                    # Retain bounded semantic history, not more image/geometry
                    # dumps. This is advice: changed geometry can make an old
                    # strategy feasible and the native planner still decides.
                    strategy=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]
                    diagnostic=call.get('planning_diagnostic') or {}
                    reason=str(diagnostic.get('code') or call.get('reason') or call.get('error') or 'tool_failure')[:160]
                    key=(strategy,reason)
                    old=self.failed_strategies.pop(key,{})
                    parameters=args.get('parameters') or {}
                    self.failed_strategies[key]=dict(strategy_id=strategy,skill=args.get('name'),
                        parameters={k:parameters[k] for k in ('pitch_offset_deg','variant','approach_distance_m',
                            'grasp_tcp_pose','approach_direction_object','target_tcp_pose','target_frame','via_tcp_poses')
                            if k in parameters},
                        reason=reason,attempts=old.get('attempts',0)+1,
                        no_motion=call.get('no_motion'),
                        recovery_hint=(diagnostic.get('metrics') or {}).get('recovery_hint'))
                    while len(self.failed_strategies)>3:
                        self.failed_strategies.pop(next(iter(self.failed_strategies)))
            if call.get('ok') is not True:
                continue
            # A new request ID, a successful RPC, or moved=True is not progress.
            # In particular locate_target is stateful but never moves the robot.
            if tool == 'execute_skill' and call.get('outcome') == 'success':
                name = args.get('name')
                request = call.get('request_id')
                new_motion = (isinstance(request, str) and bool(request)
                    and request not in self.completed_requests
                    and call.get('lifecycle') == 'completed' and call.get('no_motion') is False
                    and (call.get('evidence_recorded') is True or bool(call.get('evidence_ids'))))
                if request and call.get('lifecycle') == 'completed':
                    self.completed_requests.add(request)
                if name and (name not in self.completed or new_motion):
                    self.completed.add(name)
                    # Already-seen objects do not become new evidence merely
                    # because a skill completed. In particular a transport
                    # rejection is not resolved by rereading the same geometry.
                    self.candidates.clear()
                    self.candidate_turns = 0
                    self.visual_refresh_used = False
                    self.failed_strategies.clear()
                    progress.append('skill_completed:' + name)
            for entity in call.get('scene_entities', []):
                # Newly available semantic evidence counts, not timestamps or
                # pose jitter. Loss/reacquisition of identical evidence cannot
                # replenish the same stage's deliberation budget indefinitely.
                for field in ('visible', 'has_pose', 'held', 'inserted'):
                    key = (entity.get('object_id'), field)
                    if entity.get(field) is True and key not in self.evidence:
                        self.evidence.add(key)
                        progress.append('new_evidence:' + str(key))
            if tool in ('preview_skill','inspect_plan') and call.get('planned') is True and call.get('current_scope') is True:
                key = json.dumps(args, sort_keys=True, separators=(',', ':'))
                if key not in self.candidates:
                    self.candidates.add(key)
                    if self.candidate_turns < self.max_candidate_turns:
                        self.candidate_turns += 1
                        progress.append('new_feasible_candidate')
        # One explicit view refresh per completed stage allows the model to
        # inspect a failure before re-prompting. Repeated pictures are bounded.
        if result.get('images') and not self.visual_refresh_used:
            self.visual_refresh_used = True
            progress.append('visual_recovery_view')
        self.idle_turns = 0 if progress else self.idle_turns + 1
        exhausted = self.idle_turns >= self.max_idle_turns
        decision_context = self.decision.update(result)
        unresolved = decision_context['unresolved_blockers']
        self.last_failure = unresolved[-1] if unresolved else None
        return dict(schema='f1-planner-progress-v1', progress=progress,
            idle_turns=self.idle_turns, remaining_idle_turns=max(0, self.max_idle_turns-self.idle_turns),
            exhausted=exhausted, last_failure=self.last_failure,
            recent_failed_strategies=list(self.failed_strategies.values()),
            decision_context=decision_context,
            next_action=('return_pending_review' if exhausted else
                'execute_or_change_recovery_strategy' if self.idle_turns else 'continue'),
            motion_authorized=False)
