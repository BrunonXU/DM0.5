# G2 饮料瓶分拣：F710 人工介入与 DAgger 全流程

2026-09-07核对补充：[当前交接与证据边界](HANDOFF_20260907_F710_HIL_RECORDING.md)。下文Mock
“闭环”指模拟字段/状态验证；尚未证明真实执行、跨流因果关联或严格30Hz观测。30Hz action合同
不等于推理请求频率；HIL暂停推理时也需要持续采集。第5.3节sink设计仍待实现。

> 同日后续：[共享观测与录制离线实现](HIL_RECORDING_OFFLINE_20260907.md)已完成。下文5.2/5.3的待实现
> 描述保留为历史设计；生产policy/相机同owner接入和Powered HIL仍待验证。

状态：**F710 Powered v1控制基线已冻结；START锁存式HIL、action arbiter和可审计journal已完成MockPolicy闭环，尚未接入真实相机/policy或Powered切换。**
禁止再次把 F710 接收器插入当前 G2。F710 已被 Linux 正确识别，
但厂商常驻 `teleop_main_node`会同时消费 joystick 并控制底盘。只读探测器无法阻止另一个
进程处理同一设备。详见 `INCIDENT_20260827_F710_BASE_MOTION.md`。  
范围：只面向 DM0.5 饮料瓶分拣。  
正式输入：Logitech F710，XInput 模式；只允许插入独立Windows笔记本。  
备用输入：iPad + HEBI Mobile I/O，不进入第一版正式路径。

## 1. 为什么使用 F710

F710 输出局部、比例式的摇杆和扳机输入，适合碰倒瓶子后的 regrasp 与瓶颈交接 correction。
与 AR 方案相比，它不需要手机相机、ARKit/WebXR、局域网发现、手机坐标漂移或相机偏移
标定。它替代的是“人工末端增量输入”，不会消除 URDF、FK/IK 和 G2 安全控制需求。

现场已确认：

```text
USB ID: 046d:c21f Logitech F710 Wireless Gamepad [XInput Mode]
event:  /dev/input/event5
js:     /dev/input/js0
stable: /dev/input/by-id/usb-Logitech_Wireless_Gamepad_F710_1BF994E3-joystick
```

正式程序应优先打开 by-id 路径，避免重启后 `js0/event5`编号变化。但设备路径稳定不代表
输入所有权独占；当前 G2 厂商服务会先于本项目自动使用该设备。

Windows现场唯一入口已冻结在`START_HERE_WINDOWS_G2.md`。使用两个不依赖ExecutionPolicy、
出错会暂停的`.cmd`，不要再临时创建venv或运行旧PowerShell隧道脚本。

## 2. G2 正式硬件接口

逻辑层继续使用 DM0.5 的冻结 34D absolute target：

```text
左臂7 + 右臂7 + 左O10 10 + 右O10 10
```

硬件层必须使用当前真机验证过的 Split 路径：

```text
双臂14D -> 100Hz -> joint_servo_control(normal)
双手20D -> 约5Hz changed-only -> move_ee_pos(group=dual_tool, target_type=o10_t2)
```

统一34D Servo只保留历史诊断用途，禁止成为遥操默认。G2 Control process 必须是唯一 GDK
`Robot()` owner；手柄 reader、IK 和 recorder 不得另建 Robot。

## 3. F710 冻结逻辑映射

轴号和符号必须通过第 6 节只读探测确认；本节先冻结“物理控件语义”。

### 3.1 选择和 deadman

| 控件 | 功能 | 互锁 |
|---|---|---|
| 十字键左 | 选择左臂+左手 | 仅 `RB`松开时允许 |
| 十字键右 | 选择右臂+右手 | 仅 `RB`松开时允许 |
| 十字键上/下 | 未分配 | 无模式切换 |
| `RB`按住 | deadman/clutch，允许产生新目标 | 必须先选侧并已松开十字键 |
| `RB`松开 | 立即 HOLD | 不自动恢复旧策略chunk |
| `START`单击 | 锁存进入/退出HIL | 预期XInput button 7，必须先完成Windows只读验证 |

