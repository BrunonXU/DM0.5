#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_mujoco_types.py
================
MuJoCo 安全保护相关数据类型定义（从 mujoco_com_guard_core.py 拆分）。

包含所有 dataclass 和类型别名，供其他子模块和上层导入使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


@dataclass(frozen=True)
class JointInfo:
    """
    MuJoCo 单自由度关节的只读描述。

    字段说明：
    - `name`:
      关节名，便于日志输出和上层命令映射。
    - `joint_id`:
      MuJoCo 内部 joint 索引。
    - `qpos_adr` / `qvel_adr`:
      该关节在 `qpos`、`qvel` 中对应的起始地址。
    - `lower` / `upper`:
      关节允许范围。若模型未设置限制，则使用一个保守默认范围。
    """

    name: str
    joint_id: int
    qpos_adr: int
    qvel_adr: int
    lower: float
    upper: float


@dataclass(frozen=True)
class ProjectionState:
    """
    当前姿态下与质心保护有关的几何状态。

    - `com_xy` 是整机质心在地面平面的投影坐标；
    - `support_polygon_xy` 是由支撑点凸包构成的支撑多边形。
    """

    com_xy: Vec2
    support_polygon_xy: Tuple[Vec2, ...]


@dataclass(frozen=True)
class SupportDiagnostic:
    """
    质心投影相对支撑多边形的诊断结果。

    - `inside`:
      当前是否仍位于安全区域内。
    - `signed_clearance`:
      带符号净空。大于等于 0 表示安全，小于 0 表示已越界或预测越界。
    - `violating_edge_index`:
      最危险边的索引。
    - `edge_start` / `edge_end`:
      最危险边的两个端点，便于可视化时高亮。
    - `edge_distances`:
      质心到各条边的符号距离序列，便于调试判定过程。
    """

    inside: bool
    signed_clearance: float
    violating_edge_index: int
    edge_start: Vec2
    edge_end: Vec2
    edge_distances: Tuple[float, ...]


@dataclass(frozen=True)
class SafetyResult:
    """
    质心保护层的最终输出。

    该结构在主循环中直接参与"是否接受候选命令"的决策。
    """

    safe: bool
    com_xy: Vec2
    support_polygon_xy: Tuple[Vec2, ...]
    support_diagnostic: SupportDiagnostic


@dataclass(frozen=True)
class CollisionPrimitive:
    """
    单个连杆碰撞包络的代理几何。

    与旧版本只有一个 `CollisionSphere` 不同，这里显式保留了代理几何的形状类型：
    - `box`:
      使用与连杆主结构相近的长方体包络。
    - `cylinder`:
      使用与连杆主结构相近的圆柱包络。
    - `sphere`:
      仅在原始 URDF 明确给出球形碰撞体时使用。

    字段说明：
    - `body_id` / `parent_body_id`:
      用于在运行时识别连杆关系，并自动跳过自身或直接相邻的父子连杆。
    - `local_center`:
      代理几何中心在 body 局部坐标系中的位置。
    - `local_axes`:
      代理几何局部坐标轴在 body 坐标系中的方向，列向量分别对应 x/y/z 轴。
    - `half_extents`:
      `box` 类型的三个半边长，其余类型可为零。
    - `radius` / `half_length`:
      `cylinder` 或 `sphere` 的尺寸参数。
    """

    link_name: str
    collision_name: str
    body_id: int
    parent_body_id: int
    primitive_type: str
    local_center: np.ndarray
    local_axes: np.ndarray
    half_extents: np.ndarray
    radius: float
    half_length: float


# 为了兼容现有主脚本中的旧变量命名，保留这个别名。
CollisionSphere = CollisionPrimitive


@dataclass(frozen=True)
class CollisionDiagnostic:
    """
    arm-body 碰撞保护层的输出。

    - `signed_clearance`:
      最近 arm-body 碰撞对的净空距离。
    - `collision_pair`:
      触发最小净空的碰撞对名称，便于报警和可视化。
    - `arm_center` / `body_center`:
      最近风险对在世界坐标系下的最近点。字段名保留旧命名，
      但语义已从"球心"升级为"代理包络上的最近点"。
    """

    safe: bool
    signed_clearance: float
    collision_pair: Optional[Tuple[str, str]]
    arm_center: Optional[Vec3]
    body_center: Optional[Vec3]
    arm_collision_flags: Optional[np.ndarray] = None
