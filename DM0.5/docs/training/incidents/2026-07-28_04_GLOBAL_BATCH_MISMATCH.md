---
doc_type: incident
domain: training
status: frozen
created: 2026-07-28
updated: 2026-08-24
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../experiments/training/old_lora/EIGHT_GPU_500_STEP_GATE.md]
---

# 多卡预热与正式global batch合同不一致

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 第一次8卡500-step使用per-device batch 1，实际global batch为8 |
| 影响 | 能证明多卡链路可运行，不能代表正式global batch 32的显存和吞吐 |
| 根因 | 只记录“8卡”，遗漏batch由GPU数、单卡batch和梯度累积共同决定 |
| 如何解决 | 用 `8 × 4 × 1 = 32` 重跑正式门禁 |
| 当前状态 | 500步通过，19.83 samples/s，无OOM/非有限值 |

## 第一性根因

优化器一次参数更新所看到的样本数是 `world_size × per_device_batch × accumulation`。GPU数相同不代表优化合同和内存压力相同。任何多卡报告必须同时记录这四个量，否则吞吐、显存和loss曲线都不可比较。
