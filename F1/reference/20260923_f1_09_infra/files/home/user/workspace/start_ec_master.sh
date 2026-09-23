#!/bin/bash

# 日志目录
LOG_DIR="/home/user/workspace/logs"
mkdir -p "$LOG_DIR"

#echo "启动 Codemeter 服务..."
#service codemeter restart > "$LOG_DIR/codemeter.log" 2>&1

echo "启动 EC-Master 主站..."

LD_LIBRARY_PATH=/home/user/workspace/ec/EC-Master-V3.2.3.05-Linux-ARM_64Bit-SDK_Dongled/Bin/Linux/aarch64/Debug/ \
taskset -c 5 /home/user/workspace/ec/EC-Master-V3.2.3.05-Linux-ARM_64Bit-SDK_Dongled/Bin/Linux/aarch64/Debug/EcMasterDemoDcMT \
    -intelgbe 1 1 \
    -perf \
    -v 3 \
    -f /home/user/workspace/ec/EC-Master-V3.2.3.05-Linux-ARM_64Bit-SDK_Dongled/Bin/Linux/aarch64/Debug/f1-hm.xml \
    -a 5 \
    -t 0 > "$LOG_DIR/ec_master.log" 2>&1
