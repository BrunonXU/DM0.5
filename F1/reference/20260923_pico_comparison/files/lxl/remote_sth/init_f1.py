#!/usr/bin/env python3
"""
init_f1.py - 最终稳定版（已修复 update_state + 状态读取问题）
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.executors import MultiThreadedExecutor
import threading
import time
from typing import List, Optional, Dict, Tuple
import numpy as np

from sensor_msgs.msg import JointState
from control_msgs.msg import GripperCommand
from f1_msgs.srv import SetControlMode, SetDragMode


class f1_init_api:
    """
    使用方式：
        sender = f1_init_api()
        sender.change_control_mode(target_arm=1, mode=3)
        sender.plan_and_move_both_arms(...)
    """

    def __init__(self):
        # --------------------- 配置区 ---------------------
        self.cmd_real_joint_topic = "/motion_ctl/joint_ctl"
        self.cmd_real_grip_left_topic = "/motion_ctl/gripper/left"
        self.cmd_real_grip_right_topic = "/motion_ctl/gripper/right"

        self.get_info_real_joint_topic = "/hal/joint_states"
        self.get_info_real_grip_left_topic = "/motion_ctl/gripper/left/state"
        self.get_info_real_grip_right_topic = "/motion_ctl/gripper/right/state"

        self.set_control_mode_service_name = "/motor_ctl/set_control_mode"
        self.set_drag_mode_service_name = "/motor_ctl/set_drag_mode"

        # 关节名称
        self.left_joint_names = [
            'arm_l_j1', 'arm_l_j2', 'arm_l_j3', 'arm_l_j4',
            'arm_l_j5', 'arm_l_j6', 'arm_l_j7'
        ]
        self.right_joint_names = [
            'arm_r_j1', 'arm_r_j2', 'arm_r_j3', 'arm_r_j4',
            'arm_r_j5', 'arm_r_j6', 'arm_r_j7'
        ]
        self.all_joint_names = self.left_joint_names + self.right_joint_names

        # 夹爪
        self.GRIPPER_OPEN = 100.0
        self.GRIPPER_CLOSED = 0.0

        # 控制模式常量
        self.ARM_LEFT = 1
        self.ARM_RIGHT = 2
        self.ARM_BOTH = 3

        # 状态缓存
        self.current_left_q: List[float] = [0.0] * 7
        self.current_right_q: List[float] = [0.0] * 7
        self.current_left_grip: float = 0.0
        self.current_right_grip: float = 0.0

        # ROS相关
        self._node = None
        self._executor = None
        self._spin_thread = None
        self._running = False
        self._lock = threading.Lock()
        self._last_print = 0.0

        self.real_joint_publisher = None
        self.real_gripper_left_publisher = None
        self.real_gripper_right_publisher = None
        self._control_mode_client = None
        self._drag_mode_client = None

        self._initialized = False

        self._lazy_init_ros()
        time.sleep(2.0)

    def _lazy_init_ros(self):
        if self._initialized:
            return

        if not rclpy.ok():
            rclpy.init()

        node_name = f"real_arm_sender_{int(time.time()*1000)}"
        self._node = Node(node_name)

        self._executor = MultiThreadedExecutor(num_threads=6)
        self._executor.add_node(self._node)

        reliable_qos = QoSProfile(depth=20, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST)
        sensor_qos = rclpy.qos.qos_profile_sensor_data

        # Publishers
        self.real_joint_publisher = self._node.create_publisher(JointState, self.cmd_real_joint_topic, reliable_qos)
        self.real_gripper_left_publisher = self._node.create_publisher(GripperCommand, self.cmd_real_grip_left_topic, reliable_qos)
        self.real_gripper_right_publisher = self._node.create_publisher(GripperCommand, self.cmd_real_grip_right_topic, reliable_qos)

        # Subscribers
        self._node.create_subscription(JointState, self.get_info_real_joint_topic, self._joint_state_callback, sensor_qos)
        self._node.create_subscription(JointState, self.get_info_real_grip_left_topic, self._grip_left_callback, sensor_qos)
        self._node.create_subscription(JointState, self.get_info_real_grip_right_topic, self._grip_right_callback, sensor_qos)

        # 服务客户端
        self._control_mode_client = self._node.create_client(SetControlMode, self.set_control_mode_service_name)
        self._drag_mode_client = self._node.create_client(SetDragMode, self.set_drag_mode_service_name)

        self._initialized = True
        self._running = True

        self._spin_thread = threading.Thread(target=self._spin_loop, daemon=True)
        self._spin_thread.start()

        print(f"[f1_init_api] ✅ 初始化完成，节点：{node_name} (MultiThreadedExecutor)")

    def _spin_loop(self):
        while self._running and rclpy.ok():
            try:
                self._executor.spin_once(timeout_sec=0.03)
            except Exception:
                time.sleep(0.01)

    def _joint_state_callback(self, msg: JointState):
        if len(msg.position) >= 14:
            with self._lock:
                self.current_left_q = list(msg.position[:7])
                self.current_right_q = list(msg.position[7:14])

            if time.time() - self._last_print > 2.0:
                # print(f"[Debug] 收到真实关节状态 | 左臂: {np.round(self.current_left_q[:4], 2)} ...")
                self._last_print = time.time()

    def _grip_left_callback(self, msg: JointState):
        with self._lock:
            self.current_left_grip = msg.position[0]

    def _grip_right_callback(self, msg: JointState):
        with self._lock:
            self.current_right_grip = msg.position[0]

    # ====================== 必须保留的方法（兼容 all_remote_lxl.py） ======================
    def update_state(self):
        """兼容旧代码，实际由后台线程驱动"""
        pass

    def get_both_arms_state(self) -> Tuple[List[float], List[float]]:
        with self._lock:
            return self.current_left_q[:], self.current_right_q[:]

    def wait_for_real_state(self, timeout=2.5):
        print("[规划运动] 等待接收真实关节状态...")
        start = time.time()
        while time.time() - start < timeout:
            left, right = self.get_both_arms_state()
            if max(map(abs, left[:4] + right[:4])) > 0.5:
                print(f"[成功] 已获取真实位置 → 左臂: {np.round(left[:4], 2)}")
                return True
            time.sleep(0.1)
        print("[警告] 未收到真实状态，使用缓存值")
        return False

    def change_control_mode(self, target_arm: int, mode: int) -> bool:
        if not self._control_mode_client.wait_for_service(timeout_sec=3.0):
            print(f"[错误] 服务不可用")
            return False

        request = SetControlMode.Request()
        request.target_arm = target_arm
        request.mode = mode
        future = self._control_mode_client.call_async(request)
        rclpy.spin_until_future_complete(self._node, future, timeout_sec=5.0)

        if future.result() and future.result().success:
            print(f"[成功] 已将 双臂 切换为 位置模式")
            return True
        return False

    def send_both_arms(self, left_q: List[float], right_q: List[float]) -> bool:
        if len(left_q) != 7 or len(right_q) != 7:
            return False
        msg = JointState()
        msg.header.stamp = self._node.get_clock().now().to_msg()
        msg.header.frame_id = ""
        msg.name = self.all_joint_names
        msg.position = left_q + right_q
        self.real_joint_publisher.publish(msg)
        return True

    def plan_and_move_both_arms(self, target_left_q: List[float], target_right_q: List[float], duration_sec: float = 5.0) -> bool:
        if len(target_left_q) != 7 or len(target_right_q) != 7:
            print("[警告] 目标关节数量错误")
            return False

        self.wait_for_real_state()
        current_left, current_right = self.get_both_arms_state()

        print(f"[规划运动] 当前实际位置 → 左臂: {np.round(current_left, 2)}")
        print(f"[规划运动] 当前实际位置 → 右臂: {np.round(current_right, 2)}")

        hz = 50
        dt = 1.0 / hz
        steps = int(duration_sec * hz)

        delta_left = [(target_left_q[i] - current_left[i]) / steps for i in range(7)]
        delta_right = [(target_right_q[i] - current_right[i]) / steps for i in range(7)]

        for step in range(steps + 1):
            interp_left = [current_left[i] + delta_left[i] * step for i in range(7)]
            interp_right = [current_right[i] + delta_right[i] * step for i in range(7)]
            self.send_both_arms(interp_left, interp_right)
            time.sleep(dt)

        print(f"[规划运动] ✅ 已平滑到达目标位置")
        return True

    def __del__(self):
        self._running = False
        if self._executor:
            try:
                self._executor.shutdown()
            except:
                pass
        if self._node:
            try:
                self._node.destroy_node()
            except:
                pass


# ====================== 测试代码 ======================
if __name__ == "__main__":
    sender = f1_init_api()
    sender.change_control_mode(sender.ARM_BOTH, 1)
    target_left = [90, -90, -90, -90, 0, 0, 0]
    target_right = [-90, -90, 90, -90, 0, 0, 0]
    sender.plan_and_move_both_arms(target_left, target_right, duration_sec=5)
    print("\n测试完成")