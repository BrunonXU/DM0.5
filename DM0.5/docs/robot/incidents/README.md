---
doc_type: reference
domain: g2_control
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: [g2, rtx6000d]
canonical_for: [robot_incident_index]
supersedes: []
superseded_by: []
related: []
---

# 真机、GDK与控制事故

| 记录 | 问题 |
|---|---|
| `undated_01` | 头部相机持续空帧 |
| `undated_02` | G2到服务器非对称路由 |
| `undated_03` | GDK初始化遗漏与DDS warmup |
| `2026-07-31_01` | unified O10造成Servo长阻塞 |
| `2026-08-04_01` | O10零反馈回发导致拇指顶相机 |
| `2026-08-18_01` | RTC切换回拉 |
| `2026-08-20_01` | RTC state坐标与suffix污染 |
| `2026-08-21_01` | 同进程工作造成Servo deadline jitter |
| `2026-08-21_02` | Powered RTC被隐藏终止条件提前结束 |

现场一手混合记录仍保留在 `g2_robot/docs/G2_TROUBLESHOOTING.md`，但新增事故只写这里。
