#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_f1_control_urdf_utils.py
=========================
F1 机器人 URDF → MJCF 预处理底层工具集（拆分自 F1_safe_control_real_with_com.py）。

对外符号
--------
- 常量: PACKAGE_URI_SCHEME / UNSUPPORTED_TAGS / DEFAULT_RGBA
- 数据类: VisualModelArtifacts
- 底层函数:
    prepare_visual_mujoco_model, sanitize_urdf_for_visual_mujoco,
    remove_unsupported_tags, resolve_mesh_filename, resolve_package_path,
    mesh_fallback_candidates, prepare_mesh_cache, configure_mujoco_compiler,
    write_tree, extract_visual_specs, build_final_mjcf

注意
----
面向上层的预处理统一入口 ``_prepare_mjcf`` 定义在
``com_and_collision_protect_modules.py``，内部会调用本模块的
``prepare_visual_mujoco_model``。不要直接从本模块导入 ``_prepare_mjcf``。
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import mujoco


# ═══════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════

PACKAGE_URI_SCHEME = "package://"
UNSUPPORTED_TAGS   = {"collision_checking", "convex"}
DEFAULT_RGBA       = "0.75 0.75 0.75 1"


__all__ = [
    "PACKAGE_URI_SCHEME",
    "UNSUPPORTED_TAGS",
    "DEFAULT_RGBA",
    "VisualModelArtifacts",
    "prepare_visual_mujoco_model",
    "sanitize_urdf_for_visual_mujoco",
    "remove_unsupported_tags",
    "resolve_mesh_filename",
    "resolve_package_path",
    "mesh_fallback_candidates",
    "prepare_mesh_cache",
    "configure_mujoco_compiler",
    "write_tree",
    "extract_visual_specs",
    "build_final_mjcf",
]


# ═══════════════════════════════════════════════════════════════
#  模型准备层辅助函数
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class VisualModelArtifacts:
    sanitized_urdf_path:    Path
    final_mjcf_path:        Path
    hidden_collision_geoms: int
    inserted_visual_geoms:  int


def prepare_visual_mujoco_model(urdf_path: Path) -> VisualModelArtifacts:
    repo_root = urdf_path.parents[2]
    # 使用当前脚本目录下的固定子目录，避免每次生成新的临时文件
    _script_dir = Path(__file__).parent.resolve()
    temp_dir = Path(os.environ["PICO_UPPER_RUNTIME"]) / "model_cache" / os.environ["PICO_UPPER_RELEASE_HASH"]
    sanitized_urdf = temp_dir / urdf_path.name
    final_mjcf     = temp_dir / f"{urdf_path.stem}_visual.xml"

    # 若 MJCF 文件已存在，直接复用，不再重新生成
    if final_mjcf.exists():
        print(f"[模型] 复用已有 MJCF 缓存: {final_mjcf}")
        return VisualModelArtifacts(sanitized_urdf, final_mjcf, 0, 0)

    # 首次生成：创建目录并执行完整预处理流程
    temp_dir.mkdir(parents=True, exist_ok=True)
    tree, _   = sanitize_urdf_for_visual_mujoco(urdf_path, repo_root)
    prepare_mesh_cache(tree.getroot(), repo_root, temp_dir / "meshes")
    visuals_by_link = extract_visual_specs(tree.getroot())
    configure_mujoco_compiler(tree.getroot(), temp_dir)
    write_tree(tree, sanitized_urdf)
    hidden, inserted = build_final_mjcf(sanitized_urdf, final_mjcf, visuals_by_link)
    return VisualModelArtifacts(sanitized_urdf, final_mjcf, hidden, inserted)


def sanitize_urdf_for_visual_mujoco(
    source_urdf: Path, repo_root: Path
) -> Tuple[ET.ElementTree, Dict[str, int]]:
    tree = ET.parse(source_urdf)
    root = tree.getroot()
    remove_unsupported_tags(root)
    rv = rc = up = 0
    for link in root.findall("link"):
        for tag_name in ("visual", "collision"):
            for tag in list(link.findall(tag_name)):
                mesh = tag.find("./geometry/mesh")
                if mesh is None:
                    continue
                fn = mesh.get("filename")
                if not fn:
                    continue
                resolved = resolve_mesh_filename(fn, repo_root)
                if resolved is None:
                    link.remove(tag)
                    if tag_name == "visual":
                        rv += 1
                    else:
                        rc += 1
                    continue
                mesh.set("filename", resolved.as_posix())
                up += 1
    return tree, dict(updated_meshes=up, removed_visuals=rv, removed_collisions=rc)


