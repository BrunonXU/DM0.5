#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
f1_safety_config.py
====================
F1 安全控制配置文件解析器，支持 XML 和 YAML 两种格式。

职责：
- 从 f1_safety_config.xml 或 f1_safety_config.yaml 读取并解析所有配置节。
- 返回强类型的 dataclass 配置对象，供各功能模块直接使用。
- 提供工厂函数 load_config()，自动识别文件格式并加载。
- 提供工厂函数 load_config_yaml()，专用于 YAML 格式。

使用示例：
    from f1_safety_config import load_config

    # XML 或 YAML 均可，自动识别
    cfg = load_config("f1_safety_config.yaml")
    cfg = load_config("f1_safety_config.xml")

    # 质心保护参数
    print(cfg.com.support_margin)
    print(cfg.com.support_point_names)

    # 碰撞检测参数
    print(cfg.collision.collision_margin)
    print(cfg.collision.arm_link_prefixes)

    # 关节名映射（真机名 → URDF 名）
    urdf_name = cfg.joint_map.get("arm_l_j1")

    # 网络参数
    print(cfg.network.local_ip, cfg.network.udp_port)
"""

from __future__ import annotations

import os

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

# ── 从拆分出的类型模块 re-export 所有 dataclass ──────────────
from f1_safety_config_types import (  # noqa: F401
    ModelConfig,
    CoMGuardConfig,
    CollisionGuardConfig,
    JointLimitEntry,
    JointLimitsConfig,
    NetworkConfig,
    VisualizationConfig,
    F1SafetyConfig,
)


# ═══════════════════════════════════════════════════════════════
#  解析辅助函数
# ═══════════════════════════════════════════════════════════════

def _text(node: Optional[ET.Element], xpath: str, default: str = "") -> str:
    """安全读取 XML 子节点文本，节点缺失时返回 default。"""
    elem = node.find(xpath) if node is not None else None
    if elem is None or elem.text is None:
        return default
    return elem.text.strip()


def _float(node: Optional[ET.Element], xpath: str, default: float = 0.0) -> float:
    """安全读取 XML 子节点的浮点值。"""
    return float(_text(node, xpath, str(default)))


def _int(node: Optional[ET.Element], xpath: str, default: int = 0) -> int:
    """安全读取 XML 子节点的整数值。"""
    return int(_text(node, xpath, str(default)))


def _parse_model(root: ET.Element) -> ModelConfig:
    node = root.find("model")
    return ModelConfig(urdf_path=_text(node, "urdf_path"))


def _parse_com_guard(root: ET.Element) -> CoMGuardConfig:
    node = root.find("com_guard")
    if node is None:
        raise ValueError("XML 缺少 <com_guard> 节点")

    point_type = _text(node, "support_point_type", "body")
    points_node = node.find("support_points")
    point_names: List[str] = []
    if points_node is not None:
        for pt in points_node.findall("point"):
            if pt.text:
                point_names.append(pt.text.strip())

    margin = _float(node, "support_margin", 0.01)
    return CoMGuardConfig(
        support_point_type=point_type,
        support_point_names=tuple(point_names),
        support_margin=margin,
    )


def _parse_collision_guard(root: ET.Element) -> CollisionGuardConfig:
    node = root.find("collision_guard")
    if node is None:
        raise ValueError("XML 缺少 <collision_guard> 节点")

    margin = _float(node, "collision_margin", -0.002)

    arm_prefixes: List[str] = []
    arm_node = node.find("arm_link_prefixes")
    if arm_node is not None:
        for p in arm_node.findall("prefix"):
            if p.text:
                arm_prefixes.append(p.text.strip())

    body_prefixes: List[str] = []
    body_node = node.find("body_link_prefixes")
    if body_node is not None:
        for p in body_node.findall("prefix"):
            if p.text:
                body_prefixes.append(p.text.strip())

    excluded: List[Tuple[str, str]] = []
    pairs_node = node.find("excluded_pairs")
    if pairs_node is not None:
        for pair in pairs_node.findall("pair"):
            arm_link_elem  = pair.find("arm_link")
            body_link_elem = pair.find("body_link")
            if arm_link_elem is not None and body_link_elem is not None:
                arm_link  = (arm_link_elem.text  or "").strip()
                body_link = (body_link_elem.text or "").strip()
                if arm_link and body_link:
                    excluded.append((arm_link, body_link))

    return CollisionGuardConfig(
        collision_margin=margin,
        arm_link_prefixes=tuple(arm_prefixes),
        body_link_prefixes=tuple(body_prefixes),
        excluded_pairs=tuple(excluded),
        arm_tip_extension=_float(node, "arm_tip_extension", 0.0),
        arm_diameter_inflation=_float(node, "arm_diameter_inflation", 0.0),
    )


def _parse_joint_map(root: ET.Element) -> Dict[str, str]:
    """解析 <joint_map>，返回 {真机名: URDF名} 字典。"""
    jmap: Dict[str, str] = {}
    node = root.find("joint_map")
    if node is not None:
        for entry in node.findall("entry"):
            real = entry.get("real_name", "").strip()
            urdf = entry.get("urdf_name", "").strip()
            if real and urdf:
                jmap[real] = urdf
    return jmap


def _parse_joint_limits(root: ET.Element) -> JointLimitsConfig:
    """解析 <joint_limits>，返回限位配置。"""
    node = root.find("joint_limits")
    if node is None:
        return JointLimitsConfig(delta_deg=3.0, limits={})

    delta = _float(node, "delta_deg", 3.0)
    limits: Dict[str, JointLimitEntry] = {}
    for lim in node.findall("limit"):
        name  = lim.get("name", "").strip()
        lower = float(lim.get("lower", "0"))
        upper = float(lim.get("upper", "0"))
        if name:
            limits[name] = JointLimitEntry(name=name, lower=lower, upper=upper)

    return JointLimitsConfig(delta_deg=delta, limits=limits)


def _parse_network(root: ET.Element) -> NetworkConfig:
    node = root.find("network")
    return NetworkConfig(
        local_ip=_text(node, "local_ip", "192.0.2.13"),
        udp_port=_int(node, "udp_port", 8888),
        ip_config_path=_text(node, "ip_config_path", ""),
    )


def _parse_visualization(root: ET.Element) -> VisualizationConfig:
    node = root.find("visualization")
    return VisualizationConfig(
        support_line_width=_float(node, "support_line_width", 4.0),
        com_radius=_float(node, "com_radius", 0.05),
        collision_line_width=_float(node, "collision_line_width", 5.0),
        collision_marker_radius=_float(node, "collision_marker_radius", 0.04),
        print_interval_sec=_float(node, "print_interval_sec", 2.0),
    )


# ═══════════════════════════════════════════════════════════════
#  YAML 解析辅助函数
# ═══════════════════════════════════════════════════════════════

def _yaml_get(d: Any, *keys: str, default: Any = None) -> Any:
    """安全地从嵌套字典中按路径读取值，键不存在时返回 default。"""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def _parse_model_yaml(data: Dict) -> ModelConfig:
    return ModelConfig(
        urdf_path=os.path.expandvars(
            str(_yaml_get(data, "model", "urdf_path", default=""))
        )
    )


def _parse_com_guard_yaml(data: Dict) -> CoMGuardConfig:
    cg = data.get("com_guard", {})
    if not cg:
        raise ValueError("YAML 缺少 com_guard 节点")
    point_type = str(cg.get("support_point_type", "body"))
    points     = [str(p) for p in cg.get("support_points", [])]
    margin     = float(cg.get("support_margin", 0.01))
    return CoMGuardConfig(
        support_point_type=point_type,
        support_point_names=tuple(points),
        support_margin=margin,
    )


def _parse_collision_guard_yaml(data: Dict) -> CollisionGuardConfig:
    cg = data.get("collision_guard", {})
    if not cg:
        raise ValueError("YAML 缺少 collision_guard 节点")
    margin          = float(cg.get("collision_margin", -0.002))
    arm_prefixes    = [str(p) for p in cg.get("arm_link_prefixes",  [])]
    body_prefixes   = [str(p) for p in cg.get("body_link_prefixes", [])]
    excluded: List[Tuple[str, str]] = []
    for pair in cg.get("excluded_pairs", []):
        al = str(pair.get("arm_link",  "")).strip()
        bl = str(pair.get("body_link", "")).strip()
        if al and bl:
            excluded.append((al, bl))
    return CollisionGuardConfig(
        collision_margin=margin,
        arm_link_prefixes=tuple(arm_prefixes),
        body_link_prefixes=tuple(body_prefixes),
        excluded_pairs=tuple(excluded),
        arm_tip_extension=float(cg.get("arm_tip_extension", 0.0)),
        arm_diameter_inflation=float(cg.get("arm_diameter_inflation", 0.0)),
    )


def _parse_joint_map_yaml(data: Dict) -> Dict[str, str]:
    raw = data.get("joint_map", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _parse_joint_limits_yaml(data: Dict) -> JointLimitsConfig:
    jl = data.get("joint_limits", {})
    if not jl:
        return JointLimitsConfig(delta_deg=3.0, limits={})
    delta  = float(jl.get("delta_deg", 3.0))
    limits: Dict[str, JointLimitEntry] = {}
    for item in jl.get("limits", []):
        name  = str(item.get("name",  "")).strip()
        lower = float(item.get("lower", 0.0))
        upper = float(item.get("upper", 0.0))
        if name:
            limits[name] = JointLimitEntry(name=name, lower=lower, upper=upper)
    return JointLimitsConfig(delta_deg=delta, limits=limits)


def _parse_network_yaml(data: Dict) -> NetworkConfig:
    net = data.get("network", {})
    return NetworkConfig(
        local_ip=str(net.get("local_ip", "192.0.2.13")),
        udp_port=int(net.get("udp_port", 8888)),
        ip_config_path=os.path.expandvars(str(net.get("ip_config_path", ""))),
    )


def _parse_visualization_yaml(data: Dict) -> VisualizationConfig:
    vis = data.get("visualization", {})
    return VisualizationConfig(
        support_line_width=float(vis.get("support_line_width",      4.0)),
        com_radius=float(vis.get("com_radius",                      0.05)),
        collision_line_width=float(vis.get("collision_line_width",   5.0)),
        collision_marker_radius=float(vis.get("collision_marker_radius", 0.04)),
        print_interval_sec=float(vis.get("print_interval_sec",      2.0)),
    )


def load_config_yaml(yaml_path: str) -> F1SafetyConfig:
    """
    从 YAML 文件加载完整配置，返回 F1SafetyConfig 对象。

    Parameters
    ----------
    yaml_path:
        f1_safety_config.yaml 的路径（绝对路径或相对于当前目录）。

    Returns
    -------
    F1SafetyConfig
        包含所有子配置节的强类型配置对象。

    Raises
    ------
    FileNotFoundError
        当 YAML 文件不存在时抛出。
    ImportError
        当未安装 PyYAML（pip install pyyaml）时抛出。
    ValueError
        当必要的配置节缺失时抛出。
    """
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "加载 YAML 配置需要 PyYAML，请执行: pip install pyyaml"
        ) from exc

    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.resolve()}")

    with open(path, "r", encoding="utf-8") as f:
        data: Dict = yaml.safe_load(f) or {}

    cfg = F1SafetyConfig(
        model=_parse_model_yaml(data),
        com=_parse_com_guard_yaml(data),
        collision=_parse_collision_guard_yaml(data),
        joint_map=_parse_joint_map_yaml(data),
        joint_limits=_parse_joint_limits_yaml(data),
        network=_parse_network_yaml(data),
        visualization=_parse_visualization_yaml(data),
    )
    _validate_config(cfg)
    return cfg


# ═══════════════════════════════════════════════════════════════
#  公开工厂函数
# ═══════════════════════════════════════════════════════════════

def load_config(config_path: str) -> F1SafetyConfig:
    """
    自动识别格式（XML / YAML）并加载完整配置，返回 F1SafetyConfig 对象。

    支持的文件格式
    --------------
    - XML  : f1_safety_config.xml
    - YAML : f1_safety_config.yaml / f1_safety_config.yml

    Parameters
    ----------
    config_path:
        配置文件路径（绝对路径或相对于当前目录）。

    Returns
    -------
    F1SafetyConfig
        包含所有子配置节的强类型配置对象。

    Raises
    ------
    FileNotFoundError
        当配置文件不存在时抛出。
    ValueError
        当必要的配置节缺失或格式不支持时抛出。
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.resolve()}")

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return load_config_yaml(config_path)
    elif suffix == ".xml":
        return _load_config_xml(config_path)
    else:
        raise ValueError(
            f"不支持的配置文件格式: '{suffix}'，请使用 .xml / .yaml / .yml"
        )


