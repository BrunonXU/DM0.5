# 公共组件

`src/dagger_core/` 提供输入协议与 ACK、HIL 状态机、策略 chunk 调度、通用 IK 积分、异步写盘和三图回放基础。

依赖方向为 `F1 / G2 → dagger_core`。本层不导入机器人实现，不定义 F1 16D / G2 34D 映射，不持有厂商硬件或机器人配置。每个发布包固定所用公共代码，源码更新不会改变已部署 release。

[架构与测试](../docs/ARCHITECTURE.md) · [维护约定](../AGENTS.md)
