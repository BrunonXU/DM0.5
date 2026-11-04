"""Locally authorized synchronous policy; no synthetic operator packets."""
import os
import select
import sys

from dagger_core.hil_supervisor import HILState
from .policy import F1PolicyEngine
from .policy_errors import IKRejected


class StandaloneFinished(Exception):
    """Normal deadline/onsite interrupt, handled by the CLI cleanup path."""


class StandalonePolicyEngine(F1PolicyEngine):
    operator_required = False

    def __init__(self, *args, duration_seconds, stop_requested, **kwargs):
        self.duration_seconds = duration_seconds
        self.stop_requested = stop_requested
        self.deadline_ns = None
        self.ik_failures = 0
        self.ik_retry_limit = 3
        self.ik_state = 'running'
        self.ik_hold_pending = False
        super().__init__(*args, **kwargs)

    def authorize_local(self, now):
        with self.context_lock:
            if self.started or self.supervisor.state is not HILState.POLICY:
                raise RuntimeError('standalone authorization is one-shot; restart after a fault')
            with self.latest_lock:
                self.start_frame_anchor = None if self.latest is None else self.latest[0]
            self.minimum_observation_ns = self.started_monotonic_ns = now
            self.deadline_ns = (None if self.duration_seconds is None else
                                now + round(self.duration_seconds * 1e9))
            self.started = True
            self.emit('local_policy_authorized', monotonic_ns=now,
                      duration_seconds=self.duration_seconds, deadline_monotonic_ns=self.deadline_ns,
                      operator_transport=False, policy_epoch=self.supervisor.policy_epoch)

    def before_publication(self, now):
        if self.stop_requested():
            raise StandaloneFinished('local_stop')
        if not self.started:
            raise RuntimeError('standalone policy has no local authorization')
        if self.deadline_ns is not None and now >= self.deadline_ns:
            raise StandaloneFinished('duration_elapsed')
        recovery = ((self.ik_state == 'retrying' and self.supervisor.state is HILState.HANDOFF_HOLD)
                    or (self.ik_state == 'paused' and self.supervisor.state is HILState.SAFE_HOLD))
        if self.supervisor.state is not HILState.POLICY and not recovery:
            raise RuntimeError('standalone fault: ' + str(self.supervisor.hold_reason))

    def tick(self, now):
        if not self.started:
            return
        self.before_publication(now)
        super().tick(now)
        if self.ready is not None and self.ik_state == 'retrying':
            self.emit('ik_recovery_succeeded', monotonic_ns=now, failures=self.ik_failures,
                      request_id=self.chunk_request_id, policy_epoch=self.supervisor.policy_epoch)
            self.ik_state, self.ik_failures = 'running', 0

    def policy_failed(self, exc, now):
        if not isinstance(exc, IKRejected):
            if not self.fault_lifecycle:
                return super().policy_failed(exc, now)
            return self.pause_runtime_condition(now, 'policy_failed:' + str(exc)[:500])
        with self.context_lock:
            self.invalidate(now)
            self.ik_failures += 1
            self.ik_state = 'retrying' if self.ik_failures <= self.ik_retry_limit else 'paused'
            # Invalidate the old epoch. No request until Control has committed HOLD.
            self._record_update(self.supervisor.force_safe_hold('ik_' + self.ik_state), now)
            self.policy_enabled = False
            self.ik_hold_pending = True
            if self.ik_state == 'retrying':
                self._begin_fresh_handoff(now)
            self.emit('ik_recovery_' + self.ik_state, monotonic_ns=now,
                      failures=self.ik_failures, retry_limit=self.ik_retry_limit,
                      request_id=exc.response.get('request_id'),
                      observation_id=exc.response.get('observation_id'),
                      failed_step=len(exc.response['diagnostics']) - 1,
                      failure=exc.response['diagnostics'][-1],
                      policy_epoch=self.supervisor.policy_epoch)

    def _begin_fresh_handoff(self, now):
        self.supervisor.state = HILState.HANDOFF_HOLD
        self.supervisor.handoff_monotonic_ns = now
        self.supervisor.hold_reason = 'ik_waiting_for_fresh_policy'
        with self.latest_lock:
            self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
        self.minimum_observation_ns = now

    def recovery_hold_committed(self, now):
        with self.context_lock:
            self.ik_hold_pending = False
            if self.ik_state == 'retrying':
                self._begin_fresh_handoff(now)
                self.policy_enabled = True
            self.emit('ik_recovery_hold_committed', monotonic_ns=now,
                      state=self.ik_state, policy_epoch=self.supervisor.policy_epoch)

    def resume_local(self, now):
        self.before_publication(now)  # Deadline/stop still apply while paused.
        with self.context_lock:
            if (self.camera_issue or self.acquisition_error
                    or getattr(self, 'runtime_fault', None)):
                return False
            if self.ik_state != 'paused' or self.ik_hold_pending:
                return False
            self.ik_failures, self.ik_state = 0, 'retrying'
            self.supervisor.policy_epoch += 1
            self.invalidate(now)
            self._begin_fresh_handoff(now)
            # Re-anchor measured HOLD before any new model request.
            self.ik_hold_pending, self.policy_enabled = True, False
            self.emit('ik_recovery_resumed', monotonic_ns=now, policy_epoch=self.supervisor.policy_epoch)
            return True

    def disconnect(self, now, reason='standalone_fault'):
        from .faults import transient_control_reason
        if self.fault_lifecycle and transient_control_reason(reason):
            return self.pause_runtime_condition(now, reason)
        super().disconnect(now, reason)
        raise RuntimeError('standalone stopped: ' + reason)

    def pause_runtime_condition(self, now, reason, details=None):
        # HIL subclasses retain their real gamepad ownership protocol.
        if self.operator_required:
            return F1PolicyEngine.pause_runtime_condition(self, now, reason, details)
        with self.context_lock:
            self.invalidate(now)
            self._record_update(self.supervisor.force_safe_hold(reason), now)
            self.ik_state, self.ik_hold_pending, self.policy_enabled = 'paused', True, False
            self.pause_message = '策略已暂停，旧动作已撤销。数据恢复后按 Enter 用新观测继续。'
            return True

    def operator(self, *args, **kwargs):
        raise RuntimeError('standalone mode does not accept operator packets')


class LocalResumeInput:
    """Poll Enter only during a pause; never block the control/feedback loop."""
    def __init__(self, stream=None):
        self.stream = sys.stdin if stream is None else stream
        self.was_paused = False

    def poll(self, engine, now):
        paused = engine.ik_state == 'paused' and not engine.ik_hold_pending
        if paused and not self.was_paused:
            if self.stream.isatty():
                import termios
                termios.tcflush(self.stream.fileno(), termios.TCIFLUSH)
            print(getattr(engine, 'pause_message',
                'IK连续拒绝，已保持当前位置并暂停。按 Enter 重新观测继续；Ctrl+C结束。'), flush=True)
        self.was_paused = paused
        fd = self.stream.fileno()
        if select.select([fd], [], [], 0)[0]:
            data = os.read(fd, 4096)
            if not data and paused:
                raise RuntimeError('local input closed while IK paused')
            # Input received before pause is discarded, never queued as consent.
            if paused and b'\n' in data:
                engine.resume_local(now)
