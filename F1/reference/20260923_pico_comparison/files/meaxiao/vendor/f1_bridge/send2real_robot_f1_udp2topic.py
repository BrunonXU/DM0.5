#!/usr/bin/env python3
"""
udp_to_real_arm_full_fixed.py

修复版：解决 "Executor is already spinning" 错误 + 降低崩溃风险
"""

import socket
import numpy as np
import threading
import time
from typing import List

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from init_f1 import f1_init_api
from pico_f1.arm_output import ApprovedArmOutput
from pico_f1.config import load_control_config, arm_output_limits

import importlib.util
import os

IP_CONFIG_FILE_PATH = os.environ["PICO_F1_CONFIG"]

spec = importlib.util.spec_from_file_location("ip_config", IP_CONFIG_FILE_PATH)
ip_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ip_config)

LOCAL_IP = ip_config.local_ip
UDP_PORT = ip_config.port1

# ========================== 配置 ==========================
# LOCAL_IP = "192.0.2.13"   # ← 确认这是你当前的 IP
# UDP_PORT = 8888


class RealArmCommandSender:
    def __init__(self):
        self.cmd_real_joint_topic      = "/motion_ctl/joint_ctl"
        self.waist_control_topic       = "/control/waist/updown"

        self.left_joint_names = ['arm_l_j1', 'arm_l_j2', 'arm_l_j3', 'arm_l_j4',
                                 'arm_l_j5', 'arm_l_j6', 'arm_l_j7']
        self.right_joint_names = ['arm_r_j1', 'arm_r_j2', 'arm_r_j3', 'arm_r_j4',
                                  'arm_r_j5', 'arm_r_j6', 'arm_r_j7']
        self.all_joint_names = self.left_joint_names + self.right_joint_names

        self._node = None
        self.joint_publisher = None
        self._initialized = False
        self._spin_thread = None

        self.delta_angle_limit = 20  #机械臂关节比真正极值绝对值小3度
        config = load_control_config()
        self._arm_output_lock = threading.Lock()
        self._arm_output_stop = threading.Event()
        output_limits = arm_output_limits(config)
        self._arm_output = ApprovedArmOutput(**output_limits)
        print(f'ARM_OUTPUT_LIMITS velocity_deg_s={output_limits["max_velocity"]:g} '
              f'acceleration_deg_s2={output_limits["max_acceleration"]:g} '
              f'jerk_deg_s3={output_limits["max_jerk"]:g} target_hz=250')
        self._lazy_init_ros()
        self._arm_output_thread = threading.Thread(target=self._arm_output_loop,
                                                   name='ApprovedArmOutput250Hz', daemon=True)
        self._arm_output_thread.start()


    def _lazy_init_ros(self):
        if self._initialized:
            return

        if not rclpy.ok():
            rclpy.init()

        node_name = f"udp_to_real_arm_{int(time.time()*1000)}"
        self._node = Node(node_name)

        reliable_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST
        )

        self.joint_publisher = self._node.create_publisher(
            JointState, self.cmd_real_joint_topic, reliable_qos
        )


        self._waist_cmd_pub = self._node.create_publisher(Bool, self.waist_control_topic, reliable_qos)


        self._initialized = True
        print(f"[RealArmCommandSender] ROS2 初始化完成，节点：{node_name}")

        # 启动独立 spin 线程，不再在接收线程中调用 spin_once
        self._spin_thread = threading.Thread(target=self._spin_loop, daemon=True)
        self._spin_thread.start()

    def _spin_loop(self):
        """独立线程处理 ROS2 回调，避免与 publish 竞争 executor 锁"""
        while rclpy.ok() and self._node is not None:
            try:
                rclpy.spin_once(self._node, timeout_sec=0.01)
            except Exception:
                break

    def send_both_arms_and_grippers(self, 
                                    left_q: List[float], 
                                    right_q: List[float],
                                    left_grip: float,
                                    right_grip: float,
                                    max_effort: float = 15.0,
                                    gripper_flag : bool = True,
                                    arm_enabled=(True, True)) -> bool:
        """Stage only already-approved arm positions; output runs at 250 Hz."""
        if not self._initialized or self._node is None:
            return False
        with self._arm_output_lock:
            self._arm_output.push(left_q + right_q, time.monotonic(), arm_enabled)
        return True

    def pause_arm_stream(self):
        # The reset worker is the sole arm writer until a fresh post-reset
        # waypoint arrives. No pre-reset filter tail may survive the handoff.
        with self._arm_output_lock:
            self._arm_output.reset()

    def _arm_output_loop(self):
        period = .004
        next_tick = time.monotonic()
        report_start = next_tick
        count, max_gap, last_send = 0, 0., None
        while not self._arm_output_stop.is_set() and rclpy.ok():
            with self._arm_output_lock:
                # Timestamp after locking: a first waypoint may have been
                # seeded while this worker waited for the same lock.
                now = time.monotonic()
                q = self._arm_output.sample(now)
                if q is not None:
                    self._publish_arm_position(q.tolist())
                    if last_send is not None:
                        max_gap = max(max_gap, now - last_send)
                    last_send = now
                    count += 1
                else:
                    # Reset pauses are not active-loop scheduling failures.
                    last_send = None
                    report_start, count, max_gap = now, 0, 0.
            if now - report_start >= 5.:
                if count:
                    print(f'ARM_SERVO_OUTPUT hz={count/(now-report_start):.1f} '
                          f'max_gap_ms={max_gap*1000:.2f} target_hz=250')
                report_start, count, max_gap = now, 0, 0.
            next_tick += period
            if next_tick <= time.monotonic():
                next_tick = time.monotonic() + period
            self._arm_output_stop.wait(max(0., next_tick - time.monotonic()))

    def _publish_arm_position(self, joints):

        try:
            # 关节
            joint_msg = JointState()
            joint_msg.header.stamp = self._node.get_clock().now().to_msg()
            joint_msg.header.frame_id = ""
            joint_msg.name = self.all_joint_names
            joint_msg.position = joints
            self.joint_publisher.publish(joint_msg)
            return True
        except Exception as e:
            print(f"[发送异常] {e}")
            return False

    def publish_waist_cmd(self, up: bool):
        """发布腰部控制指令，up=True 升高，up=False 降低"""
        msg = Bool()
        msg.data = up
        self._waist_cmd_pub.publish(msg)

    def __del__(self):
        if hasattr(self, '_arm_output_stop'):
            self._arm_output_stop.set()
        if self._node:
            try:
                self._node.destroy_node()
            except:
                pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except:
                pass


