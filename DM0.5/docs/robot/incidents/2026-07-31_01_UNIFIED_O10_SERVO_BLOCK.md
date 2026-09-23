---
doc_type: incident
domain: g2_control
status: frozen
created: 2026-07-31
updated: 2026-08-24
models: [old_lora]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../../../g2_robot/docs/G2_TROUBLESHOOTING.md]
---

# Unified 34D/O10解析造成Servo长阻塞

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | unified 34D下发出现O10参数解析失败和约2.53s Servo阻塞 |
| 影响 | 100Hz双臂控制完全失去deadline，产生巨抖风险 |
| 根因 | 手部命令语义/解析与双臂Servo被耦合进同一同步调用路径 |
| 如何解决 | 正式输出改为Split：14D双臂100Hz Servo，20D双手5Hz `move_ee_pos` |
| 当前状态 | Split真机运行通过；unified仅保留显式诊断，不用于日常 |

## 第一性根因

一个100Hz环的最坏周期由其同步关键路径里最慢的调用决定。只要O10解析或设备调用可能阻塞秒级，把它与双臂下发放在同一调用就使10ms合同在结构上不可证明。Split不是改变模型34D输出，而是在硬件适配层把不同执行频率和失败模式隔离。

这次修复不能证明双臂稳定100Hz；它只移除了一个已确认的秒级阻塞源。之后仍需用发送interval度量Python和GDK的毫秒级长尾。