def remove_unsupported_tags(elem: ET.Element) -> None:
    for child in list(elem):
        if child.tag in UNSUPPORTED_TAGS:
            elem.remove(child)
        else:
            remove_unsupported_tags(child)


def resolve_mesh_filename(filename: str, repo_root: Path) -> Optional[Path]:
    mp = resolve_package_path(filename, repo_root)
    cands = (
        mesh_fallback_candidates(mp) if mp.is_absolute()
        else mesh_fallback_candidates(repo_root / mp)
    )
    for c in cands:
        if c.exists():
            return c.resolve()
    return None


def resolve_package_path(filename: str, repo_root: Path) -> Path:
    if filename.startswith(PACKAGE_URI_SCHEME):
        return repo_root / filename.removeprefix(PACKAGE_URI_SCHEME)
    return Path(filename)


def mesh_fallback_candidates(mp: Path) -> List[Path]:
    if mp.suffix.lower() == ".fbx":
        cands = [mp.with_suffix(".STL"), mp.with_suffix(".stl")]
        if mp.name.endswith("_r.fbx"):
            cands += [
                mp.with_name(mp.name.replace("_r.fbx", ".STL")),
                mp.with_name(mp.name.replace("_r.fbx", ".stl")),
            ]
        cands.append(mp)
        return cands
    return [mp]


def prepare_mesh_cache(
    root: ET.Element, repo_root: Path, mesh_cache_dir: Path
) -> None:
    mesh_cache_dir.mkdir(parents=True, exist_ok=True)
    for mesh in root.findall(".//mesh"):
        fn = mesh.get("filename")
        if not fn:
            continue
        src  = Path(fn)
        name = "__".join(src.relative_to(repo_root).parts)
        dest = mesh_cache_dir / name
        if not dest.exists():
            try:
                os.symlink(src, dest)
            except OSError:
                shutil.copy2(src, dest)
        mesh.set("filename", name)


def configure_mujoco_compiler(root: ET.Element, sanitized_dir: Path) -> None:
    mj   = root.find("mujoco") or ET.SubElement(root, "mujoco")
    comp = mj.find("compiler") or ET.SubElement(mj, "compiler")
    comp.set("meshdir", "meshes")
    comp.set("discardvisual", "false")
    comp.set("balanceinertia", "true")


