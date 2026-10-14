---
doc_type: status
domain: g2_control
status: frozen
created: 2026-09-07
updated: 2026-09-14
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../CURRENT_STATUS.md]
---

# G2 / DM0.5 新对话交接 Prompt

> 2026-09-14整理说明：正文保留2026-09-07的待办/交接快照，其中“单34D未验证”和部分名称处理要求已过时。
> 2026-09-08的单34D工作基线及证据以[控制根因文档第0节](G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)为准；现场命令见[操作指南](../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md)。
> 本次仅归位和标注历史状态，不代表重新执行或验收文中的待办。

整理日期：2026-09-07。下面正文可整段复制给新对话；也可直接要求新Agent完整阅读本文件。

---

你正在接手我的G2 / DM0.5 LoRA V2项目，请先读文件和实际代码，不要根据旧聊天或旧文档重做已经解决的问题。
本机项目根目录是 `/home/xza_100850113/projects`。先读适用的AGENTS.md；不要覆盖已有工作区改动，不要擅自commit。

## 必读顺序

1. `DM0.5/docs/CURRENT_STATUS.md`：2026-09-07审计后的当前状态。
2. `DM0.5/docs/robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md`：完整根因、实验、日志与SDK链接；前半保留历史，注意日期及superseded说明。
3. `DM0.5/docs/robot/G2_DM05_CONTROL_TODO.md`：我的三项后续TODO，均尚未完成。
4. `g2_robot/docs/00_G2_OPERATOR_GUIDE.md`第0节、`g2_robot/docs/G2_ROBOT_STRUCTURE.md`末尾：当前入口与部署记录。
5. 若涉及遥操/DAgger，读 `DAgger/docs/G2/teleop/F710_PINK_OTG_CONTROL_ARCHITECTURE.md`，尤其14.18–14.20，及其引用的冻结基线。

旧实验必须保留，但旧“手部只能10Hz”“入口19未同步”“F710尚待冻结”“RB决定HIL所有权”均不是当前结论。

## 已解决的主问题

我们接近一个月的双臂随灵巧手卡顿，强证据指向执行接口使用方式：连续20D模型手轨迹反复提交给点目标 `move_ee_pos`。
arms-only真机非常丝滑；左侧动态17D静态/可见动作探针成功；随后完整双臂双手Sync通过，现场评价“终于丝滑”。
不要再靠增大阈值、限制手指活动或降低手部采样频率掩盖问题。也不能把SDK内部的重规划/排队机制说成已直接证实，更不能用“2Hz调用×50Hz内部规划=100Hz Servo”解释。

当前基线：100Hz每tick顺序调用两次 `Robot.joint_servo_control(..., enable_low_latency=False)`，
左臂7D+左手10D、右臂7D+右手10D，normal通道。请求使用GDK动态返回的手名原字符串，包含右手已见的尾空格；只在模型维度映射时trim。
连续手部不再调用move_ee_pos，没有手部deadband/changed-only/preview。旧静态名字的unified-34不是待验证的动态34D方案。
F710离散开合意图仍可使用事件驱动move_ee_pos，不能把推理结论泛化成该函数永远不能用。

## 模型和运行基线不能误述

- NewLora / LoRA V2 75K，模型30Hz语义、50×34绝对关节位置；模型维度顺序是左臂7、右臂7、左手10、右手10。
- Sync执行前20点约0.667秒后再观测推理，不是执行完整50点。手部线性插值到100Hz，仍有120ms边界融合及物理位置裁剪。
- 双臂保留jerk weight10、C2 cubic_spline、stationary_minjerk_v1/0.0035rad。没有改模型权重，但不能说完全无后处理，也未证明所有任务成功率不变。
- Control唯一拥有Robot；Policy唯一拥有Camera/图像/网络，34D shared-memory ring传state。禁止为双手、RTC或HIL另开Robot owner。
- 当前入口 `g2_robot/scripts/19_powered_sync_dual_side_17d.sh`：默认60秒，支持 `--duration-seconds N` 或 `--continuous`；旧 `_5s.sh` 只是兼容包装。不要写死5秒，不要误用仍走旧Split的05。现场命令以操作指南第0节为准。
- 服务既有布局：6000D 10.130.153.114，Sync GPU7/7892，RTC GPU3/7893；binary WebSocket、native JPEG/PNG、5步Euler、fixed-prefix compile。09-07文档审计没有重查容器在线，要用时先只读核验。

