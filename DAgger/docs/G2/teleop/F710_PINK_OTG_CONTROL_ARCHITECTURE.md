# F710 → Pink → OTG → G2：当前状态、根因与实施路线

> 文档状态：当前遥操控制链的权威设计说明。  
> 更新日期：2026-09-07（交接核对）。  
> 范围：只讨论 F710 双臂/O10 遥操的平滑性、约束和后续 DAgger 接入；暂不展开 RTC/Sync 推理算法。  
> 安全说明：本文是架构与验收文档，不构成 Powered 启动授权。真机启动仍须遵守操作手册、RB deadman 和现场急停要求。

> 2026-09-07后续：Mock跨流/fresh关联、共享观测、三种sink及离线审阅视频已实现并完成离线测试，
> 详见[本轮报告](../HIL_RECORDING_OFFLINE_20260907.md)。14.19–14.20保留实现前历史状态，不能当成最新测试。

## 1. 当前结论

**交接更正：** 当前已验收F710基线为direct Pink + 双臂100Hz normal Servo + O10约5Hz
changed-only，Ruckig默认关闭。下述OTG流程保留为历史目标设计，不代表冻结runtime。
最新状态与原型审计局限见[9月7日交接说明](../HANDOFF_20260907_F710_HIL_RECORDING.md)。
跨项目DM0.5连续手轨迹已有双17D Servo实验进展，不能将本文早期“17D待研究”视为最新事实。

后续沿下面这条链推进：

```text
F710（Windows 采样 50 Hz）
  ↓
静态 input shaping
中心校准 / 径向死区 / expo / 坐标方向
  ↓
Cartesian velocity / twist
vx vy vz wx wy wz
  ↓
滑动的末端目标窗口
不再使用“接管点固定球面”作为主要连续运动边界
  ↓
约束感知的 Pink differential IK
FrameTask + joint-limit avoidance + singularity damping
+ 连续的 nullspace/posture objective
  ↓
dq_ik / q_ik reference
  ↓
唯一的、有状态的 joint governor / OTG
维护 q_cmd / dq_cmd / ddq_cmd
限制 velocity / acceleration / jerk，并提前制动
  ↓
最终位置安全门 / watchdog / RB deadman
  ↓
GDK normal joint_servo_control（双臂100 Hz）

F710手型意图
  ↓
5 Hz渐进采样；20D逐值变化时才提交
  ↓
GDK move_ee_pos（O10点目标，静止不重发）

q_measured feedback
  ↖ 用于漂移、限位、跟踪误差和重新接管初始化
```

各层职责必须分开：

- Pink 决定“当前末端速度应由怎样的关节速度实现”，并处理运动学约束。
- 关节限位、奇异区和 nullspace 竞争首先在 IK 内连续处理，不能只依赖末端硬裁剪。
- OTG 把 IK reference 变成速度、加速度和 jerk 连续的 100 Hz 关节位置命令。
- GDK 最终仍接收关节位置；引入 OTG 不等于把底层接口改成力矩或速度控制。
- OTG 不能掩盖不稳定的 Pink reference。正确顺序是：先可观测，再稳定 IK，再加入 OTG。

## 2. 已冻结的硬件和进程边界

### 2.1 F710 与网络

- F710 接收器只接 Windows，不再接 G2。
- 2026-08-27 已确认：G2 上厂商常驻 `teleop_main_node` 会自动消费 joystick，并可能驱动底盘。即使本项目进程没有底盘代码，也不能阻止另一个进程读同一手柄。
- Windows 通过 SSH tunnel 直接连接 G2 的回环监听端口。
- 开发服务器只负责写代码、同步代码和离线分析，不应位于实时控制数据路径中。
- receiver 只绑定 G2 `127.0.0.1`，避免把 Powered 控制端口暴露到局域网。

### 2.2 当前真机输出范围

当前 Powered 路径的设计范围是：

- 左右臂共 14 DoF：GDK normal Servo，目标循环 100 Hz。
- 左右 O10 共 20 DoF：`dual_tool` / `o10_t2` 的`move_ee_pos`；意图驱动约5Hz渐进变化，目标不变时不调用。
- 腰部只读，不发送命令。
- 底盘完全不在本项目命令结构中。
- 任一时刻只允许选择一侧手臂作为 active side；另一侧保持接管时的稳定姿态。
- 保留“14D 双臂 Servo + 20D 双手 tool command”的 Split 架构，不把它们伪装成统一 34D Servo。

### 2.3 当前还不是 DAgger recorder

现有 Powered 遥操脚本负责人工控制，但没有完成以下 DAgger 数据闭环：

- RGB/深度图像时间戳对齐；
- policy 原始动作；
- 人工 intervention 意图；
- 实际发送给机器人、经过安全层修正后的 executed action；
- `q_measured` 和夹爪反馈；
- episode start/stop、成功/失败标签及场景元数据。

因此“能够用手柄控制真机”不等于“已经自动录制了可训练的 DAgger 数据”。

## 3. 当前实际控制链

目前双臂链路可概括为：

```text
Windows pygame，50 Hz
  ↓
F710 mapper：逐轴 deadband、方向和按键状态
  ↓
平移/旋转速度意图
  ↓
latest-state 网络协议 + ACK
  ↓
G2 receiver，100 Hz
  ↓
约 150 ms Cartesian velocity slew
  ↓
积分得到 target_pose
  ↓
Pink QP 求 dq
  ↓
积分得到 candidate_q
  ↓
关节步长/范围裁剪
  ↓
14D arm action → GDK normal Servo
```

已经具备的正确基础包括：

- latest-state 语义，不追赶过期网络包；
- RB deadman、sequence/session 校验和 watchdog；
- 默认约 4 cm/s 平移与 10 deg/s 旋转；
- Cartesian 速度斜坡约 150 ms；
- Pink `FrameTask` 与 `PostureTask`；
- IK 关节速度限制约 `0.35 rad/s`，单步限制约 `0.01 rad`；
- inactive side 保持；
- 控制周期、IK 和 GDK 调用耗时日志。

这些基础解决了“是否能安全发命令”和一部分输入阶跃问题，但还不能保证整条机械臂持续运动时丝滑。

## 4. 当前抖动的结构性缺口

### 4.1 Pink 之后没有唯一的关节动态连续层

当前 `candidate_q` 基本由每个周期的 Pink 输出积分后直接下发。单步限幅只能限制位置增量，不能保证：

- `dq` 连续；
- `ddq` 有界；
- jerk 有界；
- 反向时先平滑减速到零；
- 靠近关节限位时有足够制动距离。

因此即使每个位置点本身很小，连续位置差分仍可能形成规律性的正负速度变化，表现为整条手臂均匀前后抖。

### 4.2 `virtual_q` 与真机反馈脱节

当前规划状态主要跟随上一周期命令，只在 RB 接管等时刻重新读取真实关节。这样形成了外层 command-space open loop：

```text
规划器认为的位置 q_virtual
        ≠
机器人实际位置 q_measured
```

长时间移动时，跟踪误差可能累计；停下来后底层追上命令，所以主观上又稍微顺滑。这与现场“连续移动越久抖动越明显、停止后缓解”的现象一致，但必须用同步的 `q_cmd - q_measured` 日志确认。

### 4.3 关节限制发生在求解之后

如果 Pink 生成 reference 后才被通用范围或步长裁剪，IK 并不知道它的解在输出端被改写。下一周期它可能继续向被裁剪方向请求，产生：

```text
Pink 推向限制 → 输出裁剪 → Pink 再补偿 → 再裁剪
```

需要统一 MJCF、Pink、DM0.5 数据定义和 GDK 真机的关节限位及安全 margin，并把软避限放进 IK，把硬裁剪保留为最后保险。

### 4.4 固定球形 workspace 会制造“必须重新按 RB”的体验

现有约 20 cm 球形边界以接管时的末端点为球心。它并不是 G2 的真实工作空间，而是一个临时安全笼：

- 以 4 cm/s 持续向一个方向移动，约 5 秒就会碰到球面；
- 到达球面后，即使用户想沿切线或稍微换方向，目标积分和拒绝逻辑也可能持续饱和；
- 松开并重新按 RB 会重新设置球心，于是控制“恢复”。

后续应改为“相对当前可执行/FK 位姿的滑动 lead window + 全局安全工作区”。接近边界时只投影掉继续向外的速度分量，向内运动必须立即可用，不应要求重新接管。

### 4.5 target 更新缺少事务语义

理想过程应是：

```text
propose target
  ↓
solve IK
  ↓
通过约束检查才 commit
```

如果先更新 `target_pose`，Pink 随后求解失败，而 target 没有回滚，误差会累积；下一周期 Pink 会继续追一个更远、可能不可达的目标。普通软不可行应降速、投影或回滚；只有严重安全错误才需要 latch。

### 4.6 缺少奇异区和任务竞争的可观测性

现有日志不足以区分：

- 摇杆输入是否抖；
- Cartesian target 是否抖；
- Pink 的 `dq` 是否发生小符号反转；
- 哪个关节接近限位；
- Jacobian 是否接近奇异；
- posture/nullspace 与末端 task 是否竞争；
- 命令稳定但真机反馈是否振荡。

只继续调 deadband、低通或发送 Hz，无法回答这些问题。

## 5. 现场日志已经支持什么结论

已有日志摘要显示：

- 主循环中位周期约 `10.1 ms`，p99 大致 `12–14 ms`；
- Pink IK 中位耗时约 `0.7–1 ms`；
- GDK 调用中位耗时约 `0.3 ms`；
- 最近有效运行中 watchdog 通常为 0；
- Windows 数据包通常约每 `20 ms` 到达，部分窗口 p99 约 `70–105 ms`；
- `small_reversals` 随连续操作从约 `110 → 160 → 615 → 670` 累积；
- 出现大量 `Clipped 1 ...`、Pink `NoSolutionFound` 和 `WORKSPACE SATURATED`。

最关键的一点是：当前 `small_reversals` 在 `send_action` 之前统计。它证明小幅符号反转已经存在于 GDK 之前，即 Pink/积分/裁剪生成的命令 reference 内，而不是只能归因于机械臂底层。

当前证据优先级如下：

1. Pink、约束切换或 virtual state 产生 reference chatter：强证据。
2. `q_virtual` 与 `q_measured` 漂移：高概率，需要闭环日志确认。
3. 模型限位与真机限位不一致、求解后裁剪：高概率。
4. 固定球边界和 target latch 导致卡住：现场行为已确认。
5. 网络 jitter：会改变手感，但不是稳定周期性整臂抖动的第一根因。
6. IK 或 GDK 计算太慢：现有耗时基本排除。

当前日志仍不能确认“哪个关节被裁剪”以及“命令稳定时真机反馈是否振荡”，所以不能直接宣称底层硬件已经稳定或不稳定。

## 6. Pink 层应该怎样改造

### 6.1 关节限位采用三层职责

1. **Pink 软避限**：进入安全 margin 后连续提高向内速度或惩罚向外速度。
2. **OTG 制动**：结合当前 `dq/ddq` 和剩余距离，提前减速，避免到了硬限位才截断。
3. **最终 hard gate**：只作为最后保险；每次触发必须记录关节名、上下限、输入值和输出值。

硬裁剪应在正常工作区接近零。如果频繁触发，说明上游约束设计不成立，而不是“安全层工作得很好”。

### 6.2 奇异性处理必须连续

每周期记录至少：

- `sigma_min`；
- 尺度归一化后的 Jacobian condition metric；
- 当前 damping；
- IK residual；
- Cartesian command scaling。

接近奇异时使用连续 adaptive damping，并平滑降低任务速度；必要时暂时降低 orientation task 权重。不要用一个阈值在两个 solver 配置之间硬切换，否则阈值本身会成为抖动源。

平移 Jacobian 与旋转 Jacobian 单位不同，原始 6D condition number 容易误导；应分别记录或使用明确的 characteristic length 做归一化。

### 6.3 Nullspace 目标要连续且有优先级

建议优先级：

1. 关节限位安全 margin；
2. 肘部姿态连续性；
3. 舒适/默认姿态；
4. 自碰和环境碰撞 margin；
5. 接管瞬间姿态偏好。

不能每周期改变 posture target 或突然切权重。末端 task 与 nullspace task 发生竞争时，应记录各任务误差和权重。

## 7. OTG 在本项目中的准确含义

OTG 是 online trajectory generation。它不是特定机器人接口，而是“每个控制周期根据当前状态和新 reference，生成下一步动态可执行状态”的层。

对于速度式 F710 遥操，更适合 velocity-tracking OTG：

```text
输入：dq_ik，以及必要的 q_ik/约束信息
状态：q_cmd, dq_cmd, ddq_cmd
约束：v_max, a_max, jerk_max, joint margin
输出：下一周期 q_cmd
```

预期行为：

- 摇杆保持：平滑加速到目标速度；
- 摇杆回中：平滑减速到零并保持；
- 摇杆反向：先穿过零速度，再向反方向加速；
- 接近限位：根据制动距离提前减速；
- reference 小幅反转：不把高频抖动原样变成位置命令。

