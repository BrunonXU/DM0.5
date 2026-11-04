"""RTC scheduling on the existing owner clock; never publishes commands.

Plans retain the original 30 Hz time grid, including its anchor interval.
Inference runs in the existing worker and transport child, never in Control.
"""
from collections import deque
import math
import time

import numpy as np

from dagger_core.hil_supervisor import HILState
from .standalone import StandalonePolicyEngine


class RTCTimingError(RuntimeError):
    pass


class RTCPolicyEngine(StandalonePolicyEngine):
    def __init__(self, *args, rtc_delay_steps=15, rtc_min_steps=20, **kwargs):
        if not 1 <= rtc_delay_steps <= rtc_min_steps <= 49-rtc_delay_steps:
            raise ValueError('RTC requires 1 <= d <= s_min < H-d')
        self.delays = deque([rtc_delay_steps], maxlen=10)
        self.minimum_delay_steps = rtc_delay_steps
        self.s_min = rtc_min_steps
        self.absolute_eef = None
        self.plan_id = 0
        self.pending_context = None
        self.rtc_horizon = 50
        self.rtc_observation_grid = False
        # Request completion includes client packing, transport, server work,
        # recording and the join guard. Keep timing observations separate from
        # motion ownership: revoking an epoch must not forget a slow request.
        self.rlt_request_durations_ns = deque(maxlen=10)
        self._rtc_future_timing = None
        self._rtc_deferred_plan_id = None
        self.pause_message = '策略已暂停。按 Enter 重新观测继续；Ctrl+C结束。'
        super().__init__(*args, **kwargs)

    def invalidate(self, now):
        self.absolute_eef, self.plan_id = None, 0
        super().invalidate(now)

    def before_publication(self, now):
        super().before_publication(now)
        if (self.real_future is not None and self.pending_context is not None
                and not self.pending_context['bootstrap']
                and self.real_future_epoch == self.supervisor.policy_epoch
                and (now-self.pending_context['plan_origin_ns']) / 1e9 * self.waypoint_hz
                >= self.pending_context['delay_steps']):
            self.pause_timing(now, 'inference_deadline')
        if (self.chunk is not None and self.chunk_started_ns is not None
                and (now-self.chunk_started_ns)/1e9*self.waypoint_hz >= len(self.chunk)):
            self.pause_timing(now, 'plan_exhausted')

    def pause_timing(self, now, reason):
        """Invalidate motion immediately; Control commits measured HOLD before Enter.

        Keep the in-flight future until it finishes, then discard its old epoch.
        Never block Control on socket close, worker cancellation, or a new plan.
        """
        with self.context_lock:
            if self.ik_state == 'paused':
                return
            context = self.pending_context or {}
            self.emit('rtc_timing_paused', monotonic_ns=now, reason=reason,
                request_id=self.real_future_request_id if self.real_future is not None else self.plan_id,
                plan_origin_ns=context.get('plan_origin_ns'), delay_steps=context.get('delay_steps'),
                policy_epoch=self.supervisor.policy_epoch)
            self.invalidate(now)
            self._record_update(self.supervisor.force_safe_hold('rtc_' + reason), now)
            self.ik_state, self.ik_hold_pending, self.policy_enabled = 'paused', True, False
            self.pause_message = 'RTC响应超时或计划耗尽，已保持并暂停。按 Enter 用新观测重新规划；Ctrl+C结束保存。'

    def policy_failed(self, exc, now):
        self.pause_message = 'IK连续拒绝，已保持当前位置并暂停。按 Enter 重新观测继续；Ctrl+C结束。'
        super().policy_failed(exc, now)

    def resume_local(self, now):
        resumed = super().resume_local(now)
        if resumed:
            self.delays.clear()
            self.delays.append(self.minimum_delay_steps)
            self.emit('rtc_local_resume', monotonic_ns=now, policy_epoch=self.supervisor.policy_epoch)
        return resumed

    def _rtc_infer(self, obs, epoch, rid, context):
        started = time.monotonic_ns()
        try:
            actions, absolute = self.policy.infer_rtc(obs, rid, epoch, context)
        finally:
            self.emit('policy_transport_timing', request_id=rid, observation_id=obs.observation_id,
                      policy_epoch=epoch, timing=self.policy.last_timing)
        response = time.monotonic_ns()
        self.record_chunk(obs, epoch, rid, started, response, actions)
        return actions, absolute, response

    def _observe_rlt_request_duration(self, now):
        timing = self._rtc_future_timing
        if (timing is None or timing['observed'] or not timing['rlt']
                or not timing['future'].done()):
            return
        timing['observed'] = True
        future = timing['future']
        if future.cancelled() or future.exception() is not None:
            return
        # A done callback timestamps worker completion even if Control later
        # drains a revoked request. The old action remains revoked.
        ready = timing.get('completed_monotonic_ns', now)
        duration = max(0, ready-timing['requested_monotonic_ns'])
        self.rlt_request_durations_ns.append(duration)
        self.emit('rtc_request_duration_observed', monotonic_ns=now,
                  request_id=timing['request_id'], rlt_phase='actor',
                  duration_ms=duration/1e6, includes_join_guard=True,
                  revoked_epoch=timing['epoch'] != self.supervisor.policy_epoch)

    def _rlt_prefetch_delay(self, now, origin, shift):
        """Fit a new request inside the real remaining C16 prefix coverage.

        Do not shift the observation grid to make a late request appear fresh.
        If the measured pipeline cannot fit, leave the current plan alone. Its
        existing exhaustion boundary will commit HOLD and request fresh input.
        """
        elapsed_ns = max(0, now-origin)
        estimate_ns = max(self.rlt_request_durations_ns, default=0)
        # Retain the existing one-waypoint scheduling margin. The minimum D is
        # unchanged; an observed full pipeline duration can only reserve more
        # *known old actions*, never extend their validity beyond the horizon.
        required = max(self.minimum_delay_steps,
                       math.ceil((elapsed_ns+estimate_ns)/1e9*self.waypoint_hz)+1)
        maximum = min(shift, self.rtc_horizon-shift,
                      self.s_min, self.rtc_horizon-self.s_min)
        if required <= maximum:
            return required
        if self._rtc_deferred_plan_id != self.plan_id:
            self._rtc_deferred_plan_id = self.plan_id
            self.emit('rtc_prefetch_deferred', monotonic_ns=now,
                      request_id=self.plan_id, rlt_phase='actor',
                      reason='measured_pipeline_exceeds_remaining_prefix',
                      elapsed_grid_ms=elapsed_ns/1e6,
                      estimated_pipeline_ms=estimate_ns/1e6,
                      required_delay_steps=required, available_delay_steps=maximum,
                      shift_steps=shift, plan_origin_ns=origin,
                      valid_plan_until_ns=self.chunk_started_ns+
                          round(len(self.chunk)/self.waypoint_hz*1e9),
                      action_authority_unchanged=True)
        return None

    def tick(self, now):
        if not self.started:
            return
        self.before_publication(now)
        with self.context_lock:
            self._observe_rlt_request_duration(now)
            writer = getattr(self.sink, 'downstream', self.sink)
            if getattr(writer, 'writer_error', None) or getattr(self.recorder, 'writer_error', None):
                from .faults import RecordingUnavailable
                raise RecordingUnavailable('RTC recording writer failed')
            if self.acquisition_error:
                if self.acquisition_error.startswith('RecordingUnavailable:'):
                    from .faults import RecordingUnavailable
                    raise RecordingUnavailable(self.acquisition_error)
                raise RuntimeError('RTC acquisition stopped: ' + self.acquisition_error)
            if self._camera_fault_pending is not None:
                self.disconnect(now, 'camera_unavailable:' + self._camera_fault_pending)
            # Revoked requests may outlive Y or a phase handoff, which clears
            # their RTC context. Check ownership before reading that context;
            # retain the future until the existing stale-result path drains it.
            if (self.real_future is not None
                    and self.real_future_epoch == self.supervisor.policy_epoch
                    and self.pending_context is None):
                self.policy_failed(RTCTimingError('active RTC request lost its context'), now)
                return
            if (self.real_future is not None
                    and self.real_future_epoch == self.supervisor.policy_epoch
                    and not self.pending_context['bootstrap']
                    and (now-self.pending_context['plan_origin_ns']) / 1e9 * self.waypoint_hz
                    >= self.pending_context['delay_steps']):
                self.pause_timing(now, 'inference_deadline')
            if self.real_future is not None and self.real_future.done():
                future, self.real_future = self.real_future, None
                context = self.pending_context
                if self.real_future_epoch != self.supervisor.policy_epoch:
                    self.emit('stale_policy_result_discarded', request_id=self.real_future_request_id)
                    return
                try:
                    actions, absolute, response = future.result()
                except Exception as exc:
                    self.policy_failed(exc, now)
                    return
                if not context['bootstrap']:
                    if self.plan_id != context['base_plan_id']:
                        raise RTCTimingError('RTC base plan no longer owned')
                    elapsed = (now-context['plan_origin_ns']) / 1e9 * self.waypoint_hz
                    # The suffix is solved from the last committed target. A late
                    # reply cannot silently replace already consumed waypoints.
                    if elapsed >= context['delay_steps']:
                        self.pause_timing(now, 'late_adoption')
                        return
                    if not self.rtc_observation_grid:
                        self.delays.append(max(1, math.ceil(elapsed)+1))
                    self.chunk_anchor = np.asarray(self.chunk[context['shift_steps']-1]).copy()
                    self.chunk_started_ns = context['plan_origin_ns']
                else:
                    self.chunk_anchor = self.policy.held_getter().copy()
                    self.chunk_started_ns = now
                if self.supervisor.state is HILState.HANDOFF_HOLD:
                    update = self.supervisor.accept_fresh_policy_chunk(policy_epoch=self.real_future_epoch,
                        request_id=self.real_future_request_id, response_monotonic_ns=response)
                    self._record_update(update, now)
                    if update.transition != 'fresh_policy_to_policy':
                        raise RuntimeError('RTC recovery ownership rejected')
                self.chunk, self.absolute_eef = actions, absolute
                self.plan_id = self.chunk_request_id = self.real_future_request_id
                self.ik_state, self.ik_failures = 'running', 0
                self.emit('rtc_plan_adopted', monotonic_ns=now, request_id=self.plan_id,
                    context=context, response_monotonic_ns=response, delay_buffer=list(self.delays))
            if (not self.policy_enabled or self.real_future is not None
                    or self.supervisor.state not in (HILState.POLICY, HILState.HANDOFF_HOLD)):
                return
            if self.chunk is not None:
                shift = int((now-self.chunk_started_ns) / 1e9 * self.waypoint_hz)
                threshold = (self.s_min if self.rtc_observation_grid else
                             max(self.s_min, self.minimum_delay_steps, max(self.delays)))
                if shift < threshold:
                    return
            else:
                shift = (self.s_min if self.rtc_observation_grid else
                         max(self.s_min, self.minimum_delay_steps, max(self.delays)))
            with self.latest_lock:
                latest = self.latest
            if latest is None:
                return
            obs, epoch = latest
            if (epoch != self.supervisor.policy_epoch or now < obs.available_monotonic_ns
                    or obs.capture_started_monotonic_ns <= self.minimum_observation_ns
                    or not self.frame_advancer(obs, self.last_requested)):
                return
            if self.request_id == 0 and not self.frame_advancer(obs, self.start_frame_anchor):
                return
            issue = self.input_validator(obs)
            if issue:
                self.emit('policy_input_rejected', observation_id=obs.observation_id, reason=issue)
                return
            if self.chunk is not None and self.rtc_observation_grid:
                # RTC replay starts at this observation, while the known old
                # prefix continues. Never anchor a moving observation to a
                # later, unrelated request-time grid.
                shift = int((obs.capture_started_monotonic_ns-self.chunk_started_ns)/1e9*self.waypoint_hz)
                if shift < self.s_min:
                    return
            d = max(self.minimum_delay_steps, max(self.delays))
            if self.rtc_observation_grid:
                d = self.minimum_delay_steps
                if self.chunk is not None:
                    origin = self.chunk_started_ns+round(shift/self.waypoint_hz*1e9)
                    d = self._rlt_prefetch_delay(now, origin, shift)
                    if d is None:
                        return
            rid = self.request_id+1
            if self.supervisor.state is HILState.HANDOFF_HOLD:
                if not self.frame_advancer(obs, self.handoff_frame_anchor):
                    return
                if not self.supervisor.register_fresh_policy_request(policy_epoch=epoch,
                        request_id=rid, observation_id=obs.observation_id,
                        observation_monotonic_ns=obs.capture_started_monotonic_ns, request_monotonic_ns=now):
                    return
            horizon = self.rtc_horizon
            if not d <= shift <= horizon-d:
                self.pause_timing(now, 'horizon_coverage')
                return
            bootstrap = self.chunk is None
            if not bootstrap and shift+d > len(self.chunk):
                self.pause_timing(now, 'ik_window_coverage')
                return
            context = dict(bootstrap=bootstrap, delay_steps=d, shift_steps=shift,
                coverage_steps=(16 if horizon == 16 else min(50, max(shift, d)+d+2)), base_plan_id=self.plan_id,
                plan_origin_ns=0 if bootstrap else self.chunk_started_ns+round(shift/ self.waypoint_hz*1e9),
                committed_actions16=[] if bootstrap else self.chunk[shift:shift+d].tolist(),
                previous_absolute_eef10=[] if bootstrap else self.absolute_eef[shift:].tolist())
            if horizon == 16:
                context['rlt'] = True
                context['next_decision_steps'] = self.s_min
            self.pending_context = context
            self.request_id, self.last_request_ns, self.last_requested = rid, now, obs
            self.emit('powered_policy_request', monotonic_ns=now, request_id=rid,
                      policy_epoch=epoch, observation_id=obs.observation_id, rtc=context)
            self.real_future = self.executor.submit(self._rtc_infer, obs, epoch, rid, context)
            self.real_future_epoch, self.real_future_request_id = epoch, rid
            timing = dict(future=self.real_future, request_id=rid, epoch=epoch,
                          requested_monotonic_ns=now, observed=False,
                          rlt=context.get('rlt') is True)
            self._rtc_future_timing = timing
            self.real_future.add_done_callback(lambda future, timing=timing:
                timing.update(completed_monotonic_ns=time.monotonic_ns()))

    def sample(self, now, last_submitted):
        if self.chunk is not None and (now-self.chunk_started_ns)/1e9*self.waypoint_hz >= len(self.chunk):
            self.pause_timing(now, 'plan_exhausted')
        return super().sample(now, last_submitted)
