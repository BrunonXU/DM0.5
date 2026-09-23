---
doc_type: incident
domain: training
status: frozen
created: 2026-07-28
updated: 2026-08-24
models: [dm05, old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../archive/legacy_mixed/OPERATIONS_LESSONS_20260728.md]
---

# RTX 6000D FlexAttention backward失败

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 官方默认Flex/FA2/SDPA可forward，但LLM Flex backward编译失败 |
| 影响 | 正式训练无法开始；增加GPU或减batch无效 |
| 根因 | 单个Triton kernel要求114,688B shared memory，硬件单SM上限101,376B |
| 如何解决 | 只把LLM backend从Flex改为SDPA；Vision仍用FA2，Action仍用SDPA |
| 当前状态 | 兼容组合已通过backward、checkpoint恢复和正式训练门禁 |

## 第一性根因

显存容量与单个SM的片上shared memory是不同资源。8张卡会增加总显存和并行吞吐，但不会增加某一个kernel所在SM的硬件上限；减batch也不一定改变已生成kernel的静态shared-memory需求。因此“85GB显存不够”与“GPU不空闲”都不能解释错误。

固定官方镜像和自建FA2镜像均复现同一Flex错误，排除了项目wheel作为主因。最小兼容改动是替换不能执行的LLM kernel，不是重写模型：attention语义、权重形状、loss、LoRA和checkpoint合同不变，但浮点运算顺序不同，不能声称逐bit一致。

## 验证与影响

SDPA/FA2/SDPA单卡LoRA backward峰值13.278GiB；固定镜像下恢复与续训通过。可用backend的实际训练能力大于理论更快但无法backward的backend。策略质量仍由冻结validation验证，不能仅由“数学语义相同”直接证明。

完整证据路径和切换边界保留在 [`OPERATIONS_LESSONS_20260728.md`](../../archive/legacy_mixed/OPERATIONS_LESSONS_20260728.md#13-官方镜像不等于所有官方内核都适配当前gpu)。
