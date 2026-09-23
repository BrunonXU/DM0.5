#!/usr/bin/env python3
"""
f1_full_launcher.py
最终修复版 - 彻底解决 ROS2 多重初始化问题
"""

import threading
import time
import sys
import os

import rclpy
 
# ====================== 导入控制模块 ======================
import send2real_robot_f1_udp2topic


import ip_config

def safe_rclpy_init():
    """确保 rclpy 只初始化一次"""
    if not rclpy.ok():
        rclpy.init()
        print("✅ ROS2 已初始化 (统一管理)")
    else:
        print("✅ ROS2 已处于初始化状态")


def validate_session_pose():
    """Validate the captured session pose without creating a duplicate ROS API."""
    print("🔧 [1/3] 启动 init_f1 - 读取本次会话初始位姿（不运动）...")
    try:
        safe_rclpy_init()
        from pico_f1.session_pose import load_session_pose
        session = load_session_pose()
        print(
            f"SESSION_INITIAL_POSE_READY joints={len(session['name'])} "
            f"path={session['path']} startup_motion=false\n"
        )
        return True
    except Exception as e:
        print(f"❌ init_f1 失败: {e}")
        return False


def run_udp_to_real_arm():
    """2. UDP 机械臂控制"""
    print("📡 [2/3] 启动 UDP → 真实机械臂...")
    try:
        safe_rclpy_init()
        bridge = send2real_robot_f1_udp2topic.UDPToRealArmBridge()
        
        if hasattr(bridge, 'run') and callable(getattr(bridge, 'run')):
            bridge.run()
        else:
            while True:
                time.sleep(1)
    except Exception as e:
        print(f"❌ UDP模块异常: {e}")


# ====================== 主程序 ======================
def main():
    print("="*80)
    print("🚀 F1 机械臂与复位桥启动中...")
    print("="*80 + "\n")

    # 1. 机械臂初始化
    if not validate_session_pose():
        raise RuntimeError("session initial-pose load failed; refusing to open UDP bridge")

    # 2. UDP 机械臂控制（后台线程）. The bridge owns the sole f1_init_api.
    arm_thread = threading.Thread(target=run_udp_to_real_arm, daemon=True)
    arm_thread.start()
    time.sleep(1.5)

    print("\n🎉 控制桥已启动；LeRobot 录制器由 start.sh 独立管理。\n")
    try:
        while rclpy.ok():
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n\n🛑 用户中断，正在关闭所有服务...")
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        print("✅ 程序已退出")


if __name__ == "__main__":
    main()
