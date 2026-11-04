# G2饮料瓶分拣：模型、FK与Pink IK决策

状态：**厂商模型结构审计通过；75帧FK样本已获授权传入G2并完成frame审计；
相对微增量Pink IK严格回放1200/1200通过；绝对抓取frame、Live Shadow和Powered仍未验收。**  
范围：DM0.5饮料瓶分拣中的小幅人工纠正，不覆盖导航、底盘或通用全身遥操作。  
总流程见`G2_F710_DAGGER_FULL_FLOW.md`，Windows通信操作见
`WINDOWS_TO_G2_SHADOW_RUNBOOK.md`。

## 1. 决策摘要

1. Pinocchio是唯一运动学模型、FK、Jacobian与SE(3)积分基础；
2. Pink作为正式差分IK的QP任务/约束层，不与Pinocchio二选一；
3. 裸Pinocchio damped least-squares仅作基线、诊断和结果交叉检查；
4. 从完整O10 MJCF生成左右两个fixed-base 7DOF reduced model；
5. 同一时刻只允许一侧求解，另一侧严格保持接管瞬间的最后成功目标；
6. 手臂IK只产生14D中活动侧7D候选；O10不进入IK，继续使用每手`g/c`；
7. 手柄先表达base frame速度，不能直接解释为关节或绝对pose；
8. 未通过本文件所有Shadow门槛前，`powered_allowed`必须为`false`。

2026-08-28新增的关键决策是：接管目标必须由**接管瞬间的实测7D关节状态做FK初始化**，手柄只
积分相对增量。历史H5用于验证关节顺序、左右方向、四元数和可达性，不作为当前真机的绝对世界
坐标标定。这样既符合regrasp/correction的小幅微调，也避免把跨机器人或旧标定偏置带入控制。

Pink官方定义为基于Pinocchio和QP求解器的差分IK，输出关节速度；FrameTask可跟踪末端frame，
Posture/Damping类任务可正则冗余自由度，位置和速度限制可进入QP不等式：

- https://stephane-caron.github.io/pink/introduction.html
- https://stephane-caron.github.io/pink/tasks.html
- https://stephane-caron.github.io/pink/limits.html

## 2. 厂商模型证据与使用边界

G2当前`model.urdf`链接到`G2_t2_crs`。基础URDF包含正确的双臂关节：左`idx21–27`、
右`idx61–67`。完整O10结构来自`G2_t2_crs_o10_t2_flat.xml`，碰撞禁用对来自对应SRDF。
具体路径和SHA256在`configs/g2_kinematics_model.json`中冻结。

2026-08-27使用G2自带Pinocchio 3.7.0只读加载完整MJCF，观察到：

```text
nq=69, nv=68, njoints=64, nframes=158
```

模型包含对称的`arm_{l,r}_link7`、`hand_{l,r}_base_link`和
`hand_{l,r}_middle_tip`。锁定非左臂关节后得到严格`nq=7,nv=7`并保留左手frame；右侧采用
相同策略。完整MJCF还存在一个必须记录的命名不对称：有`arm_l_end_link`，却没有对应的
`arm_r_end_link`；两侧`hand_*_base_link`都相对joint7平移0.095m。因此合同选择对称的
`arm_*_link7`作法兰参考、`hand_*_base_link`作掌心参考，禁止从`end_link`名字猜测控制frame。
这些结果只证明结构可解析，**不证明模型与实测pose数值一致**。

厂商模型默认在G2原路径原地读取。仓库不默认导出模型内容，以避免无意复制专有资产。若未来
明确授权保存副本，必须保留来源、版本、哈希和访问边界；mesh只在碰撞验证确实需要时处理。

## 3. Operational frame决策

腕部`arm_*_end_link`不是理想的人机控制点；饮料瓶抓取需要控制四指与拇指之间的瓶颈抓取
中心。最终应添加左右虚拟frame：

```text
left_bottle_grasp_frame
right_bottle_grasp_frame
```

它们作为`hand_*_base_link`的固定偏移，不修改厂商源文件。偏移通过O10几何、历史抓瓶pose和
现场Shadow可视化确定。在偏移冻结前，`candidate_frames.operational_*`保持`null`。

第一版手柄语义统一在机器人base frame：`+X`前、`+Y`左、`+Z`上。左右手使用同一世界语义，
避免换侧后摇杆反向。最终符号必须通过FK可视化确认，不能仅按轴名称推断。

## 4. FK验收

使用与DM0.5训练合同一致的原始H5，读取：

```text
state/joint/position
state/end/position
state/end/orientation
```

