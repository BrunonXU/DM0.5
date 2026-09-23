---
doc_type: incident
domain: training
status: frozen
created: 2026-07-28
updated: 2026-08-24
time_order_confidence: unknown
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../archive/legacy_mixed/OPERATIONS_LESSONS_20260728.md]
---

# 断点恢复后LoRA参数保存dtype变化

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 完整checkpoint恢复训练成功，但PEFT保存的104,848,384个LoRA参数由BF16变FP32 |
| 影响 | adapter/optimizer文件变大，容易被误判为漏权重或结构变化 |
| 根因 | 恢复/PEFT保存路径改变参数持有或序列化dtype，键和参数量未变化 |
| 如何解决 | 每次恢复记录checkpoint、step、键数、参数量、dtype、首批loss/grad/显存和文件大小 |
| 当前状态 | 训练连续且结构一致；dtype差异作为复现事实保留 |

## 第一性根因

“相同参数”至少包含名称、形状、数值和dtype四个维度。文件大小变化只观察到了存储表示，不能直接推断参数集合变化；反过来，训练能继续也不能证明精度表示完全相同。必须把这些维度分别核对。
