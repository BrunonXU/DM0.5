---
doc_type: principle
domain: g2_control
status: canonical
created: 2026-08-28
updated: 2026-08-28
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [joint_command_feedback_diagnostics, motion_quality_figures]
supersedes: []
superseded_by: []
related: [ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md, RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md, RTC_PROCESS_ISOLATION.md, experiments/SYNC_ARM_MICRO_TREMOR_ABLATION_20260827.md, experiments/SHARED_HAND_TRAJECTORY_OFFLINE_ABLATION_20260827.md]
---

# 关节命令—硬件反馈诊断合同与科研图

## 1. 问题与本轮范围

当前操作者观察到：双臂改善后，灵巧手在本不需要明显开合时仍持续小幅上下晃动。仅凭肉眼
很难判断它来自模型、平滑器、发送时序、GDK闭环还是机构本身；仅比较每轮最大jerk也容易
在解决一个峰值后暴露另一个问题。

本轮明确不增加人工`HAND_HOLD/HAND_ACTIVE`动作意图标签，也不先修改模型训练。诊断只补齐
两个客观缺口：

1. Sync双臂逐条100Hz实际下发命令及真实发送时间；
2. 唯一Control进程读取的34维真实硬件关节反馈及采样时间。

目标是把同一物理时间轴上的三层信号分开：

```text
模型预测 q_model(t) → 实际下发 q_command(t) → 硬件反馈 q_measured(t)
```

没有这三层，就只能证明“数学轨迹更平”，不能证明机器人为何仍在抖。

## 2. 当前已经有什么、缺什么

两轮2026-08-28 Powered Sync日志已经保存每次请求完整`50×34`模型action chunk；其中前14维
为双臂、后20维为双手。双手还保存约10Hz逐条`hand_commands_rad`和实际发送offset。

当前缺口是：

- Sync的`arm_commands_rad`和`arm_command_time_offsets_seconds`被有意留空，只能用确定性的
  weight10、C2、120ms边界融合和anchor离线重建，不能审计每一条实际发送；
- `dispatch_summary`只有发送周期与动力学汇总，没有连续34维`q_measured`；
- 推理观测时的一次state读取远低于双臂100Hz与双手10Hz控制时间基，不能作为闭环轨迹；
- 没有电流/力矩或外部视觉测量，因此编码器稳定但机构仍抖的情况暂时无法完全排除。

已有命令证据仍然重要。两轮共402个双手控制步中，66.2%的“关节×时间步”变化超过
`1e-5rad`，18.1%超过1mrad，56.6%落在`1e-5--1e-2rad`。主要PIP关节84.1%--97.0%的
控制步都在更新，各自出现约47--67次方向反转。这能证明手部没有真正进入命令保持，但不能
把所有反转都判成无意义动作，也不能证明硬件实际振幅。

## 3. 采集合同

### 3.1 统一时钟和身份

每条样本至少保存：

- `monotonic_ns`：本机单调时钟，不用wall clock计算间隔；
- `wall_time`：只供跨文件和视频粗对齐；
- `run_id/request_id/playback_id/chunk_step`；
- 34维固定joint layout版本与运行profile；
- sequence number和drop count，任何丢样必须可见。

模型waypoint的时间是30Hz逻辑时间，不得伪装成实际发送时间。下发时间取GDK调用附近的真实
单调时钟；反馈时间取读取完成时间，并同时保存读取耗时。不同采样率的曲线只在画图阶段
重采样，原始数据永不覆盖。

### 3.2 实际命令流

- 双臂保存每条14维Servo目标、调用前后时间、调用耗时和成功/失败；
- 双手继续保存每条20维`move_ee_pos`目标、发送时间和原始preview目标；
- 保存chunk边界、boundary blend窗口、Sync hold、RTC切换点和限位/投影触发；
- 日志写入不得阻塞100Hz发送线程。优先写预分配内存ring，由不拥有Robot的writer消费；
  ring溢出时计数并使本轮科研统计失效，不能静默丢数据。

### 3.3 硬件反馈流

Robot仍只能由Control进程拥有。不得为了采样新增第二个Robot reader，也不得让Policy进程
直接调用GDK；此前进程隔离结论不能倒退。

先做只读反馈速率probe，测量GDK一次34维读取的P50/P95/max和对Servo deadline的影响，再
选择真机采样率。双臂目标是尽量接近100Hz；O10反馈率以实测稳定能力为准，不能假定通信
标称1kHz等于20维位置可稳定1kHz读取。若读取会阻塞发送，宁可降低反馈率并如实记录，也
不能为画图破坏控制。

反馈至少保存34维位置和有效位；若GDK可靠提供，再附加速度、电流/力矩、错误码和控制模式。
派生速度、加速度和jerk必须在离线阶段用统一方法计算，并报告滤波/差分窗口，不能把编码器
量化噪声的三阶差分直接当机械jerk。

### 3.4 外部地面真值是后备，不是首轮必需

