---
doc_type: reference
domain: training
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d]
canonical_for: [training_index]
supersedes: []
superseded_by: []
related: []
---

# Training：数据、训练、checkpoint和服务器

先看三份根部文档：

- [`DATA_AUDIT.md`](DATA_AUDIT.md)：数据来源、时间戳、准入和冻结证据。
- [`CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md`](CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md)：为什么以及怎样选择checkpoint。
- [`RTX6000D_ASSETS.md`](RTX6000D_ASSETS.md)：服务器硬件、存储、镜像和资产事实。

某次训练结果在：

- [`experiments/old_lora/`](experiments/old_lora/README.md)
- [`experiments/new_lora/`](experiments/new_lora/README.md)

训练、Docker、GPU和数据处理故障在 [`incidents/`](incidents/README.md)。LoRA只区分训练实验，不区分通用服务器事故。
