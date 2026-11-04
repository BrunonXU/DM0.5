"""Right EEF10 anchored to raw feedback. No robot imports or command publication."""
from pathlib import Path

import numpy as np

from .native.kinematics import ArmModel, ContinuousIK


def solver():
    return ContinuousIK(ArmModel(Path(__file__).parent / "native/ccs_m6_40.MvKDCfg"))


def normalize_state(values, stats):
    values = np.asarray(values, np.float64)
    low, high = np.asarray(stats["low"]), np.asarray(stats["high"])
    if values.shape != (16,) or not np.isfinite(values).all():
        raise ValueError("Expected finite physical state16")
    span = high - low
    safe_span = np.where(span < 1e-4, 1., span)
    scaled = 2 * (values - (low + high) / 2) / safe_span
    # Near-constant right joints use a one-degree span, rather than amplify noise.
    deviation = np.maximum(low - values, values - high).clip(min=0)
    return np.clip(scaled, -1, 1).astype(np.float32), dict(
        outside_indices=np.flatnonzero(deviation > 0).tolist(),
        physical_deviation=deviation.tolist(), clipped_indices=np.flatnonzero(abs(scaled) > 1).tolist())


def encode_actions(model, state16, actions16):
    anchor = model.fk(1, np.asarray(state16, float)[7:14])
    inverse = np.linalg.inv(anchor)
    result = np.zeros((len(actions16), 10), np.float64)
    for i, action in enumerate(actions16):
        delta = inverse @ model.fk(1, action[7:14])
        result[i, :3] = delta[:3, 3]
        result[i, 3:9] = delta[:2, :3].reshape(6)
        result[i, 9] = (action[15] - 50) / 50
    return result.astype(np.float32)


def normalize_actions(actions10, stats):
    result = np.zeros((*actions10.shape[:-1], 32), np.float32)
    result[..., :10] = actions10
    result[..., :3] = (actions10[..., :3] - np.asarray(stats["mean"])) / np.asarray(stats["std"])
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite action labels")
    return result


def physical_actions(actions32, stats):
    action = np.asarray(actions32, np.float64)
    if action.ndim != 2 or action.shape[1] != 32 or not np.isfinite(action[:, :10]).all():
        raise ValueError("Expected finite Hx32 prediction")
    result = action[:, :10].copy()
    result[:, :3] = result[:, :3] * stats["std"] + stats["mean"]
    return result


def relative_pose(action):
    first, second = action[3:6].copy(), action[6:9].copy()
    if np.linalg.norm(first) < 1e-6:
        raise ValueError("Degenerate rotation6D first row")
    first /= np.linalg.norm(first)
    second -= np.dot(first, second) * first
    if np.linalg.norm(second) < 1e-6:
        raise ValueError("Degenerate rotation6D second row")
    second /= np.linalg.norm(second)
    pose = np.eye(4)
    pose[:3, :3] = np.stack([first, second, np.cross(first, second)])
    pose[:3, 3] = action[:3]
    return pose


def decode_prefix(ik, action10, state16, held16, execute_steps=20):
    state, held = np.asarray(state16, float), np.asarray(held16, float)
    if state.shape != (16,) or held.shape != (16,) or not np.isfinite(held).all():
        raise ValueError("Expected raw state16 and controller held16")
    if not 1 <= execute_steps <= len(action10):
        raise ValueError("Invalid execution horizon")
    anchor = ik.model.fk(1, state[7:14])
    reference = state[7:14].copy()
    output, diagnostics = [], []
    for action in action10[:execute_steps]:
        result = ik.solve(1, anchor @ relative_pose(action), reference)
        diagnostics.append(result)
        if not result["success"]:
            return dict(success=False, actions=None, diagnostics=diagnostics, reason="ik_rejected")
        reference = np.asarray(result["joints"])
        target = held.copy()
        target[7:14] = reference
        target[15] = np.clip(action[9] * 50 + 50, 0, 100)
        output.append(target.tolist())
    return dict(success=True, actions=output, diagnostics=diagnostics,
                gripper_clipped_count=int(np.count_nonzero(abs(action10[:execute_steps, 9]) > 1)))