初始必须是“无活动侧”；不能默认左臂或右臂。平移与旋转使用连续combined布局，不再切模式。
严格顺序是：RB松开时按十字键选侧，松开十字键，再按RB。deadman按住期间的换侧边沿直接
丢弃，不会在松开RB后延迟生效；若需要换侧，必须松开RB并重新按一次十字键。

### 3.2 连续位姿控制

| 控件 | 目标速度 |
|---|---|
| 左摇杆上下 | 末端前后 `vx` |
| 左摇杆左右 | 末端左右 `vy` |
| LB / LT | 末端向上`+vz` / 向下`-vz` |
| 右摇杆上/下 | pitch负/正 `wy`（自然视角） |
| 右摇杆左/右 | yaw正/负 `wz` |
| X / B | roll负/正 `wx` |
| A单击 | 在低速1倍/高速2倍之间切换；启动默认低速 |
| Y（RB松开） | 同一owner执行双臂+双手全局Home；完成后重新选侧 |

平移和旋转可同时输入。线速度向量和角速度向量分别归一化到单位球，保证对角输入保留方向
而不会超过当前速度档上限或触发IK HOLD。低速为4cm/s、10deg/s；高速为8cm/s、20deg/s。
A只在按下边沿切档，松开不回低速；再次按下才切回。高速保留原加速度斜坡、0.35rad/s关节
速度上限、奇异位形连续降速和workspace饱和；断联/重启恢复低速。

现场Powered验证后冻结基坐标符号：左摇杆上/下为`+X/-X`，左/右为`+Y/-Y`，右摇杆
LB/LT为`+Z/-Z`。左摇杆在基坐标XY平面移动，右摇杆连续控制pitch/yaw。
2026-08-28已修正左摇杆早期版本左右符号反向的问题；2026-09-01根据Powered真机反馈将
右摇杆pitch/yaw两个方向同时反号，取消类似飞行摇杆的反向手感。

### 3.4 O10 抓握控制

| 控件 | 功能 |
|---|---|
| `RT`单击 | 每侧独立切换目标`g`：首次单击闭手，再次单击开手；不要求RB |

当前人工层只改变历史抓握协同的`g`；thumb clearance residual能力仍保留在底层，但F710不再
为它分配按键：

```text
hand_10d = historical_grip_synergy(g), c = 0
```

- 四指 abduction 固定；
- 四指 PIP 按各自标定曲线同步；
- `c`主要作用于 thumb roll/abad；
- 左右手分别标定；
- 另一侧 O10 始终保持最后一次成功的命令空间目标；
- 双手20D通过约5Hz `dual_tool move_ee_pos`一起发送；只提交渐进过程中实际变化的目标，静止不重发。

RT使用0.65/0.35 Schmitt迟滞，只认“按下后完整释放”的新边沿。RT只要求先选好活动侧，
双臂仍由RB独立deadman；RB松开时手指可继续执行changed-only渐进目标。某侧第一次RT单击前其目标为
unknown，接管或换侧不会自动改变手型。RB松开只HOLD双臂，手部在网络新鲜时继续渐进；网络失联时HOLD最后成功手目标，避免通信故障掉瓶。
无线按钮不是安全级急停，
Powered 时仍必须由另一名操作者握物理急停。

## 4. 输入到机器人完整链路

```text
F710 XInput（只插Windows笔记本）
    │ USB receiver + pygame
    ▼
WindowsF710Reader
    ▼
F710InputMapper
  side / mode / deadman / twist / g_dot / c
    ▼
50Hz异步latest-state sender（ACK不阻塞手柄采样、旧帧不补播）
    ▼
SSH直连隧道 -> G2回环Shadow receiver -> TeleopSupervisor（后续）
  POLICY / TAKEOVER / INTERVENTION / HOLD
    ▼
速度积分 + Relative pose target
    ▼
差分IK + joint/workspace/singularity limits
    ▼
Logical34DComposer
    ▼
唯一 G2 Control owner
  arms14 100Hz joint_servo_control
  hands20 changed-only 5Hz move_ee_pos
```

