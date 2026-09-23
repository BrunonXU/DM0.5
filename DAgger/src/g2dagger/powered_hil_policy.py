"""Synchronous policy chunks with continuous shared acquisition for Powered HIL.

No hardware commands here. The caller owns the sole 100 Hz dispatch boundary.
"""
from dataclasses import replace
import time
import numpy as np

from .hil_real_shadow import RealObservationHILShadow
from .hil_supervisor import HILState
from .real_observation_source import frames_advance, policy_input_issue


class PoweredHILPolicy(RealObservationHILShadow):
    def __init__(self, emit, source, infer, recorder, sink, *, require_start=True, **kwargs):
        self.started = not require_start
        self.started_monotonic_ns = None
        self.startup_session = None
        self.startup_toggle = None
        self.startup_released = False
        self.startup_received_ns = None
        self.start_frame_anchor = None
        self.strict_recording = True
        self.chunk = None
        self.ready = None
        self.chunk_started_ns = None
        self.chunk_anchor = None
        self.chunk_request_id = None
        self.action_index = None
        self.policy_sampling = None
        self.minimum_observation_ns = time.monotonic_ns()
        self.last_operator_key = None
        super().__init__(emit, source, infer, recorder, sink, **kwargs)
        if require_start:
            self.emit("runtime_waiting_for_start", monotonic_ns=time.monotonic_ns(),
                      policy_epoch=self.supervisor.policy_epoch)

    def operator(self, packet, received_ns, now, *, hand_pending=False):
        """Observe each packet once; count gaps remain detectable at 100 Hz."""
        key = (packet.session_id, packet.sequence)
        if key == self.last_operator_key:
            return False
        self.last_operator_key = key
        if not self.started:
            # Consume the first new START exclusively as run authorization.
            # A pressed/replayed packet at connection cannot authorize motion.
            if now - received_ns > self.watchdog_ns:
                return False
            if (packet.session_id != self.startup_session or self.startup_received_ns is None
                    or received_ns - self.startup_received_ns > self.watchdog_ns):
                self.startup_received_ns = received_ns
                self.startup_session = packet.session_id
                self.startup_toggle = packet.hil_toggle_id
                self.startup_released = not packet.hil_button_pressed
                return False
            self.startup_received_ns = received_ns
            difference = packet.hil_toggle_id - self.startup_toggle
            self.startup_toggle = packet.hil_toggle_id
            if difference != 1 or not self.startup_released:
                self.startup_released = not packet.hil_button_pressed
                return False
            with self.latest_lock:
                self.start_frame_anchor = None if self.latest is None else self.latest[0]
            self.started = True
            self.started_monotonic_ns = now
            self.minimum_observation_ns = now
            self.emit("operator_start_authorized", monotonic_ns=now,
                      session_id=packet.session_id, hil_toggle_id=packet.hil_toggle_id,
                      policy_epoch=self.supervisor.policy_epoch)
        before = self.supervisor.state
        # Logical progressive-command completion only, never physical feedback.
        self.hand_until_ns = now + 1 if hand_pending else 0
        super().receive(replace(packet, home_reset=False), now)
        self.last_received_ns = received_ns
        changed = before != self.supervisor.state
        if changed:
            self.invalidate(now)
        return changed

    def _record_update(self, update, now):
        if update.transition == 'fresh_policy_to_policy':
            self.emit(update.transition, monotonic_ns=now,
                      policy_request_id=self.supervisor.last_policy_request_id,
                      policy_epoch=update.policy_epoch, state=update.state)
            # Parent emits status, without duplicating the transition.
            update = replace(update, transition=None)
        return super()._record_update(update, now)

    def invalidate(self, now):
        if self.chunk is not None or self.ready is not None:
            self.emit("policy_suffix_discarded", monotonic_ns=now,
                      request_id=self.chunk_request_id, action_index=self.action_index)
        self.chunk = self.ready = None
        self.chunk_started_ns = None
        self.chunk_request_id = self.action_index = None
        self.minimum_observation_ns = now

    def disconnect(self, now, reason="operator_disconnected"):
        super().disconnect(now, reason)
        self.invalidate(now)

    def _infer(self, obs, epoch, request_id):
        started = time.monotonic_ns()
        chunk = self.infer(obs, request_id)
        response = time.monotonic_ns()
        actions = np.asarray(chunk.actions, dtype=float)
        if actions.shape != (50, 34) or not np.isfinite(actions).all():
            raise ValueError("policy must return finite absolute 50x34")
        if not self.recorder.record_policy_chunk(policy_epoch=epoch, request_id=request_id,
                observation_request_id=obs.observation_id,
                observation_monotonic_ns=obs.capture_started_monotonic_ns,
                request_monotonic_ns=started, response_monotonic_ns=response,
                actions_50x34=actions):
            raise RuntimeError("policy journal rejected record")
        return epoch, request_id, obs.observation_id, response, actions.copy()

    def tick(self, now):
        with self.context_lock:
            writer = getattr(self.sink, "downstream", self.sink)
            if getattr(writer, "writer_error", None) or getattr(self.recorder, "writer_error", None):
                raise RuntimeError(f"recording writer failed: payload={getattr(writer, 'writer_error', None)!r}; journal={getattr(self.recorder, 'writer_error', None)!r}")
            if self.acquisition_error:
                raise RuntimeError("acquisition stopped: " + self.acquisition_error)
            if not self.started:
                return
            if self.last_received_ns is not None and now - self.last_received_ns > self.watchdog_ns:
                self.disconnect(now, "operator_watchdog")
            if self.acquisition_error:
                raise RuntimeError("acquisition stopped: " + self.acquisition_error)
            if self.real_future is not None and self.real_future.done():
                future, self.real_future = self.real_future, None
                try:
                    epoch, rid, oid, response, actions = future.result()
                except Exception as exc:
                    self.disconnect(now, "policy_failed:" + str(exc))
                    return
                accepted = epoch == self.supervisor.policy_epoch and self.supervisor.state in (
                    HILState.POLICY, HILState.HANDOFF_HOLD)
                if accepted and self.supervisor.state is HILState.HANDOFF_HOLD:
                    update = self.supervisor.accept_fresh_policy_chunk(
                        policy_epoch=epoch, request_id=rid, response_monotonic_ns=response)
                    self._record_update(update, now)
                    accepted = update.transition == "fresh_policy_to_policy"
                self.emit("powered_policy_result", monotonic_ns=now, request_id=rid,
                          observation_id=oid, policy_epoch=epoch, accepted=accepted,
                          response_monotonic_ns=response)
                if accepted:
                    self.ready = actions
                    self.chunk_request_id = rid
            # Sync: no overlap of executing chunk and next inference request.
            if (self.chunk is not None or self.ready is not None or self.real_future is not None
                    or self.last_received_ns is None or self.supervisor.state not in (
                        HILState.POLICY, HILState.HANDOFF_HOLD)):
                return
            with self.latest_lock:
                latest = self.latest
            if latest is None:
                return
            obs, epoch = latest
            if (epoch != self.supervisor.policy_epoch or now < obs.available_monotonic_ns
                    or obs.capture_started_monotonic_ns <= self.minimum_observation_ns
                    or not frames_advance(obs, self.last_requested)
                    or now - self.last_request_ns < self.request_period_ns):
                return
            if self.request_id == 0 and not frames_advance(obs, self.start_frame_anchor):
                return
            issue = policy_input_issue(obs)
            if issue:
                self.last_request_ns = now
                self.emit("policy_input_rejected", monotonic_ns=now,
                          observation_id=obs.observation_id, reason=issue)
                return
            rid = self.request_id + 1
            if self.supervisor.state is HILState.HANDOFF_HOLD:
                if not frames_advance(obs, self.handoff_frame_anchor):
                    return
                if not self.supervisor.register_fresh_policy_request(policy_epoch=epoch,
                        request_id=rid, observation_id=obs.observation_id,
                        observation_monotonic_ns=obs.capture_started_monotonic_ns,
                        request_monotonic_ns=now):
                    return
            self.request_id, self.last_request_ns, self.last_requested = rid, now, obs
            self.emit("powered_policy_request", monotonic_ns=now, request_id=rid,
                      policy_epoch=epoch, observation_id=obs.observation_id)
            self.real_future = self.executor.submit(self._infer, obs, epoch, rid)

    def sample(self, now, last_submitted):
        """30 Hz waypoints sampled at the owner's 100 Hz clock; no extrapolation.

        One 1/30 s anchor interval precedes the 49 original waypoint intervals.
        The anchor is a submitted target, explicitly NOT measured feedback.
        """
        self.policy_sampling = None
        if not self.started or self.supervisor.state is not HILState.POLICY:
            return np.asarray(last_submitted).copy(), "hold"
        if self.ready is not None:
            self.chunk, self.ready = self.ready, None
            self.chunk_anchor = np.asarray(last_submitted).copy()
            self.chunk_started_ns = now
        if self.chunk is None:
            return np.asarray(last_submitted).copy(), "hold"
        u = max(0., (now - self.chunk_started_ns) / 1e9 * 30.)
        if u < 1:
            result = (1-u) * self.chunk_anchor + u * self.chunk[0]
            self.action_index = 0
        else:
            position = min(u-1, 49.)
            i = int(position)
            result = (1-(position-i)) * self.chunk[i] + (position-i) * self.chunk[min(i+1, 49)]
            self.action_index = i
        lower = -1 if u < 1 else int(min(u-1, 49.))
        upper = 0 if u < 1 else min(lower+1, 49)
        alpha = u if u < 1 else min(u-1, 49.) - lower
        self.policy_sampling = dict(kind='linear_anchor_30hz', source_hz=30.,
            chunk_started_monotonic_ns=self.chunk_started_ns,
            lower_index=lower, upper_index=upper, alpha=alpha,
            anchor_34d=self.chunk_anchor.tolist() if lower == -1 else None,
            raw_waypoint_34d=self.chunk[self.action_index].tolist())
        if u >= 50:
            self.chunk = None
            self.minimum_observation_ns = now
        return result.copy(), "policy"
