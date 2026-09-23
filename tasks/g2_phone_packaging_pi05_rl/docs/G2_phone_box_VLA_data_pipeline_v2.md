# G2 手机装盒 Demo：数采、π0.5 SFT 与 RLinf 后训练数据流

> **定位**：这是一版用于验证完整 Pipeline 的 Demo 方案，不按生产级数据平台设计。  
> **任务**：G2 双臂夹爪完成「抓手机 → 双手换手 → 手机入盒 → 抓盒盖 → 盖盒」。  
> **当前主路线**：`π0.5 / OpenPI → 单任务 SFT → 固定真机评测 → RLinf SAC-Flow + RLPD（针对精细阶段）`。  
> **后续候选**：DAgger / Human Intervention / RLT；等 Pico 接管接口和 π0.5 baseline 跑通后再展开。  
> **核心原则**：先把一个单任务模型训到“基本会做”，再用固定真机评测定位真实 weak point，最后只对最需要精度的阶段做 targeted SFT 或 RL。

---

# 0. 一页看懂：我们现在到底要做什么

## 0.1 当前阶段

目前处于：

```text
阶段 A：数采方案设计
        ↓
准备 G2 + Pico 遥操
        ↓
采第一版 Human Teleop Dataset
```

现在最重要的事情不是 RL，也不是复杂的数据平台，而是：

1. 把 episode 录对；
2. 把手机、盒子等初始状态覆盖好；
3. 保留高质量 human demonstration；
4. 先训出一个 π0.5 单任务 baseline。

---

## 0.2 整体 Pipeline

```text
Human Teleop
     │
     ↓
第一版 Dataset
     │
     ↓
π0.5 SFT（LoRA / Full FT 由实验决定）
     │
     ↓
Real Robot Rollout
     │
     ↓
Failure Analysis
     │
     ├──────────────→ Targeted SFT / DAgger（后续）
     │                 │
     │                 ↓
     │           Human Correction
     │                 │
     │                 ↓
     │              SFT v2
     │
     └──────────────→ RLinf Online RL
                       │
                       ↓
                Replay Buffer
                       │
                       ↓
              SAC-Flow + RLPD
                       │
                       ↓
                  Better Policy
```

一句话概括：

```text
人教会它
→ 它自己做
→ 看它哪里不会
→ 精细阶段用 targeted data / RL 改进
→ 再做
```

---

## 0.3 第一版预定的动作接口

当前任务使用双臂普通夹爪，不沿用旧 G2 灵巧手的 34D action contract。

第一版环境动作定义为：

```text
左臂 7 个关节目标
+ 左夹爪 1 维
+ 右臂 7 个关节目标
+ 右夹爪 1 维
= env_action_dim 16
```

正式采集使用以下顺序和语义：

| Index | 字段 | 语义 |
|---:|---|---|
| 0–6 | `left_arm_target_q` | 左臂 7 关节绝对目标，单位 rad |
| 7 | `left_gripper_command` | 左夹爪连续命令，归一化为 `[0,1]`，0=全开，1=全闭 |
| 8–14 | `right_arm_target_q` | 右臂 7 关节绝对目标，单位 rad |
| 15 | `right_gripper_command` | 右夹爪连续命令，归一化为 `[0,1]`，0=全开，1=全闭 |

Pico 的 EE movement 由控制器通过 IK 转换为上述关节目标；训练标签记录安全检查后机器人实际接受的绝对 target，而不是 Pico delta。

第一版 `observation.state` 也使用相同顺序的 16D measured joint/gripper state。关节速度、电流、控制器状态等 richer proprio 保存在 raw episode 中，但首版不直接拼入 π0.5 state；需要加入时再提升 schema/config 版本。

π0.5 base 内部继续保留：

```text
model_action_dim = 32
```

16D action 进入模型前 pad 到 32D；模型输出只取前 16D。observation state 独立定义并按 OpenPI data transform 配置 padding。这样不修改 π0.5 base 的预训练动作投影。动作的关节顺序、单位、绝对/增量语义、夹爪开合范围一旦开始正式采集就不能静默修改，任何修改都必须提升 `schema_version`。

当前确定使用 16D 关节空间标签。如果 Pico 遥操验证表明 IK 后的关节目标不连续或不适合学习，只能在正式约 100 条采集前统一切换为双臂 EE action；两种动作语义不能混在同一个数据版本里。

---

## 0.4 第一版推荐的任务与物理场景

