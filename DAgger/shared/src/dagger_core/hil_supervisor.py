"""Pure POLICY/HIL ownership state machine; no GDK or robot output."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time


class HILState(str, Enum):
    POLICY = "policy"
    HIL_LATCHED = "hil_latched"
    HANDOFF_HOLD = "handoff_hold"
    SAFE_HOLD = "safe_hold"


@dataclass(frozen=True)
class HILUpdate:
    state: str
    control_source: str
    transition: str | None
    reason: str | None
    intervention_id: int
    policy_epoch: int
    hil_latched: bool
    human_arm_active: bool
    human_hand_active: bool
    fresh_policy_required: bool


class HILSupervisor:
    """Arbitrate policy and latched human control from reliable toggle IDs.

    RB never changes supervisor ownership.  It is only the arm deadman while
    HIL is already latched.  A policy handoff always waits for a fresh policy
    chunk from the epoch created at the exit request.
    """

    def __init__(self) -> None:
        self.state = HILState.POLICY
        self.intervention_id = 0
        self.policy_epoch = 0
        self.last_policy_request_id: int | None = None
        self.hold_reason: str | None = None
        self.handoff_monotonic_ns: int | None = None
        self._fresh_request: tuple[int, int, int, int] | None = None
        self._session_id: str | None = None
        self._last_toggle_id: int | None = None

    def _source(self, rb_deadman: bool, hand_motion_pending: bool) -> str:
        if self.state is HILState.POLICY:
            return "policy"
        if self.state is not HILState.HIL_LATCHED:
            return "hold"
        if rb_deadman and hand_motion_pending:
            return "human_arm_and_hand"
        if rb_deadman:
            return "human_arm"
        if hand_motion_pending:
            return "human_hand"
        return "human_hold"

    def _update(
        self,
        rb_deadman: bool,
        hand_motion_pending: bool,
        *,
        transition: str | None = None,
        reason: str | None = None,
    ) -> HILUpdate:
        return HILUpdate(
            state=self.state.value,
            control_source=self._source(rb_deadman, hand_motion_pending),
            transition=transition,
            reason=reason,
            intervention_id=self.intervention_id,
            policy_epoch=self.policy_epoch,
            hil_latched=self.state is HILState.HIL_LATCHED,
            human_arm_active=self.state is HILState.HIL_LATCHED and rb_deadman,
            human_hand_active=(
                self.state is HILState.HIL_LATCHED and hand_motion_pending
            ),
            fresh_policy_required=self.state is HILState.HANDOFF_HOLD,
        )

    def observe_operator(
        self,
        *,
        session_id: str,
        hil_toggle_id: int,
        rb_deadman: bool,
        hand_motion_pending: bool,
        transport_fresh: bool = True,
        monotonic_ns: int | None = None,
    ) -> HILUpdate:
        now = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        if now < 0:
            raise ValueError("negative operator time")
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if hil_toggle_id < 0:
            raise ValueError("hil_toggle_id must be non-negative")
        if not transport_fresh:
            return self.force_safe_hold("operator_transport_stale")

        if session_id != self._session_id:
            self._session_id = session_id
            self._last_toggle_id = hil_toggle_id
            return self._update(
                rb_deadman,
                hand_motion_pending,
                transition="operator_session_anchored",
            )

        assert self._last_toggle_id is not None
        if hil_toggle_id < self._last_toggle_id:
            return self.force_safe_hold("hil_toggle_id_regressed")
        if hil_toggle_id == self._last_toggle_id:
            return self._update(rb_deadman, hand_motion_pending)
        if hil_toggle_id != self._last_toggle_id + 1:
            self._last_toggle_id = hil_toggle_id
            return self.force_safe_hold("hil_toggle_id_gap")

        self._last_toggle_id = hil_toggle_id
        if self.state is HILState.POLICY:
            self.intervention_id += 1
            self.policy_epoch += 1
            self.last_policy_request_id = None
            self.hold_reason = None
            self.state = HILState.HIL_LATCHED
            return self._update(
                rb_deadman,
                hand_motion_pending,
                transition="policy_to_hil",
                reason="start_button_toggle",
            )

        if self.state is HILState.HIL_LATCHED:
            if rb_deadman:
                return self._update(
                    rb_deadman,
                    hand_motion_pending,
                    transition="hil_exit_rejected",
                    reason="release_rb_before_policy_handoff",
                )
            if hand_motion_pending:
                return self._update(
                    rb_deadman,
                    hand_motion_pending,
                    transition="hil_exit_rejected",
                    reason="wait_for_human_hand_motion",
                )
            self.policy_epoch += 1
            self.last_policy_request_id = None
            self.hold_reason = "waiting_for_fresh_policy"
            self.handoff_monotonic_ns = now
            self._fresh_request = None
            self.state = HILState.HANDOFF_HOLD
            return self._update(
                rb_deadman,
                hand_motion_pending,
                transition="hil_to_handoff_hold",
                reason="start_button_toggle",
            )

        if self.state in {HILState.HANDOFF_HOLD, HILState.SAFE_HOLD}:
            self.intervention_id += 1
            self.policy_epoch += 1
            self.last_policy_request_id = None
            self.hold_reason = None
            self.state = HILState.HIL_LATCHED
            return self._update(
                rb_deadman,
                hand_motion_pending,
                transition="hold_to_hil",
                reason="start_button_toggle",
            )

        raise RuntimeError(f"unhandled HIL state: {self.state}")

    def begin_policy_replan(self, now: int) -> HILUpdate:
        """Retain existing POLICY ownership, revoke its plan and require fresh input.

        This is not authorization from SAFE_HOLD or human control. The caller
        must commit a measured hold before enabling a new inference request.
        """
        if self.state is not HILState.POLICY:
            raise RuntimeError('Policy replan requires current policy ownership')
        self.policy_epoch += 1
        self.last_policy_request_id = None
        self._fresh_request = None
        self.handoff_monotonic_ns = now
        self.state = HILState.HANDOFF_HOLD
        self.hold_reason = 'rtc_waiting_for_fresh_policy'
        return self._update(False, False, transition='rtc_replan_started', reason=self.hold_reason)

    def register_fresh_policy_request(
        self, *, policy_epoch: int, request_id: int, observation_id: int,
        observation_monotonic_ns: int, request_monotonic_ns: int,
    ) -> bool:
        """Bind one pending request to a post-handoff acquisition (host clock).

        The acquisition source must supply actual capture-start time, not a
        relabelled cached frame's delivery time. Journal auditing checks the ID.
        """
        if (self.state is not HILState.HANDOFF_HOLD
                or policy_epoch != self.policy_epoch
                or min(request_id, observation_id) < 0
                or self.handoff_monotonic_ns is None
                or not self.handoff_monotonic_ns < observation_monotonic_ns <= request_monotonic_ns):
            return False
        self._fresh_request = (policy_epoch, request_id, observation_id, request_monotonic_ns)
        return True

    def accept_fresh_policy_chunk(
        self, *, policy_epoch: int, request_id: int, response_monotonic_ns: int | None = None
    ) -> HILUpdate:
        if request_id < 0:
            raise ValueError("request_id must be non-negative")
        if self.state is not HILState.HANDOFF_HOLD:
            return self._update(
                False,
                False,
                transition="policy_chunk_rejected",
                reason="not_waiting_for_policy",
            )
        if policy_epoch != self.policy_epoch:
            return self._update(
                False,
                False,
                transition="policy_chunk_rejected",
                reason="stale_policy_epoch",
            )
        response_ns = time.monotonic_ns() if response_monotonic_ns is None else response_monotonic_ns
        if (self._fresh_request is None or self._fresh_request[:2] != (policy_epoch, request_id)
                or response_ns < self._fresh_request[3]):
            return self._update(False, False, transition="policy_chunk_rejected",
                                reason="unbound_or_noncausal_fresh_request")
        self._fresh_request = None
        self.last_policy_request_id = request_id
        self.hold_reason = None
        self.state = HILState.POLICY
        return self._update(
            False,
            False,
            transition="fresh_policy_to_policy",
            reason="fresh_policy_chunk_accepted",
        )

    def force_safe_hold(self, reason: str) -> HILUpdate:
        if not reason:
            raise ValueError("safe hold reason must be non-empty")
        changed = self.state is not HILState.SAFE_HOLD or self.hold_reason != reason
        if changed:
            self.policy_epoch += 1
            self.last_policy_request_id = None
        self.hold_reason = reason
        self.state = HILState.SAFE_HOLD
        return self._update(
            False,
            False,
            transition="safe_hold_entered" if changed else None,
            reason=reason,
        )

    def telemetry_fields(self) -> dict[str, object]:
        return {
            "hil_state": self.state.value,
            "hil_latched": self.state is HILState.HIL_LATCHED,
            "intervention_id": self.intervention_id,
            "policy_epoch": self.policy_epoch,
            "policy_request_id": self.last_policy_request_id,
            "fresh_policy_required": self.state is HILState.HANDOFF_HOLD,
            "hil_hold_reason": self.hold_reason,
            "operator_session_id": self._session_id,
            "hil_toggle_id": self._last_toggle_id,
        }