游戏手柄断开、超过消息超时、轴值非法、同时换侧、IK失败或 GDK 返回非零时，一律 HOLD。
接收器重新连接后不得自动恢复运动，必须重新选择侧、重新锁定末端参考并再次按 deadman。

2026-08-28流畅度修正：Windows手柄采样与ACK接收已解耦，网络只消费最新一帧；G2 100Hz
循环错过deadline后跳过旧tick而不追赶突发下发；末端twist加入约150ms矢量速度斜坡，RB
释放仍立即HOLD。Windows和G2每5秒分别输出采样周期、ACK RTT、包间隔、packet age、控制
周期、Pink IK耗时、GDK send耗时及watchdog计数。双臂仍为100Hz；当时O10为10Hz周期刷新，
该历史语义已在2026-09-03被5Hz changed-only取代。安全速度、
初始120ms watchdog在Windows+SSH现场23秒内被151.8ms尾延迟误触发5次，现按4cm/s运动
上限调整为200ms（断联后最坏8mm）；根据首次时序日志，10cm径向包络会每约2.5秒触发一次饱和并诱发人工
反复clutch，现调整为仍然有限的20cm包络。接管时删除重复的34D状态读取，并单独记录
`takeover_ms`。

初始no-GDK Shadow传输没有TeleopSupervisor、IK、GDK或硬件输出。可执行操作见
`WINDOWS_TO_G2_SHADOW_RUNBOOK.md`。

2026-08-28已新增Live IK Shadow receiver：它是一个只读GDK状态owner，读取真实腰/臂反馈并计算
虚拟IK候选，源文件不包含任何硬件下发API。它只能由操作者在确认没有其他Robot owner后于G2
本地启动；具体命令和短脉冲验证顺序见操作手册第7节。该实现完成不等于现场验收通过。

## 5. DAgger 接管状态机

```text
POLICY
  + START rising
  -> 丢弃未执行的旧policy suffix
  -> 锁定当前34D、当前所选EEF pose
  -> HIL_LATCHED

HIL_LATCHED
  + RB held       -> 所选机械臂人工运动
  + RB released   -> 双臂HOLD，但仍留在HIL，可换侧或使用RT
  + START rising（要求RB已松开且手动作已完成）
  -> HOLD最后成功命令
  -> 重新采集三路RGB + measured 34D
  -> 请求全新policy chunk
  -> POLICY
```

活动侧之外的机械臂保持当前目标；活动侧之外的手保持最后成功的 O10 命令。不能让未被人工
控制的一侧继续播放接管前的旧 chunk。RB在POLICY中没有快速接管语义，只有START能够改变
POLICY/HIL所有权。手柄或transport断联进入SAFE_HOLD，恢复连接不会自动回POLICY。

START采用随每个latest-state包持续发送的单调`hil_toggle_id`，而不是只发送一帧按钮边沿；G2按
`session_id + hil_toggle_id`去重。新session的首个ID只建立锚点、不切换状态，ID回退或跳号进入
SAFE_HOLD。退出HIL后生成新的`policy_epoch`，只有该epoch上基于新观测返回的chunk才能恢复POLICY。

### 5.1 HIL telemetry合同

100Hz控制样本至少记录：`hil_state`、`control_source`、`hil_latched`、`intervention_id`、
`policy_epoch/request_id/action_index/chunk_valid`、`fresh_policy_required`、
`hil_toggle_id/button_pressed`、`intervention_mask`、`human_arm_mask`、`human_hand_mask`、
`policy_proposal_34d`、`human_candidate_34d`、`selected_command_34d`和`measured_34d`。

状态边沿单独记录事件：POLICY进入HIL、被丢弃的policy suffix长度、退出请求被RB/手动作拒绝、
HIL进入handoff HOLD、新观测请求、新chunk接受/拒绝、transport丢失、SAFE_HOLD和Home/reset。
控制样本与状态事件分开，避免只靠重复100Hz字段推断接管因果关系。

### 5.2 HIL episode journal候选（未接相机、未Powered）