不要把每个周期的 `candidate_q` 当成一个新的 point-to-point 轨迹目标，否则 OTG 自己也会不断重规划和刹停，产生顿挫。

系统中只能有一个权威动态状态。初次引入 OTG 时保留现有 150 ms Cartesian slew 作为固定对照；验证稳定后，再单变量比较缩短、移除或保留它。不要同时叠加强低通、强 slew、Pink 输出低通、step cap 和 OTG，否则延迟来源无法解释。

RB release/watchdog 属于安全 hold；正常摇杆回中属于平滑减速。两者语义必须分开，安全事件不能为了“丝滑”而延迟停止。

## 8. F710 input shaping 方案

输入层只解决手柄噪声与手感，不承担 IK 稳定职责：

- 启动时中心校准，检测摇杆静态偏置；
- 对每个摇杆使用径向 deadzone，而不是分别裁 X/Y 形成方形死区；
- 死区边缘连续重映射，避免跨边界跳变；
- 使用 expo 曲线提高小幅修正分辨率，同时保持满量程最大速度；
- 对平移和旋转分别限制在单位圆/球，不让对角输入意外变快；
- 只加入极小 hysteresis 抑制中心来回切换；
- 明确验证“左/右、上/下、抬升/下降”和机器人坐标一致。

在 Pink/OTG 和限位问题验收前，不把整体速度提高 1.5 倍。速度更高会更快撞上固定边界，也会放大制动距离与 reference chatter。

## 9. 必须建立的分层日志契约

所有 G2 端日志使用 monotonic nanoseconds，并包含：`session_id`、`sequence`、active side、RB 状态和实际 `dt`。

### 9.1 输入与 Cartesian 层

- 原始轴值、中心校准值、shaped 轴值；
- `twist_raw`、`twist_filtered`；
- proposed/committed target pose；
- planned FK、measured FK；
- target lead、tracking error；
- workspace projection/saturation 及具体原因。

### 9.2 Pink 层

- linearization 使用的 `q`；
- `q_ik_ref`、`dq_ik_ref`；
- translation/orientation residual；
- 每个关节到上下限的 margin，最近关节及方向；
- active inequality constraints；
- Jacobian translation/rotation/normalized-6D singular metrics；
- damping、task weights、solve status 和 solve duration。

### 9.3 OTG 与执行层

- `q_ik_ref`、`dq_ik_ref`；
- `q_cmd`、`dq_cmd`、`ddq_cmd`、jerk；
- OTG status、active constraint、braking margin；
- `q_measured`、`dq_measured` 和时间对齐后的 command/feedback error；
- 每次 clip 的关节名、来源限制和前后数值；
- GDK call duration/result；
- 100 Hz deadline miss、network age、watchdog 事件。

诊断必须能直接回答三类问题：

```text
twist 稳，dq_ik 抖       → Pink/约束/奇异/nullspace 问题
dq_ik 稳，q_cmd 抖      → OTG/积分/裁剪实现问题
q_cmd 稳，q_measured 抖 → Servo/增益/通信/机械问题
```

现有类似 `arm_step=%.1f` 的精度会把毫弧度级变化打印成 `0.0`，无法用于抖动诊断。需保留足够小数位，并提供 per-joint 统计。

## 10. 分阶段实施路线

### P0：只增加可观测性

目标是确定第一次符号反转在哪一层产生，不改变真机控制行为。

截至2026-09-01，P0已由`g2_powered_f710_20260901_141259`真机trace完成到T5：

- `ControlTelemetry`使用有界队列；100 Hz线程只做`put_nowait`，JSON序列化和磁盘写入在独立writer线程；
- 主日志先写入`DM0.5/logs/telemetry/powered/g2_powered_f710_<时间>.jsonl.partial`；只有writer完整退出后才原子改名为`.jsonl`，上传端永远不读取半条运行；
- 事件分为`runtime_start`、`f710_control_event`、`f710_control_sample`和运行结束/故障事件；
- 每个sample保存F710映射后输入、Cartesian期望/filtered速度、target、Pink q/dq、residual、
  joint margin、raw Jacobian奇异值、最终34D command、clip关节、反转累计和GDK时序；
- 如果既有只读反馈probe通过，复用唯一Control owner的feedback sidecar，文件位于
  `DM0.5/logs/telemetry/feedback/<主日志同名>.jsonl`；probe缺失或不通过时自动保持command-only；
- `g2_robot`运动诊断平台已增加`powered-f710`模式，不把Pink reference伪装成VLA `q_model`；
  首页固定使用Sync蓝、RTC紫、F710橙三种卡片颜色。
- F710运行结束后由G2主动把已封口的主日志和可用feedback sidecar上传到6000D；服务器不再为
  F710建立定时SSH拉取。上传失败不删除G2原件，也不改变Powered进程的真实退出码，可在G2用
  `bash scripts/13_g2_push_telemetry.sh --main <主日志> [--feedback <反馈日志>]`重试。
- 当前按现场偏好不配置专用密钥：上传时由操作者输入一次6000D账户密码。G2只调用服务器上的
  固定接收脚本和`telemetry-put powered|feedback <合法文件名>`参数。服务器以临时文件接收，
  验证非空和换行结尾后才原子发布到`g2_robot/logs/telemetry/`；同名文件保持首份不覆盖。

该轮20Hz反馈1121条、零drop；读取P95约0.99ms、最大约3.72ms，100Hz控制周期P95约
11.77ms。稳定全轴归零并settle后，command和measured均保持在微弧度级；稳定向上输入时却
出现83次100ms输入窗口内的小反转，其中62次集中在纯`+Z`，主要位于左臂joint2/joint7，
同时raw Jacobian最小奇异值约0.038--0.051。Pink reference与command严格相同，证明本轮
首次异常位于Pink/reference层，而非hard clip、workspace或静止Servo自激。

- 增加第 9 节分层日志；
- 记录 clip joint identity；
- 确认真机反馈的实际更新率和时间戳语义；
- 日志写入使用有界队列/环形缓冲，不能阻塞 100 Hz 控制线程；
- 建立自动报告：第一次 reversal 前后 0.5–1 s 的全链数据。

验收：一次短日志即可区分 input、Pink、OTG/裁剪和 GDK/真机四层。

### P1：让 Pink 成为约束感知的连续 reference 生成器

截至2026-09-01已实现并完成P1a短时Powered配对验证：

- 基于上述G2实测raw Jacobian阈值增加连续`smoothstep`速度缩放：sigma从0.08下降到0.035时，
  Cartesian实际应用速度从1.0连续降到0.15；scale自身限速变化，避免防护器制造新阶跃；
- Pink QP damping按同一连续风险从`1e-8`对数插值到`1e-4`；
- 日志新增`applied_*_velocity`、`singularity_sigma_min`、`singularity_speed_scale`和
  `ik_solver_damping`，profile标记为`pink_singularity_guard_v1_no_otg`；
- 这些阈值是当前G2 MJCF和raw 6D Jacobian口径下的真机经验值，不宣称为Pink通用常数；
- OTG仍未接入，P1a必须先复现同一纯`+Z`测试，并比较稳定输入反转、任务速度和residual。

P1a真机trace为`g2_powered_f710_20260901_152111`（Ctrl-C/tee旧退出缺陷使原件保留为
`.partial`，但7349条主记录和1512条feedback记录均可解析，末行分别为`runtime_stop`和
`joint_feedback_summary`）。与P0 `141259`按同一口径比较：

- 纯`+Z`稳定输入由290 tick内62次小反转，降为504 tick内0次；纯`-Z`222 tick仅1次；
- 所有稳定非零输入的反转率由约2.68次/s降为约1.65次/s；
- P1a剩余78次稳定输入反转中，68次发生在同时激活2或3个Cartesian维度，表明下一问题已从
  单轴低sigma奇异区转移到combined输入下的IK/约束竞争；
- sigma最低约0.0489；最低区速度scale约0.343、damping约`1.24e-5`，说明连续guard真实介入，
  没有触发hard clip；
- 反馈稳定20.01Hz，command→measured最佳对齐延迟约115ms，平均绝对误差约0.625mrad，未见
  静止Servo持续自激；
- packet age最大154ms、无200ms watchdog。网络P99较P0变差，但latest-state传输下不会在稳定
  输入期间周期性停发，不能解释剩余combined-input Pink反转。

因此P1a通过，不回滚。P1下一小步应针对combined输入记录每个task/constraint贡献、active
joint-limit/velocity damper和nullspace项，先消除或解释2–3维同时输入时的参考反转；在Pink
reference可接受后再进入P2 OTG。

随后完成的长时双侧验证`g2_powered_f710_20260901_155045`进一步改变了准入判断：

- 179.7秒、17536个100Hz控制样本；左臂有效输入约111.9秒，右臂约48.8秒；
- 稳定非零输入反转率降至约0.806次/s，相比P0下降约70%；单轴稳定输入4089 tick内30次，
  combined输入3726 tick内33次，已不再出现P0纯`+Z`的密集joint2/joint7成对翻转；
- 63次稳定输入反转中，56次发生在sigma大于0.08的健康区；sigma低于0.035时319 tick只有
  2次，说明继续加大奇异区阻尼已不是主要优化方向；
- joint-limit最小margin约0.100rad，position residual最大约0.605mm、orientation residual
  最大约0.00262rad，无hard clip、无watchdog；
- 20Hz feedback对齐延迟约110ms，arm MAE约0.236mrad、P95约1.031mrad，均优于P0；
- RT日志严格呈现左手`1/0`九次、右手`1/0`四次的独立二值切换，证明toggle映射已生效；
- 仍有22段workspace saturation、累计约8.89秒。该问题属于固定接管球边界，OTG不会修复，
  必须单独实现sliding lead window与global safe workspace。

这组证据满足进入P2的工程准入条件：不是宣称Pink reference已数学无反转，而是它已经从
主导故障源下降为稀疏小参考扰动，且residual、margin、clip均健康。下一变量应是active-arm
OTG；不得同时改Pink阈值、最大速度和workspace策略，否则无法做归因。P2仍须先离线回放和
Shadow，随后才做最小幅Powered配对测试。

- 统一模型和真机 joint limits、单位及 margin；
- Pink 内加入连续 joint-limit avoidance/velocity damper；
- 加入尺度合理的奇异性指标和 adaptive damping；
- 固定、连续的 nullspace/posture 策略；
- target 改成 propose → solve → commit；
- 普通 infeasible 走降速/投影/回滚，不立即造成永久卡死；
- 固定球改为 sliding lead window + global safe workspace；
- 边界处允许向内和切向运动。

验收：Pink reference 不再累积密集小反转；hard clip 正常操作为 0；到达边界后无需松开 RB 即可向内退出；residual 有界。

### P2：加入 active-arm velocity-tracking OTG

- OTG 只管理 active arm 7 DoF；inactive arm 明保持；
- 输入以 `dq_ik_ref` 为主；
- 输出仍组装为 14D position action；
- 重新接管/切手时从同步 `q_measured` 初始化 `q/dq/ddq`；
- 正常回中平滑减速，RB/watchdog 使用单独的安全停止策略；
- 先离线、Shadow 回放，再做空场景最小幅 Powered 验证。

验收：`q/dq/ddq` 连续，反向速度穿零，停止距离有界，不新增长周期小反转，active constraints 可解释，hard clip 为 0，且延迟不影响瓶颈 correction。

### P3：单变量调手感

每轮只改一个变量并保留基准日志：

1. 径向 deadzone 与中心校准；
2. expo；
3. 保留/缩短/移除当前 150 ms Cartesian slew；
4. OTG acceleration；
5. OTG jerk；
6. 最后才提高最大平移/旋转速度。

不能只凭“更像游戏”验收；同时比较 tracking lag、reversal count、jerk、边界事件和任务完成时间。

### P4：O10 手型与 DAgger

- 每只手继续采用两个主要控制量：四指同步屈伸 + 拇指协同；其余构型保持任务固定值；
- **已完成（2026-09-01）**：RT一次按下切换握住/松开；Windows映射层使用
  0.65/0.35 Schmitt迟滞并按左右手分别保持二值意图，G2只接收最新目标；
- RB 松开或网络 watchdog 只保持当前安全手型，不能意外自动张手；
- 手部动作使用约当前时间 `1/1.5` 的连续 S-curve，但维持 O10 10 Hz 接口边界；
- 接入 RGB、policy action、human action、executed action、feedback 和 episode 元数据；
- 分别采集左手碰倒后 regrasp 与左右手交替时右手瓶颈 correction，各约 10 条，再评估约 5K 步继续微调。

## 11. 真机验收矩阵

### 11.1 安全与作用域

- 底盘没有可达命令路径；
- 腰部只读；
- 只使用 normal Servo；
- RB、watchdog、session/sequence 均有效；
- 不存在未知来源 clip、solver error 或 stale feedback；
- 第二人持物理急停，空工作区，先单轴最小幅度。

### 11.2 时序

- G2 维持 100 Hz deadline，不追赶过期周期；
- 记录 p50/p95/p99/max；
- 日志系统开启后不显著恶化周期；
- 网络 jitter 只改变最新意图年龄，不造成动作队列补发。

