---
created: 2026-09-17
type: implementation
---

# F1训练器与离线EEF执行接线

按[已确认训练recipe](../../../docs/training/dm05_eef_delta_recipe.md)实现任务适配，复用共享DM0.5网络、LoRA及flow matching；IK固定[demo v1](../../../docs/training/ik_fk_demo_recipe.md)。当前完成度只见[当前方案](../../../docs/training_plan.md)，工程结果见[2026-09-17接线检查](../../../results/dm05_integration_20260917/README.md)。

## 代码职责

| 文件 | 职责 |
|---|---|
| [experiment.yaml](../experiment.yaml) | 已定参数的可读取配置；旧21D/关节19D配置另存[历史版](../experiment_state21_joint19.archive.yaml) |
| [data.py](data.py) | 来源/sidecar/FK/图像清单/统计哈希绑定；原split和有效起点核验；训练来源与统计来源必须完全相同 |
| [trainer.py](trainer.py) | 扩展共享Trainer的采样与checkpoint归档，沿用共享loss和optimizer |
| [experiment.py](experiment.py) | 配置共享LoRA、显式恢复、每5K生成动作验证回调；不自动拟合stats、不自动续训旧目录 |
| [execution.py](execution.py) | 模型归一化H50×32→物理23D→固定观测锚点→冻结IK前20步；不发送机器人动作 |
| [evaluation.py](evaluation.py) | 固定验证窗口/seed的10步Euler生成；H20/H50物理误差、停车/运动子集、逐来源报告及前20步IK诊断 |
| [validation.py](validation.py)、[validation_protocol.py](validation_protocol.py) | 两阶段manifest、独立checkpoint评估、episode配对bootstrap与80/10/10候选排序；不自动发布 |
| [validation_artifacts.py](validation_artifacts.py)、[validation_review.py](validation_review.py) | F1 checkpoint/原始预测身份、批量评估续跑、夹爪/底盘曲线与主选/备用留档 |
| [distributed_validation.py](distributed_validation.py) | 训练内长验证的独立CPU等待组；不改变训练NCCL算法或评估窗口 |

## 采样与增强复现

F1Trainer覆盖默认RandomSampler，先均匀选来源、再选有效起点。Sampler输出全局序列，**仅由Accelerate切分batch**；不能再设置reader的rank分片或套DistributedSampler。

每次抽样携带`(index, epoch, global_draw)`。图像增强的seed由`seed42/epoch/global_draw/camera`派生，显式设置Albumentations内部随机数。这样不同worker与恢复后跳过batch不会改变同一抽样的增强；再次抽到同一窗口仍可得到不同增强。增强分布、幅度、三路策略均沿recipe，不新增训练噪声。CPU检查已验证0/2 worker一致；完整4GPU训练恢复尚未运行。

## 发布清单

`f1_dm05_release_v1`包含：

- 每源NPZ及sidecar、FK及sidecar、图像manifest的路径/SHA，原split与有效起点。
- 独立stats路径/SHA；必须对应完整入选train来源，不允许混入validation或复用不同数据范围的stats。
- 明确的`training_approved`。`prepare-engineering`固定输出false，只描述现有数据，不改变审核结果。
- 正式验证绑定按已批准Pass A生成的完整`evaluation`，包括独立flow/generation窗口与seed。窗口数与seed见[验证recipe](../../../docs/training/dm05_validation_recipe.md)，来源和实际起点随正式release冻结；不能从训练集取验证或自行虚构阶段标签。

字段中的文件路径可为绝对路径或相对于清单位置的路径。当前工程清单使用6000D绝对路径，容器按同路径挂载，未实现跨机自动重定位。

正式训练同时检查release、每源包及stats的批准状态。正式统计由数据发布阶段计算，训练启动不重算。现有8条工程统计已知覆盖不足，不能通过修改一个布尔值将它变成正式发布统计。

## 使用入口

6000D，工作目录`/home/xza_100850113/projects`，已有DM0.5环境中加入：

```bash
export PYTHONPATH="$PWD/F1/foam_sort_whole_body/models/DM0.5:$PWD/DM0.5/opendm${PYTHONPATH:+:$PYTHONPATH}"
python -m f1_training inspect \
  --release F1/foam_sort_whole_body/results/dm05_integration_20260917/engineering_release.json \
  --engineering
```

`python -m f1_training --help`列出工程清单生成、只读核验与训练入口。正式入口为`train --release … --base-model … --output … [--resume …]`，要求既定4个rank、独立输出目录及已冻结的验证/选模协议；当前没有可启动正式训练的数据发布版。基础模型必须原生H50/32D且不是G2/F1 adapter。checkpoint保存stats、release、recipe、代码/配置绑定及共享Trainer的optimizer/scheduler/RNG；恢复必须显式指定同run完整checkpoint。

离线检查：

```bash
python -m f1_training.offline_check --release <engineering_release.json> --output <新的offline_ik.json>
python -m f1_training.cpu_check --release <engineering_release.json> \
  --base-model <DM05-base目录> --processor <本地processor目录> --output <新的cpu_report.json>
```

`offline_check`使用标签代替模型预测，检查反归一化与IK转换。`cpu_check`使用真实共享processor/Trainer/DataLoader和不执行前向的占位模型，另以返回标签的fixture检查验证器和RNG恢复。两者均没有测量真实policy能力、正式LoRA反向传播或实机控制。

## 尚未定的选模规则

2026-09-17用户已确认100K单run、两阶段验证与主选/备用流程，以及双臂80%／夹爪10%／底盘10%的候选排序；训练loss不变，不自动早停。原始预测/真值轨迹、H20/H50分项、seed波动及IK诊断保存。夹爪转换时间定义和任务阶段标注尚未冻结，底盘协同需结合轨迹复核，当前报告不冒充完整语义或实机验收。协议与独立评估命令见[验证recipe](../../../docs/training/dm05_validation_recipe.md)。

完整模型单卡前向/反向与10步生成已完成工程检查，未更新权重；正式数据、正式验证窗口、4GPU恢复检查、模型服务/ROS客户端仍待后续完成。数学拒绝时整段无关节目标；整机停止/重观测/调度没有由本模块自动实现。

完整模型检查入口为`python -m f1_training.model_check --help`，固定单卡工程验证、无optimizer step；批大小与网络/LoRA参数沿既定recipe。多rank运行时仅rank0写trainable summary，避免多个进程覆盖同一文件。

用户要求以迁移DM0.5既有方案为准，直接复用共享Trainer、模型和LoRA加载/保存，任务入口只适配F1数据与评价合同。对应原文件、批量评估/续跑、轨迹复核与决策JSON用法集中在[验证recipe](../../../docs/training/dm05_validation_recipe.md#迁移范围复用dm05不重建通用框架)。`infra_check.py`和`checkpoint_check.py`仅用于工程检查，不是新训练框架或正式run入口；前者使用单参数CPU测试模型，后者保存/重载未训练adapter且不更新权重。实际范围见[迁移收口记录](../../../results/validation_infra_20260917/README.md)。

## W&B监控

用户于2026-09-17明确要求启用W&B，`experiment.yaml`设为`wandb_project: F1_foam_DM05`。正式训练记录共享Trainer的训练曲线和每5K生成验证指标；[monitoring.py](monitoring.py)保持同一输出目录的run ID，显式恢复必须接续原run。rank0保存run URL与版本绑定；工程检查仍不创建W&B run。凭据复用现有账号，正式启动前检查登录/连通性；这次仅完成本地配置与指标接线检查，没有线上上传验收。完整约定见[recipe](../../../docs/training/dm05_eef_delta_recipe.md#wandb监控与恢复)。
