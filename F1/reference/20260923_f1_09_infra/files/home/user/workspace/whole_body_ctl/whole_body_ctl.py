#!/usr/bin/env python3
"""
全身一键复位/位姿切换控制器

运动模式:
    1. 分组模式（默认）: 左臂 → 右臂 → 头部 → 躯干上半 → 躯干下半
    2. 整体模式 (--whole): 所有22个关节同时运动

参数:
    --dry-run  只打印不实际发布
    --whole    所有关节同时运动（需配置所有关节目标角度）
"""

import argparse
import configparser
import math
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Int32


# ============================================================
# 关节分组（按运动顺序定义）
# ============================================================
ARM_L_JOINTS = ["arm_l_j1", "arm_l_j2", "arm_l_j3", "arm_l_j4", "arm_l_j5", "arm_l_j6", "arm_l_j7"]
ARM_R_JOINTS = ["arm_r_j1", "arm_r_j2", "arm_r_j3", "arm_r_j4", "arm_r_j5", "arm_r_j6", "arm_r_j7"]
HEAD_JOINTS = ["head_j1", "head_j2", "head_j3"]
BODY_JOINTS = ["body_j1", "body_j2", "body_j3", "body_j4", "body_j5"]

BODY_TOP_JOINTS = ["body_j1", "body_j2"]
BODY_BOTTOM_JOINTS = ["body_j3", "body_j4", "body_j5"]

# 所有关节（22个）
ALL_JOINT_NAMES = ARM_L_JOINTS + ARM_R_JOINTS + HEAD_JOINTS + BODY_JOINTS


# ============================================================
# 关节限位（度）
# ============================================================
JOINT_LIMITS = {
    "arm_l_j1": (-178.0, 178.0),
    "arm_l_j2": (-94.0, 120.0),
    "arm_l_j3": (-178.0, 178.0),
    "arm_l_j4": (-145.0, 60.0),
    "arm_l_j5": (-178.0, 178.0),
    "arm_l_j6": (-60.0, 60.0),
    "arm_l_j7": (-90.0, 90.0),
    "arm_r_j1": (-178.0, 178.0),
    "arm_r_j2": (-94.0, 120.0),
    "arm_r_j3": (-178.0, 178.0),
    "arm_r_j4": (-145.0, 60.0),
    "arm_r_j5": (-178.0, 178.0),
    "arm_r_j6": (-60.0, 60.0),
    "arm_r_j7": (-90.0, 90.0),
    "head_j1": (-40.0, 40.0),
    "head_j2": (-25.0, 25.0),
    "head_j3": (-15.0, 15.0),
    "body_j1": (-130.0, 130.0),
    "body_j2": (-10.0, 10.0),
    "body_j3": (-70.0, 0.0),
    "body_j4": (-95.0, 0.0),
    "body_j5": (-40.0, 0.0),
}


# ============================================================
# 工具函数
# ============================================================
def s_curve(t: float) -> float:
    return 3.0 * t * t - 2.0 * t * t * t


def parse_pose_config(config: configparser.ConfigParser, pose_id: int) -> Dict[str, Optional[List[float]]]:
    section = f"pose_{pose_id}"
    if not config.has_section(section):
        return None

    result = {}

    for key in ["arm_l", "arm_r", "head", "body"]:
        if config.has_option(section, key):
            values = [float(x.strip()) for x in config.get(section, key).split(',') if x.strip()]
            result[key] = values
        else:
            result[key] = None

    return result


def validate_angles(joint_names: List[str], angles: List[float]) -> Tuple[bool, str]:
    for name, angle in zip(joint_names, angles):
        if name in JOINT_LIMITS:
            min_val, max_val = JOINT_LIMITS[name]
            if angle < min_val or angle > max_val:
                return False, f"{name} 角度 {angle:.2f} 超出限位 [{min_val}, {max_val}]"
    return True, ""


