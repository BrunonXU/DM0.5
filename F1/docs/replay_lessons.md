---
created: 2026-09-17
type: operational_lessons
scope: F1_D070_replay_and_base_diagnostics
initial_remote_snapshot: 2026-09-17T05:13:03Z
evidence_cutoff: 2026-09-17T05:22:23Z
status: evidence_snapshot_not_live_status
---

# F1 使用经验：接口、底盘、回放与诊断闭环

本文把接口文档与实际故障串起来，供换机、交接和恢复工作时使用。**基础阅读快照为 2026-09-17 13:09:01（北京时间），13:13:03 首次取回远端证据；13:22 补审另一 agent 新写出的 30 Hz 反馈分段/停车协同分析及底盘归位交付记录。** 全程只读整理证据，未干预另一 agent 的 replay。任务优先级及最新现场状态仍以[当前方案](../foam_sort_whole_body/docs/training_plan.md)和对应运行记录为准；旧文档未及时回填的地方以带运行 ID 的新证据为准。

阅读入口：[系统配置](../F1系统配置.md)、[接入记录](connect/README.md)、[动作接口合同](../foam_sort_whole_body/docs/action_interface_contract.md)、[现场操作手册](../foam_sort_whole_body/docs/replay_runbook.md)。本页不替代原始日志和任务事故记录。

## 1. 先分清哪些结论已经成立

| 证据 | 可以确认 | 不能据此确认 |
|---|---|---|
| 09-16 两轮 057：各 840 帧、约 28 秒；日志、录制正常收尾 | 两轮原时序关节回放完成；第二轮用户明确报告抓放成功、无明显震动 | 身体起姿平顺、每腿距离正确、EEF/IK 策略执行通过、全部候选可用 |
| 两轮完整 `/move/State`、里程计与控制器参数 | 实际响应显著小于标签要求；当前加速度与重建曲线不匹配是有力原因 | 已找全唯一根因、可任意上调底层参数、旧采集也使用当前配置 |
| 057 重定时候选：1357 帧 / 45.233 秒，109 项回归和 F1 dry-run 通过 | 已修正候选曲线超出当前动态约束的构造问题，且另存时间映射 | 已通过实机距离验收；它也不是原 28 秒录像的新训练真值 |
| 09-17 重启后 10 秒实收底盘 1998 条、头/左腕/右腕图像 292/283/293 帧 | 当次短测中底盘和三路相机恢复出流 | 后续长期稳定、当下模式/起点一定正确、当天新 replay 已成功 |
| 新取回 `20260917T045324Z_325ada5e` 的 report / events / metadata | 09-17 12:54 已完成新一轮原 28 秒 057；840 帧、停车反馈确认、录制正常退出 | 用户物料结果尚未评估；它不是 45.233 秒重定时版本，未据此关闭距离和起姿震动问题 |
| 09-17 本轮 840 份反馈快照的后续分段/协同审计 | 松爪明显变化时仍约 0.139 m/s，放料停车协同不通过；前后/横向响应仍不足 | 程序末尾停车不代表松爪时已经停稳；不能由本轮执行残差反推原录制视频一定错标 |
| `20260917-base-home-v1` 独立归位工具 | 已部署，15 项逻辑测试、只读 status 和其他 5 条候选 dry-run 通过 | 首次归位运动尚未现场验收；只处理局部里程计 XY、不修 yaw，不修复回放中的停车协同 |

对应证据：[第二轮分析](../foam_sort_whole_body/results/replay_review_20260916_051230/README.md)、[原始报告](../foam_sort_whole_body/results/replay_review_20260916_051230/report.json)、[用户现场观察](../foam_sort_whole_body/results/replay_review_20260916_051230/onsite_observation.json)、[底盘全率核对](../foam_sort_whole_body/results/base_controller_review_20260916/README.md)、[重定时候选](../foam_sort_whole_body/results/replay_retiming_057_20260916/README.md)、[09-17 恢复与模式复查](../foam_sort_whole_body/results/live_feedback_20260917/repair/README.md)。

