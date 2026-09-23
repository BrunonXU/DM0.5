---
doc_type: reference
domain: g2_control
status: active
created: null # 原文未记录创建日期；不以文件mtime推断
updated: 2026-09-14
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../CURRENT_STATUS.md]
---

# G2 丝滑遥操：根因蒸馏、`move_ee_pos` 接口陷阱与后续路线

> 更新时间：2026-09-08（DM0.5单34D完整Sync实测后，更新当前工作基线）  
> 用途：供控制、机器人、模型和数据采集团队共享。本文独立成篇，不依赖历史聊天。  
> 范围：G2 双臂、O10 双手、F710 human-in-the-loop/DAgger，以及DM0.5 LoRA V2同步推理。

## 0. 最高优先级结论：DM0.5当前采用单34D，双17D保留回退（2026-09-08）

**当前工作基线：单次34D `joint_servo_control(normal)`、目标100Hz；双17D已验证可用，
保留为回退，不再要求重复测试。** 操作者已完成34D左右食指及完整模型推理测试，反馈“很丝滑”；
最新较长轮覆盖右手闭合，且没有发送/调度超时事件。此为当前工程选择，不是“唯一正确解”或
所有场景下理论最优的证明。入口为22，回退入口为19；现场命令只在
[操作指南](../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md)维护。

### 0.1 必须区分的两个问题

1. **连续轨迹误用点目标接口：** DM0.5连续手部waypoint应通过Servo执行，不能把连续中间点
   反复提交给`move_ee_pos`。这解释旧手部顿挫；不否定离散开合使用点目标接口。
2. **Servo发送名错误：** 右食指反馈原名`idx77_hand_r_index_pip_joint `带尾空格，运动控制接收端
   按该精确字符串查找失败。此前保留原字符串发送的双17D实现因此出现右手主要拇指+中指闭合。
   正确发送名为`idx77_hand_r_index_pip_joint`，无尾空格。反馈原名保留作证据，发送名必须校验
   已知canonical集合，并保证名称/位置配对；不能再把“原样发送反馈名”当作读写合同。

09-07两轮接收端共8094次拒绝该右食指名称；静态二进制分析支持“名称跳过、值未同步跳过”
导致后续手轴错位的机制，但没有原始C++源码级证明。修正后四指单轴及完整双17D/单34D反馈
恢复，接收端未知名错误消失。不能把此前两指抓归因于模型主动选择、C2或17D维度本身。
SDK参数层所有O10 `Param ... not found`与下游接收端未知名称错误是两类日志，不能混为一谈。
证据见[SDK右手核查](../../logs/analysis/20260907_sync_servo_audit/SDK_RIGHT_HAND_FINDINGS.md)。

### 0.2 单34D与双17D的真实区别及冻结配置

两者都调用同一GDK接口`Robot.joint_servo_control(..., enable_low_latency=False)`：

- 单34D：每周期一次完整双臂14D+双手20D请求，当前代码按canonical模型顺序配对；
- 双17D：同一Control线程依次发左臂7+左手10，再发右臂7+右手10；不是左右两个异步线程；
- 单34D减少一次API调用，把双侧目标放入同一消息，但不证明34个电机原子同时执行。

当前模型仍为NewLora75K，50×34 absolute、30Hz语义，Sync执行前20个间隔后重新观测。
Control唯一拥有Robot；Policy唯一拥有Camera/图像/网络，以34D状态ring交换数据。
维持normal、目标100Hz、双臂C2 spline/jerk weight10、stationary_minjerk_v1阈值0.0035rad、
边界融合0.12s；手部线性插值、额外平滑off，无连续move_ee_pos、preview或changed-only。
**这些平滑参数是冻结对照配置，不是逐项已证明最优；布局验证不同时调整C2/OTG。**

### 0.3 决定性较长轮：20260908_145909

| 指标 | 单34D本轮结果 |
|---|---:|
| 实际日志跨度 | 82.346秒（文件名120s不代表实际跑满） |
| 模型chunk | 88 |
| segment控制tick / 等待窗事件 | 5775 / 2204 |
| segment内控制间隔P50/P95/max | 10.090 / 11.684 / 14.975ms |
| segment内间隔>15ms | 0 |
| SDK发送平均/P95/max | 0.713 / 1.675 / 8.999ms |
| 发送超时、tick迟到超时、servo_failure事件 | 均0 |
| 已下载对应运动控制窗口未知关节名错误 | 0 |
| 右食/中/无名/小指PIP反馈峰值 | 0.982 / 0.987 / 0.987 / 0.990rad |

segment间隔不包括跨段/等待窗，不能写成全程所有控制间隔均<15ms。全部记录的segment命令
完整且finite。前一轮34D已覆盖左手闭合，本轮补齐完整推理中右手闭合的证据；不由峰值推导
精确跟踪误差、双手第二次抓取成功率或全任务验收。

### 0.4 计时告警继续是操作者明确选择，不能抹掉阻塞历史

09-08 `143923`轮在一次34D发送耗时239.842ms>200ms后停止；这是Servo发送路径计时，
不是模型网络往返。末尾software emergency stop是退出禁发记录，不是首个故障原因。
同一时段有collision imminent提示，但它不能单独证明长耗时根因；较长轮也有该提示而未超时。

