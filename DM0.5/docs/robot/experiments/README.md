---
doc_type: reference
domain: rtc
status: canonical
created: 2026-08-24
updated: 2026-08-28
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [robot_experiment_index]
supersedes: []
superseded_by: []
related: []
---

# Runtime与真机实验

| 文件 | 内容 |
|---|---|
| `RTC_ENGINEERING_DECISION_20260818.md` | RTC参数、时序约束和验证设计 |
| `RTC_IMPLEMENTATION_AND_OFFLINE_REPORT_20260818.md` | 首轮RTC实现与离线A/B |
| `DENOISING_AND_COMPILE_20260820.md` | 10→5步与fixed-prefix compile证据 |
| `G2_ACTION_SMOOTHING_PLAN_SOURCE.md` | 早期平滑原理、手部数据和方案演进 |
| `RTC_SMOOTHING_ABLATION_20260821.md` | 多轮离线/真机平滑与调度对照 |
| `PROCESS_ISOLATION_AND_TRANSPORT_20260824.md` | 单Robot-owner隔离、图像候选与binary socket离线门禁 |
| `NETWORK_TRANSPORT_AB_20260825.md` | N0-N3原生JPEG与HTTP/WebSocket匹配服务延迟A/B |
| `O10_TACTILE_AND_COMMAND_RATE_20260825.md` | O10型号/触觉实测、5-200Hz接口阶梯与30/50Hz决策边界 |
| `SYNC_ARM_MICRO_TREMOR_ABLATION_20260827.md` | 161-chunk双臂近静止反向消融与3.5mrad minimum-jerk唯一Shadow候选 |

这些是冻结证据。当前结论先看 `../../CURRENT_STATUS.md` 和robot根部原理文档。
