---
updated: 2026-09-20
type: navigation
---

# 泡沫分拣文档：按问题查阅

日常从这里进入；**进度只看当前方案**。每份维护文档负责一个问题，日期写在属性或历史记录中，不用日期作为日常文件名。

| 你要解决的问题 | 唯一正文入口 |
|---|---|
| 还差什么、首版何时结束、输入输出是什么 | [当前方案与收口](training_plan.md) |
| 4F-F1是谁、与旧机哪些一致/不同、迁移后如何运行 | [4F-F1身份与差异](../../docs/robots/4F-F1.md) |
| 4F-F1开机、录包反复失败、5K/20K/25K与保存/恢复试验起点 | [4F-F1操作流程](../../docs/robots/4F-F1_startup.md) |
| checkpoint占位、服务、EEF→joints、shadow与现场推理 | [推理操作与接口](inference_runbook.md) |
| IK部分失败提前重规划、连续失败原地暂停/继续 | [有效前缀demo部署及合同](../results/deployments/20260920-4F-ik-replan-v1/README.md) |
| 70K运动退步是训练还是泛化、loss是否主要靠底盘下降 | [匹配训练/验证运动诊断](../results/formal_training_20260917/matched_motion_probe_20260920/README.md) |
| EEF delta正式训练配方、维数决定、文献和本地依据 | [DM0.5单版训练recipe](training/dm05_eef_delta_recipe.md) |
| 两阶段validation、上肢/底盘权重与主选/备用checkpoint | [DM0.5验证recipe](training/dm05_validation_recipe.md) |
| demo采用哪版IK/FK、固定参数、源码快照与已知缺口 | [IK/FK demo recipe](training/ik_fk_demo_recipe.md) |
| FK/IK实测结果、连续求解缺口与FK缓存 | [2026-09-16运动学验证](../results/kinematics_validation_20260916/README.md) |
| 受限连续IK实现、确定性、误差和执行边界 | [受限IK实现及数值验收](../results/continuous_ik_20260916/README.md) |
| 扩大IK验证后暴露的分支/连续性问题 | [新增64条扩测与诊断](../results/ik_coverage64_20260916/README.md) |
| Exp/Trainer接线、采样与增强复现、离线EEF→IK | [DM0.5任务训练适配](../models/DM0.5/f1_training/README.md) |
| state19/action23 reader代码、验证与接入入口 | [DM0.5任务适配](../models/DM0.5/README.md) |
| 换一个任务还能复用哪些清洗经验 | [通用示教数据工作流](../../docs/dataset_workflow.md) |
| F1的数据按什么字段/时间规则处理、运行哪个脚本 | [F1处理管线](data_cleaning/f1_data_pipeline.md) |
| 底盘漏录之后怎样审核与重标 | [底盘标签修复](data_cleaning/chassis_label_repair.md) |
| 已踩过什么坑、每次为何改、怎样验证 | [清洗决策记录](data_cleaning/cleaning_decisions.md) |
| 满足什么条件才能发布清洗数据 | [数据集发布验收](dataset_acceptance.md) |
| 19D关节来源/回放怎样映射到F1接口 | [动作接口合同](action_interface_contract.md) |
| 数据包怎样导出、现场怎样回放、保存底盘起点并归位 | [replay操作手册](replay_runbook.md) |
| replay为何报错、哪些修好了、哪些仍待核对 | [回放故障与经验](incidents/replay_failures_and_lessons.md) |
| 三路视频怎样从本地打开 | [视频访问](video_access.md) |

早期维度讨论、原始数据审查和第一次对齐实验归入[历史记录](archive/README.md)，保留当时证据，不能用来替代当前选择。机器产物和每版验证见[结果索引](../results/README.md)；不把运行输出继续堆进日常文档。

新内容按问题更新对应正文；只有增加独立职责时才新增文件。当前状态不复制到通用工作流、操作手册或历史报告中。