**09-17 最新一轮已补审：** [新 report](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/files/runtime/replay/20260917T045324Z_325ada5e/report.json)给出 `source_timeline_complete=true`、零速反馈确认、录制 exit_code=0、metadata 存在，`onsite_task_success=not_assessed`。[events](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/files/runtime/replay/20260917T045324Z_325ada5e/events.jsonl)的命令 index 完整为 0..839，源时长 28 秒、最大迟到约 2.240 ms，run_end 为 12:54:00.606。840 份反馈快照均硬自动 true、软自动 false、驻车/避障 false；它们说明前序模式阻塞在本次执行期间未出现，不能把更早的 hard_auto=false 当作本次执行状态。

本轮目标 vx/vy 绝对峰值约 0.5921/0.5806 m/s，快照反馈约 0.1900/0.3880 m/s；wz 命令恒为 0，反馈绝对峰值约 0.0735 rad/s。13:13 首次阅读时仅据此确认响应差，没有据峰值认定距离或转向故障。**随后已补齐本轮 840 份约 30 Hz 最新反馈快照的分段积分和松爪协同分析，见下文；它不是完整 `.db3` 的全率分析，也没有外部测距真值。** [metadata](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/files/runtime/replay/20260917T045324Z_325ada5e/rosbag/metadata.yaml)列 16051 条消息、约 28.930 秒；底盘 5787 条，头/左/右图像 841/864/867 帧，底盘和臂命令各 841 条、每侧夹爪命令 840 条。该录制是原 11 路话题，没有新增轮速/转角诊断流。首次远端快照记录数据库为 401645568 字节，只取回报告、事件、metadata 等小文件；后续快照分析也没有独立读完整数据库确认图像可解码和逐 topic 全时段覆盖。

本地新 review 目录曾为空，远端这轮却已结束：**本地分析目录尚未回填，不代表远端没有运行产物。** 反过来，只拿 metadata 而未取 `.db3` 也不能称为录包已经完整备份。远端读取和取回依据见[实时盘点](../foam_sort_whole_body/results/migration_audit_20260917/remote_inventory.json)及[取回清单](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/manifest.json)。

历史文档中的“先测准每腿距离再清洗/训练”是当时决定。09-17 当前方案已明确接受后补速度的距离尺度偏差，数据侧优先抓放停车、驶离/停稳边界、未知与复位隔离；不把精确 0.5 m replay 当作数据清洗门槛。这项工作顺序变化不抹去底盘执行误差，也不解除身体起姿震动问题。

13:22 补审的[本轮执行审计](../foam_sort_whole_body/results/replay_review_20260917_045324/README.md)已指出放料停车协同失败。按最新方案，其他已复核样本仅作诊断对照，不据本次程序执行完成扩大重标或批准正式训练；应核对实际停稳与抓放边界，再验证满足执行动态约束的候选。

## 2. 接口形状相似，语义不能照搬