def get_target_dict(pose: Dict) -> Dict[str, float]:
    """将分组配置展开为关节名→角度字典"""
    result = {}
    mapping = {
        "arm_l": ARM_L_JOINTS,
        "arm_r": ARM_R_JOINTS,
        "head": HEAD_JOINTS,
        "body": BODY_JOINTS,
    }
    for key, joints in mapping.items():
        if pose.get(key) is not None:
            for name, angle in zip(joints, pose[key]):
                result[name] = angle
    return result


# ============================================================
# 主控制器类
# ============================================================
class WholeBodyController(Node):
    def __init__(self, dry_run: bool = False, whole_mode: bool = False):
        super().__init__('whole_body_controller')

        self.dry_run = dry_run
        self.whole_mode = whole_mode

        script_dir = Path(__file__).parent
        config_path = script_dir / 'whole_body_config.ini'

        if not config_path.exists():
            self.get_logger().error(f"配置文件不存在: {config_path}")
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        self.interpolation_steps = self.config.getint('motion', 'interpolation_steps', fallback=500)
        self.segment_duration_s = self.config.getfloat('motion', 'segment_duration_s', fallback=3.0)

        self.current_positions: Dict[str, float] = {}
        self.is_moving = False
        self.pending_pose_id: Optional[int] = None

        # 运动状态
        self.current_group_name: str = ""
        self.current_group_joints: List[str] = []
        self.current_group_start: List[float] = []
        self.current_group_end: List[float] = []
        self.current_group_step: int = 0
        self.current_group_total_steps: int = 0

        # 整体模式：所有关节同时运动
        self.whole_start: List[float] = []
        self.whole_end: List[float] = []
        self.whole_joints: List[str] = []
        self.whole_step: int = 0
        self.whole_total_steps: int = 0

        self.motion_queue: List[Dict] = []

        self.log_file = script_dir / 'whole_body_ctl.log'

        self.publisher = self.create_publisher(JointState, '/motion_ctl/joint_ctl', 10)
        self.joint_state_sub = self.create_subscription(JointState, '/hal/joint_states', self.joint_state_callback, 10)
        self.command_sub = self.create_subscription(Int32, '/motion_ctl/whole_ctl', self.command_callback, 10)

        self.timer = self.create_timer(0.005, self.publish_next_point)

        mode_text = "DRY-RUN模式(只打印不发布)" if self.dry_run else "正常模式"
        mode_text += ", 整体模式" if self.whole_mode else ", 分组模式"
        self.get_logger().info(f"全身姿态控制器已启动 [{mode_text}], 配置: {config_path}")
        self._log_to_file("INFO", f"控制器启动 [{mode_text}]")

    def joint_state_callback(self, msg: JointState):
        for name, position in zip(msg.name, msg.position):
            if name in ALL_JOINT_NAMES:
                self.current_positions[name] = position

    def command_callback(self, msg: Int32):
        pose_id = msg.data
        self.get_logger().info(f"收到姿态切换命令: pose_{pose_id}")

        if self.is_moving:
            self.get_logger().warn(f"正在运动中，忽略命令 pose_{pose_id}")
            self.pending_pose_id = pose_id
            return

        self.start_motion(pose_id)

    def start_motion(self, pose_id: int):
        missing = [name for name in ALL_JOINT_NAMES if name not in self.current_positions]
        if missing:
            # self.get_logger().error(f"缺少关节状态: {missing}")
            self._log_to_file("ERROR", f"缺少关节状态: {missing}")
            return

        try:
            pose = parse_pose_config(self.config, pose_id)
            if pose is None:
                self.get_logger().error(f"姿态 pose_{pose_id} 未定义")
                self._log_to_file("ERROR", f"姿态 pose_{pose_id} 未定义")
                return
        except Exception as e:
            self.get_logger().error(f"解析姿态配置失败: {e}")
            self._log_to_file("ERROR", f"解析姿态配置失败: {e}")
            return

        # 验证所有目标角度
        for group_name, joints, key in [
            ("左臂", ARM_L_JOINTS, "arm_l"),
            ("右臂", ARM_R_JOINTS, "arm_r"),
            ("头部", HEAD_JOINTS, "head"),
            ("躯干", BODY_JOINTS, "body"),
        ]:
            if pose.get(key) is not None:
                ok, err = validate_angles(joints, pose[key])
                if not ok:
                    self.get_logger().error(f"{group_name} {err}")
                    self._log_to_file("ERROR", f"{group_name} {err}")
                    return

        # ============================================================
        # 整体模式: 所有关节同时运动
        # ============================================================
        if self.whole_mode:
            self._start_whole_motion(pose_id, pose)
            return

        # ============================================================
        # 分组模式: 按顺序运动
        # ============================================================
        self._start_group_motion(pose_id, pose)

    def _start_whole_motion(self, pose_id: int, pose: Dict):
        """整体模式：所有22个关节同时运动"""
        target = get_target_dict(pose)

        # 检查是否所有关节都有目标值
        missing_targets = [name for name in ALL_JOINT_NAMES if name not in target]
        if missing_targets:
            self.get_logger().error(f"整体模式需要所有关节目标值，缺少: {missing_targets}")
            self._log_to_file("ERROR", f"整体模式缺少目标: {missing_targets}")
            return

        # 准备起始和目标
        self.whole_joints = ALL_JOINT_NAMES
        self.whole_start = [self.current_positions.get(name, 0.0) for name in ALL_JOINT_NAMES]
        self.whole_end = [target[name] for name in ALL_JOINT_NAMES]
        self.whole_step = 0
        self.whole_total_steps = self.interpolation_steps

        # 检查是否有变化
        has_change = False
        for s, e in zip(self.whole_start, self.whole_end):
            if abs(e - s) > 0.5:
                has_change = True
                break

        if not has_change:
            self.get_logger().info("所有关节已在目标位置，无需运动")
            return

        # 打印运动计划
        self._print_whole_plan(pose_id, target)

        self.is_moving = True
        self.pending_pose_id = None
        self._log_pose_info(pose_id, pose)

        self.get_logger().info(
            f"开始整体运动: {len(self.whole_joints)}个关节, {self.segment_duration_s}s"
        )

    def _start_group_motion(self, pose_id: int, pose: Dict):
        """分组模式：按顺序运动"""
        self.motion_queue = []

        # 1. 左臂
        if pose["arm_l"] is not None:
            current = [self.current_positions.get(j, 0.0) for j in ARM_L_JOINTS]
            target = pose["arm_l"]
            if any(abs(c - t) > 0.5 for c, t in zip(current, target)):
                self.motion_queue.append({
                    "name": "左臂",
                    "joints": ARM_L_JOINTS,
                    "start": current,
                    "end": target,
                })
            else:
                self.get_logger().info("左臂已在目标位置，跳过")

        # 2. 右臂
        if pose["arm_r"] is not None:
            current = [self.current_positions.get(j, 0.0) for j in ARM_R_JOINTS]
            target = pose["arm_r"]
            if any(abs(c - t) > 0.5 for c, t in zip(current, target)):
                self.motion_queue.append({
                    "name": "右臂",
                    "joints": ARM_R_JOINTS,
                    "start": current,
                    "end": target,
                })
            else:
                self.get_logger().info("右臂已在目标位置，跳过")

        # 3. 头部
        if pose["head"] is not None:
            current = [self.current_positions.get(j, 0.0) for j in HEAD_JOINTS]
            target = pose["head"]
            if any(abs(c - t) > 0.5 for c, t in zip(current, target)):
                self.motion_queue.append({
                    "name": "头部",
                    "joints": HEAD_JOINTS,
                    "start": current,
                    "end": target,
                })
            else:
                self.get_logger().info("头部已在目标位置，跳过")

        # 4. 躯干上半
        if pose["body"] is not None:
            current_top = [self.current_positions.get("body_j1", 0.0), self.current_positions.get("body_j2", 0.0)]
            target_top = pose["body"][:2]
            if any(abs(c - t) > 0.5 for c, t in zip(current_top, target_top)):
                self.motion_queue.append({
                    "name": "躯干上半",
                    "joints": BODY_TOP_JOINTS,
                    "start": current_top,
                    "end": target_top,
                })
            else:
                self.get_logger().info("躯干上半已在目标位置，跳过")

            # 5. 躯干下半
            current_bottom = [
                self.current_positions.get("body_j3", 0.0),
                self.current_positions.get("body_j4", 0.0),
                self.current_positions.get("body_j5", 0.0),
            ]
            target_bottom = pose["body"][2:]
            if any(abs(c - t) > 0.5 for c, t in zip(current_bottom, target_bottom)):
                self.motion_queue.append({
                    "name": "躯干下半",
                    "joints": BODY_BOTTOM_JOINTS,
                    "start": current_bottom,
                    "end": target_bottom,
                })
            else:
                self.get_logger().info("躯干下半已在目标位置，跳过")

        if not self.motion_queue:
            self.get_logger().info("所有关节已在目标位置，无需运动")
            return

        # 打印运动计划
        self._print_group_plan(pose_id, pose)

        self.is_moving = True
        self.pending_pose_id = None
        self._log_pose_info(pose_id, pose)
        self._next_group()

    def _print_whole_plan(self, pose_id: int, target: Dict[str, float]):
        """打印整体模式运动计划"""
        print("\n" + "=" * 70)
        print(f"  整体运动计划: pose_{pose_id} (所有22个关节同时运动)")
        print("=" * 70)
        print(f"  总关节数: {len(self.whole_joints)}")
        print(f"  运动时长: {self.segment_duration_s}s")
        print(f"  插值步数: {self.interpolation_steps}")
        print("")
        print("  关节目标:")
        for name in ALL_JOINT_NAMES:
            current = self.current_positions.get(name, 0.0)
            end = target.get(name, current)
            delta = end - current
            status = " (不变)" if abs(delta) < 0.5 else f" (变化 {delta:+.2f})"
            print(f"    {name}: {current:>8.2f} -> {end:>8.2f}{status}")
        print("=" * 70)

        if self.dry_run:
            print("[DRY-RUN] 不会实际发布，仅打印验证")
        print("")

    def _print_group_plan(self, pose_id: int, pose: Dict):
        """打印分组模式运动计划"""
        print("\n" + "=" * 70)
        print(f"  分组运动计划: pose_{pose_id}")
        print("=" * 70)
        print(f"  运动顺序 (共 {len(self.motion_queue)} 组):")
        for i, group in enumerate(self.motion_queue, 1):
            print(f"    {i}. {group['name']}: {len(group['joints'])}个关节")
            for j, name in enumerate(group['joints']):
                start = group['start'][j]
                end = group['end'][j]
                delta = end - start
                status = " (不变)" if abs(delta) < 0.5 else f" (变化 {delta:+.2f})"
                print(f"       {name}: {start:>8.2f} -> {end:>8.2f}{status}")
        print("=" * 70)

        if self.dry_run:
            print("[DRY-RUN] 不会实际发布，仅打印验证")
        print("")

    def _next_group(self):
        if not self.motion_queue:
            self.is_moving = False
            self.get_logger().info("所有分组运动完成")
            self._log_to_file("INFO", "所有分组运动完成")

            if self.pending_pose_id is not None:
                next_id = self.pending_pose_id
                self.pending_pose_id = None
                self.get_logger().info(f"执行待处理命令: pose_{next_id}")
                self.start_motion(next_id)
            return

        group = self.motion_queue.pop(0)
        self.current_group_name = group["name"]
        self.current_group_joints = group["joints"]
        self.current_group_start = group["start"]
        self.current_group_end = group["end"]
        self.current_group_step = 0
        self.current_group_total_steps = self.interpolation_steps

        self.get_logger().info(
            f"开始 {self.current_group_name}: "
            f"{len(self.current_group_joints)}个关节, {self.segment_duration_s}s"
        )

    def publish_next_point(self):
        if not self.is_moving:
            return

        # ============================================================
        # 整体模式
        # ============================================================
        if self.whole_mode:
            if self.whole_step > self.whole_total_steps:
                self.is_moving = False
                self.get_logger().info("整体运动完成")
                self._log_to_file("INFO", "整体运动完成")
                if self.pending_pose_id is not None:
                    next_id = self.pending_pose_id
                    self.pending_pose_id = None
                    self.get_logger().info(f"执行待处理命令: pose_{next_id}")
                    self.start_motion(next_id)
                return

            t = self.whole_step / self.whole_total_steps
            s = s_curve(t)

            msg = JointState()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = 'base_link'
            msg.name = self.whole_joints
            msg.position = []

            for i, name in enumerate(self.whole_joints):
                start = self.whole_start[i]
                end = self.whole_end[i]
                msg.position.append(start + (end - start) * s)

            msg.velocity = [0.0] * len(msg.name)
            msg.effort = [0.0] * len(msg.name)

            if self.dry_run:
                if self.whole_step % max(1, self.whole_total_steps // 20) == 0:
                    progress = self.whole_step * 100 // self.whole_total_steps
                    print(f"[DRY-RUN] 整体运动 [{progress:3}%] step={self.whole_step}")
                    for name, pos in zip(msg.name, msg.position):
                        print(f"           {name}: {pos:.2f}")
                    print("")
            else:
                self.publisher.publish(msg)

            self.whole_step += 1

            if not self.dry_run:
                if self.whole_step % max(1, self.whole_total_steps // 10) == 0:
                    progress = self.whole_step * 100 // self.whole_total_steps
                    self.get_logger().info(
                        f"整体运动 [{progress:3}%] {self.whole_step}/{self.whole_total_steps}"
                    )
            return

        # ============================================================
        # 分组模式
        # ============================================================
        if self.current_group_step > self.current_group_total_steps:
            self._next_group()
            return

        t = self.current_group_step / self.current_group_total_steps
        s = s_curve(t)

        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'base_link'
        msg.name = list(self.current_group_joints)
        msg.position = []

        for i, name in enumerate(self.current_group_joints):
            start = self.current_group_start[i]
            end = self.current_group_end[i]
            msg.position.append(start + (end - start) * s)

        msg.velocity = [0.0] * len(msg.name)
        msg.effort = [0.0] * len(msg.name)

        if self.dry_run:
            if self.current_group_step % max(1, self.whole_total_steps // 20) == 0:
                progress = self.current_group_step * 100 // self.current_group_total_steps
                print(f"[DRY-RUN] {self.current_group_name} [{progress:3}%] step={self.current_group_step}")
                for name, pos in zip(msg.name, msg.position):
                    print(f"           {name}: {pos:.2f}")
                print("")
        else:
            self.publisher.publish(msg)

        self.current_group_step += 1

        if not self.dry_run:
            if self.current_group_step % max(1, self.whole_total_steps // 10) == 0:
                progress = self.current_group_step * 100 // self.current_group_total_steps
                self.get_logger().info(
                    f"  {self.current_group_name} [{progress:3}%] "
                    f"{self.current_group_step}/{self.current_group_total_steps}"
                )

    def _log_to_file(self, level: str, message: str):
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_file, 'a') as f:
                f.write(f"[{timestamp}] [{level}] {message}\n")
        except Exception:
            pass

    def _log_pose_info(self, pose_id: int, pose: Dict):
        lines = [f"姿态切换: pose_{pose_id}"]
        for key in ["arm_l", "arm_r", "head", "body"]:
            if pose.get(key) is not None:
                lines.append(f"  {key}: {pose[key]}")
        self._log_to_file("INFO", "\n".join(lines))


def main(args=None):
    parser = argparse.ArgumentParser(description='全身姿态控制器')
    parser.add_argument('--dry-run', action='store_true',
                        help='只打印验证，不实际发布控制命令')
    parser.add_argument('--whole', action='store_true',
                        help='所有22个关节同时运动（需配置所有关节目标角度）')
    parsed_args, unknown = parser.parse_known_args(args)

    rclpy.init(args=args)
    node = WholeBodyController(
        dry_run=parsed_args.dry_run,
        whole_mode=parsed_args.whole
    )

    def signal_handler(sig, frame):
        node.get_logger().info("正在停止...")
        node.destroy_node()
        rclpy.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()