使用无价值的 dummy phone。第一版不让手机完全贴在硬桌面，也不采用完全竖直支架，而是使用近似平放的浅托架：

```text
手机倾角约 5~15°
两侧有夹爪避让缺口
手机底部与桌面保留几毫米间隙
托架相对手机的支撑几何固定，整体可在有标记的 Bin 内移动/旋转
```

第一版盒体和盒盖先使用少量可重复的标记位置，装配间隙适当放宽、盒盖放在稳定易抓的位置。正式约 100 条采集前必须冻结手机究竟采用浅托架、边缘悬空或其他方案；同一数据版本不能混用不同支撑几何。等 baseline 稳定后，再逐步缩小间隙并增加位姿扰动。

---

# 1. Episode 到底是什么

## 1.1 一次完整数采 = 一个 Episode

例如：

```text
RESET
↓
手机放好
↓
机器人回到初始姿态
↓
开始记录
↓
抓手机
↓
双手 handover
↓
手机放入盒体
↓
抓盒盖
↓
盖盒
↓
结束记录
```

这一整条就是：

```text
1 episode
```

如果中间手机掉了、任务失败并终止，也仍然是一个 episode，只不过：

```text
outcome = failure
```

---

## 1.2 一个 Episode 内部自动记录什么

假设 30 Hz 数采、任务持续 20 秒：

```text
20 × 30 = 600 timesteps
```

每个 timestep 自动记录：

```text
各 RGB camera frame + camera_timestamp
robot measured state + state_timestamp
Pico raw command + input_timestamp
安全映射/限幅后机器人实际接受的 16D target action
action_send_timestamp
frame_valid / dropped_frame_count
controller_owner（第一版固定为 HUMAN）
```

例如：

```text
Episode 0042

frame 0000
  images
  measured_robot_state
  raw_human_command
  executed_target_action
  timestamps
  validity

frame 0001
  images
  measured_robot_state
  raw_human_command
  executed_target_action
  timestamps
  validity

...

frame 0599
```

普通 π0.5 SFT 不需要人工逐帧标注。

训练本质上自动得到：

```text
(image_t, state_t, instruction)
              ↓
        expert action_t
```

其中监督标签使用机器人控制链实际接受的 `executed_target_action`，而不是未经映射、限幅和安全检查的 Pico 原始输入。两者都保存，方便排查延迟和控制映射问题。

所有时间戳必须来自统一的单调时钟。录制程序需要能检测相机、state 和 action 的错位、过期与丢帧；不能只给一整个 loop 写一个笼统的 timestamp。

---

# 2. 真人遥操轨迹的“自然不同”不是坏数据

这是这版方案最重要的修正之一。

即使：

```text
手机位置一样
盒子位置一样
机器人初始姿态一样
```

真人用 Pico 遥操 20 次，也不可能像脚本一样完全重合。

自然会出现：

```text
EE path 有小差异
速度有小差异
手腕姿态有小差异
抓取点有小差异
handover timing 有小差异
```

这些属于：

> **natural demonstration variability**

是正常的人类示范分布，不应该被当成 `bad trajectory`。

所以我们不需要人为规定：

```text
这一条必须从左绕
下一条必须从右绕
```

第一版也不需要主动制造所谓“轨迹多样性”。

真正应该主动控制的是：

> **初始状态 / 场景分布。**

即：

```text
主动随机化 scene
不主动随机化 human trajectory
```

---

# 3. 第一版 Human Teleop 数据怎么分类

为了简单，第一版不要设计复杂 ontology。

建议把：

```text
任务结果 outcome
```

和：

```text
数据有效性 data_validity
+ 行为类型 behavior_type
```

分开。

---

## 3.1 Outcome

任务结果使用三个值：

```text
success
failure
aborted
```

其中 `failure` 表示正常执行但任务没有完成；`aborted` 表示人工提前终止或安全终止。

---

## 3.2 Data validity 与 behavior type

数据是否有效和行为类型分开记录：

```text
data_validity = valid | invalid
behavior_type = clean | recovery
```

`failure` 不等于无效数据。正常发生的任务失败仍然是有效 rollout，可用于 failure analysis 和后续 RL；只有相机、同步、控制器或记录链路异常时才标记为 `invalid`。

---

## 3.3 clean

正常、自然、流畅地完成。

例如：

```text
正常抓手机
↓
正常 handover
↓
正常放入盒子
↓
正常拿盖子
↓
正常盖盒
```

