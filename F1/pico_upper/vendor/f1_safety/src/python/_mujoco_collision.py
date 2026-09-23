#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_mujoco_collision.py
====================
MuJoCo 碰撞检测核心计算（从 mujoco_com_guard_core.py 拆分）。

包含：碰撞代理加载入口、碰撞安全评估、采样与距离计算。
几何解析工具位于 _mujoco_geometry_utils.py。
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

import mujoco
import numpy as np

from _mujoco_types import (
    CollisionDiagnostic,
    CollisionPrimitive,
    Vec3,
)

# 手臂末端 link 名称正则（URDF 中无手/夹爪 link，以此定位需加长的代理）
_ARM_TIP_LINK_PATTERN = re.compile(r"^arm_[LR]_Link7$", re.IGNORECASE)

# 从几何工具模块导入 URDF 解析和代理构建函数
from _mujoco_geometry_utils import (
    _build_collision_primitive,
    _classify_collision_link,
    _extract_enabled_collision_names,
    _find_joint_for_body,
)


# ═══════════════════════════════════════════════════════════════
#  公共接口
# ═══════════════════════════════════════════════════════════════

def load_collision_spheres_from_urdf(
    model: mujoco.MjModel,
    urdf_path: str,
    arm_link_prefixes: Sequence[str],
    body_link_prefixes: Sequence[str],
    arm_tip_extension: float = 0.0,
    arm_diameter_inflation: float = 0.0,
) -> Tuple[List[CollisionPrimitive], List[CollisionPrimitive]]:
    """
    从原始 URDF 中提取 collision 几何，并构建 arm/body 两组碰撞代理。

    Parameters
    ----------
    arm_tip_extension:
        手臂末端 link（arm_*_Link7）代理沿远端方向的加长量（米）。
        URDF 未建模手/夹爪，默认 0.0 不加长。
    arm_diameter_inflation:
        全部手臂 link 代理的直径膨胀量（米），默认 0.0 不膨胀。
        仅作用于 arm 组，body 组不受影响。
    """
    from pathlib import Path
    from xml.etree import ElementTree as ET

    urdf_file = Path(urdf_path).resolve()
    root = ET.parse(urdf_file).getroot()

    arm_spheres: List[CollisionPrimitive] = []
    body_spheres: List[CollisionPrimitive] = []
    for link_elem in root.findall("link"):
        link_name = link_elem.attrib.get("name")
        if not link_name:
            continue

        group = _classify_collision_link(
            link_name=link_name,
            arm_link_prefixes=arm_link_prefixes,
            body_link_prefixes=body_link_prefixes,
        )
        if group is None:
            continue

        body_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, link_name)
        if body_id < 0:
            continue

        enabled_names = _extract_enabled_collision_names(link_elem)
        for collision_elem in link_elem.findall("collision"):
            collision_name = collision_elem.attrib.get("name", f"{link_name}_collision")
            if enabled_names and collision_name not in enabled_names:
                continue

            sphere = _build_collision_primitive(
                model=model,
                urdf_path=urdf_file,
                link_name=link_name,
                collision_name=collision_name,
                body_id=body_id,
                collision_elem=collision_elem,
            )
            if sphere is None:
                continue
            if group == "arm":
                if arm_diameter_inflation > 0.0:
                    sphere = _inflate_primitive(sphere, arm_diameter_inflation)
                if arm_tip_extension > 0.0 and _ARM_TIP_LINK_PATTERN.match(link_name):
                    sphere = _extend_primitive_distally(sphere, arm_tip_extension)
                arm_spheres.append(sphere)
            else:
                body_spheres.append(sphere)

    return arm_spheres, body_spheres


