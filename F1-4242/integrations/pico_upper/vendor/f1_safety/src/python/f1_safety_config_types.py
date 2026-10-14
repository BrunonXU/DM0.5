#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
f1_safety_config_types.py
=========================
F1 安全控制配置数据类定义（从 f1_safety_config.py 拆分）。

包含所有强类型配置 dataclass，供 f1_safety_config 及其他模块导入使用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ═══════════════════════════════════════════════════════════════
#  配置数据类（强类型，只读）
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelConfig:
    """机器人模型配置。"""
    urdf_path: str


@dataclass(frozen=True)
class CoMGuardConfig:
    """
    质心保护模块配置。

    Attributes
    ----------
    support_point_type:
        支撑点 MuJoCo 对象类型，取值 "body" / "site" / "geom"。
    support_point_names:
        支撑多边形顶点名称元组，顺序决定多边形连线方向。
    support_margin:
        质心到支撑多边形边界的最小安全距离（米）。
    """
    support_point_type:  str
    support_point_names: Tuple[str, ...]
    support_margin:      float


@dataclass(frozen=True)
class CollisionGuardConfig:
    """
    碰撞检测模块配置。

    Attributes
    ----------
    collision_margin:
        碰撞净空阈值（米）；负值允许轻微几何重叠。
    arm_link_prefixes:
        手臂侧 link 名前缀元组。
    body_link_prefixes:
        机身侧 link 名前缀元组。
    excluded_pairs:
        排除检测的 (arm_link, body_link) 名称对元组。
    arm_tip_extension:
        手臂末端 link（arm_*_Link7）碰撞代理沿远端方向的加长量（米）。
        URDF 中未建模手/夹爪，用该值把末端包络朝远端延长以覆盖手部；
        0.0 表示不加长。
    arm_diameter_inflation:
        全部手臂 link（arm_*_Link1~7）碰撞代理的直径膨胀量（米）。
        用于补偿代理拟合误差、线缆和软包皮等未建模外形；
        表面外扩量为该值的一半，0.0 表示不膨胀。
    """
    collision_margin:       float
    arm_link_prefixes:      Tuple[str, ...]
    body_link_prefixes:     Tuple[str, ...]
    excluded_pairs:         Tuple[Tuple[str, str], ...]
    arm_tip_extension:      float = 0.0
    arm_diameter_inflation: float = 0.0


@dataclass(frozen=True)
class JointLimitEntry:
    """单个关节的硬件限位（度）。"""
    name:  str
    lower: float   # 硬件限位下界（度）
    upper: float   # 硬件限位上界（度）


@dataclass(frozen=True)
class JointLimitsConfig:
    """
    关节限位配置。

    Attributes
    ----------
    delta_deg:
        软限位内缩裕量（度），实际软限位 = [lower+delta, upper-delta]。
    limits:
        各关节限位字典，key 为真机关节名。
    """
    delta_deg: float
    limits:    Dict[str, JointLimitEntry]

    def get_soft_limits_rad(self, joint_name: str) -> Optional[Tuple[float, float]]:
        """
        返回指定关节的软限位（弧度）元组 (lower_rad, upper_rad)。
        未找到时返回 None。
        """
        entry = self.limits.get(joint_name)
        if entry is None:
            return None
        lo = math.radians(entry.lower + self.delta_deg)
        hi = math.radians(entry.upper - self.delta_deg)
        return lo, hi


@dataclass(frozen=True)
class NetworkConfig:
    """UDP 网络配置。"""
    local_ip:      str
    udp_port:      int
    ip_config_path: str  # ip_config.py 路径，如存在则优先使用其中的 IP/Port


@dataclass(frozen=True)
class VisualizationConfig:
    """MuJoCo 可视化参数。"""
    support_line_width:      float
    com_radius:              float
    collision_line_width:    float
    collision_marker_radius: float
    print_interval_sec:      float


@dataclass(frozen=True)
class F1SafetyConfig:
    """
    F1 安全控制完整配置，由 load_config() 返回。

    包含所有子模块配置，可直接作为参数传入各功能模块。
    """
    model:         ModelConfig
    com:           CoMGuardConfig
    collision:     CollisionGuardConfig
    joint_map:     Dict[str, str]       # 真机名 → URDF 名
    joint_limits:  JointLimitsConfig
    network:       NetworkConfig
    visualization: VisualizationConfig
