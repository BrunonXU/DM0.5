#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
f1_collision_guard.py
=====================
手臂-机身碰撞检测功能模块（从 f1_safety_modules.py 拆分）。

包含 CollisionGuardResult 和 CollisionGuard 类。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import mujoco
import numpy as np

from mujoco_com_guard_core import (
    CollisionDiagnostic,
    load_collision_spheres_from_urdf,
    resolve_joint_infos,
)

# 配置类型
from f1_safety_config import CollisionGuardConfig


# ═══════════════════════════════════════════════════════════════
#  CollisionGuardResult  ──  碰撞检查结果
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CollisionGuardResult:
    """
    碰撞检测单次检查结果。

    Attributes
    ----------
    safe:
        True 表示网格凸包净空（扣除手臂外扩）及末端包络通过 collision_margin。
    signed_clearance:
        最近碰撞代理对之间的带符号距离（米）；正值=安全，负值=碰撞。
    collision_pair:
        最近碰撞代理名称对 (arm_link, body_link)；安全时可为 None。
    arm_center:
        手臂侧最近代理球中心坐标；无碰撞候选时为 None。
    body_center:
        机身侧最近代理球中心坐标；无碰撞候选时为 None。
    arm_collision_flags:
        URDF 左右臂 14 维碰撞标志（按 arm_L_joint1..7, arm_R_joint1..7 顺序）。
    raw:
        底层 CollisionDiagnostic 原始对象。
    """
    safe:              bool
    signed_clearance:  float
    collision_pair:    Optional[Tuple[str, str]]
    arm_center:        Optional[Tuple[float, float, float]]
    body_center:       Optional[Tuple[float, float, float]]
    arm_collision_flags: np.ndarray
    raw:               CollisionDiagnostic


# ═══════════════════════════════════════════════════════════════
#  CollisionGuard  ──  碰撞检测功能模块
# ═══════════════════════════════════════════════════════════════

