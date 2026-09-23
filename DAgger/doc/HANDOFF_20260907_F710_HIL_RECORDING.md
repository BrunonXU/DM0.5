# F710 / HIL / DAgger 对话交接（2026-09-07）

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](../RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

> **最新首轮Powered HIL已有真实日志：** 155216轮4次人工介入、3次fresh恢复，769组三路payload审计通过；控制中位10ms、人工全程1倍速。完整标签/跨流审计未通过，不能训练。见[数据流证据、问题与等待裁剪建议](POWERED_HIL_FIRST_FLOW_REVIEW_20260907.md)。下述“尚未真机启动”为之前快照。

> **当前唯一下一场景入口：40号 Powered 同步 POLICY↔F710 HIL 候选。** 用户要求直接真实场景，已实现同owner动作接入、录制与离线测试，尚待用户首次真机启动。见[Powered HIL命令、证据和限制](POWERED_HIL_CANDIDATE_20260907.md)。不再让用户用12或Shadow代替Powered HIL验证。

> **最新优先级：用户跳过剩余网络 Shadow 专项现场测试，直接进入真实采集/policy 零输出阶段。** 39 候选已实现；本轮本地 143 passed / 6 skipped，G2 无硬件测试 9 passed。现场尚未启动，Powered HIL 尚未接入。入口与限制见[真实观测 Shadow](HIL_REAL_OBSERVATION_SHADOW_20260907.md)。下面此前的“下一现场门”按历史快照阅读。

> 首轮网络Shadow已完整封口：15694条、4150输入包零拒绝，两次模拟fresh恢复；RB/START主路径成立。RT/退出互锁/迟到response现场覆盖待补，仍无Powered HIL。详见[完整日志结论](HIL_NETWORK_SHADOW_20260907.md)。

> 现场Shadow已有首轮活动日志：两次HIL→HOLD→模拟新policy恢复，RB松开仍留HIL。存在watchdog，RT等覆盖待补；详见[日志快照结论](HIL_NETWORK_SHADOW_20260907.md)。尚未Powered。

> 下一现场门已准备：[Windows→G2零输出HIL入口与验收步骤](HIL_NETWORK_SHADOW_20260907.md)，独立端口7906；尚待现场运行。

> **同日后续实现已完成：** [离线实现、新验证与未决项](HIL_RECORDING_OFFLINE_20260907.md)。
> 下文是实现前的交接快照；Mock时间缺口、fresh绑定及三种sink已在后续修复/实现，真实HIL仍未Powered。

> **现场后续：START原始按钮只读验证通过。** 用户运行独立probe，六次START均为button=7 DOWN/UP。
> 仅确认Windows物理键号；sender→协议→G2 HIL及Powered切换仍未验收。证据见同日离线报告末节。

本页按2026-09-07本地代码和文档核对整理。它区分实机验收、离线原型和设计，优先于旧文档的阶段性表述。本次只更新文档；没有重新进行真机实验或重跑历史全量测试。

## 1. 当前进度

- **F710遥操已真机验收并冻结**：`f710-powered-v1-20260904`。Windows F710 50Hz latest-state；direct Pink机械臂参考通过normal Servo以100Hz执行；O10使用约5Hz changed-only渐进点目标。Ruckig不是默认主链。冻结manifest在`configs/f710_powered_v1_baseline.json`，回滚归档在`baselines/`。
- 操作已支持：先十字键选侧并释放，再按RB控制臂；RT独立于RB开合所选手；A单击切换1/2倍速度，启动低速；Y在同一Robot owner内回全局Home。rolling envelope解决固定接管包络造成的“像断链”停顿。
- 冻结验收日志`g2_powered_f710_20260904_132248.jsonl`：历史分析记录18次recenter（平移6、旋转12），workspace saturation、watchdog、IK HOLD、runtime fault及telemetry drop均为0。这是该轮证据，不是所有运行永不出错的保证。
- **HIL纯逻辑原型已完成**：`hil_supervisor.py`、`hil_arbiter.py`。只有START toggle改变POLICY/HIL所有权；RB无快速接管入口；松RB仍在HIL；退出需松RB且手动作完成，随后HANDOFF_HOLD等待新epoch；失联SAFE_HOLD。
- **journal原型已完成**：`hil_episode.py`记录observations、policy_chunks、control、events四条JSONL；`.partial`关闭发布；图像只有引用，`training_ready=false`。
- **尚未完成**：真实policy/F710共同owner接入、三路图像payload sink、时间域与反馈对齐、训练转换、START/button7现场只读验证、Powered HIL切换和正式DAgger录制。
- 历史回归记录：本地116 passed / 6 skipped；G2 HIL纯测试13 passed。脚本33/34已在本地和G2做过模拟journal生成/审计。此处保留历史结果，不称为9月7日新验收。

## 2. 本次发现的文档/原型边界

1. 架构文档开头的OTG流程和早期91秒replay是历史方案。当前F710默认direct Pink；原速replay入口为`scripts/32_g2_source_speed_qdirect_o10_raw5.sh`，不能再把91秒称为原速。
2. `33_hil_mock_episode.py`是合成状态序列，不是真实推理/相机/执行器闭环：executed和measured直接赋值为selected。审计通过只能证明这些模拟字段内部一致，不能证明硬件实际收到了同样的命令。
3. Mock每3个10ms tick记录一次观测，实际模拟间距30ms（33.33Hz），不是严格30Hz。它还在tick38引用observation 13，而该观测到tick39才记录；审计器尚未核对跨流引用和先后关系。下一步应先补真正的观测关联、因果时间与故障注入测试。
4. `accept_fresh_policy_chunk`目前检查状态与epoch，但没有验证该request实际关联退出HIL后采集的新观测。因此“fresh”是调用方责任，真实runtime必须落实，不能仅换epoch标签。
5. 30Hz是模型action waypoint时间合同，不等于每秒30次推理请求，也不保证现有相机/观测采集达到30Hz。录制采集节奏需独立明确，尤其HIL期间暂停推理仍须采集；主采集源可持续产出，policy和recorder消费同源数据。
6. 原始相机时间戳必须保留单位和时钟域，另记host接收monotonic；不可把设备时间戳直接与控制monotonic相减。要检查重复帧、缺帧、三路偏斜及state采样时间。O10反馈全零或无效不得标为可信实测，更不能用目标伪装反馈。
7. 真实执行中selected、裁剪后提交目标、SDK返回、左右臂/手实际调用时间和measured必须分开。原型的selected==executed审计需要扩展，不能据此否认合法的裁剪或多接口执行差异。

## 3. 另一条控制支线已有新进展

`DM0.5/docs/robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md`已记录9月4日DM0.5连续手轨迹通过**左右各17D、动态原始手关节名称、100Hz normal Servo**真机测试；对应实现位于`g2_robot/g2_sort/deployment/g2_client/robot_control.py`，日志位于`g2_robot/logs/telemetry/powered/`，文件包含`dual_side_17d_servo100_20260904_144901`。

该文档记录用户“终于丝滑”的现场评价。当前本地存在代码和对应日志，但本次没有重新量化整轮日志。不能再把17D说成尚未开始的支线；也不能把它默认为已迁入F710。F710离散手意图仍是冻结的changed-only路径。未来POLICY/HIL切换要明确两种手部执行语义如何在同一owner交接，不能同时开两个Robot进程。

单请求动态34D是否能替代两个17D仍需单独核查最新记录；不要把历史名称不匹配的34D失败概括为所有34D请求永远不可能。SDK内部重规划机制目前是外部行为支持的解释，不能声称已经看见其私有实现。

## 4. 录制方向与下一步

已讨论采用同一推理/控制主链，可插拔旁路sink：`inference-only`只留内存及轻量telemetry；`hil-record`完整落盘；推荐的`prebuffer-record`先在有界内存保存约3–5秒上下文，START介入后提交并持续录制。这些模式和`TimestampedObservation / NullSink / RingBufferSink / EpisodeVideoSink`目前都是待实现设计。

下一步顺序：

1. 修正上述Mock因果时间/跨流审计缺口，明确观测ID、request ID、policy epoch、control sequence及action label定义。
2. 核对真实GDK相机消息的时间戳字段、单位/时钟域。比较DM0.5旧camera adapter与g2_robot较新的原生JPEG/PNG passthrough；不要凭空猜字段或重复建设。
3. 定义同源带时间信息的图像/state接口，实现有界内存缓冲和异步payload落盘。原生JPEG/PNG帧保存不等于已经得到可用MP4/H265；视频封装/编码在后台或离线完成，并保留精确帧索引映射。
4. 测试慢盘、队列溢出、异常关闭、重复帧、跨流引用、缺失反馈、handoff迟到chunk；drop显式记录且数据不能静默进入训练。评估内存/CPU/I/O开销，不能只因使用线程就宣称完全不影响控制。
5. 对齐DM0.5三路视频+时间戳+34D训练合同，明确HIL active/hold/Home和未被人工控制维度的标签选择；完成审计前保持training_ready=false。
6. 再验证Windows START/button7，接真实policy的无输出HIL路径，最后由用户启动Powered测试。

## 5. 协作规则与入口

- 可以自行改代码、离线测试和同步到`agi@10.130.86.13:/home/agi/xza/G2Teleop`；Powered测试由用户操作，届时给明确命令和日志路径。不要自动启动真机或另建Robot owner。
- 现场确认使用ENTER；不要增加RUN DYNAMIC O10等文字口令。不要把数据集手部大跳变自动判为数据错误并恢复任意实验门限；保留明确硬件故障处理。
- F710接收器只插Windows；A是单击切档，RT无需RB，START是HIL锁存键，没有RB快速入口。
- 保留冻结控制基线和用户已有改动；每轮记录改动、验证、日志、结论与未决项。
- 不展开RTC调度算法；确需接policy时按真实代码合同处理接口，先完成录制与HIL主线。

继续阅读：本目录`F710_PINK_OTG_CONTROL_ARCHITECTURE.md`、`G2_F710_DAGGER_FULL_FLOW.md`、`START_HERE_WINDOWS_G2.md`；跨项目参考`g2_robot/docs/GDK_2.6.3_RELEVANT_API.md`、`DM0.5/docs/robot/JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md`和上述SUPER_IMPORTANT文档。先检查各目录AGENTS.md，跨项目代码可能不是同一版本，按实际入口核对。
