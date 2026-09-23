#!/bin/bash

echo "=== 开始启动三个程序 (nohup 后台模式) ==="

# ==================== ROS2 环境激活 ====================
source /opt/ros/jazzy/setup.bash
source ~/workspace/f1/install/setup.bash

# ==================== 抑制 CycloneDDS 海量 WARN 日志 ====================
# 避免 "Failed to parse type hash" 刷屏导致 CPU/IO 资源浪费
export RCUTILS_LOGGING_MAX_SEVERITY=2  # 2=ERROR，抑制 WARN/INFO/DEBUG

# ==================== Conda 环境激活 ====================
CONDA_BASE="/home/user/miniconda3"
ENV_NAME="pico4ultra_py312"
source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate ${ENV_NAME} || {
    echo "❌ Conda 环境激活失败！"
    exit 1
}

echo "✅ Conda 环境 ${ENV_NAME} 已成功激活"

sleep 1

# ==================== 第一个程序 (加密版) ====================
echo "1. 启动 all_remote_lxl.py [加密版] ..."
cd /home/user/lxl/remote_sth/
nohup python all_remote_lxl.py > ../remote.log 2>&1 &
sleep 2

# ==================== 第二个程序 (加密版) ====================
echo "2. 启动 teleop_lxl.py [加密版] ..."
cd /home/user/lxl/XRoboToolkit-Teleop-Sample-Python

# 确保入口文件同级目录有 pyarmor_runtime（首次运行时复制，后续跳过）
# if [ ! -d "scripts/simulation/pyarmor_runtime_000000" ]; then
#     cp -r pyarmor_runtime_000000 scripts/simulation/
# fi

# 设置 .so 库路径（SDK 动态库）
# export LD_LIBRARY_PATH=/home/user/lxl/XRoboToolkit-Teleop-Sample-Python/dependencies/XRoboToolkit-PC-Service-Pybind/lib/aarch64:$LD_LIBRARY_PATH
# 设置 PYTHONPATH 让加密版能找到 dist/ 根目录的包（xrobotoolkit_teleop 等）
# export PYTHONPATH=/home/user/lxl/XRoboToolkit-Teleop-Sample-Python/dist:$PYTHONPATH

nohup python scripts/simulation/teleop_lxl.py > ../teleop.log 2>&1 &
sleep 2

# ==================== 第三个程序 (原生二进制) ====================
echo "3. 启动 Roboticsservice (runService.sh) ..."
cd /opt/apps/roboticsservice
nohup ./runService.sh > roboticsservice.log 2>&1 &
sleep 3

echo ""
echo "✅ 三个程序已在后台启动！"
echo ""

# ---------- 启动成功检测 ----------
echo "当前进程检测结果（判断是否启动成功）："
ps aux | grep -E 'all_remote_lxl|teleop_lxl|runService|RoboticsServiceProcess' | grep -v grep

echo ""
echo "日志查看命令："
echo "   tail -f ~/lxl/remote_sth/remote.log"
echo "   tail -f ~/lxl/XRoboToolkit-Teleop-Sample-Python/teleop.log"
echo "   tail -f /opt/apps/roboticsservice/roboticsservice.log"

echo ""
echo "如果需要停止程序，请运行：./stop_all_pico.sh"
