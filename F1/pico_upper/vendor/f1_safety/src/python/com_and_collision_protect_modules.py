#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
com_and_collision_protect_modules.py
====================================
F1 机器人质心保护与碰撞检测的统一对外入口（聚合 facade 模块）。

聚合内容
--------
- _prepare_mjcf       : URDF → MJCF 预处理入口（在本文件内直接定义，内部委托
                        _f1_control_urdf_utils.prepare_visual_mujoco_model）
- CoMGuard            : 质心保护检查器（来自 f1_safety_modules）
- CollisionGuard      : 碰撞检测检查器（来自 f1_safety_modules）
- CombinedSafetyGuard : 联合安全门，封装上述两个模块（来自 f1_safety_modules）
- SafetyProtector     : 一站式安全保护类（实现在 _safety_protector.py）

配置/初始化辅助函数
------------------
- load_safety_config(config_path=None) -> cfg
    自动加载 YAML/XML 安全配置；config_path 为 None 时按优先级搜索同目录默认文件。

- load_safety_modules(config_path=None, use_com=True, use_col=True)
        -> (cfg, mjcf_path, com_guard, collision_guard, safety_guard)
    一站式入口：加载配置 → URDF→MJCF 预处理 → 初始化 CoMGuard / CollisionGuard
    → 组装 CombinedSafetyGuard。任何步骤失败时对应返回项为 None，调用方据此
    决定降级行为。

设计意图
--------
本模块是 _prepare_mjcf 的唯一入口：F1_safe_control_real_with_com、_f1_control_bridge、
F1_state_viewer 等所有上层脚本都从本文件导入这些符号和初始化函数，不再依赖
_f1_control_urdf_utils.py 或 F1_safe_control_real_with_com.py 提供 _prepare_mjcf：

    from com_and_collision_protect_modules import (
        _prepare_mjcf,
        load_safety_modules,
        SafetyProtector,
    )
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional, Tuple

# ── 底层符号 re-export ─────────────────────────────────────────
# URDF 预处理：仅依赖底层工具函数，不再与 F1_safe_control_real_with_com 关联
from _f1_control_urdf_utils import prepare_visual_mujoco_model  # type: ignore
from f1_safety_modules import (  # type: ignore
    CoMGuard,
    CollisionGuard,
    CombinedSafetyGuard,
)

# ── SafetyProtector 及常量 re-export（拆分自本模块） ───────────
from _safety_protector import (  # noqa: F401
    SafetyProtector,
    LEFT_JOINT_NAMES,
    RIGHT_JOINT_NAMES,
    HEAD_JOINT_NAMES,
    BODY_JOINT_NAMES,
    ALL_JOINT_NAMES,
    CHASSIS_WHEEL_JOINT_NAMES,
    IDX_LEFT_ARM,
    IDX_RIGHT_ARM,
    IDX_HEAD,
    IDX_BODY,
    CMD_DIM_DEFAULT,
)

