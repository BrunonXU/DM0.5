---
doc_type: archive
domain: operations
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [archive_index]
supersedes: []
superseded_by: []
related: [../README.md]
---

# 历史文档与迁移索引

这里保存被替代的原始材料，不作为当前执行依据。

| 旧文档 | 原因 | 当前入口 |
|---|---|---|
| `01_REPRODUCTION_PLAN` | 阶段路线已大部分完成 | `../CURRENT_STATUS.md` |
| `02_IMPLEMENTATION_STATUS` | 停留在2026-07-30且混有事故 | `../CURRENT_STATUS.md`、`../training/incidents/`、`../robot/incidents/` |
| `06_OPERATIONS_LESSONS` | 原理与多个事故混写 | `../core/`、`../training/incidents/` |
| `07_RTX6000D_SERVER_HANDOFF` | 早期交接状态已过时 | `../training/` |
| `12_G2_CLIENT_DM05_RUNTIME`旧副本 | G2仓库副本后来更新，仍与目标架构不同 | `../robot/` |
| `15_G2_35K_HTTP_TO_ROBOT_RUNBOOK` | checkpoint、端口和链路均已变化 | G2 operator guide |
| `16_new_lora.md` | Python源码误放进docs | `../../g2_sort/training/NewLora_dm05_lora.py` |

迁移原则是“保留原文 + 给出successor”，而不是删除不符合当前认识的历史证据。
