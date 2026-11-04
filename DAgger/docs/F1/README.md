# F1 文档

最新事故：[2026-10-16 第二条START与左腕USB断开](incidents/20261016-next-episode-left-usb-disconnect.md)。


[输入暂停、RB恢复与连续录制说明](incidents/20261015-input-pause-recovery.md)。最新补证与用户确认的直接V1路线见[断连停录与Y复位修订](incidents/20261015-transport-close-and-y-repeat.md)。

[当前状态](STATUS.md) 是版本和现场事实的唯一汇总；以下验证记录保留各自发生日期。

| 目的 | 文档 |
|---|---|
| 2026-10-15 v31 Y上抬J6真实跟随平台再现、SDK与数值反事实 | [本次彻查](incidents/20261015-y-lift-j6-following.md) |
| Pico-LXL / meaxiao超时恢复、独立录制与30Hz对照决策 | [输入与录制对照](incidents/20261015-pico-input-comparison.md) |
| 2026-10-15 历史手柄输入统计、暂停录制及有效段利用讨论 | [历史统计与方案](incidents/20261015-input-history-analysis.md) |
| 2026-10-15 示教输入超时自动收尾、完成提示音误导 | [三条录制核对](incidents/20261015-unexpected-episode-end.md) |
| Y复位复用4242曲线、V1预设与到位等待 | [复用说明](incidents/20261014-home-workbench-reuse.md) |
| 2026-10-14 v29现场上抬跟随偏差、重试组合限位 | [跟随与限位事故](incidents/20261014-y-v29-following.md) |
| 2026-10-14 最近11次Y上抬停止：额外腕部约束与数值对照 | [姿态预算审查](incidents/20261014-y-orientation-budget.md) |
| 2026-10-14 复位预演卡住：认证轨迹终态与腕限位同步 | [预演失败与修复](incidents/20261014-reset-preview-terminal.md) |
| 2026-10-13 Y 避桶复位停滞与采集受阻 | [现场失败记录](incidents/20261013-y-clearance-stall.md) |
| 4242 三路线启动、模型、结束保存与视频 | [HIL_WORKBENCH](HIL_WORKBENCH.md) |
| 稳定旧入口、按键、16D 合同与导出 | [F1_HG_DAGGER](F1_HG_DAGGER.md) |
| 历次软件、ARM、shadow 和现场证据 | [F1_VALIDATION](F1_VALIDATION.md) |
| 训练导出过度切段、因果采样与真实数据对照 | [2026-10-13重采样修正](incidents/20261013-training-resampling.md) |
| ComplexyToys 任务预处理、RLinf/π0.5 训练与推理 | [任务入口](../../../tasks/F1_ComplexyToys/README.md) |
| HIL 改良首次候选的交付记录 | [HIL_RELEASE](HIL_RELEASE.md) |
| 2026-10-10 三路线交付记录 | [HIL_ROUTES_RELEASE](HIL_ROUTES_RELEASE.md) |

源码在 [F1/](../../F1)，公共组件在 [shared/](../../shared)，原始结果在 [artifacts/](../../artifacts/README.md)。当前开发命令与发布包路径的区别见 [架构](../ARCHITECTURE.md)。

- [2026-10-16 开始录制输入卡顿调查](incidents/20261016-start-input-lag.md)：两端日志确认ACK超时主动重连；采样无同量级停顿，初始链路停顿位置待定位。