class CollisionGuard:
    """
    手臂-机身碰撞检测功能模块（Coal 网格凸包；Link7 保留加长包络）。

    职责：给定一组期望关节角（弧度），检查手臂代理球与机身代理球
    之间的最小净空是否满足安全要求，并返回 CollisionGuardResult。
    """

    def __init__(
        self,
        model:               mujoco.MjModel,
        arm_spheres,
        body_spheres,
        collision_margin:    float,
        excluded_pairs:      Sequence[Tuple[str, str]],
        joint_name_map:      Optional[Dict[str, str]] = None,
        controlled_joints:   Optional[Sequence[str]]  = None,
        arm_padding: float = 0.0,
    ) -> None:
        """
        Parameters
        ----------
        model:
            MuJoCo 模型对象。
        arm_spheres:
            手臂碰撞代理球列表（由 load_collision_spheres_from_urdf 返回）。
        body_spheres:
            机身碰撞代理球列表。
        collision_margin:
            碰撞净空阈值（米）；负值允许轻微几何重叠。
        excluded_pairs:
            排除检测的 (arm_link, body_link) 名称对列表。
        joint_name_map:
            真机名 → URDF 名映射；None 时不做名称转换。
        controlled_joints:
            受控关节名列表；None 时自动解析全部 1-DoF 关节。
        """
        self._model            = model
        self._trial_data       = mujoco.MjData(model)
        self._arm_spheres      = arm_spheres
        self._body_spheres     = body_spheres
        self._collision_margin = collision_margin
        self._excluded_pairs   = tuple(excluded_pairs)
        self._joint_name_map   = joint_name_map or {}
        from f1_mesh_collision import MeshCollisionEvaluator
        self._mesh_evaluator = MeshCollisionEvaluator(
            model, arm_spheres, body_spheres, excluded_pairs, arm_padding)

        self._joint_infos = resolve_joint_infos(model, controlled_joints)
        if not self._joint_infos:
            raise RuntimeError("CollisionGuard: 模型中没有找到可控 1-DoF 关节")
        self._name_to_idx: Dict[str, int] = {
            ji.name: idx for idx, ji in enumerate(self._joint_infos)
        }
        mujoco.mj_forward(model, self._trial_data)
        self._last_result: Optional[CollisionGuardResult] = None
        self._last_arm_collision_flags: np.ndarray = np.zeros(14, dtype=bool)

    # ── 工厂方法：从配置对象构建 ──────────────────────────────

    @classmethod
    def from_config(
        cls,
        model:          mujoco.MjModel,
        col_cfg:        CollisionGuardConfig,
        urdf_path:      str,
        joint_name_map: Optional[Dict[str, str]] = None,
    ) -> "CollisionGuard":
        """
        从 CollisionGuardConfig 配置对象构建实例。

        Parameters
        ----------
        model:
            已加载的 MuJoCo 模型。
        col_cfg:
            由 load_config() 解析得到的 CollisionGuardConfig。
        urdf_path:
            URDF 文件路径（用于加载碰撞代理球几何）。
        joint_name_map:
            真机名 → URDF 名映射。
        """
        arm_spheres, body_spheres = load_collision_spheres_from_urdf(
            model=model,
            urdf_path=urdf_path,
            arm_link_prefixes=col_cfg.arm_link_prefixes,
            body_link_prefixes=col_cfg.body_link_prefixes,
            arm_tip_extension=getattr(col_cfg, "arm_tip_extension", 0.0),
            arm_diameter_inflation=getattr(col_cfg, "arm_diameter_inflation", 0.0),
        )
        if not arm_spheres or not body_spheres:
            raise RuntimeError(
                "CollisionGuard: 从 URDF 加载手臂/机身碰撞几何失败，"
                "请检查 arm_link_prefixes / body_link_prefixes 配置"
            )
        return cls(
            model=model,
            arm_spheres=arm_spheres,
            body_spheres=body_spheres,
            collision_margin=col_cfg.collision_margin,
            excluded_pairs=col_cfg.excluded_pairs,
            joint_name_map=joint_name_map,
            arm_padding=0.5 * col_cfg.arm_diameter_inflation,
        )

    # ── 主接口 ────────────────────────────────────────────────

    def check_collision_safety(self, desired_joint_cmd: Dict[str, float]) -> CollisionGuardResult:
        """
        对期望关节位置做碰撞安全预测。

        Parameters
        ----------
        desired_joint_cmd:
            {关节名 → 弧度值} 字典，键为真机名或 URDF 名均可。

        Returns
        -------
        CollisionGuardResult
            包含 safe 标志、signed_clearance、collision_pair 等字段。
        """
        _apply_cmd_dict(
            self._trial_data, self._model,
            self._joint_infos, self._name_to_idx,
            desired_joint_cmd, self._joint_name_map,
        )
        diag = self._mesh_evaluator.evaluate(self._trial_data, self._collision_margin)
        flags = diag.arm_collision_flags if diag.arm_collision_flags is not None else np.zeros(14, dtype=bool)
        result = CollisionGuardResult(
            safe=diag.safe,
            signed_clearance=diag.signed_clearance,
            collision_pair=diag.collision_pair,
            arm_center=diag.arm_center,
            body_center=diag.body_center,
            arm_collision_flags=flags,
            raw=diag,
        )
        self._last_result = result
        self._last_arm_collision_flags = flags
        return result

    # 兼容别名：旧代码中 guard.check(...) 仍可用
    check = check_collision_safety

    @property
    def last_result(self) -> Optional[CollisionGuardResult]:
        """最近一次 check_collision_safety() 的结果。"""
        return self._last_result

    @property
    def last_arm_collision_flags(self) -> np.ndarray:
        """最近一次碰撞检查产生的 URDF 左右臂 14 维碰撞标志。"""
        return self._last_arm_collision_flags.copy()

    @property
    def arm_spheres(self):
        return self._arm_spheres

    @property
    def body_spheres(self):
        return self._body_spheres

    @property
    def trial_data(self) -> mujoco.MjData:
        return self._trial_data


# ═══════════════════════════════════════════════════════════════
#  内部工具函数（供本模块和 f1_safety_modules 共用）
# ═══════════════════════════════════════════════════════════════

def _apply_cmd_dict(
    data:          mujoco.MjData,
    model:         mujoco.MjModel,
    joint_infos,
    name_to_idx:   Dict[str, int],
    cmd_dict:      Dict[str, float],
    joint_name_map: Optional[Dict[str, str]] = None,
) -> None:
    """
    将关节命令字典写入 MuJoCo ``data.qpos`` 并执行前向运动学。
    """
    cmd_array = np.zeros(len(joint_infos), dtype=float)
    for raw_name, rad in cmd_dict.items():
        urdf_name = joint_name_map.get(raw_name, raw_name) if joint_name_map else raw_name
        idx = name_to_idx.get(urdf_name)
        if idx is not None:
            cmd_array[idx] = rad
    # Geometry must evaluate the actual supplied pose, not a different pose
    # silently clipped to historical URDF ranges. This is FK, not a motor command
    # or a replacement for hardware command-limit enforcement.
    if not np.all(np.isfinite(cmd_array)):
        raise ValueError('non-finite collision joint positions')
    for joint, target in zip(joint_infos, cmd_array):
        data.qpos[joint.qpos_adr] = float(target)
        data.qvel[joint.qvel_adr] = 0.0
    mujoco.mj_forward(model, data)
