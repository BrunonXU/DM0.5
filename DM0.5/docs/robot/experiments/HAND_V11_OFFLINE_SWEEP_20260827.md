---
doc_type: experiment
domain: g2_control
status: active
created: 2026-08-27
updated: 2026-08-27
models: [new_lora]
hardware: [g2]
canonical_for: [hand_v11_offline_sweep]
supersedes: []
superseded_by: []
related: [../ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md, G2_ACTION_SMOOTHING_PLAN_SOURCE.md, O10_TACTILE_AND_COMMAND_RATE_20260825.md]
---

# O10 10Hz手部V1.1离线扫参与部署候选

## 问题与假设

最新正式C2 Sync的37个chunk中，双手chunk最大单步P95约0.1997rad，V1累计命中速度/
加速度限制26/54次。当前2rad/s在10Hz下允许约0.2rad一步，现场也观察到少数明显陡变。

假设：适度降低手部单步、速度和加速度，并把大幅方向反转hold限制在400ms边界内，可以
降低内部尖峰；但参数过小会让手指在0.667秒执行窗内追不上闭手意图，因此必须同时检查
跟踪误差和关键动作到位，不能只按jerk排序。

## 数据、冻结变量与评估器

评估覆盖三批、五份正式10Hz Sync日志：最新C2 37个chunk、此前30个chunk和更早56个
chunk，共123个chunk、822条执行窗命令和233个幅度至少0.1rad的关键半程事件。冻结模型、
原始20D目标、10Hz、400ms cubic边界guide和0.15rad反转误差阈值，只改变：

- 内部单步上限；
- 最大速度；
- 最大加速度；
- 方向反转hold是否只在边界窗口生效。

评估器为`g2_robot/g2_sort/validation/evaluate_hand_v11_sweep.py`，产物为：

- `g2_robot/logs/analysis/hand_v11_sweep_c2_latest_20260827.json`；
- `g2_robot/logs/analysis/hand_v11_sweep_previous30_20260827.json`；
- `g2_robot/logs/analysis/hand_v11_sweep_previous56_20260827.json`。

旧telemetry没有把Sync推理等待窗内的10Hz手命令逐条归入`dispatch_summary`。评估器使用
相邻`action_chunk.wall_time`、已记录的10Hz相位和执行终点20D重建等待窗hold命令，再连续
携带候选tracker状态。旧V1回放相对真实命令的偏差P95约0.0022rad、最大约0.0061rad，
说明该重建足够用于候选排序，但仍不等于编码器反馈或真实O10运动。

## 第一轮：过强平滑被关键动作门淘汰

首轮网格为0.05/0.075/0.10rad、0.5/0.75/1.0rad/s和3/5rad/s²。0.05rad方案虽然把
jerk P95降至约19--22rad/s³，却在各批出现29--45个关键动作未达到半程；0.10rad候选在
历史任务中仍遗漏最多7/106个。它们属于“通过追不上目标换取平滑”，不进入Powered。

## 膝点复验与选择

第二轮搜索0.10--0.18rad、1.25--1.8rad/s和5--10rad/s²。最终选择：

```text
profile                  v1_1
max delta                0.175 rad / 10Hz command
max velocity             1.75 rad/s
max acceleration         7.5 rad/s²
reversal hold            only inside 400ms boundary guide
```

| 数据 | 指标 | 旧V1实际 | V1.1反事实 | 变化 |
|---|---|---:|---:|---:|
| 最新37 | 最大单步 | 0.20034 | 0.17500 | -12.6% |
| 最新37 | jerk P95 | 45.39 | 38.17 | -15.9% |
| 最新37 | tracking error P95 | 0.0671 | 0.0922 | +0.0251rad |
| 最新37 | 关键半程遗漏 | 0/71 | 0/71 | 不变 |
| 此前30 | 最大单步 | 0.20000 | 0.17500 | -12.5% |
| 此前30 | jerk P95 | 49.87 | 42.67 | -14.4% |
| 此前30 | tracking error P95 | 0.0509 | 0.1229 | +0.0720rad |
| 此前30 | 关键半程遗漏 | 0/56 | 0/56 | 不变 |
| 更早56 | 最大单步 | 0.20049 | 0.17500 | -12.7% |
| 更早56 | jerk P95 | 47.70 | 45.64 | -4.3% |
| 更早56 | tracking error P95 | 0.0890 | 0.0886 | -0.0004rad |
| 更早56 | 关键半程遗漏 | 0/106 | 0/106 | 不变 |