### 11.3 Pink

- 稳定输入下没有密集 `dq_ik` 小符号反转；
- 接近限位/奇异时行为可由日志解释；
- 边界处向内退出无需重新按 RB；
- residual 和 damping 连续有界。

### 11.4 OTG

- `q_cmd/dq_cmd/ddq_cmd` 连续；
- 反向先经过零速度；
- 松杆停止距离有界；
- OTG 不制造新的 reversal；
- constraint/braking 状态可解释；
- 不产生不可接受的 correction 延迟；
- 正常轨迹 hard clip 为 0。

### 11.5 闭环

- `q_cmd` 与 `q_measured` 做时间对齐比较；
- 若 `q_cmd` 稳而 `q_measured` 振荡，立即停止继续调 OTG，转查 Servo/增益/反馈；
- 顺序为单轴平移、单轴旋转、组合小运动、最后才是实际抓瓶任务；
- 任何异常声音、持续振动或非计划运动立即松 RB/急停。

## 12. 已冻结决策与禁止项

已冻结：

- F710 只接 Windows；
- Windows 直接 SSH tunnel 到 G2；
- receiver 绑定 loopback；
- 双臂 normal Servo 100 Hz；
- O10 `move_ee_pos` 10 Hz；
- 底盘禁用、腰部只读、一次只控一侧、RB deadman；
- 网络使用 latest-state，不 replay 旧输入；
- 稳定性验收前不提高最大速度。

禁止：

- 再把 F710 接收器插到 G2；
- 为追求顺滑切换 low-latency Servo；
- 擅自提高 O10 命令频率；
- 仅靠加大 deadband/低通掩盖 Pink chatter；
- 没有 `q_measured` 证据就宣称底层稳定；
- 用 OTG 隐藏不可解释的 Pink reference；
- 把当前遥操程序误认为 DAgger 录制器；
- 从开发服务器远程直接启动 Powered 控制。

## 13. P2 Ruckig 实施状态（2026-09-01）

P0分层日志与P1a奇异区连续保护已经完成。P2代码、G2原生依赖、历史回放、Live Shadow和
第一次Powered实验均已完成。第一次Powered实验仍明显抖动且触发一次OTG lag HOLD，因此
P2明确没有通过验收，不能因“Ruckig已运行”就当作正式控制基线。

已完成：

- 使用官方Ruckig `0.19.4`，不是自写OTG；
- 使用velocity control interface，输入Pink `dq_ref`，不把每帧`q_ref`当作新的P2P终点；
- active arm独立维护`q/dq/ddq`，输出仍是7D位置，再组装为14D GDK Servo命令；
- 默认候选固定为`v=0.35rad/s`、`a=2.0rad/s²`、`jerk=20rad/s³`、phase synchronization；
- 接管从同步实测`q`、零速度和零加速度初始化；
- workspace安全饱和、RB/watchdog/HOLD不使用缓慢滑行，而是立即冻结并清零OTG动态状态；
- Ruckig不可导入时在GDK初始化之前拒绝启动；
- 日志增加OTG target、`q/dq/ddq/jerk`、计算耗时、约束利用率、反向/减速状态和限位余量；
- 仪表盘T6能够显示Pink目标速度、Ruckig输出速度以及acceleration/jerk约束利用率；
- `15_replay_ruckig_otg.py`可对历史F710日志做无GDK离线回放。

对`g2_powered_f710_20260901_155045.jsonl`的第一轮固定参数回放：

| synchronization | 小反转 direct→OTG | 位置偏差P95 | 速度偏差P95 | direct→OTG jerk P95 | Ruckig计算P95 |
|---|---:|---:|---:|---:|---:|
| phase | 101→88（-12.9%） | 0.00790rad | 0.01933rad/s | 22.8→20.0rad/s³ | 约1.1µs |
| time | 101→86（-14.9%） | 0.00790rad | 0.01935rad/s | 22.8→20.0rad/s³ | 约1.1µs |
| none | 101→92（-8.9%） | 0.00789rad | 0.01927rad/s | 22.8→20.0rad/s³ | 约0.9µs |

phase虽然反转计数不是最低，但能在从静止加速时保持Pink关节速度向量的比例，优先作为
Cartesian任务保真的首个候选。历史回放不闭环重算Pink，因此以上偏差只能筛掉明显不合格
候选，不能替代Live Shadow/Powered配对验证。该参数把历史最大acceleration从约4.44限制到
1.99rad/s²、最大jerk从约120.2限制到20rad/s³，计算耗时相对10ms周期可忽略。

G2实机域控已从清华PyPI源码构建并安装`ruckig==0.19.4`到项目私有`vendor_ik`，生成原生
`cp312-cp312-linux_aarch64` wheel，纯数学smoke通过。相同`155045`日志在G2复现的三种
synchronization统计与6000D一致；phase计算P95约3µs，仍仅占10ms控制周期的0.03%。

### P2第一次Live Shadow发现并修正的状态耦合

`g2_shadow_f710_ruckig_20260901_173054.jsonl`是第一轮100Hz零输出Shadow：2951个控制样本、
1366个enabled样本、日志丢样0、workspace saturation 0、无IK/OTG HOLD；控制周期P50/P95/P99
约10.08/10.71/13.20ms，Ruckig计算P50/P95约19/32µs。全程明确`hardware_output=false`。

但该轮不能作为Powered放行依据。第一版让Pink从Ruckig滞后的command state继续解算，形成：

```text
Pink pose-error correction → Ruckig lag → Pink继续追赶 → target dq翻转
```

在部分摇杆已回中的窗口，Pink目标仍饱和到0.35rad/s，OTG输出累计97次小反转；最坏约0.98秒
回中窗口内，虚拟关节累计路程约0.177rad而净位移仅0.044rad，结束速度仍约0.20rad/s。
因此拒绝第一版直接上Powered。

修正版严格拆开两种状态：

```text
Pink独立运动学reference q_ik → dq_ref
Ruckig唯一命令动态状态 q_cmd/dq_cmd/ddq_cmd
```

Pink不再用OTG滞后状态形成位置追赶反馈；摇杆回中后Pink reference归零，Ruckig只完成一次
有界减速。以无OTG真实Pink日志`155045`回放该结构，所有长回中窗口的OTG速度均收敛到近零，
没有第一版的往返路程放大；全局`|q_ik-q_cmd|`最大约0.0176rad。运行时新增0.03rad硬lag门，
超过即OTG HOLD并要求松RB清除，不能让运动学reference与实际command无限分离。

### P2第一次Powered结论：OTG没有解决抖动

`g2_powered_f710_20260901_174134.jsonl`是修正状态耦合后的第一次Powered OTG运行：

- 6565个dispatch，约67.7秒，其中5987个enabled；
- normal碰撞保护Servo、14D双臂100Hz、O10 Split 10Hz；
- 无watchdog、workspace saturation、手臂clip、奇异区降速或关节限位接近；
- Jacobian最小奇异值保持约0.10以上，singularity scale始终为1；
- GDK调用P50/P95约0.28/1.06ms，单次调用不是主要阻塞源；
- 20Hz反馈在可用窗口内与command速度相关系数约0.98–0.994，估计纯延迟约100ms；
- 反馈在约45秒因一次读取7.242ms超过5ms门限自动关闭，因此不能用该sidecar排除后半段或
  10Hz以上的硬件振动。

现场表现为X/Y平移明显抖，旋转相对正常。日志不支持“Pink持续正负翻转”是本轮唯一根因：
多个持续单向平移段中Pink和OTG均无速度反转、`travel/net≈1`，但现场仍抖。差异更符合关节
动力学与发送时基共同作用：X平移主要驱动近端关节1/4，速度P95约0.16/0.207rad/s、加速度
P95均约0.8rad/s²；旋转Y/Z主要落在较轻的腕部关节6/7。相同速度脉动在整臂平移中更明显。

本轮控制loop的tick周期为P50/P95/P99/max约
`10.08/12.47/13.46/26.35ms`，平均仅约96.94Hz；382个周期超过12ms。Ruckig和GDK请求仍按
固定10ms计算，因此连续位置即使无反转，也会以不均匀真实时间到达，形成“走一点—多等几
毫秒—再走”的速度脉动。网络包P95虽约33.7ms、max约144.6ms，但latest-state链路没有
watchdog，恒定摇杆期间不会重放或清零，因此网络不是持续平移抖动的首要解释。

OTG本身满足2rad/s²和20rad/s³硬上界，但20rad/s³上限在X/Y/Z平移中分别约23.5%、34%和
49%的active样本被顶满，说明“数学有界”尚不等于“物理足够柔和”。约第48.5秒从正X回中
再反X时，Pink/OTG在关节4的lag超过0.03rad并触发一次OTG HOLD。直接继续降低jerk会进一步
增加该lag，因此当前禁止盲调参数。

## 14. P2.1 发送时基优先实验（已实现，待Powered A/B）

2026-09-01新增`timing-ab`诊断档，目标是先隔离100Hz执行时基，不改变机器人运动语义：

- 保持normal碰撞保护，禁止low-latency；
- 保持双臂14D Servo 100Hz与O10 Split 10Hz；
- 保持现有速度、Cartesian slew、Pink和OTG参数；
- 显式关闭T5同步反馈读取，避免它成为实验混杂变量；
- 每个100Hz tick保存紧凑`f710_dispatch_sample`：真实GDK调用开始/结束、相邻dispatch间隔、
  control deadline/lateness、IK耗时、输入、14D command和安全状态；
- 完整T1–T3/T6重型分层降到20Hz，避免JSON序列化与大对象构造持续竞争100Hz Python控制线程；
- dashboard把紧凑dispatch当作真实100Hz command，不把20Hz分层样本伪装成100Hz；T5明确显示
  “实验性关闭”；
- `MinimumPeriodPacer`只在最后0.5ms短自旋以减少Linux sleep overshoot；任何晚周期都从实际
  start重新计时，不允许短周期追赶过期tick。

启动入口只有一个参数不同：

```bash
cd /home/agi/xza/G2Teleop

# A：无OTG timing基线
bash scripts/18_g2_powered_f710_timing_ab.sh direct

# B：Ruckig OTG；必须在A完成并回到同一Home后再运行
bash scripts/18_g2_powered_f710_timing_ab.sh otg
```

每轮只做左臂、无接触、小幅、相同顺序：`+X 2s → 回中1s → -X 2s → 回中1s → +Y 2s →
回中1s → -Y 2s → 回中1s → 单轴旋转各2s`。不得在同一轮调整速度、deadzone、jerk、
acceleration或synchronization。第二人持续持物理急停；任何持续振动、异常声音或非计划运动
立即松RB并停止，不为凑满时长继续。

日志回传后在6000D执行：

```bash
cd /home/xza_100850113/projects/G2Teleop
PYTHONPATH=src python3 scripts/19_analyze_f710_timing_ab.py \
  /path/to/direct.jsonl /path/to/otg.jsonl
```

候选时序门不是最终安全认证，只用于决定是否继续：dispatch interval P95≤10.8ms、P99≤12ms、
`>15ms=0`、telemetry drop=0、无watchdog/IK/OTG HOLD。即使统计通过，只要现场仍抖，也判失败。

### 14.1 2026-09-02 离线加固状态

当前暂时不具备真机测试条件，不阻塞代码侧工作；但也不能把“离线测试通过”写成“抖动已修复”。
本轮完成以下不触碰GDK的修正：

- `scripts/19_analyze_f710_timing_ab.py`不再只凭dispatch百分位给出通过；总门限现在同时要求：
  `timing-ab`档、至少100个有效间隔、正常且非恢复式`runtime_summary`、无`runtime_fault`、
  telemetry drop为0、`control_sequence`连续且单调、无watchdog以及无任何`*_hold`；
- 分开报告`timing_thresholds_pass`和`run_integrity_pass`。前者只说明100Hz时基，后者说明日志
  可用于结论；只有二者都通过，`candidate_timing_gate_pass`才为真；
- O10零反馈回退判断已收敛为同一个纯逻辑入口。新鲜、严格验证过的最后成功命令可用；过期记录
  只有20D逐值精确等于独立审核Home才可用；其他情况继续HOLD，不能通过延长时限或伪造Home绕过；
- `scripts/12_g2_powered_f710.sh`会在要求操作者按Enter之前先跑files-only preflight。它验证模型、
  Ruckig依赖、端口和O10回退文件，不导入或初始化GDK，也不发送机器人命令；
- files-only preflight显示`O10 ZERO-FEEDBACK FALLBACK READY`时，表示若现场反馈为零仍有合法锚点；
  显示`UNAVAILABLE`时并不证明手坏了，只表示Powered启动只有在实时O10反馈非零时才能继续，否则
  必须先执行审核过的Home复位流程；
- 最新现场报错`age=56103.7s`属于上述安全门按设计拒绝，不是OTG或timing loop崩溃。修复目标是
  更早、更清楚地暴露条件，而不是取消安全门。

