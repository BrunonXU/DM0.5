# Recovery

本目录保存训练初始姿态提取、内部离线预览和正式Split Home实现。

- `hand_home.py`：正式实现。双手使用人工批准的20D命令空间Home；双臂使用800条
  H5第0帧前14D中位数。执行时双手5Hz最多8步，随后双臂100Hz默认5秒回位。
- `reset_pose.py`：旧的内部34D对照工具，真实执行保持锁定；不再提供现场Shell入口。
- `extract_reset_pose.py`：从训练H5重新生成统计，只用于离线数据维护。

现场唯一入口：`scripts/03_reset_home.sh`。默认只读；`--execute`要求30分钟内的最后
成功手指命令、`ok`确认和物理急停。完成或失败都会停止双手与双臂下发线程。