操作者明确要求仅34D超时记录后继续。入口22启用`--servo-timing-warning-only`，仅允许
powered-sync/unified-34/normal。发送/tick迟到超过200ms分别写`servo_send_overrun`和
`servo_tick_overrun`，继续运行；调度丢弃过期槽，不立即补发积压命令。SDK返回错误、人工停止
及其他控制故障仍有效；双17D的原超时策略不变。34D的0Hz/0次汇总遗漏已修正。

**145909轮没有复现长耗时，不能说它靠放开看门狗扛过阻塞，也不能说罕见阻塞已被修复。**

### 0.5 推理时延与反馈证据边界

- 本轮请求往返平均252.514ms；服务端模型执行及结果回CPU平均147.840ms。
- 09-07修正后三轮双17D共135次请求，往返加权平均234.238ms，模型及回CPU平均144.588ms。
- 上述指标不等于纯网络传输时间；场景/输入/运行时间不同，不能归因于17D/34D布局，也不是
  严格冻结轨迹A/B。当前单34D SDK P95较低，只支持减少调用开销的工程判断。
- 本轮独立诊断反馈288条ok，在14.586秒因单次读取5.700ms>5ms关闭，后约67.76秒缺少此流。
  主telemetry与模型观测继续；零队列drop不等于全程有measured数据，不能声称全程跟踪更优。
- 09-07静态34D左手反馈曾大幅往返；09-08静态探针多次因全零反馈发送0次退出。这些记录保留，
  不能被后续完整推理通过改写为静态hold已验收；全零值/缓存刷新机制仍未解决。

后续使用单34D工作基线，保留双17D回退；不为“证明唯一解”继续重复同类布局测试。
C2/OTG另作单变量评估，不把加入更多平滑当作必需步骤。独立F710执行不变。09-08后续按操作者要求，DAgger入口40的POLICY、等待/恢复策略
阶段也迁移至单34D normal/100Hz及200ms发送仅告警；同owner人工HIL的臂Servo/离散手部
语义不变。此DAgger版本通过本地回归，实际新版本HIL交接待验收，training_ready=false。
RTC仅在入口代码标记`TODO(RTC_UNIFIED_34D_MIGRATION)`，暂未迁移运行行为。

详细证据：

- [145909较长轮审计](../../logs/analysis/20260908_unified34/REPORT_145909.md)
- [34D完整推理原始遥测](../../logs/analysis/20260908_unified34/powered)
- [34D对应反馈](../../logs/analysis/20260908_unified34/feedback)
- [较长轮运动控制窗口](../../logs/analysis/20260908_unified34/motion_145909.log)
- [09-07名称修正后双17D审计](../../logs/analysis/20260907_sync_servo_audit/after_fix/REPORT.md)
- [单34D静态/单指测试及未决异常](../../logs/analysis/20260907_sync_servo_audit/layout_34d/REPORT.md)

> 以下保留原理推导与09-03/09-04实验史。涉及“当前入口19”“34D尚不可用”或“原样发送尾空格”
> 的历史说法已由本节覆盖，不得再次当作当前实施要求；历史数值和反例不删除。

## 1. 一句话结论

交接入口：[当前状态](../CURRENT_STATUS.md)、[三项TODO](G2_DM05_CONTROL_TODO.md)、[新对话prompt](G2_DM05_NEW_CHAT_PROMPT.md)。当前入口22（单34D），19（双17D）保留回退；具体命令见[操作指南](../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md)。本次仅更新文档，采用已完成真机日志与前述审计结果。

G2硬件、GDK normal Servo、Python 100Hz发送以及移动双臂与动态O10并发，本身都能够非常丝滑。
近一个月问题的架构级根因是：**把DM0.5每帧连续变化的20D手部轨迹交给点目标接口
`move_ee_pos`反复刷新，而不是交给连续流接口`joint_servo_control`。** 在手指尚未完成当前
点到点运动时继续提交新中间点，会触发强证据支持的目标替换/重规划/排队/状态重置效应；O10
自身分段，并通过共享机器人控制链使运动中的双臂跟踪观感也变卡。

这并不等于“所有10Hz都坏”或“5Hz是神奇常数”。真正的变量是：

```text
点目标是否在执行过程中被高频、重复地替换
```

正确方案必须按上游语义区分：

```text
F710离散开/合意图：
  双臂100Hz joint_servo_control
  双手仅在意图/endpoint变化时调用move_ee_pos

DM0.5连续20D手轨迹：
  30Hz模型waypoint线性插值到100Hz
  当前：双臂14D+双手20D canonical名称/值 → 一次34D joint_servo_control
  回退：左17D、右17D在同一Control线程依次发送
  不调用move_ee_pos；无阈值、deadband、changed-only或手部重塑
```

因此不能简化成“`move_ee_pos`永远不能用于手”。准确结论是：它适合离散目标，不适合把连续
模型轨迹当作waypoint stream反复刷新。连续模型手轨迹现在使用Servo。

## 2. 最终验证过的两种可用架构

### 2.1 DM0.5 LoRA V2连续双手：09-04历史架构与证据（当前以第0节为准）