| 对象 | 本机任务使用的含义 | 容易踩的坑 / 迁移核对点 |
|---|---|---|
| 双臂 `/motion_ctl/joint_ctl` | `JointState`，具名 `arm_l_j1..7`、`arm_r_j1..7`；每侧完整 7 轴；硬件角度为度 | ROS 消息类型不能替代实际单位合同。已录 `/lead` 是左 7 右 7 的硬件目标缓存，不能再做一遍上游 Placo 变号/偏置；只 remap topic 也不会改内部关节名 |
| `/lead` 与 `/hal/joint_states` | 前者是目标来源，后者是测得状态 | 共用目标缓存不等于 UDP、topic 同时发出；`/lead` 不是执行 ACK。复位可能绕过普通 lead，不能跨缺口补成正常示教 |
| 双夹爪 | `GripperCommand.position`：0 全开、100 闭合；本机为 OmniPicker | 0–100 不是毫米。当前实现忽略 `max_effort`，填 0 或示例值都不是夹持力保证；更换夹爪必须重核 |
| 底盘 `/move/ManualMoveCmd` | `TwistStamped.twist` 的 vx / vy / wz，单位 m/s、m/s、rad/s；任务使用车体接口 | `/move/State` 是反馈，不是命令 topic。坐标方向必须同时查合同、录制和实机；不能按字段名推断采集时软件版本 |
| 底盘模式 | 当前回放合同要求硬自动 true、软自动 false，并检查驻车/避障 | “策略自动运行”不表示应切软自动。文档 `SetControlMode.mode=0` 表示软自动，`1` 表示软手动；不能拿它替代硬自动恢复 |
| 底盘姿态 | `State.odometry.orientation.w` **直接存 yaw 弧度**；x/y/z 不使用 | 这个消息虽复用 Quaternion 类型，协议不是标准四元数。`(0,0,0,0)` 不应仅因范数为零判坏；只有导出到标准四元数工具时才显式转换 |
| 身体 body5 | 躯干 5 个具名电机，映射 UDP 电机 3..7；目标为度 | body5 不是“底盘 3 ＋腰 2”。19D replay 不下发 body5；起姿单独驱动 arm14＋body5。旧接口文档标“弃用”不能推翻当前运行二进制已验证的 ROS 路径 |
| 数据维度 | 来源 state21 / 关节 action19；训练 state19 / EEF action23，模型补齐 32D | 数字相同不代表字段相同，不能复用 G2 的 34D 合同。关节 replay 没有经过 EEF 解码和连续 IK，成功不能代替策略执行验收 |

接口依据：[F1 系统接口清单](../F1系统接口清单.md)双臂、夹爪和身体章节；[底盘接口文档](../通用机器人软件接口文档.md)的 `move/State`（约 3110 行）、`SetControlMode`（约 3225 行）、`ManualMoveCmd`（约 3430 行）。任务核对见[动作合同](../foam_sort_whole_body/docs/action_interface_contract.md)、[身体当前接口证据](../foam_sort_whole_body/results/pose_setup_20260916/body_interface_findings.md)、[运行限位证据](../foam_sort_whole_body/results/replay_runtime_fix_20260915/README.md)。

当前两臂限位是运行二进制 SHA 绑定的固定代码，ROS 没有声明全部 28 个限位参数。迁移时要先 List 再 Get，保留“参数不存在”和“参数服务超时”的区别。057 的右 J2 有 582 帧按核定范围截断，最大改动 2.129463°；原始数据保留，`source_action`、`command_action`、`action_adjustments` 分开记录。不能把旧示例范围或任务软件 0.65 m/s 拒绝上限称作厂家额定规格。

## 3. 底盘：积分正确不等于会走到目标距离

后补底盘标签依据视觉阶段与路线先验构造：前后腿约 0.5 m、左右约 1.6 m，采用 smoothstep 的速度导数；它不是从图像直接测出的米制速度。以距离 d、时长 T 构造曲线，其峰值速度为 `1.5|d|/T`，峰值加速度为 `6|d|/T²`。积分等于 d 只证明这条标签自洽。

09-16 `/controller_node` 读回 `max_speed.linear=1.5`、`max_acceleration.linear=0.13`、`max_deceleration.linear=0.5`。两轮反馈典型加速斜率约 0.125–0.139 m/s²，与 0.13 配置吻合；057 首次后退却要求约 1.87 m/s²。8 条已复核来源的 40 个已知移动段里，33 段 smoothstep 超出当前约束，24 段连理想起止静止的最快轨迹都无法在原窗口完成先验距离。这是当前执行条件与派生标签冲突，不是 8 条原始录制都应删除。

完整 bag 分别有 5749 / 5808 条底盘反馈，两轮均有 841 条底盘命令（840 帧加停车）。按接收时间积分并计入每腿后的等待：

| 阶段 | 指令积分 | 第一轮速度积分 / 里程计 | 第二轮速度积分 / 里程计 |
|---|---:|---:|---:|
| 首次后退 | 50 cm | 16.76 / 17.41 cm | 4.32 / 5.25 cm |
| 放料前前进 | 50 cm | 14.80 / 15.86 cm | 16.24 / 16.94 cm |
| 返程后退 | 50 cm | 21.59 / 22.41 cm | 22.06 / 22.87 cm |
| 右横移 | 160 cm | 92.64 / 94.58 cm | 90.74 / 92.36 cm |
| 左横移 | 160 cm | 65.87 / 66.91 cm | 67.89 / 69.40 cm |

