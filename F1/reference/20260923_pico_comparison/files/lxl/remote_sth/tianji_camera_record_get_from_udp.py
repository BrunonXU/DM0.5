#!/usr/bin/env python3
"""
udp_controlled_bag_recorder.py
使用 UDP 信号（端口 8899）控制开始/停止录制：
- ROS2 bag 录制（关节状态、夹爪状态、双臂末端位姿、相机压缩图像）
"""

import time
import threading
import socket
import os 
import subprocess
import signal
from typing import Optional, List

import numpy as np
import rclpy
from rclpy.node import Node
import rclpy.executors

import importlib.util
IP_CONFIG_FILE_PATH = "/home/user/lxl/ip_config.py"

# ==================== 配置：bag 录制话题 ====================
BAG_RECORD_TOPICS = [
    "/hal/joint_states",
    "/motion_ctl/gripper/left/state",
    "/motion_ctl/gripper/right/state",
    "/state/right_arm/tcp_pos",
    "/state/left_arm/tcp_pos",

    "/camera/left_wrist/color/image_raw/compressed",
    "/camera/right_wrist/color/image_raw/compressed",
    "/camera/head/color/image_raw/compressed",

    "/lead/joint_states",
    "/motion_ctl/gripper/left",
    "/motion_ctl/gripper/right",
    '/lead/left_tcp',
    '/lead/right_tcp',
]
BAG_BASE_DIR = os.path.expanduser("~/lxl/pico_data/all_data")
# ===========================================================


class BagRecorderManager:
    """管理 ROS2 bag 录制子进程"""
    def __init__(self, base_dir=BAG_BASE_DIR, topics=BAG_RECORD_TOPICS):
        self.base_dir = base_dir
        self.topics = topics
        self.process = None
        self.is_recording = False
        os.makedirs(self.base_dir, exist_ok=True)

    def _get_next_episode_index(self) -> int:
        max_idx = -1
        if not os.path.exists(self.base_dir):
            return 0
        for filename in os.listdir(self.base_dir):
            if filename.startswith("episode_"):
                try:
                    idx = int(filename[8:14])
                    max_idx = max(max_idx, idx)
                except:
                    continue
        return max_idx + 1

    def start_recording(self):
        if self.is_recording:
            print("⚠️ bag 录制已经在进行中，忽略重复开始")
            return False

        episode_idx = self._get_next_episode_index()
        bag_name = f"episode_{episode_idx:06d}"
        bag_path = os.path.join(self.base_dir, bag_name)

        # 构建命令
        cmd = [
            "ros2", "bag", "record",
            "--storage", "mcap",
            "-o", bag_path,
            *self.topics
        ]
        print(f"🔴 启动 bag 录制: {' '.join(cmd)}")
        try:
            # 使用 Popen 启动子进程，不阻塞
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.is_recording = True
            print(f"🚀 开始 bag 录制 Episode {episode_idx:06d}")
            print(f"📁 保存路径: {bag_path}")
            return True
        except Exception as e:
            print(f"❌ 启动 bag 录制失败: {e}")
            return False

    def stop_recording(self):
        if not self.is_recording or self.process is None:
            return

        print("⏹️ 正在停止 bag 录制...")
        # 发送 SIGINT 信号，让 ros2 bag record 正常结束
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None
        self.is_recording = False
        print("✅ bag 录制已停止")


class UDPBagRecorderController(Node):
    def __init__(self):
        super().__init__('udp_bag_recorder_controller')

        self.bag_manager = BagRecorderManager()

        spec = importlib.util.spec_from_file_location("ip_config", IP_CONFIG_FILE_PATH)
        ip_config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ip_config)
        self.LOCAL_IP = ip_config.local_ip
        self.UDP_PORT = ip_config.port2

        # UDP 接收线程
        self.udp_thread = threading.Thread(target=self._udp_listen_loop, daemon=True)
        self.udp_thread.start()

        self.get_logger().info('✅ UDP控制 bag 录制器已启动')
        self.get_logger().info(f'   UDP 监听端口: {self.LOCAL_IP}:{self.UDP_PORT} （1=开始, 0=停止）')

    def _udp_listen_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.LOCAL_IP, self.UDP_PORT))
            print(f"✅ UDP 信号监听启动 → {self.LOCAL_IP}:{self.UDP_PORT}")
        except Exception as e:
            print(f"❌ UDP bind 失败: {e}")
            return

        while True:
            try:
                data, addr = s.recvfrom(1024)
                frame = np.frombuffer(data, dtype=np.float64)
                if len(frame) >= 1:
                    signal = int(round(frame[0]))
                    if signal == 1:
                        self.get_logger().info(f'🚀 收到 UDP 开始录制信号 (来自 {addr})')
                        self._start_recording()
                    elif signal == 0:
                        self.get_logger().info(f'⏹️ 收到 UDP 停止录制信号 (来自 {addr})')
                        self._stop_recording()
            except Exception as e:
                print(f"UDP 信号处理异常: {e}")
                time.sleep(0.01)

    def _start_recording(self):
        if self.bag_manager.is_recording:
            self.get_logger().warn("bag 录制已经在进行中，忽略开始信号")
            return

        self.bag_manager.start_recording()

    def _stop_recording(self):
        self.bag_manager.stop_recording()

    def destroy_node(self):
        self._stop_recording()
        super().destroy_node()


def main(args=None):
    # 不重复 init，由 all_remote_lxl.py 统一管理
    if not rclpy.ok():
        rclpy.init(args=args)
    node = UDPBagRecorderController()
    # 使用独立 Executor，避免与 UDP 桥接的 spin 线程冲突
    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(node)
    try:
        while rclpy.ok():
            executor.spin_once(timeout_sec=0.1)
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断，程序退出")
    except Exception as e:
        print(f"程序异常: {e}")
    finally:
        executor.remove_node(node)
        executor.shutdown()
        node.destroy_node()


if __name__ == '__main__':
    main()