V1.1不是“最平”的候选，而是三批均不丢关键半程事件时jerk改善最稳定的保守膝点。
tracking error在此前30个chunk上明显增加，说明真机必须重点观察闭手是否变慢；离线门
只支持短时候选，不能直接宣布正式稳定。

## 实现、回退和验证状态

运行时新增固定`v1_1` profile，参数不能因手写CLI悄悄漂移。正式Sync入口显式选择
`v1_1`，日志文件名增加`hand_v11`；启动信息和`runtime_start`同时记录profile、速度、
加速度和单步上限。显式传`--hand-smoothing-profile v1`即可回退旧算法。

6000D无GDK相关回归为106 passed。五个运行文件已逐文件同步G2，远端Python/Shell语法、
CLI解析与SHA256一致；同步过程没有初始化GDK、Camera、Robot或Servo，也没有发送动作。

## 决定与真机门

下一步只做一次正式Sync短时单变量Powered：双臂C2、weight10、120ms、网络、10Hz和任务
其余部分全部冻结，只把手部V1换成V1.1。必须同时检查：

1. 主观陡变和声音是否下降；
2. 实际最大单步是否被0.175rad约束；
3. jerk、tracking error、限幅和等待窗连续状态；
4. 闭手、抓取和放置时机是否明显变慢；
5. O10/GDK、Servo和deadline是否正常。

任一任务意图明显恶化则立即回退V1，不继续靠更低cap强行平滑。通过短Sync后才把V1.1
作为RTC内部手部候选；RTC跨chunk方案仍是独立变量。

## Powered结果与尾部追赶定位

随后正式V1.1运行保存46个`dispatch_summary`；首段缺少进程内手部锚点，因此位置统计
使用45个可配对segment。操作者主动停止不作为故障。与旧V1的37个chunk对照如下；两轮
模型轨迹不同，所以这是现场分布比较，不是严格同轨迹因果A/B：

| per-chunk峰值口径 | 旧V1 | V1.1 |
|---|---:|---:|
| 手部最大步幅P95 / max | 0.1997 / 0.2003rad | 0.1750 / 0.1750rad |
| hand jerk P95 / max | 221.5 / 297.9rad/s³ | 142.9 / 206.6rad/s³ |
| tracking error P95 / max | 0.1283 / 0.3759rad | 0.1880 / 0.4115rad |
| Sync等待窗累计位移P95 / max | 0.1013 / 0.3995rad | 0.1041 / 0.1718rad |

V1.1把最坏单步、jerk和等待窗极值压低，但通过更大的tracking lag换取平滑。位置分布
进一步揭示剩余问题：15个至少0.12rad的大步中11个落在最后三条10Hz命令；45个可计算
内部jerk的segment中42个最大jerk落在最后三条，31个落在最后两条。每段通常只有6--7条
手部命令，400ms boundary guide先占约4条，剩余误差因而集中到最后2--3条释放；同步推理
等待时后台手线程又继续追最后一个旧目标。该规律说明V1.1是有效限幅基线，但不是应直接
复制进RTC的最终轨迹器。

后续不再继续缩小固定cap。下一候选必须跨segment保留位置/速度/加速度状态，以完整未来
waypoint做滚动preview并显式限制jerk；无法在当前chunk安全完成的误差允许平滑带入下一段，
同步等待窗则受限减速到hold。该实现必须由Sync与RTC共享，避免`v1_1`和`rtc_cap_v2`
继续分叉。
