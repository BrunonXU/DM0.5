# G2 独立遥操文档

源码统一在 [G2/teleop](../../../G2/teleop/README.md)。当前硬件与采集状态见 [G2 进度](../RECORDING_AND_PROGRESS.md)；历史参数和试验结果按文内日期理解。

| 目的 | 文档 |
|---|---|
| Windows / G2 三端启动与退出 | [START_HERE_WINDOWS_G2](START_HERE_WINDOWS_G2.md) |
| 控制架构、根因分析、冻结基线和阶段证据 | [F710_PINK_OTG_CONTROL_ARCHITECTURE](F710_PINK_OTG_CONTROL_ARCHITECTURE.md) |
| 模型来源、FK 和 Pink IK 计划 | [G2_KINEMATICS_AND_PINK_IK_PLAN](G2_KINEMATICS_AND_PINK_IK_PLAN.md) |
| 无输出 Shadow 通信 | [WINDOWS_TO_G2_SHADOW_RUNBOOK](WINDOWS_TO_G2_SHADOW_RUNBOOK.md) |
| F710 接收器触发底盘事故 | [INCIDENT_20260827](INCIDENT_20260827_F710_BASE_MOTION.md) |
| HIL 接入、录制、回放和完整历史 | [G2 文档索引](../00_INDEX.md) |
| 原入口/索引历史及最初迁移 | [旧 README](README_BEFORE_LAYOUT_20261009.md)、[迁移记录](DAGGER_MOVED.md) |

独立 F710 和 HIL 使用不同启动入口，不能同时运行输出 owner。接收器保持连接 Windows；不改变已确认的 G2 厂商 joystick 消费问题。完整历史源码、环境和文件去向清单见 [迁移归档](../../../artifacts/archive/g2_teleop_migration_20261012/)。

开发机在 DAgger 根目录，使用已有数值/IK 环境运行独立遥操测试：

```bash
PYTHONPATH=G2/teleop/src:G2/teleop/vendor_ik python -m pytest G2/teleop/tests
```

不在 G2 上改系统包；缺少特定依赖时报告限制，不把 skipped 当作通过。本次迁移的文件保全、环境和测试结果见 [verification.json](../../../artifacts/archive/g2_teleop_migration_20261012/verification.json)。
