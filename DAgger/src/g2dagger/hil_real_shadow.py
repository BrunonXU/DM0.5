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


class RealObservationHILShadow(HILNetworkShadow):
    def __init__(self, emit, source, infer, recorder, sink, *, capture_hz=10., request_period=.7):
        super().__init__(emit, synthetic=False)
        if not 0 < capture_hz <= 30 or request_period <= 0:
            raise ValueError("capture_hz must be (0,30], request period positive")
        self.source, self.infer, self.recorder, self.sink = source, infer, recorder, sink
        self.capture_period = 1. / capture_hz
        self.request_period_ns = int(request_period * 1e9)
        self.context_lock = threading.RLock()
        self.latest_lock = threading.Lock()
        self.latest = None
        self.handoff_frame_anchor = None
        self.last_requested = None
        self.last_request_ns = 0
        self.real_future = None
        self.acquisition_error = None
        self.stop_event = threading.Event()
        self.record_trigger = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="g2-readonly-policy")
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="g2-readonly-acquisition")
        self.capture_thread.start()

    def _capture_loop(self):
        try:
            while not self.stop_event.is_set():
                with self.context_lock:
                    started = time.monotonic_ns()
                    state, epoch, intervention = (self.supervisor.state.value, self.supervisor.policy_epoch,
                                                   self.supervisor.intervention_id)
                obs = self.source.capture(started)
                # Only this thread touches the sink, including prebuffer trigger.
                if self.record_trigger.is_set():
                    triggered = self.sink.trigger()
                    if getattr(self, "strict_recording", False) and not triggered:
                        raise RuntimeError("prebuffer sink rejected trigger")
                    self.record_trigger.clear()
                submitted = self.sink.submit(obs)
                recorded = self.recorder.record_timestamped_observation(obs, hil_state=state,
                    intervention_id=intervention, policy_epoch=epoch)
                if getattr(self, "strict_recording", False) and (not submitted or not recorded):
                    raise RuntimeError("observation sink/journal rejected record")
                with self.latest_lock:
                    self.latest = (obs, epoch)
                remaining = self.capture_period - (time.monotonic_ns() - started) / 1e9
                self.stop_event.wait(max(0., remaining))
        except Exception as exc:
            self.acquisition_error = f"{type(exc).__name__}: {exc}"

    def receive(self, packet, now):
        with self.context_lock:
            before = self.supervisor.state
            ack = super().receive(packet, now)
            after = self.supervisor.state
            if before is not after and after is HILState.HIL_LATCHED:
                self.record_trigger.set()
            if before is not after and after is HILState.HANDOFF_HOLD:
                with self.latest_lock:
                    self.handoff_frame_anchor = None if self.latest is None else self.latest[0]
            return ack

    def disconnect(self, now, reason="operator_disconnected"):
        with self.context_lock:
            super().disconnect(now, reason)

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

    def close(self):
        self.stop_event.set()
        self.capture_thread.join(timeout=5.)
        if self.capture_thread.is_alive():
            raise RuntimeError("GDK acquisition still running; do not release GDK underneath it")
        self.executor.shutdown(wait=True)
