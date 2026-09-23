---
doc_type: experiment
domain: g2_control
status: active
created: 2026-08-27
updated: 2026-08-28
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [sync_arm_micro_tremor_ablation]
supersedes: []
superseded_by: []
related: [../ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md, SYNC_ARM_INTERPOLATION_ABLATION_20260827.md, SHARED_HAND_TRAJECTORY_OFFLINE_ABLATION_20260827.md]
---

# Sync双臂原地小幅颤动离线消融

## 问题与假设

共享手部preview第二轮Powered后，操作者仍观察到双臂在几乎不产生净位移时原地颤动。
Telemetry给出命令层直接反例：request 61右臂joint3在0.66秒内净位移仅0.004°，但C2目标
累计往返0.471°、反向3次、活动范围0.182°。发送时序无对应deadline miss，因此本实验
假设残余主要是chunk内部近静止反向，而不是网络、手部或chunk边界。

待验证的问题不是“能否把曲线画平”，而是能否在不延迟真实方向变化、不遗漏显著动作、
不改变端点的条件下，只删除最终100Hz目标中的近静止空走。

## 唯一变量与冻结链路

冻结三份Powered Sync日志：V1.1 46-chunk、共享preview首轮43-chunk、第二轮75-chunk；排除
每轮没有前序arm anchor的首chunk后，共161个可严格重建chunk。固定：

- 原始30Hz模型动作、物理位置clip；
- 双臂内部weight10；
- C2 natural cubic spline；
- 100Hz时间基、前20个30Hz interval；
- 120ms cubic smoothstep边界融合和真实前序命令anchor。

唯一变量是近静止抑制器及阈值。纯数学实现位于
`g2_robot/g2_sort/validation/evaluate_sync_arm_micro_tremor.py`，机器可读结果为：

- `g2_robot/logs/analysis/sync_arm_micro_tremor_ablation_latest_20260827.json`；
- `g2_robot/logs/analysis/sync_arm_micro_tremor_ablation_161chunks_20260827.json`。

## 指标与门槛

小反向定义为相邻100Hz增量换符号，且两侧单步均在`[1e-5, 0.003]rad`。近静止轴以基线
100Hz轨迹范围不超过0.005rad、净位移不超过0.003rad冻结选集，避免候选改变自己的统计
分母。Powered候选必须同时满足：

1. 近静止小反向至少减少50%；
2. chunk最大轨迹偏差P95不超过0.003rad，全局最大不超过0.008rad；
3. 净位移至少0.03rad的显著动作零遗漏，净位移保持率P05至少95%；
4. 0.03rad方向到达延迟P95不超过20ms；
5. 端点误差单独报告，不能用降低总体jerk替代意图门。

## 候选与反例

扫描0.5--8mrad的六类候选：play/deadband hysteresis、局部均值转折投影、三点中值极值
过滤、30Hz近静止线性趋势、速度反向门，以及最终100Hz近静止最小jerk投影。

- play最多能强力压低近静止反向，但从2mrad开始方向到达P95延迟40ms且净位移保持率
  P05降到93.8%；更高阈值继续通过压制意图换观感，否决。
- 局部均值和中值改变waypoint后会被全局C2重新耦合；近静止反向最多反而增加10%，否决。
- 简单速度反向门造成停—放锯齿，全部反向最多增加256%，否决。
- 30Hz趋势投影在边界anchor参与前误判“是否静止”，只能降低约21%的近静止反向，否决。

这些反例说明判断必须针对机器人最终收到的C2+边界融合100Hz目标，不能只看原始模型点或
单独提高weight。

## 胜出候选

`stationary_minjerk_0.0035rad`仅在最终100Hz单轴轨迹满足“至少两次反向、范围不超过
3.5mrad、净位移不超过范围的60%”时触发；它用同起点、同终点的五次minimum-jerk
`10u³-15u⁴+6u⁵`替换该轴。该曲线位置/速度/加速度在端点连续，不是限幅或低通。

| 161-chunk指标 | weight10+C2基线 | 3.5mrad候选 | 变化 |
|---|---:|---:|---:|
| 近静止小反向 | 145 | 65 | -55.2% |
| 全部小反向 | 612 | 532 | -13.1% |
| chunk最大偏差P95 | 0 | 0.002903rad | 门内 |
| 全局最大偏差 | 0 | 0.003325rad | 门内 |
| 端点误差P95 | 0 | 0 | 不变 |
| 显著净位移保持率P05 | 100% | 100% | 不变 |
| 0.03rad方向到达延迟P95 | 0 | 0ms | 不变 |
| 显著动作遗漏 | 0 | 0 | 不变 |

