---
doc_type: experiment
domain: g2_control
status: active
created: 2026-08-27
updated: 2026-08-27
models: [new_lora]
hardware: [g2]
canonical_for: [shared_hand_trajectory_offline_ablation]
supersedes: []
superseded_by: []
related: [../ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md, HAND_V11_OFFLINE_SWEEP_20260827.md, RTC_SYNC_BASELINE_MIGRATION_20260827.md]
---

# Sync/RTC共享手部轨迹器离线消融

## 问题、假设与架构边界

V1.1在chunk边界压低了手部大步，但最大jerk仍集中在每个chunk最后三条命令；RTC临时
`rtc_cap_v2`又是另一套只限制位置步幅的逻辑。若继续分别修补，两种模式会产生不同状态、
不同参数和不同失败方式。

本实验的假设是：Sync与RTC应共享同一个跨chunk状态连续的10Hz手部reference governor。
它只接收当前位置状态和未来20D参考，不读取调度模式，也不在普通chunk/segment边界重置。
内部连续保留`q/v/a`，并对速度、加速度和jerk施加硬约束。两种离线候选使用同一个类、
同一状态、同一约束和同一求解器，唯一变量是能看见的参考：

- point-to-point（P2P）只重复当前目标；
- preview-QP使用模型已经给出的未来waypoint。

这意味着以后Sync和RTC的差别只能是“何时提供新参考、当前能看到多少未来”，不能再各自
维护一份平滑算法。等待新推理结果时，Sync继续给旧终点参考，轨迹器按同一约束受限进入
hold；RTC重规划时继续滚动更新preview。

## 数据、冻结项和候选网格

离线回放覆盖6份正式10Hz Powered Sync日志，共169个chunk、1131条执行窗命令和321个
幅度至少0.1rad的关键半程事件：

| 日志批次 | chunk | 命令 |
|---|---:|---:|
| 2026-08-27 V1.1 | 46 | 309 |
| 2026-08-27 旧V1/C2 | 37 | 252 |
| 2026-08-26 17:46:34 | 18 | 121 |
| 2026-08-26 17:48:04 | 12 | 75 |
| 2026-08-26 17:23:44 | 17 | 111 |
| 2026-08-26 17:26:08 | 39 | 263 |

冻结原始20D模型目标、10Hz、双臂C2/weight10和网络条件。所有新候选固定最大速度
1.75rad/s、最大加速度7.5rad/s²、6步滚动窗口和加速度惩罚0.02；扫描：

- P2P jerk上限30/50/80rad/s³；
- preview jerk上限30/50/80rad/s³；
- preview jerk惩罚0.02/0.05/0.10；
- preview远期参考衰减0.2/0.5。

实现和评估器分别为
`g2_robot/g2_sort/deployment/g2_client/trajectory_smoothing.py`中的
`CausalHandTrajectoryPlanner`，以及
`g2_robot/g2_sort/validation/evaluate_shared_hand_planner.py`。完整机器可读结果保存在
`g2_robot/logs/analysis/shared_hand_planner_169chunks_20260827.json`。

## 门槛与统计口径

每份运行先独立计算指标，再对比例/极值取6次运行中的最坏值；计数跨运行相加。这是保守
run-max，不是把全部样本混在一起的均值。候选必须同时满足：

1. 321个关键事件零半程遗漏；
2. 不比V1.1更早跨过半程，也不增加半程时差P95；
3. 最大单步和tracking error P95不劣于V1.1的保守run-max；
4. 历史回放中求解器零不收敛；
5. 通过上述门后，依次按jerk P95、等待窗最大累计位移和tracking error排序。

旧日志尚未逐条记录Sync等待窗命令，因此等待窗按相邻chunk时间、10Hz相位和旧终点重建。
这足以做同轨迹候选排序，但不等于O10编码器反馈。

## 结果

P2P三档全部被关键时机门淘汰：30/50/80rad/s³分别遗漏80/25/8个半程事件。它没有利用
模型已经给出的未来意图，为限制jerk只能大幅滞后，因此不适合作为正式方向。

30rad/s³ preview仍遗漏8个事件；部分衰减0.5的候选还出现3--8个历史求解不收敛。通过
全部门槛后，最优候选为`preview_j80_jp0.1_d0.2`：

| 保守run-max指标 | V1.1反事实 | 共享preview候选 | 变化 |
|---|---:|---:|---:|
| 最大单步 | 0.1750rad | 0.1750rad | 不增加 |
| jerk P95 | 45.64rad/s³ | 19.53rad/s³ | -57.2% |
| tracking error P95 | 0.1229rad | 0.0747rad | -39.3% |
| chunk末三步出现最大jerk | 154/169（91.1%） | 123/169（72.8%） | -18.3个百分点 |
| 等待窗累计位移P95 | 0.1836rad | 0.1598rad | -13.0% |
| 等待窗累计位移max | 0.3335rad | 0.2839rad | -14.9% |
| 半程遗漏 | 0/321 | 0/321 | 不变 |
| 最大提前量 | 0.000s | 0.000s | 不提前 |
| 求解不收敛 | 不适用 | 0/1519步 | 通过 |

结果支持“模型未来waypoint不是多余信息”：同一约束和求解器下，preview既比P2P更及时，
也比V1.1更平滑。末端jerk集中现象明显减少但没有消失，因此它是下一轮Powered候选，不是
已完成的最终方案。

## 能证明什么、不能证明什么

能够证明：在这6份已记录模型目标的同轨迹回放中，共享preview候选优于本轮P2P网格和
V1.1反事实；Sync和RTC没有必要维护两套手部平滑状态机。

