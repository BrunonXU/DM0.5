---
created: 2026-09-14
updated: 2026-09-17
type: runbook
---

# F1示教replay：导出、现场运行与停止

入口为 [`scripts/replay.sh`](../scripts/replay.sh)。程序和数据分开：清洗完成后导出一段数据，
替换 [`configs/replay_selection.json`](../configs/replay_selection.json) 的 `bundle` 路径即可。
默认选择文件仍为空；保留000083的两机显式诊断选择配置，该条已因底盘标签错配撤出联合真机候选。具体候选、部署版本与现场就绪状态见[当前方案](training_plan.md)，不在本文重复维护。F1运行参数仍须核定。

v3数据使用版本化入口[`scripts/replay_v3.sh`](../scripts/replay_v3.sh)，部署在F1任务根目录`replay_v3.sh`。
`list`查看样本，`inspect 057`检查默认包，`dry-run 057`数值预演；编号可换成list中的其他来源。
该入口显式绑定客户端版本和数据目录，自动加载ROS模式所需agv环境，保留默认不动作及原执行检查。
当前执行参数位于`runtime/replay_sets/runtime_fix_20260916/replay_f1.json`；旧配置保留。
先在F1任务根目录运行`./replay_v3.sh preflight 057`，预检成功后运行`./replay_v3.sh execute 057`，现场按Enter开始。
入口固定使用F1现有CycloneDDS配置连接底盘，motor_ctl只读参数查询通过独立Fast DDS子进程完成；不改变现有驱动环境。

## 保存底盘起点与回放后归位

4F-F1/D071使用新增`trial_home.sh`同时保存本机姿态与里程计，详见[4F-F1操作流程](../../docs/robots/4F-F1_startup.md)。下面`base_home.sh`路径与旧发布属于D070历史，不直接用于新机。

`base_home.sh`保存本次开机的**局部里程计XY**，结束后反馈闭环平移返回；不是SLAM世界绝对位置，不修正朝向。
最高0.10m/s、软件加速度上限0.08m/s²；误差2.5cm以内且持续停车才报告到位，阈值不是外部定位精度保证。
`status/capture`不发布动作；`return`现场Enter后才创建底盘发布者，不控制双臂、腰部、夹爪，不自动切换控制模式。

**F1终端，先摆好相应样本的机器人地面起点和物料：**

```bash
cd /home/user/Bruno/F1/foam_sort_whole_body
./base_home.sh capture &&
./pose_review.sh to-start 003 &&
./replay_review.sh preflight 003 &&
./replay_review.sh execute 003
```

003可换020、096、093、192；这是同一泡沫任务不同示教样本。每条使用自己的身体/双臂/夹爪起姿配置。
020/096的新起姿配置显式选择HAL与首帧目标容差的交集；两项2°检查及硬限位不变，其他样本沿用原HAL目标。见[修复依据](../results/pose_start_intersection_20260917/README.md)。
`pose_review.sh`沿用旧起姿实现，**未修复已有身体震动问题**；出现明显震动Ctrl-C，未停止则现场急停。
058和135有未决数据区间，未加入本入口。057仍保留旧28秒入口；不能用旧入口来验45.233秒候选。
新样本包为诊断执行候选，不表示距离/时序跟踪已验收。

结束后，确认低速直线返回路径及双臂扫过范围无障碍，在F1终端**另行**执行：

```bash
cd /home/user/Bruno/F1/foam_sort_whole_body
./base_home.sh status
./base_home.sh return
```

第一次真机归位运动仍需现场验证；没有绕障规划。角差大于2度或距离大于3米拒绝；不自动旋转找方向。
新起点保存会保留上一份备份，但不要在回放结束后capture，否则保存的是终点。
没有回放前保存的起点就不能恢复“刚才之前”的位置；脚本不会猜测。控制器重启、里程计重置或机器人重启后重新保存起点；备份有效期1小时。
归位仅为下一次测试的辅助操作，不插入训练动作、不计入原回放验收。

