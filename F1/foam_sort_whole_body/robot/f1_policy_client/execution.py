"""Validate an entire server IK prefix before exposing any executable command.

The server owns the frozen FK/IK and action normalization. The robot independently
checks response identity, all target fields, live feedback and the first transition,
then reuses the existing replay scheduler and publishers. There is no IK fallback,
trajectory clipping, retiming, collision model or new dynamics limit in this module.
The first-step proximity heuristic can explicitly be telemetry-only for model tests;
joint limits, wrist coupling, response identity and fresh feedback remain checked.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from f1_replay.bundle import ARM_NAMES, ReplayBundle, ReplayFrame
from f1_replay.runtime import ReplayError, check_feedback, finite_number, validate_actions

from .observation import Observation, positive_seconds


CONTRACT = "f1_foam_inference_v1"
IK_RECIPE = "f1_ikfk_demo_v1"
PREFIX_POLICY = 'f1_ik_valid_prefix_replan_v1'


class ObservationExpired(ReplayError):
    """A past observation exceeded its budget; request new input, never reuse it."""


class FirstArmStepRejected(ReplayError):
    """A prefix violates the selected rejecting first-step proximity policy."""


class IKPrefixRejected(ReplayError):
    """Identity-checked response with no executable IK trajectory."""

    def __init__(self, response):
        self.ik = deepcopy(response.get('ik', {}))
        super().__init__(f"Policy/IK prefix rejected: ik_rejected; details={self.ik}")


@dataclass(frozen=True)
class PreparedPrefix:
    bundle: ReplayBundle
    response: dict
    gripper_mapping: tuple = ()
    first_arm_delta_deg: tuple = ()


def _vector(value, length, label):
    if (not isinstance(value, (list, tuple)) or len(value) != length
            or not all(finite_number(number) for number in value)):
        raise ReplayError(f"{label}: expected {length} finite numbers")
    return tuple(float(number) for number in value)


def _matrix(value, rows, columns, label):
    if not isinstance(value, list) or len(value) != rows:
        raise ReplayError(f"{label}: expected a complete {rows}-step prefix")
    return tuple(_vector(row, columns, f"{label}[{index}]") for index, row in enumerate(value))


def prepare_prefix(response, observation: Observation, reference_snapshot, profile, *,
                   now_ns, request_id, expected_model_binding, max_observation_age_s,
                   command_hz=30, execute_steps=20, start_index=0):
    """Return a fully checked bundle, or raise without a partial trajectory.