允许存在真人遥操天然的小轨迹差异。

只要：

```text
没有明显设备异常
没有明显误操作
没有明显失败后长时间救火
```

就属于：

```text
outcome = success
data_validity = valid
behavior_type = clean
```

这是：

> **第一版 SFT 的绝对主力数据。**

---

## 3.4 recovery

真人遥操过程中出现了正常的小偏差，人自然纠正回来，并最终成功。

例如：

```text
手机准备入盒
↓
发现稍微偏了一点
↓
人往旁边修 2~3 mm
↓
继续完成
```

或者：

```text
handover 时位置略偏
↓
重新调整
↓
交接成功
```

这种数据不是坏数据。

它包含：

> **合理的 correction / recovery behavior。**

可以：

```text
少量进入初始 SFT
```

对应标记为：

```text
outcome = success
data_validity = valid
behavior_type = recovery
```

以后也可以单独成为：

```text
Recovery Pool
```

---

## 3.5 invalid

`invalid` 不是“轨迹和其他人不一样”。

而是：

> **这条数采本身存在明确异常，不适合当 expert demonstration。**

例如：

```text
Pico 映射突然异常
Pico 短暂失灵
操作者误按按钮
机器人底层突然异常抖动
相机严重掉帧
state/action timestamp 明显错位
数据记录中断
发生非任务本身要求的意外碰撞
```

即使最终被人救回来并完成：

```text
outcome = success
```

也应该标记为：

```text
data_validity = invalid
```

默认不进入 π0.5 SFT。

---

## 3.6 failure

如果任务本身没有完成：

```text
手机掉落
handover 失败
手机无法放入盒体
盒盖卡死
进入不可恢复状态
```

则：

```text
outcome = failure
data_validity = valid
```

第一版 Human SFT 中：

> **不作为 expert demonstration 使用。**

但原始数据可以保留，以后用于 failure analysis。

---

# 4. 最简单的判断表

| Outcome | Validity / Behavior | 初始 π0.5 SFT | 后续用途 |
|---|---|---:|---|
| success | valid + clean | ✅ 主力 | SFT / Demo Buffer |
| success | valid + recovery | ✅ 少量 | SFT / Recovery reference |
| success / failure / aborted | invalid | ❌ | 仅保留原始记录与异常分析 |
| failure | valid | ❌ BC | Failure analysis / 后续 RL Replay |
| policy rollout failure | valid | ❌ BC | RL Replay / 后续 DAgger trigger |
| human intervention segment | valid | 后续决定 | DAgger / HIL Replay |

---

# 5. 真机数采最重要的不是“自然正态分布”，而是 Coverage

如果完全让操作员随手 reset，很容易出现：

```text
手机经常放桌子中央
盒子也经常放最舒服的位置
yaw 经常接近 0
handover 位置也习惯固定
```

最后可能：

```text
500 episodes
```

看起来很多，

但大部分初始状态集中在很小区域。

真正应该控制的是：

> **deployment 范围内的 state coverage。**

---

# 6. 第一版只随机化少数关键变量

不要把事情搞复杂。

手机任务第一版建议只控制：

```text
Phone:
  x
  y
  yaw

Box:
  x
  y
  yaw

Lid:
  x
  y
  yaw

Robot:
  initial arm pose
```

如果 handover 后面发现特别敏感，再单独增加：

```text
handover position
```

第一版不必一开始把所有变量都随机。

---

# 7. 怎么随机化：分 Bin，而不是随便摆

例如手机 x：

```text
LEFT
CENTER
RIGHT
```

手机 y：

```text
NEAR
CENTER
FAR
```

手机 yaw：

```text
LEFT_ROT
CENTER
RIGHT_ROT
```

盒子同理。

例如程序告诉你下一条：

```text
Episode 0047

Phone:
  X = RIGHT
  Y = CENTER
  Yaw = LEFT_ROT

Box:
  X = LEFT
  Y = CENTER
  Yaw = CENTER

Robot Init:
  B
```

你按照要求 reset。

然后开始录制。

这就是：

> **controlled real-world randomization**

和仿真 domain randomization 思想类似，但不是程序直接生成场景，而是人按照 scenario reset。

---

# 8. 不需要做所有组合

例如有 8 个变量，每个 3 档：

```text
3^8
```

组合数量会迅速爆炸。

第一版不用做完整笛卡尔积。

我们的目标只是：

```text
每个重要变量的不同区间都被覆盖
+
不要出现某一个 scenario 占绝大多数
```