离线验证结果：新增启动决策和A/B完整性测试均通过；除一个需要本机loopback socket、受当前
沙箱权限限制而未执行的既有transport测试外，其余`68 passed, 2 skipped, 1 deselected`。这只证明
纯逻辑没有回归。真正的运动结论仍需以后完成同一Home、同一动作的direct/OTG Powered配对。

### 14.2 已有direct timing-ab揭示的dispatch相位错误

重新用严格分析器检查`g2_powered_f710_20260901_181619.jsonl`，该轮约90.94秒、8975个dispatch，
日志完整性通过：零drop、control sequence连续、无watchdog/HOLD、正常结束。但时序门失败：

- dispatch interval P50/P95/P99/max为`9.91/12.78/15.35/37.55ms`；
- 648次超过12ms，114次超过15ms；
- loop-start control interval P50/P95/P99/max为`10.00/10.67/12.91/29.19ms`；
- IK P50/P95/P99/max为`1.11/3.16/5.04/28.54ms`；
- `corr(dispatch interval, 当前IK耗时-上一帧IK耗时)=0.902`。

这暴露了P2.1第一版的具体结构错误：`MinimumPeriodPacer`稳定的是loop/IK开始时刻，而真正的
`controller.send_action()`位于可变耗时IK之后。于是相邻IK耗时的变化直接调制了GDK发送相位；
提高名义Hz不会消除该误差。

当前代码已改为`compute → pre-dispatch pacer → GDK send`：

- Pink/OTG先完成计算，正常小于10ms的变化只消耗本周期剩余slack；
- 同一个不追赶pacer在真实GDK调用紧前方建立10ms边界；计算超预算时立即下发，并如实记录
  `dispatch_lateness_ms`，下一周期仍从实际dispatch重新计时，不补发；
- watchdog在真实dispatch时刻再次判定。若计算/等待期间输入越过200ms，丢弃本tick新解、恢复
  上一个手臂命令并释放IK/OTG状态，下一次有效输入必须重新从实测关节接管；
- O10状态只在dispatch freshness确认后推进，因此不会多发送一个过期的夹爪步；
- runtime和每个sample记录`pacer_phase=pre_dispatch`，并保留旧字段别名以兼容dashboard。

这是一项有历史日志直接证据的时基修复，但仍只完成离线代码验证；它尚未获得G2 Powered结论。
下一条真机direct timing-ab首先要验证dispatch P95/P99是否收敛，然后才能跑OTG配对。

下一步严格按以下顺序：

1. ~~固定Ruckig、历史回放、Live Shadow、第一次Powered OTG；~~ 已完成但Powered未通过；
2. 当前无真机阶段：只继续离线回放、日志完整性、启动预检和dashboard解析，不改运动参数；
3. 恢复真机条件后，先完成审核Home，随后以同一Home和相同动作完成`direct timing-ab`与
   `otg timing-ab`配对；
4. 先比较真实dispatch interval、pacer lateness与IK耗时，再比较command反转和现场视频；
5. 若两轮时序均不达门，优先把100Hz command owner与Python JSON/非实时工作做进程级隔离，
   不调OTG；
6. 若时序达门但仅平移仍抖，再做近端关节command—feedback/外部视频诊断，并评估Cartesian
   jerk shaping、per-joint约束和Pink anti-windup；
7. 只有配对结果证明OTG改善且无lag HOLD，才讨论`phase/time`和约束参数；当前OTG仍为显式实验。

### 14.3 BC→EEF→当前链路第一版实验（历史设计，已被14.5取代）

2026-09-02明确纠正实验设计：此前`training_data_replay.py`把训练集34D关节目标直接送进
`joint_servo_control`，只能回答“GDK直接关节回放是否抖”，不能回答当前摇操链的抖动来自
EEF输入、Pink、Ruckig还是Servo。新的对照链固定为：

```text
训练集双臂14D关节轨迹
  → G2_t2_crs_o10_t2 Pinocchio FK
  → 左右 hand_*_base_link EEF轨迹
  → 100 Hz理想Cartesian twist
  → 现有input slew / Pink / singularity与joint-limit / Ruckig
  → 双臂14D关节reference
  → low-latency joint_servo_control 100 Hz

训练集双手20D（第一版同时加入，14.5已从首轮机械臂因果实验移除）
  → 当前Split O10 v1_1 → move_ee_pos 10 Hz
```

双臂使用两个独立`PoweredArmTarget`，不再复用F710只能选择单臂的active-side状态。frame 0只
作为相对轨迹原点，实际轨迹叠加在接管时的实测EEF上，避免启动时把绝对EEF目标跳到机器人。
命令范围不包含底盘或腰部输出。第一版计划把O10和`low-latency`同时加入Powered回放；复核后发现
这会同时改变输出线程、GDK接口和Servo通道，无法定位双臂抖动，因此该Powered设计不得继续使用。

第一次零输出离线回放保留原31.567秒时标，得到：3158个tick中2200个EEF输入速度超出现有
4cm/s和10deg/s上限，897个tick触发相对目标边界，`max|q_source-q_cmd|=0.9104rad`。实际示教
EEF速度最大为：左/右平移0.445/0.289m/s，左/右旋转0.989/1.610rad/s；完整路径相对frame 0
覆盖左/右平移0.256/0.309m、旋转0.524/0.925rad。由此拒绝原时标直接Powered：那不是同路径
A/B，而是大面积削顶后的另一条轨迹。

第二次零输出采用`velocity-limited`时间重参数化：保持全部948个EEF路径点和顺序，只让每个原始
区间获得满足当前Cartesian与0.35rad/s关节速度上限所需的时间，并保留80%利用率给150ms input
slew和OTG；诊断专用clutch包络扩展到该已知示教的有限实际覆盖范围。结果时长91.044秒、9105个
tick，输入削顶0、workspace触发0。Pink位置残差最大约0.486/0.482mm，姿态残差最大约
0.00210/0.00210rad；Pink/Ruckig最大位置lag左0.01184rad、右0.00974rad，均低于0.03rad门。
这证明数学链能够完整运行，但不等于真机不抖。

以下入口从2026-09-02起仅生成离线冻结产物，不再是Powered入口：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/20_g2_bc_eef_chain_replay.sh
```

该脚本不提供`--execute`，不导入GDK，只计算共同时间轴上的`Q-direct/Q-pink/Q-otg`并写入NPZ。
真正的Powered读取端是`scripts/21_g2_frozen_arm_stage_replay.py`，但在完成14.5规定的离线产物
审计和人工授权前不得加`--execute`。

### 14.4 当前研究想法、OTG实际效果与未决问题摘要

#### 用户的核心想法

当前工作的目标不是继续堆叠滤波器，而是把抖动按控制层做可归因定位：

1. G2应用层应使用GDK公开的`joint_servo_control`；机器人内部更高频的电机环、状态演化和插值由
   厂商维护。应用侧不应因为平板看起来丝滑，就假设自己需要绕过SDK或直接实现1kHz电机控制。
2. “直接训练关节回放较平稳”与“F710 EEF摇操很抖”只有在输出通道、路径、时间轴和日志层可比时，
   才能定位差异。直接34D回放不能替代当前EEF控制链测试。
3. 正确对照应把同一条训练双臂轨迹先用FK转成左右EEF，再经过当前的input shaping、Pink、约束和
   Ruckig，最后输出关节reference。这样才能判断新增抖动是否出现在EEF→IK→OTG，而不是只证明
   SDK可以接收关节角度。
4. 第一版曾计划固定low-latency并同时加入双手；14.5复核后将这两个因素拆开。首轮必须是
   normal Servo、arms-only；low-latency和O10各自作为后续单变量，不能混入基准对照。

#### Ruckig目前确实做到了什么

Ruckig在当前实现中是关节reference之后的有状态在线轨迹生成器，维护唯一的
`q_cmd/dq_cmd/ddq_cmd`状态并约束速度、加速度和jerk。修正Pink与OTG状态耦合后，它能做到：

- 防止关节速度reference直接形成无界的速度、加速度或jerk跳变；
- RB回中后完成一次连续减速，而不是由Pink追逐滞后command造成往返振荡；
- 在历史日志离线回放和新的BC→EEF链中保持有限lag；新的retimed全链最大lag为左0.01184rad、
  右0.00974rad，低于0.03rad门；
- 计算耗时通常只有几十微秒，不是100Hz loop的主要计算负担；
- 提供明确的`q/dq/ddq/jerk`和约束利用率日志，使“输入反转、IK反转、OTG减速、最终command”可以
  分层观察。

因此OTG不是“完全没用”：它解决了关节reference的动态可行性和连续性问题，并消除了第一版
Pink追赶OTG状态形成的反馈振荡。但它也没有在第一次Powered实验中消除现场的整臂平移抖动，
所以不能把“Ruckig启用”写成“丝滑问题已解决”。

#### 已暴露的问题

- OTG约束的是关节轨迹，不会自动修复上游EEF输入削顶、Pink目标边界、IK零空间变化、奇异性策略
  切换或错误的dispatch时基。
- 第一次Powered OTG中，X/Y平移仍明显抖、旋转相对较好；多个持续单向窗口没有Pink/OTG符号
  翻转，说明“微小反向意图没有被识别”不是唯一充分解释。
- 已发现真实GDK dispatch曾被可变IK耗时调相。即使每次Ruckig按10ms算出连续位置，若真实发送
  间隔在约10–30ms之间波动，机器人看到的仍是速度脉动。pre-dispatch pacer已修，但还需要成对
  Powered日志验证。
- Ruckig的jerk上界在不少平移样本中被顶满；数学上满足20rad/s³不代表近端重关节在物理上足够
  柔和。盲目继续降低jerk又会放大Pink/OTG lag，可能触发0.03rad HOLD。
- 原始31.567秒训练轨迹远快于F710小幅修正链：约69.7%的tick发生EEF速度削顶，并且完整姿态变化
  超过F710的相对clutch包络。原时标直接Powered会改变路径，不能作为公平A/B。
- 速度重参数化把同一组EEF路径点扩展到91.044秒，离线实现了零输入削顶、零workspace触发以及
  亚毫米级Pink位置残差。这只证明算法链可运行，不证明真机跟踪、dispatch或机械响应已经平滑。
- `max|q_source-q_cmd|`不能单独判定IK失败：双臂7DOF对6D EEF存在冗余，同一EEF路径可能对应不同
  关节姿态。必须同时看期望EEF、Pink target、IK residual、`q_ref`、Ruckig `q_cmd`和实测`q`。
- low-latency去掉厂商碰撞保护，但不会自动提高应用侧时基稳定性，也不会替代Ruckig或机器人内部
  伺服。它只是本次A/B中固定的GDK输出通道。

#### 截至目前的实验状态

```text
直接训练关节回放（normal）
  已完成：仍有一抖一抖，但主观程度小于F710链
  局限：链路与输出通道均不匹配，不能直接归因

F710 → Pink → Ruckig → normal Powered
  已完成：平移仍明显抖；OTG未通过“解决抖动”的验收

BC joint → FK EEF → current Pink/Ruckig（原时标，zero output）
  已完成：大量输入和目标边界削顶，不具备Powered可比性

BC joint → FK EEF → current Pink/Ruckig（velocity-limited，zero output）
  已完成：路径数学链通过，0次削顶、0次边界触发、有限IK/OTG残差

同上 → 第一版fixed low-latency + O10 Powered
  已撤销：同时改变多个变量，不能形成因果结论；从未执行
