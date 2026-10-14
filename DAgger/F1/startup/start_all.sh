#!/bin/bash -l
echo "start f1 ros nodes... " >> /tmp/f1.log
# 启动所有相机和机械臂节点
# ===== 关键：加载完整用户环境 =====
source /home/user/.bashrc
source /opt/ros/jazzy/setup.sh

# ===== 加载各个工作空间的环境 =====
source /home/user/workspace/f1/install/setup.sh
source /home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.sh
source /home/user/workspace/camera/ros2_ws/install/setup.sh
#source /home/user/workspace/agv/install/setup.sh

# ===== 设置 ROS2 相关环境变量 =====
# export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

# 获取脚本所在目录
SCRIPT_DIR=/home/user/workspace
cd "$SCRIPT_DIR"

# 日志目录
LOG_DIR=/home/user/Bruno/DAgger-runtime/base-start-logs
mkdir -p "$LOG_DIR"

echo "启动机械臂运动学节点..."
ros2 launch motor_ctl kinematics.launch.xml> "$LOG_DIR/kine_arm.log" 2>&1 &

echo "启动机械臂电机节点..."
ros2 launch motor_ctl motor_ctl.launch.xml > "$LOG_DIR/motor_ctl.log" 2>&1 &

sleep 2



echo "启动头部奥比相机..."
bash /home/user/Bruno/DAgger/releases/20261008-f1-hgdagger-v11/DAgger/scripts/60_f1_camera_internal.sh start head > "$LOG_DIR/head_camera.log" 2>&1 &

sleep 2

echo "启动右手腕相机..."
bash /home/user/Bruno/DAgger/releases/20261008-f1-hgdagger-v11/DAgger/scripts/60_f1_camera_internal.sh start right > "$LOG_DIR/right_wrist.log" 2>&1 &

sleep 2

echo "启动左手腕相机..."
bash /home/user/Bruno/DAgger/releases/20261008-f1-hgdagger-v11/DAgger/scripts/60_f1_camera_internal.sh start left > "$LOG_DIR/left_wrist.log" 2>&1 &

sleep 2

echo "启动升降控制节点..."
# ==================== Conda 环境激活 ====================
CONDA_BASE="/home/user/miniconda3"
source "${CONDA_BASE}/etc/profile.d/conda.sh"
echo "Conda 环境 ${ENV_NAME} 已成功激活"
conda deactivate
/home/user/workspace/EC_Line_Control/start_waist_updown_ros2.sh

sleep 2
echo "启动一键复位节点..."
/home/user/workspace/whole_body_ctl/start_whole_body_ctl.sh

sleep 2
echo "启动手柄控制节点..."
/home/user/workspace/whole_body_ctl/gamepad_get_send/start_gamepad_ctl.sh

echo "所有节点已启动！"
echo "f1 ros nodes started... " >> /tmp/f1.log