def _load_config_xml(xml_path: str) -> F1SafetyConfig:
    """
    从 XML 文件加载完整配置（内部实现，由 load_config 自动分发）。
    """
    path = Path(xml_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.resolve()}")

    tree = ET.parse(str(path))
    root = tree.getroot()

    if root.tag != "f1_safety_config":
        raise ValueError(
            f"XML 根节点应为 <f1_safety_config>，实际为 <{root.tag}>"
        )

    cfg = F1SafetyConfig(
        model=_parse_model(root),
        com=_parse_com_guard(root),
        collision=_parse_collision_guard(root),
        joint_map=_parse_joint_map(root),
        joint_limits=_parse_joint_limits(root),
        network=_parse_network(root),
        visualization=_parse_visualization(root),
    )

    _validate_config(cfg)
    return cfg


def _validate_config(cfg: F1SafetyConfig) -> None:
    """对配置做基础合法性检查，不合法时抛出 ValueError。"""
    if not cfg.model.urdf_path:
        raise ValueError("配置错误: <model><urdf_path> 不能为空")
    if not cfg.com.support_point_names:
        raise ValueError("配置错误: <com_guard><support_points> 至少需要 3 个点")
    if len(cfg.com.support_point_names) < 3:
        raise ValueError(
            f"配置错误: 支撑点数量 {len(cfg.com.support_point_names)} < 3，"
            "无法构成有效支撑多边形"
        )
    if not cfg.collision.arm_link_prefixes:
        raise ValueError("配置错误: <collision_guard><arm_link_prefixes> 不能为空")
    if not cfg.collision.body_link_prefixes:
        raise ValueError("配置错误: <collision_guard><body_link_prefixes> 不能为空")


