#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_mujoco_geometry_utils.py
=========================
URDF 碰撞几何解析与代理构建工具（从 _mujoco_collision.py 进一步拆分）。

包含：URDF collision 元素解析、mesh 加载、代理几何拟合、坐标变换。
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

import mujoco
import numpy as np

from _mujoco_types import CollisionPrimitive, Vec3


def _classify_collision_link(
    link_name: str,
    arm_link_prefixes: Sequence[str],
    body_link_prefixes: Sequence[str],
) -> Optional[str]:
    """根据 link 名称前缀，把 link 粗分为 `arm` 或 `body`。"""
    if any(link_name.startswith(prefix) for prefix in arm_link_prefixes):
        return "arm"
    if any(link_name.startswith(prefix) for prefix in body_link_prefixes):
        return "body"
    return None


def _extract_enabled_collision_names(link_elem: ET.Element) -> Optional[set[str]]:
    """读取 URDF 中的 collision_checking/convex 白名单配置。"""
    checking_elem = link_elem.find("collision_checking")
    if checking_elem is None:
        return None
    names = {
        convex.attrib["name"]
        for convex in checking_elem.findall("convex")
        if "name" in convex.attrib
    }
    return names or None


def _build_collision_primitive(
    model: mujoco.MjModel,
    urdf_path: Path,
    link_name: str,
    collision_name: str,
    body_id: int,
    collision_elem: ET.Element,
) -> Optional[CollisionPrimitive]:
    """将一个 URDF collision 元素转换为单个代理几何。"""
    geometry_elem = collision_elem.find("geometry")
    if geometry_elem is None:
        return None

    origin_elem = collision_elem.find("origin")
    translation, rotation = _parse_origin(origin_elem)
    local_center = np.array(translation, dtype=float)
    local_axes = np.array(rotation, dtype=float)
    half_extents = np.zeros(3, dtype=float)
    radius = 0.0
    half_length = 0.0
    primitive_type: Optional[str] = None

    if geometry_elem.find("box") is not None:
        size = _parse_xyz_triplet(geometry_elem.find("box").attrib["size"])
        half_extents = 0.5 * np.array(size, dtype=float)
        primitive_type = "box"
    elif geometry_elem.find("cylinder") is not None:
        cylinder_elem = geometry_elem.find("cylinder")
        radius = float(cylinder_elem.attrib["radius"])
        half_length = 0.5 * float(cylinder_elem.attrib["length"])
        primitive_type = "cylinder"
    elif geometry_elem.find("sphere") is not None:
        radius = float(geometry_elem.find("sphere").attrib["radius"])
        primitive_type = "sphere"
    elif geometry_elem.find("mesh") is not None:
        mesh_elem = geometry_elem.find("mesh")
        scale = _parse_xyz_triplet(mesh_elem.attrib.get("scale", "1 1 1"))
        (
            primitive_type, mesh_center, mesh_axes,
            half_extents, radius, half_length,
        ) = _fit_mesh_collision_primitive(
            _resolve_mesh_path(urdf_path, mesh_elem.attrib["filename"]),
            scale,
        )
        local_center = np.array(translation, dtype=float) + rotation @ np.array(mesh_center, dtype=float)
        local_axes = np.array(rotation, dtype=float) @ mesh_axes
    else:
        return None

    if primitive_type is None:
        return None

    return CollisionPrimitive(
        link_name=link_name,
        collision_name=collision_name,
        body_id=body_id,
        parent_body_id=int(model.body_parentid[body_id]),
        primitive_type=primitive_type,
        local_center=local_center,
        local_axes=local_axes,
        half_extents=half_extents,
        radius=radius,
        half_length=half_length,
    )


def _parse_origin(origin_elem: Optional[ET.Element]) -> Tuple[Vec3, np.ndarray]:
    """解析 URDF origin 标签，返回平移向量和旋转矩阵。"""
    if origin_elem is None:
        return (0.0, 0.0, 0.0), np.eye(3)
    translation = _parse_xyz_triplet(origin_elem.attrib.get("xyz", "0 0 0"))
    rpy = _parse_xyz_triplet(origin_elem.attrib.get("rpy", "0 0 0"))
    return translation, _rpy_to_matrix(rpy)


def _parse_xyz_triplet(raw_value: str) -> Vec3:
    """解析形如 "x y z" 的三元组文本。"""
    parts = [float(part) for part in raw_value.split()]
    if len(parts) != 3:
        raise ValueError(f"expected 3 floats, got: {raw_value}")
    return parts[0], parts[1], parts[2]


def _rpy_to_matrix(rpy: Vec3) -> np.ndarray:
    """将 URDF 常用的 RPY 欧拉角转换为旋转矩阵。"""
    roll, pitch, yaw = rpy
    cr = math.cos(roll)
    sr = math.sin(roll)
    cp = math.cos(pitch)
    sp = math.sin(pitch)
    cy = math.cos(yaw)
    sy = math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=float,
    )