指令逐腿积分与先验相差小于 1 mm，不能归因于“50 cm 写成 5 cm”或整段漏发。两轮全程软自动 false、硬自动 true、未驻车、未避障；也不支持这两轮中途模式切换导致差异。首次后退两轮差别很大，提示统一放大速度不是可靠补偿。转向准备、初始轮角、低速响应和采集时配置仍未闭合。

速度与里程计可能共享轮反馈，并非两套独立真值；上表也不是地面尺量或 SLAM 终点误差。轮反馈 `cumulative_mileage` 当次为 0，server 全局位姿未核定，不能随手拿来作真值。每腿分开看，正负腿全程净积分会相互抵消；积分窗口应包括响应尾巴，而不是命令一归零就截止。

证据：[参数 RPC](../foam_sort_whole_body/results/base_controller_review_20260916/parameters_raw.json)、[解码参数](../foam_sort_whole_body/results/base_controller_review_20260916/parameters_decoded.json)、[完整分段计算](../foam_sort_whole_body/results/base_controller_review_20260916/fullrate_analysis.json)、[加速斜率](../foam_sort_whole_body/results/base_controller_review_20260916/observed_acceleration.json)、[40 段动态约束审计](../foam_sort_whole_body/results/base_controller_review_20260916/candidate_dynamics.json)。同节点的 `chassis.threshold.acceleration.linear=1.5` 不是当前 `max_acceleration.linear=0.13`，不能据参数名直接调高。

若后续研究精确距离，应把单段响应、轮速/转角、里程计、实际命令、模式与外部位移测量放在同一证据包。若采用执行重定时，双臂、夹爪和底盘必须共享映射；新日志按新窗口分析，不能套原 28 秒边界。45.233 秒诊断时间轴不能直接替换原始图像时间轴的训练标签。

### 09-17 补充：末尾停车通过，放料时却没有停稳

本轮[分段结果](../foam_sort_whole_body/results/replay_review_20260917_045324/base_response.json)使用 events 中 840 份最新反馈，按实际接收时刻积分，并计入每腿后等待；首次后退、右移、放料前前进、返程后退、左返程分别约 **−0.084、−0.919、＋0.154、−0.220、＋0.633 m**，对应命令约 −0.5、−1.6、＋0.5、−0.5、＋1.6 m。它们是带符号车体轴速度积分，不能冒充外部测量的世界位移。09-16 读取的 0.13 m/s² 参数用于解释历史已知约束；本轮没有重新读控制器参数，不能说 09-17 参数也已复核。

[协同诊断](../foam_sort_whole_body/results/replay_review_20260917_045324/coordination_diagnostics.json)按源录制秒对齐：17.333 秒左夹爪目标从接近 100 降到 43.529 时，派发前最新底盘反馈（17.327 秒）仍约 **0.139 m/s**；17.367 秒目标降到 21.569，反馈仍约 **0.1275 m/s**。视觉审核的停车阶段 17.333–18.800 秒内，直到 17.627 秒仍有大于 0.02 m/s 的快照。这足以拒绝“本轮松爪时已停稳”；0.02 仅是诊断阈值，不是新增或放宽的验收标准。抓取前段 0.1–5.133 秒反馈零速，其他等待段则存在减速尾巴，应逐段核对，不能给全轮一个“停车成功”标签。

物料是否最终落下仍需现场/视频；执行滞后也不能直接当作原录制标签错位的证据。**最终零速、物料抓放结果、阶段内停车协同要分别验收。** 即使接受距离尺度偏差，松爪时继续移动仍是需要处理的协同问题。

终点未回起点另有任务范围原因：既有段落是“后退 → 右移 → 前进 → 后退 → 左移”，**没有最后人工补的前进腿**，理想命令净前后位移本来就约 −0.5 m、横向约 0 m。再加本轮左右反馈约 0.919 / 0.633 m 不对称，不能把终点差全归于跟踪故障，也不能为了闭环回原地伪造原训练数据不存在的最后一步。