原始文件实际采用“每帧一个HDF5 group”的结构。2026-08-27已只读确认每帧同时存在14D
`state/joint/position`、5D `state/waist/position`、base position/orientation及两侧end
position/orientation；base静止四元数末位约1，支持源顺序为`xyzw`的候选解释。FK配置必须纳入
base与腰部，不能只把14D手臂塞进完整模型。`scripts/06_export_h5_fk_samples.py`只导出均匀抽样
的数值状态，不导出图像、action或完整轨迹；`scripts/07_g2_fk_candidate_audit.py`在G2本地用
厂商模型比较腕部和掌心frame。

当前抽样产物覆盖三个episode、每条25帧，共75帧、约133KB；位于服务器项目的
`data_staging/dm05_fk_samples_3ep_75.json`，该目录不进入Git。用户已对该文件和目的地单独
明确授权，现已传入G2：

```text
/home/user/xza/G2Teleop/data_staging/dm05_fk_samples_3ep_75.json
sha256 d11f1315469229d859809339d9290cd466aa7e42af732b17cce143069d42ce77
```

`scripts/07_g2_fk_candidate_audit.py`与`scripts/08_g2_fk_fixed_frame_fit.py`的结果确认：

- `state/end/orientation`是`xyzw`，不是`wxyz`；错误解释的姿态P95约88度；
- `state/end/*`两侧顺序是`[left,right]`，没有交换；
- `state/end/arm_*`不是可与7轴腕部FK直接比较的笛卡尔真值，禁止误用；
- 两条`sorting_*`来自同一AID `G2A0004C203358`和同一软件版本；UUID episode来自另一台
  `G2A0004C202013`，必须排除跨机器绝对frame拟合；
- 同AID的左侧固定偏置姿态非常稳定，但位置P95仍约6.9--11.4mm；右侧约18.5--20.1mm，
  未达到2mm绝对frame门槛。

因此当前结论是“运动学结构、顺序和方向可信”，不是“绝对`bottle_grasp_frame`已标定”。
`candidate_frames.operational_*`继续保持`null`，Powered不得引用H5拟合偏置。

先证明记录的end pose究竟对应腕、掌心还是其他frame，并确认四元数顺序、base frame和单位。
样本必须覆盖左右臂工作空间，而不是只检查home pose。

需要输出：

- 34D到Pinocchio joint name/index的显式映射；
- 每侧位置误差与旋转角误差的p50/p95/max；
- 最差样本的关节、记录pose和FK pose；
- 左右镜像符号、limit和fixed transform审计；
- 模型哈希、数据集标识和脚本版本。

初始验收候选值是p95位置误差不超过2mm、p95旋转误差不超过1度；如果记录pose本身含滤波或
frame偏移，先解释系统误差再冻结阈值，不能调高阈值掩盖frame错误。

## 5. Pink差分IK结构

每侧独立求解器包含：

- FrameTask：活动`bottle_grasp_frame`的6D目标；
- PostureTask：偏向takeover时的实测姿态，防止7DOF冗余漂移；
- DampingTask或LowAccelerationTask：抑制速度抖动；
- ConfigurationLimit：关节位置限制；
- VelocityLimit：项目内更保守的关节速度限制；
- AccelerationLimit：限制启动、换向和停止冲击；
- workspace barrier：限制瓶子工作区和身体附近禁入区；
- 奇异性/条件数诊断：异常时HOLD，不用增大速度“冲过去”。

手柄50Hz意图先变成有界twist，再以实际`dt`积分目标pose。IK产生关节速度/位移候选，随后做
独立finite、step、joint limit和FK round-trip检查。候选不直接创建GDK Robot；只有现有唯一
G2 Control owner可以在未来授权后组合14D并走normal arm servo。

第一轮Shadow候选上限：平移20mm/s、旋转10deg/s。第一次Powered还要额外限制为单次1–2mm
或单轴极小角度，这些数值在实测前都不是批准值。

## 6. QP依赖与部署

G2系统原有Pinocchio 3.7.0、NumPy和SciPy。2026-08-28已验证并锁定：

```text
pin-pink       4.3.0
qpsolvers      4.13.0
proxsuite      0.7.3
solver         proxqp
```

服务器x86隔离环境还必须固定`cmeel-urdfdom==4.0.1`和`cmeel-tinyxml2==10.0.0`，否则浮动到
6/11会与Pinocchio 3.7.0所需ABI不兼容。精确合同见`configs/g2_pink_ik_shadow.json`和
`pyproject.toml`的`kinematics-ik`extra。

G2没有`ensurepip/python3.12-venv`，禁止为此安装APT包。实际部署使用：