日志：F1 `runtime/base_home/runs/<UTC_PID>/`；当前起点`runtime/base_home/start.json`。
源码/发布/实测依据见[归位发布记录](../results/base_home_delivery_20260917/README.md)。

## 显式同步重定时诊断

`--retiming <计划JSON>`仅用于已绑定源包的诊断执行，将移动阶段的双臂、夹爪与底盘同步重定时，并记录原时间映射。默认原时序模式不变；下文“不插值、不改变原时间”的说明针对默认模式。重定时生成帧不是新的训练源，距离需要现场另验。当前057候选的版本、入口、验证范围和现场步骤见[候选报告](../results/replay_retiming_057_20260916/README.md)，统一进度见[当前方案](training_plan.md)。

## 回放什么

按源时间发送19D：左臂7、右臂7、左右夹爪2、底盘vx/vy/wz。
双臂硬件度数原值映射到 `arm_l_j1..7`、`arm_r_j1..7`；不再次做Placo变换。
保留等待和最后一帧的真实持续时间，不插值、不删帧赶进度。
**按用户2026-09-15确认，双臂超限目标截断到核定上下限。** 原始数据保持不变；dry-run与execute共用同一映射，
分别记录`source_action`、`command_action`和`action_adjustments`。不对夹爪或底盘速度做隐藏裁剪。
实际控制器限位由当前运行二进制SHA绑定，不能用旧launch参数代替。
21D状态用于起始姿态核对；本程序不驱动身体/头部，所选片段的body5须保持在核定容差内。

| 入口 | 行为 |
|---|---|
| `inspect`（默认） | 检查数据完整性、字段、源时间；列出未核定参数，无ROS |
| `dry-run` | 导出逐帧数值命令和报告，无ROS、不动作 |
| `check-env` | 检查现有ROS Python依赖，不创建节点 |
| `feedback` | 只读订阅反馈、查看命令发布者和motor_ctl参数，不创建控制发布者 |
| `preflight` | 一次汇总数据、映射、参数、反馈、起点和命令发布者问题；失败不会发送动作 |
| `execute` | F1现场Enter确认、倒计时、录包后按时间发送；Ctrl-C停止 |

数值预演与真机执行使用同一数据包reader；它**不是正式训练reader的三路图像验收**。
训练样本、图像对时和任务结果仍按[清洗验收文档](dataset_acceptance.md)检查。

## 清洗完成后替换数据

在6000D使用已有NumPy环境，选择连续有效区间。下例路径和帧号需换成实际选择：

```bash
python scripts/export_replay_bundle.py \
  --aligned-npz /path/to/cleaned/episode.npz \
  --cleaning-version CLEANING_VERSION \
  --start-index START --stop-index STOP \
  --output /path/to/new_bundle
./scripts/replay.sh inspect --bundle /path/to/new_bundle
./scripts/replay.sh dry-run --bundle /path/to/new_bundle
```

默认读取同名JSON；区间为 `[START, STOP)`，源 `time_ns[STOP]` 必须存在，用作结束边界。
存在无效state/action mask时拒绝整次导出，不跳帧拼接；未知尾部不补齐。
产物只有 `manifest.json` 和 `frames.jsonl`，包含来源/清洗版本/SHA256，F1读取不依赖NumPy。
导出状态为 `candidate`；数据复核完成后记录依据并标记 `replay_ready`，不等于真机验收成功。

将数据包单独放入F1的 `/home/user/Bruno/F1/foam_sort_whole_body/runtime/replay_bundles/<版本>/`，
在新发布配置中把 `bundle` 指向该目录。源代码同步遵循[F1发布流程](../../AGENTS.md)，
既有部署先保存现场改动和备份；不用覆盖旧包来切换数据。

## F1现场运行

