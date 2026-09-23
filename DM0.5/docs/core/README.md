---
doc_type: reference
domain: operations
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [core_index]
supersedes: []
superseded_by: []
related: []
---

# Core：整个项目共同遵守什么

- [`TRAINING_AND_DEPLOYMENT_CONTRACT.md`](TRAINING_AND_DEPLOYMENT_CONTRACT.md)：模型输入、动作、数据、训练与安全边界的共同合同。
- [`CUSTOMIZATION_MAP.md`](CUSTOMIZATION_MAP.md)：G2相对OpenDM上游改了什么、代码边界在哪里。

只把训练侧和真机侧都必须共同理解的内容放在这里。checkpoint选择属于training；RTC和compile属于robot。
