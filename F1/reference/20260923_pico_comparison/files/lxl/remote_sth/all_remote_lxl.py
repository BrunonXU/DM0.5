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
 
# ====================== 导入你的三个模块 ======================
import init_f1
import send2real_robot_f1_udp2topic
import tianji_camera_record_get_from_udp


sys.path.insert(0, '/home/user/lxl')
import ip_config

def safe_rclpy_init():
    """确保 rclpy 只初始化一次"""
    if not rclpy.ok():
        rclpy.init()
        print("✅ ROS2 已初始化 (统一管理)")
    else:
        print("✅ ROS2 已处于初始化状态")


def run_init_f1():
    """1. 机械臂初始化"""
    print("🔧 [1/3] 启动 init_f1 - 机械臂初始化...")
    try:
        safe_rclpy_init()
        sender = init_f1.f1_init_api()
        
        sender.update_state()
        #sender.change_control_mode(sender.ARM_BOTH, 1)
        
        # target_left = [90, -90, -90, -90, 0, 0, 0]
        # target_right = [-90, -90, 90, -90, 0, 0, 0]

        # ========== 从配置文件读取初始位姿 ==========
        target_left = ip_config.target_left
        target_right = ip_config.target_right


        
        sender.plan_and_move_both_arms(target_left_q=target_left, target_right_q=target_right, duration_sec=3.0)
        print("✅ 机械臂已成功回到初始位置\n")
        return sender
    except Exception as e:
        print(f"❌ init_f1 失败: {e}")
        return None


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


def patched_camera_main():
    """3. 相机录制 - 绕过内部的 rclpy.init()"""
    print("🎥 [3/3] 启动相机 UDP 控制录制...")
    try:
        safe_rclpy_init()
        
        # === 关键修复：绕过相机文件内部的 rclpy.init ===
        original_init = rclpy.init
        rclpy.init = lambda *args, **kwargs: None  # 临时禁用 init
        
        # 直接执行相机文件的 main 逻辑，但避免重复初始化
        if hasattr(tianji_camera_record_get_from_udp, 'main'):
            # 如果 main 接受参数，我们传 None
            tianji_camera_record_get_from_udp.main(None)
        else:
            tianji_camera_record_get_from_udp.main()
            
        # 恢复（其实不需要了）
        rclpy.init = original_init
            
    except SystemExit:
        pass
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"❌ 相机模块异常: {e}")


# ====================== 主程序 ======================
def main():
    print("="*80)
    print("🚀 F1 三合一统一启动器启动中...")
    print("="*80 + "\n")

    # 1. 机械臂初始化
    run_init_f1()
    time.sleep(1.0)

    # 2. UDP 机械臂控制（后台线程）
    arm_thread = threading.Thread(target=run_udp_to_real_arm, daemon=True)
    arm_thread.start()
    time.sleep(1.5)

    # 3. 相机录制（主线程）
    print("\n🎉 所有模块已启动！按 Ctrl+C 退出全部服务。\n")
    
    try:
        patched_camera_main()
    except KeyboardInterrupt:
        print("\n\n🛑 用户中断，正在关闭所有服务...")
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        print("✅ 程序已退出")


if __name__ == "__main__":
    main()