## 4. 时间、反馈与录制要单独核验

**至少保留三种时间：源动作时间、运行端单调接收/派发时间、消息 header 时间。** 两轮 `/move/State.header` 比 Thor bag 接收时间约落后 87.581 秒。稳定偏差更符合跨主机时钟不一致，尚未核完各时钟；不能称为 87 秒网络延迟，也不能直接减这个常数去修原 801 条。现有底盘分析按接收时刻对齐，没有把 header 与相机裸拼。

30 Hz 是数据/源动作节奏，31 Hz 是客户端允许的最大派发速率（用于容纳睡眠误差），200 Hz 是底盘反馈文档频率；这三者用途不同。命令 header 在执行时重新生成，源时间只负责调度和追溯。正式 replay 不跳帧、不突发赶进度，保留等待和末帧持续时间；生成的起姿轨迹允许短抖动延长总时长，不等于正式 replay 可以任意拉伸。

双臂目标与“发送前最近反馈”的瞬时差包含跟随滞后。第二轮相对目标平均绝对差约 0.184°、瞬时最大约 7.478°；离线整体移位 3 帧后的拟合支持约 100 ms 滞后的解释，不能据此将未来 HAL 放进当前 state，也不能声称独立测得固定硬件时延。[两轮误差分析](../foam_sort_whole_body/results/replay_review_20260916_051230/analysis.json)保留原始统计。

| 表现 | 已定位的问题及处理 | 验证方式 / 尚不能推断 |
|---|---|---|
| HAL 反馈过旧，但其他反馈在更新 | 单次 spin 可能先处理夹爪/底盘，HAL 仍在队列；改为有界排空回调 | [feedback-drain](../foam_sort_whole_body/results/deployments/20260916-feedback-drain-v1/README.md)有 12 次只读复现与回归；没有放宽 200 ms 新鲜度 |
| 起姿很早就连续迟到 | 到 deadline 后额外做反馈 tick，开销逐帧累积；改为等待期间处理反馈、到点派发 | [schedule-wait](../foam_sort_whole_body/results/deployments/20260916-schedule-wait-v1/README.md)独立 topic 60 秒 / 1800 帧验证；不是身体机械平滑验收 |
| 起姿遇短抖动中止 | 生成轨迹与录制时间轴误用同一种调度；生成起姿逐点延时、不跳点追赶 | [pose-paced](../foam_sort_whole_body/results/deployments/20260916-pose-paced-v1/README.md)注入卡顿测试；正式 replay 保持源轴 |
| rosbag 退出 -6、缺 metadata | 直接异常是 SQLite `database is locked`；就绪检查并发读正在写的数据库，存在锁竞争 | [recorder-ready](../foam_sort_whole_body/results/deployments/20260916-recorder-ready-v1/README.md)改查非空文件、订阅日志/图，录制关闭后检查库；短录制与两轮完整录制通过。TF hash warning 不是该次直接异常 |

读正在 replay 的日志时只记录“截至何时已看到什么”，不要查询正在写入的 SQLite 来推断最终完整性，也不要改写源 events、report。等录制进程退出后核对退出码、metadata、各 topic 数据量与时间覆盖。目录已创建、订阅者已注册、文件非空分别是中间条件，不是录制完成。

## 5. 现场常见故障：按层定位，不通过删检查解决

