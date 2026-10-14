#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_mujoco_com.py
==============
MuJoCo 质心投影计算与支撑多边形安全判定（从 mujoco_com_guard_core.py 拆分）。

包含：关节解析、质心投影、支撑多边形构建与安全诊断。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

import mujoco
import numpy as np

from _mujoco_types import (
    JointInfo,
    ProjectionState,
    SafetyResult,
    SupportDiagnostic,
    Vec2,
)


def resolve_joint_infos(
    model: mujoco.MjModel,
    controlled_joint_names: Sequence[str] | None,
) -> List[JointInfo]:
    """
    自动筛选模型中所有 1-DoF 关节，或者使用用户手工指定的关节列表。
    """
    selected_names = list(controlled_joint_names) if controlled_joint_names else []
    if not selected_names:
        for joint_id in range(model.njnt):
            joint_type = model.jnt_type[joint_id]
            if joint_type in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE):
                selected_names.append(
                    mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, joint_id)
                )

    joint_infos: List[JointInfo] = []
    for joint_name in selected_names:
        joint_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, joint_name)
        if joint_id < 0:
            raise ValueError(f"joint '{joint_name}' not found in model")

        joint_type = model.jnt_type[joint_id]
        if joint_type not in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE):
            raise ValueError(
                f"joint '{joint_name}' is not 1-DoF, only hinge/slide joints are supported"
            )

        qpos_adr = int(model.jnt_qposadr[joint_id])
        qvel_adr = int(model.jnt_dofadr[joint_id])
        if bool(model.jnt_limited[joint_id]):
            lower = float(model.jnt_range[joint_id][0])
            upper = float(model.jnt_range[joint_id][1])
        else:
            lower = -1.0
            upper = 1.0

        joint_infos.append(
            JointInfo(
                name=joint_name,
                joint_id=joint_id,
                qpos_adr=qpos_adr,
                qvel_adr=qvel_adr,
                lower=lower,
                upper=upper,
            )
        )

    return joint_infos


def resolve_support_ids(
    model: mujoco.MjModel,
    point_type: str,
    point_names: Sequence[str],
) -> List[int]:
    """
    将支撑点名称解析为 MuJoCo 内部 ID。
    """
    type_to_obj = {
        "body": mujoco.mjtObj.mjOBJ_BODY,
        "site": mujoco.mjtObj.mjOBJ_SITE,
        "geom": mujoco.mjtObj.mjOBJ_GEOM,
    }
    if point_type not in type_to_obj:
        raise ValueError(f"unsupported support point type: {point_type}")

    object_type = type_to_obj[point_type]
    object_ids: List[int] = []
    for name in point_names:
        object_id = mujoco.mj_name2id(model, object_type, name)
        if object_id < 0:
            raise ValueError(f"{point_type} '{name}' not found in model")
        object_ids.append(object_id)
    return object_ids


def apply_joint_positions(
    data: mujoco.MjData,
    joint_infos: Sequence[JointInfo],
    targets: Sequence[float],
) -> None:
    """
    将期望关节位置直接写入 qpos，便于做快速几何试算。
    """
    for joint, target in zip(joint_infos, targets):
        data.qpos[joint.qpos_adr] = float(np.clip(target, joint.lower, joint.upper))
        # print(f"关节最小范围：{joint.lower}")
        data.qvel[joint.qvel_adr] = 0.0


def compute_com_projection_from_joint_targets(
    model: mujoco.MjModel,
    base_data: mujoco.MjData,
    joint_infos: Sequence[JointInfo],
    joint_targets: Sequence[float],
    point_type: str,
    support_ids: Sequence[int],
) -> ProjectionState:
    """
    根据候选关节命令构造试算姿态，并计算质心在平面的投影。
    """
    trial_data = mujoco.MjData(model)
    trial_data.qpos[:] = base_data.qpos
    trial_data.qvel[:] = base_data.qvel
    apply_joint_positions(trial_data, joint_infos, joint_targets)
    mujoco.mj_forward(model, trial_data)
    return compute_projection_state(model, trial_data, point_type, support_ids)


def compute_projection_state(
    model: mujoco.MjModel,
    data: mujoco.MjData,
    point_type: str,
    support_ids: Sequence[int],
) -> ProjectionState:
    """
    读取当前状态下的质心平面投影和支撑多边形。
    """
    support_points_xy = _get_support_points_xy(data, point_type, support_ids)
    support_polygon_xy = _convex_hull(support_points_xy)
    com_xy = _compute_com_xy(model, data)
    return ProjectionState(
        com_xy=com_xy,
        support_polygon_xy=support_polygon_xy,
    )