```text
模型保持原样输出50×34 absolute joint positions（30Hz语义）
  └─ 双臂前14D：原有C2/边界处理后100Hz执行
  └─ 双手后20D：已选执行前缀的相邻waypoint线性插值到100Hz，不按阈值丢弃手部动作
        ├─ 左臂7D + get_end_state()原样返回的左手10D名称/目标 → 17D Servo
        └─ 右臂7D + get_end_state()原样返回的右手10D名称/目标 → 17D Servo

每个100Hz tick依次调用两次：
  Robot.joint_servo_control(JointServoControlReq, enable_low_latency=False)

禁止回退为：
  连续20D模型轨迹 → 周期性move_ee_pos点目标刷新
```

决定性在线A/B和最终真机结果：

| 项目 | 旧DM0.5 Split | 新双侧动态17D Servo |
|---|---:|---:|
| 手部接口 | `move_ee_pos` | `joint_servo_control` |
| 手部下发 | 标称5Hz，含等待窗成功约4.65Hz | 每个控制tick，标称100Hz |
| 手部动作处理 | raw5 changed-only | 原始20D线性插值，无阈值/去重 |
| 真机观感 | 双臂随动态手部明显卡顿 | 双臂双手“终于丝滑” |
| arm dispatch P50/P95/P99/max | 10.072/10.965/12.936/14.112ms | 10.090/11.427/13.135/14.331ms |
| arm dispatch >15ms | 0 | 0 |
| 每tick GDK总调用P95/max | 0.738/3.948ms（14D臂请求） | 2.029/6.883ms（左右两个17D） |

新日志实际运行13.36秒、15个chunk，包含952个segment控制tick，另有327个等待窗手部tick。
旧日志123次segment手部发送加37次等待窗发送，共160/34.38秒≈4.65Hz；不能遗漏等待窗后写成3.6Hz。
新方案两次调用的实测总耗时最大值小于10ms；表中gap仅统计segment，不代表完整运行所有窗口。
手部从低频点目标改为连续Servo后，操作者确认顿挫消失，但新旧segment gap都无>15ms，不能据此
计算“丝滑改善百分比”。文件名300s是请求配置，不是300秒长稳验收；完整新旧运行还改变了Servo
通道等参数，不是严格单变量配对。旧列调用时间只计14D臂请求，并非旧控制链全部SDK开销。

模型权重与50×34输出合同未改；Sync只执行前20点（约0.667秒），手部仍有线性插值、120ms边界
融合与物理位置裁剪，双臂仍有weight10/C2/3.5mrad minimum-jerk。因此“无手部重塑”指关闭preview、
deadband和changed-only，不等于完全无后处理；模型任务成功率是否保持需要后续实测。

证据：

- `g2_robot/logs/telemetry/powered/g2_powered_split_sync_300s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_dual_side_17d_servo100_20260904_144901.jsonl`
- `g2_robot/logs/telemetry/feedback/g2_powered_split_sync_300s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_dual_side_17d_servo100_20260904_144901.jsonl`
- `g2_robot/logs/telemetry/powered/g2_powered_split_sync_10s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_arms_only_20260904_141927.jsonl`
- `g2_robot/logs/diagnostics/single_side_17d_visible_left_20260904_143434.json`

这里不把command-feedback误差写成“新方案数值更小”：新方案使用normal通道，旧Sync/arms-only
A/B使用low-latency，反馈延迟和控制通道不同，不能做同口径质量排名。决定性证据是配对隔离、
接口调用完整性、无调度失速和同轮现场运动观感。

### 2.2 F710/离散意图与数据replay：已验证的endpoint架构（2026-09-03）

```text
高质量数采轨迹
  ├─ 14D双臂 measured states（原始30Hz）
  │      └─ 线性时间插值到100Hz
  │             └─ joint_servo_control(normal)，arms-only请求
  │
  └─ 20D双手 recorded actions（同一原始时钟）
         └─ 5Hz采样
                └─ 数值实际变化时才调用一次 move_ee_pos
                       └─ 静止段完全不刷新
```

2026-09-03 15:14:46的完整真机结果：

| 项目 | 结果 |
|---|---:|
| 原始数据 | 948帧，30Hz |
| 原始动作时长 | 31.57s |
| 末端保持 | 1.00s |
| 起点准备 | 2.00s |
| Powered总时长 | 34.57s |
| 双臂发送 | 3458次 |
| 双臂有效发送率 | 98.739Hz |
| O10调用 | 48次：1次初始目标 + 47个变化目标 |
| arm发送间隔P50/P95/max | 10.009/11.115/14.460ms |
| GDK调用耗时P95/max | 0.906/3.290ms |
| feedback | 702条有效样本，零drop |
| 主telemetry | 零drop，`status=completed` |
| 操作者评价 | 双臂和双手“非常丝滑、非常可以接受” |

对应证据：

- `DM0.5/logs/telemetry/powered/g2_source_speed_o10_raw5_20260903_151446.jsonl`
- `DM0.5/logs/telemetry/feedback/g2_source_speed_o10_raw5_20260903_151446.jsonl`
- `G2Teleop/logs/g2_source_speed_o10_raw5_20260903_151446.console.log`
- `G2Teleop/scripts/32_g2_source_speed_qdirect_o10_raw5.sh`

