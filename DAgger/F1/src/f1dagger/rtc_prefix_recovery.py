"""Explicit experimental recovery; importing this module enables nothing.

Retain a verified rejection's feasible prefix, then hold its final target while
the EXISTING output trajectory converges. This engine never resets planner v/a,
publishes, changes limits, projects EEF, or issues a new request before stopping.
The ControlLoop integration must inject evidence from the same actual owner.
"""
from dataclasses import dataclass

import numpy as np

from dagger_core.hil_supervisor import HILState
from dagger_core.powered_hil_policy import ChunkPolicy
from .policy_errors import IKRejected
from .rtc import RTCPolicyEngine
from .standalone import StandalonePolicyEngine


@dataclass(frozen=True)
class PrefixStopRequest:
    policy_epoch: int
    request_id: int
    owner_token: str
    target16: tuple
    endpoint_monotonic_ns: int


@dataclass(frozen=True)
class PrefixStopEvidence:
    """Evidence callback contract, all times on the local monotonic clock.

    ``published_monotonic_ns`` identifies an ACTUALLY dispatched stationary
    right-arm command, not target approval. Keep this publication's identity
    until all seven feedback stamps advance beyond it; using the latest 250 Hz
    publication would unnecessarily outrun slower feedback. The certificate is
    the same owner's active certified target, with unreset planner p/v/a read
    from that command. ``owner_token`` is an integration-owned stable identity.
    No inferred/synthetic feedback or state-reset-to-zero is admissible.
    """
    policy_epoch: int
    request_id: int
    owner_token: str
    target16: tuple
    certificate_valid: bool
    certified_right_rad: tuple
    planner_right_position_rad: tuple
    planner_right_velocity_rad_s: tuple
    planner_right_acceleration_rad_s2: tuple
    published16: tuple
    published_monotonic_ns: int
    measured16: tuple
    feedback_timestamps_ns: tuple
    observed_monotonic_ns: int


