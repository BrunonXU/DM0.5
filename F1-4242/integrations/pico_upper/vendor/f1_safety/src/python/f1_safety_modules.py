#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
f1_safety_modules.py — F1 安全控制模块

提供 CoMGuard（质心保护）、CombinedSafetyGuard（联合安全门）。
CollisionGuard 已拆分至 f1_collision_guard.py，本模块 re-export。

调用示例::

    from f1_safety_modules import CoMGuard, CollisionGuard, CombinedSafetyGuard
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import mujoco
import numpy as np

from mujoco_com_guard_core import (
    SafetyResult,
    compute_projection_state,
    evaluate_projection_safety,
    resolve_joint_infos,
    resolve_support_ids,
)

# 配置类型（从 f1_safety_config 导入）
from f1_safety_config import (
    CoMGuardConfig,
    F1SafetyConfig,
)

# ── 从拆分出的模块 re-export ─────────────────────────────────
from f1_collision_guard import (  # noqa: F401
    CollisionGuard,
    CollisionGuardResult,
    _apply_cmd_dict,
)


# ═══════════════════════════════════════════════════════════════
#  CoMGuardResult  ──  质心检查结果
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CoMGuardResult:
    """
    质心保护单次检查结果。

    Attributes
    ----------
    safe:
        True 表示质心投影在支撑多边形安全边界内。
    signed_clearance:
        质心到最近边界的带符号距离（米）；正值=安全，负值=越界。
    com_xy:
        当前质心在地面平面的投影坐标 (x, y)（米）。
    support_polygon_xy:
        当前支撑多边形各顶点坐标序列。
    violating_edge_index:
        越界时触发的边索引；安全时为 -1。
    raw:
        底层 SafetyResult 原始对象（需要更多细节时使用）。
    """
    safe:                 bool
    signed_clearance:     float
    com_xy:               Tuple[float, float]
    support_polygon_xy:   Tuple[Tuple[float, float], ...]
    violating_edge_index: int
    raw:                  SafetyResult


# ═══════════════════════════════════════════════════════════════
#  CoMGuard  ──  质心保护功能模块
# ═══════════════════════════════════════════════════════════════

