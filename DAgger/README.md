# DAgger：G2真实人工纠正与数据录制

> **2026-09-10：本阶段暂停，G2右手拇指故障送修（操作者现场确认）。** 待维修后重新验收再恢复真机采集；已有数据保留，`training_ready=false`。详见 [暂停交接记录](doc/PAUSED_HAND_REPAIR_20260910.md)。

## ▶ [启动命令：START_HERE.md](START_HERE.md)

## ▶ [进度、剩余工作、录制策略详解](RECORDING_AND_PROGRESS.md)

## ▶ [按顺序阅读文档](doc/00_INDEX.md)

本目录是HIL/DAgger的后续工作入口。G2Teleop保留独立冻结F710遥操，DAgger不从G2Teleop导入代码或读取运行配置。

| 目录 | 用途 |
|---|---|
| `src/g2dagger/` | HIL、录制、审计，以及独立固定副本的F710支持组件 |
| `scripts/40_g2_powered_hil.sh` | 真机同步policy与人工接管入口 |
| `windows_client/` | 可复制到Windows的独立sender/tunnel，保留既有Python环境 |
| `tests/` | DAgger离线测试 |
| `configs/` | 本目录的模型合同和手部拟合配置快照 |
| `data/raw/` | 原始三路图像、时间戳及运行journal；不可原地剪辑 |
| `data/archive/` | 历史离线证据与迁移前源码备份 |
| `logs/` | 新遥测、console及验证结果 |
| `doc/` | 历史设计、日志核查和迁移证据 |

固定副本来源见 `MIGRATION_MANIFEST.json`；不会跟随G2Teleop修改自动变化。仍显式依赖G2官方GDK、厂商模型、已安装IK包，以及 `/home/agi/xza/DM0.5` 的共享硬件/推理客户端和Home配置。解耦不等于复制整个GDK/机器人客户端。

DAgger是新运行的写入目录；G2的data/logs通过符号链接落在/data/logs/xza/DAgger。历史md与原始journal内的旧路径不重写，以保留证据。
当前 `training_ready=false`。当前合同与最新已核对证据统一见 [RECORDING_AND_PROGRESS](RECORDING_AND_PROGRESS.md)，不要把历史文档的“当前”当成最新状态。
