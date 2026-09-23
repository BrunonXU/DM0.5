---
doc_type: incident
domain: g2_control
status: frozen
created: 2026-08-04
updated: 2026-08-24
models: [old_lora]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../../../g2_robot/docs/G2_TROUBLESHOOTING.md]
---

# O10零反馈回发导致拇指顶相机

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 上半身重启后O10 position均为0；powered在模型动作前回发“current hold”，双拇指上翘触碰相机 |
| 影响 | 模型尚未执行就产生危险手部运动 |
| 根因 | 把“可读取的零反馈”误当作“命令空间中的安全保持”；GDK将全零命令定义为开手 |
| 如何解决 | 首个安全模型目标前零硬件发送；Split输出；Home使用人工确认的成功命令锚点，不用零反馈 |
| 当前状态 | 修复后完成真实Split抓取；批准Home已验证 |

## 第一性根因

measurement和command即使维度相同，也不必具有相同语义。传感器返回0可能表示未初始化、不可用或某一编码原点；命令0则由执行器接口定义为明确姿态。把measurement原样回发只有在“单位、零点、方向、有效性和时间”全部满足恒等合同后才安全，本次没有这个合同。

日志证明运动发生在模型chunk接受前，因此不能归因为模型输出。仍不能仅据此断言O10传感器硬件损坏；能确认的是这组零值不允许作为hold命令。

## 预防

- powered首个已验证动作前命令计数必须为0；
- Home来自人工确认、带时间有效期的最后成功命令和批准锚点；
- 重启、人工掰动或急停后不得复用旧命令状态；
- 恢复顺序和净空由现场runbook管理。