15:13:37还有一轮约13.15秒的人工Ctrl-C试跑，只覆盖1315次arm和18次O10调用，不应与上述完整
自然结束结果混为一轮。

## 3. `Q-direct`到底是什么

`Q-direct`不是一种OTG，也不是GDK接口名称。

- `q`表示关节位置向量；
- `direct`表示把已经存在的高质量14D关节轨迹直接按时间插值后送给Servo；
- 它绕过F710输入、EEF目标生成、Pink IK和Ruckig；
- 它仍通过真实GDK `joint_servo_control(normal)`驱动真机。

它是一个因果隔离基准：如果Q-direct丝滑，就证明从关节command向下的发送链、GDK Servo和
机器人机械系统有能力丝滑执行这类轨迹；如果F710仍抖，问题应优先在Q-direct所绕过的上游层中
寻找，例如手柄整形、EEF目标、Pink reference、OTG状态或真实dispatch时基。

Q-direct的结论不能被误读成“F710不再需要IK”。F710输入是Cartesian末端速度，仍然必须通过
Pink或等价IK生成关节reference。Q-direct只是告诉我们：不要继续把所有问题笼统归因到Servo或
硬件。

## 4. 为什么`move_ee_pos`单次很丝滑，连续调用反而会卡

### 4.1 第一性原理：接口语义不同

连续Servo和点目标接口解决的是两类不同问题。

```text
joint_servo_control
  输入语义：当前控制周期应该到达的关节位置
  调用语义：100Hz连续流
  外部控制器负责整条轨迹的时序连续性

move_ee_pos
  输入语义：末端要去的一个position目标
  调用语义：提交点目标
  O10内部控制器负责从当前状态运动到该目标
```

GDK 2.6.3公开的`move_ee_pos`请求只有各关节`position`，没有本项目已经验证过的目标速度、持续
时间或轨迹时间戳字段。调用一次最终close目标时，O10内部可以连续完成自己的点到点动作；但外部
每100ms提交一个新中间点，相当于在当前动作尚未完成时不断修改目的地。

### 4.2 可能发生的内部过程

固件实现没有公开，因此以下是由A/B证据支持的机制解释，而不是对固件源码的断言：

```text
t=0.0  提交目标1 → O10启动一次内部点到点运动
t=0.1  目标1尚未结束，提交目标2 → 当前目标被替换/重新规划/重新排队
t=0.2  再提交目标3 → 再次影响正在执行的动作
...
```

任何“替换当前目标、重新规划、队列化、控制器状态重新初始化”的实现，都可能在目标边界产生
减速、等待或相位重置。外部看到的就是若干运动chunk及chunk之间的停顿。客户端目标数值连续，
并不保证机器人内部执行轨迹连续。

### 4.3 为什么不能简单归结为“10Hz太低”

最初很容易误判为“手只以10Hz控制，所以看起来不连续”。实验反而证明问题不是单纯采样率低：

- 单次最终目标：调用频率更低，但物理动作非常丝滑；
- 10Hz连续中间目标：数值更密、更连续，物理动作反而顿挫；
- 静态目标以10Hz重复：静止双臂没有抖动；
- 动态目标只在变化时约5Hz提交：正常速度完整回放可接受。

所以关键不是“频率越高越丝滑”，而是接口是否具有流式轨迹语义。对点目标接口，过密刷新可能
比稀疏、明确的目标更差。

### 4.4 为什么5Hz changed-only现在可用

当前raw 5Hz方案有两个同时发生的变化：

1. 动态目标间隔从约100ms增至约200ms，让O10内部执行有更多推进时间；
2. 静止样本不再周期重发，完全消除没有信息增量的重新提交。

这两项共同降低了正在运动时的目标替换次数。它仍是分段渐进目标，不是严格连续、恒速的20D
轨迹，但真机正常速度完整回放已经证明其物理效果“非常丝滑、非常可以接受”。因此它是当前工程
基线，而不是关于所有任务、所有速度的理论最优值。

## 5. 最关键的因果实验链

| 实验 | 隔离变量 | 现场/日志结果 | 得到的结论 |
|---|---|---|---|
| Q-direct双臂，arms-only | 排除IK/OTG/网络/O10 | 操作者确认非常丝滑 | GDK Servo和机械臂具备丝滑能力 |
| 移动双臂 + v1_1 O10 10Hz | 加入动态重复O10 | 明显抖动，双臂对齐反馈恶化 | O10路径是触发因素，但尚不等于10Hz本身 |
| 静态双臂100Hz + 静态O10 10Hz | 只保留线程/接口并发 | 双臂稳定 | 并发和10Hz静态调用本身不足以制造抖动 |
| 静态双臂 + 动态O10 10Hz | 去掉移动双臂 | 双臂稳定，双手分段顿挫 | 动态`move_ee_pos`重提直接影响手部运动 |
| 同起点/终点A/B | A单次终点；B为12个10Hz中间点 | A丝滑；B顿挫 | 强因果证据：反复重提点目标是主要触发机制 |
| event endpoint + 移动Q-direct双臂 | 每个手动作窗只提交终点 | 已覆盖前缀双臂/双手都丝滑 | 移动双臂和O10不天然冲突 |
| raw 5Hz + 91.22s限速时间轴 | 保留渐进手动作，降低重提 | 完整轮分段丝滑、可接受 | 5Hz changed-only可保留渐进语义 |
| raw 5Hz + 原始31.57s时间轴 | 恢复真实数采速度 | 完整轮非常丝滑 | 当前可用架构获得最终正向验证 |