def _resolve_mesh_path(urdf_path: Path, mesh_filename: str) -> Path:
    """解析 collision mesh 的实际文件路径。"""
    if mesh_filename.startswith("package://"):
        relative_path = mesh_filename.removeprefix("package://")
        parts = relative_path.split("/", 1)
        pkg_name = parts[0] if parts else ""
        pkg_relative = parts[1] if len(parts) > 1 else ""
        for parent in urdf_path.parents:
            candidate = parent / pkg_name / pkg_relative
            if candidate.exists():
                return candidate.resolve()
            if parent.name == pkg_name:
                candidate = parent / pkg_relative
                if candidate.exists():
                    return candidate.resolve()
        raise FileNotFoundError(f"failed to resolve mesh path: {mesh_filename}")

    candidate = Path(mesh_filename)
    if candidate.is_absolute():
        return candidate.resolve()
    return (urdf_path.parent / candidate).resolve()


def _fit_mesh_collision_primitive(
    mesh_path: Path,
    scale: Vec3,
) -> Tuple[str, Vec3, np.ndarray, np.ndarray, float, float]:
    """根据 mesh 顶点拟合更贴近真实连杆外形的代理几何。"""
    vertices = _load_mesh_vertices(mesh_path)
    scaled_vertices = vertices * np.array(scale, dtype=float)
    minimum = scaled_vertices.min(axis=0)
    maximum = scaled_vertices.max(axis=0)
    center = 0.5 * (minimum + maximum)
    extents = maximum - minimum
    half_extents = 0.5 * extents

    major_axis = int(np.argmax(extents))
    minor_axes = [axis for axis in range(3) if axis != major_axis]
    minor_values = extents[minor_axes]
    minor_min = float(np.min(minor_values))
    minor_max = float(np.max(minor_values))
    elongation = float(extents[major_axis] / max(minor_max, 1e-8))
    roundness = float(minor_max / max(minor_min, 1e-8))

    if elongation >= 1.25 and roundness <= 1.35:
        return (
            "cylinder",
            (float(center[0]), float(center[1]), float(center[2])),
            _major_axis_frame(major_axis),
            np.zeros(3, dtype=float),
            0.5 * minor_max,
            0.5 * float(extents[major_axis]),
        )

    return (
        "box",
        (float(center[0]), float(center[1]), float(center[2])),
        np.eye(3, dtype=float),
        np.array(half_extents, dtype=float),
        0.0,
        0.0,
    )


def _load_mesh_vertices(mesh_path: Path) -> np.ndarray:
    """从 STL 文件中提取顶点数组。"""
    if mesh_path.suffix.lower() != ".stl":
        raise ValueError(f"unsupported collision mesh format '{mesh_path.suffix}' for {mesh_path}")

    mesh_bytes = mesh_path.read_bytes()
    if len(mesh_bytes) >= 84:
        triangle_count = struct.unpack_from("<I", mesh_bytes, 80)[0]
        expected_size = 84 + triangle_count * 50
        if expected_size == len(mesh_bytes):
            triangle_dtype = np.dtype(
                [
                    ("normal", "<f4", (3,)),
                    ("vertices", "<f4", (3, 3)),
                    ("attr", "<u2"),
                ]
            )
            triangles = np.frombuffer(
                mesh_bytes,
                dtype=triangle_dtype,
                count=triangle_count,
                offset=84,
            )
            return np.array(triangles["vertices"].reshape(-1, 3), dtype=float)

    vertices = []
    for raw_line in mesh_bytes.decode("utf-8", errors="ignore").splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("vertex "):
            continue
        _, x_value, y_value, z_value = stripped.split()
        vertices.append((float(x_value), float(y_value), float(z_value)))
    if not vertices:
        raise ValueError(f"failed to parse STL vertices from {mesh_path}")
    return np.array(vertices, dtype=float)


def _major_axis_frame(axis_index: int) -> np.ndarray:
    """为拟合出的圆柱生成一个局部坐标系。"""
    if axis_index == 0:
        return np.array(
            [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=float,
        )
    if axis_index == 1:
        return np.array(
            [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]],
            dtype=float,
        )
    return np.eye(3, dtype=float)


def _find_joint_for_body(
    model: mujoco.MjModel,
    body_name: str,
) -> Optional[str]:
    """查找直接连接指定 body 到其父 body 的关节名称。"""
    body_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, body_name)
    if body_id < 0:
        return None

    for joint_id in range(model.njnt):
        if model.jnt_bodyid[joint_id] == body_id:
            joint_name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, joint_id)
            return joint_name
    return None
