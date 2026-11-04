# 训练记录索引

讨论“现在怎么训”请读[根训练recipe](../TRAINING_RECIPE.md)；判断“这一轮训练结果如何”请读对应版本复盘。两类文档分开，避免把旧smoke参数当成当前配方。

你通常只需要看最新复盘的开头结论和影响。逐步指标、保存恢复、参数清单与测试回执留在后文或artifacts，不需要逐份阅读。

## 优先看

| 内容 | 入口 |
|---|---|
| 当前训练合同与方法 | [训练recipe](../TRAINING_RECIPE.md) |
| 冻结B1250接入RLT与离线验收 | [RLT首轮配方](experiments/20261026-rlt-frozen-b1250.md) |
| 本轮标准布局纠正混训配方 | [V1＋DAgger](../V1_DAgger_FullSFT.md) |
| 新单臂与混采实施/阻塞 | [本轮验收](experiments/20261021-v1-dagger-fullsft.md) |
| 旧版Full实际配方 | [V1_FullSFT](../V1_FullSFT.md) |
| Full预检、正式1K与续训判断 | [Full汇总复盘](experiments/20261020-v1-fullsft.md) |
| 旧LoRA50K曲线与过拟合边界 | [50K复盘](experiments/20261020-v1-50k-review-full-plan.md) |

## 历史证据，需要追溯时打开

| 版本 / 问题 | 报告 |
|---|---|
| 15条LoRA数据与正式训练 | [正式记录](experiments/20261016-v1-formal-training.md)、[当时配方建议](experiments/20261016-v1-formal-recipe-proposal.md) |
| 八卡转四卡且保留恢复状态 | [四卡续训](experiments/20261019-four-gpu-resume.md) |
| 单条右EEF/LoRA首次闭环 | [单条实验](experiments/20261016-v1-eef-lora.md) |
| 最早绝对关节、无增强smoke | [smoke记录](experiments/20261015-v1-smoke.md) |
| recipe历次决定完整原文 | [历史快照](experiments/20261021-training-recipe-history.md) |

最早训练首页的完整设计与来源保存在[整理前原文](../artifacts/20261021-doc-review/before/tasks/F1_ComplexyToys/training/README.md.txt)。这些记录只说明各自版本，不覆盖当前recipe。