同起终点A/B是最重要的一轮：起点、终点、双臂姿态、Robot/GDK实例和场景相同，主要变量只有
“一次提交最终目标”与“运动中连续提交12个中间目标”。这比单纯比较两次不同轨迹更接近因果证据。

## 6. 曾经的90多秒为什么不是正常速度

原始数据有948帧、30Hz，因此最后一帧时间为：

```text
(948 - 1) / 30 = 31.57s
```

此前91.22秒产物是为限制双臂速度而生成的`velocity-limited`时间轴，把原始轨迹约拉长三倍；再加
2秒准备，真机总时长为93.22秒。那一轮证明了拉长时间轴上的raw 5Hz可接受，但不能称为“正常
速度”。正常速度入口32直接读取原始JSON，并在31.57秒源时间上完成双臂插值和双手采样；34.57秒
完整真机结果才是最终正常速度证据。

## 7. GDK接口到底有没有选错

结论：**没有完全选错，但此前混淆了“合法接口”和“适合连续流的接口”。**

GDK 2.6.3官方Robot文档明确支持：

```text
move_ee_pos(group=dual_tool, target_type=o10_t2)
左O10 10D + 右O10 10D = 20 positions
```

因此Split O10调用是官方合法路径，适合开手、闭手、选择某个手型等离散点目标。官方同时明确
`joint_servo_control`以100Hz运行，并说明机械臂和末端可以同时控制；示例将单侧7D手臂名称与
`get_end_state()`动态返回的同侧末端名称拼接后发送。

但官方示例不等于本项目历史上的单请求双臂+双手34D。历史34D真机尝试中：

- 20个O10名称反复出现约22080条`Param ... not found`；
- 曾出现一次约2.53秒同步阻塞；
- 因此生产路径恢复为14D双臂Servo + 20D双手`move_ee_pos`。

所以不能因为官方有一个单侧示例，就直接宣布34D unified Servo可用；也不能因为历史34D失败，
就宣布O10绝对不能Servo。两者不是同一个实验。

2026-09-04现已补齐关键实测：

- 左侧动态名称17D静态hold：100/100请求成功，P95/max `1.005/1.730ms`；
- 左食指PIP 0.20rad连续轨迹：401/401请求成功，P95/max `0.850/4.706ms`，肉眼“非常丝滑”；
- 完整DM0.5双侧推理：每tick左17D+右17D均成功，双臂双手“终于丝滑”；
- `get_end_state()`历史原始数据中右手`idx77_hand_r_index_pip_joint `带一个尾空格，而冻结手写名称
  不带空格。这是历史34D出现`Param not found`的高可信名称差异线索，但不是对全部失败机制的
  唯一证明。

当前双17D不是因为GDK明确禁止一次34D，而是因为官方已核对示例只明确验证单侧7D+10D，且历史
单次34D有严重失败记录。严格仿照官方合同、左右各一次，是先取得可靠连续控制的最小风险路径。

官方依据：

- [GDK 2.6.3 Python Robot API](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/reference/Python/robot.md)
- `g2_robot/docs/GDK_2.6.3_RELEVANT_API.md`
- `DM0.5/docs/robot/incidents/2026-07-31_01_UNIFIED_O10_SERVO_BLOCK.md`

## 8. 手反馈为什么不能用来反推手指是否一卡一卡

本轮实验还发现`get_end_state()`在当前GDK/O10组合中不具备可靠的连续编码器轨迹语义：

- A组闭手已经肉眼完成，反馈直到下一次reset调用后才显示接近close；
- reset后的open也在后续调用发生时才明显刷新；
- 停止调用后，最终close并不总能及时反映。

因此日志中约0.5--0.6秒一次的手状态更新不能直接解释成手指真实每0.5--0.6秒运动一段。物理
顿挫由现场同轮A/B观察确认；feedback在此只用于记录接口返回状态，不能替代视频或肉眼运动评价。

这也是为什么结论必须同时使用：命令日志、调用时序、双臂反馈和现场观察，而不能只看O10返回值。

## 9. 一个容易误读的日志警告

正常速度完整轮中，每个arm tick都打印：

```text
Clipped 1 G2 servo positions to physical limits (max_delta=0.004995)
```

这不是3458次真实手指撞限位。arms-only诊断脚本内部保留了一份启动时34D实测action，其中一个
手反馈值比项目静态限位多约0.005rad；通用`send_action`先裁剪完整34D，但最终Servo请求只发送
前14D双臂，后20D手值没有进入Servo。它没有破坏本轮运动结论，但制造了严重日志噪声，后续应让
arms-only路径只检查和报告实际会发送的14D，避免同事误判。

## 10. 对F710 human-in-the-loop主线的直接改造