`hil_episode.py`已实现不阻塞未来控制循环的异步四流journal：30Hz逻辑观测、完整50x34
policy chunk、100Hz控制证据和稀疏ownership事件分别写入JSONL。目录先以`.partial`存在，只有
正常结束、writer无错误且零drop时才原子发布。审计器不仅检查shape、序号和单调时间，还逐tick
验证`control_source -> selected_command_34d -> executed_command_34d`一致，防止日志只记录候选
而没有证明实际下发的是谁。

当前journal有意固定`training_ready=false`：它只记录三路图像引用，还没有保存图像载荷；现有
`G2CameraRig`也只保留一次逻辑采集起始时间，未把三路GDK消息各自的设备时间戳传出。冻结的DM0.5
原始数据则通过三份相机时间戳文件把H5帧严格映射到视频帧，不能用H5/请求序号代替视频帧号。
因此这一版可验证HIL所有权和标签因果链，但不能被转换器当训练episode。

无硬件演练入口：

```bash
PYTHONPATH=src python3 scripts/33_hil_mock_episode.py /tmp/g2_hil_mock_episode
PYTHONPATH=src python3 scripts/34_audit_hil_episode.py /tmp/g2_hil_mock_episode
```

当前演练覆盖`POLICY -> START接管 -> RB人工运动 -> RB松开仍在HIL -> START退出 ->
HANDOFF_HOLD -> fresh policy epoch恢复`。下一实现门是扩展camera adapter保留每路消息时间戳，接入
实际视频sink，再把journal与冻结DM0.5 episode转换合同对齐；完成前不得宣称可直接训练。
本地完整回归为`116 passed, 6 skipped`；同步后G2端13项HIL纯逻辑测试与同一Mock审计通过，全程
未导入GDK或创建任何硬件控制对象。

### 5.3 一条推理主链、三种录制sink（设计已冻结，待实现）

不建立两套推理架构。`inference-only`、`hil-record`与推荐的`prebuffer-record`必须消费同一种
携带三路原始时间戳的`TimestampedObservation`，并复用相同的policy和控制调度。差别只在旁路
sink：`NullSink`不落图像，`RingBufferSink`在内存保留最近约3--5秒压缩帧，
`EpisodeVideoSink`异步落盘。START接管时，预录模式将介入前上下文连同后续人工修正一起提交，
以保留模型犯错的起因。

普通推理因此不会默认永久录制全部图片；正式DAgger采集才默认持久化完整episode。但所有模式都
走同一观测对象，禁止录制器重新采相机或形成另一条预处理路径。相机/state约30Hz、policy chunk按
请求、控制证据100Hz、按钮与ownership边沿按事件分别记录。sink队列drop或写盘失败只使episode
不可训练，不得阻塞100Hz命令链。真实视频帧与三路设备时间戳接通前，manifest仍强制
`training_ready=false`。

## 6. 分阶段实施

### 阶段 1：F710 只读轴号与方向探测（只在Windows执行）

F710只插Windows，在PowerShell项目根目录运行：

```powershell
py scripts\02_windows_f710_readonly.py
```

按顺序单独操作并记录：

1. 左摇杆左、右、上、下；
2. 右摇杆左、右、上、下；
3. LT从松开到压到底；
4. RT从松开到压到底；
5. 十字键左、右、上、下；
6. LB、RB；
7. A、B、X、Y；
8. 关闭手柄电源并确认 reader 报断联/停止更新。

禁止为了探测在 G2 上重新插入接收器。只有探测结果与配置一致后，才把 `mapping_status`从 `expected_unverified`改为
`read_only_verified`。此阶段不导入 GDK。

### 阶段 2：纯逻辑映射与故障注入

- deadzone与归一化；
- deadman、换侧和换模式互锁；
- 扳机同时按下净速度为零；
- joystick EOF/拔出进入断联；
- 重新连接不自动 enable；
- 日志显示当前侧、模式和所有输出意图。

### 阶段 3：G2 Shadow

唯一 Control owner 读取严格34D，但禁止调用两个硬件下发接口。手柄生成 twist、末端目标、
IK候选和逻辑34D，仅记录不发送。

