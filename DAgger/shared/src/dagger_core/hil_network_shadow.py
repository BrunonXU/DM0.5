"""Real operator packets, synthetic observations/policy, ZERO hardware output."""
from dataclasses import asdict

from .hil_supervisor import HILState, HILSupervisor
from .shadow import ShadowSession


class HILNetworkShadow:
    def __init__(self, emit, *, watchdog_ns=200_000_000, response_delay_ns=1_000_000_000, synthetic=True):
        self.synthetic = synthetic
        self.emit = emit
        self.supervisor = HILSupervisor()
        self.session = ShadowSession()
        self.watchdog_ns = watchdog_ns
        self.response_delay_ns = response_delay_ns
        self.last_received_ns = None
        self.packet = None
        self.hand_until_ns = 0
        self.hand_targets = {}
        self.pending = None
        self.observation_id = 0
        self.request_id = 0
        self.next_observation_ns = 0
        self.last_status = None

    def _record_update(self, update, now):
        fields = asdict(update)
        if update.transition:
            self.emit(update.transition, monotonic_ns=now, **fields)
        status = (update.state, update.control_source)
        if status != self.last_status:
            self.emit("status", monotonic_ns=now, **fields)
            self.last_status = status
        return update

    def receive(self, packet, now):
        ack = self.session.process(packet)
        self.emit("operator_packet", monotonic_ns=now, packet=asdict(packet), accepted=ack.accepted,
                  reason=ack.reason)
        if not ack.accepted:
            self.disconnect(now, "packet_rejected:" + str(ack.reason))
            return ack
        if self.packet is not None and self.packet.session_id != packet.session_id:
            self.disconnect(now, "operator_session_changed")
        self.packet = packet
        self.last_received_ns = now
        # This is an explicitly synthetic hand completion interval, not O10 feedback.
        if self.synthetic and self.supervisor.state is HILState.HIL_LATCHED and packet.active_side and packet.grip_target is not None:
            if self.hand_targets.get(packet.active_side) != packet.grip_target:
                self.hand_targets[packet.active_side] = packet.grip_target
                self.hand_until_ns = now + 1_000_000_000
                self.emit("synthetic_hand_intent", monotonic_ns=now, side=packet.active_side,
                          target=packet.grip_target, simulated_complete_ns=self.hand_until_ns)
        update = self.supervisor.observe_operator(
            session_id=packet.session_id, hil_toggle_id=packet.hil_toggle_id,
            rb_deadman=packet.deadman, hand_motion_pending=now < self.hand_until_ns,
            monotonic_ns=now)
        if update.transition in ("policy_to_hil", "hold_to_hil"):
            self.hand_targets.clear()
            if packet.active_side and packet.grip_target is not None:
                self.hand_targets[packet.active_side] = packet.grip_target
        if packet.home_reset:
            self.emit("home_ignored_zero_output", monotonic_ns=now)
        self._record_update(update, now)
        return ack

    def disconnect(self, now, reason="operator_disconnected"):
        self.session.watchdog_timeout()
        self.last_received_ns = None
        self.hand_until_ns = 0
        self.pending = None
        self._record_update(self.supervisor.force_safe_hold(reason), now)

    def tick(self, now):
        if self.last_received_ns is not None and now - self.last_received_ns > self.watchdog_ns:
            self.disconnect(now, "operator_watchdog")
        # Keep observation generation independent of paused inference. No catch-up bursts.
        if self.synthetic and now >= self.next_observation_ns:
            self.next_observation_ns = now + 33_333_334
            oid = self.observation_id
            self.observation_id += 1
            self.emit("synthetic_observation", monotonic_ns=now, observation_id=oid,
                      policy_epoch=self.supervisor.policy_epoch, cameras_present=False, measured_present=False)
            if (self.supervisor.state is HILState.HANDOFF_HOLD and self.pending is None
                    and now > self.supervisor.handoff_monotonic_ns):
                self.request_id += 1
                epoch = self.supervisor.policy_epoch
                assert self.supervisor.register_fresh_policy_request(policy_epoch=epoch,
                    request_id=self.request_id, observation_id=oid,
                    observation_monotonic_ns=now, request_monotonic_ns=now)
                self.pending = (epoch, self.request_id, now + self.response_delay_ns)
                self.emit("synthetic_policy_request", monotonic_ns=now, observation_id=oid,
                          request_id=self.request_id, policy_epoch=epoch,
                          handoff_monotonic_ns=self.supervisor.handoff_monotonic_ns)
        if self.pending is not None and now >= self.pending[2]:
            epoch, request_id, _ = self.pending
            self.pending = None
            self.emit("synthetic_policy_response", monotonic_ns=now, request_id=request_id, policy_epoch=epoch)
            self._record_update(self.supervisor.accept_fresh_policy_chunk(
                policy_epoch=epoch, request_id=request_id, response_monotonic_ns=now), now)
        if self.packet is not None and self.last_received_ns is not None:
            self._record_update(self.supervisor.observe_operator(
                session_id=self.packet.session_id, hil_toggle_id=self.packet.hil_toggle_id,
                rb_deadman=self.packet.deadman, hand_motion_pending=now < self.hand_until_ns,
                monotonic_ns=now), now)