最新run单独复验近静止反向70→34（-51.4%），不是由旧日志支撑的偶然结果。典型request 61
的累计往返从0.477°降到约0.001°，图见
`g2_robot/logs/analysis/sync_micro_tremor_candidate_20260827.png`。

4mrad能把最新run近静止反向降低60%，但chunk最大偏差P95为0.00315rad，越过冻结门，明确
不选；3mrad只降低45.7%，未达到效果门。3.5mrad是本轮离散扫描的膝点，不代表物理SOTA。

## 能证明、不能证明与决策

能够证明：在三份已记录Sync模型轨迹和真实anchor上，3.5mrad最终目标投影比本轮其他
候选更好地满足“少反向且不延迟显著意图”的离线合同；原地颤动不是换C2或继续加weight
即可解决。

不能证明：O10/G2实际闭环振动一定同比下降，也不能证明0.20°内的往返在所有任务上都是
无意义动作。Telemetry没有1kHz measured-state反馈，本实验只证明命令目标，不证明机械
共振或控制器跟踪误差。

决策：保留3.5mrad为唯一候选，淘汰其余候选。2026-08-28已经把同一生产纯函数接入共享
Servo目标路径，默认`off`；正式05 Sync入口显式选择`stationary_minjerk_v1/0.0035rad`，
RTC与其他入口仍保持关闭。runtime记录profile、阈值、触发轴、原始反向数和最大修改幅度，
日志tag为`arm_micro_minjerk_v1`。精确播放终点已同时纳入离线评估与runtime，完整回归
140 passed、1 skipped。6个运行文件已逐项同步G2，并通过远端SHA、源码、CLI和Shell
静态核验。

## 首次Powered复验（2026-08-28）

操作者连续完成两轮正式Sync，共21+39=60个完整chunk；两轮均确认
`stationary_minjerk_v1/0.0035rad`、C2、weight10和共享手部preview实际启用。运行中没有
Servo failure、动作拒绝、QP失败或安全故障。runtime投影在33/60个chunk触发，共覆盖71个
轴实例；这些轴原轨迹共有254次方向反转。

排除每轮缺少前序anchor的首chunk后，对58个chunk用生产代码和真实播放终点做同轨迹反事实：

| Powered同轨迹指标 | C2基线 | 3.5mrad候选 | 变化 |
|---|---:|---:|---:|
| 全部小反向 | 179 | 165 | -7.8% |
| 冻结近静止小反向 | 36 | 22 | -38.9% |
| 触发轴累计往返 | 0.214154rad | 0.039464rad | -81.6% |
| chunk最大偏差P95 / max | 0 | 0.002606 / 0.003190rad | 门内 |
| 端点误差P95 | 0 | 0 | 不变 |
| 显著净位移保持率P05 | 100% | 100% | 不变 |
| 方向到达延迟P95 / 显著动作遗漏 | 0ms / 0 | 0ms / 0 | 不变 |

两轮近静止小反向分别10→6（-40.0%）和26→16（-38.5%），方向一致；触发后的轴累计
往返减少81.6%，说明投影本身确实删除了空走，而不是仅靠某一轮偶然轨迹。但总体小反向只
下降7.8%，冻结近静止下降38.9%，未达到预先声明的Powered效果门“至少50%”。因此它是
安全的局部改善，不是已经验收的完整解法。

非同轨迹描述统计只作为旁证：新两轮合并双臂实际jerk P95为3837.8rad/s^3，低于此前两轮
的5300.7和3994.9；但双臂步幅P95为0.0130rad，略高于此前约0.0118--0.0121rad，不能据此
建立因果。双手jerk P95从最近一轮36.1升到70.2rad/s^3，而本候选完全不修改双手，说明
剩余手部大事件是独立问题。新日志双臂最强残余尖峰多位于未触发的右joint5、左joint4等
较大动作，超出“3.5mrad近静止轴”的治理范围。

时序没有解释这两轮微抖：双臂发送间隔最大19.63ms，零次超过20ms；手部周期约100ms，
没有严重deadline miss。机器可读反事实结果为
`g2_robot/logs/analysis/sync_arm_micro_tremor_powered2_counterfactual_20260828.json`，真机触发
案例图为`g2_robot/logs/analysis/sync_micro_tremor_powered_candidate_20260828.png`。

当前决策：保留Sync候选供继续比较，但不宣布通过Powered效果门、不扩大到4mrad，也暂不
迁入RTC。下一步应扩大“无意义往返”的识别覆盖率，同时保持现有偏差与意图门；不能用简单
增阈值或幅值限额掩盖较大动作中的chunk内部尖峰。
