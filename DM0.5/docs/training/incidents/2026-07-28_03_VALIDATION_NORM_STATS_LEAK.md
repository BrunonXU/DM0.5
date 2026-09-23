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
related: [../CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md]
---

# Validation误寻独立norm stats

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 切换到validation dataset name后，程序寻找validation专属norm stats |
| 影响 | 若为通过程序而计算该统计，会泄露validation分布并改变缩放空间 |
| 根因 | 上游stats路径digest包含dataset name，数据选择与归一化来源被错误绑定 |
| 如何解决 | validation选择held-out内容，但明确复用冻结的train-only stats |
| 当前状态 | 已修正并重新比较base/checkpoint |

## 第一性根因

validation的作用是模拟模型未用于拟合的数据。如果均值、方差或quantile由validation本身计算，验证信息在推理前已经进入数据变换；即使没有更新模型权重，评估也不再完全held-out。同时训练和验证使用不同坐标尺度，会把模型误差与缩放差异混在一起。

因此“选哪些样本”和“用什么归一化坐标系”必须解耦：样本来自validation split，坐标系只能来自train split。程序第一次明确失败是好事，禁止静默生成新的stats绕过门禁。
