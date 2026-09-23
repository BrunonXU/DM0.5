---
doc_type: reference
domain: operations
status: canonical
created: 2026-08-24
updated: 2026-09-14
models: [dm05, old_lora, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [documentation_map, chronological_document_index]
supersedes: [numbered_flat_doc_index, document_type_first_hierarchy]
superseded_by: []
related: [AGENTS.md]
---

# DM0.5 × G2 文档

## 先看哪里

| 你正在做什么 | 进入 |
|---|---|
| 看当前做到哪里、阻塞和下一步 | [`CURRENT_STATUS.md`](CURRENT_STATUS.md) |
| 理解项目共同合同和代码边界 | [`core/`](core/README.md) |
| 处理数据、训练、checkpoint、服务器 | [`training/`](training/README.md) |
| 处理部署、推理延迟、RTC、灵巧手和真机 | [`robot/`](robot/README.md) |
| 追溯旧方案和原始历史 | [`archive/`](archive/README.md) |

后续Agent写文档前读 [`AGENTS.md`](AGENTS.md)。目录只解决“去哪里找”，文档类型、状态和日期以每篇开头的metadata为准。

## 最新进展：09-08单34D Servo同步推理

09-08记录已采用单34D normal Servo作为Sync工作基线，双17D保留回退；完整证据和限制见
[控制根因与最新基线](robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)第0节。
[CURRENT_STATUS](CURRENT_STATUS.md)、[控制待办](robot/G2_DM05_CONTROL_TODO.md)和
[对话交接](robot/G2_DM05_NEW_CHAT_PROMPT.md)仍保留09-07快照，部分结论已被09-08记录更新。
平滑重评与RTC迁移另行跟进；2026-09-14仅整理文档，未重验服务在线或真机状态。

## 历史专题：旧O10点目标链路30Hz与50Hz（不能套用到新Servo）

严格按“专门研究30Hz/50Hz”统计，目前有 **1篇实验文档**：
[`O10_TACTILE_AND_COMMAND_RATE_20260825.md`](robot/experiments/O10_TACTILE_AND_COMMAND_RATE_20260825.md)。

若把权威原理入口也算入阅读链，则共 **2篇**：上述实验文档，加上
[`RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md`](robot/RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md)。后者只负责解释RTC、chunk和控制频率，并指向实验报告；30Hz/50Hz实测、结论和下一轮A/B设计都集中在前者，没有拆成多份重复报告。

历史状态：上述50/30/20Hz振动结果和10Hz候选来自旧点目标执行链；不能推导出O10连续Servo只允许10Hz。当前Sync已使用双侧100Hz Servo；RTC新出口和d/s仍待迁移重测。

## 按写作时间倒序的内容文档

以下索引不重复列各级`README.md`和给Agent看的`AGENTS.md`。排序以`created`为主；若后来有实质更新，会同时标出`updated`。同一天内把较新的专题和当前仍活跃的文档放在前面。每篇之间均有独立分隔线。

### 2026-09-07 · [`G2_DM05_CONTROL_TODO.md`](robot/G2_DM05_CONTROL_TODO.md)

**状态：frozen，updated 2026-09-14。** 保留Servo布局、平滑重评和RTC迁移三项原始待办；注明布局结论以09-08根因文档为准。

---

### 2026-09-07 · [`G2_DM05_NEW_CHAT_PROMPT.md`](robot/G2_DM05_NEW_CHAT_PROMPT.md)

**状态：frozen，updated 2026-09-14。** 保留当时的交接背景、证据和协作约束；已标出过时结论及新版入口。

---

### 2026-08-28 · [`MOTION_DIAGNOSTICS_DASHBOARD_IMPLEMENTATION.md`](robot/MOTION_DIAGNOSTICS_DASHBOARD_IMPLEMENTATION.md)

**状态：active，updated 2026-08-31。** 冻结G2只采集、6000D静态出图的轻量仪表盘：按日期倒序浏览每轮run，兼容旧日志，补齐Sync双臂命令和通过probe门控的34维反馈sidecar，并给出日常命令、安全降级与首轮三层验收门。

---

### 2026-08-28 · [`JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md`](robot/JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md)

**状态：canonical。** 定义模型目标、实际下发与34维硬件反馈的统一时间轴，明确不增加动作意图标签；给出活动热图、三层关节轨迹、tracking error、chunk对齐、PSD/coherence、RTC计划分歧和配对A/B等科研图及采集优先级。

---

### 2026-08-27 · [`SYNC_ARM_MICRO_TREMOR_ABLATION_20260827.md`](robot/experiments/SYNC_ARM_MICRO_TREMOR_ABLATION_20260827.md)

**状态：active，updated 2026-08-28。** 3.5mrad最终100Hz minimum-jerk投影已完成两轮60-chunk Powered：触发轴往返减少81.6%、意图和端点不变，但冻结近静止反向只减少38.9%，未过50%效果门；Sync暂留候选，4mrad和RTC迁移继续冻结。

---

### 2026-08-27 · [`2026-08-27_01_SHARED_PREVIEW_POSITION_LIMIT_AND_START_PHASE.md`](robot/incidents/2026-08-27_01_SHARED_PREVIEW_POSITION_LIMIT_AND_START_PHASE.md)

**状态：active，updated 2026-08-27。** 首次共享preview因QP缺少手部物理位置约束被GDK拒绝，并发现startup首周期仅49.6ms；修复版已通过43-chunk Powered，零越界/不收敛/Servo failure且首周期恢复约100ms，事故机制已关闭。

---

### 2026-08-27 · [`SHARED_HAND_TRAJECTORY_OFFLINE_ABLATION_20260827.md`](robot/experiments/SHARED_HAND_TRAJECTORY_OFFLINE_ABLATION_20260827.md)

**状态：active，updated 2026-08-27。** 用169个历史chunk选择共享preview并完成43+75 chunk Powered；手部小反向和jerk显著改善，但大单步仍随轨迹出现。最新日志直接捕获双臂原地颤动：净移0.004°却往返0.471°，下一步做waypoint层近静止反向抑制消融。

---

### 2026-08-27 · [`RTC_SYNC_BASELINE_MIGRATION_20260827.md`](robot/experiments/RTC_SYNC_BASELINE_MIGRATION_20260827.md)

**状态：active。** 审计Sync已验证配置到RTC的真实迁移：确认GPU3/7893已运行相同native binary WebSocket、五步和compile链路，统一RTC Shadow/完整/arms-only为weight10+C2，并把共享手部轨迹器与d/s重标定列为下一门。

---

### 2026-08-27 · [`HAND_V11_OFFLINE_SWEEP_20260827.md`](robot/experiments/HAND_V11_OFFLINE_SWEEP_20260827.md)

**状态：active，updated 2026-08-27。** 用123个历史chunk选择V1.1后完成46-chunk Powered；极端步幅和jerk下降，但42/45个segment的最大内部jerk落在最后三条命令，定位到400ms guide后的尾部追赶并转向共享preview轨迹器。

---

### 2026-08-27 · [`ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md`](robot/ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md)

**状态：canonical，updated 2026-08-27。** 从模型输出、chunk内部、chunk之间、执行端滤波和底层实时性五层解释平滑问题；最新顺序为共享state-continuous/jerk-limited手部轨迹器、短Sync门、7893 RTC Shadow与d/s标定、有限RTC，最后才考虑滤波。

---

### 2026-08-27 · [`SYNC_ARM_INTERPOLATION_ABLATION_20260827.md`](robot/experiments/SYNC_ARM_INTERPOLATION_ABLATION_20260827.md)

**状态：active。** 用最新与此前Powered Sync的82个可重建chunk，在冻结weight10、120ms边界blend和100Hz时间基下比较linear、Hermite、C2 spline、PCHIP与quintic；C2在两组数据上均取得最低jerk，现已显式写入Sync和带tag的Async入口。

---

### 2026-08-26 · [`2026-08-26_01_GPU_INFERENCE_TRAINING_CONTENTION.md`](robot/incidents/2026-08-26_01_GPU_INFERENCE_TRAINING_CONTENTION.md)

**状态：active。** 记录7892与他人OpenPI训练共享GPU4造成1.708秒模型长尾、集中GPU1方案的入口错误与外部争用，以及正式Sync迁到GPU7后的纯Sync端口布局和暖态验收。

---

### 2026-08-25 · [`O10_TACTILE_AND_COMMAND_RATE_20260825.md`](robot/experiments/O10_TACTILE_AND_COMMAND_RATE_20260825.md)

**更新：2026-08-26；状态：active。** 核验O10型号与触觉可用性，记录50Hz振动声、30Hz强震荡、20Hz仍抖动，以及正式Sync回退10Hz稳定基线。

---

### 2026-08-25 · [`NETWORK_TRANSPORT_AB_20260825.md`](robot/experiments/NETWORK_TRANSPORT_AB_20260825.md)

**更新：2026-08-26；状态：active。** 对比N0–N3图片与网络传输方案，并记录native JPEG/WebSocket从7896候选迁入原正式Sync端口7892的部署结果；RTC仍待原7893端口升级。

---

### 2026-08-24 · [`CURRENT_STATUS.md`](CURRENT_STATUS.md)

**更新：2026-09-07；状态：active。** 当前双17D Servo Sync基线、13.36秒真机证据边界、模型能力与后处理区分、三个TODO及F710/HIL未完成项。旧10Hz结论不再作为当前建议。

---

### 2026-08-24 · [`CURRENT_STATUS_THROUGH_20260831.md`](archive/CURRENT_STATUS_THROUGH_20260831.md)

**原更新：2026-08-31；状态：superseded；09-07归档。** 完整保存旧状态中的网络、频率、preview与平滑实验数字和当时判断；当前状态由CURRENT_STATUS替代。

---

### 2026-08-24 · [`TRANSPORT_BINARY_WEBSOCKET.md`](robot/TRANSPORT_BINARY_WEBSOCKET.md)

**更新：2026-08-25；状态：active。** 定义G2到DM0.5的原始JPEG透传、持久WebSocket连接、binary请求和`50×34 float32`动作响应协议。

---

### 2026-08-24 · [`PROCESS_ISOLATION_AND_TRANSPORT_20260824.md`](robot/experiments/PROCESS_ISOLATION_AND_TRANSPORT_20260824.md)

**状态：active。** 记录单Robot-owner进程隔离、shared-memory state ring和binary transport候选的离线门禁结果。

---

### 2026-08-24 · [`RTC_PROCESS_ISOLATION.md`](robot/RTC_PROCESS_ISOLATION.md)

**状态：canonical。** 定义Control唯一拥有Robot、Policy唯一拥有Camera的目标架构，以及进程职责、IPC和故障隔离合同。

---

### 2026-08-24 · [`RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md`](robot/RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md)

**状态：canonical。** 从第一性原理解释30Hz模型waypoint、100Hz Servo、RTC chunk接缝和deadline；O10的30/50Hz专题从这里进入最新实验报告。

---

### 2026-08-24（事故日期不明）· [`undated_03_GDK_INIT_AND_DDS_WARMUP.md`](robot/incidents/undated_03_GDK_INIT_AND_DDS_WARMUP.md)

**状态：frozen。** 记录GDK初始化遗漏与DDS启动初期空状态的现象、根因和处理边界；`2026-08-24`仅为建档日期。

---

### 2026-08-24（事故日期不明）· [`undated_02_G2_TO_SERVER_ASYMMETRIC_ROUTE.md`](robot/incidents/undated_02_G2_TO_SERVER_ASYMMETRIC_ROUTE.md)

**状态：frozen。** 记录G2直连RTX 6000D超时和非对称路由问题，以及网络诊断与恢复结论；事故原日期未知。

---

### 2026-08-24（事故日期不明）· [`undated_01_HEAD_CAMERA_NULL.md`](robot/incidents/undated_01_HEAD_CAMERA_NULL.md)

**状态：frozen。** 记录头部相机持续空帧的故障表现、影响和排查结果；事故原日期未知。

---

### 2026-08-21 · [`RTC_SMOOTHING_ABLATION_20260821.md`](robot/experiments/RTC_SMOOTHING_ABLATION_20260821.md)

**状态：frozen。** 汇总RTC轨迹平滑多轮离线/真机消融，比较C0/C1/C2、Hermite/spline、Servo blend和手部下发问题。

---

### 2026-08-21 · [`INFERENCE_LATENCY_FIRST_PRINCIPLES.md`](robot/INFERENCE_LATENCY_FIRST_PRINCIPLES.md)

**更新：2026-08-24；状态：canonical。** 系统推导五步去噪、fixed-prefix compile、端到端耗时分解与后续优化的收益和证据边界。

---

### 2026-08-21 · [`2026-08-21_02_RTC_EARLY_TERMINATION.md`](robot/incidents/2026-08-21_02_RTC_EARLY_TERMINATION.md)

**更新：2026-08-24；状态：frozen。** 记录Powered RTC未按计划完成100次运行而提前结束的隐藏终止条件、影响和修复。

---

### 2026-08-21 · [`2026-08-21_01_SERVO_DEADLINE_JITTER.md`](robot/incidents/2026-08-21_01_SERVO_DEADLINE_JITTER.md)

**更新：2026-08-24；状态：active。** 记录RTC工作与100Hz Servo同进程时产生deadline jitter的证据，并推动进程隔离架构。

---

### 2026-08-21 · [`RTC_DUAL_GDK_ARCHITECTURE_20260821.md`](archive/legacy_mixed/RTC_DUAL_GDK_ARCHITECTURE_20260821.md)

**更新：2026-08-24；状态：superseded。** 保存曾考虑的双GDK RTC进程隔离方案和真机验证路径；已由单Robot-owner架构替代。

---

### 2026-08-20 · [`DENOISING_AND_COMPILE_20260820.md`](robot/experiments/DENOISING_AND_COMPILE_20260820.md)

**状态：frozen。** 记录Euler 10→5步、fixed-prefix compile、端到端延迟和离线质量对照，是当前推理优化结论的实验依据。

---

### 2026-08-20 · [`2026-08-20_01_RTC_STATE_COORDINATE_AND_SUFFIX.md`](robot/incidents/2026-08-20_01_RTC_STATE_COORDINATE_AND_SUFFIX.md)

**更新：2026-08-24；状态：frozen。** 记录RTC state坐标错误及full-50平滑污染suffix的根因、影响和修复。

---

### 2026-08-18 · [`RTC_IMPLEMENTATION_AND_OFFLINE_REPORT_20260818.md`](robot/experiments/RTC_IMPLEMENTATION_AND_OFFLINE_REPORT_20260818.md)

**状态：frozen。** 报告首轮RTC实现、观测年龄、可调度率、chunk接缝指标和离线A/B结果。

---

### 2026-08-18 · [`RTC_ENGINEERING_DECISION_20260818.md`](robot/experiments/RTC_ENGINEERING_DECISION_20260818.md)

**状态：frozen。** 定义NewLora RTC参数、时序约束、committed prefix语义、验证设计与上线门禁。

---

### 2026-08-18 · [`2026-08-18_01_RTC_ROLLBACK.md`](robot/incidents/2026-08-18_01_RTC_ROLLBACK.md)

**更新：2026-08-20；状态：frozen。** 记录RTC切换回退事故、二次根因、递归回放证据和修复顺序。

---

### 2026-08-17 · [`VISUAL_AUG_PAIRED_REPORT.md`](training/experiments/new_lora/VISUAL_AUG_PAIRED_REPORT.md)

**状态：frozen。** 对NewLora 70K/75K轻量视觉增强进行配对测试，记录质量差异与选择结论。

---

### 2026-08-17 · [`VALIDATION_REPORT.md`](training/experiments/new_lora/VALIDATION_REPORT.md)

**状态：frozen。** 汇总NewLora Validation v1的离线指标、候选checkpoint表现和证据限制。

---

### 2026-08-17 · [`CHECKPOINT_VALIDATION_DESIGN.md`](training/experiments/new_lora/CHECKPOINT_VALIDATION_DESIGN.md)

**状态：frozen。** 从第一性原理设计NewLora checkpoint验证与选拔，包括指标、数据冻结和晋级门槛。

---

### 2026-08-17 · [`CHECKPOINT_75K_DEPLOYMENT_AND_VISUAL_GATE.md`](training/experiments/new_lora/CHECKPOINT_75K_DEPLOYMENT_AND_VISUAL_GATE.md)

**状态：frozen。** 保存75K部署、`20/50`执行窗口和视觉门禁的阶段性状态快照。

---

### 2026-08-13（旧索引）· [`LORA_EXPERIMENTS_OLD_INDEX.md`](archive/legacy_mixed/LORA_EXPERIMENTS_OLD_INDEX.md)

**更新：2026-08-24；状态：superseded。** 保存旧版G2 DM0.5 LoRA实验索引，供追溯迁移前的文档组织和链接。

---

### 2026-08-04 · [`G2_ACTION_SMOOTHING_PLAN_SOURCE.md`](robot/experiments/G2_ACTION_SMOOTHING_PLAN_SOURCE.md)

**更新：2026-08-24；状态：frozen。** 保存动作平滑方案演进、双臂与O10手部历史数据、离线回放及真机验证，是后续平滑文档的来源证据。

---

### 2026-08-04 · [`2026-08-04_01_O10_ZERO_FEEDBACK_REPLAY.md`](robot/incidents/2026-08-04_01_O10_ZERO_FEEDBACK_REPLAY.md)

**更新：2026-08-24；状态：frozen。** 记录O10零反馈被回发后导致拇指顶相机的事故、根因和首条手部命令安全约束。

---

### 2026-07-31 · [`2026-07-31_01_UNIFIED_O10_SERVO_BLOCK.md`](robot/incidents/2026-07-31_01_UNIFIED_O10_SERVO_BLOCK.md)

**更新：2026-08-24；状态：frozen。** 记录Unified 34D/O10解析造成Servo秒级阻塞的事故，并说明改为双臂/双手Split下发的原因。

---

### 2026-07-30 · [`EXTENSION_50K_TO_100K.md`](training/experiments/old_lora/EXTENSION_50K_TO_100K.md)

**状态：frozen。** 记录OldLora从50K延长到100K的训练过程、结果和继续训练的决策边界。

---

### 2026-07-30 · [`CHECKPOINT_VALIDATION_AND_SELECTION.md`](training/experiments/old_lora/CHECKPOINT_VALIDATION_AND_SELECTION.md)

**状态：frozen。** 定义OldLora checkpoint验证方法、证据和修订后的选型协议。

---

### 2026-07-30 · [`G2_35K_HTTP_RUNBOOK.md`](archive/legacy_mixed/G2_35K_HTTP_RUNBOOK.md)

**更新：2026-08-24；状态：archived。** 保存35K checkpoint通过HTTP接入G2 Shadow的历史操作手册，仅供追溯旧部署流程。

---

### 2026-07-29 · [`CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md`](training/CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md)

**更新：2026-08-24；状态：canonical。** 推导从离线checkpoint选型到真机闭环验收的完整流程、指标层级与决策边界。

---

### 2026-07-29 · [`2026-07-29_01_TMUX_KILLED_TRAINING_CONTINUED.md`](training/incidents/2026-07-29_01_TMUX_KILLED_TRAINING_CONTINUED.md)

**更新：2026-08-24；状态：frozen。** 记录tmux被误杀后Docker训练仍继续运行的现场误判、根因和检查方法。

---

### 2026-07-29 · [`G2_CLIENT_RUNTIME_20260729.md`](archive/legacy_mixed/G2_CLIENT_RUNTIME_20260729.md)

**更新：2026-08-24；状态：superseded。** 保存早期G2 Client到DM0.5真机运行时设计；当前部署和时序结论应看robot目录的canonical文档。

---

### 2026-07-28 · [`TRAINING_AND_DEPLOYMENT_CONTRACT.md`](core/TRAINING_AND_DEPLOYMENT_CONTRACT.md)

**更新：2026-08-24；状态：canonical。** 冻结DM0.5 × G2跨训练与部署共同遵守的数据、34D动作、坐标、模型和运行合同。

---

### 2026-07-28 · [`CUSTOMIZATION_MAP.md`](core/CUSTOMIZATION_MAP.md)

**更新：2026-08-24；状态：canonical。** 索引G2相对OpenDM上游的代码改造点、维护边界和对应文档。

---

### 2026-07-28 · [`DATA_AUDIT.md`](training/DATA_AUDIT.md)

**更新：2026-08-24；状态：frozen。** 审计G2分拣数据的结构、动作维度、视频/H5对齐、数据质量和训练可用性。

---

### 2026-07-28 · [`RTX6000D_ASSETS.md`](training/RTX6000D_ASSETS.md)

**更新：2026-08-24；状态：frozen。** 记录RTX 6000D服务器上的数据、checkpoint、缓存、容器和实施资产位置。

---

### 2026-07-28 · [`FORMAL_50K_TRAINING.md`](training/experiments/old_lora/FORMAL_50K_TRAINING.md)

**更新：2026-07-29；状态：frozen。** 记录OldLora正式50K训练配置、执行过程、产物和阶段结论。

---

### 2026-07-28 · [`EIGHT_GPU_500_STEP_GATE.md`](training/experiments/old_lora/EIGHT_GPU_500_STEP_GATE.md)

**状态：frozen。** 记录8卡500-step预热门禁，验证训练链路、吞吐、显存和继续正式训练的条件。

---

### 2026-07-28 · [`2026-07-28_08_RESUME_ADAPTER_DTYPE_CHANGE.md`](training/incidents/2026-07-28_08_RESUME_ADAPTER_DTYPE_CHANGE.md)

**更新：2026-08-24；状态：frozen。** 记录断点恢复后LoRA参数保存dtype变化的问题、风险与校验方式。

---

### 2026-07-28 · [`2026-07-28_07_ACTION_HEAD_32D_TO_34D.md`](training/incidents/2026-07-28_07_ACTION_HEAD_32D_TO_34D.md)

**更新：2026-08-24；状态：frozen。** 记录官方32D base适配G2 34D动作头时可能丢失或截断权重的风险与处理。

---

### 2026-07-28 · [`2026-07-28_06_VIDEO_H5_TIMESTAMP_ALIGNMENT.md`](training/incidents/2026-07-28_06_VIDEO_H5_TIMESTAMP_ALIGNMENT.md)

**更新：2026-08-24；状态：frozen。** 记录H5帧号不能直接当作视频帧号导致的时间对齐问题和正确映射原则。

---

### 2026-07-28 · [`2026-07-28_05_LAUNCHER_STOPPED_CONTAINER_ALIVE.md`](training/incidents/2026-07-28_05_LAUNCHER_STOPPED_CONTAINER_ALIVE.md)

**更新：2026-08-24；状态：frozen。** 记录launcher停止但Docker训练仍存活的状态分裂，以及确认真实训练进程的方法。

---

### 2026-07-28 · [`2026-07-28_04_GLOBAL_BATCH_MISMATCH.md`](training/incidents/2026-07-28_04_GLOBAL_BATCH_MISMATCH.md)

**更新：2026-08-24；状态：frozen。** 记录多卡预热与正式训练global batch合同不一致的问题及其可比性影响。

---

### 2026-07-28 · [`2026-07-28_03_VALIDATION_NORM_STATS_LEAK.md`](training/incidents/2026-07-28_03_VALIDATION_NORM_STATS_LEAK.md)

**更新：2026-08-24；状态：frozen。** 记录Validation错误寻找独立norm stats造成的合同混乱，并明确训练/验证共享统计量边界。

---

### 2026-07-28 · [`2026-07-28_02_IDE_LONG_TASK_TERMINATED.md`](training/incidents/2026-07-28_02_IDE_LONG_TASK_TERMINATED.md)

**更新：2026-08-24；状态：frozen。** 记录长任务绑定IDE终端后被终止的事故和持久任务运行原则。

---

### 2026-07-28 · [`2026-07-28_01_FLEX_ATTENTION_BACKWARD.md`](training/incidents/2026-07-28_01_FLEX_ATTENTION_BACKWARD.md)

**更新：2026-08-24；状态：frozen。** 记录RTX 6000D上FlexAttention backward失败的环境表现、根因范围和回退方案。

---

### 2026-07-28 · [`RTX6000D_SERVER_HANDOFF_20260728.md`](archive/legacy_mixed/RTX6000D_SERVER_HANDOFF_20260728.md)

**更新：2026-08-24；状态：archived。** 保存RTX 6000D服务器早期执行交接、目录和任务状态，现仅作历史参考。

---

### 2026-07-28 · [`PROJECT_ROADMAP_202607.md`](archive/legacy_mixed/PROJECT_ROADMAP_202607.md)

**更新：2026-08-24；状态：archived。** 保存项目早期目标、阶段路线和当时优先级；当前进度以`CURRENT_STATUS.md`为准。

---

### 2026-07-28 · [`OPERATIONS_LESSONS_20260728.md`](archive/legacy_mixed/OPERATIONS_LESSONS_20260728.md)

**更新：2026-08-24；状态：archived。** 保存迁移前的服务器、训练和真机操作经验与踩坑记录。

---

### 2026-07-28 · [`IMPLEMENTATION_STATUS_20260730.md`](archive/legacy_mixed/IMPLEMENTATION_STATUS_20260730.md)

**更新：2026-08-24；状态：archived。** 保存旧实施状态快照；不再代表当前事实，当前状态统一看`CURRENT_STATUS.md`。

## 目录结构

```text
docs/
├── README.md
├── CURRENT_STATUS.md
├── AGENTS.md
├── core/
├── training/
│   ├── experiments/{old_lora,new_lora}/
│   └── incidents/
├── robot/
│   ├── experiments/
│   └── incidents/
└── archive/
```

文件夹只解决“人去哪里找”。文档是原理、实验还是事故，由文件名和metadata说明，不再拆成更多一级目录。

---

### 创建日期未记录 · [`G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md`](robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)

**状态：active，updated 2026-09-14；技术内容更新至2026-09-08。** 原 `SUPER_IMPORTANT/` 根因文档，保留连续轨迹与点目标接口的完整推导、实验和单34D基线。创建日期原文未记录，因此单列，不以mtime推断。