若`q_command`稳定、编码器`q_measured`也稳定，而高速视频中仍看见手指振动，说明内置反馈
无法观察传动间隙、末端柔性或高频振动。此时再增加同步高速视频、标记点/光学跟踪或IMU。
NIST的机器人动态测量工作同样强调独立外部测量和跨系统同步时间戳，而不是只信控制器内部
反馈。

## 4. 必画图：从总览到根因

### Figure A：34DoF活动热图（第一张看）

横轴是真实时间，纵轴按左臂7、右臂7、左手10、右手10分组。分别画三块颜色：
`|Δq_model|`、`|Δq_command|`和`|Δq_measured|`，并叠加chunk边界、推理等待和RTC切换线。

用途：一眼判断抖动是全身同步、单手成组、某类PIP，还是只在chunk尾部/边界出现。34条线
不能堆在同一坐标轴上；全局总览必须用heatmap。

### Figure B：Top-K关节三层轨迹small multiples（根因主图）

自动按反转数、累计空走、跟踪误差或频带能量选Top-K关节，每个关节单独子图，叠加：

- 模型目标`q_model`；
- 实际命令`q_command`；
- 硬件位置`q_measured`。

判读合同：

| 现象 | 首要归因 |
|---|---|
| `q_model`先反转，后两层跟随 | 模型/chunk输出包含抖动 |
| `q_model`较稳，`q_command`新增反转 | 插值、preview、投影或chunk拼接 |
| `q_command`稳定，`q_measured`振荡 | GDK闭环、增益、时延、传动或机构 |
| `q_measured`稳定但视频振动 | 编码器不可观测的柔性/间隙/高频机构问题 |

图上同时显示该窗口的净位移、累计路程、`travel/max(net, ε)`、反向次数、跟踪误差P95和
峰值频率。位置、速度、加速度、jerk不要用四个量纲共用一根纵轴。

### Figure C：命令—反馈误差图

定义`e(t)=q_measured(t)-q_command(t-delay)`。先用互相关或事件对齐估计纯延迟，再画：

- 34DoF tracking-error heatmap；
- 每关节MAE/RMSE/P95/max和稳态bias；
- 命令速度对误差的scatter/hexbin；
- 延迟估计及其随时间变化。

这张图把“目标不好”与“硬件跟不上”分开。没有延迟补偿时，正常相位滞后会被误报成大误差。

### Figure D：chunk边界对齐图

把每个chunk按边界`t=0`堆叠，画均值、中位数、P05--P95，而不是只挑最坏案例：

- 位置首跳、速度差、加速度和jerk；
- 跟踪误差；
- 反转率和累计路程；
- 从边界前200ms到后400ms的时间曲线。

如果尖峰稳定锁定`t=0`，优先查chunk衔接/RTC；如果均匀散落在chunk内部，RTC不会解决
主体。Physical Intelligence的RTC工作正是把推理延迟与跨chunk不连续作为独立变量，并用
hard/soft overlap约束消除新旧chunk策略跳变。

### Figure E：频域图，只对均匀采样的真实反馈使用

对每个问题关节画Welch PSD；若频率随动作阶段变化，再画spectrogram。进一步画命令—反馈
coherence和传递增益：

- 命令和反馈同频峰：命令驱动；
- 反馈有强峰、命令没有：闭环/机构共振候选；
- 多关节同频同相：共享调度、电源或结构模态候选；
- 只在某根手指出现：局部执行器、传动或关节增益候选。

10Hz双手命令的Nyquist频率只有5Hz，不能用它解释更高频嗡鸣；高频结论必须来自更高率的
硬件反馈、IMU或视频。

### Figure F：RTC/重规划计划分歧图

把旧chunk和新chunk对齐到同一未来物理时间，画每个关节的plan fan与
`|q_old(t)-q_new(t)|`热图，并叠加hard mask、soft mask和实际切换点。它回答“模型在相邻
观测下是否改变策略”，而不是只看最终播放结果。

RTC论文显示仅冻结必然执行的短prefix可能仍不足以保持策略一致，soft mask利用更长重叠区
增强跨chunk连续；2026年的action-chunk分析进一步把chunk收益归因之一解释为不同延迟预测
的隐式ensemble。因此，重叠计划分歧是RTC调参必须看的内部量。

### Figure G：配对A/B统计图

同一记录轨迹做候选反事实，至少报告：

- per-chunk配对散点或slope graph；
- ECDF，而不是只给平均值；
- median、P95、max与bootstrap置信区间；
- 改善比例、退化比例和最坏反例；
- endpoint偏差、方向延迟、累计路程、反向、tracking error及deadline。

真机不同rollout之间不能直接宣称因果。RTC论文在延迟实验中做单变量比较，并对成功率画
95% Wilson区间；Diffusion Policy则分别消融action horizon和人工加入的latency。我们的
参数选择也必须采用冻结链路、配对反事实、短Powered复验三段式。

### Figure H：发送时序与延迟瀑布图