```

当前不能下结论“抖动就是IK”或“抖动就是网络”。已有证据只把候选集中到：真实dispatch时基、
EEF/Pink产生的关节reference特征、low-latency与normal执行差异、机器人内部跟踪及近端关节物理响应。
下一条真机日志需要让这些层在同一轨迹和时间轴上同时可见，才可继续收敛因果结论。

### 14.5 2026-09-02 独立复核后的共同轨迹实验合同（当前有效）

#### 为什么最新F710日志还不能给OTG定性

重新检查最新三条Powered F710日志：

- direct `g2_powered_f710_20260902_110050.jsonl`的dispatch P50/P95/P99约为
  `10.000/10.632/12.841ms`；去掉7.47秒未激活间隙后仍有2个区间超过15ms，并出现4次watchdog；
- OTG `110651`为`10.000/10.480/12.865ms`、4次超过15ms，并触发一次`0.030845rad` lag HOLD；
- OTG `111145`为`10.000/10.604/12.875ms`、2次超过15ms，并触发一次`0.030447rad` lag HOLD；
- pre-dispatch pacer已把`dispatch interval`与相邻IK耗时变化的相关系数从旧日志的`0.902`降至
  约`0.104/0.220/0.234`，证明发送相位修正确有作用，但三轮P99仍未通过12ms门。

更重要的是三轮操作者输入完全不配对。按时间积分的归一化绝对XYZ输入分别约为：direct
`30.596/47.814/27.899 axis·s`，两条OTG为`35.075/20.447/1.050`和
`34.686/15.306/0.000 axis·s`。因此反转次数、workspace触发和主观抖动不能用于宣称OTG改善或
恶化；这正是改用冻结数据集轨迹的原因。

#### 数据集不是天然的平滑命令

`training_replay_v1.json`同时保存948×34的`states`和`actions`，30Hz、31.567秒。此前加载器只保留
`actions`，丢失了“实测示教路径”和“记录命令”的区别。当前文件本身SHA256为
`28233ac288e6fdbeb552dc44bc491817eea31c2f6b99949c8080b2068f799f3d`；重新计算得到：

- `max|action-state|`为双臂`0.06687rad`、双手`0.92103rad`，两者不可互换；
- states双臂30Hz帧间最大跳变`0.06750rad`，速度P95/最大值`0.38890/2.02506rad/s`；
- actions对应值为`0.06750rad`和`0.39088/2.02506rad/s`；
- actions离散加速度/jerk最大约`19.51rad/s²`和`1051.84rad/s³`。

所以“高质量示教”适合作为空间路径证据，不代表原始30Hz数组可直接作为满足当前动态限制的
100Hz command。当前默认选`states`重建实际走过的EEF路径，同时保留`--path-source actions`用于
独立敏感性分析；任何报告都必须写明来源。

#### 当前实现的实验不变量

`scripts/20_g2_bc_eef_chain_replay.py`现为严格离线生成器：

1. 验证states/actions均为对齐、有限的Nx34，并记录训练文件自身SHA256和源JSONL SHA256；
2. 只做`velocity-limited`时间重参数化，不删除或重排EEF waypoint；
3. 在同一100Hz `time_s`上同时冻结：`Q-direct`（选定数据源的插值关节路径）、`Q-pink`
   （同一EEF输入经Pink但无OTG）和`Q-otg`（同一Pink语义再经Ruckig）；
4. 追加默认1秒零输入settle尾段，并保存hands参考，但不启动任何O10输出；
5. 以`g2-causal-command-replay-v1` NPZ保存全部数组、参数、时间轴和metadata；生成后再记录产物SHA256。

Powered发送已拆到`scripts/21_g2_frozen_arm_stage_replay.py`。它不导入FK、Pink或Ruckig，只从同一个
NPZ选择一条已冻结stage；首轮固定`arms-only`，不会创建O10线程。默认normal碰撞保护Servo；只有把
`--servo-channel low-latency`作为独立实验变量时才关闭碰撞保护。`--execute`前的硬门包括：公共
时间轴确为100Hz、settle至少0.5秒、所选stage最后250ms最大速度不超过`0.005rad/s`、实测起点与
首命令最大差不超过`0.05rad`。

每个真实发送tick记录artifact时间、stage、14D命令、deadline、真实send start/end、相邻发送间隔、
pacer lateness和controller调用耗时。`DispatchTelemetry`从同一controller记录已确认的14D发送及
实际动态统计；只有已通过probe的10/20Hz 34D feedback才在同一GDK owner内采样并写sidecar。首轮
不再让O10线程、磁盘I/O或独立GDK读线程成为隐藏变量。

#### 本轮离线验证与明确限制

本地完整纯逻辑回归（排除当前沙箱禁止loopback socket的既有transport测试）结果为
`78 passed, 2 skipped`；新增冻结文件加载、states/actions对齐、三stage公共时钟、offline-only生成器、
arms-only Powered读取端及精确发送时间字段均有测试覆盖。`py_compile`通过。

本轮随后已逐文件同步到G2，并在G2原位vendor MJCF上完成zero-output生成和FK审计；全过程未导入
GDK、未构造`Robot`、未发送Servo/O10命令。冻结产物及日志为：

- NPZ：`g2_bc_eef_causal_commands_20260902_164309.npz`，SHA256
  `d1ef250c722a449fb47f431be03625af6f0af7a13039a43e42671ebec6cf90bd`，2.7MB；
- telemetry：`g2_bc_eef_causal_commands_20260902_164309.jsonl`，SHA256
  `e33cf418250de0634e3a2f9454d9ff7b2b68e395bc3116744ceb93fdbcedd5a8`，127MB；
- FK报告：`g2_bc_eef_causal_commands_20260902_164309.analysis_v2.json`，SHA256
  `ba635658c5bbc24a0c3b36b2da10226256a955c2ec453a445d29cfe2b2feff4e`。

states路径重参数化后的运动时长为`90.223626s`，加1秒settle后共9123 tick、总时长91.22秒。
telemetry包含9123条连续sample和start/artifact/summary事件，sequence 0–9126连续，最终状态
`completed`，drop=0；理想EEF输入削顶0、workspace触发0、target limit 0。并行独立计算的Pink-only
与OTG分支上游Pink reference最大差严格为0，证明三stage确实使用相同上游输入而非两次不同实验。

三个冻结stage的离散动态如下：

| stage | 最大速度 rad/s | 最大加速度 rad/s² | 最大jerk rad/s³ | 小幅反转数 |
|---|---:|---:|---:|---:|
| Q-direct | 0.28000 | 14.66097 | 1466.09698 | 127 |
| Q-pink | 0.27803 | 1.82632 | 116.56847 | 55 |
| Q-otg | 0.27759 | 1.19235 | 20.00000 | 46 |

这说明单纯速度retiming没有让`Q-direct`动态连续：它仍是分段线性30→100Hz轨迹，在原waypoint
速度切换处保留很大的加速度和jerk尖峰。Pink的150ms Cartesian slew显著减少尖峰和反转；Ruckig
进一步把jerk严格压到20rad/s³。Ruckig左右臂合计约`5182/18246=28.4%`的tick达到至少99% jerk
利用率，说明该约束在本轨迹上持续参与，而不是闲置滤波层。

另一方面，平滑层没有严格保持`Q-direct`的时间对齐EEF轨迹。以同一timestamp比较FK：

| stage | 左/右位置P95 | 左/右位置最大 | 左/右姿态P95 | 左/右姿态最大 |
|---|---|---|---|---|
| Q-pink | 2.184/1.413mm | 2.283/1.440mm | 0.00779/0.01208rad | 0.00846/0.01267rad |
| Q-otg | 4.520/2.571mm | 4.687/3.127mm | 0.00995/0.01225rad | 0.01229/0.01401rad |

其中一部分是合理的时间lag，但1秒settle后仍存在终点偏差：Q-pink左/右位置
`1.521/1.440mm`、姿态`0.00750/0.01267rad`；Q-otg为`3.364/2.435mm`、
`0.01041/0.01225rad`。最终`max|Q-pink-Q-otg|=0.009956rad`，即两者速度均已归零但位置lag没有
消失。这与当前Ruckig层只跟踪Pink velocity、在reference归零后停在现有位置的实现一致。它证明
当前OTG是动态连续层，但不是位置收敛或严格路径保持层；不能再用“settle后一定追上Pink”描述它。

该偏差仍未证明OTG会导致肉眼抖动。相反，动态指标显示它显著减少jerk和小反转；需要同一NPZ的
Powered command/feedback和现场视频，才能判断几毫米时间/终点偏差与更低jerk之间的真实机械权衡。
旧的91.044秒actions结果继续只作为历史证据，不能替代本轮states产物。

#### 下一步唯一顺序

1. ~~在G2本地生成states/velocity-limited三stage并回收日志；~~ 已完成；
2. ~~对同一NPZ完成direct/pink/otg zero-output静止门、动态和FK审计；~~ 已完成，三者最后250ms
   速度均为0或数值零；
3. 当前已到Powered门并停止。经明确授权、审核Home和物理急停就位后，先在normal Servo上
   做arms-only `Q-direct`，回Home后再做同NPZ、同channel的`Q-otg`；现场视频、dispatch和feedback
   缺一不可；
4. `Q-pink`只在direct/OTG差异需要拆解时加入。normal/low-latency必须另开一组配对，不能在
   direct/OTG两轮之间切换；
5. 只有机械臂因果链定位完成后，才依次加入固定O10 hold、记录O10轨迹，最后回到live F710。

这一顺序能回答：数据本身是否抖、Pink是否引入新的关节结构、OTG是否改善或制造lag、Servo通道及
机器人闭环是否放大同一命令。它仍不会单凭一次主观视频把原因归给IK、网络、SDK或硬件。

### 14.6 Q-direct第一次Powered观察（部分运行，不能冒充完整A/B）

2026-09-02 17:21第一次启动在发送前被安全门拒绝：G2报告`mode=5,error_code=2`。该轮没有进入
command loop，feedback保持partial；这属于预期的安全拒绝。状态恢复后17:22运行同一冻结NPZ的
`Q-direct`，固定normal碰撞保护、arms-only、100Hz、20Hz同owner feedback；O10线程、腰、底盘、
Pink/OTG在线计算、网络输入和模型推理均不存在。实测起点误差只有`1.32e-6rad`。

操作者现场评价为“非常丝滑”。这条主观证据有明确隔离意义：G2硬件、normal
`joint_servo_control`和应用侧100Hz位置流并不会无条件地产生此前F710的明显抖动；至少在本轨迹
前25秒，机器人可以平滑执行包含双臂较大协同运动的冻结命令。因此后续重点应继续比较命令生成层、
真实dispatch及O10并发，而不是预设SDK或机器人硬件必然有问题。

但该轮在artifact time `25.36s`收到操作者Ctrl-C，只发送`2537/9123`次，coverage `27.8%`，最终
`runtime_summary.status=operator_ctrl_c`；没有`frozen_arm_stage_summary`，不得登记为完整通过。
已完成前缀的客观数据为：

- dispatch interval P50/P95/P99/max：`9.998/10.371/12.057/13.538ms`；26次超过12ms，0次超过15ms；
- pacer lateness P50/P95/P99/max：`0.00010/0.346/2.138/3.529ms`；
- controller调用耗时P50/P95/P99/max：`0.476/1.393/2.407/5.027ms`；其中原生arm send
  P50/P95/max为`0.232/0.732/2.710ms`；
- 主telemetry sequence 0–2540连续、drop=0；dispatch为有限14D，hand send count严格为0；
- 20Hz feedback有效样本512条、实际`20.033Hz`、drop=0，读取耗时P50/P95/P99/max为
  `0.233/0.935/1.881/3.502ms`，没有触发自动关闭；
- 反馈相对当前command看似有约2mrad平均误差，主要是时间延迟。对固定lag扫描在约70ms取得最小值：
  全元素MAE `0.103mrad`、P95 `0.410mrad`、最大`1.647mrad`。该值是离线时间对齐估计，不是厂家
  保证的精确闭环延迟，但说明执行精度很高且“当前命令误差”不能忽略反馈年龄直接解释。

当前结论是“Q-direct前25秒主观和客观均为强正证据”，不是“完整Q-direct已通过”。严格下一步仍是
让同一`direct`入口自然运行完91.22秒并得到`status=completed`和1秒settle。随后操作者指出后续
双臂路径需要对应手型，否则会发生场景碰撞；因此该arms-only完整运行计划已被14.7的双手同步版本
取代，不得为了实验纯度忽略真实安全约束。

### 14.7 场景碰撞修正：states双臂 + actions双手的v2冻结产物

操作者在25.36秒主动停止Q-direct，是因为继续执行双臂而不改变手型会碰撞，不是因为抖动或时序
故障。该停止是正确安全决策，也说明首轮arms-only假设只适合空场景，不能覆盖这条完整sorting轨迹。

不能直接启用v1 NPZ中的`hands`：v1沿用了所选`states`完整34D，而本episode手部
`max|action-state|=0.921rad`。states是数据集记录的手反馈，不等于当时的O10命令。当前v2合同改为：

- 双臂空间路径仍来自`states[:14]`，保持刚才丝滑Q-direct的定义；
- 双手命令严格来自`actions[14:]`，在同一velocity-limited时间轴采样；
- metadata必须同时包含`arm_path_source=states`和`hand_command_source=actions`；旧v1产物被
  Powered recorded-hands入口硬拒绝；
- 双臂仍为normal Servo 100Hz；O10使用已有Split `move_ee_pos`、v1_1、10Hz；无腰和底盘输出。

新zero-output产物为`g2_bc_eef_causal_commands_20260902_173215.npz`，SHA256
`31537fe91ef96f1a9931bd4d33cedf51f344a757b2a21810ee67978965c54984`。双臂三stage与v1数值不变；
O10 actions在10Hz观察到的原始最大步幅约`0.354rad`。用生产v1_1离线复现后，实际最大步幅被限制为
`0.175rad`，最大瞬时tracking lag约`0.183rad`，末尾误差0；速度/加速度限幅确实参与。数据集原始
actions+O10曾用raw 5Hz完成过真机回放，本轮10Hz v1_1比该历史接口更保守，但仍必须现场急停监护。

`21_g2_frozen_arm_stage_replay.py`新增显式`--hand-mode recorded-actions`；只有v2/action metadata才允许
创建O10线程。有效手反馈时执行0.08rad起点门；已知全零/近零无效反馈沿用审核过的训练frame-0命令
锚点语义。唯一人工入口`23_g2_frozen_arm_stage_powered.sh`现已固定上述v2 SHA、normal通道和双手模式，
远端编译、Shell语法、zero-output direct预检通过；尚未执行这版Powered。

### 14.8 Q-direct加入O10后的抖动复现

17:37的v2 Q-direct + recorded O10运行到artifact time 66.56秒后，操作者因明显抖动Ctrl-C停止。
双臂前25.36秒command与17:22 arms-only逐值完全相同，normal Servo、100Hz、数据路径和20Hz
feedback也相同；唯一新增控制因素是Split O10线程及手指动作。因此“O10路径是本次抖动复现的
主要触发因素”已有配对证据，但还不能缩写成“10Hz太低”或“改5Hz就会好”。

前25.36秒同命令比较：arms-only与arms+O10的dispatch P95为`10.371/10.556ms`、P99为
`12.057/12.536ms`、最大值`13.538/13.731ms`，超过12ms为`26/49`，两者均无超过15ms。反馈
最佳固定lag约由68ms增至100ms；lag对齐后的MAE/P95/max由`0.103/0.410/1.647mrad`增至
`0.349/1.516/11.931mrad`。这是离线固定lag估计，但主观抖动和闭环误差恶化方向一致。

完整66.56秒前缀发送6657条arm和672条hand command，主/feedback drop均为0；arm约99.03Hz，
O10为9.99998Hz，O10 interval P95/max为100.43/102.48ms。O10 hand delta P95/max为
0.0738/0.1750rad，v1_1发生56次速度、106次加速度和20次delta cap，raw-target最大lag 0.302rad。
arm dispatch只中度恶化，长周期也没有明显集中到单一10Hz相位，所以现有数据不支持仅调5/10Hz。

仍需分离：同一`Robot`对象的Servo/`move_ee_pos`并发、手指实际动作的机械反作用、v1_1追赶稀疏
阶跃，以及手型改变接触负载。下一轮固定双臂Home 100Hz和O10 Home 10Hz，所有目标静止20秒；
若仍抖优先定位接口/线程并发，若不抖则转向手部动作轨迹和接触。入口为
`scripts/24_g2_o10_hold_contention_probe.sh`。

17:51第一次静态contention probe没有进入控制循环。Powered初始化后的有效手反馈与产物frame-0
手命令最大相差`0.903682rad`，超过`0.08rad`起点门，程序在任何arm/hand发送前fault退出；主
telemetry只有1条`sequence=0` runtime summary，feedback partial也只有summary。上一轮17:37在
artifact time 66.56秒中止时的最后有效反馈与frame-0最大相差`0.973476rad`（hand index 4：
`0.989700`对`0.016224rad`），证明双手仍处在中途抓握姿态，而不是静态Home。该结果是安全闸门
按设计工作，不能通过放宽阈值绕过；先由现场人工执行审核过的全身Home并确认完成，再重跑入口24。
若Home之后仍触发同一门，则应停止并检查Home/O10反馈语义，不发送静态probe。

随后按操作者要求，测试结果不再采用人为数值门限判定PASS/FAIL，而保留连续telemetry和主观观感。
数据集中的双手大跳变是需要被真实保留和评价的输入，不再被解释为数据错误；recorded-actions模式
已删除0.08rad手部起点拒绝，只把实测起点差作为telemetry记录。后续判断的是该输入经过O10/轨迹器
和Split控制链后的实际丝滑性，不以手部步幅大小本身拒绝实验。
入口24也不再要求当前姿态匹配数据集frame-0：Powered初始化后读取一次34D实测状态，将当前14D
双臂和20D双手分别冻结为100Hz/10Hz静态命令，因此无需先回Home，也不存在约0.9rad的起点追赶。
该current-pose静态模式也不再套用数据集轨迹的settle/动态效果门，只报告`ZERO-OUTPUT CHECK`；
关节物理限位、Servo/GDK故障、急停、非有限状态和O10全零/近零无效反馈拒绝仍是硬件安全互锁，
不属于实验效果门限。双臂轨迹的起点保护保持独立，不再把它与允许的手部跳变混为一谈。

18:00的current-pose静态contention probe自然完成20秒，状态`completed`：双臂常值命令发送2001
次，O10常值命令发送202次，主/feedback telemetry均零drop。双臂实测峰峰值最大仅
`1.44e-6rad`；因此同一Robot对象上的100Hz arm Servo与10Hz静态`move_ee_pos`并发本身没有造成
双臂抖动。arm dispatch P50/P95/P99/max为`10.001/10.449/12.777/15.123ms`，其中大于12ms 41次、
大于15ms 1次；即使存在这些调度长尾，静态双臂也保持稳定，不能把上一轮抖动简单归因为10Hz
线程争用。

需要单独注意：虽然20D手命令全程完全相同，手反馈在启动0.110秒出现最大`0.895976rad`跳变，
0.701秒又出现`0.878548rad`跳变；1秒后手反馈峰峰值降至`0.010455rad`，1.25秒后为
`0.003735rad`。这指向O10首次进入/反馈语义/手控制器启动瞬态之一，而不是数据集目标变化。
控制台末尾`G2 software emergency stop latched`是完成后cleanup主动关闭发送的固定日志，不是运行中
故障；PTP设备警告也没有阻止本轮完成。下一步应保持双臂静态，专门回放变化的双手目标，以区分
“动态O10命令”与“运动双臂×O10交互”，不先调整5/10Hz。

2026-09-03已准备下一单变量入口`scripts/25_g2_o10_dynamic_arm_hold_probe.sh`：启动时读取双臂
实测14D并以normal Servo 100Hz原地保持，双手则在完整91.22秒共同时间轴上以现有v1_1、10Hz
回放recorded actions。手部起点差和数据集大跳变只记录、不拒绝；无在线Pink/Ruckig、网络、模型、
腰或底盘输出。G2端对应zero-output检查显示9123 tick、双臂max step/velocity均为0；尚未Powered。

11:55入口25自然完成91.22秒。arm/O10分别发送9123/920次，主状态`completed`且feedback零drop；
arm dispatch P50/P95/max为`9.993/10.594/14.520ms`，O10实际`9.99997Hz`、interval P95/max为
`100.543/101.576ms`。双臂命令严格静止，双臂反馈最大峰峰值仅`1.91e-5rad`；操作者也确认双臂
没有卡顿。这说明动态O10单独运动并不足以让静止双臂抖，17:37的双臂恶化需要移动双臂与动态O10
共同存在，或当时场景接触/负载等额外条件。

双手自身则被现场观察为多段顿挫，而不是连续慢关。日志确认客户端命令没有0.x秒停发：第一次
闭手时v1_1输出在约8.48--9.68秒连续12个10Hz变化步，最大单步约0.175rad；但O10反馈只在
约9.154、9.753、10.355秒出现主要更新，对应约0.34、0.79、0.99rad，间隔约0.60秒。全部动态窗
中反馈更新常见间隔约0.5--0.6秒，而发送间隔始终约0.1秒。这把停顿位置定位到
`move_ee_pos`调用之后的O10执行/状态发布链；结合肉眼观感，不能只解释为反馈显示抽样，但尚不能
区分点目标反复重规划、内部队列/控制器节奏或反馈缓存。

原始双手reference本身只在10个动作窗变化，918个相邻10Hz间隔中819个严格不变；这是sorting
数据的真实手型事件结构。v1_1把输出活动窗维持在约0.1--2.8秒，但它只是逐步追赶器，不是保证
物理手指连续慢关的时间参数化轨迹。下一步优先做单次最终O10点目标与10Hz连续中间目标的配对，
双臂保持静止：若单次目标物理运动丝滑，说明反复`move_ee_pos`重规划是主要机制；若仍分段，则
继续定位O10内部执行/接口语义。此后才比较5/10Hz或替换手部轨迹器。

从2026-09-03起，现场实验入口不再要求输入`RUN ...`固定短语；显示测试范围后按一次Enter进入
既有5秒倒计时，Ctrl-C仍可退出。该交互规则适用于后续新增入口。

对应配对实验已实现为`scripts/26_g2_o10_point_target_ab.py`与人工入口
`scripts/27_g2_o10_point_target_ab.sh`。两组均固定当前双臂100Hz normal Servo，选用冻结产物
8.4--9.5秒的第一次左手闭合事件，并使用完全相同的open起点与close终点：A在第6秒只调用一次
最终close目标，之后不再刷新O10；第12秒只调用一次open目标复位；B在第18秒开始发送12条v1_1
中间目标，10Hz持续1.1秒，到达同一close终点后同样停止刷新。Controller以arms-only模式运行，
新增的`send_hands_once`明确要求没有后台hand worker，避免伪装成one-shot但实际仍周期发送。
整轮24.1秒，20Hz同owner反馈继续记录；本地zero-output与81项G2Teleop、109项机器人相关回归
通过。运行文件已逐项同步，G2端Python/Shell、3项纯mock测试和zero-output检查通过；尚未Powered。

### 14.9 O10单次终点与10Hz中间目标的同轮A/B

14:07入口27自然完成24.1秒，状态`completed`、主/feedback drop均为0。双臂常值normal Servo
发送2411次，实测约99.17Hz，反馈最大峰峰值仅`1.44e-6rad`。O10总计15次调用严格组成：1次open
准备、A的1次close终点、1次open复位、B的12次v1_1中间目标；不存在后台hand worker。B的11个
实际调用间隔为约99.77--106.07ms，命令从open连续递增到与A完全相同的close终点。

操作者同轮观察：A单次目标“非常丝滑，一下子就到达”；B恢复为此前的多段顿挫。由于起点、终点、
双臂姿态、Robot/GDK实例和场景均相同，唯一主要变量是A让O10内部自行完成一次点到点运动，而B
在运动中每约100ms重新提交一个中间目标。证据强烈支持：`move_ee_pos`动态刷新会反复影响内部
点目标执行（很可能是重规划/替换当前目标），v1_1在客户端命令层连续并不等于O10物理层连续；
10Hz刷新正是本轮顿挫的触发条件。这里仍不把未公开的固件内部机制写成已确认事实。

本轮也纠正了对`get_end_state()`的解释。A在6秒已肉眼完成闭合，但手部状态直到12.159秒、即
reset调用之后才显示接近close；12秒reset后的open直到18.206秒、即B开始调用后才显示；B结束后
日志也没有可靠显示最终close。因此当前GDK手状态呈现明显缓存/随后续O10调用刷新的语义，不能作为
连续编码器轨迹来量化物理运动时间。14.8中“反馈约0.5--0.6秒一段”的数字仍是日志事实，但不再
作为物理手指每0.5--0.6秒跳动的证据；真实顿挫由现场A/B观感确认。

直接工程含义是：O10不应仿照arm Servo持续流式提交细分位置。下一步先在双臂静止条件下，把整条
数据集手型轨迹压缩为“每个语义动作窗只在开始时提交一次最终endpoint”；若全部开合仍丝滑，再把
同一event-driven O10序列加入Q-direct移动双臂。这将直接验证能否恢复“丝滑双臂 + 丝滑双手”的
完整replay，并为F710的按钮/抓握意图采用目标事件而非10Hz位置流提供依据。

### 14.10 完整数据集双手的event-driven候选

下一单变量入口已实现为`scripts/28_g2_o10_event_replay.py`和
`scripts/29_g2_o10_event_arm_hold.sh`。双臂读取当前姿态后以arms-only normal Servo 100Hz静止
保持；双手不创建后台worker，也不运行v1_1逐步追赶，而是在完整91.22秒共同时间轴上，把actions
的每个连续变化窗压缩成一次窗末endpoint调用。0--0.1秒的数据集初始归一化只作为第1秒的open
准备调用，不算任务事件。

冻结产物在10Hz网格上识别出10个真实任务窗：`8.40--9.50`、`24.20--25.70`、
`25.80--26.70`、`37.00--38.00`、`40.90--41.30`、`45.50--45.60`、
`53.30--54.30`、`64.30--67.20`、`79.70--80.70`和`83.70--83.90s`。因此全程O10总调用
数固定为11次（1次准备+10次任务），而不是上一动态探针的920次。插值相同常值时会产生最大约
`4.44e-16rad`的浮点运算残差；切窗只消除`1e-12rad`以内的这种数值残渣，不过滤数据集的任何
物理手部变化，也不是实验效果PASS/FAIL门限。

本地Python/Shell、zero-output和无loopback回归通过后，运行文件逐项同步；G2端Python/Shell、
6项纯逻辑测试和zero-output也通过，确认产物SHA仍为`31537f...984`、任务事件10个、O10调用11次。

14:18的Powered轮在artifact time `71.24s`由操作者Ctrl-C结束，覆盖率78.1%，不是完整91.22秒
通过。停止前严格执行1次准备和event 0--7共8个任务endpoint，合计9次O10调用；event 8/9
（79.7/83.7秒）尚未执行。操作者确认已执行动作均丝滑。双臂7125条命令严格静止，反馈峰峰值最大
`1.67e-6rad`；arm dispatch P50/P95/P99/max为`10.001/10.460/12.579/14.710ms`，主/feedback
telemetry均零drop。这把A/B的单动作结论扩展到左右手开合和不同幅度的8个数据集动作窗，但不能
把未执行的最后两个窗登记为真机证据。

### 14.11 Q-direct双臂与event-driven O10的合并候选

下一轮不再重复静态双臂剩余两窗，而直接测试真正需要的组合：`scripts/28_g2_o10_event_replay.py`
新增`--arm-mode q-direct`，人工入口为`scripts/30_g2_qdirect_o10_event_replay.sh`。启动后先用
2秒准备段保持Q-direct首点，并只调用一次open目标；之后执行完整91.22秒Q-direct 100Hz normal
Servo，同时沿用完全相同的10个O10 endpoint事件。全程仍无后台hand worker、10Hz手部刷新、在线
Pink/Ruckig、网络、模型、腰或底盘输出。

Q-direct起点仍保留独立的手臂轨迹连续性保护；它不检查或拒绝数据集手部跳变。候选总时长93.22秒，
本地zero-output确认10个事件/11次O10调用，无loopback回归为`83 passed, 2 skipped`。逐文件同步后，
G2端Python/Shell、7项纯逻辑测试和zero-output复核通过，未导入GDK或发送机器人命令；尚未Powered。
若本轮双臂和双手同时丝滑，则当前replay层的核心问题已基本收敛，可进入F710实时命令层
改造；若双手丝滑但移动双臂重新抖，则剩余变量将收缩到“运动双臂与O10动作的耦合/场景接触”，
而不是重新怀疑单独的Q-direct或单独的O10点目标。

14:34首次尝试在Controller构造阶段因`mode=5,error_code=2`退出，没有发送任何arm/O10命令；恢复
Servo健康状态后的14:34:48轮成功进入控制。该轮在总时间27.33秒、artifact time 25.33秒由操作者
Ctrl-C停止，状态`operator_ctrl_c`、覆盖率29.3%，不是完整组合通过。已执行单次open准备、event 0
左手闭合和event 1右手闭合共3次O10调用，操作者确认整轮“非常丝滑”。

移动段2534条Q-direct命令与17:22 arms-only基准相同前缀逐值完全一致。arm dispatch
P50/P95/P99/max为`10.001/10.461/12.500/15.108ms`，主/feedback均零drop；约74ms固定lag对齐后
MAE/P95/max为`0.095/0.372/4.061mrad`。arms-only基准同类值约为
`0.094/0.376/1.449mrad`，而旧10Hz重复O10组合为`0.349/1.516/11.931mrad`。因此在已覆盖前缀内，
event-driven O10不仅主观恢复丝滑，双臂反馈的MAE/P95也恢复到arms-only量级。这反驳“移动双臂
与O10天然冲突”，并把主要问题进一步锁定为运动中反复更新O10点目标；最大单点误差仍高于基准，
且后续8个事件/其余66秒未覆盖，不能据此外推完整轨迹。

### 14.12 保留数采手部时长的raw 5Hz组合候选

14.11的event endpoint实验证明执行可丝滑，但它在动作窗起点直接提交窗末目标，O10按内部默认速度
快速完成，因此没有保留数采中约1--3秒逐渐开合的时间语义。单次目标不是单控制帧瞬移，但当前
GDK 2.6.3公开`move_ee_pos`合同和本项目实际请求只有20个position，没有已验证的速度或完成时长
字段；不得猜测未记录参数来减速。

为回答“原始慢速轨迹是否能在较低重规划频率下保持丝滑”，新增人工入口
`scripts/31_g2_qdirect_o10_raw5_replay.sh`。双臂仍按相同91.22秒Q-direct以100Hz normal Servo
执行；双手从同一冻结actions时间轴按5Hz采样，关闭v1_1，并只对数值实际变化的样本调用一次
`move_ee_pos`，静止样本不周期重发。zero-output得到58个变化样本，加一次初始目标共59次调用，
保留动作窗的渐进时长和大跳变。这不是event-driven最终方案，而是时间语义与重提频率的诊断点：
它可能像数采时慢慢关闭，也可能重新暴露分段顿挫，需由真机观感判断。

本地Python/Shell与zero-output通过，无loopback回归为`84 passed, 2 skipped`。入口30现显式固定
`event-endpoint`，所以新增模式不会改变已验证行为。逐文件同步后，G2端Python/Shell、8项纯逻辑
测试和zero-output复核通过，确认58个变化样本/59次总调用；未导入GDK或发送命令。候选尚未Powered。

14:58入口31自然完成，总Powered时间93.22秒：arm发送9323次、O10调用59次，主/feedback telemetry
均零drop，feedback共1880条。arm dispatch P50/P95/max为`9.999/10.567/15.194ms`；按约73ms lag
对齐后的双臂反馈MAE/P95/max为`0.128/0.468/9.966mrad`。操作者观察双手为“分段丝滑，没什么
问题”，说明raw 5Hz变化目标的物理效果已可接受，但它仍有分段时间语义，不能写成连续匀速手指轨迹。

这里必须纠正一次时间轴命名：入口31使用的是先前为双臂限速生成的91.22秒
`velocity-limited`产物，而原始948帧、30Hz数采只覆盖`947/30=31.57s`。因此本轮是
“限速时间轴上的raw 5Hz”，不是用户要求的原始正常速度；加2秒起点准备后才会达到93.22秒。
此前把它称为正常速度是错误的。

### 14.13 GDK接口复核与原始30Hz时间轴候选

重新完整核对GDK 2.6.3官方Robot文档后，`move_ee_pos(group=dual_tool,
target_type=o10_t2)`明确支持左右O10各10D、共20个position，所以Split O10路径不是从一开始就
选错接口。它的公开请求只表达点目标，没有速度或完成时长；因此它适合离散开/合或目标事件，但
反复提交一串中间点能否形成连续轨迹并没有合同保证，而真机A/B已经证明本机上会产生顿挫。

官方同时明确`joint_servo_control`以100Hz运行，并给出“一侧7D机械臂 + 从`get_end_state()`动态
读取的同侧末端名称”在同一个Servo请求中同时控制的示例。这个语义更接近连续模型/数采手轨迹，
但它不等价于本项目曾失败的单请求双臂+双手34D：后者使用相同官方O10名称时出现约22080条
`Param ... not found`和一次2.53秒同步阻塞。故当前不能直接把正式链路切回unified 34D；若原始
速度回放后还需追求连续手指时长，下一项应是严格仿照官方示例的单侧17D、动态名称独立探针，先
回答当前G2/GDK实例是否接受末端Servo，再决定架构，不把该变量混入本轮全身replay。

为修正时间轴，新增`scripts/32_g2_source_speed_qdirect_o10_raw5.sh`：直接读取
`training_replay_v1.json`（SHA256 `28233ac...f3d`），把原始30Hz的14D measured states线性插值到
100Hz normal Servo；20D actions沿同一原始时钟取raw 5Hz变化样本，不运行v1_1。回放31.57秒，
末端保持1秒，另加2秒起点准备，总Powered时间34.57秒；共3258个回放tick、47个变化手目标加1次
初始目标。zero-output测得arm最大单步`0.020250560rad`、最大离散速度`2.025056rad/s`。本地
Python/Shell、zero-output及无loopback回归为`85 passed, 2 skipped`。
运行文件和文档随后已逐文件同步；G2端Python/Shell、9项纯逻辑测试与zero-output复核通过，确认
源文件SHA、32.57秒回放信号、47个变化样本/48次O10调用一致。远端检查未使用`--execute`，没有
导入GDK、初始化Robot或发送机器人命令；该正常速度候选尚未Powered。

15:13首次入口32运行约13.15秒后由操作者Ctrl-C停止，arm/O10为1315/18次，属于未完成试跑。
15:14:46第二轮自然完成34.57秒，状态`completed`：arm发送3458次、O10恰为48次，主telemetry
零drop；feedback记录702条有效样本且零drop。arm有效发送频率`98.739Hz`，发送间隔
P50/P95/max为`10.009/11.115/14.460ms`，没有超过15ms；GDK调用耗时P95/max为
`0.906/3.290ms`。现场评价“双臂和双手非常丝滑、非常可以接受”。因此正向结论已经覆盖原始
31.57秒速度、完整轨迹、移动双臂与raw 5Hz渐进双手的组合，不再需要用更多replay重复证明
`move_ee_pos`在该用法下可用。

控制台每个arm tick还打印一次`Clipped 1 ... max_delta=0.004995`。原因是arms-only诊断仍把启动时
34D实测值作为内部action，其中一个手反馈略超项目静态限位约0.005rad；`send_action`先裁剪完整
34D再仅发送前14D，所以该手值没有进入本轮Servo请求，也没有破坏运动。它是日志语义/噪声问题，
后续应在不改变命令的前提下清理，但不是继续replay或改运动算法的理由。

当前主线转回F710。优先将已验证的执行分工移植到实时遥操：双臂保持100Hz normal Servo；手柄的
手型/抓握状态只在用户意图变化时生成目标，不在每个控制tick重复调用O10。若需要渐进闭合，则由
独立低频手目标调度器在意图持续期间按约5Hz更新变化目标，目标不变时不重发；按钮释放、方向反转
或新手型选择可直接定义为新意图。先做shadow与结构化telemetry，再由操作者进行F710真机测试。

单侧17D末端Servo不作为F710前置条件。它保留为RTC连续20D手指轨迹的独立接口研究：只有当未来
模型手轨迹需要比当前raw 5Hz更连续的时间语义，或`move_ee_pos`再次成为可观测瓶颈时，才按官方
示例从单侧静态17D开始验证；不得把未验证Servo接口混入当前已经丝滑的主线。

### 14.14 F710 changed-only O10改造与真机前状态

F710正式入口已完成对应改造。`12_g2_powered_f710.py`现在以arms-only Controller维持双臂100Hz
normal Servo，不再创建旧Split 10Hz/v1_1 hand worker；同一个Robot owner在双臂调用完成后，
用`ChangedOnlyHandScheduler`按5Hz槽推进手型，并通过`send_hands_once`提交变化后的20D目标。
调度器保证相邻O10采样不短于200ms，不追赶漏掉的slot；到达目标后即使Windows继续发送同一个
`grip_target`也不会再调用。新意图会立即登记`hand_intent_id`，并在下一个合法5Hz槽执行，不以
突破5Hz间距换取响应速度。

首次RT明确目标之前，仅选择侧并按RB不会把当前实测手型投影到synergy，也不发送O10。A键已改为
按住期间手臂Cartesian目标速度2倍，不再产生thumb clearance意图。RB松开、断联或watchdog时
冻结当前手命令，重新接管后才继续渐进。
结构化telemetry新增intent ID、变化标记、调度原因、是否采样/实际发送，以及每次
`move_ee_pos`调用起止与耗时。

本地G2Teleop无loopback回归更新为`90 passed, 2 skipped`，机器人client完整文件为76项通过。
运行代码与测试已逐文件同步到G2；远端Python/Shell检查和19项F710纯逻辑测试通过，files-only
preflight显示新鲜O10 fallback可用，并明确未导入/初始化GDK。2026-09-03的首次direct Pink
Powered验收由操作者评价为“非常丝滑”：64.79秒内6410个dispatch样本，6016个enabled样本，
无runtime fault、watchdog、drop或sequence gap；dispatch间隔P50/P95/P99/max为
10.000/10.513/12.771/14.921ms。51次O10调用只出现在变化目标上，稳定手型没有周期重发。

随后实现下一版交互：十字键选侧必须发生在RB之前且十字键先释放；RB期间或与RB同帧的选侧边沿
被消费并拒绝，不再延迟生效。A采用按键上升沿在低速4cm/s、10deg/s与高速8cm/s、20deg/s
之间持久切换，启动与重连默认低速；加速度斜坡、0.35rad/s关节速度上限、奇异位形连续降速和
20cm接管包络保持不变。
Pink输入验证上限与常速已显式分离，避免2倍目标被旧4cm/s上限误判为HOLD。该版本地全量回归为
`91 passed, 6 skipped`。随后将RT从手臂RB deadman中拆开：选侧后RT可在RB松开时独立切换该侧
抓握目标；协议允许`deadman=false`携带离散`grip_target`但仍禁止任何Cartesian、连续手速度、
thumb或加速输出；G2以独立`hand_enabled`和packet freshness驱动5Hz changed-only O10，双臂保持
HOLD，watchdog仍冻结未完成的手部渐进。完整回归更新为`93 passed, 6 skipped`。以上新交互仍待
下一次Powered验收。Y现定义为与既有复位相同的双臂+双手全局Home，仅在RB松开时接受。

17:45首次Y测试证明Windows映射、协议和G2事件识别均成功：telemetry记录
`global_home_requested`并正常封口。但第一版随后关闭F710 owner并另起`03_reset_home.sh`，新
Controller在10秒稳定等待后读到`mode=5,error_code=2`，因此在零Home命令下安全退出；末尾密码
提示只是失败后的telemetry上传。该结果复现历史上跨进程重新初始化偶发的error 2，说明失败来自
owner交接而不是Y按键。

修正版取消第二个进程和low-latency重初始化。当前唯一normal Servo owner内部执行相同目标和顺序：
从触发时实测14D开始，双臂100Hz线性5秒回H5初始姿态；双手保持不动，随后从最后成功20D锚点
开始，按5Hz、不追赶漏帧、最多8个变化点回批准Home。完成后清除旧RT目标、恢复低速并要求重新
选侧，receiver和GDK全程不重启。新增纯`GlobalHomePlan`测试；完整本地回归为
`98 passed, 6 skipped`，修正版Y仍待Powered复测。

### 14.15 同owner Y验收与“像断链一样停住”的日志结论

`g2_powered_f710_20260903_175718.jsonl`中，同一个normal Servo owner连续完成3次
`global_home_requested -> global_home_completed`，之后均可重新选侧并takeover；整轮正常封口，
证明修正版Y全局Home已经通过Powered验收，不再依赖跨进程Controller交接。

随后长测`g2_powered_f710_20260903_181416.jsonl`共记录约212秒、21200条事件。该轮没有
`watchdog_hold`、runtime fault或fresh=false；RB有效期间19700个样本全部保持fresh，包龄最大
179.81ms，低于200ms watchdog。因此操作者观察到的“像断链、松开再按RB后恢复”，在该轮不能
归因于网络断链。日志明确记录483个workspace saturation tick（约4.83秒）：271个为相对接管
平移包络，212个为相对接管旋转包络。当前Pink目标以每次RB takeover为原点，平移包络为20cm，
旋转包络为20度；到边界时同方向命令保持最后安全目标，反向摇杆可立即退出。松开再按RB会以当前
实测位姿重新建立包络，所以表现为“重新按RB才恢复”。这属于有限相对工作空间/离合器语义，不是
关节机械限位，也不是IK、Servo或链路故障。

更早的`g2_powered_f710_20260903_180914.jsonl`确有一次真实watchdog：包龄205.72ms越过200ms，
说明传输暂停仍偶尔可能发生，但它与最新长测中的主要现象是两个独立机制。下一步应先改进包络边界
交互与telemetry提示，再单独统计Windows sender/tunnel的真实watchdog；不要通过盲目增大机械范围
来混合处理两类问题。

### 14.16 rolling envelope修复候选

针对14.15的483个假断链样本，`PinkRelativeArmIK`新增无目标跳变的envelope rebase：目标即将越过
单段20cm/20度边界时，先把最后已接受的EEF target设为新段锚点、把当前IK reference设为新的
posture参考，再在同一个10ms tick重试原输入。EEF target、关节command和RB状态均不中断，因此
操作者不再需要松开并重按RB。它没有增大输入速度、关节速度/步幅、模型关节限位或奇异位形阈值；
真实网络watchdog仍会立即HOLD，并在新鲜输入恢复后从实测状态自动takeover。

telemetry新增`target_envelope_recentered/reason/count`，每次滚动还产生
`target_envelope_recentered`控制事件。允许本机loopback后的完整离线回归为
`99 passed, 6 skipped`；G2端18项相关纯逻辑测试、Python编译和files-only preflight均通过，后者
明确未导入或初始化GDK。实现与文档SHA一致地同步到G2，待Powered长距离平移/旋转验收。

### 14.17 rolling envelope Powered验收与F710 v1冻结

`g2_powered_f710_20260904_132248.jsonl`由操作者Ctrl-C正常封口。12687个dispatch样本中11380个
enabled；自动recenter共18次（平移6、旋转12），旧workspace saturation、watchdog、IK HOLD、
runtime fault、fresh=false和telemetry drop均为0。包龄最大161.88ms；dispatch间隔P50/P95/max为
`10.000/10.872/17.686ms`，GDK发送耗时P95/max为`1.483/6.340ms`。操作者确认修复效果很好。

当前控制基线正式冻结为`f710-powered-v1-20260904`，机器可读清单位于
`configs/f710_powered_v1_baseline.json`，固定Windows/G2 runtime、控制参数、批准Home配置及本轮
主/feedback日志SHA。冻结含义是后续数据采集开发不得静默改变控制语义；任何改变先成为新候选，
重新跑离线回归和Powered验收后才能替换基线。可回滚归档为
`baselines/f710-powered-v1-20260904.tar.gz`，SHA256为
`3cbcf313e82e958ff6580362d6fdf5462d682305c70f87dcd46c26807e825226`。下一主线转为纯人工F710
episode recorder。

### 14.18 START锁存式HIL基础框架（未Powered）

在冻结v1之后建立下一候选，不修改归档。`hil_supervisor.py`实现纯
`POLICY/HIL_LATCHED/HANDOFF_HOLD/SAFE_HOLD`状态机：只有START toggle改变所有权，RB在POLICY中
没有快速入口；HIL内RB仅为机械臂deadman，松开仍留在HIL。退出要求RB松开且手动作完成，随后必须
等待新`policy_epoch`的fresh chunk；transport断联不自动恢复POLICY。

`hil_toggle_id`为单调持久状态而非易丢的单帧event；新session只锚定首值，回退/跳号进入
SAFE_HOLD。`hil_arbiter.py`在纯34D层保证POLICY只能选policy action，人工活动只能选human action，
其余HIL暂停/交接/故障状态只能选hold，禁止旧suffix重新进入命令链。现有Powered telemetry先加入
兼容占位字段；未来HIL runtime将记录policy/human/selected三路34D及分离的latched/arm/hand masks。

Windows reader中预期START为XInput button 7，但明确标记为未现场验证；只读探测器已补充A/B/X/Y/
START显示。当前代码不得据此直接进行Powered HIL切换；当时完整离线回归为`113 passed, 6 skipped`。

### 14.19 HIL recorder与MockPolicy因果闭环（未接相机、未Powered）

新增`hil_episode.py`异步记录四条独立证据流：30Hz逻辑观测、原始50x34 policy chunk、100Hz
policy/human/selected/executed/measured控制向量和稀疏状态事件。完整episode仅在正常关闭、零queue
drop且writer无错误时从`.partial`原子发布；审计逐tick验证所有权对应的候选恰好等于selected，
且selected恰好等于executed。`33_hil_mock_episode.py`在不导入GDK的情况下完整走过START接管、
RB人工运动、HIL暂停、handoff HOLD和fresh epoch恢复；60 tick结果为policy 32、human_arm 19、
human_hold 6、hold 3，结构审计通过。

该结果只证明状态机、arbiter和记录因果链能够拼成闭环，不证明真机切换或数据可训练。当前相机
adapter没有向上保留三路GDK消息各自的设备时间戳，journal也尚未保存图像payload，因此manifest
强制写入`training_ready=false`及两个明确blocker。下一步先补时间戳保持的视频录制sink和DM0.5
兼容转换/审计，再做button 7只读验证，最后才申请一次Powered HIL切换测试。
加入recorder与独立审计入口后的完整回归为`116 passed, 6 skipped`。
上述文件已同步到G2；G2端只运行了Python编译、13项HIL纯逻辑测试和Mock episode审计，结果全部
通过。该远端命令未导入GDK、未读取相机、未创建Robot/Servo，也没有真机输出。

### 14.20 推理与录制架构决策（2026-09-04，待实现）

不维护“纯内存推理”和“可录制推理”两套控制架构。两套代码会使观测时序、预处理、policy
request ID和最终34D下发语义逐渐分叉，导致普通推理测试无法代表DAgger采集时的真实行为。正式
方案是一条共享主链加可插拔旁路sink：

```text
GDK三路相机消息 + measured 34D
            -> TimestampedObservation
                 |-> 同一Policy推理/控制主链
                 |-> NullSink（只推理）
                 |-> RingBufferSink（内存预录）
                 `-> EpisodeVideoSink（异步落盘）
```

