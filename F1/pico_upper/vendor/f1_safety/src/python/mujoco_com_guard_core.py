from __future__ import annotations

"""
MuJoCo 质心保护与碰撞保护几何核心库。

本文件是薄包装层，从拆分的子模块中 re-export 所有公共符号，
确保现有 ``from mujoco_com_guard_core import XXX`` 等导入语句不受影响。

拆分子模块：
- ``_mujoco_types``      : 数据类型定义（JointInfo, CollisionPrimitive 等）
- ``_mujoco_com``        : 质心投影计算、支撑多边形、关节解析
- ``_mujoco_collision``  : 碰撞检测、URDF 碰撞几何加载

本文件保留：
- ``render_collision_primitives()``
- ``_render_single_primitive()``
"""

# ── 从拆分子模块 re-export 所有公共符号 ────────────────────────
from _mujoco_types import (  # noqa: F401
    Vec2,
    Vec3,
    JointInfo,
    ProjectionState,
    SupportDiagnostic,
    SafetyResult,
    CollisionPrimitive,
    CollisionSphere,
    CollisionDiagnostic,
)

from _mujoco_com import (  # noqa: F401
    resolve_joint_infos,
    resolve_support_ids,
    apply_joint_positions,
    compute_com_projection_from_joint_targets,
    compute_projection_state,
    evaluate_projection_safety,
    diagnose_support_state,
)

from _mujoco_collision import (  # noqa: F401
    load_collision_spheres_from_urdf,
    evaluate_arm_body_collision_safety,
    find_collision_joint_pair,
    _primitive_pair_signed_clearance,
    _should_skip_primitive_pair,
)


# ═══════════════════════════════════════════════════════════════
#  渲染函数（保留在本文件中）
# ═══════════════════════════════════════════════════════════════

import mujoco
import numpy as np

from _mujoco_collision import _primitive_pose_world


def render_collision_primitives(
    scene: mujoco.MjvScene,
    data: mujoco.MjData,
    arm_spheres,
    body_spheres,
    arm_rgba: tuple = (0.2, 0.6, 1.0, 0.25),
    body_rgba: tuple = (1.0, 0.4, 0.2, 0.25),
) -> int:
    """
    将所有碰撞代理几何渲染到 MuJoCo 场景中，可视化碰撞包络。

    参数：
    - `scene`: MuJoCo viewer 的 user_scn，用于追加调试 geom。
    - `data`: 当前 MuJoCo 数据，用于计算代理的世界位姿。
    - `arm_spheres`: 手臂碰撞代理列表。
    - `body_spheres`: 机身碰撞代理列表。
    - `arm_rgba`: 手臂代理的颜色 (R, G, B, A)。
    - `body_rgba`: 机身代理的颜色 (R, G, B, A)。

    返回：实际渲染的代理数量。
    """
    count = 0
    for primitive in arm_spheres:
        if _render_single_primitive(scene, data, primitive, arm_rgba):
            count += 1
    for primitive in body_spheres:
        if _render_single_primitive(scene, data, primitive, body_rgba):
            count += 1
    return count


def _render_single_primitive(
    scene: mujoco.MjvScene,
    data: mujoco.MjData,
    primitive,
    rgba: tuple,
) -> bool:
    """
    将单个碰撞代理几何渲染到场景中。

    根据代理类型渲染不同的 MuJoCo geom：
    - box → mjGEOM_BOX
    - cylinder → mjGEOM_CYLINDER
    - sphere → mjGEOM_SPHERE
    """
    if scene.ngeom >= scene.maxgeom:
        return False

    center_world, axes_world = _primitive_pose_world(data, primitive)
    rgba_arr = np.array(rgba, dtype=float)

    if primitive.primitive_type == "sphere":
        size = np.array([primitive.radius, 0.0, 0.0], dtype=float)
        geom = scene.geoms[scene.ngeom]
        mujoco.mjv_initGeom(
            geom,
            mujoco.mjtGeom.mjGEOM_SPHERE,
            size,
            center_world,
            np.eye(3).reshape(-1),
            rgba_arr,
        )
        scene.ngeom += 1
        return True

    if primitive.primitive_type == "cylinder":
        size = np.array([primitive.radius, primitive.half_length, 0.0], dtype=float)
        geom = scene.geoms[scene.ngeom]
        rot = axes_world.copy()
        geom = scene.geoms[scene.ngeom]
        mujoco.mjv_initGeom(
            geom,
            mujoco.mjtGeom.mjGEOM_CYLINDER,
            size,
            center_world,
            rot.reshape(-1),
            rgba_arr,
        )
        scene.ngeom += 1
        return True

    if primitive.primitive_type == "box":
        size = np.array(primitive.half_extents, dtype=float)
        geom = scene.geoms[scene.ngeom]
        rot = axes_world.copy()
        mujoco.mjv_initGeom(
            geom,
            mujoco.mjtGeom.mjGEOM_BOX,
            size,
            center_world,
            rot.reshape(-1),
            rgba_arr,
        )
        scene.ngeom += 1
        return True

    return False