所以：

```text
LEFT / CENTER / RIGHT
```

大致都有数据即可。

---

# 9. 同一个 Scenario 多采几次有没有意义

有。

因为真人轨迹自然不同，同时还有：

```text
camera noise
joint noise
细微抓取误差
速度差异
timing 差异
```

所以一个 scenario 不应该只采 1 次。

但边际收益会下降。

例如：

```text
CENTER-CENTER 已经有 30 条

RIGHT-FAR 只有 3 条
```

那下一条优先采：

```text
RIGHT-FAR
```

而不是继续中心场景。

核心不是：

```text
绝对不能重复
```

而是：

> **不要让一个简单场景无限重复，挤掉真正需要的 coverage。**

---

# 10. 第一版数据量和比例

这不是固定公式，而是 Demo 起步 recipe。

如果第一次正式 SFT 使用约：

```text
100 episodes
```

建议大概：

```text
80~90 条 clean success
+
10~20 条自然 recovery / 较难但合理的 clean success
```

注意：

这里不需要再硬拆：

```text
45% nominal
30% coverage
15% boundary
10% recovery
```

第一版不用这么精确。

最核心的是：

```text
绝大多数 = clean expert behavior
+
场景位置覆盖合理
+
少量真实 recovery
```

就够了。

---

# 11. 不要一开始故意制造大量“极端长尾”

第一轮数采不需要：

```text
手机放到极限位置
yaw 90°
盒子摆到机器人几乎够不到
```

如果这些情况本来就不属于 Demo 正常工作域：

> 不需要训练。

第一版应该覆盖：

```text
正常范围
+
正常范围边缘
```

而真正最有价值的 long-tail：

> **等第一版 policy rollout 后，让模型自己告诉我们。**

例如你以为最难的是手机入盒，

结果模型真正经常失败的是：

```text
handover
```

那第二轮数据就应该重点补 handover。

---

# 12. Episode Metadata 怎么记录

不需要采完以后手工回忆。

Metadata 应该：

> **数采时跟 episode 一起记录。**

第一版至少记录：

```text
episode_id
task
outcome
data_validity
behavior_type
source
scenario_id
failure_stage
invalid_reason
split
schema_version
dataset_version
robot_config_id
camera_calibration_id
```

---

## 12.1 示例

```csv
episode_id,task,outcome,data_validity,behavior_type,source,scenario_id,failure_stage,invalid_reason,split,schema_version,dataset_version
0001,phone_box,success,valid,clean,teleop,S001,,,train,1,phone_box_v1
0002,phone_box,success,valid,recovery,teleop,S002,,,train,1,phone_box_v1
0003,phone_box,failure,valid,,teleop,S003,handover,,train,1,phone_box_v1
0004,phone_box,aborted,invalid,,teleop,S004,,camera_drop,excluded,1,phone_box_v1
```

`robot_config_id` 固定本次机器人、夹爪、关节顺序和控制参数；`camera_calibration_id` 指向当次使用的内外参版本。正式采集开始后如更换动作语义、相机布局或标定，必须创建新数据版本。

---

# 13. 数采程序应该怎么交互

不要手写编号。

程序自动：

```text
Episode 0001
Episode 0002
Episode 0003
...
```

Episode 结束以后弹：

```text
[1] Success + Clean
[2] Success + Recovery
[3] Failure（有效任务失败）
[4] Aborted（人工/安全终止）
[5] Invalid（记录或设备异常）
```

操作者按一下键即可。

例如：

```text
按 1
```

程序自动写：

```text
episode_0042
outcome = success
data_validity = valid
behavior_type = clean
```

然后进入下一条。

---

# 14. Scenario 也尽量自动记录

最好不要一条结束后再回忆：

```text
刚才手机到底在左边还是右边？
```

正确流程：

```text
准备 Episode 0042
↓
程序先生成 scenario S042
↓
显示 reset 要求
↓
人摆手机、盒子、盒盖
↓
开始采
↓
结束后判断 outcome / validity / behavior type
```

这样 scenario 本身自动和 episode_id 绑定。

Bin 只表示目标 reset 区间。若条件允许，还应保存手机、盒体、盒盖的实际初始 pose 或 reset 检查结果，避免名义上同属一个 Bin、实际摆放却差异过大。第一版可以使用带刻度的桌垫、定位模板或外置 fiducial 辅助 reset。

---

## 14.1 数据划分与固定真机评测

第一版数据只需要：