现在应停止继续增加相似replay，转回F710。推荐顺序如下。

### 10.1 双手：复制已验证语义，不复制旧10Hz worker

F710手控应成为明确的意图状态机：

```text
RT完整按下并释放
  → 产生一次新的open/close意图
  → 生成该侧目标手型

A状态变化
  → 产生thumb-clearance新意图
  → 更新该侧目标手型

意图持续且需要渐进动作
  → 约5Hz生成新的变化目标

目标未变化
  → 不调用move_ee_pos
```

由于GDK使用`dual_tool`20D调用，更新一侧时应把另一侧最后一次成功命令一并带上；但不能因此让
后台线程在没有新意图时永久重发同一20D目标。

建议telemetry至少记录：

- 原始按钮/扳机边沿；
- 当前活动侧；
- `hand_intent_id`与意图类型；
- 目标20D及与上次已发送目标的差；
- 本tick为何发送或为何跳过；
- `move_ee_pos`调用开始、结束、耗时和返回值；
- 从意图产生到首次O10提交的延迟。

### 10.2 双臂：先验证direct Pink，不先押注Ruckig

F710仍需Pink把Cartesian摇杆输入变成关节reference。下一轮应先使用当前已经实现的
`compute → pre-dispatch pacer → GDK send`时基修正版，保持normal Servo，并先关闭Ruckig：

1. Shadow复查输入、EEF目标、Pink q/dq和实际计划dispatch；
2. 真机小幅单轴平移、回中、反向和旋转；
3. 再加入已经改造的O10开合/拇指意图；
4. 对照Q-direct基准判断剩余不丝滑来自Pink reference还是发送时基。

如果direct Pink已经满足人工遥操手感，就把它作为F710基线。Ruckig只用于进一步改善启停、加速度
和jerk，不应被当成“只要接上就必然丝滑”。此前Powered Ruckig仍抖且发生lag HOLD，已经证明
数学约束有界不等于物理手感自动变好。

### 10.3 DAgger意义

DAgger前置人工控制最重要的是：操作者的连续意图必须映射成可预测、低延迟且可复现的实际command。
当前分工满足这个方向：

- 双臂是连续100Hz控制；
- 双手是低带宽、目标型执行；
- 每次抓握都有明确意图ID，而不是依赖周期采样猜测“是不是新动作”；
- 可同时记录model proposal、human intent、executed command和measured state。

这能避免把控制器自己制造的O10顿挫采回数据集，再让模型学习错误的执行伪影。

## 11. 17D Servo验证结果与一次34D优化路线（09-04历史，已由第0节更新）

### 11.1 单侧17D已经从研究支线升级为实测成立

2026-09-04已完成原计划中的左侧静态、左侧小幅动态、左侧肉眼可见动态，以及完整双侧DM0.5
在线推理。结论不再是“是否接受O10 Servo未知”，而是：当前G2/GDK实例接受动态名称单侧17D，
且左右各17D连续执行已经解决DM0.5双手推理引起的顿挫。

当前正式候选回答的是：

```text
当前G2 + GDK 2.6.3接受
单侧7D arm + 该侧10D O10 = 17D
的100Hz normal joint_servo_control；DM0.5以左右两个17D请求覆盖34D。
```

### 11.2 为什么当前每tick是两次SDK调用

GDK官方示例明确可核对的是单侧17D合同，历史单次34D同时改变了左右两侧、34个名称、请求规模和
双末端，并发生约22080条`Param ... not found`与一次2.53秒阻塞。为了不重复该事故，当前每个
100Hz tick严格发送两个官方形状的请求：

```text
call 1: left arm 7D + exact dynamic left O10 10D
call 2: right arm 7D + exact dynamic right O10 10D
```

代价是每tick多一次Python→GDK调用，并产生约1--2ms量级的左右提交偏斜。当前在线日志中两个
请求合计耗时P95/max为2.029/6.883ms，100Hz dispatch没有超过15ms，所以它不是当前瓶颈。

### 11.3 一次动态34D值得验证，但尚不能直接替换

现在发现历史右手动态名称存在尾空格差异，因此值得构造新的单次34D候选：两侧手名全部来自同次
`get_end_state()`原始字符串，不再使用冻结手写O10名称。这可能减少一次SDK调用、降低总调用耗时
并消除左右提交偏斜。

但以下仍未知：

- GDK是否允许左右两侧同时出现在一个`JointServoControlReq`；
- 历史失败究竟只由名称差异引起，还是还包含双侧/请求规模限制；
- 单次动态34D是否会重新触发长阻塞。

因此优化顺序必须是：

1. 保持当前已丝滑的双17D为可立即回退基线；
2. 用两侧动态原始名称构造一次34D，但只做当前位置静态hold；
3. 严格记录调用耗时、返回值、native错误和是否阻塞；
4. 静态通过后做单指0.20rad往返；
5. 再做同一冻结模型输出的双17D/单34D配对A/B；
6. 只有调用、调度、现场观感全部不差于双17D，才升级正式入口。

全过程记录每次GDK调用耗时、返回值、实际joint names/order、Servo状态和同owner feedback。若再次
出现`Param ... not found`或同步阻塞，应保留完整原始证据并停止扩大范围，不通过切到low-latency
掩盖接口合同问题。

