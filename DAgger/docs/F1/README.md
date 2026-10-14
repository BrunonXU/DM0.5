# F1 文档

[当前状态](STATUS.md) 是版本和现场事实的唯一汇总；以下验证记录保留各自发生日期。

| 目的 | 文档 |
|---|---|
| 2026-10-14 复位预演卡住：认证轨迹终态与腕限位同步 | [预演失败与修复](incidents/20261014-reset-preview-terminal.md) |
| 2026-10-13 Y 避桶复位停滞与采集受阻 | [现场失败记录](incidents/20261013-y-clearance-stall.md) |
| 4242 三路线启动、模型、结束保存与视频 | [HIL_WORKBENCH](HIL_WORKBENCH.md) |
| 稳定旧入口、按键、16D 合同与导出 | [F1_HG_DAGGER](F1_HG_DAGGER.md) |
| 历次软件、ARM、shadow 和现场证据 | [F1_VALIDATION](F1_VALIDATION.md) |
| 训练导出过度切段、因果采样与真实数据对照 | [2026-10-13重采样修正](incidents/20261013-training-resampling.md) |
| HIL 改良首次候选的交付记录 | [HIL_RELEASE](HIL_RELEASE.md) |
| 2026-10-10 三路线交付记录 | [HIL_ROUTES_RELEASE](HIL_ROUTES_RELEASE.md) |

源码在 [F1/](../../F1/)，公共组件在 [shared/](../../shared/)，原始结果在 [artifacts/](../../artifacts/README.md)。当前开发命令与发布包路径的区别见 [架构](../ARCHITECTURE.md)。