复用系统Python3、ROS Jazzy和已安装的 `move` 消息工作区；不复制6000D的venv。
脚本按需加载现有 `/opt/ros/jazzy/setup.bash` 和 `/home/user/workspace/f1/install/setup.bash`，
保留现有DDS配置。2026-09-14只读核实本机move消息来自`/home/user/workspace/agv/install/setup.bash`；现场运行`check-env/feedback/execute`前，另行`source`该已安装环境。默认f1工作区本身不提供move消息。
可通过 `F1_REPLAY_PYTHON`、`F1_REPLAY_ROS_SETUP`、`F1_REPLAY_WORKSPACE_SETUP` 指定现有路径。

```bash
cd /home/user/Bruno/F1/foam_sort_whole_body/current
source /home/user/workspace/agv/install/setup.bash
./scripts/replay.sh check-env
./scripts/replay.sh feedback
./scripts/replay.sh inspect
./scripts/replay.sh execute
```

首次执行前核定 [`configs/replay_f1.json`](../configs/replay_f1.json) 的硬件来源与任务软件策略，填写证据。
本版限位、控制器版本、夹爪类型与模式已有核对记录；时序、新鲜度、起点容差、停车预算属于任务软件策略。
31Hz是派发间隔的上限，源仍为30Hz，保留10ms迟到预算与禁止跨帧补发；0.65m/s是本批软件拒绝上限，
不是硬件最大速度。当前直线轨迹wz为0；旋转任务需另核配置。当前OmniPicker实现忽略max_effort，0只是协议占位，不能作为限力手段。
这些数值不能从供应商示例或旧launch直接当作实机结论；读取ROS参数也不证明SDK临时模式未被切换。
程序检查起点、反馈新鲜度、软手动/驻车状态、控制器订阅和其他命令发布者，不自动归位或切控制模式。
软件配置`verified`和数据`replay_ready`都不表示已经真机验收；execute会重新检查现场条件。
现场需还原机器人位置、物料和场景；当前没有SLAM起点自动核对。

故障原因、处理与未关闭项统一见[回放故障与经验](incidents/replay_failures_and_lessons.md)，不要根据旧报错反复更改保护阈值。

## 停止与结果

### 起始姿态准备与恢复

**2026-09-16现场更新：057已到位，但起姿运动明显震动、静止后停止。身体起姿平滑性未验收，暂不重复下面的`to-start`或`restore`。当前先用只读preflight核对已到位状态；19D正式回放不下发body5轨迹。约29.8Hz、身体每步接近0.1°是待修正的平滑性疑点，不能用目标到位证明动作平稳。见[记录](../results/deployments/20260916-recorder-ready-v1/README.md)。**

`replay_v3.sh`仍只回放19D。另有`pose_v3.sh`负责现场慢速调整双臂14轴、身体5轴和双夹爪；
身体按HAL具名电机角度同时插补，经已核对的`motor_ctl` ROS入口及其内部UDP路由执行。
不是升降按钮近似，不绕过现有限位；头部读数附存但不驱动，底盘只发送零速度，不恢复地面位置。

用户要求保留的最初基线为2026-09-16 11:38:07（北京时间），两机均有原始反馈及可恢复JSON。
F1固定路径：`runtime/pose_backups/20260916T033807Z_3577f269/baseline_pose.json`。
恢复入口绑定此文件SHA，不会被后续status/capture或运行前自动备份替换。

在F1任务根目录：

```bash
./pose_v3.sh status              # 只读比较，并保存本次读数
./pose_v3.sh plan-start 057      # 只生成当前→057的插补计划
./pose_v3.sh to-start 057        # 现场Enter确认，慢速到057起姿
./replay_v3.sh preflight 057 && ./replay_v3.sh execute 057
./pose_v3.sh restore             # 现场Enter确认，恢复11:38保存的关节/身体/夹爪
```