### 阶段 4：URDF/FK/IK 离线验证

已定位G2厂商`G2_t2_crs + o10_t2`的URDF、完整MJCF和SRDF，并用G2自带Pinocchio 3.7
验证完整模型可解析、左右单臂均可缩减成严格7DOF。模型来源路径、SHA256、Pink/Pinocchio
职责边界、`bottle_grasp_frame`设计和全部验收门见`G2_KINEMATICS_AND_PINK_IK_PLAN.md`。

已使用原始H5数值样本确认`xyzw`、`[left,right]`和同AID数据边界。绝对工具frame位置尚未达到
2mm门槛，因此不冻结H5拟合偏置。正式相对控制以接管瞬间实测7D做FK零点。

G2上的Pink 4.3.0 + Pinocchio 3.7.0 + ProxQP 0.7.3已完成全部50个sorting样本姿态、左右侧、
六轴正负1mm/1度的严格离线回放：1200/1200通过0.1mm/0.05度阈值，求解时延最大5.460ms。
这只通过`IK_REPLAY_PASS`，下一门是读取真机状态但不发送动作的`LIVE_IK_SHADOW_PASS`；不得
直接跳到Powered。

### 阶段 5：O10 `g/c`标定

从 DM0.5 饮料瓶数据提取左右手历史抓握曲线，分别标定 `g`和 thumb clearance residual。
已从当前AID `G2A0004C203358`的832条有效sorting episode抽样得到左右各91,629帧。
第一主成分解释率左手99.955%、右手99.993%，支持每手两个操作量。冻结映射位于
`configs/hand_synergy_dm05_current_aid.json`：四指PIP同步从0到1 rad，外展固定为0；拇指
使用左右各自历史开/闭模板，`c=1`只把拇指平滑回撤到历史开手模板。该数据拟合仍明确
标为`dm05_fitted_hardware_microtest_required`，首次Powered必须空载、毫米级、由专人握急停。

### 阶段 6：Powered 微动作

必须显式授权、normal channel、专人握物理急停：

1. 单臂平移、毫米级；
2. 单轴旋转；
3. 单手 `g`；
4. 单手 `c`；
5. 左右侧互锁；
6. 策略接管、HOLD和全新观测恢复。

2026-08-28已实现显式Powered入口`scripts/12_g2_powered_f710.sh`。它复用DM0.5已验证
`G2RobotController`的Split边界，要求G2本地回车确认、mode 5健康检查、normal通道、
200ms网络watchdog。O10反馈全零时不回发零值，只允许以6小时内且通过冻结合同校验的
DM0.5最后成功`move_ee_pos`命令作为HOLD初值；过期记录仅在20D逐值完全等于人工批准Home
时豁免，否则拒绝启动。唯一现场启动顺序和按键表见
`START_HERE_WINDOWS_G2.md`；禁止从6000D远程执行该入口。

### 阶段 7：采集与训练

两类各约10条：左手碰倒后的 recovery/regrasp；左右交替时右手瓶颈 correction。每条记录
三路RGB、measured/executed 34D、原策略 action、手柄原始event、归一化意图、活动侧、
模式、IK、`g/c`、deadman边沿、intervention mask及时间戳。

从 NewLora 75K 分支，以旧数据60--70%、correction 30--40%平衡回放，保持原34D norm
stats，使用低学习率，并在1K、2.5K、5K分别评估定向改善和原任务回归。

## 7. 禁止事项

- 禁止在 G2 上用 `apt`安装 joystick 工具；
- 禁止在厂商 `teleop_main_node`输入隔离方案验证前再次把 F710 插入 G2；
- 禁止让两个进程拥有 GDK Robot；
- 禁止用 `unified-34`作为正式输出；
- 禁止自动切 low-latency；
- 禁止把接收器重连解释为继续运动；
- 禁止把无线手柄当物理急停；
- 禁止未验证轴号/符号就发送动作；
- 禁止未验证 URDF/FK/IK 就 Powered；
- 禁止把模型文件名匹配或结构可加载误当成FK数值验证；
- 禁止改变 DM0.5 的34D训练合同。