def write_tree(tree: ET.ElementTree, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def extract_visual_specs(root: ET.Element) -> Dict[str, List[Dict[str, Any]]]:
    mats: Dict[str, str] = {}
    for mat in root.findall("material"):
        name  = mat.get("name")
        color = mat.find("color")
        if name and color is not None and color.get("rgba"):
            mats[name] = color.get("rgba", DEFAULT_RGBA)
    result: Dict[str, List[Dict[str, Any]]] = {}
    for link in root.findall("link"):
        lname = link.get("name")
        if not lname:
            continue
        for idx, visual in enumerate(link.findall("visual")):
            mesh = visual.find("./geometry/mesh")
            if mesh is None or not mesh.get("filename"):
                continue
            origin   = visual.find("origin")
            material = visual.find("material")
            rgba     = DEFAULT_RGBA
            if material is not None:
                color = material.find("color")
                if color is not None and color.get("rgba"):
                    rgba = color.get("rgba", DEFAULT_RGBA)
                elif material.get("name") in mats:
                    rgba = mats[material.get("name")]  # type: ignore[index]
            result.setdefault(lname, []).append({
                "name": f"visual_{lname}_{idx}",
                "file": mesh.get("filename"),
                "pos":  _parse_xyz(origin.get("xyz") if origin is not None else None),
                "quat": _rpy_to_quat(origin.get("rpy") if origin is not None else None),
                "rgba": rgba,
            })
    return result


def _parse_xyz(text: Optional[str]) -> str:
    return "0 0 0" if not text else " ".join(text.split())


def _rpy_to_quat(rpy_text: Optional[str]) -> str:
    if not rpy_text:
        return "1 0 0 0"
    roll, pitch, yaw = (float(v) for v in rpy_text.split())
    cr, sr = math.cos(roll * .5), math.sin(roll * .5)
    cp, sp = math.cos(pitch * .5), math.sin(pitch * .5)
    cy, sy = math.cos(yaw * .5), math.sin(yaw * .5)
    qw = cr*cp*cy + sr*sp*sy
    qx = sr*cp*cy - cr*sp*sy
    qy = cr*sp*cy + sr*cp*sy
    qz = cr*cp*sy - sr*sp*cy
    return f"{qw} {qx} {qy} {qz}"


def build_final_mjcf(
    sanitized_urdf: Path,
    mjcf_output_path: Path,
    visuals_by_link: Dict[str, List[Dict[str, Any]]],
) -> Tuple[int, int]:
    model     = mujoco.MjModel.from_xml_path(str(sanitized_urdf))
    temp_mjcf = mjcf_output_path.parent / f".{mjcf_output_path.name}.tmp.xml"
    mujoco.mj_saveLastXML(str(temp_mjcf), model)
    mjcf_tree = ET.parse(temp_mjcf)
    mjcf_root = mjcf_tree.getroot()
    worldbody = mjcf_root.find("worldbody")
    if worldbody is None:
        raise ValueError("compiled MJCF is missing worldbody")
    hidden   = _hide_collision_geoms(worldbody)
    inserted = _inject_visual_geoms(mjcf_root, visuals_by_link)
    _insert_ground_plane(worldbody)
    _insert_scene_lights(worldbody)
    write_tree(mjcf_tree, mjcf_output_path)
    temp_mjcf.unlink(missing_ok=True)
    return hidden, inserted


def _hide_collision_geoms(worldbody: ET.Element) -> int:
    count = 0
    for geom in worldbody.findall(".//geom"):
        geom.set("rgba", "1 1 1 0")
        geom.set("group", "3")
        count += 1
    return count


def _inject_visual_geoms(
    mjcf_root: ET.Element,
    visuals_by_link: Dict[str, List[Dict[str, Any]]],
) -> int:
    asset     = mjcf_root.find("asset")
    worldbody = mjcf_root.find("worldbody")
    if worldbody is None:
        raise ValueError("compiled MJCF is missing worldbody")
    if asset is None:
        idx   = list(mjcf_root).index(worldbody)
        asset = ET.Element("asset")
        mjcf_root.insert(idx, asset)
    mesh_names = _add_visual_assets(asset, visuals_by_link)
    body_lkp   = {
        b.get("name"): b
        for b in worldbody.findall(".//body")
        if b.get("name")
    }
    inserted = 0
    for lname, visuals in visuals_by_link.items():
        parent = worldbody if lname == "base_link" else body_lkp.get(lname)
        if parent is None:
            continue
        for vis in visuals:
            mname = mesh_names[(lname, vis["file"])]
            ET.SubElement(
                parent, "geom",
                name=f"{vis['name']}_geom",
                type="mesh", mesh=mname,
                pos=vis["pos"], quat=vis["quat"], rgba=vis["rgba"],
                contype="0", conaffinity="0", group="1",
            )
            inserted += 1
    return inserted


def _add_visual_assets(
    asset_elem: ET.Element,
    visuals_by_link: Dict[str, List[Dict[str, Any]]],
) -> Dict[Tuple[str, str], str]:
    mesh_names: Dict[Tuple[str, str], str] = {}
    idx = 0
    for lname, visuals in visuals_by_link.items():
        for vis in visuals:
            key = (lname, vis["file"])
            if key in mesh_names:
                continue
            mname = f"visual_mesh__{idx}"
            ET.SubElement(
                asset_elem, "mesh",
                name=mname, file=vis["file"],
                content_type="model/stl",
            )
            mesh_names[key] = mname
            idx += 1
    return mesh_names


def _insert_ground_plane(worldbody: ET.Element) -> None:
    for geom in worldbody.findall("geom"):
        if geom.get("name") == "ground_plane":
            return
    ET.SubElement(
        worldbody, "geom",
        name="ground_plane", type="plane",
        size="500 500 0.01", pos="0 0 -0.22",
        rgba="0.15 0.15 0.15 0.8",
        contype="0", conaffinity="0", group="0",
    )


def _insert_scene_lights(worldbody: ET.Element) -> None:
    ET.SubElement(worldbody, "light",
        directional="true", pos="0 0 3", dir="0 0 -1",
        diffuse="0.7 0.7 0.7", specular="0.3 0.3 0.3")
    ET.SubElement(worldbody, "light",
        directional="true", pos="2 0 2", dir="-0.5 0 -0.5",
        diffuse="0.4 0.4 0.4", specular="0.1 0.1 0.1")
    ET.SubElement(worldbody, "light",
        directional="true", pos="0 2 1.5", dir="0 -0.5 -0.5",
        diffuse="0.35 0.35 0.35", specular="0.1 0.1 0.1")