| 症状 | 原因 / 处理经验 | 证据与边界 |
|---|---|---|
| 有 ROS 环境却导入不了 `move.msg.State` | 本机消息来自 `/home/user/workspace/agv/install/setup.bash`；仅 source Jazzy 和 f1 工作区不够 | [系统配置](../F1系统配置.md)。迁移核对真实 overlay，不复制两机不兼容的 venv/build |
| 臂可见而底盘不可见，或参数查询超时 | 本机 CycloneDDS 绑定底盘网口；motor_ctl 参数另用隔离 Fast DDS 只读进程 | [DDS 对照](../foam_sort_whole_body/results/replay_runtime_fix_20260915/README.md)。controller 参数本次用 Cyclone 可读，不能泛化成“全部参数都必须 Fast DDS” |
| 停止遥操作仍报占用，出现 UNKNOWN 发布者 | 注册发布者不代表正在发命令；观察实际命令并按自身 RMW 身份过滤 | [控制占用修复](../foam_sort_whole_body/results/control_ownership_20260916/README.md)。任何外部命令，包括零速，都触发中止；不是控制器仲裁锁，外部首条命令仍可能先到硬件 |
| `is_hard_auto=false`，但反馈新鲜、速度为零、无外部命令 | 硬模式被切换是独立问题；09-16 拔手柄 USB 后恢复，09-17 再次发生，后者切换来源未确定 | [09-16 复现](../foam_sort_whole_body/results/replay_failure_check_20260916/README.md)、[09-17 复查](../foam_sort_whole_body/results/live_feedback_20260917/repair/hard_mode_recheck.log)。不能改 expected_hard_auto 或切软自动绕过 |
| 网线显示 Link up / 1000 Mb/s，却无底盘反馈 | 09-17 本机 igb `Detected Tx Unit Hang`，ping `No buffer space available`；Cyclone/Fast 两者均无 State | [网口诊断](../foam_sort_whole_body/results/live_feedback_20260917/README.md)。是发送层异常，不等于对端断电或 DDS 选错；恢复深层根因未闭合 |
| 网口 down/up 之后反而无 IPv4 | 实际重连超时，profile 静态地址保留不等于地址已重新生效 | [恢复记录](../foam_sort_whole_body/results/live_feedback_20260917/repair/README.md)。本次现场重启后恢复原 IP 与出流；单次重启成功不证明不会复发 |
| 左腕节点活着却没图 | 本次原驱动报指定 RealSense 序列号找不到；现场接回后同一驱动恢复 | 同上。核实真实三路图像；不拿右腕代左腕，不仅凭 USB sysfs 数字修改 SDK 序列号绑定 |
| preflight 仅列一个起点错误 | 检查可能在身体不符处先失败，后续臂/夹爪尚未全部验 | 同上。09-17 capture 另见右臂 J4、body_j3/4/5 和双夹爪差异，不能把“仅一个报错”解释成其他部件通过 |
| 一轮完成后不能立即重跑 | 任务终态不是示教初态；复位段已被 mask，姿态准备也不恢复地面位置、物料 | [第一轮检查](../foam_sort_whole_body/results/replay_review_20260916_050829/README.md)。需要独立核对关节、底盘位置和场景 |

启动检查应该逐层看：连接和网卡 → ROS/消息/实际出流 → 控制器版本与参数 → 模式和实际命令占用 → 起点与场景 → 录制条件。前一层恢复不代表后一层自动通过。多任务共用机器人时保留待机服务，不凭未知节点名杀进程；真实动作由现场操作者启动。

## 6. 身体起姿是独立的未关闭问题

09-16 16:02 的身体起姿再次明显震动，操作者 Ctrl-C。日志有 152 帧、29.71 Hz、最大迟到 3.23 ms、body_j4 最大相邻目标步长约 0.0832°，没有证据指向长时间调度卡顿。原厂身体起姿名义 200 Hz，客户端用示教 30 Hz；这是值得核对的实现差异，尚未证明升到 200 Hz 就能解决。

停止后独立 5 秒观测中底盘为零，身体角度峰峰变化最大约 0.0042°，支持当时反馈稳定；它不是结构振动的加速度测量。正式 19D replay 不发身体轨迹，因此“回放无震动”和“到位后停止震动”均不能替起姿过程验收。

本次另取回 09-17 12:49 的[起姿报告](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/files/runtime/pose_runs/20260917T044901Z_fc19abf4/report.json)：`mode=to-start`、`motion_started=true`、`target_reached=true`，停车反馈确认，报告时长约 18.767 秒。因此“09-17 尚未起姿/尚未动作”已不是最新事实；但报告没有给出身体机械震动的现场验收，仍不能据到位结果宣称平滑性已修复。报告顶部 `at_demonstration_start=false` 和 differences 是动作前检查，最终 `target_reached=true` 是动作后结果，不能只摘一个布尔值。