def _inflate_primitive(
    primitive: CollisionPrimitive,
    diameter_inflation: float,
) -> CollisionPrimitive:
    """
    将代理几何按“直径增加 diameter_inflation 米”关于中心对称膨胀。

    直径增量的一半即表面外扩量：
    - `sphere` / `cylinder`:
      `radius += diameter_inflation / 2`；圆柱长度（half_length）不变，
      因为“直径”只描述横截面。
    - `box`:
      三个半边长各 `+= diameter_inflation / 2`（均匀外扩）。

    中心与朝向保持不变。
    """
    offset = 0.5 * diameter_inflation
    if primitive.primitive_type in ("sphere", "cylinder"):
        return replace(primitive, radius=primitive.radius + offset)
    return replace(
        primitive,
        half_extents=np.asarray(primitive.half_extents, dtype=float) + offset,
    )


def _extend_primitive_distally(
    primitive: CollisionPrimitive,
    extension: float,
) -> CollisionPrimitive:
    """
    将代理几何沿“远离关节原点”的方向加长 `extension` 米。

    为了只往远端长（不往近端反向长、避免压到父 link），尺寸只增加 extension/2，
    同时把中心沿同一方向平移 extension/2，因此近端端面保持不动、
    远端端面外推 extension。

    远端方向的判定：代理中心相对 body 原点（即该 link 的关节位置）已经
    偏向远端，所以取“中心在主轴上的投影符号”作为远端正方向。
    若投影几乎为 0（代理正好包着关节），则退化为对称加长。
    """
    if primitive.primitive_type == "sphere":
        # 球无方向性，只能整体撑大半径
        return replace(primitive, radius=primitive.radius + 0.5 * extension)

    if primitive.primitive_type == "cylinder":
        axis_index = 2                       # 圆柱轴向固定为局部 z
        new_half_length = primitive.half_length + 0.5 * extension
        new_half_extents = primitive.half_extents
    else:                                    # box：沿最长的那条边加长
        axis_index = int(np.argmax(primitive.half_extents))
        new_half_extents = np.array(primitive.half_extents, dtype=float)
        new_half_extents[axis_index] += 0.5 * extension
        new_half_length = primitive.half_length

    axis_local = np.asarray(primitive.local_axes, dtype=float)[:, axis_index]
    projection = float(np.dot(primitive.local_center, axis_local))
    if abs(projection) < 1e-6:
        # 分辨不出远端方向：不平移中心，退化为两端各长 extension/2
        new_center = primitive.local_center
    else:
        distal_dir = axis_local * (1.0 if projection > 0.0 else -1.0)
        new_center = np.asarray(primitive.local_center, dtype=float) + distal_dir * (0.5 * extension)

    return replace(
        primitive,
        local_center=new_center,
        half_extents=new_half_extents,
        half_length=new_half_length,
    )