```text
train
validation
excluded
```

不单独保留传统 offline test split。`validation` 按完整 episode / scenario 划分，不能随机拆 frame；normalization statistics 只使用 `train`。

真正的 test 是固定真机评测协议。正式训练前冻结一组不会参与训练的 reset 场景，例如：

```text
E1 标准位置
E2 手机左右偏移
E3 盒体左右偏移
E4 手机与盒体轻微 yaw 扰动
E5 正常工作域内的综合扰动
```

每个 checkpoint 在相同场景和相同试验次数下评测，记录完整成功率、分阶段成功率、完成时间、掉落/碰撞、人工介入次数和失败阶段。评测 rollout 后续如果加入训练集，必须提升数据集版本，并保留新的未见评测条件。

---

# 15. 第一阶段训练：π0.5 单任务 SFT

我们当前不是训练 generalist。

就是：

```text
一个任务
=
一个 task-specific checkpoint
```

例如：

```text
π0.5 Base
↓
phone_box human demos
↓
SFT（LoRA 或 Full FT 由首轮显存与效果实验决定）
↓
π_phone_box_v1
```

无论使用哪种训练方式，都保持：

```text
env_action_dim = 16
model_action_dim = 32
```

夹爪和关节动作只占前 16 维，其余维度由数据 transform pad，并在 loss / inference 中按统一配置处理。

第一版完全不需要考虑：

```text
手机任务 + USB + 饮料瓶
```

怎么混。

也不用考虑 π0 预训练中的：

```text
w ∝ n^0.43
```

那是 foundation pretraining 的跨任务/跨机器人 mixture 问题。

我们这里只关心：

> **一个任务内部的数据质量和 coverage。**

---

# 16. 第一版 SFT 用哪些数据

第一版：

```text
D_SFT_v1 =
clean_success
+
少量 recovery_success
```

不要加入：

```text
invalid episode
valid failure 中的错误 action
以后 policy 自己执行失败产生的 action
```

原因很简单：

SFT 会把数据里的 action 当作：

> expert target

所以不能把错误 action 教给模型。

---

# 17. 第一版 SFT 完以后做什么

不要马上继续闷头采 500 条。

先让：

```text
π_phone_box_v1
```

按第 14.1 节冻结的场景做真机评测，总计先跑：

```text
约 30~50 次
```

然后统计每个阶段。

例如：

| Stage | Success |
|---|---:|
| grasp phone | 95% |
| handover | 80% |
| place phone | 55% |
| grasp lid | 90% |
| close box | 50% |

这时候就知道：

```text
place_phone
close_box
```

才是真正 weak point。

---

# 18. 第二阶段：Failure-driven Data Collection

第一版模型出来以后，数采逻辑改变。

不再问：

```text
还差多少条才到 500？
```

而是问：

```text
模型到底在哪里失败？
```

如果发现：

```text
phone-right + box-left
```

特别容易失败，

就补这一类。

如果：

```text
handover offset
```

失败最多，

就补 handover。

这时候才真正开始：

> **failure-driven data collection**

---

# 19. 什么时候继续 SFT，什么时候上 DAgger / RL

## 情况 A：模型根本没学会任务逻辑

例如：

```text
抓错东西
不知道什么时候 handover
不知道下一步应该拿盒盖
```

这是：

> SFT / task learning 问题。

继续补：

```text
clean expert demonstrations
```

---

## 情况 B：模型整体流程已经会了，但是精细阶段失败

例如：

```text
手机已经到盒子上方
↓
最后差几毫米
↓
碰边
↓
失败
```

或者：

```text
盒盖已经移动到盒体上方
↓
最后 alignment 不稳定
```

这才适合进入精细阶段后训练。当前优先路线是：

```text
π0.5 负责长程顺序和大范围动作
↓
选出一个奖励清晰、重置可控的 weak stage
↓
RLinf SAC-Flow + RLPD
```

第一轮不使用 SAC-Flow 从头优化整个“抓取—换手—入盒—盖盒”长程任务。优先选择 `place_phone` 或 `close_box` 中的一个精细阶段，使用离线 demonstration buffer 与在线 replay buffer 混合训练。

RLT 更直接地利用 VLA feature 和 reference action chunk，保留为后续研究路线；等 π0.5 baseline、reward、RLinf G2 environment 和 SAC-Flow 闭环稳定后再比较。DAgger / Human Intervention 也等 Pico 接管接口完成后单独设计。

的阶段。

---

