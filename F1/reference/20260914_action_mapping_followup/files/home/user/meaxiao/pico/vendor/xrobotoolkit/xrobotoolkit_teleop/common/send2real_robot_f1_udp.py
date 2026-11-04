#!/usr/bin/env python3
"""
real_arm_command_sender.py
 
职责单一的类：向真实机械臂发送关节位置指令 + 夹爪指令 + 控制模式切换 + 拖动示教模式
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
import time
from typing import List, Optional, Dict, Tuple

# 标准消息类型
from sensor_msgs.msg import JointState
from control_msgs.msg import GripperCommand

# 自定义服务
from f1_msgs.srv import SetControlMode, SetDragMode   # 新增 SetDragMode
import numpy as np
import socket
import importlib.util
import os

IP_CONFIG_FILE_PATH = os.environ["PICO_F1_CONFIG"]


class RealArmCommandSender:
    """
    使用方式：
        sender = RealArmCommandSender()
        sender.change_control_mode(target_arm=1, mode=3)
        sender.change_drag_mode(target_arm=1, drag_type=1, duration_ms=0)
    """

    def __init__(self):
        # self.TARGET_IP = "192.0.2.13"
            # 动态导入指定路径的 ip_config.py
        spec = importlib.util.spec_from_file_location("ip_config", IP_CONFIG_FILE_PATH)
        ip_config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ip_config)
        
        self.TARGET_IP = ip_config.local_ip
        self.port_data = [ip_config.port1]
        self.port_singal_start_record_video = [ip_config.port2]

        # self.port_data = [8888]
        # self.port_singal_start_record_video = [8899] 

        # ---------------- 网络 ----------------
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)  # 增大发送缓冲区
        self.sock.setblocking(False)  # 非阻塞模式，避免网络抖动卡住主线程

    # qpos, np
    def send_upper_to_sim(self,qpos):
        """
        实时把上肢发给 MuJoCo
        """
        q = np.array(qpos)
        packet = q.astype(np.float64).tobytes()
        for port in self.port_data:  
            try:
                self.sock.sendto(packet, (self.TARGET_IP, port))
            except BlockingIOError:
                pass  # 发送缓冲区满，丢弃当前帧，不阻塞主线程       

    def send_start_video_signal(self,qpos):
        """
        通知启停录制视频
            - qpos = 1: 开始录制
            - qpos = 0: 停止录制
        """
        q = np.array(qpos)
        packet = q.astype(np.float64).tobytes()
        for i in range(3):
            for port in self.port_singal_start_record_video:  # 同一帧发两次
                self.sock.sendto(packet, (self.TARGET_IP, port)) 


    def angle_map(self,left_joint_rad,right_joint_rad):

        # 添加由于urdf不完全对称，导致的left初始逆解角度差异
        #left_joint_rad[0] += 0.026
        #left_joint_rad[1] -= 0.235
        #left_joint_rad[2] += 0.225
        #left_joint_rad[3] -= 0.002
        #left_joint_rad[4] -= 0.232
        #left_joint_rad[5] -= 0.024
        #left_joint_rad[6] -= 0.227

        #test
        # left_joint_rad[4] -= 0.175
 

        left_joint_deg = np.zeros(7, dtype=float)
        right_joint_deg = np.zeros(7, dtype=float)      
          
        left_joint_deg[0] =  np.rad2deg(left_joint_rad[0]) + 90
        left_joint_deg[1] =  -np.rad2deg(left_joint_rad[1]) - 90
        left_joint_deg[2] =  np.rad2deg(left_joint_rad[2]) - 90
        left_joint_deg[3] =  -np.rad2deg(left_joint_rad[3]) - 90
        left_joint_deg[4] =  np.rad2deg(left_joint_rad[4])       
        left_joint_deg[5] =  -np.rad2deg(left_joint_rad[5])
        left_joint_deg[6] =   np.rad2deg(left_joint_rad[6])

        right_joint_deg[0] =  np.rad2deg(right_joint_rad[0]) - 90
        right_joint_deg[1] =  np.rad2deg(right_joint_rad[1]) - 90
        right_joint_deg[2] =  np.rad2deg(right_joint_rad[2]) + 90
        right_joint_deg[3] =  np.rad2deg(right_joint_rad[3]) - 90
        right_joint_deg[4] =  -np.rad2deg(right_joint_rad[4])       
        right_joint_deg[5] =  -np.rad2deg(right_joint_rad[5])
        right_joint_deg[6] =  np.rad2deg(right_joint_rad[6])

        return left_joint_deg, right_joint_deg


            # target_left = [90, -90, -90, -90, 0, 0, 0]
            # target_right =  [-90, -90, 90, -90, 0, 0, 0]

    def home_position(self):
        # 长度为1，回home
        data = 1
        self.send_upper_to_sim(data)

    #用来发送一些单独的功能，比如腰部升起降落等
    def fun_send(self,int_data):
        self.send_upper_to_sim(int_data)

    def __del__(self):
        node = getattr(self, "_node", None)
        if node is not None:
            try:
                node.destroy_node()
            except:
                pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except:
                pass


# ====================== 测试代码 ======================
def main():
    send = RealArmCommandSender()
    print(f"🚀 开始以 20Hz 向 {send.TARGET_IP} 的端口 {send.port_data} 发送 7DOF 机械臂数据...")
    print("数据格式: [qpos(7) + qvel(7)]，共14个 float64")
    print("按 Ctrl+C 停止发送\n")

    count = 0
    last_print_time = time.time()
    


    try:
        while True:
            loop_start = time.perf_counter()

            # 随机生成 7自由度位置 + 7自由度速度 = 14个数据
            qpos = np.random.uniform(-np.pi, np.pi, 7).astype(np.float64)
            qvel = np.random.uniform(-2.0, 2.0, 7).astype(np.float64)
            q_grip = np.random.uniform(0.0, 100.0, 2).astype(np.float64)  # 夹爪开合度
            # 合并成14维数组
            data_14 = np.concatenate([qpos, qvel])

            data_16 = np.concatenate([qpos, qvel, q_grip])  # 如果需要夹爪数据，可以使用这个16维数组

            # 发送
            send.send_upper_to_sim(data_16)

            count += 1

            # 精确控制 20Hz
            elapsed = time.perf_counter() - loop_start
            sleep_time = max(0.0, 0.05 - elapsed)   # 50ms = 20Hz
            time.sleep(sleep_time)

            # 每秒打印一次实际频率
            if time.time() - last_print_time >= 1.0:
                print(f"📡 已发送 {count} 帧 | 实际频率 ≈ {count} Hz")
                count = 0
                last_print_time = time.time()

    except KeyboardInterrupt:
        print("\n\n⏹️ 程序已停止")
    finally:
        send.sock.close()
        print("Socket 已关闭")


# ================== 程序入口 ==================
if __name__ == "__main__":
    main()