def evaluate_arm_body_collision_safety(
    data: mujoco.MjData,
    arm_spheres: Sequence[CollisionPrimitive],
    body_spheres: Sequence[CollisionPrimitive],
    margin: float,
    excluded_link_pairs: Sequence[Tuple[str, str]] = (),
) -> CollisionDiagnostic:
    """
    逐连杆遍历"手臂代理集合"与"机身代理集合"之间是否发生碰撞。
    """
    if not arm_spheres or not body_spheres:
        raise ValueError("collision primitive list must not be empty")

    excluded_pairs = {
        frozenset((first, second)) for first, second in excluded_link_pairs
    }
    minimum_distance: Optional[float] = None
    minimum_pair: Optional[Tuple[str, str]] = None
    nearest_arm_center: Optional[Vec3] = None
    nearest_body_center: Optional[Vec3] = None

    arm_min_clearances: Dict[str, float] = {
        arm_sphere.link_name: float("inf")
        for arm_sphere in arm_spheres
    }

    arm_poses = [
        _primitive_pose_world(data, arm_sphere)
        for arm_sphere in arm_spheres
    ]
    body_poses = [
        _primitive_pose_world(data, body_sphere)
        for body_sphere in body_spheres
    ]
    arm_radii = [_primitive_approximate_radius(arm_sphere) for arm_sphere in arm_spheres]
    body_radii = [_primitive_approximate_radius(body_sphere) for body_sphere in body_spheres]

    def _link_name_to_joint_index(link_name: str) -> Optional[int]:
        match = re.match(r"^arm_([LR])_Link(\d+)$", link_name, re.IGNORECASE)
        if not match:
            return None
        side = match.group(1).upper()
        joint_idx = int(match.group(2)) - 1
        if joint_idx < 0 or joint_idx >= 7:
            return None
        return joint_idx if side == "L" else 7 + joint_idx

    for arm_index, arm_sphere in enumerate(arm_spheres):
        arm_center, arm_axes = arm_poses[arm_index]
        arm_radius = arm_radii[arm_index]
        for body_index, body_sphere in enumerate(body_spheres):
            if _should_skip_primitive_pair(
                arm_sphere, body_sphere, excluded_pairs,
            ):
                continue

            body_center, body_axes = body_poses[body_index]
            if minimum_distance is not None:
                center_dist = float(np.linalg.norm(arm_center - body_center))
                if center_dist - (arm_radius + body_radii[body_index]) >= minimum_distance:
                    continue

            (
                signed_clearance,
                arm_nearest_point,
                body_nearest_point,
            ) = _primitive_pair_signed_clearance_from_pose(
                arm_center, arm_axes, arm_sphere,
                body_center, body_axes, body_sphere,
            )
            if minimum_distance is None or signed_clearance < minimum_distance:
                minimum_distance = signed_clearance
                minimum_pair = (
                    f"{arm_sphere.link_name}/{arm_sphere.collision_name}",
                    f"{body_sphere.link_name}/{body_sphere.collision_name}",
                )
                nearest_arm_center = (
                    float(arm_nearest_point[0]),
                    float(arm_nearest_point[1]),
                    float(arm_nearest_point[2]),
                )
                nearest_body_center = (
                    float(body_nearest_point[0]),
                    float(body_nearest_point[1]),
                    float(body_nearest_point[2]),
                )
            if signed_clearance < arm_min_clearances[arm_sphere.link_name]:
                arm_min_clearances[arm_sphere.link_name] = signed_clearance

    if minimum_distance is None:
        raise ValueError("no valid arm-body collision pairs were found")

    arm_collision_flags = np.zeros(14, dtype=bool)
    for link_name, clearance in arm_min_clearances.items():
        idx = _link_name_to_joint_index(link_name)
        if idx is None:
            continue
        if clearance <= margin:
            arm_collision_flags[idx] = True

    return CollisionDiagnostic(
        safe=minimum_distance > margin,
        signed_clearance=minimum_distance,
        collision_pair=minimum_pair,
        arm_center=nearest_arm_center,
        body_center=nearest_body_center,
        arm_collision_flags=arm_collision_flags,
    )


def find_collision_joint_pair(
    model: mujoco.MjModel,
    arm_link_name: str,
    body_link_name: str,
) -> Tuple[Optional[str], Optional[str]]:
    """根据碰撞对的 link 名称，查找对应的关节名称。"""
    arm_joint = _find_joint_for_body(model, arm_link_name)
    body_joint = _find_joint_for_body(model, body_link_name)
    return arm_joint, body_joint


# ═══════════════════════════════════════════════════════════════
#  内部辅助函数：坐标变换、采样、距离计算
# ═══════════════════════════════════════════════════════════════

def _primitive_approximate_radius(primitive: CollisionPrimitive) -> float:
    """返回代理几何的近似包络半径，用于快速剪枝。"""
    if primitive.primitive_type == "sphere":
        return primitive.radius
    if primitive.primitive_type == "cylinder":
        return math.hypot(primitive.radius, primitive.half_length)
    return float(np.linalg.norm(primitive.half_extents))


def _primitive_pose_world(
    data: mujoco.MjData,
    primitive: CollisionPrimitive,
) -> Tuple[np.ndarray, np.ndarray]:
    """将代理几何从 body 局部系转换到世界坐标系。"""
    body_rotation = np.asarray(data.xmat[primitive.body_id], dtype=float).reshape(3, 3)
    body_translation = np.asarray(data.xpos[primitive.body_id], dtype=float)
    center_world = body_translation + body_rotation @ primitive.local_center
    axes_world = body_rotation @ primitive.local_axes
    return center_world, axes_world


