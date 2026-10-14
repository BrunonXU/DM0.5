#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_safety_protector.py
====================
SafetyProtector 类实现（从 com_and_collision_protect_modules 拆出）。

本模块仅承担 SafetyProtector 类的实现，不改变任何调用方式。
上层代码继续从 ``com_and_collision_protect_modules`` 导入 SafetyProtector，
后者会 re-export 本模块中的实现。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ============================================================
#  常量：F1 全身关节布局（与 F1_state_viewer / facade 一致）
# ============================================================

LEFT_JOINT_NAMES  = [f"arm_l_j{i}" for i in range(1, 8)]
RIGHT_JOINT_NAMES = [f"arm_r_j{i}" for i in range(1, 8)]
HEAD_JOINT_NAMES  = [f"head_j{i}" for i in range(1, 4)]
BODY_JOINT_NAMES  = [f"body_j{i}" for i in range(1, 6)]
ALL_JOINT_NAMES   = LEFT_JOINT_NAMES + RIGHT_JOINT_NAMES + HEAD_JOINT_NAMES + BODY_JOINT_NAMES

# 底盘轮子关节（URDF 原生名，yaml joint_map 中未配置，须直接使用 URDF 名）
# 安全检测不参与轮子运动，所以 candidate 中统一被赋值 0 弧度。
CHASSIS_WHEEL_JOINT_NAMES = [
    "chassis_L_wheel_front_joint1", "chassis_L_wheel_front_joint2",
    "chassis_L_wheel_rear_joint1",  "chassis_L_wheel_rear_joint2",
    "chassis_R_wheel_front_joint1", "chassis_R_wheel_front_joint2",
    "chassis_R_wheel_rear_joint1",  "chassis_R_wheel_rear_joint2",
]

IDX_LEFT_ARM   = slice(0, 7)
IDX_RIGHT_ARM  = slice(7, 14)
IDX_HEAD       = slice(14, 17)
IDX_BODY       = slice(17, 22)
CMD_DIM_DEFAULT = 24


__all__ = [
    "SafetyProtector",
    "LEFT_JOINT_NAMES",
    "RIGHT_JOINT_NAMES",
    "HEAD_JOINT_NAMES",
    "BODY_JOINT_NAMES",
    "ALL_JOINT_NAMES",
    "CHASSIS_WHEEL_JOINT_NAMES",
    "IDX_LEFT_ARM",
    "IDX_RIGHT_ARM",
    "IDX_HEAD",
    "IDX_BODY",
    "CMD_DIM_DEFAULT",
]


# ============================================================
#  SafetyProtector：一站式安全保护类
# ============================================================