class CoMGuard:
    """
    质心保护功能模块。

    职责：给定一组期望关节角（弧度），预测质心投影是否仍在
    支撑多边形安全边界内，并返回 CoMGuardResult。

    初始化后可多次调用 check()，内部 trial_data 独立，线程安全。
    （注意：单个实例不适合多线程并发调用 check()；如需并发请各自实例化。）
    """

    def __init__(
        self,
        model:             mujoco.MjModel,
        support_point_type: str,
        support_point_names: Sequence[str],
        support_margin:    float,
        joint_name_map:    Optional[Dict[str, str]] = None,
        controlled_joints: Optional[Sequence[str]]  = None,
    ) -> None:
        """
        Parameters
        ----------
        model:
            MuJoCo 模型对象（已加载完毕）。
        support_point_type:
            支撑点 MuJoCo 对象类型，"body" / "site" / "geom"。
        support_point_names:
            支撑多边形各顶点名称序列。
        support_margin:
            质心到支撑多边形边界的最小安全距离（米）。
        joint_name_map:
            {真机关节名 → URDF 关节名}；None 时不做名称转换。
        controlled_joints:
            受控关节名列表；None 时自动解析模型中全部 1-DoF 关节。
        """
        self._model            = model
        self._trial_data       = mujoco.MjData(model)
        self._support_pt_type  = support_point_type
        self._support_margin   = support_margin
        self._joint_name_map   = joint_name_map or {}

        self._joint_infos = resolve_joint_infos(model, controlled_joints)
        if not self._joint_infos:
            raise RuntimeError("CoMGuard: 模型中没有找到可控 1-DoF 关节")

        self._name_to_idx: Dict[str, int] = {
            ji.name: idx for idx, ji in enumerate(self._joint_infos)
        }
        self._support_ids = resolve_support_ids(
            model, support_point_type, support_point_names
        )
        # 初始化前向计算
        mujoco.mj_forward(model, self._trial_data)
        self._last_result: Optional[CoMGuardResult] = None

    # ── 工厂方法：从配置对象构建 ──────────────────────────────

    @classmethod
    def from_config(
        cls,
        model:          mujoco.MjModel,
        com_cfg:        CoMGuardConfig,
        joint_name_map: Optional[Dict[str, str]] = None,
    ) -> "CoMGuard":
        """
        从 CoMGuardConfig 配置对象构建实例。

        Parameters
        ----------
        model:
            已加载的 MuJoCo 模型。
        com_cfg:
            由 load_config() 解析得到的 CoMGuardConfig。
        joint_name_map:
            真机名 → URDF 名映射（来自 F1SafetyConfig.joint_map）。
        """
        return cls(
            model=model,
            support_point_type=com_cfg.support_point_type,
            support_point_names=com_cfg.support_point_names,
            support_margin=com_cfg.support_margin,
            joint_name_map=joint_name_map,
        )

    # ── 主接口 ────────────────────────────────────────────────

    def check_com_safety(self, desired_joint_cmd: Dict[str, float]) -> CoMGuardResult:
        """
        对期望关节位置做质心安全预测。

        Parameters
        ----------
        desired_joint_cmd:
            {关节名 → 弧度值} 字典。键可以是真机名或 URDF 名，
            由构造时传入的 joint_name_map 自动转换。

        Returns
        -------
        CoMGuardResult
            包含 safe 标志、signed_clearance、com_xy 等字段。
        """

        # 应用关节命令：desired_joint_cmd → trial_data.qpos，代表“假设按期望运动后”的位姿
        _apply_cmd_dict(
            self._trial_data, self._model,
            self._joint_infos, self._name_to_idx,
            desired_joint_cmd, self._joint_name_map,
        )

        # 调试：打印写入后的受控关节 qpos（MjData 默认 repr 看不到内部数据）
        if True:  # 需调试时改为 True
            qpos_view = {
                ji.name: float(self._trial_data.qpos[ji.qpos_adr])
                for ji in self._joint_infos
            }
            # print(f"[CoMGuard] desired_joint_cmd: {desired_joint_cmd}")
            # print("***************")
            # print(f"[CoMGuard] trial qpos      : {qpos_view}")

        proj   = compute_projection_state(
            model=self._model,
            data=self._trial_data,
            point_type=self._support_pt_type,
            support_ids=self._support_ids,
        )
        safety = evaluate_projection_safety(proj, margin=self._support_margin)

        result = CoMGuardResult(
            safe=safety.safe,
            signed_clearance=safety.support_diagnostic.signed_clearance,
            com_xy=proj.com_xy,
            support_polygon_xy=proj.support_polygon_xy,
            violating_edge_index=(
                safety.support_diagnostic.violating_edge_index
                if not safety.safe else -1
            ),
            raw=safety,
        )
        self._last_result = result
        return result

    # 兼容别名：旧代码中 guard.check(...) 仍可用
    check = check_com_safety

    @property
    def last_result(self) -> Optional[CoMGuardResult]:
        """最近一次 check_com_safety() 的结果。"""
        return self._last_result

    @property
    def trial_data(self) -> mujoco.MjData:
        """暴露内部 trial_data，供 Visualizer 读取位姿。"""
        return self._trial_data


# ═══════════════════════════════════════════════════════════════
#  CombinedSafetyGuard  ──  质心 + 碰撞联合安全门
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CombinedCheckResult:
    """
    CombinedSafetyGuard.check_combined_safety() 的返回值。

    Attributes
    ----------
    safe:
        True 当且仅当质心和碰撞两项检查均通过。
    reason:
        safe=False 时描述拒绝原因的字符串；safe=True 时为 "SAFE"。
    com:
        质心检查单项结果。
    collision:
        碰撞检查单项结果。
    """
    safe:      bool
    reason:    str
    com:       CoMGuardResult
    collision: CollisionGuardResult


