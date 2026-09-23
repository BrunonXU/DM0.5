# G2 饮料瓶分拣：iPad 人工介入与 DAgger 全流程

状态：设计冻结，阶段 1（iPad 只读）已建立代码骨架；所有 G2 Shadow/Powered 接口尚未实现。  
范围：只面向 DM0.5 饮料瓶分拣，不包含 `tasks/` 中的其他任务。  
安全原则：默认只读；没有准确 URDF/FK 验证、Shadow 报告、现场授权和物理急停时，禁止 Powered。

## 1. 关键结论与接口纠正

G2 正式运行路径不是统一 34D Servo。最新 `g2_robot` 真机证据冻结的默认路径是
**Split**：

```text
逻辑 action：左臂7 + 右臂7 + 左O10 10 + 右O10 10 = 34D absolute target

硬件下发：
  双臂前14D ── 100Hz ── joint_servo_control（normal channel）
  双手后20D ──  10Hz ── move_ee_pos(group=dual_tool, target_type=o10_t2)
```

`send_action(34D)`可以作为上层逻辑接口，但正式硬件适配器必须在内部拆分。禁止把新遥操
默认接到历史 `unified-34` 诊断模式：该模式在当前 G2 上曾出现 O10 参数拒绝与阻塞。

状态读取仍为严格 34D：

```text
get_joint_states() -> 左右臂14D measured motor_position
get_end_state()    -> 左右O10各10D
```

权威实现参考：

- `../../g2_robot/g2_sort/deployment/g2_client/robot_control.py`
- `../../g2_robot/g2_sort/deployment/g2_client/joint_layout.py`
- `../../g2_robot/docs/GDK_2.6.3_RELEVANT_API.md`
- `../../g2_robot/docs/G2_ROBOT_STRUCTURE.md`

本项目不得在 G2 上创建第二个 GDK `Robot()`。未来接入时，现有 Control process 保持唯一
Robot owner；iPad 输入、策略输出和 DAgger recorder 都向它提交意图或数据，不能直接抢占
硬件接口。

## 2. 第一版控制空间

一次只允许控制一侧机械臂和同侧 O10。

### 2.1 机械臂

iPad ARKit 输出相对 6D 位姿：

```text
delta position: x, y, z
delta rotation: roll, pitch, yaw
```

每次按下 deadman 时重新锁定：

```text
当前iPad pose <-> 当前所选G2末端pose
```

因此 iPad 在松开期间可以拿回舒适位置，再按下时机器人不会跳回旧参考点。

### 2.2 每只 O10 只保留两个控制量

```text
g：整体抓握状态
c：大拇指避障状态
```

模型、数据和硬件命令仍保持每手 10D；低维量只存在于人工输入层：

```text
hand_10d = historical_grip_synergy(g) + thumb_clearance_residual(c)
```

- 四指 abduction 固定在历史分布附近；
- 四指 PIP 同步，但使用分别标定的曲线，不假设四个数值严格相等；
- 历史抓握曲线同时给出大拇指基线；
- `c`主要修改 thumb roll 与 thumb abad；
- 左右手分别标定，不共用字面数值或符号；
- `c`第一版为两态目标，但必须连续平滑，不得直接跳变；
- 未完成真实数据标定前，O10 Powered 必须被软件锁定。

## 3. HEBI Mobile I/O 按键映射

### 3.1 冻结映射

| 控件 | 含义 | 生效规则 | 设计理由 |
|---|---|---|---|
| `B1` | Deadman / clutch | 必须持续按住才允许产生新运动目标；松开立即 HOLD | 沿用 LeRobot 官方 iOS 习惯，失手即停 |
| `B2` | 选择左臂+左手 | 只允许在 `B1`松开时选择 | 显式左右选择，避免单个 toggle 状态歧义 |
| `B3` | 选择右臂+右手 | 只允许在 `B1`松开时选择 | 与 `B2`一一对应，适合双臂任务 |
| `A3` | 所选手整体抓握速度 | 正值/负值分别开合，回到中性区即保持 | 与官方 A3 gripper velocity 语义一致；接管时不会因绝对滑块错位跳手 |
| `B4` | 所选手大拇指避障 | 按住目标为避让，松开目标回历史抓握基线；两向都平滑 | 瞬时动作比持久 toggle 更不容易遗忘状态 |
| 其他 `A/B` | 未分配 | 忽略并记录 | 第一版减少模式和误触负担 |

### 3.2 强制互锁

1. 初始没有活动侧；必须先按 `B2`或`B3`选择。
2. `B2`与`B3`同时按下属于输入故障，不改变活动侧。
3. `B1`按下期间忽略所有换侧请求。
4. 没有活动侧时按 `B1`，只记录告警，不输出运动意图。
5. `A3`设置 deadband；中性区内 `g`保持不变。
6. `g`以有限速度积分，并限制在 `[0,1]`；正负方向必须经过现场开合方向标定。
7. `B4`只影响当前选择侧；另一只手保持最后一次成功的命令空间目标。
8. `B1`松开后，双臂保持、双手保持，不自动恢复策略。
9. 无线 iPad 按钮不是安全级急停。现场 Powered 必须始终有人握物理急停。

### 3.3 为什么不用 A3 绝对抓握位置

若 A3 当前滑块位置与机器人真实抓握状态不一致，接管瞬间会要求手指跳到另一姿态。
速度式输入满足：

```text
A3处于中性 -> 当前g保持
A3离开中性 -> 以受限速率改变g
```

它更适合在策略已经抓住瓶子后进行小幅 correction。后续如果实测 HEBI A3 不是回中控件，
则在软件中使用“软接管”或改为两个开/合按钮，不能直接降级为无保护绝对映射。

## 4. iPad 到 G2 的通信架构