旧 `to-start` / `restore` 的暂停要求仍保留，不能因为手柄模式恢复或网卡恢复再次推荐未经修复的起姿入口。只读 status/capture/plan 与动作入口分开。完整日志和原因边界见[身体起姿事故](../foam_sort_whole_body/results/pose_vibration_20260916/README.md)。

保存姿态还要记录它绑定到哪个恢复入口：旧 restore 绑定 09-16 11:38 基线，新 capture 不会自动改写该绑定。09-17 新姿态已另存两机，但不意味着旧 restore 会恢复它。代码回退、关节姿态恢复、机器人地面位置恢复是三件事。

### 新增底盘归位工具的适用范围

09-17 新发布的 `base_home.sh` 保存当前开机的**局部里程计 XY**，由现场操作者独立启动平移返回，不是 SLAM 世界绝对归位，也不修正 yaw；它不发送旋转、双臂、身体或夹爪命令。2.5 cm 是软件到位退出阈值，不是实际绝对定位精度保证。超过 2° 角差、3 m 距离、1 小时备份或检测到重启、运行中里程计跳变/过旧、外部控制接管时会拒绝或停止。即使 Linux 未重启，只要底盘控制器重启/里程计重置，旧起点也应作废重存；不要到回放结束后才 capture，把终点保存成起点。