class CombinedSafetyGuard:
    """
    质心保护 + 碰撞检测联合安全门。

    推荐用于真机部署：传入期望关节命令字典，返回联合检查结果。
    内部包含独立的 CoMGuard 和 CollisionGuard 实例，两者共用同一
    MuJoCo 模型但各自维护独立的 trial_data，互不干扰。

    构建方式
    --------
    推荐使用工厂方法 from_config()，传入 MJCF 路径和配置对象：

        guard = CombinedSafetyGuard.from_config(mjcf_path, cfg)
        result = guard.check({"arm_l_j1": 0.5, ...})
    """

    def __init__(
        self,
        com_guard:       CoMGuard,
        collision_guard: CollisionGuard,
        joint_name_map:  Optional[Dict[str, str]] = None,
    ) -> None:
        self._com_guard       = com_guard
        self._collision_guard = collision_guard
        self._joint_name_map  = joint_name_map or {}
        self._last_result: Optional[CombinedCheckResult] = None

    # ── 工厂方法 ───────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        mjcf_path: str,
        cfg:       F1SafetyConfig,
    ) -> "CombinedSafetyGuard":
        """
        从 MJCF 路径和 F1SafetyConfig 配置对象构建联合安全门。

        Parameters
        ----------
        mjcf_path:
            由 prepare_visual_mujoco_model() 生成的 MJCF 文件路径。
        cfg:
            由 load_config() 返回的完整配置对象。
        """
        model = mujoco.MjModel.from_xml_path(mjcf_path)

        com_guard = CoMGuard.from_config(
            model=model,
            com_cfg=cfg.com,
            joint_name_map=cfg.joint_map,
        )
        col_guard = CollisionGuard.from_config(
            model=model,
            col_cfg=cfg.collision,
            urdf_path=cfg.model.urdf_path,
            joint_name_map=cfg.joint_map,
        )
        return cls(
            com_guard=com_guard,
            collision_guard=col_guard,
            joint_name_map=cfg.joint_map,
        )

    # ── 主接口 ────────────────────────────────────────────────

    def check_combined_safety(self, desired_joint_cmd: Dict[str, float]) -> CombinedCheckResult:
        """
        对期望关节位置执行质心 + 碰撞联合安全检查。

        Parameters
        ----------
        desired_joint_cmd:
            {关节名 → 弧度值} 字典；键可以是真机名（arm_l_j1…）
            或 URDF 名（arm_L_joint1…），由配置中的 joint_map 自动转换。

        Returns
        -------
        CombinedCheckResult
            包含联合安全标志、拒绝原因和两项子结果。

        示例
        ----
            result = guard.check_combined_safety({"arm_l_j1": 0.3, "arm_r_j2": -0.5})
            if not result.safe:
                print(result.reason)
                print("CoM clearance:", result.com.signed_clearance)
                print("Col clearance:", result.collision.signed_clearance)
        """
        # print(f"****** desired_joint_cmd--621 {desired_joint_cmd}")
        com_result = self._com_guard.check_com_safety(desired_joint_cmd)
        col_result = self._collision_guard.check_collision_safety(desired_joint_cmd)

        safe    = com_result.safe and col_result.safe
        reasons = []
        if not com_result.safe:
            reasons.append(
                f"CoM out of support polygon "
                f"(clearance={com_result.signed_clearance:.4f}m)"
            )
        if not col_result.safe:
            reasons.append(
                f"Arm-body collision risk "
                f"(clearance={col_result.signed_clearance:.4f}m, "
                f"pair={col_result.collision_pair})"
            )
        reason = "; ".join(reasons) if reasons else "SAFE"

        result = CombinedCheckResult(
            safe=safe,
            reason=reason,
            com=com_result,
            collision=col_result,
        )
        self._last_result = result
        return result

    # 兼容别名：旧代码中 guard.check(...) 仍可用
    check = check_combined_safety

    # ── 属性访问 ──────────────────────────────────────────────

    @property
    def last_result(self) -> Optional[CombinedCheckResult]:
        return self._last_result

    @property
    def com_guard(self) -> CoMGuard:
        return self._com_guard

    @property
    def collision_guard(self) -> CollisionGuard:
        return self._collision_guard

    @property
    def model(self) -> mujoco.MjModel:
        """返回共用 MuJoCo 模型（供 Visualizer 使用）。"""
        return self._com_guard._model

    # 兼容旧接口：保持与 SafetyGuard.arm_spheres / body_spheres 一致
    @property
    def arm_spheres(self):
        return self._collision_guard.arm_spheres

    @property
    def body_spheres(self):
        return self._collision_guard.body_spheres

    @property
    def joint_infos(self):
        return self._com_guard._joint_infos

    @property
    def _joint_name_to_idx(self) -> Dict[str, int]:
        return self._com_guard._name_to_idx

    @property
    def last_safety(self):
        """兼容旧 SafetyGuard 接口，返回质心检查的 raw SafetyResult。"""
        return self._last_result.com.raw if self._last_result else None

    @property
    def last_collision(self):
        """兼容旧 SafetyGuard 接口，返回碰撞检查的 raw CollisionDiagnostic。"""
        return self._last_result.collision.raw if self._last_result else None