def _primitive_pair_signed_clearance_from_pose(
    first_center: np.ndarray,
    first_axes: np.ndarray,
    first: CollisionPrimitive,
    second_center: np.ndarray,
    second_axes: np.ndarray,
    second: CollisionPrimitive,
) -> Tuple[float, np.ndarray, np.ndarray]:
    """估算两个已知世界坐标姿态代理之间的签名净空。"""
    minimum_distance = float("inf")
    nearest_first = np.array(first_center, dtype=float)
    nearest_second = np.array(second_center, dtype=float)

    for sample_point in _sample_primitive_points(first_center, first_axes, first):
        signed_distance, closest_point = _point_to_primitive_signed_distance(
            sample_point, second_center, second_axes, second,
        )
        if signed_distance < minimum_distance:
            minimum_distance = signed_distance
            nearest_first = sample_point
            nearest_second = closest_point

    for sample_point in _sample_primitive_points(second_center, second_axes, second):
        signed_distance, closest_point = _point_to_primitive_signed_distance(
            sample_point, first_center, first_axes, first,
        )
        if signed_distance < minimum_distance:
            minimum_distance = signed_distance
            nearest_first = closest_point
            nearest_second = sample_point

    return minimum_distance, nearest_first, nearest_second


def _primitive_pair_signed_clearance(
    data: mujoco.MjData,
    first: CollisionPrimitive,
    second: CollisionPrimitive,
) -> Tuple[float, np.ndarray, np.ndarray]:
    """估算两个代理几何之间的带符号净空。"""
    first_center, first_axes = _primitive_pose_world(data, first)
    second_center, second_axes = _primitive_pose_world(data, second)
    return _primitive_pair_signed_clearance_from_pose(
        first_center, first_axes, first,
        second_center, second_axes, second,
    )


def _should_skip_primitive_pair(
    first: CollisionPrimitive,
    second: CollisionPrimitive,
    excluded_pairs: set[frozenset[str]],
) -> bool:
    """判断两个代理是否应被跳过。"""
    if first.body_id == second.body_id:
        return True
    if first.body_id == second.parent_body_id or second.body_id == first.parent_body_id:
        return True
    return frozenset((first.link_name, second.link_name)) in excluded_pairs


def _sample_primitive_points(
    center_world: np.ndarray,
    axes_world: np.ndarray,
    primitive: CollisionPrimitive,
) -> List[np.ndarray]:
    """为代理几何生成一组有代表性的采样点。"""
    points: List[np.ndarray] = [np.array(center_world, dtype=float)]

    if primitive.primitive_type == "sphere":
        for axis_index in range(3):
            axis_vec = axes_world[:, axis_index]
            points.append(center_world + axis_vec * primitive.radius)
            points.append(center_world - axis_vec * primitive.radius)
        return points

    if primitive.primitive_type == "cylinder":
        radial_dirs = []
        for angle in np.linspace(0.0, 2.0 * math.pi, num=8, endpoint=False):
            radial_dirs.append(
                math.cos(angle) * axes_world[:, 0] + math.sin(angle) * axes_world[:, 1]
            )
        for z_value in (-primitive.half_length, 0.0, primitive.half_length):
            axial_point = center_world + axes_world[:, 2] * z_value
            points.append(axial_point)
            for radial_dir in radial_dirs:
                points.append(axial_point + radial_dir * primitive.radius)
        return points

    # box 代理使用角点、面心和边中点采样。
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                local = np.array(
                    [sx * primitive.half_extents[0], sy * primitive.half_extents[1], sz * primitive.half_extents[2]],
                    dtype=float,
                )
                points.append(center_world + axes_world @ local)

    for axis_index in range(3):
        for sign in (-1.0, 1.0):
            local = np.zeros(3, dtype=float)
            local[axis_index] = sign * primitive.half_extents[axis_index]
            points.append(center_world + axes_world @ local)

    edge_index_sets = ((0, 1), (0, 2), (1, 2))
    for first_axis, second_axis in edge_index_sets:
        third_axis = ({0, 1, 2} - {first_axis, second_axis}).pop()
        for sign_first in (-1.0, 1.0):
            for sign_second in (-1.0, 1.0):
                local = np.zeros(3, dtype=float)
                local[first_axis] = sign_first * primitive.half_extents[first_axis]
                local[second_axis] = sign_second * primitive.half_extents[second_axis]
                local[third_axis] = 0.0
                points.append(center_world + axes_world @ local)

    return points


