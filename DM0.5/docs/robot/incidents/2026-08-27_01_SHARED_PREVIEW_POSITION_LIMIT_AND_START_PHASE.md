---
doc_type: incident
domain: g2_control
status: active
created: 2026-08-27
updated: 2026-08-27
models: [new_lora]
hardware: [g2]
canonical_for: [shared_preview_position_limit_failure]
supersedes: []
superseded_by: []
related: [../experiments/SHARED_HAND_TRAJECTORY_OFFLINE_ABLATION_20260827.md, ../ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md]
---

# Shared Preview首次Powered位置越界与启动相位事故

## 摘要

2026-08-27首次`shared_preview_v1` Powered Sync实际配置100秒，但第6次请求刚开始即停止。
这不是时长预算或操作者pause：GDK拒绝左手joint[5]的`-0.000186366rad`，其合法范围为
`[0, 1.48353]`，运行时按fail-closed产生`servo_failure`并停止。前5个完整chunk主观明显
更顺，规划器零不收敛；但该观感的一部分来自错误使用速度/加速度/jerk硬限幅，不能据此
宣布平滑问题已从根因解决。

## 影响与直接证据

- 日志：`g2_powered_split_sync_100s_c2_spline_hand_shared_preview_v1_20260827_151913.jsonl`；
- `runtime_start.max_requests=150`，实际只有6个action chunk，排除写死时长；
- GDK原生错误：`end_effector joint[5] position out of range [0, 1.48353]: -0.000186366`；
- request 5双臂成功6帧，手部0帧，随后`servo_failure`；
- 前5段planner均零不收敛，故障不是QP超时；
- 首段前两个已确认手部命令间隔49.60ms，实际jerk峰值216.97rad/s³，后续间隔约100ms。

## 根因

第一，原始模型chunk先经过物理位置clip，但preview QP在clip之后重新生成命令。QP只含
速度、加速度和jerk盒约束，没有20D关节位置约束，因此数值优化可产生极小越界；GDK官方
合同要求position必须处于限位，即使只越界0.00019rad也会拒绝。

第二，首次手部发送有80ms startup delay，但`next_send`仍保留线程创建时的旧deadline。
第一次发送后，第二个deadline只剩约50ms，缩短了首个10Hz周期。

第三，也是更根本的设计错误：离线候选把1.75rad/s、7.5rad/s²和80rad/s³作为硬盒约束。
这样得到的“顺”可能来自截断模型快速意图，而不是利用未来waypoint把运动在时间上合理重排。
项目原有Split Sync对原始chunk动力学异常采用warning-only；新planner不应暗中恢复一套更紧
的硬导数cap。

## 修复

1. QP只保留GDK必须满足的20D物理位置硬约束；求解后仍做位置安全投影。
2. 速度、加速度和jerk阈值改为telemetry告警，`enforce_derivative_limits=false`，不截断命令。
3. 平滑只来自tracking、acceleration和jerk软代价，以及跨chunk连续q/v/a和6步preview。
4. 10Hz deadline以第一次实际成功发送时刻为基准，保证其后仍有完整100ms周期。
5. `runtime_start`显式记录位置约束开启、导数约束关闭、horizon和三个软代价参数。

## 软约束离线复验

在原6份169-chunk/321-event数据上重新扫描软jerk penalty。旧0.1权重因QP过冲出现
0.326rad最大步幅；提高到5.0形成膝点，不依赖导数clip：

| 指标 | V1.1 | 软约束Preview |
|---|---:|---:|
| 关键半程遗漏 | 0/321 | 0/321 |
| jerk P95 | 45.64 | 14.60rad/s³ |
| tracking error P95 | 0.1229 | 0.0932rad |
| 最大步幅 | 0.1750 | 0.1700rad |
| 末三步最大jerk | 91.1% | 69.2% |
| 求解不收敛 | 不适用 | 0/1519 |

0.170rad是软目标优化后的自然结果，不是运行时cap。penalty继续升到10会遗漏6个关键事件，
因此不采用“越平越好”的方向。

## 状态与下一门

代码和离线复验完成，无GDK完整回归131 passed、1 skipped；修复版4个运行文件已同步G2。
随后Powered复验完成43个chunk、286个QP step：零位置越界、零QP不收敛、零Servo failure，
首段前三个手命令间隔97.89/101.79ms，位置硬约束与导数warning-only均由runtime telemetry
确认。因此本事故的两个机制缺陷已在真机关闭。任务动作是否被软代价压慢仍属于算法验收门，
不再属于本事故的安全/调度修复范围。