The request anchor is immutable even if the execution feedback has advanced.
Subsequent prefixes may start with a moving base; initial stopped-base and
long-lived body-pose checks belong to the runtime preflight/tick.
"""
    budget_ns = positive_seconds(max_observation_age_s, "max_observation_age_s")
    if (type(now_ns) is not int or type(observation.captured_ns) is not int
            or now_ns < observation.captured_ns):
        raise ReplayError("Policy observation clock is invalid or in the future")
    if now_ns - observation.captured_ns > budget_ns:
        raise ObservationExpired("Policy response belongs to an expired observation: "
                                 f"age={(now_ns-observation.captured_ns)/1e9:.3f}s, "
                                 f"budget={max_observation_age_s:g}s")
    if command_hz != 30 or type(execute_steps) is not int or execute_steps not in (20, 50):
        raise ReplayError("F1 requires H20 sync or H50 native at 30 Hz")
    if type(start_index) is not int or not 0 <= start_index < execute_steps or (execute_steps == 20 and start_index):
        raise ReplayError('Invalid executable prefix start index')
    if not isinstance(response, dict) or response.get("contract") != CONTRACT:
        raise ReplayError("F1 policy response contract mismatch")
    if execute_steps == 50 and response.get('execution_mode') != 'async_native_h50_v1':
        raise ReplayError('H50 native response capability mismatch')
    if (not isinstance(request_id, str) or not request_id
            or response.get("request_id") != request_id):
        raise ReplayError("Policy response request_id mismatch")
    if (type(response.get("observation_monotonic_ns")) is not int
            or response["observation_monotonic_ns"] != observation.captured_ns):
        raise ReplayError("Policy response observation timestamp mismatch")
    state19 = _vector(observation.state19, 19, "observation.state19")
    body5 = _vector(observation.body5, 5, "observation.body5")
    anchor = _vector(response.get("anchor_joints14_deg"), 14, "anchor_joints14_deg")
    if anchor != state19[:14]:
        raise ReplayError("Policy response FK anchor differs from the request HAL joints")
    if (not isinstance(expected_model_binding, dict) or not expected_model_binding
            or response.get("model_binding") != expected_model_binding):
        raise ReplayError("Policy response checkpoint/statistics binding mismatch")
    expected_ik = profile.get('inference_ik_recipe_version', IK_RECIPE)
    approximate_ik_options = {
        'f1_ikfk_demo_pos5mm_v1': {'position_tolerance_mm': 5.0, 'rotation_tolerance_deg': 0.001},
        'f1_ikfk_demo_pos5mm_rot005deg_v1': {'position_tolerance_mm': 5.0, 'rotation_tolerance_deg': 0.05},
    }
    if expected_ik not in (IK_RECIPE, *approximate_ik_options):
        raise ReplayError('Unknown selected IK recipe')
    if response.get("ik_recipe_version") != expected_ik:
        raise ReplayError("Policy response uses a different frozen IK recipe")
    if expected_ik in approximate_ik_options and response.get('ik_options') != approximate_ik_options[expected_ik]:
        raise ReplayError('OOD IK tolerance binding mismatch')
    if (not finite_number(response.get("frequency_hz")) or response["frequency_hz"] != 30
            or type(response.get("execute_steps")) is not int or response["execute_steps"] != execute_steps):
        raise ReplayError("Policy response frequency/horizon differs from selected execution mode")
    prefix_policy=profile.get('inference_prefix_policy','fixed')
    if prefix_policy not in ('fixed',PREFIX_POLICY):
        raise ReplayError('Unknown executable prefix policy')
    if prefix_policy==PREFIX_POLICY:
        if execute_steps!=20 or start_index or expected_ik!='f1_ikfk_demo_pos5mm_rot005deg_v1':
            raise ReplayError('Variable prefix requires the selected demo IK and synchronous H20')
        if response.get('prefix_policy')!=PREFIX_POLICY:
            raise ReplayError('Variable prefix capability mismatch')
        count=response.get('valid_steps')
        if type(count) is not int or not 0<=count<=20:
            raise ReplayError('Invalid valid_steps')
        if response.get('status')=='ready':
            if not count:raise ReplayError('Ready response has an empty prefix')
            truncation=response.get('truncation')
            if count<20 and (not isinstance(truncation,dict) or truncation.get('reason')!='ik_rejected'
                             or type(truncation.get('step')) is not int or truncation['step']!=count
                             or type(truncation.get('arm')) is not int or truncation['arm'] not in (0,1)):
                raise ReplayError('Invalid partial-prefix failure boundary')
            if count==20 and truncation is not None:raise ReplayError('Full prefix marked truncated')
        elif count!=0:
            raise ReplayError('Rejected response exposes accepted commands')
    else:
        if response.get('prefix_policy','fixed')!='fixed':
            raise ReplayError('Unexpected variable prefix response')
        count=execute_steps
    if response.get("status") != "ready":
        if response.get('status') == 'rejected' and response.get('reason') == 'ik_rejected':
            raise IKPrefixRejected(response)
        raise ReplayError(f"Policy/IK prefix rejected: {response.get('reason', response.get('status'))}")

    joints = _matrix(response.get("joint_targets_deg"), count, 14, "joint_targets_deg")
    grippers = _matrix(response.get("gripper_targets"), count, 2, "gripper_targets")
    base = _matrix(response.get("base_velocity_targets"), count, 3, "base_velocity_targets")
    gripper_policy = profile.get("inference_gripper_range_policy", "reject")
    first_gripper_policy = profile.get("inference_gripper_start_policy", "proximity")
    if gripper_policy not in ("reject", "saturate_0_100") or first_gripper_policy not in ("proximity", "target_position"):
        raise ReplayError("Unknown inference gripper execution policy")
    mapping = []
    if gripper_policy == "saturate_0_100":
        mapped = []
        for index, row in enumerate(grippers):
            values = tuple(min(100., max(0., value)) for value in row)
            for side, raw, command in zip(("left", "right"), row, values):
                if raw != command:
                    mapping.append({"frame": index, "side": side, "raw": raw, "command": command})
            mapped.append(values)
        grippers = tuple(mapped)
    # IK targets must pass unchanged. The replay profile may explicitly clip
    # legacy lead targets, but that mapping would alter a solved EEF trajectory.
    strict_profile = deepcopy(profile)
    strict_profile["arm_limit_policy"] = "reject"
    state21 = state19[:16] + body5
    frames = []
    for index in range(count):
        for arm in (0, 1):
            j6, j7 = joints[index][arm * 7 + 5:arm * 7 + 7]
            # Match the frozen solver's ~1e-9 rad numerical limit tolerance.
            if 1.025 * abs(j6) + abs(j7) > 110.5 + 1e-7:
                raise ReplayError(f"Prefix step {index}, arm {arm}: frozen wrist coupling exceeded")
        if index < start_index:
            continue
        relative_ns = round((index-start_index) * 1e9 / command_hz)
        frames.append(ReplayFrame(relative_ns, observation.captured_ns + round(index * 1e9 / command_hz),
                                  state21, joints[index] + grippers[index] + base[index]))
    manifest = {
        "status": "replay_ready", "kind": "f1_policy_prefix",
        "request_id": request_id, "model_binding": deepcopy(expected_model_binding),
        "ik_recipe_version": expected_ik, "observation_monotonic_ns": observation.captured_ns,
        "timing_reference": "local_request_observation_monotonic_ns",
        "execution_contract": {"arm_limit_policy": "reject",
                               "gripper_range_policy": gripper_policy,
                               "gripper_start_policy": first_gripper_policy},
        "gripper_mapping": mapping,
        "start_index": start_index,
        "decoded_steps": execute_steps,
        "valid_steps": count,
        "prefix_policy": prefix_policy,
        "truncation": deepcopy(response.get('truncation')),
    }
    digest = hashlib.sha256(json.dumps({"manifest": manifest,
                                       "commands": [frame.action for frame in frames]},
                                      sort_keys=True, allow_nan=False).encode()).hexdigest()
    bundle = ReplayBundle(frames, round((count-start_index) * 1e9 / command_hz), manifest,
                          digest, Path("<live-policy-prefix>"))
    validate_actions(bundle, strict_profile)
    feedback = check_feedback(reference_snapshot, frames[0], strict_profile, now_ns, initial=False)
    first_arm_limit = profile.get("inference_first_arm_max_delta_deg", profile["start_arm_tolerance_deg"])
    first_arm_policy = profile.get("inference_first_arm_policy", "reject")
    if first_arm_policy not in ("reject", "report_only"):
        raise ReplayError("Unknown inference first-arm policy")
    if first_arm_policy == "reject" and (not finite_number(first_arm_limit) or first_arm_limit <= 0):
        raise ReplayError("Invalid inference first-arm delta limit")
    first_arm_delta = tuple(target-actual for actual, target in zip(feedback["arms"], joints[start_index]))
    violations = [f"{name}: feedback={actual:.5f}, target={target:.5f}, delta={delta:+.5f}deg"
                  for name, actual, target, delta in zip(ARM_NAMES, feedback["arms"], joints[start_index], first_arm_delta)
                  if first_arm_policy == "reject" and abs(delta) > first_arm_limit]
    if violations:
        raise FirstArmStepRejected(f"First IK target too far from current HAL feedback (limit={first_arm_limit:g}deg): "
                          + "; ".join(violations))
    for actual, target in zip(feedback["grippers"], grippers[start_index]):
        if not 0 <= actual <= 100:
            raise ReplayError("Gripper feedback must use the F1 0..100 scale")
        if first_gripper_policy == "proximity" and abs(actual - target) > profile["start_gripper_tolerance"]:
            raise ReplayError("First gripper target too far from current feedback")
    return PreparedPrefix(bundle, deepcopy(response), tuple(mapping), first_arm_delta)