def _point_to_primitive_signed_distance(
    point_world: np.ndarray,
    primitive_center_world: np.ndarray,
    primitive_axes_world: np.ndarray,
    primitive: CollisionPrimitive,
) -> Tuple[float, np.ndarray]:
    """计算"世界系点 -> 代理几何"的签名距离，并返回代理上的最近点。"""
    if primitive.primitive_type == "sphere":
        offset = point_world - primitive_center_world
        norm = float(np.linalg.norm(offset))
        if norm > 1e-8:
            nearest = primitive_center_world + offset / norm * primitive.radius
        else:
            nearest = primitive_center_world + primitive_axes_world[:, 0] * primitive.radius
        return norm - primitive.radius, nearest

    local_point = primitive_axes_world.T @ (point_world - primitive_center_world)
    if primitive.primitive_type == "cylinder":
        return _point_to_cylinder_signed_distance(
            local_point, primitive_center_world, primitive_axes_world,
            primitive.radius, primitive.half_length,
        )
    return _point_to_box_signed_distance(
        local_point, primitive_center_world, primitive_axes_world, primitive.half_extents,
    )


def _point_to_box_signed_distance(
    local_point: np.ndarray,
    center_world: np.ndarray,
    axes_world: np.ndarray,
    half_extents: np.ndarray,
) -> Tuple[float, np.ndarray]:
    """计算点到 OBB（有向长方体）的签名距离。"""
    q = np.abs(local_point) - half_extents
    outside = np.maximum(q, 0.0)
    outside_distance = float(np.linalg.norm(outside))
    inside_distance = min(float(np.max(q)), 0.0)
    signed_distance = outside_distance + inside_distance

    clipped = np.clip(local_point, -half_extents, half_extents)
    if signed_distance >= 0.0:
        closest_local = clipped
    else:
        closest_local = np.array(local_point, dtype=float)
        remaining = half_extents - np.abs(local_point)
        axis_index = int(np.argmin(remaining))
        sign = 1.0 if local_point[axis_index] >= 0.0 else -1.0
        closest_local[axis_index] = sign * half_extents[axis_index]

    closest_world = center_world + axes_world @ closest_local
    return signed_distance, closest_world


def _point_to_cylinder_signed_distance(
    local_point: np.ndarray,
    center_world: np.ndarray,
    axes_world: np.ndarray,
    radius: float,
    half_length: float,
) -> Tuple[float, np.ndarray]:
    """计算点到有限长圆柱的签名距离。"""
    radial = local_point[:2]
    radial_norm = float(np.linalg.norm(radial))
    q = np.array(
        [radial_norm - radius, abs(float(local_point[2])) - half_length],
        dtype=float,
    )
    outside = np.maximum(q, 0.0)
    outside_distance = float(np.linalg.norm(outside))
    inside_distance = min(float(np.max(q)), 0.0)
    signed_distance = outside_distance + inside_distance

    if radial_norm > 1e-8:
        radial_dir = radial / radial_norm
    else:
        radial_dir = np.array([1.0, 0.0], dtype=float)

    if signed_distance >= 0.0:
        clamped_z = float(np.clip(local_point[2], -half_length, half_length))
        if abs(local_point[2]) > half_length and radial_norm <= radius:
            clamped_radial = radial
        else:
            clamped_radial = radial_dir * min(radial_norm, radius)
    else:
        if q[0] >= q[1]:
            clamped_z = float(local_point[2])
            clamped_radial = radial_dir * radius
        else:
            clamped_z = math.copysign(half_length, float(local_point[2]))
            clamped_radial = radial

    closest_local = np.array(
        [clamped_radial[0], clamped_radial[1], clamped_z],
        dtype=float,
    )
    closest_world = center_world + axes_world @ closest_local
    return signed_distance, closest_world
