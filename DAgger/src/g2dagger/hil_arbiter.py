"""Pure 34D action selection for the DAgger HIL supervisor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .hil_supervisor import HILState, HILUpdate


ACTION_DIM = 34


def _action(values: Sequence[float] | None, label: str) -> np.ndarray | None:
    if values is None:
        return None
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (ACTION_DIM,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{label} must be finite shape ({ACTION_DIM},)")
    return array.copy()


@dataclass(frozen=True)
class HILSelection:
    command: np.ndarray
    control_source: str
    intervention_mask: bool
    human_arm_mask: bool
    human_hand_mask: bool
    telemetry: dict[str, object]


class HILActionArbiter:
    """Choose exactly one full 34D command without mixed stale ownership."""

    def select(
        self,
        update: HILUpdate,
        *,
        hold_action: Sequence[float],
        policy_action: Sequence[float] | None = None,
        human_action: Sequence[float] | None = None,
        policy_request_id: int | None = None,
        policy_action_index: int | None = None,
        discarded_policy_suffix_steps: int | None = None,
    ) -> HILSelection:
        hold = _action(hold_action, "hold_action")
        policy = _action(policy_action, "policy_action")
        human = _action(human_action, "human_action")
        assert hold is not None

        if update.state == HILState.POLICY.value:
            if policy is None:
                raise ValueError("POLICY state requires policy_action")
            command = policy
            source = "policy"
        elif update.state == HILState.HIL_LATCHED.value and (
            update.human_arm_active or update.human_hand_active
        ):
            if human is None:
                raise ValueError("active HIL state requires human_action")
            command = human
            source = update.control_source
        else:
            command = hold
            source = update.control_source

        telemetry = {
            "hil_state": update.state,
            "control_source": source,
            "hil_transition": update.transition,
            "hil_transition_reason": update.reason,
            "hil_latched": update.hil_latched,
            "intervention_id": update.intervention_id,
            "policy_epoch": update.policy_epoch,
            "policy_request_id": policy_request_id,
            "policy_chunk_valid": (
                update.state == HILState.POLICY.value and policy is not None
            ),
            "policy_action_index": policy_action_index,
            "discarded_policy_suffix_steps": discarded_policy_suffix_steps,
            "fresh_policy_required": update.fresh_policy_required,
            "intervention_mask": update.hil_latched,
            "human_arm_mask": update.human_arm_active,
            "human_hand_mask": update.human_hand_active,
            "policy_proposal_34d": None if policy is None else policy.tolist(),
            "human_candidate_34d": None if human is None else human.tolist(),
            "selected_command_34d": command.tolist(),
        }
        return HILSelection(
            command=command,
            control_source=source,
            intervention_mask=update.hil_latched,
            human_arm_mask=update.human_arm_active,
            human_hand_mask=update.human_hand_active,
            telemetry=telemetry,
        )
