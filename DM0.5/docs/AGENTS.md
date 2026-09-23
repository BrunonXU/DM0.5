# DM0.5 文档规则（给后续Agent）

本文件约束 `DM0.5/docs/`。新增或移动文档前，先读本文件和 `README.md`。

## 1. 人看目录，Agent看metadata

物理目录只按人实际处理的四种场景分类：

| 一级目录 | 人什么时候进来 | 包含什么 |
|---|---|---|
| `core/` | 想理解整个项目的共同合同 | 跨训练和真机都有效的基础合同、代码改造边界 |
| `training/` | 正在处理数据、训练、checkpoint或服务器 | 原理、证据、服务器事实、Old/NewLora实验、训练事故 |
| `robot/` | 正在部署、压延迟、做RTC或上真机 | 推理/控制原理、当前架构、runtime实验、真机事故、操作入口 |
| `archive/` | 追溯旧方案或原数字文档 | 被替代但不能删除的历史证据 |

顶层只允许 `README.md`、`CURRENT_STATUS.md`、`AGENTS.md` 和以上四个目录。不要再次按 `principles/architecture/evidence/reference` 建立一级目录；这些是文档属性，不是人的入口。

## 2. 写到哪里

- 数据、训练配置、checkpoint、GPU、Docker、训练服务器 → `training/`；
- 推理服务、transport、compile、RTC、Servo、GDK、真机安全 → `robot/`；
- 两边必须共同遵守且无法归到单侧的合同 → `core/`；
- 已过时、仅供追溯 → `archive/`。

在 `training/` 和 `robot/` 内，长期原理和当前架构直接放根部；可复现实验进入 `experiments/`；实际故障进入 `incidents/`；不再增加其他分类层。

LoRA只在 `training/experiments/{old_lora,new_lora}` 分类。RTC、compile、网络或GDK问题即使最初在NewLora上发现，也属于 `robot/`。

## 3. 第一性原理不能因整理而消失

原理文档必须保留：问题、不可再约简的事实和约束、逐步推导、假设、对质量/延迟/安全/维护的影响、能证明与不能证明什么、对应实验和反例。

去重时只允许把完整推导集中到一份canonical文档，其他报告链接它；禁止把完整推导压缩成几条结论。历史实验为了自包含而保留的旧解释不得删除，但必须标记 `frozen` 或 `superseded`。

## 4. metadata代替文件夹分类

新建或实质重写的Markdown使用：

```yaml
---
doc_type: principle | architecture | experiment | incident | runbook | status | reference | archive
domain: data | training | server | inference | transport | rtc | g2_control | g2_hardware | operations
status: canonical | active | frozen | superseded | archived
created: YYYY-MM-DD
updated: YYYY-MM-DD
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: []
related: []
---
```

metadata负责告诉Agent文档类型，不要为了metadata再创建物理目录。

## 5. 特殊文档

- 实验必须写假设、唯一变量、冻结参数/代码/数据、结果、统计口径、限制和决策；旧结果不可覆盖。
- 事故采用 `YYYY-MM-DD_NN_slug.md`；无日期用 `undated_NN`，不得根据mtime杜撰。开头必须回答遇到什么、影响、根因、解决和状态。
- `CURRENT_STATUS.md`只写当前事实、阻塞和下一步。
- G2现场命令只在 `g2_robot/docs/00_G2_OPERATOR_GUIDE.md`维护。
- 新增实质内容文档时，必须同步更新顶层`README.md`的“按写作时间倒序的内容文档”：按metadata里的`created`倒序加入文件名、状态和一至两行摘要，并在相邻文档之间保留独立水平分隔线。不要用文件mtime杜撰写作日期；各级README和本规则文件不重复收入该时间线。

## 6. 当前权威入口

| 问题 | 位置 |
|---|---|
| 当前进度 | `CURRENT_STATUS.md` |
| 项目共同合同 | `core/TRAINING_AND_DEPLOYMENT_CONTRACT.md` |
| 数据、训练和checkpoint | `training/` |
| 去噪、compile和总延迟 | `robot/INFERENCE_LATENCY_FIRST_PRINCIPLES.md` |
| RTC、chunk与100Hz | `robot/RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md` |
| Control唯一拥有Robot架构 | `robot/RTC_PROCESS_ISOLATION.md` |
| 训练/服务器事故 | `training/incidents/` |
| 真机事故 | `robot/incidents/` |

## 7. 跨仓库与检查

`DM0.5/docs`是唯一跨服务器和G2的知识库。`g2_robot/docs`只保留现场离线需要的操作、GDK参考、部署快照和指针。

更新前后检查：是否写进正确场景；是否已有canonical解释；是否保留第一性推导和证据边界；是否把实验结论误当普遍原理；是否为旧文档标注successor；是否更新README和链接；是否泄露凭据或私钥。
