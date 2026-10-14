"""Continuous real acquisition + single-flight policy alongside operator HIL.

This stage intentionally has no IK, selected human commands, or output adapter.
Returned policy chunks are evidence only. It is not a training episode converter.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from .hil_network_shadow import HILNetworkShadow
from .hil_supervisor import HILState
from .real_observation_source import frames_advance, policy_input_issue


from dagger_core.hil_real_shadow import ObservationHIL

class RealObservationHILShadow(ObservationHIL):
    def _infer(self, obs, epoch, request_id):
        started = time.monotonic_ns()
        chunk = self.infer(obs, request_id)
        response = time.monotonic_ns()
        self.recorder.record_policy_chunk(policy_epoch=epoch, request_id=request_id,
            observation_request_id=obs.observation_id,
            observation_monotonic_ns=obs.capture_started_monotonic_ns,
            request_monotonic_ns=started, response_monotonic_ns=response,
            actions_50x34=chunk.actions)
        return epoch, request_id, obs.observation_id, response

    def tick(self, now):
        with self.context_lock:
            super().tick(now)
            if self.acquisition_error:
                raise RuntimeError("acquisition stopped: " + self.acquisition_error)
            if self.real_future is not None and self.real_future.done():
                future, self.real_future = self.real_future, None
                try:
                    epoch, request_id, oid, response = future.result()
                except Exception as exc:
                    self.disconnect(time.monotonic_ns(), "policy_failed:" + str(exc))
                    return
                accepted = epoch == self.supervisor.policy_epoch and self.supervisor.state in (
                    HILState.POLICY, HILState.HANDOFF_HOLD)
                self.emit("real_policy_result", monotonic_ns=time.monotonic_ns(),
                    response_monotonic_ns=response, policy_epoch=epoch, request_id=request_id,
                    observation_id=oid, accepted=accepted, execution="none")
                if accepted and self.supervisor.state is HILState.HANDOFF_HOLD:
                    self._record_update(self.supervisor.accept_fresh_policy_chunk(
                        policy_epoch=epoch, request_id=request_id, response_monotonic_ns=response), time.monotonic_ns())
            if (self.real_future is not None or self.last_received_ns is None
                    or self.supervisor.state not in (HILState.POLICY, HILState.HANDOFF_HOLD)):
                return
            with self.latest_lock:
                latest = self.latest
            if latest is None:
                return
            obs, epoch = latest
            if epoch != self.supervisor.policy_epoch or now < obs.available_monotonic_ns:
                return
            if now - self.last_request_ns < self.request_period_ns or not frames_advance(obs, self.last_requested):
                return
            issue = policy_input_issue(obs)
            if issue:
                # Rate limit diagnostic, without replacing any measured value.
                self.last_request_ns = now
                self.emit("policy_input_rejected", monotonic_ns=now, observation_id=obs.observation_id, reason=issue)
                return
            request_id = self.request_id + 1
            if self.supervisor.state is HILState.HANDOFF_HOLD:
                if not frames_advance(obs, self.handoff_frame_anchor):
                    return
                if not self.supervisor.register_fresh_policy_request(policy_epoch=epoch, request_id=request_id,
                        observation_id=obs.observation_id, observation_monotonic_ns=obs.capture_started_monotonic_ns,
                        request_monotonic_ns=now):
                    return
            self.request_id = request_id
            self.last_request_ns = now
            self.last_requested = obs
            self.emit("real_policy_request_submitted", monotonic_ns=now, policy_epoch=epoch,
                request_id=request_id, observation_id=obs.observation_id,
                observation_monotonic_ns=obs.capture_started_monotonic_ns)
            self.real_future = self.executor.submit(self._infer, obs, epoch, request_id)

