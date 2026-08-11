"""与正式DM0.5运行时隔离的旧版轻量残差RL工具。

用途：保留replay buffer、奖励塑形和线性actor-critic，供未来显式实验复用。
输入：34D state/action、next state、done和可选策略均值/残差特征。
输出：采样残差动作、TD/actor参数更新及可选NPZ replay文件。
方法：Gaussian线性残差策略加TD(0) critic；生产CLI默认完全不启用本模块。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .contracts import ACTION_DIM

logger = logging.getLogger(__name__)

LEFT_ARM = slice(0, 7)
RIGHT_ARM = slice(7, 14)


class SimpleReplayBuffer:
    """Fixed-capacity circular replay buffer from the legacy client."""

    def __init__(self, capacity: int = 50_000) -> None:
        self.capacity = capacity
        self.ptr = 0
        self.states: list[np.ndarray] = []
        self.actions: list[np.ndarray] = []
        self.rewards: list[float] = []
        self.next_states: list[np.ndarray] = []
        self.dones: list[bool] = []
        self.policy_means: list[np.ndarray] = []
        self.policy_residuals: list[np.ndarray] = []
        self.policy_phis: list[np.ndarray] = []

    def add(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: float,
        next_state: np.ndarray,
        done: bool,
        policy_mean: np.ndarray | None = None,
        policy_residual: np.ndarray | None = None,
        policy_phi: np.ndarray | None = None,
    ) -> None:
        zero_action = np.zeros_like(action)
        zero_phi = np.zeros(action.shape[0] + 1, dtype=np.float32)
        values = (
            state,
            action,
            reward,
            next_state,
            done,
            policy_mean if policy_mean is not None else zero_action,
            policy_residual if policy_residual is not None else zero_action,
            policy_phi if policy_phi is not None else zero_phi,
        )
        if len(self.states) < self.capacity:
            self.states.append(values[0])
            self.actions.append(values[1])
            self.rewards.append(values[2])
            self.next_states.append(values[3])
            self.dones.append(values[4])
            self.policy_means.append(values[5])
            self.policy_residuals.append(values[6])
            self.policy_phis.append(values[7])
        else:
            index = self.ptr
            self.states[index] = values[0]
            self.actions[index] = values[1]
            self.rewards[index] = values[2]
            self.next_states[index] = values[3]
            self.dones[index] = values[4]
            self.policy_means[index] = values[5]
            self.policy_residuals[index] = values[6]
            self.policy_phis[index] = values[7]
        self.ptr = (self.ptr + 1) % self.capacity

    def size(self) -> int:
        return len(self.states)

    def save(self, path: str | Path) -> None:
        np.savez_compressed(
            path,
            states=np.stack(self.states),
            actions=np.stack(self.actions),
            rewards=np.asarray(self.rewards, dtype=np.float32),
            next_states=np.stack(self.next_states),
            dones=np.asarray(self.dones, dtype=bool),
            policy_means=np.stack(self.policy_means),
            policy_residuals=np.stack(self.policy_residuals),
            policy_phis=np.stack(self.policy_phis),
        )
        logger.info("RL buffer saved to %s (size=%d)", path, self.size())


_REWARD_INIT_POSE_CACHE: dict[str, np.ndarray | None] = {
    "init_left": None,
    "init_right": None,
}


def compute_reward(
    previous_state: np.ndarray,
    next_state: np.ndarray,
    task_done: bool,
) -> float:
    """Legacy completion/return-to-start reward shaping."""
    reward = 0.0
    for key, group in (("init_left", LEFT_ARM), ("init_right", RIGHT_ARM)):
        if _REWARD_INIT_POSE_CACHE[key] is None:
            _REWARD_INIT_POSE_CACHE[key] = previous_state[group].copy()
        initial = _REWARD_INIT_POSE_CACHE[key]
        assert initial is not None
        previous_distance = float(
            np.linalg.norm(previous_state[group] - initial)
        )
        next_distance = float(np.linalg.norm(next_state[group] - initial))
        reward += 0.25 * (previous_distance - next_distance)
    reward += -1e-3 + (1.0 if task_done else 0.0)
    if task_done:
        _REWARD_INIT_POSE_CACHE["init_left"] = None
        _REWARD_INIT_POSE_CACHE["init_right"] = None
    return float(reward)


class RealRobotRLManager:
    """Legacy linear Gaussian residual actor and TD(0) critic."""

    def __init__(
        self,
        capacity: int = 50_000,
        train_interval: int = 200,
        save_path: str = "rl_buffer.npz",
        action_dim: int = ACTION_DIM,
        actor_state_dim: int = ACTION_DIM,
        mix_alpha: float = 0.3,
        actor_std: float = 0.05,
        residual_scale: float = 0.3,
    ) -> None:
        self.buffer = SimpleReplayBuffer(capacity)
        self.train_interval = train_interval
        self.save_path = save_path
        self.steps = 0
        self.gamma = 0.99
        self.lr = 1e-3
        self.batch_size = 128
        self.value_w: np.ndarray | None = None
        self.actor_state_dim = actor_state_dim
        self.action_dim = action_dim
        self.actor_w: np.ndarray | None = None
        self.actor_std = float(actor_std)
        self.actor_lr = self.lr
        self.mix_alpha = float(mix_alpha)
        self.residual_scale = float(residual_scale)

    def _init_value(self, state_dim: int) -> None:
        if self.value_w is None:
            self.value_w = np.zeros(state_dim + 1, dtype=np.float32)

    def _init_actor(self) -> None:
        if self.actor_w is None:
            base = np.zeros(self.actor_state_dim + 1, dtype=np.float32)
            self.actor_w = np.tile(base[:, None], (1, self.action_dim))

    def _phi(self, state: np.ndarray, *, actor: bool = False) -> np.ndarray:
        size = self.actor_state_dim if actor else len(state)
        return np.append(state[:size].astype(np.float32), 1.0)

    def sample_residual(
        self, state: np.ndarray
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        self._init_actor()
        assert self.actor_w is not None
        phi = self._phi(state, actor=True)
        mean = phi @ self.actor_w
        residual = mean + self.actor_std * np.random.randn(
            self.action_dim
        ).astype(np.float32)
        return residual.astype(np.float32), {
            "phi": phi.astype(np.float32),
            "mean": mean.astype(np.float32),
        }

    def on_step(
        self,
        previous_state: np.ndarray,
        executed_action: np.ndarray,
        next_state: np.ndarray,
        task_done: bool,
        actor_info: dict[str, np.ndarray] | None = None,
        residual: np.ndarray | None = None,
    ) -> None:
        reward = compute_reward(previous_state, next_state, task_done)
        self.buffer.add(
            previous_state.astype(np.float32),
            executed_action.astype(np.float32),
            reward,
            next_state.astype(np.float32),
            task_done,
            policy_mean=actor_info.get("mean") if actor_info else None,
            policy_residual=residual,
            policy_phi=actor_info.get("phi") if actor_info else None,
        )
        self.steps += 1
        if (
            self.steps % self.train_interval == 0
            and self.buffer.size() >= self.batch_size
        ):
            self.train()
        if task_done:
            self.buffer.save(self.save_path)

    def train(self) -> None:
        size = self.buffer.size()
        if size < self.batch_size:
            return
        indices = np.random.choice(size, self.batch_size, replace=False)
        states = np.stack([self.buffer.states[i] for i in indices])
        rewards = np.asarray(
            [self.buffer.rewards[i] for i in indices], dtype=np.float32
        )
        next_states = np.stack(
            [self.buffer.next_states[i] for i in indices]
        )
        dones = np.asarray([self.buffer.dones[i] for i in indices], dtype=bool)
        means = np.stack([self.buffer.policy_means[i] for i in indices])
        residuals = np.stack(
            [self.buffer.policy_residuals[i] for i in indices]
        )
        phis = np.stack([self.buffer.policy_phis[i] for i in indices])

        self._init_value(states.shape[1])
        assert self.value_w is not None
        features = np.concatenate(
            [states, np.ones((len(states), 1), dtype=np.float32)], axis=1
        )
        next_features = np.concatenate(
            [
                next_states,
                np.ones((len(next_states), 1), dtype=np.float32),
            ],
            axis=1,
        )
        current_value = features @ self.value_w
        next_value = next_features @ self.value_w
        targets = rewards + self.gamma * next_value * (
            1.0 - dones.astype(np.float32)
        )
        td_errors = targets - current_value
        critic_gradient = -2.0 * features.T @ td_errors / len(states)
        self.value_w -= self.lr * critic_gradient

        self._init_actor()
        assert self.actor_w is not None
        scaled_residuals = self.residual_scale * residuals
        log_policy_gradient = (
            scaled_residuals - means
        ) / self.actor_std**2
        actor_gradient = -phis.T @ (
            log_policy_gradient * td_errors[:, None]
        ) / len(phis)
        self.actor_w -= self.actor_lr * actor_gradient
        logger.debug(
            "RL train step=%d td_loss=%.6f",
            self.steps,
            float(np.mean(td_errors**2)),
        )