不能证明：真实O10机械响应、接触时抓取成功率、RTC不同重规划时刻下的闭环稳定性或Linux
调度长尾。回放使用下发目标而非电机反馈；旧等待窗是重建值；随机极端参考的额外压力测试
也不能代替真实任务分布。求解器即使超时仍有逐步投影的安全约束兜底，但上真机前必须把
收敛状态、迭代数、速度/加速度/jerk命中和active/wait阶段写入telemetry。

## 决策和下一门

离线保留`preview_j80_jp0.1_d0.2`，淘汰本轮P2P候选。随后已将同一组冻结参数封装成唯一
`shared_preview_v1` runtime profile：Sync和RTC CLI引用同一个planner实现，控制状态机把
当前chunk未来参考传给10Hz手线程，普通segment边界不重置q/v/a；正式05 Sync入口已选择
该profile。短暂网络恢复后8个运行文件已逐项同步G2，远端源码compile、CLI解析、Shell语法
和SHA256一致；没有初始化GDK或发送动作。RTC 09脚本本轮仍保持`rtc_cap_v2`，避免在第一
次Powered时同时改变调度模式。

## Powered反例与软约束修正

首次Powered前5个完整chunk主观明显更顺，但第6次请求因QP生成`-0.000186366rad`的左手
位置微小越界被GDK拒绝。该反例证明两处原设计不成立：仅在QP前clip原始目标不能保证QP后
输出合法；80rad/s³硬jerk盒约束也会通过截断快速意图换取平滑观感。

修正版仅把20D官方位置范围作为硬约束，速度/加速度/jerk全部改为warning-only；轨迹形状
由tracking、acceleration、jerk软代价、跨chunk q/v/a和6步preview共同决定。重新扫描
169个chunk后选择`acceleration penalty=0.02, jerk penalty=5.0, decay=0.2`：321个关键
事件零遗漏、jerk P95为14.60rad/s³、tracking error P95为0.0932rad、最大步幅自然为
0.1700rad、1519步零不收敛。penalty=10开始遗漏6个关键事件，明确否决继续追求更平。

同时修复startup delay未推进deadline的问题；旧首段前两次手命令只隔49.60ms，修复后
第一个10Hz周期也必须完整等待100ms。事故与证据详见
[`../incidents/2026-08-27_01_SHARED_PREVIEW_POSITION_LIMIT_AND_START_PHASE.md`](../incidents/2026-08-27_01_SHARED_PREVIEW_POSITION_LIMIT_AND_START_PHASE.md)。

## Powered软约束复验

修正版已在7892完成43个完整chunk、286个QP step的Powered Sync。位置约束开启、导数约束
关闭，QP零不收敛、无位置越界、无Servo failure；首段前三个实际手命令间隔为
97.89/101.79ms，确认旧49.60ms首周期缺陷已修复。

与46-chunk V1.1真机run作非同轨迹描述性比较：手部最大单步P95从0.1750降到
0.1315rad（-24.9%），实际jerk P95从142.9降到62.1rad/s³（-56.5%），实际加速度
P95从10.94降到4.92rad/s²。主观“更顺”有telemetry支持，但手部最大单步仍达到
0.1525rad，尚未完成任务意图/成功率门。

该run同时暴露双臂剩余问题：双臂最大单步P95从0.01261降到0.01016rad，但实际jerk
P95从3429升到4337rad/s³。不同模型轨迹使该变化不能归因为preview；不过43个chunk中
只有4个chunk的最大双臂jerk落在120ms边界blend，说明当前细碎高频抖动主要是双臂
chunk内部轨迹，而非手部或边界本身。结论必须分开：肉眼可见的大幅位置突跳仍由灵巧手
主导，细碎高频抖动则双臂仍是主要来源。

对比图保存在`g2_robot/logs/analysis/shared_preview_soft_vs_v11_20260827.png`。下一步先对该
43-chunk双臂原始模型waypoint、C2输出和真实发送时间做同轨迹分层，不继续盲目加强手部
penalty；确认关键动作未被软化后，再把同一手部profile接入7893 RTC Shadow并重标定d/s。
不允许为RTC复制、改名或私自调另一套参数。

第二轮软约束Powered持续75个chunk、约66.6秒，同样零Servo failure和QP失败。相对第一轮，
E2E-to-Control P50/P95从204.8/237.4ms小幅降到203.3/234.8ms；手部实际小反向保持约
1.28次/chunk，远低于V1.1的20.57次/chunk，手部jerk P95进一步从62.1降到49.6rad/s³。
但手部单步P95回升到0.1630rad，说明大幅手指事件仍随模型轨迹变化，不能只看jerk。

操作者报告的“原地小幅颤动”在双臂命令中有直接证据：原始30Hz双臂waypoint小反向从首轮
81.9增到90.8次/chunk；weight10将第二轮压到14.5次/chunk，C2目标再压到约3.84次/chunk，
但未归零。request 61右臂joint3在0.66秒内净位移仅0.004°，累计往返0.471°、3次反向，
活动范围0.182°。同轨迹五种插值中C2仍把chunk jerk-max P95从linear的4899.6降至
2096.6rad/s³，且PCHIP/普通三次的反向次数没有实质优势，因此不应通过换插值解决。
证据图为`g2_robot/logs/analysis/sync_micro_tremor_soft2_20260827.png`。下一候选应在双臂30Hz
waypoint层做“近静止反向抑制/迟滞”离线消融，并同时约束净意图误差；不得直接提高weight或
加末端低通，以免再次用压制动作换取观感。