冻结三个运行模式：

1. `inference-only`：不永久写三路图像，只保留当前运行所需内存对象和轻量telemetry；
2. `hil-record`：每个有效模型观测都保存三路原始压缩图像、各自原始时间戳和同一时刻的measured
   34D，并保存policy、人类候选、selected、executed及intervention mask；
3. `prebuffer-record`：默认推荐。平时在内存环形缓冲保留最近约3--5秒压缩帧与状态；START进入
   HIL时先提交介入前上下文，再继续写到episode结束，从而保留模型开始犯错的原因。

三种模式必须复用完全相同的`TimestampedObservation`和推理调度。sink只能异步消费已经产生的
观测，不能重新采一次相机、改变模型输入或阻塞100Hz控制。磁盘、编码或队列故障应记录drop并把
该episode判为不可训练，但不能拖慢或改变机器人命令。

记录按语义分频，不能把100Hz tick伪装成30Hz训练帧：三路相机与measured state按有效模型观测
约30Hz保存；完整50x34 chunk按每次推理保存；policy/human/selected/executed/measured控制证据按
100Hz保存；START、RB、RT、换侧、handoff和故障使用稀疏事件流。录制开关控制是否持久化，不控制
推理数据合同。

下一代码顺序冻结为：先定义携带三路原始设备时间戳与压缩payload的观测接口；再实现`NullSink`、
有界`RingBufferSink`和异步`EpisodeVideoSink`；最后把当前journal的图像引用接到真实视频帧并扩展
审计。完成图像payload、时间戳文件与视频帧严格映射前，继续保持`training_ready=false`。