__all__ = [
    "_prepare_mjcf",
    "CoMGuard",
    "CollisionGuard",
    "CombinedSafetyGuard",
    "load_safety_config",
    "load_safety_modules",
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

_THIS_DIR = Path(__file__).parent.resolve()

_DEFAULT_CONFIG_NAMES = (
    "f1_safety_config.yaml",
    "f1_safety_config.yml",
    "f1_safety_config.xml",
)


# ============================================================
#  URDF → MJCF 预处理入口
# ============================================================

def _prepare_mjcf(urdf_path_str: str) -> str:
    """预处理 URDF，返回 MJCF 路径字符串。

    本函数是面向上层的官方入口，内部委托给
    ``_f1_control_urdf_utils.prepare_visual_mujoco_model`` 完成实际的 URDF→MJCF
    转换与缓存。F1_safe_control_real_with_com、_f1_control_bridge 等上层模块
    均从本模块 ``from com_and_collision_protect_modules import _prepare_mjcf``
    获取入口，不再直接依赖 _f1_control_urdf_utils 或 F1_safe_control_real_with_com。

    Parameters
    ----------
    urdf_path_str : URDF 文件绝对路径。

    Returns
    -------
    str
        预处理完成后的 MJCF 文件路径（字符串）。

    Raises
    ------
    FileNotFoundError
        指定 URDF 文件不存在时抛出。
    """
    urdf_path = Path(urdf_path_str).resolve()
    if not urdf_path.exists():
        raise FileNotFoundError(f"未找到 URDF: {urdf_path}")
    artifacts = prepare_visual_mujoco_model(urdf_path)
    print(
        f"[模型] MJCF: {artifacts.final_mjcf_path} | "
        f"隐藏碰撞={artifacts.hidden_collision_geoms} "
        f"插入视觉={artifacts.inserted_visual_geoms}"
    )
    return str(artifacts.final_mjcf_path)


# ============================================================
#  配置加载
# ============================================================

def load_safety_config(config_path: Optional[str] = None) -> Optional[Any]:
    """
    加载安全配置文件（自动识别 XML / YAML 格式），失败时返回 None。

    查找顺序
    --------
      1. 显式传入的 config_path
      2. 同目录下的 f1_safety_config.yaml
      3. 同目录下的 f1_safety_config.yml
      4. 同目录下的 f1_safety_config.xml

    Returns
    -------
    F1SafetyConfig | None
        加载成功返回配置对象；找不到文件或解析失败返回 None。
    """
    try:
        sys.path.insert(0, str(_THIS_DIR))
        from f1_safety_config import load_config  # type: ignore

        if config_path:
            path = config_path
        else:
            path = None
            for name in _DEFAULT_CONFIG_NAMES:
                candidate = _THIS_DIR / name
                if candidate.exists():
                    path = str(candidate)
                    break
            if path is None:
                print("[SafetyModules] 未找到任何配置文件，将跳过安全检查")
                return None

        cfg = load_config(path)
        print(f"[SafetyModules] 安全配置加载成功: {path}")
        return cfg
    except Exception as exc:
        print(f"[SafetyModules] 安全配置加载失败（将跳过安全检查）: {exc}")
        return None


# ============================================================
#  安全模块一站式初始化
# ============================================================

def load_safety_modules(
    config_path: Optional[str] = None,
    use_com:     bool = True,
    use_col:     bool = True,
) -> Tuple[
    Optional[Any],                       # cfg
    Optional[str],                       # mjcf_path
    Optional["CoMGuard"],                # com_guard
    Optional["CollisionGuard"],          # collision_guard
    Optional["CombinedSafetyGuard"],     # safety_guard
]:
    """
    一站式加载：配置 + URDF→MJCF 预处理 + CoMGuard / CollisionGuard / CombinedSafetyGuard。

    Parameters
    ----------
    config_path : 配置文件路径；为 None 时自动搜索同目录默认文件。
    use_com     : 是否启用 CoMGuard。
    use_col     : 是否启用 CollisionGuard。

    Returns
    -------
    (cfg, mjcf_path, com_guard, collision_guard, safety_guard)
        - cfg            : F1SafetyConfig；加载失败为 None。
        - mjcf_path      : MJCF 文件路径；URDF 预处理失败为 None。
        - com_guard      : CoMGuard 实例；未启用或失败为 None。
        - collision_guard: CollisionGuard 实例；未启用或失败为 None。
        - safety_guard   : CombinedSafetyGuard；仅在 com_guard 与 collision_guard
                           都成功时构建，否则为 None（调用方走单模块独立分支）。
    """
    # 1. 加载配置
    cfg = load_safety_config(config_path)
    if cfg is None:
        return None, None, None, None, None

    # 2. URDF → MJCF + 加载 MuJoCo 模型
    try:
        import mujoco
        mjcf_path = _prepare_mjcf(cfg.model.urdf_path)
        model     = mujoco.MjModel.from_xml_path(mjcf_path)
    except Exception as exc:
        print(f"[SafetyModules] URDF/MJCF 预处理失败: {exc}")
        return cfg, None, None, None, None

    joint_map = cfg.joint_map  # {真机名 → URDF 名}

    # 3. CoMGuard
    com_guard: Optional[CoMGuard] = None
    if use_com:
        try:
            com_guard = CoMGuard.from_config(
                model=model,
                com_cfg=cfg.com,
                joint_name_map=joint_map,
            )
            print("[SafetyModules] CoMGuard 初始化完成")
        except Exception as exc:
            print(f"[SafetyModules] CoMGuard 初始化失败: {exc}")

    # 4. CollisionGuard
    collision_guard: Optional[CollisionGuard] = None
    if use_col:
        try:
            collision_guard = CollisionGuard.from_config(
                model=model,
                col_cfg=cfg.collision,
                urdf_path=cfg.model.urdf_path,
                joint_name_map=joint_map,
            )
            print("[SafetyModules] CollisionGuard 初始化完成")
        except Exception as exc:
            print(f"[SafetyModules] CollisionGuard 初始化失败: {exc}")

    # 5. CombinedSafetyGuard（仅两者都成功时组装）
    safety_guard: Optional[CombinedSafetyGuard] = None
    if com_guard is not None and collision_guard is not None:
        safety_guard = CombinedSafetyGuard(
            com_guard=com_guard,
            collision_guard=collision_guard,
            joint_name_map=joint_map,
        )
        print("[SafetyModules] CombinedSafetyGuard 初始化完成（CoM + Collision）")
    elif com_guard is not None:
        print("[SafetyModules] 仅 CoMGuard 模式（无 CollisionGuard）")
    elif collision_guard is not None:
        print("[SafetyModules] 仅 CollisionGuard 模式（无 CoMGuard）")

    return cfg, mjcf_path, com_guard, collision_guard, safety_guard
