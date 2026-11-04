# F1 泡沫棉：历史任务与保留证据

**当前不再推进本任务。** 2026-10-23按用户决定清理51.64 GiB可再生的reader图像与灰度帧缓存；保留代码、数据标签、来源清单、数值评测和历史视频。外盘原始MCAP未删除，F1积木模型包另行完整保留。

清理后旧训练输入与部分图像审阅不能直接使用，历史通过记录仍只描述当时版本。需要复用经验时查下方文档；如以后恢复任务，先看[当前状态与重建条件](docs/training_plan.md)，不能直接启动旧训练。

## 历史目标与方案

本任务面向用户自研 F1：抓取泡沫棉，移动到放置位置，放下，再返回抓取位置。
F1 与 G2 构造相似，但不是已经验证过的 G2 控制接口。

当时目标由用户明确为朴素的全身 VLA 端到端实验：同一模型联合输出双臂、夹爪和底盘动作。
当时正式训练选定19D状态输入（双臂14＋夹爪2＋当前底盘速度3）、EEF相对动作23D（补齐至模型32D），保留右臂与右夹爪；
body5不进模型，训练当前底盘已知速度使用已复核重建估计、未知输入显式编码unknown；未来未知底盘仅mask底盘监督，保留有效双臂窗口。推理使用现场速度反馈。
决定及依据见[单版训练recipe](docs/training/dm05_eef_delta_recipe.md)。现有清洗/关节回放仍使用19D来源表示，
数据进度与迁移完成度统一见[当前方案](docs/training_plan.md)。
文档按问题从[文档导航](docs/README.md)进入；复用 DM0.5 的通用模型与训练组件，任务数据、配置和评估保存在本目录。

## 常用入口

| 需求 | 入口 |
|---|---|
| 当前数据状态、剩余工作与收口范围 | [当前方案](docs/training_plan.md) |
| 推理服务/客户端、checkpoint占位、EEF到关节 | [推理手册](docs/inference_runbook.md) · [配置](configs/inference.json) |
| 正式训练参数、EEF编码和参考文献 | [DM0.5 EEF delta recipe](docs/training/dm05_eef_delta_recipe.md) |
| 清洗方法、事故处理、验收、接口与回放文档 | [文档导航](docs/README.md) |
| 新任务复用的采集与清洗经验 | [通用示教数据工作流](../docs/dataset_workflow.md) |
| 查看底盘、复位与循环视频 | [三路视频工作台](results/20260914/videos/index.html) · [本地访问说明](docs/video_access.md) |
| 各版本清单、统计、原图及验证证据 | [结果索引](results/README.md) |
| 场景与数据字段 | [场景配置](configs/scenario.yaml) · [数据合同草案](configs/data_contract.draft.yaml) |
| 本任务的模型配方与适配 | [DM0.5说明](models/DM0.5/README.md) · [实验设计配置](models/DM0.5/experiment.yaml) |

## 数据位置

6000D原始数据保留在`/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw/`，任务按路径引用，不复制或移动约639GB的数据。
FTP同步已按用户要求停止并禁用自动启动，原801条已拉齐；同级`transfer/`保留脚本与断点记录，停止状态记录为`complete / stopped_by_user`。
下载期间的历史快照与后续处理证据见结果索引，候选范围与验收状态只看当前方案。

## 边界

| 位置 | 职责 |
|---|---|
| `DM0.5/opendm/` | 通用 DM0.5 模型、动作投影、训练组件 |
| 本任务 `scripts/` | MCAP/元数据审计、预览与后续转换入口 |
| 本任务 `configs/` | F1 字段语义、单位、坐标系、时钟、动作编码与筛选规则 |
| 本任务 `docs/` | 场景设计、证据、实验决策 |
| 本任务 `robot/` | F1回放与推理客户端，复用F1 ROS适配；真机推理验收见当前方案 |
| 本任务 `deploy/` | 源版本打包、首次F1静态部署、逐文件校验；既有版本升级须另行落实备份 |

新项目与部署统一遵循[机器人工作约定](../AGENTS.md)，实机参考见[系统配置](../F1系统配置.md)。
两机保持同名任务：6000D在本目录开发，F1入口为`/home/user/Bruno/F1/foam_sort_whole_body/current/`。
[machine.6000d.yaml](configs/machine.6000d.yaml)与[machine.f1.yaml](configs/machine.f1.yaml)记录路径和职责；
`models/DM0.5/`随任务一起发布，模型权重、服务和接口适配的完成度仍以当前方案及每版部署记录为准。

旧饮料瓶任务的 34D 是双臂 14D + O10 双手 20D。F1 原始 `/hal/joint_states`
的 34 个名字具有不同含义，禁止复用旧字段顺序、归一化统计和部署合同。

## 复现审计

推荐在 6000D 的独立环境运行，依赖见 [requirements-audit.txt](requirements-audit.txt)。
本次实际解释器为 `/tmp/f1-foam-audit-venv/bin/python`；该临时环境不保证跨重启存在。
可用现有 Python 3.10+ 另建虚拟环境，再安装这些依赖。不要修改机器人域控环境。

```bash
./scripts/run_audit.sh metadata
./scripts/run_audit.sh mcap
./scripts/run_audit.sh cameras
./scripts/run_audit.sh previews
./scripts/run_audit.sh screening
./scripts/run_audit.sh videos
```

`F1_AUDIT_PYTHON` 可以指定独立环境解释器，`F1_DATA_ROOT` 指定源目录，
`F1_AUDIT_OUTPUT` 指定输出目录。所有审计仅读原始数据；筛选通过清单表达，不删除原文件。

## 当前决策原则

1. 先区分测量状态、控制命令和后补弱标签。
2. 先修正字段解释与时间轴，再生成训练样本。
3. 本轮主线是单模型联合控制；抓取与移动之间不使用外部规则状态机或导航接管。
4. 本轮将已复核底盘重建轨迹用于当前速度输入与未来动作监督，明确记录其来源；推理使用当前速度反馈。
5. 腰部有状态不等于有足够的腰部动作示教；以最终 MCAP 审计及 F1 实际接口为准。