class RTCFeasiblePrefixEngine(RTCPolicyEngine):
    """Default off. Only integrations providing actual stop evidence may opt in.

    stop_probe(request, now) returns None while still moving or fresh feedback
    is unavailable, else PrefixStopEvidence. The tolerance and feedback-age
    feedback age and planner deadband MUST come from the existing machine
    profile/planner. Feedback following tolerance is an explicit experimental
    setting, not a claim of robot accuracy. A bounded settle timeout is required.
    """
    def __init__(self, *args, feasible_prefix_enabled=False, stop_probe=None,
                 prefix_owner_token=None, prefix_stop_tolerance_deg=None,
                 prefix_feedback_max_age_ns=None, prefix_settle_timeout_s=None,
                 prefix_planner_deadband_rad=None,
                 tracking_hold_recovery_enabled=False,
                 **kwargs):
        self.feasible_prefix_enabled = bool(feasible_prefix_enabled)
        self.prefix_stop_probe = stop_probe
        self.prefix_stop_diagnostics = None
        self.prefix_owner_token = prefix_owner_token
        self.prefix_stop_tolerance_deg = prefix_stop_tolerance_deg
        self.prefix_feedback_max_age_ns = prefix_feedback_max_age_ns
        self.prefix_settle_timeout_s = prefix_settle_timeout_s
        self.prefix_planner_deadband_rad = prefix_planner_deadband_rad
        self.prefix_recovery = None
        self.tracking_hold_recovery = None
        self.tracking_hold_recovery_enabled = bool(tracking_hold_recovery_enabled)
        if self.feasible_prefix_enabled:
            if (not callable(stop_probe) or not isinstance(prefix_owner_token,str) or not prefix_owner_token
                    or prefix_stop_tolerance_deg is None or not np.isfinite(prefix_stop_tolerance_deg) or prefix_stop_tolerance_deg < 0
                    or not isinstance(prefix_feedback_max_age_ns,int) or prefix_feedback_max_age_ns <= 0
                    or prefix_planner_deadband_rad is None or not np.isfinite(prefix_planner_deadband_rad) or prefix_planner_deadband_rad < 0
                    or prefix_settle_timeout_s is None or not np.isfinite(prefix_settle_timeout_s) or prefix_settle_timeout_s <= 0):
                raise ValueError('Feasible-prefix recovery requires same-owner stop evidence and existing profile bounds')
        super().__init__(*args, **kwargs)

    def invalidate(self, now):
        self.prefix_recovery = None
        self.tracking_hold_recovery = None
        super().invalidate(now)

    def pause_timing(self, now, reason):
        # Keep the original fail-closed transition and measured HOLD. Diagnose
        # before invalidate() clears the failed plan; never infer task success.
        if self.ik_state=='paused':
            return
        recovery=self.prefix_recovery or self.tracking_hold_recovery
        if reason in ('prefix_recovery_stop_timeout','tracking_hold_confirmation_timeout') and recovery is not None and recovery.get('request') is not None:
            request=recovery['request']
            diagnostic=dict(blockers=['diagnostic_probe_unavailable'])
            if callable(self.prefix_stop_diagnostics):
                try:
                    diagnostic=self.prefix_stop_diagnostics(request,now)
                except Exception as exc:
                    diagnostic=dict(blockers=['diagnostic_probe_error'],error_type=type(exc).__name__)
            self.emit('rtc_prefix_recovery_stop_failed' if self.prefix_recovery is not None else 'rtc_tracking_hold_failed',monotonic_ns=now,
                request_id=request.request_id,policy_epoch=request.policy_epoch,
                diagnostic=diagnostic,terminal_execution_confirmed=False,
                inference_timeout=False,automatic_retry=False)
        super().pause_timing(now,reason)
        if reason=='prefix_recovery_stop_timeout':
            self.pause_message='IK可行前段结束，但未通过停止/实测到位确认，已保持并暂停。按 Enter 用新观测重新规划；Ctrl+C结束保存。'
        elif reason=='prefix_recovery_identity_changed':
            self.pause_message='IK恢复的计划身份发生变化，已保持并暂停。按 Enter 用新观测重新规划；Ctrl+C结束保存。'
        elif reason=='tracking_hold_confirmation_timeout':
            self.pause_message='实测保持未通过发布和新鲜反馈确认，已暂停。按 Enter 重新观测继续；Ctrl+C结束保存。'

    def _proposal(self, exc, now):
        """Return a fully checked prefix or raise; never mutate live plan here."""
        if self.ik_failures >= self.ik_retry_limit:
            raise ValueError('retry_budget_exhausted')
        if self.real_future is not None or self.prefix_recovery is not None:
            raise ValueError('unexpected_inflight_or_recovery')
        response, ctx = exc.response, self.pending_context
        if not isinstance(ctx,dict):
            raise ValueError('missing_pending_context')
        if self.last_requested is None or self.last_request_ns is None:
            raise ValueError('missing_request_observation')
        timing = self.policy.last_timing
        if (timing.get('response_verified') is not True or timing.get('outcome')!='ik_rejected'
                or timing.get('request_id')!=self.real_future_request_id
                or timing.get('policy_epoch')!=self.supervisor.policy_epoch
                or self.real_future_epoch!=self.supervisor.policy_epoch
                or response.get('request_id')!=self.real_future_request_id
                or response.get('policy_epoch')!=self.supervisor.policy_epoch
                or response.get('session_id')!=timing.get('session_id')
                or not timing.get('session_id')):
            raise ValueError('unverified_rejection_identity')
        expected = {k:ctx[k] for k in ('bootstrap','delay_steps','shift_steps','coverage_steps','plan_origin_ns','base_plan_id')}
        if (response.get('rtc_context')!=expected or response.get('success') is not False
                or response.get('reason')!='ik_rejected' or response.get('actions') is not None
                or response.get('active_action_indices')!=[7,8,9,10,11,12,13,15]
                or getattr(self.policy,'right_only',False) is not True):
            raise ValueError('unsupported_rejection_contract')
        prefix = np.asarray(ctx['committed_actions16'],float).reshape(-1,16)
        held = np.asarray(self.policy.held_getter(),float)
        absolute = np.asarray(response.get('absolute_eef10'),float)
        details = response.get('diagnostics')
        if (held.shape!=(16,) or absolute.shape!=(50,10) or not np.isfinite(held).all()
                or not np.isfinite(absolute).all() or not np.isfinite(prefix).all()
                or not isinstance(details,list) or len(details)<2
                or len(prefix)+len(details)>ctx['coverage_steps']
                or details[-1].get('success') is not False or details[-1].get('joints') is not None):
            raise ValueError('missing_or_invalid_feasible_points')
        if ctx['bootstrap']:
            if len(prefix) or ctx['base_plan_id']!=0 or ctx['plan_origin_ns']!=0:
                raise ValueError('invalid_bootstrap_prefix')
            anchor,origin = held.copy(),now
        else:
            shift,delay = ctx['shift_steps'],ctx['delay_steps']
            if (self.chunk is None or self.plan_id!=ctx['base_plan_id'] or len(prefix)!=delay
                    or shift < 1 or shift+delay>len(self.chunk)
                    or not np.array_equal(prefix,self.chunk[shift:shift+delay])
                    or ctx['plan_origin_ns']!=self.chunk_started_ns+round(shift/self.waypoint_hz*1e9)):
                raise ValueError('changed_committed_prefix_or_owner')
            elapsed=(now-ctx['plan_origin_ns'])/1e9*self.waypoint_hz
            if not 0 <= elapsed < delay:
                raise ValueError('late_prefix_adoption')
            anchor,origin = self.chunk[shift-1].copy(),ctx['plan_origin_ns']
        actions = prefix.tolist()
        for offset,diag in enumerate(details[:-1],len(prefix)):
            q=np.asarray(diag.get('joints'),float)
            error=np.array([diag.get('position_error_mm',np.inf),diag.get('rotation_error_deg',np.inf)],float)
            if (diag.get('success') is not True or diag.get('within_limits') is not True
                    or q.shape!=(7,) or not np.isfinite(q).all() or not np.isfinite(error).all()
                    or np.any(error<0) or error[0]>.01 or error[1]>.001):
                raise ValueError('unverified_feasible_joint')
            action=held.copy();action[7:14]=q
            action[15]=np.clip(absolute[offset,9]*50+50,0,100)
            actions.append(action.tolist())
        actions=np.asarray(actions,float)
        # Preserve the original valid gripper sequence as ordinary decoding
        # does. Stopping later holds its final value; it does not undo opening or
        # claim that a grasp/release phase can be interrupted without task cost.
        if len(actions)<=len(prefix):
            raise ValueError('no_valid_new_point')
        for action in actions:
            self.policy.validate_action(action)  # Existing profile limits/coupling.
        endpoint=origin+round(len(actions)/self.waypoint_hz*1e9)
        if endpoint<=now:
            raise ValueError('prefix_already_consumed')
        return actions,absolute,anchor,origin,endpoint

    def policy_failed(self, exc, now):
        if not self.feasible_prefix_enabled or not isinstance(exc,IKRejected):
            return super().policy_failed(exc,now)
        with self.context_lock:
            try:
                actions,absolute,anchor,origin,endpoint=self._proposal(exc,now)
            except (ValueError,TypeError,KeyError,AttributeError,IndexError) as reason:
                self.emit('rtc_prefix_recovery_declined',monotonic_ns=now,reason=str(reason))
                return super().policy_failed(exc,now)
            # Normal _rtc_infer never records a rejected response. Save the
            # actually selected partial chunk before it can appear in Control.
            self.record_chunk(self.last_requested,self.real_future_epoch,self.real_future_request_id,
                self.last_request_ns,now,actions)
            if self.supervisor.state is HILState.HANDOFF_HOLD:
                update=self.supervisor.accept_fresh_policy_chunk(policy_epoch=self.real_future_epoch,
                    request_id=self.real_future_request_id,response_monotonic_ns=now)
                self._record_update(update,now)
                if update.transition!='fresh_policy_to_policy':
                    raise RuntimeError('Prefix recovery ownership rejected')
            elif self.supervisor.state is not HILState.POLICY:
                return super().policy_failed(exc,now)
            self.chunk,self.absolute_eef,self.chunk_anchor=actions,absolute,anchor
            self.chunk_started_ns=origin
            self.plan_id=self.chunk_request_id=self.real_future_request_id
            self.policy_enabled=False
            self.ik_state='prefix_advancing'
            request=PrefixStopRequest(self.supervisor.policy_epoch,self.plan_id,self.prefix_owner_token,
                tuple(actions[-1]),endpoint)
            self.prefix_recovery=dict(request=request,exception=exc,
                deadline_ns=endpoint+round(self.prefix_settle_timeout_s*1e9))
            self.emit('rtc_prefix_recovery_started',monotonic_ns=now,request_id=self.plan_id,
                policy_epoch=self.supervisor.policy_epoch,committed_points=len(self.pending_context['committed_actions16']),
                valid_points=len(actions),actions16=actions.tolist(),endpoint_monotonic_ns=endpoint,
                failures_already_counted=self.ik_failures,original_target_preserved=True,
                observation_id=self.last_requested.observation_id,context=self.pending_context,
                original_absolute_eef10=absolute.tolist(),
                original_rejection=exc.response['diagnostics'][-1],
                gripper_values_change=bool(np.any(np.diff(np.vstack([anchor,actions])[:,14:],axis=0)!=0)),
                terminal_gripper_targets=actions[-1,14:].tolist(),
                planner_deadband_rad=self.prefix_planner_deadband_rad,
                experiment_feedback_following_tolerance_deg=self.prefix_stop_tolerance_deg)

    def _command_stopped(self, proof, request, now):
        if not isinstance(proof,PrefixStopEvidence):
            raise TypeError('stop_probe must return PrefixStopEvidence or None')
        if (proof.owner_token!=request.owner_token or proof.policy_epoch!=request.policy_epoch
                or proof.request_id!=request.request_id or proof.certificate_valid is not True
                or tuple(proof.target16)!=request.target16):
            return False
        target=np.asarray(request.target16,float)
        arrays=[np.asarray(v,float) for v in (proof.certified_right_rad,proof.planner_right_position_rad,
            proof.planner_right_velocity_rad_s,proof.planner_right_acceleration_rad_s2)]
        published,measured=np.asarray(proof.published16,float),np.asarray(proof.measured16,float)
        stamps=np.asarray(proof.feedback_timestamps_ns)
        if (any(v.shape!=(7,) or not np.isfinite(v).all() for v in arrays)
                or published.shape!=(16,) or measured.shape!=(16,) or stamps.shape!=(16,)
                or not np.isfinite(published).all() or not np.isfinite(measured).all()
                or not np.issubdtype(stamps.dtype,np.integer)):
            return False
        cert,pos,velocity,acceleration=arrays
        return bool(np.allclose(cert,np.deg2rad(target[7:14]),atol=self.prefix_planner_deadband_rad,rtol=0)
            and np.allclose(pos,cert,atol=self.prefix_planner_deadband_rad,rtol=0)
            and np.max(abs(velocity))<=1e-9 and np.max(abs(acceleration))<=1e-9
            and np.allclose(np.deg2rad(published[7:14]),cert,atol=self.prefix_planner_deadband_rad,rtol=0)
            and np.array_equal(published[14:],target[14:])
            and request.endpoint_monotonic_ns<=proof.published_monotonic_ns<=proof.observed_monotonic_ns<=now
            and now-proof.observed_monotonic_ns<=self.prefix_feedback_max_age_ns
            and np.all(stamps[7:14]>proof.published_monotonic_ns)
            and np.all(stamps[7:14]<=proof.observed_monotonic_ns)
            and np.all(now-stamps[7:14]<=self.prefix_feedback_max_age_ns))

    def _stopped(self, proof, request, now):
        return bool(self._command_stopped(proof,request,now)
            and np.max(abs(np.asarray(proof.measured16)[7:14]-np.asarray(request.target16)[7:14]))
                <=self.prefix_stop_tolerance_deg)

    def _begin_tracking_hold(self, recovery, proof, now):
        """Abandon the unmet target, then use the EXISTING measured HOLD path.

        This is not target arrival. The original planner must have naturally
        stopped before its ordinary HOLD reset; no new request is authorized
        until that HOLD was actually published and newer feedback confirms it.
        """
        request=recovery['request']
        if (not self.tracking_hold_recovery_enabled or self.ik_failures>=self.ik_retry_limit
                or proof is None or not self._command_stopped(proof,request,now)
                or self._stopped(proof,request,now)):
            return False
        original_target=request.target16
        super().policy_failed(recovery['exception'],now)
        self.policy_enabled=False
        self.tracking_hold_recovery=dict(request=None,original_request_id=request.request_id,
            original_target16=original_target,policy_epoch=self.supervisor.policy_epoch,
            deadline_ns=now+round(self.prefix_settle_timeout_s*1e9))
        self.emit('rtc_tracking_hold_started',monotonic_ns=now,request_id=request.request_id,
            policy_epoch=self.supervisor.policy_epoch,original_target16=list(original_target),
            measured16=list(proof.measured16),original_target_reached=False,
            original_command_stationary_publication_ns=proof.published_monotonic_ns,
            recovery_action='existing_measured_right_hold',failures=self.ik_failures,
            retry_limit=self.ik_retry_limit)
        return True

    def recovery_hold_committed(self, now):
        if self.tracking_hold_recovery is None:
            return super().recovery_hold_committed(now)
        # Control calls this only AFTER owner.publish commits last_submitted.
        # Capture the actual HOLD publication, not earlier measured state.
        with self.context_lock:
            held=np.asarray(self.policy.held_getter(),float)
            if held.shape!=(16,) or not np.isfinite(held).all():
                return self.pause_timing(now,'tracking_hold_confirmation_timeout')
            self.ik_hold_pending=False
            recovery=self.tracking_hold_recovery
            recovery['request']=PrefixStopRequest(self.supervisor.policy_epoch,
                recovery['original_request_id'],self.prefix_owner_token,tuple(held),now)
            self.emit('rtc_tracking_hold_published',monotonic_ns=now,
                request_id=recovery['original_request_id'],policy_epoch=self.supervisor.policy_epoch,
                held_target16=held.tolist(),model_output=False)

    def _check_tracking_hold(self, now):
        StandalonePolicyEngine.before_publication(self,now)
        recovery=self.tracking_hold_recovery
        if self.supervisor.policy_epoch!=recovery['policy_epoch'] or self.real_future is not None:
            return self.pause_timing(now,'prefix_recovery_identity_changed')
        if now>=recovery['deadline_ns']:
            return self.pause_timing(now,'tracking_hold_confirmation_timeout')
        request=recovery['request']
        if request is None:return
        proof=self.prefix_stop_probe(request,now)
        if proof is not None and self._stopped(proof,request,now):
            self.tracking_hold_recovery=None
            self._begin_fresh_handoff(now)
            self.policy_enabled=True
            self.emit('rtc_tracking_hold_confirmed',monotonic_ns=now,
                request_id=request.request_id,policy_epoch=request.policy_epoch,
                held_target16=list(request.target16),original_target_reached=False,
                minimum_new_observation_ns=self.minimum_observation_ns,failures=self.ik_failures)

    def before_publication(self, now):
        if self.tracking_hold_recovery is not None:
            return self._check_tracking_hold(now)
        if self.prefix_recovery is None:
            return super().before_publication(now)
        StandalonePolicyEngine.before_publication(self,now)  # Ctrl+C/deadline/owner.
        recovery=self.prefix_recovery;request=recovery['request']
        if (self.supervisor.policy_epoch!=request.policy_epoch or self.plan_id!=request.request_id
                or self.real_future is not None):
            return self.pause_timing(now,'prefix_recovery_identity_changed')
        if now>=recovery['deadline_ns']:
            if self.tracking_hold_recovery_enabled:
                proof=self.prefix_stop_probe(request,now)
                if self._begin_tracking_hold(recovery,proof,now):return
            return self.pause_timing(now,'prefix_recovery_stop_timeout')
        if now>=request.endpoint_monotonic_ns:
            proof=self.prefix_stop_probe(request,now)
            if proof is not None and self._stopped(proof,request,now):
                self.emit('rtc_prefix_recovery_stopped',monotonic_ns=now,request_id=request.request_id,
                    published_monotonic_ns=proof.published_monotonic_ns,
                    feedback_timestamps_ns=list(proof.feedback_timestamps_ns))
                self.prefix_recovery=None
                # Increment the EXISTING retry counter only now. Partial motion
                # never resets it; a complete accepted window retains that role.
                super().policy_failed(recovery['exception'],now)

    def tick(self, now):
        if self.prefix_recovery is None:
            return super().tick(now)
        self.before_publication(now)
        writer=getattr(self.sink,'downstream',self.sink)
        if getattr(writer,'writer_error',None) or getattr(self.recorder,'writer_error',None):
            raise RuntimeError('RTC recording writer failed')
        if self.acquisition_error:
            raise RuntimeError('RTC acquisition stopped: '+self.acquisition_error)
        if self._camera_fault_pending is not None:
            self.disconnect(now,'camera_unavailable:'+self._camera_fault_pending)
        # No inference scheduling, including the tick that confirms stopping.

    def sample(self, now, last_submitted):
        if self.prefix_recovery is None:
            return super().sample(now,last_submitted)
        self.before_publication(now)
        if self.prefix_recovery is None:
            return super().sample(now,last_submitted)
        if now>=self.prefix_recovery['request'].endpoint_monotonic_ns:
            self.action_index=len(self.chunk)-1
            self.policy_sampling=self.sampling_metadata(self.action_index,self.action_index,0.)
            self.policy_sampling['prefix_recovery_terminal_target']=True
            return self.chunk[-1].copy(),'policy'
        return ChunkPolicy.sample(self,now,last_submitted)