# ========================== 主桥接类 ==========================
class UDPToRealArmBridge:
    def __init__(self):
        self.sender = RealArmCommandSender()
        self.receive_count = 0
        self.last_print_time = time.time()
        self.last_packet_monotonic = 0.0

        self.f1 = f1_init_api()
        self._reset_in_progress = threading.Event()
        self._reset_lock = threading.Lock()
        # Start receiving only after the reset API is fully initialized.
        self.recv_thread = threading.Thread(target=self.udp_recv_loop, daemon=True)
        self.recv_thread.start()
        print("🚀 UDP → Real Arm + Gripper 低延迟桥接程序已启动（已修复 executor 问题）")

    def _request_session_reset(self, reply=None):
        with self._reset_lock:
            if self._reset_in_progress.is_set():
                print("RESET_DUPLICATE_IGNORED already_in_progress=true")
                if reply is not None:
                    reply(False)
                return
            self._reset_in_progress.set()
        print("RESET_COMMAND_ACCEPTED immediate=true")
        if hasattr(self, 'sender'):
            self.sender.pause_arm_stream()

        def worker():
            success = False
            try:
                success = bool(self.f1.plan_and_move_session_pose(duration_sec=0.6))
                if not success:
                    print("ARMS_BODY_J3_RESET_BLOCKED")
            except Exception as exc:
                print(f"ARMS_BODY_J3_RESET_FAILED error={exc}")
            finally:
                self._reset_in_progress.clear()
                if reply is not None:
                    reply(success)

        threading.Thread(
            target=worker, daemon=True, name="SessionPoseReset"
        ).start()

    def udp_recv_loop(self):
        """UDP 接收 + 立即发送 + spin 全部放在同一个线程，避免 executor 冲突"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)  # 128KB 接收缓冲区，防止突发丢包
        s.settimeout(0.2)
        try:
            s.bind((LOCAL_IP, UDP_PORT))
        except Exception as e:
            print(f"❌ UDP bind 失败: {e} （可能是 IP 不正确或端口被占用）")
            return

        print(f"✅ UDP 监听启动 → {LOCAL_IP}:{UDP_PORT}")

        while True:
            try:
                data, addr = s.recvfrom(1024)
                frame = np.frombuffer(data, dtype=np.float64)

                self.receive_count += 1

                # 立即处理并发送
                if (len(frame) == 2 and np.all(np.isfinite(frame))
                        and frame[0] == 1 and addr[0] == '127.0.0.1'):
                    # Capture token/address by value for the reset worker.
                    def acknowledge(success, token=float(frame[1]), peer=addr):
                        s.sendto(np.asarray([token, float(success)],
                                           dtype=np.float64).tobytes(), peer)
                    self._request_session_reset(acknowledge)
                else:
                    self.process_and_send(frame)

                # 每50次打印一次状态
                if self.receive_count % 50 == 0:
                    now = time.time()
                    freq = 50 / (now - self.last_print_time + 1e-6)
                    print(f"📥 UDP 接收 | count={self.receive_count} | 频率≈{freq:.1f} Hz | 数据长度={len(frame)}")
                    self.last_print_time = now

            except TimeoutError:
                continue
            except Exception as e:
                print(f"UDP 循环异常: {e}")
                time.sleep(0.001)

    def limit_joint_left(self, left_deg):

        """
        左臂关节角度限位
        :param left_deg: 7个关节角度列表（度）
        :return: 限制后的7个关节角度列表（弧度），可直接传给 control_left_arm
        """
        limits_deg = [
            (-268.0, +88.0),  # J1 旋转      -90
            (-210.0, +30.0),  # J2 下臂      -（+90）
            (-88.0, +268.0),  # J3 上臂      +90
            (-150.0, + 55.0),  # J4 手腕旋转   -（+90）
            (-178.0, +178.0),  # J5 手腕摆动
            (- 60.0, + 60.0),  # J6 手腕回转    -
            (- 90.0, + 90.0),  # J7 手腕回转    -
        ]
        
        clamped_deg = []
        for i, deg in enumerate(left_deg):
            lo, hi = limits_deg[i]
            clamped = max(lo, min(hi+self.delta_angle_limit, deg-self.delta_angle_limit))
            if clamped != deg:
                print(f"⚠️ 左臂 J{i+1} 角度 {deg:.2f}° 超出限位 [{lo}, {hi}]，已裁剪为 {clamped:.2f}°")
            clamped_deg.append(clamped)
        

        return clamped_deg


    def limit_joint_right(self, right_deg):
        """
        右臂关节角度限位
        :param right_deg: 7个关节角度列表（度）
        :return: 限制后的7个关节角度列表，可直接传给 control_right_arm
        """
        limits_deg = [
            (-88.0, +268.0),  # J1 旋转      +90
            (-30.0, +210.0),  # J2 下臂      +90
            (-268.0, +88.0),  # J3 上臂      -90
            (-150.0, + 55.0),  # J4 手腕旋转   -（+90）
            (-178.0, +178.0),  # J5 手腕摆动
            (- 60.0, + 60.0),  # J6 手腕回转    -
            (- 90.0, + 90.0),  # J7 手腕回转    -
        ]
        
        clamped_deg = []
        for i, deg in enumerate(right_deg):
            lo, hi = limits_deg[i]
            clamped = max(lo, min(hi+self.delta_angle_limit, deg-self.delta_angle_limit))
            if clamped != deg:
                print(f"⚠️ 右臂 J{i+1} 角度 {deg:.2f}° 超出限位 [{lo}, {hi}]，已裁剪为 {clamped:.2f}°")
            clamped_deg.append(clamped)

        return clamped_deg


    def process_and_send(self, frame: np.ndarray):
        """解析 UDP 数据并立即下发（不再调用 spin_once，由独立 spin 线程处理）"""
        if not np.all(np.isfinite(frame)):
            print("[SAFETY WARN] dropped UDP frame containing NaN/Inf")
            return
        self.last_packet_monotonic = time.monotonic()
        if len(frame) in (16, 18):
            if self._reset_in_progress.is_set():
                return
            left_q   = frame[0:7].tolist()
            right_q  = frame[7:14].tolist()
            left_grip  = float(frame[14])
            right_grip = float(frame[15])
            enabled = (True, True)
            if len(frame) == 18:
                if not np.all(np.isin(frame[16:18], [0., 1.])):
                    print('[SAFETY WARN] invalid per-arm output enables')
                    return
                enabled = tuple(bool(x) for x in frame[16:18])
            self.sender.send_both_arms_and_grippers(left_q, right_q, left_grip, right_grip,
                                                  arm_enabled=enabled)

        elif len(frame) == 14:
            if self._reset_in_progress.is_set():
                return
            left_q   = frame[0:7].tolist()
            right_q  = frame[7:14].tolist()
            left_grip = 0.0
            right_grip = 0.0
            self.sender.send_both_arms_and_grippers(left_q, right_q, left_grip, right_grip, False)


        #长度为1时做一些特定动作，比如复位、升降腰等，
        elif len(frame) == 1:
            # 自定义规则，回home
            # # 端平4轴
            # 复位位姿，不是初始位置
            # target_left = [90, -90, -90, -90, 0, 0, 0]
            # target_right =  [-90, -90, 90, -90, 0, 0, 0]
            if frame[0] == 1:
                self._request_session_reset()
            #升腰
            elif frame[0] == 2:
                print("升高腰----------------")
                self.sender.publish_waist_cmd(True)
            #降腰
            elif frame[0] == 3:
                print("降低腰----------------")
                self.sender.publish_waist_cmd(False)

    def run(self):
        """主线程只保持程序不退出"""
        try:
            print("🔄 程序运行中... 请保持运行，按 Ctrl+C 退出")
            while True:
                time.sleep(0.1)   # 主线程轻睡眠，不参与 spin
        except KeyboardInterrupt:
            print("\n\n⏹️ 用户中断，程序退出")
        except Exception as e:
            print(f"主线程异常: {e}")
        finally:
            print("🛑 程序结束")


# ========================== 启动 ==========================
if __name__ == "__main__":
    bridge = UDPToRealArmBridge()
    bridge.run()