# 20. DAgger：到底怎么接入

> **当前状态**：本章只保留未来数据用途的概念说明，不作为第一版 Human Teleop 采集的实现要求。Pico 与 G2 的接管、重锚定、安全状态机和恢复控制将在接口开发时单独设计与验证。

DAgger 的核心不是“把失败 rollout 全部重新 SFT”。

而是：

> **模型自己进入了一个 expert 数据里少见的 state，然后人告诉它这里应该怎么做。**

---

## 20.1 实际流程

```text
Policy 自动跑
↓
前面都正常
↓
手机入盒开始偏
↓
你判断马上失败
↓
Pico takeover
↓
人重新对准
↓
恢复
↓
松开 Pico
↓
Policy 继续
```

---

## 20.2 哪些数据进入 DAgger

假设：

```text
frames 0~350
policy

frames 351~380
human takeover

frames 381~500
policy
```

真正进入 DAgger supervised correction pool 的主要是：

```text
frames 351~380
```

也就是：

```text
policy 实际访问到的 state
+
human correction action
```

---

## 20.3 Policy 自己犯错的 Action 不进入 BC

例如：

```text
state = 手机已经偏了

policy_action = 继续往错误方向走
```

不能把：

```text
(state, policy_action)
```

作为普通 BC label。

否则就是在教模型继续犯错。

---

# 21. DAgger 的数据池

逻辑上增加：

```text
D_DAgger
```

里面存：

```text
human takeover 时的 correction segment
```

第二次 SFT：

```text
D_SFT_v2 =
原始 expert core
+
DAgger correction
```

第一版可以保守一点：

```text
70~80% 原始 clean expert
+
20~30% DAgger / recovery correction
```

不用精确到百分点。

核心原则：

> DAgger 用来补洞，不要让模型天天只学“怎么救火”。

---

# 22. Human Intervention 到底怎么做

对我们的 G2 来说，可以非常直接：

```text
G2 自动跑 policy
↓
人站旁边
↓
手上拿 Pico standby
```

正常：

```text
不操作
```

马上失败：

```text
按住 Pico trigger
↓
human takeover
```

修正完成：

```text
松开 trigger
↓
进入 HOLD，清空旧 action chunk 并重新观测
↓
通过独立确认后 policy resume
```

---

## 22.1 建议做成 dead-man switch

例如：

```text
Trigger 按住：
human control

Trigger 松开：
HOLD（不能直接恢复旧 policy chunk）
```

程序自动记录：

```text
intervention_mask = 1 / 0
```

不需要人工逐帧标。

---

## 22.2 Takeover 要注意相对控制

切换到 Pico 的瞬间必须重新 anchor。

不要让 Pico absolute pose 和机器人当前 EE pose 不一致导致瞬间跳动。

正确思路：

```text
takeover moment

当前 Robot EE
=
Pico reference origin
```

后面只用：

```text
Pico Δpose
→ Robot ΔEE
```

---

# 23. DAgger 和 HIL-SERL 的 Human Intervention 有什么不同

> 本章属于后续接口设计参考，不是当前正式采集的前置条件。

看起来都是：

```text
policy 要失败
↓
human takeover
```

但是数据用途不同。

---

## DAgger

Human correction 被当成：

```text
expert supervised label
```

所以：

```text
human intervention segment
→ BC / SFT
```

---

## HIL-SERL / Online RL

整个真实交互被记录成：

```text
(s_t, a_t, r_t, s_t+1, done)
```

包括：

```text
policy success
policy failure
human intervention
human recovery
```

然后进入：

```text
RL Replay Buffer
```

供：

```text
Critic / Q
Actor
```

训练。

---

# 24. 为什么 failure 在 RL 里反而有用

SFT 里：

```text
错误 action
```

不能当 expert target。

但是 RL 的 Critic 要学：

```text
什么 action 好
什么 action 差
```

所以：

```text
policy failure
```

是有价值的数据。

例如：

```text
状态：
盒盖已经碰到盒体边缘

Action A：
继续硬压
→ failure
→ reward 低

Action B：
退一点重新对准
→ success
→ reward 高
```

Critic 才能逐渐学习：

```text
Q(state, B) > Q(state, A)
```

---

# 25. 进入 HIL / Online RL 后的数据结构

此时逻辑上只需要两个 buffer。

---

## 25.1 Demo Buffer

来自：

```text
高质量 human teleop
+
少量高质量 recovery
```

作用：

