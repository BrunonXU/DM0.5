---
doc_type: incident
domain: g2_control
status: frozen
created: 2026-08-24
updated: 2026-08-24
date_confidence: unknown
models: [dm05]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../../../g2_robot/docs/G2_TROUBLESHOOTING.md]
---

# GDK初始化遗漏与DDS刚启动空状态

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 独立恢复程序出现Config request timeout；另有初始化后MotionControlStatus暂时为空 |
| 影响 | Robot调用失败或把启动暂态误判为硬件故障 |
| 根因 | 前者未调用完整 `gdk_init`；后者是DDS订阅尚未收到首个有效样本 |
| 如何解决 | 每进程严格 `gdk_init → Robot/Camera → gdk_release`；preflight等待并连续验证 `mode=5,error=0` |
| 当前状态 | exact-current hold和连续状态读取已验证 |

## 为什么放在同一记录

两种现象都发生在GDK生命周期边界，但根因不同：缺初始化是确定性合同错误，DDS warmup是异步系统的启动暂态。它们共享同一预防门——任何进程都必须完整拥有自己的GDK生命周期，并等待有效样本；不能因为一次空读就补0或直接进入Powered。
