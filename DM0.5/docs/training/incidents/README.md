---
doc_type: reference
domain: training
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d]
canonical_for: [training_incident_index]
supersedes: []
superseded_by: []
related: []
---

# 训练与服务器事故

| 记录 | 问题 |
|---|---|
| `2026-07-28_01` | FlexAttention backward超出单SM shared-memory上限 |
| `2026-07-28_02` | 长任务绑定IDE终端后被终止 |
| `2026-07-28_03` | validation错误寻找自己的norm stats |
| `2026-07-28_04` | 多卡smoke与正式global batch不一致 |
| `2026-07-28_05` | launcher停止但Docker容器仍存活 |
| `2026-07-28_06` | H5与视频没有按时间戳对齐 |
| `2026-07-28_07` | 32D base适配34D时可能丢权重或截断 |
| `2026-07-28_08` | resume后LoRA保存dtype变化 |
| `2026-07-29_01` | tmux误杀但训练容器继续运行 |

每个问题的根因、修复和证据在对应文件中。更早的Git、SMB和NVMe零散记录完整保留于archive。