```text
给 policy / critic 一个可靠 prior
```

---

## 25.2 RL Replay Buffer

来自：

```text
policy rollout success
policy rollout failure
human intervention
recovery
reward
next state
```

作用：

```text
off-policy RL
```

例如：

```text
SAC-Flow + RLPD（当前优先）
其他 off-policy RL（后续对照）
```

---

# 26. DAgger 和 HIL 数据可以来自同一次 Takeover

例如：

```text
Policy
↓
开始出错
↓
Pico takeover
↓
Human recovery
```

这段数据：

```text
human action segment
```

可以同时具有两个用途：

```text
                 Human Intervention
                         │
             ┌───────────┴───────────┐
             ↓                       ↓
          DAgger                    HIL-RL
             │                       │
   supervised correction       RL transition
             │                       │
           SFT v2              Replay Buffer
```

第一版实验不用一开始同时全部上。

可以先：

```text
SFT → DAgger
```

跑通。

再上：

```text
HIL / Online RL
```

---

# 27. 未来加入 Simulation 怎么处理

第一版先不用。

以后如果增加 sim：

```text
Real Expert
+
Sim Expert / Sim Rollout
+
Real Policy Rollout
+
Human Intervention
```

这时候才真正需要认真考虑：

```text
不同 source 的 mixture
```

但仍然是：

> **同一个 phone_box task 内的数据 source mixture。**

不是 foundation pretraining 的跨任务 mixture。

第一版原则：

```text
Real Human Demo
= anchor

Sim
= 扩 coverage

Real Rollout / HIL
= 修真实部署 failure
```

具体比例以后再通过实验决定，不提前规定固定数字。

---

# 28. 推荐的最小文件 / Metadata 结构

物理上不用把数据复制很多份。

逻辑上保留：

```text
phone_box_dataset/
│
├── raw/
│   └── 所有原始 episode
│
├── metadata/
│   ├── episodes.csv
│   ├── scenarios.csv
│   └── dataset_manifest.yaml
│
├── manifests/
│   ├── train_v1.txt
│   ├── validation_v1.txt
│   ├── sft_v2_dagger.txt
│   ├── demo_buffer.txt
│   └── excluded.txt
│
├── evaluation/
│   ├── frozen_scenarios_v1.yaml
│   └── results/
│
└── lerobot/
```

`episodes.csv` 第一版：

```csv
episode_id,task,outcome,data_validity,behavior_type,source,scenario_id,failure_stage,invalid_reason,split,schema_version,dataset_version
0001,phone_box,success,valid,clean,teleop,S001,,,train,1,phone_box_v1
0002,phone_box,success,valid,recovery,teleop,S002,,,validation,1,phone_box_v1
0003,phone_box,failure,valid,,teleop,S003,place_phone,,train,1,phone_box_v1
```

Pico 接管和 RL 开始后再增加：

```text
policy_checkpoint
intervention_mask
reward
```

不要现在一次做完。

---

# 29. 推荐的阶段划分

## Stage 0：Recorder / Teleop 验证

目标：

```text
确认数据链没问题
```

先采：

```text
10~20 episodes
```

检查：

```text
3 路相机内容、顺序、时间戳和丢帧
measured state 的维度、单位、关节顺序
16D executed target action 的维度、单位、顺序和范围
Pico raw command → 机器人 target action 映射
state / action / image 时间对齐
episode boundary 和异常终止
LeRobot / OpenPI 字段转换与回读
只用 train split 计算 norm_stats
```

这批甚至不追求最终训练效果。

只有本节全部通过，才进入正式约 100 条数据采集。

---

## Stage 1：第一版 Human Dataset

目标：

```text
训练出 π_phone_box_v1
```

采：

```text
约 80~120 条有效 demonstration
```

主力：

```text
clean
```

少量：

```text
recovery
```

保证 phone / box / lid 的初始 pose 有基本 coverage。

按完整 episode / scenario 划分 `train` 和 `validation`；不要随机拆帧。

---

## Stage 2：π0.5 SFT

```text
π0.5
↓
D_SFT_v1
↓
LoRA / Full FT（首轮实验决定）
↓
π_phone_box_v1
```

保持 `env_action_dim=16`、`model_action_dim=32`，不修改 π0.5 base 的动作头尺寸。

目标：

> 模型能基本完成完整任务。

不用追求生产级成功率。

---

## Stage 3：Real Rollout + Failure Analysis

按冻结真机评测场景让模型跑：

```text
30~50 次
```