class SafetyProtector:
    """
    F1 机器人安全保护封装类。

    一个类实例包含：
      - 配置加载（load_safety_config）
      - URDF→MJCF 预处理
      - CoMGuard / CollisionGuard / CombinedSafetyGuard 初始化
      - 期望命令缓存（current_cmd / last_safe_cmd）
      - 安全检查（联合门 / 单模块独立路径）
      - 回退策略：当前期望不安全 → 下发上一帧未触发保护的期望命令

    典型使用
    --------
        protector = SafetyProtector(
            config_path=None,        # 为 None 时自动查找同目录默认配置
            use_com_guard=True,
            use_collision_guard=True,
        )
        protector.set_feedback(state_rx.get_positions())   # 可选：推荐每帧更新
        is_safe, arm_collision_indices, final_cmd, arm_collision_flags = protector.run(target_cmd_24d)

    输入
    ----
    target_cmd : np.ndarray, shape=(cmd_dim,)
        期望全身关节位置（默认 24 维，各区间布局同 F1_state_viewer.py）。

    输出
    ----
    (is_safe, final_cmd, arm_collision_flags, arm_collision_indices) : Tuple[bool, np.ndarray, np.ndarray, list]
        is_safe   : 本帧安全检查是否通过。
        final_cmd : 最终应下发的期望关节位置。
                    - 通过   : 返回当前期望命令（target_cmd）
                    - 未通过 : 返回上一帧 last_safe_cmd（未触发保护的期望命令）
        arm_collision_flags : 14 维布尔数组，按 URDF 左臂 arm_L_joint1..7、右臂 arm_R_joint1..7 顺序返回手臂碰撞标志。
        arm_collision_indices: 发生碰撞的手臂关节索引列表（零基，范围 0..13），按与 `arm_collision_flags` 相同的顺序。

        返回值说明
        --------
        - 若当前 target_cmd 安全：返回当前期望命令。
        - 若当前 target_cmd 不安全且检测到手臂碰撞：返回一个修正后的命令，
          其中只冻结碰撞的手臂关节，非碰撞手臂和非手臂关节仍保留最新期望值。
        - 若当前 target_cmd 不安全但未检测到手臂碰撞（例如 CoM 越界）：
          返回上一帧安全命令。
    """

    def __init__(
        self,
        config_path:        Optional[str] = None,
        use_com_guard:      bool          = True,
        use_collision_guard:bool          = True,
        cmd_dim:            int           = CMD_DIM_DEFAULT,
        verbose:            bool          = True,
    ) -> None:
        # 延迟导入 load_safety_modules，避免与 facade 循环依赖
        from com_and_collision_protect_modules import load_safety_modules  # type: ignore

        # 初始化时直接运行加载逻辑
        (self._cfg,
         self._mjcf_path,
         self._com_guard,
         self._collision_guard,
         self._safety_guard) = load_safety_modules(
            config_path=config_path,
            use_com=use_com_guard,
            use_col=use_collision_guard,
        )

        self._cmd_dim:      int   = int(cmd_dim)
        self._verbose:      bool  = bool(verbose)

        # 期望命令缓存：当前帧 / 上一帧未触发保护的安全命令
        self._current_cmd:   np.ndarray = np.zeros(self._cmd_dim, dtype=float)
        self._last_safe_cmd: np.ndarray = np.zeros(self._cmd_dim, dtype=float)

        # 可选的真机反馈（用于 candidate 的“躯干/头部/夹爪”部分以真机为准）
        self._feedback_positions: Dict[str, float] = {}

        # 统计
        self._loop_count:   int = 0
        self._reject_count: int = 0
        self._com_reject:   int = 0
        self._col_reject:   int = 0

        # 记录最近一次安全检查的详细结果（供上层日志展示使用）
        self._last_com_result       = None  # type: Optional[Any]
        self._last_collision_result = None  # type: Optional[Any]
        self._last_reason: str = "SAFE"
        self._start_time:   float = time.time()

    # ── 对外属性 ──────────────────────────────────────

    @property
    def cfg(self):
        """已加载的安全配置对象（加载失败为 None）。"""
        return self._cfg

    @property
    def mjcf_path(self) -> Optional[str]:
        """URDF 预处理后的 MJCF 路径（失败为 None），供上层复用。"""
        return self._mjcf_path

    @property
    def has_guard(self) -> bool:
        """是否启用了任意一个安全模块。"""
        return (self._safety_guard is not None
                or self._com_guard is not None
                or self._collision_guard is not None)

    @property
    def current_cmd(self) -> np.ndarray:
        """当前帧期望命令（返回 copy）。"""
        return self._current_cmd.copy()

    @property
    def last_safe_cmd(self) -> np.ndarray:
        """上一帧未触发保护的期望命令（返回 copy）。"""
        return self._last_safe_cmd.copy()

    @property
    def last_com_result(self):
        """最近一次质心检查结果（CoMGuardResult）；未检查过为 None。"""
        return self._last_com_result

    @property
    def last_collision_result(self):
        """最近一次碰撞检查结果（CollisionGuardResult）；未检查过为 None。"""
        return self._last_collision_result

    @property
    def last_reason(self) -> str:
        """最近一次安全检查的拒绝理由（SAFE 表示未拒绝）。"""
        return self._last_reason

    @property
    def stats(self) -> Dict[str, Any]:
        """返回运行统计（loop / reject / com / col / freq）。"""
        elapsed = max(time.time() - self._start_time, 1e-9)
        return {
            "loop":   self._loop_count,
            "reject": self._reject_count,
            "com":    self._com_reject,
            "col":    self._col_reject,
            "freq":   self._loop_count / elapsed,
            "reject_rate": self._reject_count / max(self._loop_count, 1) * 100.0,
        }

    # ── 反馈注入 ──────────────────────────────────────

    def set_feedback(self, positions: Dict[str, float]) -> None:
        """设置真机反馈关节位置（可选）。

        未设置时，所有 candidate 关节完全来自 target_cmd；
        设置后，躯干/头部/夹爪以真机反馈为准，手臂以期望为准。
        """
        if positions:
            self._feedback_positions = dict(positions)

    # ── 首帧安全兜底初始化 ──────────────────────────────

    def init_safe_cmd(self, cmd: np.ndarray) -> None:
        """预置回退命令 last_safe_cmd（首帧安全兜底）。

        默认情况下 last_safe_cmd 初始化为全零（机器人零位）。若首帧
        run() 就触发保护，将返回该全零命令，可能导致机器人从当前实际
        姿态突然跳变回零位，造成危险。

        本方法用于在首次调用 run() 之前，将 last_safe_cmd 预置为
        机器人当前实际姿态，从而在首帧被拒绝时安全地保持不动。

        Parameters
        ----------
        cmd : np.ndarray, shape=(cmd_dim,)
            机器人当前实际关节位置（弧度），布局与 target_cmd 一致。
        """
        arr = np.asarray(cmd, dtype=float)
        if arr.shape[0] != self._cmd_dim:
            raise ValueError(
                f"init_safe_cmd 维度应为 {self._cmd_dim}，实际为 {arr.shape[0]}"
            )
        self._last_safe_cmd = arr.copy()
        self._current_cmd   = arr.copy()

    def init_safe_cmd_from_feedback(
        self,
        positions: Dict[str, float],
        joint_order: Optional[List[str]] = None,
        in_degrees: bool = True,
    ) -> None:
        """从真机反馈字典预置回退命令 last_safe_cmd（首帧安全兜底）。

        便捷封装：将 {关节名 → 角度} 字典按 joint_order 转为弧度数组后，
        调用 init_safe_cmd()。适合在拿到真机第一帧 /joint_states 后调用。

        Parameters
        ----------
        positions   : Dict[str, float]
            真机反馈关节位置，键为关节名，值默认为度。
        joint_order : Optional[List[str]]
            关节名顺序（长度须为 cmd_dim）；为 None 时使用
            ALL_JOINT_NAMES（22 个受控关节，夹爪补 0）。
        in_degrees  : bool
            positions 的值是否为度（True）或弧度（False）。默认 True。
        """
        order = joint_order if joint_order is not None else ALL_JOINT_NAMES
        arr = np.zeros(self._cmd_dim, dtype=float)
        for i, name in enumerate(order):
            if i >= self._cmd_dim:
                break
            if name in positions:
                val = float(positions[name])
                arr[i] = np.radians(val) if in_degrees else val
        self.init_safe_cmd(arr)

    # ── 内部工具 ──────────────────────────────────────

    def _build_candidate(self, smooth: np.ndarray) -> Dict[str, float]:
        """构建供 CoMGuard / CollisionGuard 使用的全身关节命令字典。

        输出约定
        --------
        candidate 是一份 ``{关节名 → 弧度值}`` 字典，后续会被
        ``_apply_cmd_dict`` 按 ``joint_name_map`` 转为 URDF 名后写入试算
        ``MjData.qpos``。其中：

          - 左右手臂 14 个关节 → 以期望值 smooth 为准（主要检测对象）
          - 躯干 5 / 头部 3 个关节 → 同样为受控关节，随 smooth 一起下发真机，所以
            这里也以期望值无条件覆盖反馈。否则会用“滞后反馈”评估“未来姿态”，造成错判
          - 底盘 8 个轮子关节 → 统一补 0 弧度（轮子不影响质心 / 手臂-机
            身碰撞，但需为 CoMGuard 下层 ``MjData`` 提供确定的初始位姿）
          - 其他未出现在字典中的关节（如夜爪） → ``_apply_cmd_dict`` 会默认 0。
          - 真机反馈 ``self._feedback_positions`` 只作为其他不受控关节的兑底值，
            不会覆盖受控关节的期望。
        """
        # 1) 以真机反馈为底座（为空则起始为空字典）,和真机反馈的字典一致
        candidate: Dict[str, float] = (
            dict(self._feedback_positions) if self._feedback_positions else {}
        )

        # 2) 受控关节（手臂 / 躯干 / 头部）无条件以期望值覆盖反馈
        for i, name in enumerate(LEFT_JOINT_NAMES):
            candidate[name] = float(smooth[IDX_LEFT_ARM][i])
        for i, name in enumerate(RIGHT_JOINT_NAMES):
            candidate[name] = float(smooth[IDX_RIGHT_ARM][i])
        for i, name in enumerate(HEAD_JOINT_NAMES):
            candidate[name] = float(smooth[IDX_HEAD][i])
        for i, name in enumerate(BODY_JOINT_NAMES):
            candidate[name] = float(smooth[IDX_BODY][i])

        # 3) 底盘 8 个轮子关节 → 一律补 0（URDF 原生名）
        for name in CHASSIS_WHEEL_JOINT_NAMES:
            candidate[name] = 0.0

        return candidate

    def _check(self, candidate: Dict[str, float]) -> Tuple[bool, str]:
        """执行安全检查，返回 (safe, reason)。

        同时将子模块详细结果保存到 self._last_com_result /
        self._last_collision_result，供上层 (如 F1_state_viewer) 调用
        ``last_com_result`` / ``last_collision_result`` 属性读取。
        """
        safe, reason = True, "SAFE"
        self._last_com_result       = None
        self._last_collision_result = None
        try:
            if self._safety_guard is not None:
                r = self._safety_guard.check_combined_safety(candidate)
                safe, reason = r.safe, r.reason
                self._last_com_result       = r.com
                self._last_collision_result = r.collision
                if not r.com.safe:       self._com_reject += 1
                if not r.collision.safe: self._col_reject += 1
            else:
                msgs: List[str] = []
                if self._com_guard is not None:
                    cr = self._com_guard.check_com_safety(candidate)
                    self._last_com_result = cr
                    if not cr.safe:
                        safe = False
                        self._com_reject += 1
                        msgs.append(f"CoM(clearance={cr.signed_clearance:.4f}m)")
                if self._collision_guard is not None:
                    cr = self._collision_guard.check_collision_safety(candidate)
                    self._last_collision_result = cr
                    if not cr.safe:
                        safe = False
                        self._col_reject += 1
                        msgs.append(f"Collision(clearance={cr.signed_clearance:.4f}m,"
                                    f"pair={cr.collision_pair})")
                reason = "; ".join(msgs) if msgs else "SAFE"
        except Exception as exc:
            print(f"[Safety] check error: {exc}")
            # A model/backend error is not a clearance approval.
            self._last_reason = f"check_error:{exc}"
            raise RuntimeError(self._last_reason) from exc
        self._last_reason = reason
        return safe, reason

    # ── 对外运行接口 ───────────────────────────────────

    def run(
        self,
        target_cmd: np.ndarray,
    ) -> Tuple[bool, list, Optional[np.ndarray], Optional[np.ndarray]]:
        """主入口：输入期望关节位置 → 返回安全检查结果和碰撞索引。

        处理流程
        --------
          1. 记录当前期望关节命令 self._current_cmd 与上一帧 self._last_safe_cmd
          2. 构建 candidate 字典（反馈 + 期望手臂覆盖）
          3. 调用 CombinedSafetyGuard / 单模块 进行安全检查
          4. 当前期望安全 → 更新 last_safe_cmd 为当前期望，下发当前期望
             当前期望不安全 → 下发上一帧 last_safe_cmd（未触发保护的期望命令）

        返回值
        ------
        (is_safe, arm_collision_indices, final_cmd, arm_collision_flags)
          - is_safe               : 是否安全
          - arm_collision_indices : 发生碰撞时必须返回的索引列表
          - final_cmd             : 若需要则返回最终下发命令，否则可忽略
          - arm_collision_flags   : 若需要则返回 14 维碰撞标志，否则可忽略
        """
        target = np.asarray(target_cmd, dtype=float)
        if target.shape != (self._cmd_dim,) or not np.all(np.isfinite(target)):
            raise ValueError(
                f"target_cmd must contain {self._cmd_dim} finite values"
            )

        self._loop_count += 1

        # 记录当前帧的期望命令（供调试/查询）；last_safe_cmd 不在此处更新，
        # 仅在“安全检查通过”后才同步为当前期望，以保证不安全时能回退到
        # “上一帧未触发保护的期望命令”。
        self._current_cmd = target.copy()

        # Missing protection is an error, never an implicit approval.
        if not self.has_guard:
            raise RuntimeError("SafetyProtector has no initialized guard")

        # 安全检查（使用当前期望手臂 + 真机反馈躯干/头部构建 candidate）
        candidate    = self._build_candidate(target)

        safe, reason = self._check(candidate)
        arm_collision_flags = (
            self._collision_guard.last_arm_collision_flags
            if self._collision_guard is not None
            else np.zeros(14, dtype=bool)
        )
        arm_collision_indices = [int(i) for i, v in enumerate(arm_collision_flags) if v]

        if safe:
            # 当前期望安全 → 更新 last_safe_cmd 为当前期望，下发当前期望
            self._last_safe_cmd = target.copy()
            return True, arm_collision_indices, target.copy(), arm_collision_flags

        # 当前期望不安全 → 下发上一帧 last_safe_cmd（保持上一帧安全位置）
        self._reject_count += 1
        if self._verbose and self._loop_count % 20 == 1:
            t = time.time() - self._start_time
            print(f"[Safety] REJECT t={t:.2f}s | {reason}")
        return False, arm_collision_indices, self._last_safe_cmd.copy(), arm_collision_flags