```text
HEBI Mobile I/O / ARKit
  family=HEBI, name=mobileIO
          │
          │ 局域网 HEBI discovery + feedback
          ▼
IpadHebiReader（只读）
  pose + A1..A8 + B1..B8 + timestamp
          ▼
InputMapper
  active_side + deadman + grip_velocity + thumb_clearance
          ▼
TeleopSupervisor
  policy / intervention / hold 状态与唯一控制权
          ▼
RelativePoseLatch + FK/IK + safety bounds
          ▼
Logical34DComposer
  active arm IK + inactive arm hold + left/right hand 10D
          ▼
唯一 G2 Control process
       ┌─────────────────────────────┐
       │ arms14 -> 100Hz joint_servo │
       │ hands20 -> 10Hz move_ee_pos │
       └─────────────────────────────┘
```

iPad 流中断、姿态非法、四元数非法、消息超时、活动侧不明确或 IK 失败时，一律进入 HOLD，
不使用最后一个手机速度继续外推。

## 5. 接管状态机

```text
DISCONNECTED
  -> IPAD_READY             iPad位姿与输入稳定
  -> SHADOW_READY           G2状态、FK、IK只读验证通过
  -> POLICY                 策略拥有逻辑目标

POLICY + B1 rising edge
  -> TAKEOVER_LATCH         丢弃策略未承诺suffix，锁定当前state/EEF/iPad pose
  -> INTERVENTION           人工控制所选侧

INTERVENTION + B1 released
  -> HOLD                   保持最后成功的双臂与手命令
  -> FRESH_OBSERVATION      重新采三图与34D measured state
  -> POLICY                 仅接受新请求返回的chunk
```

不能在 B1 松开后直接恢复消费接管前的旧 chunk。

## 6. 分阶段实施与验收

### 阶段 1：iPad 只读，不导入 GDK

- 发现 `HEBI/mobileIO`；
- 连续记录 ARKit position、wxyz quaternion、A/B 输入；
- 检查帧率、间隔、丢帧和非法姿态；
- 检查 B1/B2/B3/B4/A3 映射；
- iPad 熄屏、App 退后台或 Wi-Fi 断开时，程序必须检测超时。

通过标准：持续移动和按钮测试中无 NaN/零四元数，输入状态机没有换侧歧义。

### 阶段 2：G2 read-only 与 Shadow

- 由唯一 Control owner 读取严格34D；
- 验证 O10 型号均为 `o10_t2`、G2 健康且 mode=5；
- 不调用 `joint_servo_control`和`move_ee_pos`；
- iPad delta 只生成并记录末端目标和候选关节目标。

### 阶段 3：URDF/FK/IK 离线验证

必须获得准确的 G2 URDF、joint axes/signs/limits、base frame 和左右 tool frame。使用软件
版本一致的原始 H5：

```text
state/joint/position
state/end/position
state/end/orientation
```

做 FK 误差统计，再做 `q -> FK -> 小delta -> IK -> FK` round trip。没有量化报告不得进入
Powered。

### 阶段 4：O10 synergy 标定

- 从饮料瓶数据提取左右手历史抓握主曲线；
- 标定 `g=0/1`及中间值；
- 标定 `c=0/1`对 thumb roll/abad 的安全残差；
- 在静止双臂、无瓶、无交接条件下先 Shadow，再单次小步验证；
- 手目标按10Hz更新并受速度、加速度、单步变化限制。

### 阶段 5：Powered 微动作

每一步都要求现场显式授权、倒计时、normal channel 和物理急停：

1. 单臂、position-only、毫米级；
2. 单轴旋转；
3. 组合6D小幅运动；
4. 单手 `g`；
5. 单手 `c`；
6. 左右侧选择；
7. 策略到人工接管；
8. 人工 HOLD 后全新策略观测恢复。

### 阶段 6：DAgger 采集

目标两类各约10条：

1. 左手碰倒瓶子后的 recovery + regrasp；
2. 左右手交替时，右手瓶颈抓取 correction。

每条至少记录：三路RGB、measured 34D、executed 34D、原策略 action、iPad 原始 pose、
EEF target、IK q、`g/c`、活动侧、B1边沿、intervention mask、单调时间戳和失败/退出原因。

人工监督标签只来自真正执行的人工纠正段；模型导致失败的旧动作可以作为上下文记录，但不能
作为专家 target。

### 阶段 7：继续训练

- 从 NewLora 75K 分支，不覆盖原实验；
- 保持34D合同和原800条 train-only norm stats；
- batch 建议旧数据60--70%、correction 30--40%；
- 两类 correction 平衡采样；
- 低学习率约 `2.5e-5`到`5e-5`；
- 在1K、2.5K、5K分别评估；
- 同时检查定向修复和原任务回归。

## 7. 当前阻塞项

以下内容不能猜测：

- 准确 G2 URDF 与左右 tool frame；
- iPad 具体型号及相机相对平板控制参考点的偏移；
- HEBI A3 在该布局上的实际中心值、方向和范围；
- 左右手 `g/c`的数值曲线；
- G2 当前现场部署快照与6000D `g2_robot`是否完全一致。

这些项目必须通过只读探测、数据分析或现场 Shadow 校验后冻结。

## 8. 禁止事项

- 禁止在 G2 域控执行 `apt`或系统包变更；
- 禁止自动切换 low-latency；
- 禁止让两个进程同时拥有 GDK Robot；
- 禁止把重启后的 O10 全零反馈作为安全保持目标回发；
- 禁止把统一34D Servo作为正式默认；
- 禁止跳过 Shadow 直接 Powered；
- 禁止把 iPad 的 B1/B4 当成物理急停；
- 禁止修改 DM0.5 的34D训练合同来迎合2D手部遥操。
