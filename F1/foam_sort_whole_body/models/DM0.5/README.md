# F1 泡沫分拣 × DM0.5

本目录保存任务专用模型配方，通用实现引用 `../../../../DM0.5/opendm/`。
[experiment.yaml](experiment.yaml) 为state19/EEF23/pad32的既定recipe；旧版保存在[历史配置](experiment_state21_joint19.archive.yaml)。训练与离线执行见[任务接线说明](f1_training/README.md)，实际训练和验证状态统一见[当前方案](../../docs/training_plan.md)。
2026-09-15确定、2026-09-16修订输入的[EEF delta单版训练recipe](../../docs/training/dm05_eef_delta_recipe.md)，包含决定、文献和本地代码证据；
2026-09-16新增[任务reader](f1_data/README.md)，2026-09-17接入Exp/Trainer/DataLoader及离线EEF→IK；2026-09-18新增[f1_inference服务](f1_inference/policy.py)，复用同版数据处理和冻结IK。启动、客户端和接口边界见[推理手册](../../docs/inference_runbook.md)。

本目录随任务源码同步到F1。后续本任务的DM0.5训练/推理配方、模型版本引用与客户端适配入口都归属这里，
共享模型实现仍由6000D统一维护。当前推理安排为6000D提供模型服务、F1运行请求客户端；
F1收到本目录不表示已安装模型环境、权重或启动推理。
两机路径由[machine.6000d.yaml](../../configs/machine.6000d.yaml)和[machine.f1.yaml](../../configs/machine.f1.yaml)表达，
推理配置使用[configs/inference.json](../../configs/inference.json)，只将checkpoint留空，release/统计绑定正式772条版本。
上述machine配置仍是部署元数据；推理服务及客户端直接读取inference.json。

正式版：三路图像、任务文本、19D状态（双臂14＋夹爪2＋当前底盘速度3）→ 同一DM0.5的23D实际动作chunk，补齐到原生32D。
23D为双臂各位置3＋连续旋转6、夹爪2、底盘vx/vy/wz 3；手臂采用当前观测锚点的EEF相对轨迹。
双臂和双夹爪均保留，不冻结右侧；body5不进模型，保留原始记录与部署姿态检查，维持相容姿态。当前数据/实施进度见[当前方案](../../docs/training_plan.md)。
已有19D为双臂关节14＋夹爪2＋底盘3，是清洗/关节回放来源，不直接等同策略输出。
已复核底盘重建轨迹同时提供当前速度输入与未来速度监督；推理输入来自`/move/State.speed`，由同一模型联合输出底盘动作。

身体5D指body_j1..5电机反馈，逐轴机械映射仍待核对；它不能提供或唯一确定底盘速度。本轮不输出身体动作。

原始 `/hal` 34D 和旧 G2 动作 34D 都不是本任务的动作布局。
动作按EEF相对位姿、夹爪绝对目标、底盘速度分别编解码，不执行整个向量的 action−state。
共享代码复用、数据处理清单和评估见 [当前训练方案](../../docs/training_plan.md)。