## 证据与限制

日志目录 `g2_robot/logs/telemetry/powered/`，关键文件：

- 旧Split：`g2_powered_split_sync_500s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_20260903_180504.jsonl`
- arms-only：`g2_powered_split_sync_10s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_arms_only_20260904_141927.jsonl`
- 新双17D：`g2_powered_split_sync_300s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_dual_side_17d_servo100_20260904_144901.jsonl`

新日志实际13.36秒、15chunk，952个segment tick+327个等待窗手部tick；filename的300s不代表完成300秒。
segment gap P95/max 11.427/14.331ms，0次>15ms；两次SDK合计P95/max 2.029/6.883ms。
旧Split的segment gap也0次>15ms。旧手部123次segment+37次等待窗=160次/34.38秒≈4.65Hz，不能漏算等待窗写成3.6Hz。
新反馈264次成功、零drop。现场观感已通过，但没有完整长稳、任务成功率或所有滤波重评结果。
最终新旧运行还改变了Servo通道，不能用这两份非配对日志计算因果“改善百分比”。没有独立右侧动态探针通过的证据，不要补写。

## 下一步三项TODO

1. 17D+17D是否足够，还是能用动态原始名称一次34D减少调用？当前单动态34D尚未实施/验证，保留双17D回退。
2. 新接口下重新评价OTG/Ruckig/C2/平滑滤波；同一轨迹、同一通道、一次只改变一个变量。旧离线数学结果保留，但旧完整真机结论不能无条件继承。不要默认继续叠滤波。
3. RTC换新Servo出口，重新验证并标定d/s、观测年龄与chunk接缝。当前RTC入口还没完成这次迁移。

这些是待办，不是本次已完成的改造；先让我选择下一项，不要同时扩大到训练、RTC、HIL全部重写。

## 并行F710 / HIL状态

F710执行层已冻结 `f710-powered-v1-20260904`，Y同owner Home和滚动workspace recenter已通过现场验收。
START锁存式HIL、34D仲裁、Mock episode因果审计已实现；RB仅是HIL内部臂使能，不决定所有权。
START预期button7尚未现场验证；真实policy切换、三路视频payload和原设备时间戳闭环未完成，training_ready=false。
统一推理链配NullSink/RingBufferSink/EpisodeVideoSink、3–5秒预录是待实现架构，不是已上线功能。

## 代码与已发现的维护点

从 `g2_robot/scripts/19_powered_sync_dual_side_17d.sh`、`scripts/lib/powered_split_runner.sh` 和
`g2_robot/g2_sort/deployment/g2_client/{app,cli,robot_control,control_loop,servo,interpolation,process_pipeline}.py` 追踪。
旧日志tag仍带raw5/changed_only，配置以runtime_start的request_layout、hand_stream_hz与servo_channel为准。
双17D发送路径没有调用旧 `_persist_hand_command`；不能假定last_hand_command.json包含最新Servo手型。
O10重启后零反馈事故仍需注意，当前位置hold不能仅因时间戳新鲜就把全零值直接回发。这些是审计待处理点，不是本轮新增限制或已修复功能。

## 协作要求

我的偏好是尽量保留模型动作能力；不要擅加动作阈值、deadband、硬导数限幅、手部冻结或固定测试时长。
这不授权删除既有硬件保护。修改SDK接口前按g2_robot/AGENTS.md核对官方资料，未知要明确标注。
2026-10-12本地目录归并后，遥操源码位于 `DAgger/G2/teleop/`。下述远端路径是历史部署记录，目录整理未迁移实机。
6000D的g2_robot映射到G2 `agi@10.130.86.13:/home/agi/xza/DM0.5`，G2Teleop映射到 `/home/agi/xza/G2Teleop`。
同步只scp具体文件，G2禁用apt；不通过SSH执行Robot/GDK/Servo/复位/Powered。修改及无硬件测试后，由我在G2现场执行真机命令。
09-07这一轮只更新本地文档，没有同步、运行测试或操作机器人。先用几句话确认你读到的当前基线和下一步，再继续我指定的任务。