def evaluate_projection_safety(
    projection_state: ProjectionState,
    margin: float,
) -> SafetyResult:
    """
    根据质心投影和支撑多边形，判断是否超出安全区域。
    """
    diagnostic = diagnose_support_state(
        point=projection_state.com_xy,
        polygon=projection_state.support_polygon_xy,
        margin=margin,
    )
    return SafetyResult(
        safe=diagnostic.inside,
        com_xy=projection_state.com_xy,
        support_polygon_xy=projection_state.support_polygon_xy,
        support_diagnostic=diagnostic,
    )


def diagnose_support_state(
    point: Vec2,
    polygon: Sequence[Vec2],
    margin: float,
) -> SupportDiagnostic:
    """
    计算质心投影相对于支撑多边形的净空和越界边信息。
    """
    if len(polygon) < 3:
        raise ValueError("support polygon must contain at least 3 points")

    # 计算质心投影到每条边（有向向量）的符号距离
    #     d > 0：质心在边的左侧
    # d < 0：质心在边的右侧
    # |d|：质心到边所在直线的垂直距离
    distances = tuple(
        _signed_distance_to_edge(point, polygon[idx], polygon[(idx + 1) % len(polygon)])
        for idx in range(len(polygon))
    )

    # 通过两个方向计算净空，取大的结果
    ccw_clearance = min(distances) + margin
    cw_clearance = min((-value for value in distances)) + margin

    if ccw_clearance >= cw_clearance:   #ccw方向正确
        signed_clearance = float(ccw_clearance)
        edge_values = distances
        violating_edge_index = int(np.argmin(edge_values))
    else:       #cw方向正确
        signed_clearance = float(cw_clearance)
        edge_values = tuple(-value for value in distances)
        violating_edge_index = int(np.argmin(edge_values))

    return SupportDiagnostic(
        inside=signed_clearance >= 0.0,
        signed_clearance=signed_clearance,
        violating_edge_index=violating_edge_index,
        edge_start=polygon[violating_edge_index],
        edge_end=polygon[(violating_edge_index + 1) % len(polygon)],
        edge_distances=distances,
    )


# ═══════════════════════════════════════════════════════════════
#  内部辅助函数
# ═══════════════════════════════════════════════════════════════

def _get_support_points_xy(
    data: mujoco.MjData,
    point_type: str,
    object_ids: Sequence[int],
) -> Tuple[Vec2, ...]:
    """
    根据支撑点类型，从 MuJoCo 状态中读取支撑点平面坐标。
    """
    points: List[Vec2] = []
    for object_id in object_ids:
        if point_type == "body":
            pos = data.xpos[object_id]
        elif point_type == "site":
            pos = data.site_xpos[object_id]
        else:
            pos = data.geom_xpos[object_id]
        points.append((float(pos[0]), float(pos[1])))
    return tuple(points)


def _compute_com_xy(model: mujoco.MjModel, data: mujoco.MjData) -> Vec2:
    """
    计算整机质心在 XY 平面上的投影。
    """
    masses = np.asarray(model.body_mass)
    positions = np.asarray(data.xipos)
    total_mass = float(np.sum(masses))
    if total_mass <= 0.0:
        raise RuntimeError("model total mass must be positive")

    com = np.sum(positions * masses[:, None], axis=0) / total_mass
    return float(com[0]), float(com[1])


def _cross(o: Vec2, a: Vec2, b: Vec2) -> float:
    """二维叉积，常用于凸包构造和转向判断。"""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _convex_hull(points: Iterable[Vec2]) -> Tuple[Vec2, ...]:
    """使用单调链算法构造二维点集的凸包。"""
    unique_points = sorted(set(points))
    if len(unique_points) <= 1:
        return tuple(unique_points)

    lower: List[Vec2] = []
    for point in unique_points:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)

    upper: List[Vec2] = []
    for point in reversed(unique_points):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)

    return tuple(lower[:-1] + upper[:-1])


def _signed_distance_to_edge(point: Vec2, start: Vec2, end: Vec2) -> float:
    """计算点到有向边的符号距离。"""
    edge = np.array([end[0] - start[0], end[1] - start[1]], dtype=float)
    rel = np.array([point[0] - start[0], point[1] - start[1]], dtype=float)
    edge_len = float(np.linalg.norm(edge))
    if edge_len == 0.0:
        return float(np.linalg.norm(rel))
    return float((edge[0] * rel[1] - edge[1] * rel[0]) / edge_len)