### 11.4 当前生产决策

DM0.5连续手轨迹使用双侧17D Servo；F710离散手意图继续使用event-driven `move_ee_pos`。一次动态
34D只作为减少调用和侧间偏斜的性能优化，不是解决丝滑问题的前置条件，也不得在未验证前覆盖
当前双17D入口。

## 12. 已证实、强推断与仍未知（09-04历史分类，最新边界见第0节）

### 已证实

- Q-direct双臂通过normal Servo可以非常丝滑；
- 单次`move_ee_pos`终点可以非常丝滑；
- 相同起终点下，12个10Hz中间目标会恢复多段顿挫；
- 静态100Hz arm Servo与静态10Hz O10并发不会自动制造双臂抖动；
- 原始正常速度下，移动双臂 + raw 5Hz changed-only O10完整运行非常丝滑；
- 主日志和feedback在最终完整轮中均零drop。
- DM0.5相同Sync配置仅禁用手部时，双臂现场“非常丝滑”，1000次Servo且手部命令为0；
- 左侧动态名称17D static Servo与0.20rad可见连续手指轨迹均真实执行成功；
- DM0.5完整双臂双手通过左右两个动态17D、100Hz normal Servo现场“终于丝滑”；
- 新双17D的952个segment控制tick，dispatch gap P95/max为11.427/14.331ms，零次超过15ms；等待窗另计；
- 新执行层不对模型20D手轨迹使用阈值、changed-only或`move_ee_pos`。

### 强推断

- 旧顿挫的架构级根因是点目标接口被当成连续流反复重提；
- O10内部存在目标替换、重规划、队列或状态重置中的至少一种效应；
- 旧组合中的双臂反馈恶化由动态O10重提触发，而不是移动双臂与O10天然冲突。
- 历史统一34D失败至少部分可能由手写名称与GDK动态名称不完全一致导致，右手尾空格是明确线索。

### 仍未知

- O10固件收到新`move_ee_pos`时究竟替换、排队还是重建哪部分轨迹；
- `get_end_state()`的缓存与刷新机制；
- 用两侧动态原始名称构造的单次34D Servo是否可用；
- 历史34D失败除名称差异外，是否还包含双侧组合或请求规模限制；
- F710剩余抖动中Pink reference、真实dispatch时基和Ruckig各自的最终占比；
- 5Hz是否适用于所有未来手型和负载。当前结论仅是它已覆盖本次完整高质量轨迹且现场可接受。

## 13. 决策摘要

> 本节13.1--13.3主要记录F710/DAgger时间线。对DM0.5连续手部推理，2026-09-04最新决定是
> `dual-side-17d` 100Hz Servo（09-08已更新为第0节单34D工作基线）；不得从下述F710 event-driven方案反推模型手部也应走5Hz
> `move_ee_pos`。

截至2026-09-03，本节F710 O10改造已完成、同步G2但尚未Powered：正式入口不再创建10Hz/v1_1
后台hand worker，而由同一个控制owner在双臂100Hz发送后显式执行5Hz changed-only
`move_ee_pos`。新增hand intent ID、调度原因、是否实际发送和调用耗时telemetry；首次RT目标前，
仅选择侧并按RB不会改变当前手型。本地G2Teleop无loopback回归与机器人客户端相关回归均通过，
G2端Python/Shell、19项纯逻辑测试和files-only preflight已经通过且未初始化GDK；下一门是无
Ruckig的direct Pink空载小幅F710真机。

```text
现在：
  停止扩展相似replay
  → 把5Hz changed-only / 意图驱动O10移植到F710
  → Shadow
  → direct Pink真机
  → 再决定是否需要Ruckig

并行但不阻塞主线：
  单侧17D normal Servo静态探针
  → 单个小手指动作
  → 连续轨迹
  → 分别验证左右侧
  → 不直接跳到34D
```

### 13.1 2026-09-03 F710真机闭环与下一版交互

上述F710 changed-only版本已经完成direct Pink Powered验收，操作者评价“非常丝滑”。64.79秒内
6410个dispatch样本、6016个enabled样本，无runtime fault、watchdog、drop或sequence gap；
dispatch间隔P50/P95/P99/max为10.000/10.513/12.771/14.921ms。51次O10调用只对应变化目标，
稳定手型不再周期重提。主线因此已经从“待Powered”推进为“已获得可用丝滑基线”。

下一版交互已实现并同步G2，但尚待Powered：

- 严格先按十字键左/右选侧，松开十字键，再按RB；RB期间或与RB同帧的选侧直接拒绝并消费；
- A单击时在低速4cm/s、10deg/s与高速8cm/s、20deg/s之间切换，初始/重连默认低速；
  松开A不切回，原加速度斜坡、0.35rad/s关节限速、奇异位形降速和20cm包络不变；
- RT成为独立手控：选侧后无需RB即可切换开/合目标；双臂保持HOLD，O10仍只按5Hz
  changed-only推进，网络watchdog仍会冻结手部动作；
