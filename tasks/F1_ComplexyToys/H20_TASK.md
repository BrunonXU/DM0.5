# H20任务副本与维护边界

6000D是任务代码与文档的维护源；H20运行经过校验的冻结版本，存放数据、日志和checkpoint。修改先在6000D完成，再发布新版本，避免两个目录互相覆盖。

本页用于找机器上的文件和理解同步方式。当前模型、GPU用途及训练是否运行只看[STATUS](STATUS.md)，现场完整命令看[STARTUP](inference/STARTUP.md)；冻结release内的旧文档只描述当时版本。

## 当前这轮的文件位置

下表是已交付路径，不代表此刻进程在线。使用前按[H20系统档案](../../infra/h20/SYSTEM_PROFILE.md)核对实时资源与身份。

| 内容 | H20路径 / 证据 |
|---|---|
| Full训练冻结代码 | `/home/user/projects/task-releases/f1-toys-20261020-v1-full-r1` |
| Home/mask数据 | `/u01/user/datasets/f1_toys_v1_home_mask_20261020/dataset` |
| Full主run、完整checkpoint和日志 | `/u01/user/runs/f1-toys-v1-full-20261020-r1`；目录细节见[Full实验](training/experiments/20261020-v1-fullsft.md) |
| 统一推理装配 | `/u01/user/inference-framework/unified-ab-full-20261023-r1`；旧Full机器配置装配同路径加`-legacy`。模型/模式/机器登记和边界见[推理recipe](INFERENCE_RECIPE.md#stable-inference-base) |
| Full625/1K/1.5K/2K推理冻结代码 | `/home/user/projects/task-releases/f1-toys-20261021-full-rtc` |
| 原625/1K推理run | `/u01/user/runs/f1-toys-full-625-rtc-20261021`、`/u01/user/runs/f1-toys-full-1000-rtc-20261021` |
| 1.5K / 2K推理run | `/u01/user/runs/f1-toys-full-1500-rtc-20261021`、`/u01/user/runs/f1-toys-full-2000-rtc-20261022`；[本轮验收](artifacts/20261022-full-four-h20/acceptance.json) |
| 部署、身份与验收 | [安装回执](artifacts/20261021-full-h20/installation.json)、[验收回执](artifacts/20261021-full-h20/acceptance.json) |

`/home/user/projects/tasks/F1_ComplexyToys`是H20受管理任务入口，可能指向某个交付release；不要凭目录名认为它永远跟随6000D工作区。操作前读实际软链接和SOURCE_MANIFEST，运行进程以它加载的冻结路径为准。

## 复制什么

- 任务recipe、预处理SOP、配置、共享处理器、reader、训练/推理/导出脚本、测试与Markdown报告；训练必需的原生运行资产随源码保留。
- 本次需要的冻结RLinf runtime、入口与许可证；不复制整个Projects、DM0.5/G2/ROS或另一套LeRobot依赖。
- 大图、原始日志和训练包单独放`/u01`；完整优化器checkpoint留H20，6000D只取推理包与必要结果。
- 凭据沿安全配置使用，不进入task、manifest正文、文档或Git。

2026-10-23起，[stage_h20.py](scripts/stage_h20.py)的新源码包对`Eval/`和各级`visualizations/`只保留Markdown说明，教学PNG、音频、视频及可视化数据不随每次训练重复打包；`artifacts/`仅保留任务根的README导航。完整图文继续在6000D原件/Why&How阅读，需要的额外实验输入单独列入数据交付。新manifest以`source_scope=task-source-and-markdown-v1`记录范围，既有冻结staging和H20 release保持原样。

## 怎样更新

1. 先读[连接说明](../../infra/h20/README.md)、[环境锁](../../infra/h20/ENVIRONMENT.md)、[多人使用约定](../../infra/h20/MULTIUSER_AND_SETUP.md)，复查GPU/CPU/磁盘。
2. 在6000D准备新版本，明确源码、数据和合同身份；既有同名原件先校验。
3. 用既有staging工具dry-run，再同步具体文件，逐文件SHA校验；不得覆盖正在运行或已冻结的release。
4. 需要切任务软链接时，校验后原子切换受管理链接；不替换已有实体目录。模型服务切换另按推理验收流程。
5. 文档整理不等于发布模型。持续阅读以6000D任务原件/Why&How为准，H20冻结快照不跟着文档改写。

相关工具：训练与历史整体交付用[stage_h20.py](scripts/stage_h20.py)；新的同合同推理登记用[统一装配器](scripts/inference_runtime.py)和[定向发布](scripts/deploy_inference_runtime.py)。旧Full RTC staging只作历史实例，不再按checkpoint复制脚本。当前两套训练/数据环境保持独立，不能混装NumPy依赖。

## V1＋DAgger交付（2026-10-22）

九条原件和新数据集已校验交付；H20任务软链接指向`f1-toys-20261022-v1-dagger-r3`。r1预检发现Torch 2.7类型判断编译兼容问题，r3已加入等价修复，完整预检已通过、正式Full已开始；运行状态以STATUS为准。r2仅暂存未运行，原r1失败证据保留。

| 内容 | H20路径 |
|---|---|
| 新冻结代码 | `/home/user/projects/task-releases/f1-toys-20261022-v1-dagger-r3` |
| 数据 | `/u01/user/datasets/f1_toys_v1_dagger_right_only_20261022/dataset` |
| 新run | `/u01/user/runs/f1-toys-v1-dagger-20261022-r3` |
| tmux | `f1-v1-dagger-20261022-r3` |

只使用已授权GPU4–7，0–3现用于旧V1四个Full对比，见STATUS。W&B预检归`00_Engineering_Checks`，正式新run归`04_RightOnly_FullSFT`；不接写旧V1 Full run。实际reader、断点恢复、导出和编译验收见[实施报告](training/experiments/20261021-v1-dagger-fullsft.md)。冻结release中的文档保留冻结时快照，不在原地追写新进度。

## 释放诊断与A/B部署（2026-10-23）

阶段采样A/B两组1250已用统一装配部署；另完成一次100步小样本拟合诊断，诊断权重不作为真机策略。当前在线身份与GPU占用只看STATUS；上述旧Full/DAgger交付表保持原版本含义。

| 内容 | H20路径 |
|---|---|
| A/B训练、推理包与离线结果 | `/u01/user/runs/f1-toys-stage-sampling-20261022-r3` |
| 当前推理装配 | `/u01/user/inference-framework/unified-ab-full-20261023-r1`；旧Full另有同名`-legacy`装配 |
| 恢复Full1K服务代码／run | `/home/user/projects/task-releases/f1-toys-20261023-full-restore-r1`／`/u01/user/runs/f1-toys-full-1000-rtc-20261023`；原模型包不变 |
| 小样本诊断冻结代码 | `/home/user/projects/task-releases/f1-toys-20261023-release-fit-r1` |
| 小样本诊断数据 | `/u01/user/datasets/f1_toys_release_fit_20261023/dataset` |
| 诊断run、100步断点与前后评估 | `/u01/user/runs/f1-toys-release-fit-20261023-r1` |

诊断GPU4–7已释放，W&B独立`06_LearningChain_Diagnostics/diagnostic`；[审计与拟合结论](DataPrehandle/reports/20261023-release-supervision.md)、[A/B部署验收](inference/experiments/20261023-stage-ab-deployment.md)为本次证据入口。冻结release文档不原地追写。

## 历史位置按版本查询

[旧smoke](training/experiments/20261015-v1-smoke.md)、[单条LoRA](training/experiments/20261016-v1-eef-lora.md)、[15条LoRA](training/experiments/20261016-v1-formal-training.md)、[四卡恢复](training/experiments/20261019-four-gpu-resume.md)、[RTC演进](inference/experiments/20261019-rtc.md)。整理前全部路径与交付过程保存在[H20旧说明原文](artifacts/20261021-doc-review/before/tasks/F1_ComplexyToys/H20_TASK.md.txt)。