## 15. 相关文档与代码入口

- [Windows/G2 唯一入口](START_HERE_WINDOWS_G2.md)
- [F710 DAgger 全流程](../G2_F710_DAGGER_FULL_FLOW.md)
- [G2 运动学与 Pink IK 计划](G2_KINEMATICS_AND_PINK_IK_PLAN.md)
- [Windows 到 G2 Shadow 手册](WINDOWS_TO_G2_SHADOW_RUNBOOK.md)
- [2026-08-27 底盘误动作事故记录](INCIDENT_20260827_F710_BASE_MOTION.md)
- [`powered_teleop.py`](../../../G2/teleop/src/g2teleop/powered_teleop.py)
- [`12_g2_powered_f710.py`](../../../G2/teleop/scripts/12_g2_powered_f710.py)
- [`15_replay_ruckig_otg.py`](../../../G2/teleop/scripts/15_replay_ruckig_otg.py)
- [`16_install_ruckig_g2.sh`](../../../G2/teleop/scripts/16_install_ruckig_g2.sh)
- [`17_g2_ruckig_otg_shadow.sh`](../../../G2/teleop/scripts/17_g2_ruckig_otg_shadow.sh)
- [`18_g2_powered_f710_timing_ab.sh`](../../../G2/teleop/scripts/18_g2_powered_f710_timing_ab.sh)
- [`19_analyze_f710_timing_ab.py`](../../../G2/teleop/scripts/19_analyze_f710_timing_ab.py)
- [`20_g2_bc_eef_chain_replay.sh`](../../../G2/teleop/scripts/20_g2_bc_eef_chain_replay.sh)
- [`21_g2_frozen_arm_stage_replay.py`](../../../G2/teleop/scripts/21_g2_frozen_arm_stage_replay.py)
- [`22_analyze_causal_command_artifact.py`](../../../G2/teleop/scripts/22_analyze_causal_command_artifact.py)
- [`23_g2_frozen_arm_stage_powered.sh`](../../../G2/teleop/scripts/23_g2_frozen_arm_stage_powered.sh)
- [`24_g2_o10_hold_contention_probe.sh`](../../../G2/teleop/scripts/24_g2_o10_hold_contention_probe.sh)
- [`25_g2_o10_dynamic_arm_hold_probe.sh`](../../../G2/teleop/scripts/25_g2_o10_dynamic_arm_hold_probe.sh)
- [`26_g2_o10_point_target_ab.py`](../../../G2/teleop/scripts/26_g2_o10_point_target_ab.py)
- [`27_g2_o10_point_target_ab.sh`](../../../G2/teleop/scripts/27_g2_o10_point_target_ab.sh)
- [`28_g2_o10_event_replay.py`](../../../G2/teleop/scripts/28_g2_o10_event_replay.py)
- [`29_g2_o10_event_arm_hold.sh`](../../../G2/teleop/scripts/29_g2_o10_event_arm_hold.sh)
- [`30_g2_qdirect_o10_event_replay.sh`](../../../G2/teleop/scripts/30_g2_qdirect_o10_event_replay.sh)
- [`31_g2_qdirect_o10_raw5_replay.sh`](../../../G2/teleop/scripts/31_g2_qdirect_o10_raw5_replay.sh)
- [`32_g2_source_speed_qdirect_o10_raw5.sh`](../../../G2/teleop/scripts/32_g2_source_speed_qdirect_o10_raw5.sh)
- [`33_hil_mock_episode.py`](../../../G2/teleop/scripts/33_hil_mock_episode.py)
- [`34_audit_hil_episode.py`](../../../G2/teleop/scripts/34_audit_hil_episode.py)
- [`eef_replay.py`](../../../G2/teleop/src/g2teleop/eef_replay.py)
- [`control_timing.py`](../../../G2/teleop/src/g2teleop/control_timing.py)
- [`joint_otg.py`](../../../G2/teleop/src/g2teleop/joint_otg.py)
- [GDK 2.6.3 相关接口](../../../../g2_robot/docs/GDK_2.6.3_RELEVANT_API.md)
- [关节命令/反馈诊断](../../../../DM0.5/docs/robot/JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md)