# ═══════════════════════════════════════════════════════════════
#  独立模块测试（无需 ROS / 真机）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    快速验证：加载配置 + 预处理 URDF + 随机关节命令测试。
    运行方式：
        cd /home/cyb/F1_robot/src/python
        python3 f1_safety_modules.py [config_xml_path]
    """
    import sys
    import time as _time
    from pathlib import Path

    # 将父目录加入 sys.path，确保能导入 f1_safety_config
    _here = Path(__file__).parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))

    from f1_safety_config import load_config, print_config_summary

    cfg_path = sys.argv[1] if len(sys.argv) > 1 else str(_here / "f1_safety_config.xml")
    print(f"[Test] 加载配置: {cfg_path}")
    cfg = load_config(cfg_path)
    print_config_summary(cfg)

    # 预处理 URDF → MJCF（复用主程序的函数）
    # 由于本文件不包含 URDF 处理函数，直接导入主程序模块
    import importlib.util
    main_path = str(_here / "F1_safe_control_real_with_com.py")
    spec = importlib.util.spec_from_file_location("main_mod", main_path)
    main_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_mod)

    print("[Test] 预处理 URDF...")
    mjcf_path = main_mod._prepare_mjcf(cfg.model.urdf_path)
    print(f"[Test] MJCF: {mjcf_path}")

    print("[Test] 初始化 CombinedSafetyGuard...")
    guard = CombinedSafetyGuard.from_config(mjcf_path, cfg)
    print("[Test] 初始化完成")

    rng      = np.random.default_rng(0)
    ji_list  = guard.joint_infos
    arm_ji   = [ji for ji in ji_list if ji.name.startswith(("arm_L_", "arm_R_"))]

    total = reject = 0
    print("[Test] 开始随机命令测试（100 帧）...")
    t0 = _time.time()
    for _ in range(100):
        cmd = {ji.name: float(rng.uniform(ji.lower, ji.upper)) for ji in arm_ji}
        result = guard.check(cmd)
        total += 1
        if not result.safe:
            reject += 1

    elapsed = _time.time() - t0
    print(
        f"[Test] 完成 | 总帧={total} | 拒绝={reject} "
        f"| 耗时={elapsed*1000:.1f}ms | 单帧={elapsed/total*1000:.2f}ms"
    )
    print(f"[Test] 最后一帧: safe={result.safe}")
    print(f"  CoM  clearance={result.com.signed_clearance:.4f}m")
    print(f"  Col  clearance={result.collision.signed_clearance:.4f}m")