[归位交付记录](../foam_sort_whole_body/results/base_home_delivery_20260917/README.md)确认 15 项逻辑测试、F1 只读 status 及 003/020/096/093/192 的 dry-run 通过；首次物理归位尚未验收，配套起姿仍沿用旧实现，身体震动没有在此发布修复。它只是准备下一次测试的辅助工具，**归位后回到起点不能抵消前一轮放料时底盘仍在运动，也不插入原训练动作或计入原回放成功。** 操作只维护在[回放手册的归位章节](../foam_sort_whole_body/docs/replay_runbook.md#保存底盘起点与回放后归位)，本页不另存一套可能过时的启动命令。

## 7. 交接时按同一条证据链归档

一次可复核运行至少串起以下内容，缺一项就明确缺项：

1. **发布和入口**：任务代码版本、待发文件 SHA、实际入口解析到的 release、控制器指纹、当前参数、消息依赖与 DDS 配置。`current` 与独立候选 wrapper 可能指向不同 release，不能只看目录名判断正在跑哪个版本。
2. **数据与映射**：来源包 manifest / frames 的 SHA、原始时间区间、mask / split、限位映射；重定时则加映射计划及执行时间轴。原图和历史日志不原地替换。
3. **现场条件**：动作前姿态备份、底盘/物料地面初态、硬软模式、驻车/避障、实际命令监听结果、三路相机身份与出流。
4. **实际运行**：`events.jsonl`、`report.json`、完整 rosbag（metadata 和数据文件）、stdout/stderr；按需录轮速、转角、controller 里程计、STM32 / driver / server 状态。保存 header 与接收时间。
5. **运行后结果**：源时间轴是否遍历完成、录制是否完整、零速反馈是否确认、臂保持是否仅已发布、用户任务观察、每腿响应/位移与余项。用户观察单独 sidecar，不回写程序原始 report。

机器人运行日志在 `/home/user/Bruno/F1/foam_sort_whole_body/runtime/replay/<运行ID>/`；服务器分析与取回证据在 `F1/foam_sort_whole_body/results/`，发布和旧版备份在 `results/deployments/`。**服务器是开发主副本，不代表机器人新产生的运行日志会自动同步回来。** 迁走前还应取回当前运行完整产物、现场改动和当前入口信息；正在录制的 bag 先等正常关闭，再做完整性与哈希校验。

这份经验文档使用服务器既有证据及本次只读取回的新运行记录，没有单凭日志审阅证明“服务器永远最新”。版本同步和迁移依据[发布约定](../AGENTS.md)、具体部署清单及本次[实时比较](../foam_sort_whole_body/results/migration_audit_20260917/comparison.json)核验；参考快照、发布包和整机恢复备份各有不同覆盖范围。

## 8. 本次阅读快照指纹

以下前五项为 2026-09-17 13:09:01（北京时间）基础本地阅读快照，后四项为本次远端取回后读取的文件 SHA256，用于识别后续 agent 更新导致的结论边界变化。下表路径均相对 `F1/foam_sort_whole_body/`；新运行文件另以 `results/migration_audit_20260917/remote_snapshot/files/runtime/` 下的相对路径缩写。

| 本地证据（相对 `F1/foam_sort_whole_body/`） | SHA256 |
|---|---|
| `docs/training_plan.md` | `47c10de4dd4aa5fc5eb0f9e8a62271313f6e5b734de715f5860c25682666e6b8` |
| `docs/incidents/replay_failures_and_lessons.md` | `cb97da628a79ca8d03755206a02822d54c7bd7eef6833c623cb610162f69f3f6` |
| `results/live_feedback_20260917/repair/README.md` | `3b3f97bfb78592f57a3eff5305aba97712f85d67d0fa9a48ef3cb707e6821f66` |
| `results/replay_review_20260916_051230/report.json` | `388135932a7b75d1b2284a8e36eea1c3e0ce94c02fab8dff84d2f8f5f0eecf18` |
| `results/replay_review_20260916_051230/onsite_observation.json` | `cb3399bdf76761a46c825126bc7ad8a97e9e347a79af3f245353d16dc81605d3` |
| 新取回 `replay/20260917T045324Z_325ada5e/report.json` | `388135932a7b75d1b2284a8e36eea1c3e0ce94c02fab8dff84d2f8f5f0eecf18` |
| 新取回 `replay/20260917T045324Z_325ada5e/events.jsonl` | `f2480f2c1b31cb120539130e5113cef77ece9f8cb4606467e78d0a91d2f375fa` |
| 新取回 `replay/20260917T045324Z_325ada5e/rosbag/metadata.yaml` | `b4ac7b1c0078f446b959052a72ce00b4e62401048e08e794518b6d552c8a0ed3` |
| 新取回 `pose_runs/20260917T044901Z_fc19abf4/report.json` | `a60a311c40b7f4734ce78de65ca5cbb5a0d575435816de33ff45fae7934bdd41` |

两轮不同运行的 report 内容恰好相同，因此 SHA 也相同：必须把报告同运行 ID、events、metadata 一起绑定，不能把 report 的 SHA 当作唯一运行身份。上表保留 13:13 首次证据边界；13:22 的后续本地补审增加以下指纹，不覆盖旧快照：

| 后续证据（相对 `F1/foam_sort_whole_body/`） | SHA256 |
|---|---|
| `results/replay_review_20260917_045324/README.md` | `9a448e598a1ceeb17ffd08231375e37bb96db09f6edcc4530b6dd2f7a1e2709f` |
| `results/replay_review_20260917_045324/base_response.json` | `e8700fccf0c43371d26cc9c208f8c6381f560f43176f64bd96047a9d32b825c2` |
| `results/replay_review_20260917_045324/coordination_diagnostics.json` | `92001308c23433489812de7f3dfdd1e2b756f3567d17137c68f498eda8a47680` |
| `results/base_home_delivery_20260917/README.md` | `7d8b993216cff97d9285c89a68351fe5676f83b2fa5a4138bf855595bfefbd2a` |
| 更新后 `docs/training_plan.md` | `9c532831ec5c288ce9fbb66f32724b63b56c2ac681e62070d618bf00747ea91b` |
| 更新后 `docs/replay_runbook.md` | `1fbe7957271592059296180f8b569ba5fd078a9909f155d8da52ddc583a0dd14` |

本轮已有软件完成、30 Hz 反馈响应与停车协同分析；现场物料效果与完整录包检查仍需分别补齐，不沿用 09-16 的用户成功观察，也不把 30 Hz 快照写成完整数据库全率分析。