# ═══════════════════════════════════════════════════════════════
#  快速打印辅助
# ═══════════════════════════════════════════════════════════════

def print_config_summary(cfg: F1SafetyConfig) -> None:
    """将配置摘要打印到终端，方便启动时确认加载是否正确。"""
    print("=" * 55)
    print("[Config] F1 安全控制配置摘要")
    print("=" * 55)
    print(f"  URDF          : {cfg.model.urdf_path}")
    print(f"  CoM margin    : {cfg.com.support_margin} m")
    print(f"  Support pts   : {cfg.com.support_point_names}")
    print(f"  Col. margin   : {cfg.collision.collision_margin} m")
    print(f"  Arm tip ext.  : {cfg.collision.arm_tip_extension} m")
    print(f"  Arm dia. infl.: {cfg.collision.arm_diameter_inflation} m")
    print(f"  Arm prefixes  : {cfg.collision.arm_link_prefixes}")
    print(f"  Body prefixes : {cfg.collision.body_link_prefixes}")
    print(f"  Excl. pairs   : {len(cfg.collision.excluded_pairs)} pairs")
    print(f"  Joint map     : {len(cfg.joint_map)} entries")
    print(f"  Joint limits  : {len(cfg.joint_limits.limits)} joints "
          f"(delta={cfg.joint_limits.delta_deg}deg)")
    print(f"  Network       : {cfg.network.local_ip}:{cfg.network.udp_port}")
    print("=" * 55)


# ═══════════════════════════════════════════════════════════════
#  命令行快速验证
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    cfg_file = sys.argv[1] if len(sys.argv) > 1 else "f1_safety_config.yaml"
    try:
        cfg = load_config(cfg_file)
        print_config_summary(cfg)
        print("[Config] 配置文件解析成功 (format: " + Path(cfg_file).suffix + ")")
    except Exception as e:
        print(f"[Config] 解析失败: {e}")
        sys.exit(1)