开机常驻的复位、升降和旧回放客户端可以待机共存，不因注册发布者而要求退出。当前入口采用实际命令监听：先观察静默，执行时区分自身RMW来源与其他来源；任何外部命令（包括零速度）会中止本次任务。回放期间不要触发遥操作、复位或升降。监听不是控制器仲裁，不能保证外部第一条命令在被检测前未被硬件接收；需要同时运行两套控制时应另做控制器侧仲裁。诊断与验证见[命令占用修复记录](../results/control_ownership_20260916/README.md)。
每次运动前在`runtime/pose_runs/<ID>/before_move_pose.json`再保存一份，随后写plan、events和report。
按用户要求，当前准备/恢复速度为身体峰值≤3°/s、双臂≤6°/s，夹爪仍≤10单位/秒；历史起姿计划14.43秒，现已到位；该速度与步进组合尚未通过现场平滑性验收。旧1°/s身体策略已被替代。
这不是碰撞规划，现场须检查整个动作路径空间。body3/4/5用同一插补比例，不能逐轴先后搬动。

起姿/恢复使用生成轨迹专用调度：短时调度抖动只延长总时长，逐点继续，不跳步、不追赶；连续卡顿或发送阻塞超过200毫秒停止，反馈过期、跟随误差和外部命令检查仍保留。正式录制replay继续按源时间轴执行，未因此加速或改为可延长时间。详见[起姿调速与调度修复](../results/deployments/20260916-pose-paced-v1/README.md)。

到位必须同时满足示教HAL与首条已截断命令的既有容差，并保持稳定反馈；不能只以命令发完为成功。
Ctrl-C或异常时尝试底盘零速和新鲜双臂＋身体19关节保持，不自动松夹爪，也不立即跳回基线。
若中断后要回原姿态，现场另运行restore；保存姿态不能代替机器人/物料在地面上的复位。
详细证据与数值验收见[姿态准备记录](../results/pose_setup_20260916/README.md)。

### 回放停止与日志

正常结束、Ctrl-C及运行异常都会尝试持续发送底盘零速度，以新接收的速度反馈确认停车；
有新鲜且未超限的臂反馈时发布当前角度保持，不自动松夹爪。保持命令发出不等于测得机械臂已停止。
丢失通信、进程被强杀或控制器异常时不能保证软件停车，需现场处理；程序退出本身不作停车证明。

F1输出位于任务的 `runtime/replay/<运行ID>/`：`events.jsonl` 记录实际发布、源时间、反馈与异常，
`rosbag/` 录制命令、HAL、夹爪、底盘反馈和三路相机，`report.json` 单列时间轴完成与停车反馈结果。
录制启动需确认数据库topic与录制订阅齐全；仍需事后检查录包内容和实际任务效果。
6000D数值预演默认输出到 `results/replay_preview/`。本地合成测试不代表F1依赖或真机动作已验收。
2026-09-14：66项离线测试与导出→脚本预演检查通过，见[验证记录](../results/replay_client_20260914/validation.json)。

## 现场验收项目

使用已核对的初始双臂、身体姿态和场景，选完成验收的完整循环；由现场操作者启动。
记录实际发出的臂/夹爪/底盘命令、HAL反馈、三路图像、真实 `/move/State`、模式/驻车/避障状态；
若有SLAM定位同时记录，作为诊断数据，不因此改变本轮VLA输入或导航策略。

先核对接口方向与跟随关系，再执行选定整轮，输出关节跟随误差、夹爪事件、底盘实际移动/停车时刻和任务结果。
停车、结束与中断时的零速度发送及控制器超时行为必须明确，不假设进程退出就已经停车。
底层发生限幅、避障介入或控制权切换时保留原日志，不能把改变后的行为算成原动作精确回放。
具体跟随/位置误差阈值依据F1实际控制规格与任务余量确定；文档未给出时不虚构一套硬件指标。

回放若失败，先定位是数据标签、映射、时序、底层跟随还是场景初始状态；回放通过也只验证本次示教与执行链路，
不等于模型泛化成功。完成这些后再进行小集过拟合和正式训练。


字段及单位的依据统一见[动作接口合同](action_interface_contract.md)，本文只维护运行与现场验证。