画observation、请求、服务端推理、响应、控制接收、每条arm/hand command和feedback sample
的统一时间线；另画发送间隔直方图/ECDF和deadline miss raster。它用于排除“轨迹数学上
平滑，但发送时间不均匀”。

## 5. 专业研究流程，而不是继续逐个补丁

顶级机器人策略团队通常不会从一张jerk柱状图直接决定滤波器，而会按以下顺序缩小因果：

1. 冻结模型、输入、控制频率和硬件，只改变一个候选；
2. 保存完整rollout、状态、动作chunk、执行索引、时间和视频；OpenPI官方DROID示例逐控制步
   读取state、按open-loop horizon取chunk动作、维持控制频率并保存整段视频；
3. 用三层轨迹和tracking error先区分策略、调度与硬件；
4. 用chunk对齐图判断内部还是边界，用PSD/coherence判断命令还是机构频率；
5. 用同轨迹反事实筛候选，再做多次Powered配对或随机化次序的A/B；
6. 同时报告运动质量、任务结果、延迟和安全，不以“更慢”冒充“更平滑”；
7. 保留失败候选和门槛，避免下一次重新发明已经否决的deadband、强限幅或过度滤波。

ACT用真实双臂精细任务的成功率验证action chunking，而不是只优化轨迹导数；Diffusion
Policy同时研究预测/执行horizon、控制表示和人工延迟；Physical Intelligence RTC同时报告
延迟鲁棒性、任务吞吐、成功率和soft-mask消融。这些工作共同说明：科研级结论必须覆盖
“信号连续、闭环跟踪、延迟鲁棒、真实任务”四个层面。

## 6. 对当前G2的实施优先级

| 优先级 | 工作 | 产物 | 进入下一步的门 |
|---|---|---|---|
| P0 | 补Sync双臂逐条命令与真实发送时间 | 完整`q_command` | 零静默丢样，原deadline统计不退化 |
| P0 | 在唯一Control owner内做反馈速率probe | 安全采样率与读取延迟 | 不新增Robot reader，不制造Servo miss |
| P1 | 记录34维反馈sidecar | `q_measured`+timestamp+drop count | 可与命令统一对齐，采样缺口可见 |
| P1 | 生成Figure A--D、H | 根因报告 | 能明确到层、关节和chunk位置 |
| P2 | 只对均匀且足够快的反馈生成Figure E | 频率/相干性报告 | 不越过Nyquist解释高频 |
| P2 | 为RTC生成Figure F | old/new plan分歧 | d/s和soft mask有直接证据 |
| P3 | 候选反事实和Powered A/B生成Figure G | 可验收结论 | 同时通过平滑、偏差、延迟、安全门 |

在P0--P1完成前，不继续凭肉眼调hand jerk penalty，也不把手指持续漂移简单归为模型根因。

## 7. 首轮仪表盘的固定版式

一次run默认输出一张总览和一个可展开报告：

1. 第一行：34DoF command/measured活动热图与chunk边界；
2. 第二行：Top-4问题关节三层位置small multiples；
3. 第三行：tracking error heatmap与发送间隔/延迟时间线；
4. 第四行：chunk边界对齐统计；
5. 附页：每个关节指标表、PSD/coherence、最坏事件放大图和A/B配对ECDF。

默认报告双臂与双手分开尺度。任何图必须在标题或caption注明run、profile、采样率、数据层
（model/command/measured）、是否重采样、差分方法和单位。

## 8. 能证明与不能证明什么

补齐两条数据后可以证明：哪一层首次产生反向，实际命令是否按期发送，硬件跟踪误差多大，
抖动是否锁定chunk边界，以及可观测频带内是否有固定振荡。

仍不能自动证明：某段模型手指动作在任务语义上是否必要、编码器未观测到的末端柔性、接触
力或高于反馈Nyquist的机械振动。本轮接受不做动作意图标签；相应代价是算法门只能使用
运动学、跟踪和任务结果，不能把所有“小动作”自动命名为无意义。

## 9. 一手参考

- [Physical Intelligence：Real-Time Execution of Action Chunking Flow Policies](https://arxiv.org/abs/2506.07339)
- [Physical Intelligence OpenPI官方DROID rollout示例](https://github.com/Physical-Intelligence/openpi/blob/main/examples/droid/main.py)
- [Physical Intelligence OpenPI仓库](https://github.com/Physical-Intelligence/openpi)
- [Learning Fine-Grained Bimanual Manipulation with Low-Cost Hardware（ACT）](https://arxiv.org/abs/2304.13705)
- [Diffusion Policy: Visuomotor Policy Learning via Action Diffusion](https://arxiv.org/abs/2303.04137)
- [Why Does Action Chunking Improve Behavioral Cloning Performance in Robotic Control?](https://arxiv.org/abs/2608.02547)
- [NIST：Mobile Manipulator Performance Measurement Data](https://www.nist.gov/publications/mobile-manipulator-performance-measurement-data)

