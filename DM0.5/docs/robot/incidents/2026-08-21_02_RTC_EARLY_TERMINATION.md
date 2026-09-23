---
doc_type: incident
domain: rtc
status: frozen
created: 2026-08-21
updated: 2026-08-24
models: [new_lora]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../experiments/RTC_SMOOTHING_ABLATION_20260821.md]
---

# Powered RTC提前结束而非按100次完整运行

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 调大到100后仍很快结束，表面像参数被写死 |
| 影响 | 无法观察完整长时效果，实验次数与运行时长被混淆 |
| 根因 | 多个终止条件/脚本默认限制共同作用，`max_requests`并不是唯一生命周期上限 |
| 如何解决 | 移除手臂/双臂硬停止上限，仅保留警告；日志明确记录最终stop reason |
| 当前状态 | 后续长跑由操作者因手指接近碰撞主动停止，不再是隐藏请求上限 |

## 第一性根因

系统生命周期由所有终止谓词的逻辑“或”决定：任意一个duration、request count、安全门、worker错误、用户输入或脚本wrapper上限成立，程序都会结束。只改 `max_requests=100` 无法覆盖其他谓词。正确诊断必须让最终日志给出唯一stop reason，而不是从体感反推哪个参数生效。