统计：

```text
抓手机
handover
手机入盒
抓盒盖
盖盒
```

找真正 weak point。

---

## Stage 4A：DAgger

如果主要问题是：

```text
模型进入某些错误状态以后不知道怎么恢复
```

就：

```text
Pico standby
↓
必要时 takeover
↓
收 human correction segment
↓
D_DAgger
↓
SFT v2
```

---

## Stage 4B：RLinf SAC-Flow + RLPD

如果：

```text
流程已经会
但最后精细操作成功率仍不够
```

就进入：

```text
选定一个 weak stage（优先 place_phone 或 close_box）
↓
为该阶段固定 reset 与自动 reward / termination
↓
Demo Buffer + Online Replay Buffer
↓
SAC-Flow + RLPD
```

第一轮不直接对完整长程任务做 end-to-end online RL。π0.5 继续负责长程流程，SAC-Flow policy 先负责一个精细阶段。后续再比较：

```text
DAgger / Human Intervention
RLT（VLA feature + reference chunk）
更完整的 VLA online RL
```

---

# 30. 最终完整数据流

```text
                         Human Teleop
                              │
                              ↓
                         Raw Episodes
                              │
               ┌──────────────┼───────────────┐
               ↓              ↓               ↓
          clean success    recovery        invalid / valid failure
               │              │               │
               └──────┬───────┘               │
                      ↓                       │
                  SFT Pool                    │
                      ↓                       │
                  π0.5 SFT                    │
                      ↓                       │
               π_phone_box_v1                 │
                      ↓                       │
              Real Robot Rollout              │
                      ↓                       │
                 Failure Analysis             │
                  /             \             │
                 /               \            │
                ↓                 ↓           │
       Targeted SFT/DAgger   SAC-Flow+RLPD ←──┘
                │                 │
       Human Correction      Replay Buffer
                │            /           \
                ↓      policy rollout   demo buffer
             SFT v2            │             │
                │              └──────┬──────┘
                │                     ↓
                │              Online RL Update
                │                     │
                └──────────┬──────────┘
                           ↓
                      Better Policy
```

---

# 31. 这一版 Demo 只记住 8 条规则

### 规则 1

```text
一次完整执行 = 一个 episode
```

普通 VLA SFT 不需要逐帧人工标注。

---

### 规则 2

真人遥操天然存在轨迹差异。

```text
轨迹不完全一样 ≠ bad data
```

不要把正常的人类 variability 当异常。

---

### 规则 3

主动控制的是：

```text
手机 / 盒子 / 盒盖 / Robot init
```

不是人为设计各种不同 human path。

---

### 规则 4

初始 SFT：

```text
clean expert
+
少量合理 recovery
```

为主。

```text
failure / invalid
```

不当 expert label。

---

### 规则 5

不要一开始想象所有 long-tail。

先：

```text
SFT
↓
Policy Rollout
↓
让 Policy 自己暴露真正 failure
```

再补。

---

### 规则 6

后面一定分清：

```text
DAgger
=
Human Correction → Supervised SFT

HIL / RL
=
Success / Failure / Intervention
→ Replay Buffer
→ Reward / Critic / Actor
```

### 规则 7

```text
env action = 16D
π0.5 model action = 32D
```

第一版使用 padding / slicing，不为夹爪任务修改 π0.5 base 动作头。

---

### 规则 8

离线只划分 `train / validation / excluded`；最终 test 是冻结场景下的闭环真机评测。所有 checkpoint 使用同一套场景、次数和阶段指标比较。

---

# 32. 我们当前最实际的下一步

现在不用讨论 500 条、复杂 mixture 或生产级平台。

先完成：

```text
① 冻结 16D action 顺序、单位、范围和绝对/增量语义
② G2 + Pico 数采 recorder
③ 统一时钟、episode 自动编号、scenario 自动记录
④ episode 结束后标记 outcome / validity / behavior type
⑤ 采 10~20 条 sanity check
⑥ 检查同步、丢帧、动作映射、LeRobot/OpenPI 转换和回读
⑦ 冻结 train / validation 划分与真机评测场景
⑧ 正式采第一批约 80~120 条有效 demonstration
⑨ π0.5 SFT
⑩ 固定场景真机 rollout，统计完整与分阶段成功率
⑪ 根据真实 failure 决定 targeted SFT，或选择一个精细阶段进入 SAC-Flow + RLPD
```

这就是当前 Demo 最合理的最小闭环。
