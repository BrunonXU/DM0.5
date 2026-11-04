---
doc_type: status
domain: g2_control
status: active
created: 2026-08-24
updated: 2026-09-17
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [project_current_status]
supersedes: [archive/CURRENT_STATUS_THROUGH_20260831.md]
superseded_by: []
related: [robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md, robot/G2_DM05_CONTROL_TODO.md, robot/G2_DM05_NEW_CHAT_PROMPT.md]
---

# 当前进展与下一步

**2026-09-17服务状态更新：** 用户确认7892/7893均为本人服务，并授权暂停以释放F1训练资源。`NewLora-dm05-ws-75k-formal`（GPU7/7892）与`NewLora-dm05-ws-75k-rtc`（GPU3/7893）已停止、端口关闭，容器及权重保留；本条替代下文历史“运行中”描述。停止核对、恢复方法见[F1资源记录](../../F1/foam_sort_whole_body/results/gpu_wandb_setup_20260917/README.md)。

> 2026-09-14整理提示：下文是09-07状态，尚未整篇更新。09-08已采用单34D Sync工作基线，双17D保留回退；最新证据见[控制根因第0节](robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)。不要继续把下文“单34D待验证”视为当前事实。

更新2026-09-07：右食指、中指、无名指、小指单轴canonical名称真机测试完成，操作者确认正常；正式Sync名称修正已完成本地检查，完整抓取复验待办。Sync Docker 7892只读检查为运行中。

## 当前可回退基线

- DM0.5 LoRA V2 / NewLora 75K同步推理已经完成双臂双手连续Servo真机测试，操作者评价“终于丝滑”。
- 入口为 `g2_robot/scripts/19_powered_sync_dual_side_17d.sh`，默认60秒，可设置时长或continuous；旧05仍是Split，不可当作新方案。现场命令仅在[操作指南](../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md)维护。
- 每个100Hz控制tick顺序调用两次 `Robot.joint_servo_control(..., enable_low_latency=False)`：左臂7D+左手10D、右臂7D+右手10D。手名按动态反馈顺序配对，trim后校验已知关节集合，再作为Servo发送名；原始反馈名写日志。右食指尾空格已被运动控制端证实拒绝，禁止继续原样发送。不使用连续 `move_ee_pos`，不使用手部changed-only/deadband/preview。
- 模型仍输出30Hz语义的50×34绝对关节目标；Sync执行前20点约0.667秒，然后重新观测推理。手部线性插值到100Hz；仍有120ms边界融合和物理位置裁剪。双臂保留jerk weight 10、C2 spline、3.5mrad minimum-jerk候选。因此没有改权重，但不是“所有后处理清零”或“50点全部执行”；任务成功率是否保持尚无严格A/B结论。
- Control进程唯一拥有Robot，Policy进程唯一拥有Camera/图像/网络，以34D shared-memory state ring交换状态；不得增加第二个Robot owner。
- 已记录的服务布局：Sync GPU7/7892、RTC GPU3/7893，服务器192.0.2.1；native JPEG/PNG binary WebSocket、75K、5步Euler、fixed-prefix compile。部署记录不是当前存活证明。
- 完整证据和SDK链接见[根因主文档](robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)。旧频率/preview/滤波研究仍有历史价值，但“手部只能10Hz、20/30/50Hz均不可用”不能跨接口套用到100Hz Servo。

## 已验证与不能声称的部分

- arms-only对照丝滑、手部0次发送；左侧0.20rad可见连续Servo成功；随后双侧在线Sync成功。09-07新增右侧四根手指逐轴往返：每轮401次发送，对应控制端窗口未知名称错误均为0；现场确认指定手指动作正常。实际增量约0.141/0.176/0.150/0.157rad（食/中/无名/小），小于0.20rad目标，不能视为跟踪精度完全通过。证据见[逐指报告](../logs/analysis/20260907_sync_servo_audit/right_index_canonical/)。
- 最终日志后缀 `dual_side_17d_servo100_20260904_144901.jsonl` 实际约13.36秒、15个chunk，包含952个segment tick以及327个等待窗手部tick；不是文件名所暗示的300秒长稳。
- segment dispatch gap P95/max为11.427/14.331ms，零次超过15ms；两次GDK合计P95/max为2.029/6.883ms。旧Split的segment gap也没有超过15ms，因此不能把观感改善等价为“调度抖动减少某百分比”。
- 新轮有264条成功feedback、零drop，旧“尚无measured数据”已经过时；这些短样本仍不证明任务成功率或任意场景长稳。
- 实验支持连续手轨迹误用点目标接口是主要架构问题；SDK/固件内部究竟重规划、替换还是排队，以及双臂耦合的具体机制仍未直接观测。
- 新旧完整运行并非所有参数相同（normal/low-latency等也有差异）；最终两份日志不能当作严格单变量配对。
- 双17D候选相关测试记录117 passed；排除沙箱socket受限的process pipeline后154 passed、1 skipped。不是本轮重跑的测试，也不是当前全套160通过。

## 下一步

优先：将已验证canonical名称用于入口19的双17D完整Sync复验；确认右手完整闭合及第二次抓取的双手配合。仅修改发送名，不改模型、C2、边界融合或Control/Policy隔离。此前09-04“丝滑”不能证明右手完整映射或任务成功率。以下三项仍未完成。

1. 验证动态单次34D是否可代替17D+17D；历史静态名称unified-34不能冒充新候选。
2. 在同一新Servo接口、通道和冻结模型输出下，重新评价raw/C2/OTG/Ruckig/滤波，保留旧证据，不默认叠加平滑。
3. 将RTC出口迁移至新Servo并重新标定d/s、评估observation age和新旧chunk接缝；Sync继续作为回退。

细项见[09-07控制待办](robot/G2_DM05_CONTROL_TODO.md)。不靠增大阈值、冻结手部或写死测试时长消除抖动。

## F710 / HIL并行进度及遗留检查

F710执行层已冻结为 `f710-powered-v1-20260904`；离散RT手意图仍用事件驱动move_ee_pos，不能误称它也已迁移双17D。START锁存HIL状态机、34D仲裁和Mock recorder已有纯逻辑闭环；START button 7未现场确认，未接真实policy/相机视频闭环，`training_ready=false`。统一推理加Null/RingBuffer/Video旁路sink仍是待实现决策。最新细节见[F710架构14.18–14.20](../../DAgger/docs/G2/teleop/F710_PINK_OTG_CONTROL_ARCHITECTURE.md)。

代码审计待处理：旧日志filename仍含raw5/changed_only，判配置应读runtime_start；双17D发送路径未调用旧手命令持久化逻辑，不能假定last_hand_command.json反映最新Servo手型。既有O10重启零反馈事故仍有效，做34D静态hold前不能把全零读数直接视为安全当前位置。本轮没有修改上述运行逻辑。

此前完整进度已[归档保留](archive/CURRENT_STATUS_THROUGH_20260831.md)，不再作为当前操作建议。
