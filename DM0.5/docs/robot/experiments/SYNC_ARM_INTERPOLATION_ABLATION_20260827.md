---
doc_type: experiment
domain: g2_control
status: active
created: 2026-08-27
updated: 2026-08-27
models: [new_lora]
hardware: [g2]
canonical_for: [sync_arm_interpolation_ablation]
supersedes: []
superseded_by: []
related: [G2_ACTION_SMOOTHING_PLAN_SOURCE.md, RTC_SMOOTHING_ABLATION_20260821.md, ../RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md]
---

# Sync双臂30→100Hz插值离线消融

## 问题与唯一变量

2026-08-26最新两轮10Hz Powered Sync中，双臂实际发送命令的jerk P95约3109rad/s³、
最坏5525rad/s³；最坏点位于chunk内部而非120ms边界窗口。正式Sync仍使用linear
30→100Hz插值。假设是：weight10已经平滑30Hz waypoint，但linear在每个waypoint产生
速度不连续；C2 cubic spline应降低100Hz jerk而不显著改变模型轨迹。

本实验冻结同一批已经通过生产weight10与物理clip的`action_chunk.actions`、每次执行
20个30Hz interval、100Hz理想采样、120ms cubic smoothstep边界blend和日志中的上一条
实际双臂命令。唯一变量是`linear / C1 cubic Hermite / C2 cubic spline / PCHIP /
quintic`插值。手部、模型、网络、调度和waypoint均不参与变化。

评估器为`g2_robot/g2_sort/validation/evaluate_sync_arm_interpolation.py`。每份运行的首个
chunk没有进程内上一条成功arm anchor，无法严格重建边界blend，因此最新30个chunk中
使用28个，此前56个中使用54个。结果产物位于：

- `g2_robot/logs/analysis/sync_arm_interpolation_latest30_20260827.json`；
- `g2_robot/logs/analysis/sync_arm_interpolation_previous56_20260827.json`。

## 最新两轮：28个可重建chunk

| 插值 | blend后内部jerk P95 | chunk jerk max P95 | 加速度max P95 | step max P95 | 相对linear偏差max P95 | 最大过冲 | 限位违反 |
|---|---:|---:|---:|---:|---:|---:|---:|
| linear | 997.25 | 4076.48 | 40.76 | 0.01133 | 0 | 0 | 0 |
| C1 Hermite | 667.17 | 2187.94 | 28.43 | 0.01142 | 0.00086 | 0.000620 | 0 |
| **C2 spline** | **594.05** | **1861.39** | **28.25** | 0.01142 | 0.00145 | 0.001056 | 0 |
| PCHIP | 796.04 | 3282.05 | 34.02 | 0.01143 | 0.00140 | 0 | 0 |
| quintic | 759.38 | 3281.91 | 28.52 | 0.01147 | 0.00102 | 0.000718 | 0 |

单位依次为rad/s³、rad/s³、rad/s²和rad。C2相对linear把内部jerk P95降低40.4%，
chunk jerk max P95降低54.3%，加速度max P95降低30.7%。相对C1 Hermite，C2仍把两个
jerk指标分别降低11.0%和14.9%。最大偏差约0.00145rad，最大过冲约0.00106rad，无
物理限位违反。

## 此前两轮：54个可重建chunk复验

| 插值 | blend后内部jerk P95 | chunk jerk max P95 | 加速度max P95 | step max P95 | 相对linear偏差max P95 | 最大过冲 | 限位违反 |
|---|---:|---:|---:|---:|---:|---:|---:|
| linear | 938.99 | 4694.30 | 48.27 | 0.01132 | 0 | 0 | 0 |
| C1 Hermite | 639.59 | 2109.45 | 30.80 | 0.01138 | 0.00096 | 0.001038 | 0 |
| **C2 spline** | **566.56** | **1778.00** | 29.58 | 0.01136 | 0.00143 | 0.001735 | 0 |
| PCHIP | 762.90 | 3240.59 | 34.18 | 0.01139 | 0.00138 | 0 | 0 |
| quintic | 737.64 | 3164.17 | **27.86** | 0.01141 | 0.00117 | 0.001089 | 0 |

C2相对linear把内部jerk P95降低39.7%、chunk jerk max P95降低62.1%，复现最新数据结论。
Quintic在该组的加速度峰值最低，但两个jerk指标均明显弱于C2；PCHIP无过冲但仅C1，
jerk也更高。因此若主要目标是解决已观察到的双臂抖动，C2仍是五种方法中的首选。

## 证据边界与决定

本实验能证明同一模型waypoint在理想100Hz时间基下，C2的命令轨迹jerk最优且偏差很小；
不能证明G2机械反馈、Servo发送时间抖动或任务成功率已经改善，也不能解决双手V1的内部
追赶。评估使用日志中的实际上一命令重建边界，但没有编码器反馈。

决定：C2 cubic spline通过离线门，不再为Hermite、PCHIP或quintic各自增加真机轮次。
正式Sync与实验Async脚本均显式选择C2；Sync其余参数冻结，Async仍保留60ms blend、
jerk weight 0和双手5Hz。两个入口在文件名与运行配置中写入`c2_spline` tag。下一步先做
10秒Sync单变量真机验证，通过现场双臂抖动、声音、actual-time jerk、限位和任务意图
门禁后再决定RTC迁移；本轮不修改RTC。
