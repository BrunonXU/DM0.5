# G2 HIL / DAgger

G2 策略运行、F710 人工接管与 34D 纠正数据采集；独立遥操源码在 [teleop/](teleop/README.md)。

先读 [当前状态与录制合同](../docs/G2/RECORDING_AND_PROGRESS.md)，再用 [三个终端的完整启动命令](../docs/G2/START_HERE.md)。最后记录仍为右拇指送修后暂停，恢复前需现场重新验收。独立遥操与 HIL 共用机器，不能并行占用输出。

G2 配置和实现分别在 `configs/`、`src/g2dagger/`，公共组件在 `../shared/`。`g2_robot`、DM0.5 与厂商 SDK 是显式外部依赖；本次源码收口不搬动实机安装、参数或原始数据。

[全部 G2 文档](../docs/G2/00_INDEX.md) · [独立遥操说明](../docs/G2/teleop/README.md)
