# G2 独立 F710 遥操

G2 双臂 / O10 独立遥操、诊断与冻结基线，统一维护在本目录。

先看 [G2 当前状态](../../docs/G2/RECORDING_AND_PROGRESS.md)，再按 [Windows / G2 启动说明](../../docs/G2/teleop/START_HERE_WINDOWS_G2.md) 操作。接收器连接 Windows；独立遥操和 HIL 不能同时占用机器人输出。

本目录保留自己的 `src/`、`configs/`、`scripts/`、`tests/` 与必要模型；不包含另一份 DAgger HIL 实现。源码迁移不改变实机安装路径或冻结控制参数。

[完整文档](../../docs/G2/teleop/README.md) · [HIL 入口](../README.md) · [原基线与整理证据](../../artifacts/archive/g2_teleop_migration_20261012/)