- Y在RB松开时请求与现有复位一致的双臂+双手全局Home。17:45首次实现成功识别Y，但关闭F710
  owner后新Home Controller读到`mode=5,error_code=2`而零命令退出，证明跨进程交接不可靠。
  修正版在当前唯一normal Servo owner内执行“双臂5秒回H5 Home → 双手最多8个5Hz步进回批准
  Home”，不重启GDK；完成后清除旧RT目标、恢复低速并要求重新选侧。

本地全量回归为98 passed、6 skipped；同owner Y全局Home尚待Powered复测。

### 13.2 最新Powered验收和偶发停住

同owner Y全局Home已在`g2_powered_f710_20260903_175718.jsonl`中连续成功3次，正式通过真机验收。
最新长测`g2_powered_f710_20260903_181416.jsonl`无watchdog、无fresh=false、无runtime fault；
但有483个Pink相对takeover工作空间饱和tick，其中平移271、旋转212。到边界后同方向不再推进，
反向摇杆可退出；重新按RB之所以恢复，是因为重新以当前实测位姿建立20cm/20度相对包络。它不是
机械关节限位或网络断链。更早的`180914`轮确有一次205.72ms的真实watchdog，两者需要分别处理。

修复候选现把20cm/20度定义为可滚动的单段积分包络：越界前在同一10ms tick以最后EEF target和
当前IK reference无跳变重设锚点，再执行原输入。连续RB运动不再停住；速度、关节限位、Pink
`safety_break`、奇异位形保护及真实网络watchdog均保持不变。新增结构化recenter事件，离线测试
通过，待Powered长距离平移/旋转验收。

Powered日志`g2_powered_f710_20260904_132248.jsonl`随后记录18次无停顿自动recenter（平移6、旋转
12），旧workspace saturation、watchdog、IK HOLD、runtime fault和fresh=false均为0；操作者确认
修复效果很好。当前执行层冻结为`f710-powered-v1-20260904`，精确文件/配置/验收日志SHA见
`G2Teleop/configs/f710_powered_v1_baseline.json`。后续主线转入人工episode recorder，控制语义变化
必须以新候选重新验收，不能直接污染该基线。

冻结后已建立START锁存式HIL纯逻辑候选：仅START改变POLICY/HIL所有权，RB只在HIL内部控制机械臂，
松开RB仍可换侧和使用RT；退出后必须等待新观测对应的新policy epoch。34D仲裁器禁止旧policy suffix
在HIL/HOLD状态重新被选择，telemetry合同分开记录所有权、policy/human/selected action及三种mask。
预期START button 7仍待Windows只读验证，当前未接真实policy且未Powered；离线回归113项通过。

### 13.3 手柄完成度与DAgger入口判断

> 以下为冻结前的历史规划（superseded），不得当作当前步骤。执行层已在13.2冻结；所有权采用
> START锁存，不是下文旧设想的RB接管/释放。最新进展见F710架构14.18–14.20：Mock episode已通过
> 因果审计，但缺真实视频payload与设备时间戳闭环，training_ready=false；统一推理/录制旁路sink待实现。

F710执行层已经是“功能完成候选”，但要经过A切档、RT无RB和Y全局Home这一轮真机验收后才能冻结。
现有`f710_control_sample`等结构化telemetry用于控制诊断，不能直接冒充训练episode：它不包含完整
三路视频与统一时间戳，也没有逐帧冻结model proposal、human intent、最终executed 34D、measured
34D和intervention mask。

下一主线按以下顺序推进：

1. 完成本轮F710功能验收并冻结映射、速度档和Home语义；
2. 先做“纯人工F710 episode recorder”，复用现有三相机/状态采集合同，将intent、执行命令和实测
   状态对齐到同一时间轴；
3. 录制少量人工episode，执行完整性审计、可视化和命令回放，证明生成数据确实对应机器人动作；
4. 把F710接收器合入DM0.5唯一Control owner，禁止独立F710进程与模型进程同时创建`Robot()`；
5. 再加入DAgger intervention状态机：RB上升沿丢弃未执行policy suffix，记录`intervention_mask=1`；
   RB释放后保持、采集新观测，再恢复policy；
6. 最后才开展正式human-in-the-loop采集和数据回灌。

因此当前已经可以正式开始DAgger工程接入，但还不能直接宣布“DAgger可采集训练数据”。最近的明确
验收门是：人工episode能同时复现视频、measured/executed 34D和F710 intent，且离线审计与回放一致。

## 14. 详细材料入口

- `G2Teleop/doc/F710_PINK_OTG_CONTROL_ARCHITECTURE.md`
- `G2Teleop/doc/G2_F710_DAGGER_FULL_FLOW.md`
- `g2_robot/docs/GDK_2.6.3_RELEVANT_API.md`
- `g2_robot/docs/G2_ROBOT_STRUCTURE.md`
- `g2_robot/docs/G2_TROUBLESHOOTING.md`
- `DM0.5/docs/robot/JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md`
- `DM0.5/docs/robot/incidents/2026-07-31_01_UNIFIED_O10_SERVO_BLOCK.md`
- `G2Teleop/scripts/26_g2_o10_point_target_ab.py`
- `G2Teleop/scripts/28_g2_o10_event_replay.py`
- `G2Teleop/scripts/32_g2_source_speed_qdirect_o10_raw5.sh`