```text
/home/user/xza/G2Teleop/vendor_ik
```

`pip --target --no-deps`只安装Pink、qpsolvers、ARM64 ProxQP及两个纯Python辅助包；运行时用
`PYTHONPATH=vendor_ik:src`复用G2系统Pinocchio，没有覆盖或修改系统Python。失败的标准venv尝试
没有安装任何包，后续也不得以此为理由运行`apt install python3.12-venv`。

依赖验证与回放流程是：

1. 服务器真实Pin/Pink/ProxQP单元测试23/23通过；
2. G2 ARM64 wheel导入通过，`qpsolvers.available_solvers == ['proxqp']`；
3. `scripts/09_g2_pink_ik_replay.py`只加载厂商MJCF和授权数值样本，不导入GDK；
4. 每个takeover姿态分别构建锁定当前base/waist/另一臂的7DOF reduced model；
5. 对左右手掌frame执行三轴正负1mm和三轴正负1度相对目标。

严格全量报告位于G2：

```text
/home/user/xza/G2Teleop/logs/pink_ik_replay_sorting_all50_strict.json
rows_sha256 175c06db6eb047c2539dd5fff8d9aa334896a06e12ce73dcd5bf40795240264e
```

两条同AID sorting episode的50个姿态，共1200个trial全部通过0.1mm/0.05度阈值。求解时延
P50/P95/P99/max分别为0.230/0.599/1.211/5.460ms；平移最多5次迭代、旋转最多8次；累计
关节变化最差约0.0223rad，单步继续受0.01rad和0.35rad/s双限位。

不能仅因为某backend在x86服务器最快就选择它；G2目标架构、wheel可用性、最坏求解时间和失败
语义同样是验收项。

## 7. O10 `g/c`与IK边界

Pink只控制双臂14D。每只O10仍为两个操作量：

```text
g_dot = RT - LT
c = LB held
```

从DM0.5饮料瓶数据分别拟合左右手：`g`沿历史开合主轨迹移动；`c`主要形成thumb roll/abad
避障残差。四指abad固定、四指屈曲同步但允许各关节有不同标定曲线。最终仍输出左右各10D，
通过10Hz `move_ee_pos(group=dual_tool,target_type=o10_t2)`发送并平滑。

## 8. 传输与状态机遗留项

第一次Windows→G2 Shadow中协议、sequence和HOLD正确，但日志出现4次100ms watchdog。Powered前
必须统计包间隔和ACK RTT的p50/p95/p99/max，区分正常关闭、Windows调度、Wi-Fi/SSH抖动和程序
阻塞。不能直接通过放宽watchdog掩盖卡顿；最终阈值需同时满足误停频率和故障制动时间。

DAgger接管还必须实现：RB上升沿丢弃旧policy suffix；锁定非活动侧；记录原策略动作、人工意图、
IK候选、执行动作和intervention mask；RB释放后重新采集观测并请求新chunk。

### Live IK Shadow状态合同

DM0.5现有`G2RobotController`必须继续作为唯一GDK `Robot()` owner。它的严格`read_state()`已提供
训练合同34D，新版process pipeline也已有control-owned timestamped shared ring；不得为了IK再建
一个Robot reader。

但是34D不含腰部5D，而手臂Jacobian和base-frame方向依赖实际腰姿。Live Shadow需要由同一owner
在一次`get_joint_states()`结果中发布最小运动学快照：

```text
sequence + sampled_at_monotonic + waist5 + arms14
```

G2Teleop只消费该快照并输出IK候选日志。快照缺字段、重复关节、非有限、过期、sequence倒退或腰姿
越出冻结任务范围时一律HOLD。禁止用H5 nominal waist静默补值；base在本任务中保持固定，若未来
允许底盘/躯干运动则合同必须显式扩展，不能沿用本版本。

## 9. 分阶段验收门

1. `MODEL_STRUCTURE_PASS`：哈希、关节、frame和左右7DOF reduced model通过；
2. `FK_DATASET_PASS`：**部分通过/绝对frame阻塞**；顺序、方向、姿态链通过，绝对位置未到2mm；
3. `IK_REPLAY_PASS`：**已通过相对微增量严格回放1200/1200**；
4. `LIVE_IK_SHADOW_PASS`：读取真机状态但绝不下发，方向和时延通过；
5. `HAND_SYNERGY_PASS`：左右`g/c`离线回放通过；
6. `POWERED_MICRO_AUTHORIZED`：用户显式授权、现场物理急停、单侧毫米级；
7. `DAGGER_COLLECTION_READY`：接管状态机与记录合同通过。

任何一门未通过，都不得跳到Powered。
