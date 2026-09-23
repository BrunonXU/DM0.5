# G2 Robot现场代码结构

2026-09-08 新增并同步 `scripts/23_capture_right_hand_pose.sh` 与
`operations/diagnostics/save_right_hand_pose.py`：复用hand_state_check只读采10帧，
保存原始双手报告及右手10D参考（含拇指和小指）。标记为反馈参考，不覆盖批准Home，
不重放开机零反馈、不发送动作。Shell语法、历史报告提取及43项运维测试通过；
当前开机姿态待操作者在G2运行采集。用户要求后续开关手迁移Servo，尚未在本次修改执行路径。

最近核对：2026-09-07（右食指名称修正诊断候选）<br>
G2当前部署目录：`/home/agi/xza/DM0.5`<br>
6000D机器人端源码：`/home/xza_100850113/projects/g2_robot`<br>
6000D模型训练/验证/服务：`/home/xza_100850113/projects/DM0.5`

2026-09-08 复位排查同步：仅更新 `robot_control.py` 的启动拒绝诊断与
`config/hand_home_approved.json`。健康门禁仍要求 mode=5/error_code=0；失败额外输出
官方 `error_msg`、两组 collision_pairs，不自动清错或切模式。右小指横向关节
`idx84_hand_r_pinky_abad_joint` Home 从 0.000775 调为 0 rad，保留调整来源和待实机确认
标记；差值仅约 0.0444°，不能据此认定明显外张根因已解决。43 项运维纯逻辑测试、
03_reset_home Shell 语法和 Home 无GDK预览通过。未执行真机。
现场 motion-control 日志显示 16:37:26.967 STOPED，16:37:57.565 进入规划，
16:37:59.318 返回 REACTIVE_CONTROL；这与用户先平板复位再继续相符，但尚未捕获
此次 MotionControlStatus 的 error_msg，不能给 error_code=2 猜测枚举定义。

## 目录职责

```text
6000D /projects/DM0.5
├── g2_sort/training/               # NewLora训练与HTTP推理配方
├── g2_sort/validation/             # 离线checkpoint验证与运动诊断静态页面
├── infra/host/                     # 6000D训练/推理启动脚本
└── docs/LoRAExperiments/NewLora/   # NewLora实验记录

6000D /projects/g2_robot
├── g2_sort/deployment/             # G2正式VLA客户端
├── g2_sort/operations/             # 只读诊断和受控操作
├── scripts/                        # 现场命令入口
├── tests/                          # 无GDK纯逻辑测试
└── docs/                           # G2操作、接口和状态文档

G2 /home/agi/xza/DM0.5
└── g2_robot的现场部署副本；logs只在G2产生并保留
```

`/home/agi/DM0.5`只保留历史日志/旧目录，不再作为代码入口，也不与当前目录合并，
避免覆盖现场证据。模型checkpoint和服务代码不复制到G2；正式Sync通过内网访问GPU7的
`ws://10.130.153.114:7892/process_frame`；原正式端口保持不变，只升级传输与所在GPU。

## 当前数据合同

```text
G2三路native JPEG/PNG + 34D measured state
→ 6000D NewLora 75K persistent binary WebSocket服务（Sync GPU7/7892；RTC GPU3/7893）
→ 模型内部：手臂14D relative、灵巧手20D absolute
→ 服务端反变换：手臂加回当前state，手保持absolute
→ 返回严格50×34 absolute action
→ G2基线同步客户端每次执行20/50步后重新观测
→ 双臂14D/C2 cubic spline/100Hz joint_servo_control
  + 双手20D/10Hz move_ee_pos（当前稳定基线；模型仍为30Hz语义）
```

RTC正式端口7893已在GPU3运行native-image binary WebSocket、五步compile和
`approx_identity/0.5` guidance；G2 RTC入口使用该协议和双手10Hz。服务已部署不等于
新配置通过Shadow或Powered，必须继续按门禁顺序验证：

```text
首个普通50步chunk → 逻辑第20步发RTC请求 → 推理期间继续旧chunk
→ 早到响应缓存到固定d=16，迟到/超窗按安全状态机处理
→ 客户端重投影C0，只平滑未承诺arm suffix并固定离散C1 → 切换
```

G2相关实现位于`g2_sort/deployment/g2_client/rtc.py`和`control_loop.py`的
`powered-rtc`状态机；`scripts/08_rtc_shadow.sh`只读，`scripts/09_powered_rtc.sh`
是唯一完整Split RTC带电入口。2026-08-21离线32对分组后参数改为
`H=50,d=16,s=20`，手部V1关闭。该d/s仍是旧候选，必须用新7893 Shadow延迟重新标定，
不能从Sync延迟直接决定。Powered RTC仍启用action safety gates，但按现场完整
效果测试决定，双臂0.05rad和双手0.35rad首跳在bootstrap与稳态重规划均只产生
`start_jump_warning`、不终止；稳态手部超限另记`rtc_hand_feedback_gap_warning`。
RTC边界已由已承诺逻辑前缀做C0投影，而O10低于模型30Hz的物理反馈会滞后于轨迹。首跳保护
不再代替人工急停；clip、动力学和运行故障hard gate不变。

2026-08-27把Sync已验证的双臂结论统一到RTC：08 Shadow、09完整Split和10 arms-only
均显式使用weight10与入口速度约束的C2 `cubic_spline`。bootstrap保留120ms blend，
稳态RTC已有C0/C1所以blend为0，避免每次切换人为减速再恢复。双手暂以10Hz启用
`rtc_cap_v2`，首条/内部最大变化为0.15/0.30rad；它只是共享手部轨迹器上线前的显式
基线，不采用V1.1的400ms尾部追赶状态机。正式日志tag为
`c2_spline_hand_rtc_cap_v2`。详见DM0.5
`docs/robot/experiments/RTC_SYNC_BASELINE_MIGRATION_20260827.md`。

7893现为五步fixed-prefix `torch.compile`、`approx_identity/0.5`。首次预编译
18.841秒；预热后本机归档HTTP smoke的baseline/RTC总耗时为0.185/0.159秒，模型
分别为0.140/0.139秒，switch arm max为0、hand max为0.0000477rad，未见recompile。

2026-08-21最新RTC控制周期对比确认线程版仍弱于Sync：RTC/Sync最大控制间隔为
24.14/14.96ms，`>15ms`间隔为68/0。长间隔与三相机采集/JPEG阶段重合，线程级yield
实验反而退化并已撤回。新09入口因此启用进程隔离：控制进程独占Robot、RTC安全状态机、
14D双臂100Hz和20D双手10Hz；spawn策略子进程独占Camera、只读state、native图片和binary WebSocket，
图片不跨进程。Policy不再创建只读Robot；Control是唯一Robot owner并按观测请求把
timestamped 34D state写入共享ring。09会在Powered授权前自动运行11单Robot-owner
只读Preflight。2026-08-24 G2日志已验证Control是唯一Robot owner、Policy worker是
唯一Camera owner；双进程RTC控制循环约97.56Hz、p99约13.17ms、最大约14.77ms，未再出现
超过15ms的控制长尾。该证据证明隔离与控制时序，不证明RTC任务质量已经超过Sync。完整设计见
DM0.5 `docs/robot/RTC_PROCESS_ISOLATION.md`。

训练与推理必须共用`14×JOINT + 20×GRIPPER`的state description和checkpoint内
`norm_stats.json`。G2客户端不做relative转换，也不需要持有checkpoint。

## 训练数据硬件隔离回放

`g2_sort/config/training_replay_v1.json`冻结一条train episode的948帧34D state/action；
`g2_sort/operations/hardware_tests/training_data_replay.py`只取前14D，通过normal通道
完整回放双臂，双手保持不下发；操作者入口是`scripts/07_training_data_replay.sh`。
该入口不访问6000D HTTP模型，结果写入G2 `logs/diagnostics/`。

## 同步规则

6000D修改不会自动出现在G2。每次只用`scp`覆盖本次确认变更的具体文件；不复制
`logs/`、缓存、checkpoint、训练数据或整个仓库。任何Servo、复位和powered动作仍由
Bruno在G2终端手动执行。

## 2026-08-28 Sync双臂近静止Minimum-Jerk快照

三轮161个可严格重建Sync chunk离线消融后，正式`05_powered_sync.sh`新增唯一
`stationary_minjerk_v1/0.0035rad`候选。它位于weight10、C2和120ms边界融合之后，直接
检查最终100Hz双臂目标：单轴至少反向两次、活动范围不超过3.5mrad且净位移不超过范围
60%时，替换成同起终点五次minimum-jerk曲线。它不修改双手、频率、模型waypoint或GDK
接口；底层CLI和RTC默认仍为`off`，只有正式05 Sync显式启用。日志文件tag增加
`arm_micro_minjerk_v1`，每个chunk用`arm_micro_motion_projection`记录触发轴、原始反向数
和最大偏差。

精确播放终点口径下，161-chunk近静止反向145→65（-55.2%），端点误差0、显著动作零
遗漏、方向到达延迟P95为0、chunk最大偏差P95为0.00290rad。完整本地纯逻辑回归
140 passed、1 skipped。以下6个运行文件已逐项scp到G2，远端源码compile、CLI参数、Shell
语法和SHA256通过；未初始化GDK或发送动作：

- `servo.py`: `6a42b38e91bc330fa1898df0b31ece4d46752143ebabeec1eab3931c6d7b68fa`；
- `control_loop.py`: `d6cb99e53421e03393f79cf639dcd5c20f321fee8895339a1fe24c31e4b3c073`；
- `cli.py`: `da3ad85521d8d3f1cc1aa20577dd2aca4e573029bd9ba02ba6285af257b5bdb8`；
- `app.py`: `e92fa52ed99e407f13d0d990d7ea498c5300ebe136d35016c1a6307ead0e5ddd`；
- `powered_split_runner.sh`: `d91dd38b3547e13fedabdd19cb60fcc0bc6f849db28b886aa2f0c8aba9260818`；
- `05_powered_sync.sh`: `85ed9088cccbe0f91a87f9a86bde443771fc4ee5e306c3fdbc47816605800115`。

首次Powered已连续完成21+39=60个完整chunk，零Servo/QP/安全失败。排除两轮首chunk后，
58个可严格重建chunk的同轨迹反事实为：冻结近静止小反向36→22（-38.9%）、全部小反向
179→165（-7.8%）；触发轴累计往返0.214154→0.039464rad（-81.6%），chunk最大偏差
P95/max为0.002606/0.003190rad，端点误差0、显著动作遗漏0、方向延迟P95为0。候选触发时
有效且没有用压制意图换观感，但未达到预设50% Powered效果门；正式Sync暂留候选继续
比较，不增至4mrad、不迁入RTC。剩余尖峰包含未触发的较大双臂动作和独立的手部事件。

## 2026-08-27 Sync共享Preview软约束修复快照

首次Powered并非时长结束：实际为100秒/150请求预算，第6个request刚开始时GDK拒绝
左手joint[5] `-0.000186366rad`，因其低于0物理下限而fail-closed停止。另发现startup后
首两条手命令仅隔49.60ms。初版1.75rad/s、7.5rad/s²、80rad/s³硬盒约束也会压制模型
快速意图，因此初版运行结论否决。

修正版仍使用唯一`shared_preview_v1` profile，但语义改为：只硬约束20D GDK物理位置；
速度/加速度/jerk阈值只做telemetry告警；轨迹由tracking、acceleration penalty 0.02、
jerk penalty 5.0、decay 0.2、跨chunk q/v/a和6步preview共同优化。10Hz deadline以
第一次实际成功发送时刻为基准，不能再缩短第一个100ms周期。正式日志tag改为
`c2_spline_hand_shared_preview_soft`。

同一6份169-chunk/321-event软约束复验得到：321事件零遗漏、jerk P95 14.60rad/s³、
tracking error P95 0.0932rad、最大步幅自然为0.1700rad、1519步零不收敛；penalty 10因
遗漏6个事件否决。6000D完整无GDK回归131 passed、1 skipped。4个变化运行文件已逐项
scp到G2；远端源码compile、纯数学位置QP、CLI/Shell和SHA256通过，未初始化GDK或发送动作：

- `trajectory_smoothing.py`: `6d5cd2b4ec7d1402c4dfa9027ea112327cf85461d5579ee07c44cf0deb9ac2d8`；
- `robot_control.py`: `ecaae4a8a517f2a3779e8c84067ed90adba9a5c84eb809369f604588897a8902`；
- `app.py`: `e28aaa6a9c68b6f9172842fa74d5af47aaa85abe195d8703bae968596b5597e1`；
- `powered_split_runner.sh`: `1ea6378a449b9b8b90d376181743dcf782f62f8df21ea5f5f262dcd586a956cd`。

修复版尚未再次Powered。首次只做有限Sync，先检查GDK零位置拒绝、首两条间隔约100ms、
`hand_derivative_constraints_enforced=false`和planner零不收敛，再讨论RTC迁移。

## 2026-08-27 Sync共享Preview手部轨迹器初版快照（已否决）

以下记录只保留追溯，已由上方软约束修复版替代。6份历史Powered Sync日志共169个chunk、321个关键半程事件的同轨迹离线消融淘汰P2P，
保留`preview_j80_jp0.1_d0.2`。运行时唯一命名为`shared_preview_v1`：Sync与RTC CLI共用
`CausalHandTrajectoryPlanner`，跨普通segment保留q/v/a，以未来6个10Hz参考滚动规划，冻结
1.75rad/s、7.5rad/s²、80rad/s³、acceleration penalty 0.02、jerk penalty 0.1和
preview decay 0.2。控制状态机显式把当前chunk未来参考交给手线程，不能退化成只看当前点的
伪preview；Sync等待时继续以旧终点作为参考并受同一约束趋近hold。

6000D本地完整无GDK回归为128 passed、1 skipped。正式`05_powered_sync.sh`已选择
`shared_preview_v1`，日志tag为`hand_shared_preview_v1`；09 RTC脚本本轮仍保持
`rtc_cap_v2`，先维持Sync单变量真机门。8个运行文件已逐项scp到G2；远端源码compile、
CLI `powered-sync + 10Hz + shared_preview_v1`解析、Shell语法和SHA256均通过，未初始化GDK、
Camera、Robot或Servo，也未发送动作：

- `trajectory_smoothing.py`: `faa003707ba2ebb7c134aaf68922fa693abb646873c78dc9bb509b86c18f7656`；
- `robot_control.py`: `9a2842a15f23e3d6dd51ae946f424ed5463f7d08495fb32ea2b079d5bdd034e3`；
- `telemetry.py`: `df74cdd29b65364c051bdf176a1df404a7b62c4fe980167783dc80f0a8f014a4`；
- `control_loop.py`: `e9db448f82af6531f862afe1500905e81a2fc8966e3327bc1b601def3d28b07e`；
- `cli.py`: `f436613871a9bb802ec2983a34776cf96eeabedacd82271069e9226e56aba356`；
- `app.py`: `49ae485d98666aeca650aad7ecd285c589fe9d5346f048d777327d5e471c1899`；
- `05_powered_sync.sh`: `6d9d9a17a8b7578eeb216e65e195c561bd38668a3a0519bbf52f8dc6e560d8a2`；
- `powered_split_runner.sh`: `53e036693ff4e4f4e9ad701ef21cee3289d26f0685183f93e5dd5805b73dda91`。

首次真机只运行10秒，保持7892 native binary WebSocket、双臂weight10/C2/120ms和手部10Hz
不变。新telemetry记录active/wait手部命令、规划收敛/最大迭代数及速度/加速度/jerk约束
命中；运行后先拉回日志再决定是否进入RTC，不直接continuous。

## 2026-08-27 手部V1.1同步快照

三批共123个10Hz Sync chunk完成手部离线连续回放。0.05--0.10rad强平滑候选因历史关键
动作遗漏被排除；V1.1固定为0.175rad单步、1.75rad/s、7.5rad/s²，方向反转hold只在
400ms边界生效。三批最大单步约下降12.5%、jerk P95下降约4%--16%，233个关键半程
事件0遗漏；一批tracking error P95增加约0.072rad，因此它只是一次10秒Powered候选。

正式Sync入口显式选择`v1_1`，日志名增加`hand_v11`；传
`--hand-smoothing-profile v1`可回退旧算法。已逐文件同步并完成远端Python/Shell语法、
CLI参数解析和SHA256核对；未初始化GDK、Camera、Robot或Servo，也未发送动作：

- `trajectory_smoothing.py`: `9e6880f945a06296e65881410cc2a1cb89a8f1c96fc132a3412b70a18507cdda`；
- `cli.py`: `51537940e6c68cb4537e82d83d0e5e44f44a607129f8118f2a1127b5969769cb`；
- `robot_control.py`: `40327098642b4ef7987ed11e0acb2c7e10f95a429595c1e7d648b3d839fefce7`；
- `powered_split_runner.sh`: `3cb6028afb03707c20fb1a22f896490bf1036dc3a89670b5e7b775fc170d68f8`；
- `05_powered_sync.sh`: `df522fc4096ff61920784c9abf52dd97a65983d04951d2e2f749111c5bcb11ce`。

6000D相关无GDK回归为106 passed。离线评估器和JSON产物只保留在6000D，不进入G2
运行路径。下一步只做一次10秒Sync单变量验证，RTC和Async本轮均未修改。

## 2026-08-27 Sync/Async C2插值同步快照

最新两组离线重建共82个可用weight10 chunk均显示C2 cubic spline的双臂jerk优于
linear、C1 Hermite、PCHIP和quintic。正式Sync与实验Async入口因此均显式传入
`--arm-interpolation-profile cubic_spline`，不再依赖低层CLI默认值。Sync保留
120ms boundary blend、jerk weight 10和双手10Hz；Async保留60ms、weight 0和双手5Hz，
使本次只改变双臂30→100Hz插值。

公共runner把实际profile写入启动信息和Python运行配置，并把持久tag加入日志文件名：
`cubic_spline → c2_spline`、`cubic_hermite → c1_hermite`、`linear → linear`。因此
Async的后续日志不会再与旧linear实验混淆。本次逐文件同步到G2并完成远端Shell语法、
`--help`与SHA256核对；未初始化GDK/Servo，也未发送机器人命令：

- `powered_split_runner.sh`: `adcb839dfc117435ac874c0052b764183bbdb7bbc1abb17f0c2b732da8b6eee7`；
- `05_powered_sync.sh`: `45b60d5f961a9067b5db0559c0fe9600228dc2583ae6b7b1d3ba4d58f9915245`；
- `06_powered_async_experimental.sh`: `60543fe21296b9323158f7e308b6b713e01cebff1f4a88e2c7a28b71db1a3344`。

远端`interpolation.py`与本地SHA256同为
`481686f5642369ce233eea8f996bdfe4c8fbf90c75a95b57b19e624c87354922`，已具备C2实现；
`servo.py`差异仅为手部频率说明文字，因此没有覆盖。6000D无GDK运行时回归为68 passed。
下一步只允许正式Sync 10秒单变量Powered验证；本轮没有修改RTC。

## 2026-08-25 binary WebSocket Shadow同步快照

该阶段（2026-08-25）6000D曾在GPU1和端口7896启动NewLora 75K、五步compile、50×34
binary WebSocket候选；7892 Sync和7893 RTC当时未修改。匹配HTTP对照只用于离线实验端口
7897，实验后保持停止。当前部署以本文件后面的2026-08-26快照为准。
固定三图N0-N3结果选择“原生GDK JPEG + persistent WebSocket binary + compression off”进入
G2 Shadow；该选择尚未进入任何powered入口。

G2已有binary协议、原生图片和WebSocket backend的同版本实现，本次只同步缺失的
`scripts/12_sync_binary_shadow.sh`，以及允许`shadow + rtc-process-isolation`的`cli.py`
单行入口差异。G2端SHA256分别为：

- `12_sync_binary_shadow.sh`: `7fa73b0f9d5dc7ed14a3036939708925fef46d1cf51f678cbddd4d7a6390af47`（默认400请求，约60秒以上）；
- `cli.py`: `ed57e32af93408c9b4fe8e3b27badb962ccb9eaa5e161ebeeeb4702582641310`。

同步后Shell语法通过，G2现有`websockets 16.0`可完成到
`ws://10.130.153.114:7896/process_frame`的握手。检查未读取相机、未构造Robot/Servo、未发送
任何动作。真实三相机与公司内网分布仍需Bruno在G2手动运行只读Shadow。

## 2026-08-25 binary正式Sync入口同步快照

N3真实Shadow运行68.37秒、348请求后，操作者决定跳过额外匹配HTTP Shadow，先在独立7896
候选上验证正式Sync；7892继续保留回退，7893 RTC不变。正式入口默认60秒模型动作时间，
先自动运行一轮双进程零输出Preflight，再要求人工输入`ok`和5秒倒计时。Control仍唯一拥有
Robot，Policy worker唯一拥有Camera并直接透传native GDK JPEG。

本次逐文件同步并在G2完成Shell语法、帮助输出、SHA256和7896握手检查；未初始化GDK或Servo：

- `11_rtc_process_isolation_preflight.sh`: `16f63a581a4b30980c0350649acaae141d6a4f58a960a65db6d958e0546829da`；
- `13_powered_sync_binary.sh`: `6bc50df7e10fca988042aeab6acc083fc048168862e8bf66130195706fdf630f`；
- `powered_split_runner.sh`: `ceb4dc24a540f935d8ff16cbc93667b10d5f2d9a650b9d3aa118e1b6ab7e8d6b`。

6000D无GDK相关测试为9 passed、1 skipped；skip来自本地Python缺websockets，G2现有16.0和
6000D候选镜像15.0.1均已通过真实握手/Shadow。真实powered动作仍只能由Bruno在G2终端执行。

## 2026-08-26 GPU7正式Sync、native WebSocket与手部频率快照

7892曾与他人OpenPI训练共享GPU4，10Hz Sync第15次请求的模型/CUDA段达到1.708秒。
服务一度收敛到GPU1，但外部任务再次使7896候选P50/P95退化到260/294ms；在GPU3/7释放后，
先把候选迁到GPU7。首次compile约19.73秒，暖态10次P50/P95/max为
158.5/182.3/188.1ms，模型段P50约139.7ms。

随后纯Sync进程移除RTC启动参数；最终按操作者要求恢复原正式端口7892，7896只保留为
已停止的历史候选。纯Sync
重新compile约20.77秒，5次暖态请求P50/P95/max为162.5/202.8/207.3ms，模型段P50约
142.3ms，`50×34 float32`合同通过，容器无重启或OOM。正式RTC仍使用7893，后续迁到GPU3。

正式`05_powered_sync.sh`现默认native GDK JPEG/PNG、persistent binary WebSocket和
GPU7/7892。O10手部30Hz出现强烈震荡、20Hz仍抖动，连同50Hz均已否决；正式入口回退
10Hz，并先在新网络链路上重复5秒确认。
G2只同步三份差异文件：`05_powered_sync.sh`、
`powered_split_runner.sh`和`cli.py`；binary/native模块与服务器版本原已一致。远端Shell、
Python、SHA256和WebSocket握手均通过，未初始化GDK、Servo或机器人动作。RTC脚本没有切换，
旧7893当前停止，等待本次Sync结果后再独立部署。此处记录的是当时快照；RTC WebSocket/10Hz
准备状态以本文件开头当前架构为准。

## 2026-08-26 RTC WebSocket与10Hz客户端准备快照

在不启动7893服务、不初始化GDK/Servo且不发送动作的前提下，RTC客户端入口已改为默认连接
原正式`ws://10.130.153.114:7893/process_frame`，并在完整Split RTC中显式传入双手10Hz。
`d=16,s=20`暂时保持旧候选；必须等GPU3/7893 WebSocket部署后先跑Shadow，以真实RTC
observation age重新标定，不能直接按Sync E2E换算。RTC手部仍使用边界/内部cap分离的
`rtc_cap_v2`，没有盲目移植Sync V1的整段追赶状态机。

本次逐文件同步到G2并完成远端Shell/Python语法与SHA256核对：

- `08_rtc_shadow.sh`: `65fc626b8eeae99c4263798ffa1021e3d340b2e341da521f2b9ce56d6e48c194`；
- `09_powered_rtc.sh`: `3c7f3afa3eddcbac14a77f66b6dfde4753d2d20cc90d9d88235a169d421a9f61`；
- `10_powered_rtc_arms_only_10s.sh`: `dd1eba4a7f50e54e37870c970ac24074bd2bb284e47cd89b7774a78703c16c35`；
- `11_rtc_process_isolation_preflight.sh`: `d8d24631d9f7cd586869160726ea37bcc702870bac0a69cafea1546df4c0e127`；
- `cli.py`: `a3c6bc654c2913ffce154584a6ff59d76b612b01e5dc6690ce2504ac354eccfc`。

6000D相关无GDK测试为76 passed。服务端GPU3/7893启动脚本也已准备native JPEG/PNG、
persistent binary WebSocket、五步compile和RTC guidance 0.5，但容器保持未启动；Powered
RTC继续禁止，先完成Sync内部jerk优化和RTC Shadow。08现在默认旧候选d16/s20并接受
显式`--d/--s`，日志名自动携带组合；这同时消除了此前Shadow d20/s25与Powered d16/s20
不一致的问题。

## 2026-08-27 Sync基线迁入RTC与等待窗telemetry快照

6000D只读核对确认GPU7/7892与GPU3/7893均已运行：两者使用同一75K、五步compile、
native JPEG/PNG binary WebSocket镜像，7893额外启用RTC guidance。没有新建端口或重启
容器。客户端08/09/10统一显式使用双臂weight10+C2 cubic spline；10的旧d20/s25改为
与当前候选一致的d16/s20。09日志名固定包含`c2_spline_hand_rtc_cap_v2`，明确手部仍是
临时cap基线，不冒充最终共享轨迹器。

`telemetry.py`新增quiet JSONL事件`hand_wait_command`：只在已完成一个segment、尚未
开始下一个segment时记录成功下发的10Hz手部命令，字段包含`phase=wait`、前一request、
command/raw target、相邻步幅、tracking error和各类限幅命中。它不打印高频控制台日志，
不改变tracker、目标或GDK调用。下一份Sync日志可以直接区分active与wait，不再反推。

6000D完整无GDK回归为121 passed、1 skipped；首次沙箱运行的5个localhost测试因禁止
创建127.0.0.1 socket报PermissionError，在允许回环socket后全部通过。相关Shell均通过
`bash -n`，Python通过`py_compile`。本次只逐文件同步运行代码、入口和现场文档，未远程
执行GDK、Camera、Robot、Servo或Powered动作。同步后逐文件拉回临时目录，SHA256与
6000D一致：`telemetry.py=05b25156...`、`08=357446f2...`、`09=5ff7d7c0...`、
`10=0146fc01...`、`scripts/README=87fa989a...`、`operator=db28c54f...`。

## 2026-08-24 精确三图与正式时长同步快照

G2只读`00_preflight.sh`现在从每路同一条GDK相机消息同时保存两种表示：与生产multipart
HTTP后端相同的Pillow quality95 JPEG，以及可供binary候选直接透传的原生JPEG/PNG字节；
`preflight.json`记录固定顺序、字节数、SHA256以及HTTP编码后解码像素误差。该预检不构造
Servo、不发送动作。正式Sync、实验Async和完整RTC入口省略
时长参数时统一默认60秒；`--continuous`仍由操作者以`q`或`Ctrl+C`停止。单请求guarded
smoke和固定arms-only隔离诊断继续保持短时，不作为完整任务评估。

已逐文件同步并只读核对G2端SHA256：

- `cameras.py`: `dc7c77aa333b88217a595e3f2117b8017b79bbc875a4997e34aa8abc456ac445`；
- `preflight.py`: `8a5ac5fca125604c0c6daaa630f723a19662ed97fdfebe0b03126c024090568c`；
- `09_powered_rtc.sh`: `155ce714cbea7b93f45a51cc948b3f684e2b0adbbbe5d9f64411528885eea0ef`；
- `06_powered_async_experimental.sh`: `06d03894cb0acafe080eb574d57f38ba694c240fbb0b98793aa48b86cbb063c5`；
- `powered_split_runner.sh`: `0a01b5a0f039f8675bfba1e2cd870bfce48d2280a4489fd01f750dc6be8e65ff`。

6000D已通过Python编译、相关JPEG单测、Shell语法与diff检查；同步过程未启动GDK、Servo
或机器人进程。新的精确三图档案仍需在G2执行一次只读`00_preflight.sh`后拉回分析。

## 2026-08-20 RTC同步快照

已将本轮RTC客户端修复逐文件同步到`/home/agi/xza/DM0.5`，未复制日志、模型或整个
仓库。G2端SHA256：

- `app.py`: `ad4744bcc7b9b9ddb3cecdb7f9cbaace2c7934775cd90022ca86be01dc3916c4`；
- `control_loop.py`: `7e34a8108f5d765bca87ecd4c39b01b5c06d68441ccf2009f6d83dfddbb70dbb`；
- `trajectory_smoothing.py`: `b4a2bf16560748fffb9ccc8cb6f3c5d9c00924f87145340621cc09c7ae2f5508`；
- `test_g2_client_runtime.py`: `34633b7ec30ed6db0148e460cc22e26e946d2f0678e638535fb3d31971e128fe`；
- `09_powered_rtc.sh`: `1ad757bae4e273448beda1dd959e613303fce3526dad4e9ab5b5922ef1541693`；
- `10_powered_rtc_arms_only_10s.sh`: `7aaf0e7d7965dc1f7abcc7655e625cb3dc04431b28123e6355d151e29331fb19`。

同步后只执行了无GDK测试：Python编译和`09_powered_rtc.sh`语法通过，
`tests/test_g2_client_runtime.py`为`50 passed`。最新Shadow与Powered共用fixed-d、C0和
suffix C1后处理，并额外记录seam、速度跳变和反转数。15:29双臂修复已取得4次真机
正证据，但hands hard gate拒绝完整系统；09现已硬封锁。下一步唯一入口10固定约10秒，
硬件层为arms-only且安全门只评价实际执行14D。同步验证未启动GDK、Servo或机器人进程。

## 2026-08-20 03复位顺序同步快照

`scripts/03_reset_home.sh --execute`的恢复顺序改为先双臂、后双手。恢复专用控制器启动时
关闭Split手输出闸门；双臂以100Hz回H5 frame-0中位Home期间不向O10发送命令。只有双臂
播放完成、最终Home保持且Servo无故障后才开启手输出，先重发最后成功手指命令作锚点，
再以5Hz最多八步回批准Home。生产客户端默认仍开启手输出，其他入口行为不变。

同步前逐行确认G2旧文件与本地差异仅为上述补丁。6000D容器内无GDK测试
`tests/test_g2_operations.py tests/test_g2_client_runtime.py`为`77 passed`，shell语法、
Python编译和diff检查均通过。同步过程未启动GDK、Servo、复位或机器人进程；真实03动作
仍需由Bruno在G2终端手动验证并握住物理急停。

G2端运行文件SHA256：

- `robot_control.py`: `c19ea6e7791523403eadbb3ec28b5991c22967db163485b638b52a612c62ae25`；
- `hand_home.py`: `d48a0e839be2246f7281901dfa0a3e29de57b84d05f832f4eb0b580e4df6b3ee`；
- `03_reset_home.sh`: `6250159cb4140125dd106f708d6b690a9bae683485af738422acc1e7feb68516`。

G2同步后shell/Python语法检查通过；现场已有的无GDK测试为`76 passed`。新增闸门顺序测试
只在6000D测试副本中，因此两端测试数相差一项，不代表运行代码差异。

## 2026-08-28 运动诊断仪表盘与反馈采集快照

新增`g2_sort/validation/build_motion_dashboard.py`与6000D的update/serve入口，用现有
NumPy、Pillow和静态HTML从日志生成倒序run页面；生成物只进入
`logs/analysis/motion_dashboard`。Sync的dispatch summary现保存14D逐条命令和segment
单调起点。

新增`joint_feedback.py`、只读`joint_feedback_rate_probe.py`和脚本14。没有通过probe时
Powered为command-only；通过后仍由唯一Controller在成功arm调用后读取，writer线程只写盘
不调用GDK。慢读、失败或连续发送长间隔只关闭采样。本轮已逐文件同步G2并通过Python编译、
Shell语法和76项无硬件运行时测试；尚未运行probe或执行任何真机动作，真实频率和Servo影响
仍未验证。

## 2026-09-02 F710 pre-dispatch时基与启动预检同步快照

已有direct timing-ab日志`20260901_181619`完整性通过，但真实dispatch P95/P99为
12.78/15.35ms；`dispatch interval`与相邻IK耗时差的相关系数为0.902。定位到第一版pacer
稳定的是IK开始时刻，而非真实GDK调用时刻。本轮将F710 Powered顺序改为
`Pink/OTG compute → pre-dispatch pacer → joint_servo_control`，并在dispatch边界重新检查
watchdog；越龄tick丢弃新解、保持上一手臂命令并释放IK/OTG，O10状态也不推进。接口、normal
碰撞保护、100Hz、速度以及Ruckig约束均未改变。

同时加入GDK初始化前的files-only preflight，提前显示O10零反馈文件锚点是READY还是
UNAVAILABLE。旧命令过期且不精确等于审核Home时仍保持安全拒绝，没有放宽6小时门限。
A/B分析器现在把完整结束、零drop、control sequence连续、无fault/watchdog/HOLD纳入候选门，
不再只凭百分位误判通过。

## 2026-09-02 完整数采Split回放的O10起点语义

`07_training_data_replay.sh --with-hands`用于隔离模型、网络、Pink和OTG：双臂把冻结的
30Hz数采action按连续时间线性采样到100Hz `joint_servo_control(normal)`，双手把同一
数采action以原始5Hz送入`move_ee_pos(dual_tool,o10_t2)`，不启用手部平滑。

O10重启后可能返回20D全零/近零反馈；在GDK命令空间中全零表示开手命令，不能被解释为
物理手指位置。完整数采回放因此只在该已知无效反馈形态下跳过`measured-vs-frame0`起点
误差门，直接以冻结数采frame-0命令作为手部回放锚点，并在JSON报告记录
`hand_start_source=training_frame0_zero_feedback_unavailable`。有效非零反馈仍执行原有
0.08rad起点一致性检查。948帧action、30Hz时间语义、双臂100Hz和双手5Hz均不改变。

本次仅逐文件同步`G2Teleop/scripts/12_g2_powered_f710.py`、对应Shell入口、
`19_analyze_f710_timing_ab.py`和两份操作文档；没有SSH启动GDK、Servo、复位或Powered动作。
6000D无loopback测试为`68 passed, 2 skipped, 1 deselected`；被排除的是当前沙箱禁止创建本机
socket的既有transport测试。pre-dispatch改动仍是未真机验证候选，下一次只能先跑direct
timing-ab验证dispatch时基，再决定是否进行OTG配对。

## 2026-09-02 G2Teleop共同轨迹因果回放同步快照

本轮只把G2Teleop的`eef_replay.py`、`control_telemetry.py`、20/21/22三个脚本和控制架构文档
逐文件scp到`/home/agi/xza/G2Teleop`。两端SHA逐项一致，远端Python编译和Shell语法通过；没有
复制整个仓库、厂商模型、训练数据或旧日志。

脚本20已移除`--execute`以及全部GDK/controller路径，只在G2原位MJCF上生成同一100Hz时钟的
Q-direct/Q-pink/Q-otg NPZ。脚本21是独立Powered读取端，固定arms-only，默认normal碰撞保护，
但本轮只对三个stage运行未加`--execute`的zero-output预检。脚本22只做Pinocchio FK和离散动态
审计。远程执行过程没有导入`agibot_gdk`、初始化Robot、发送Servo/O10、复位或Powered动作。

states产物`g2_bc_eef_causal_commands_20260902_164309.npz`为9123 tick/91.22秒，SHA256
`d1ef250c722a449fb47f431be03625af6f0af7a13039a43e42671ebec6cf90bd`；输入削顶、workspace、
target limit和telemetry drop均为0。Q-direct/Pink/OTG最大jerk分别为1466.10/116.57/20.00
rad/s³，小幅反转127/55/46。详细证据和OTG终点位置lag限制记录在G2Teleop控制架构文档14.5。
6000D无loopback完整回归为`78 passed, 2 skipped`。当前已停在第一个normal、arms-only、
Q-direct Powered门，必须由Bruno在G2终端手动执行并持物理急停。

第一次Q-direct人工Powered于17:21因`mode=5,error_code=2`在发送前安全拒绝。状态恢复后的17:22轮
固定normal、arms-only、20Hz feedback，操作者报告“非常丝滑”；但在artifact time 25.36秒由
Ctrl-C结束，只发送2537/9123次，主状态为`operator_ctrl_c`，不能登记为完整通过。已执行前缀中
dispatch P50/P95/P99/max为9.998/10.371/12.057/13.538ms，0次超过15ms；20Hz feedback 512条、
零drop，按约70ms lag对齐后关节误差MAE/P95/max约0.103/0.410/1.647mrad。下一次仍先完整运行
同一Q-direct至自然结束，再允许Q-otg配对。

操作者随后说明25.36秒停止的原因是后续sorting双臂路径若没有同步手型会碰撞，因此不得继续完整
arms-only。新`g2-causal-command-replay-v2`保持双臂states路径，但明确改用数据集actions作为20D
O10命令；旧v1中states hands与actions最大相差0.921rad，双手Powered入口会硬拒绝旧产物。新NPZ
`20260902_173215` SHA为`31537fe91ef96f1a9931bd4d33cedf51f344a757b2a21810ee67978965c54984`。
10Hz原始手目标最大步幅0.354rad，现有v1_1离线复现把实际步幅限制到0.175rad并在终点收敛。
人工入口23固定normal arms100Hz + Split O10 v1_1 10Hz，无腰/底盘；G2端zero-output预检已通过，
尚未执行该版Powered。

17:37双臂+O10运行到66.56秒，操作者因明显抖动停止。与arms-only相同的前25.36秒中，14D command
逐值相同；加入O10后dispatch P99 12.057→12.536ms、>12ms 26→49，仍无>15ms。反馈最佳lag约
68→100ms，lag对齐误差MAE/P95/max由0.103/0.410/1.647增至0.349/1.516/11.931mrad。
O10实际9.99998Hz，但v1_1发生56次速度、106次加速度和20次delta cap。证据支持O10路径是触发
因素，不足以认定10Hz频率本身。新增入口24把双臂和O10都固定在Home，仅隔离100Hz Servo与10Hz
move_ee_pos并发；仍为人工Powered门。

17:51首次入口24尝试被手部起点安全门在零发送阶段拒绝：有效反馈与frame-0手命令最大差
`0.903682rad`，主telemetry仅含`sequence=0` fault summary，feedback partial也没有采样命令。
上一轮中途停止时最后手反馈与frame-0最大差`0.973476rad`，说明当前手型不是Home。此门不得放宽；
必须先由现场人工运行审核过的`03_reset_home.sh --execute`，确认Home完成后才可重跑静态contention
probe；若Home后仍不匹配，则停止并调查Home/O10反馈语义。

按操作者后续要求，实验效果不再用人为数值门限贴PASS/FAIL。静态入口24已改为初始化后读取当前
34D实测姿态，并把当前双臂/双手原地冻结为100Hz/10Hz命令；因此不再比较数据集frame-0，也无需
先回Home，也不套用数据集轨迹的settle/动态效果门。物理关节限位、Servo/GDK故障、急停、非有限状态与O10全零/近零无效反馈拒绝继续作为
硬件安全互锁保留。数据集双手大跳变被定义为真实实验输入而非数据错误；recorded-actions入口已
删除0.08rad手起点拒绝，只记录起点差并评价控制后的丝滑性。双臂轨迹起点保护保持独立。

18:00静态current-pose探针自然完成20秒：arm/O10分别发送2001/202次，零telemetry drop；双臂
反馈最大峰峰值仅`1.44e-6rad`。这排除了“100Hz Servo + 10Hz静态O10并发本身必然导致双臂抖动”。
arm dispatch P99/max为12.777/15.123ms，但静态双臂仍稳定。常值手命令下，手反馈在0.110/0.701秒
仍分别出现0.895976/0.878548rad启动跳变，1.25秒后峰峰值降至0.003735rad；需继续隔离动态O10
目标与运动双臂的交互。控制台末尾software emergency-stop是自然完成后的cleanup日志，并非故障。

2026-09-03已同步G2Teleop入口25，用当前实测双臂姿态做100Hz静态normal Servo，同时把数据集动态
双手actions经v1_1以10Hz回放91.22秒。手部大跳变不作为拒绝条件；Pink/Ruckig、网络、模型、腰和
底盘均不参与。G2零输出检查为9123 tick且双臂命令max step/velocity均为0；尚未Powered。

11:55该探针完整结束：arm/O10为9123/920次，零drop；双臂反馈峰峰值最大`1.91e-5rad`，现场确认
双臂不卡顿。双手自身存在分段顿挫：第一次闭手的客户端v1_1在8.48--9.68秒连续发送12个10Hz
变化步，但O10反馈主要只在9.154/9.753/10.355秒更新，约每0.60秒一段。发送P95/max仍只有
100.543/101.576ms，因此0.x秒停顿发生在`move_ee_pos`调用之后的执行/状态链，不是客户端漏发。
下一步配对单次最终点目标与10Hz连续中间目标，双臂继续静止。所有后续现场实验入口改为按一次
Enter进入5秒倒计时，不再要求输入`RUN ...`固定短语。

该配对现已实现为G2Teleop入口27：双臂全程以arms-only normal Servo保持当前姿态；A只提交一次
冻结close终点，第12秒单次open复位，B提交12条10Hz v1_1中间目标到相同close终点。新增公开
`send_hands_once`诊断原语只允许在arms-only且无hand worker时调用，因此A结束后不会被后台线程
重复刷新。整轮24.1秒，保留20Hz同owner反馈。6000D上G2Teleop 81项及机器人相关109项纯逻辑
回归通过。运行文件已逐项同步，G2端Python/Shell、3项纯mock测试和zero-output检查通过；尚未Powered。

14:07入口27同轮A/B自然完成。A只发送一次close终点，现场“非常丝滑，一下子就到达”；B以
99.77--106.07ms间隔发送12条v1_1中间目标到同一终点，现场出现原有多段顿挫。双臂2411次常值
normal Servo期间反馈峰峰值仅`1.44e-6rad`，零drop。相同起终点下的单变量证据强烈支持动态刷新
`move_ee_pos`触发顿挫，而不是O10机械上天然不能丝滑。

同时发现`get_end_state()`手部值不具备本实验所需的实时反馈语义：6秒A完成后直到12.159秒reset
调用才显示close，12秒reset直到18.206秒B调用才显示open，B停止调用后也未可靠显示最终close。
因此此前0.5--0.6秒反馈更新不能再解释为实际运动分段；手部状态更像缓存/由后续O10调用刷新。
下一步把完整数据集手轨迹变成每个动作窗一次endpoint的event-driven回放，先固定双臂，再加入
Q-direct移动双臂。

2026-09-03已在6000D完成下一event-driven候选：G2Teleop入口29让当前实测双臂以arms-only
normal Servo 100Hz静止保持，在91.22秒数据集时间轴上仅对10个真实手部动作窗各调用一次窗末
endpoint；加上第1秒的一次open准备，全程O10恰为11次调用且无后台hand worker。动作窗起点为
8.40、24.20、25.80、37.00、40.90、45.50、53.30、64.30、79.70和83.70秒。常值线性插值产生
的亚机器精度残差被按数值等价处理，不过滤任何物理手部变化，也不作为实验PASS/FAIL门限。
本地zero-output及G2Teleop无loopback回归为`82 passed, 2 skipped`。运行文件与文档随后已逐文件
同步；G2端Python/Shell语法、6项纯逻辑测试和zero-output复核通过，
产物SHA、10个任务窗及11次调用均与6000D一致；该复核没有导入GDK、初始化Robot或发送任何命令。
Powered仍未执行，唯一人工入口为`/home/agi/xza/G2Teleop/scripts/29_g2_o10_event_arm_hold.sh`。

14:18入口29运行至artifact time 71.24秒后由操作者Ctrl-C停止，状态`operator_ctrl_c`、覆盖率
78.1%，因此不是完整通过。已执行1次准备和前8个任务endpoint，现场确认全部丝滑；最后79.7与
83.7秒两事件未覆盖。双臂7125次常值命令、反馈峰峰值最大`1.67e-6rad`，dispatch P95/P99/max为
10.460/12.579/14.710ms，主/feedback均零drop。该结果把一次点目标A的正证据扩展到8个数据集
动作窗，进一步支持顿挫来自运动中反复刷新`move_ee_pos`，而非O10机械能力或5/10Hz数字本身。

下一候选已在6000D实现为G2Teleop入口30：先用2秒保持Q-direct起点并单次提交open准备目标，随后
完整执行91.22秒Q-direct双臂，同时按同一10个动作窗各提交一次O10 endpoint。仍为normal Servo、
arms-only控制器加显式one-shot O10，无在线Pink/Ruckig、网络、模型、腰或底盘。手部大跳变不设
拒绝门，手臂起点连续性保护独立保留。本地Python/Shell、zero-output及回归为
`83 passed, 2 skipped`。运行文件与文档已逐项同步；G2端Python/Shell、7项纯逻辑测试和
zero-output复核通过，未导入GDK或发送命令。Powered尚未执行，唯一人工入口为
`/home/agi/xza/G2Teleop/scripts/30_g2_qdirect_o10_event_replay.sh`。

14:34首次入口30尝试因Servo状态`mode=5,error_code=2`在Controller构造时退出，零命令发送。状态
恢复后的14:34:48轮运行到总时间27.33秒/artifact 25.33秒后由操作者Ctrl-C停止，覆盖率29.3%，
执行了open准备及前两个任务endpoint。现场确认双臂和双手“非常丝滑”。2534条移动Q-direct命令
与17:22 arms-only前缀逐值完全相同；dispatch P95/P99/max为10.461/12.500/15.108ms，主/feedback
零drop。按约74ms lag对齐的反馈MAE/P95/max为0.095/0.372/4.061mrad，MAE/P95已回到arms-only
基准0.094/0.376mrad量级，显著优于旧10Hz重复O10组合0.349/1.516mrad。该前缀证据反驳移动双臂
与O10天然冲突，并进一步定位到反复重提O10目标；由于后8个事件未覆盖，仍不登记为完整通过。

event endpoint虽然丝滑，但按O10内部默认速度快速完成，未保留数采约1--3秒的渐进手部时长。
当前GDK 2.6.3已记录的`move_ee_pos`请求只有20个position，没有已验证速度/时长字段。下一入口31
因此保留Q-direct 100Hz双臂和共同91.22秒时间轴，把原始hand actions按5Hz采样、关闭v1_1，并只
提交58个实际变化样本；静止样本不重复调用。加初始目标共59次O10调用。该版本用于判断原始5Hz
渐进动作能否同时保持丝滑，不预设结果。本地zero-output和回归为`84 passed, 2 skipped`。运行文件
与文档随后已逐文件同步；G2端Python/Shell、8项纯逻辑测试和zero-output复核通过，确认
58个变化样本/59次总调用；没有导入GDK或发送机器人命令。唯一人工入口为
`/home/agi/xza/G2Teleop/scripts/31_g2_qdirect_o10_raw5_replay.sh`，尚未Powered。

14:58入口31随后自然完成，总时间93.22秒，arm/O10分别为9323/59次，主/feedback零drop；现场
评价双手“分段丝滑，没什么问题”。dispatch P95/max为10.567/15.194ms，约73ms lag对齐后的
双臂反馈MAE/P95/max为0.128/0.468/9.966mrad。该结果证明限速时间轴上的raw 5Hz候选可接受，
但91.22秒来自双臂`velocity-limited`时间伸展，并非原始30Hz数据的正常速度；此前称其为正常
速度是记录错误。原始948帧只覆盖31.57秒。

GDK 2.6.3官方复核确认`move_ee_pos(dual_tool,o10_t2)`确实是合法20D双手点目标接口，故Split路径
本身没有选错；但公开合同没有速度/时长字段。官方`joint_servo_control`则明确用于100Hz，并展示
单侧7D arm与从`get_end_state()`动态获得的同侧末端名称合并发送。该示例不等于历史失败的双侧34D
请求，因此下一接口诊断应是单侧17D动态名称探针，而不是直接切回曾产生`Param not found`和2.53秒
阻塞的production unified 34D。

已在6000D新增真正原始速度入口32：直接读取30Hz `training_replay_v1.json`，14D states插值到
100Hz normal Servo，20D actions在同一时钟上按raw 5Hz只提交变化样本。回放31.57秒+末端保持1秒
+起点准备2秒，总34.57秒；3258个回放tick、47个变化目标+1个初始O10调用，arm最大单步
0.020250560rad、最大离散速度2.025056rad/s。本地zero-output和无loopback回归为
`85 passed, 2 skipped`。
运行文件与文档随后已逐文件同步；G2端Python/Shell、9项纯逻辑测试和zero-output复核通过，输出
仍为32.57秒、47个变化目标/48次O10调用。复核没有`--execute`，未导入GDK或发送机器人命令；
入口32尚未Powered。

15:13入口32首次运行约13.15秒后人工停止；15:14:46第二轮完整自然结束34.57秒。完整轮arm/O10
为3458/48次，主telemetry和702条feedback均零drop；arm发送间隔P50/P95/max为
10.009/11.115/14.460ms，GDK调用耗时P95/max为0.906/3.290ms。现场确认双臂与双手“非常丝滑、
非常可以接受”。这完成了原始30Hz速度下移动双臂+raw 5Hz渐进O10的正向验证，replay主线可停止
扩展，下一步转入F710实时控制。

本轮每个tick的0.004995rad clip警告来自arms-only诊断内部保留的一个略超静态限位的手反馈值；
实际Servo只发送前14D，不是本轮手或臂运动异常。后续作为日志噪声清理。单侧17D末端Servo仅保留
为RTC连续手轨迹的独立研究项，不阻塞F710：F710双臂沿用100Hz normal Servo，O10采用意图变化
驱动、约5Hz渐进变化目标且静止不重发的已验证分工。

同日F710代码已按该分工完成离线改造：`12_g2_powered_f710.py`不再创建Split 10Hz/v1_1 hand
worker，Controller固定arms-only 100Hz normal Servo；同一owner通过显式`send_hands_once`提交
5Hz changed-only O10目标。`ChangedOnlyHandScheduler`只在5Hz槽推进渐进手状态，两个O10调用
不会小于200ms；目标到达后即使RT目标持续存在也不再调用。首次明确RT抓握目标前，仅选择侧并
按RB不会把实测手型投影到synergy或发送O10。telemetry新增intent ID、调度原因、是否发送和
`move_ee_pos`耗时。新增100Hz调用模拟后，本地G2Teleop回归为`90 passed, 2 skipped`，机器人
client完整文件76项通过。运行代码和测试已逐文件同步G2；远端Python/Shell、19项F710纯逻辑测试
及files-only preflight通过，后者确认新鲜O10 fallback可用且没有导入/初始化GDK。改造尚未
Powered，下一门为无Ruckig的direct Pink空载小幅F710测试。

## 2026-09-04 正式Sync arms-only配对隔离入口

为隔离DM0.5连续手目标对双臂的影响，新增
`scripts/15_powered_sync_arms_only_10s.sh`。它与正式`05_powered_sync.sh`连接同一GPU7/7892
75K Sync服务，保留相同的20/50执行前缀、low-latency双臂通道、120ms boundary blend、
arm jerk weight 10、C2 spline及`stationary_minjerk_v1/0.0035rad`，但硬件层固定
`servo-output-mode=arms-only`：模型仍返回50x34，控制器只执行前14D且不创建O10 worker、
不调用`move_ee_pos`。`--split-control-profile`仅为该诊断允许显式arms-only；仍拒绝历史
`unified-34`。

6000D完整无GDK回归为`155 passed, 1 skipped`，Shell语法和diff检查通过。运行代码、入口、
测试和脚本文档已逐文件同步到`/home/agi/xza/DM0.5`，随后逐文件拉回`/tmp`并通过`cmp`核对。
同步与核对没有通过SSH执行GDK、Robot、Servo或Powered动作；真机测试仍必须由操作者在G2现场
运行入口15并握住物理急停。

入口15于2026-09-04 14:19完成现场10秒测试，操作者评价“非常丝滑”。主日志确认
`request_layout=arms-only`，15个chunk共1000次14D Servo、0次手部发送且无任何hand command
事件；单次GDK发送最大3.218ms，相邻arm dispatch最大13.826ms。该在线配对结果把当前Sync明显
卡顿的主要触发项定位到连续O10下发链，而不是7892推理等待、模型双臂输出或100Hz手臂线程。

随后新增`scripts/16_single_side_17d_servo_probe.sh`，用于验证GDK 2.6.3官方单侧“7D arm +
动态10D end”Servo合同。默认只读5次新鲜反馈；`--execute`固定仅左侧、normal碰撞保护通道、
100Hz当前位置保持1秒。探针严格保留`get_end_state()`返回的末端名称原字符串，同时用去除首尾
空白后的名称做集合校验；这是为了覆盖本机历史右手动态名称中
`idx77_hand_r_index_pip_joint `带尾空格、而冻结手写名称不带空格的真实差异。任何名称/健康状态/
限位/静止门失败均不发送命令，不使用模型轨迹，也不控制另一侧。

加入探针后的6000D完整回归为`158 passed, 1 skipped`，Shell/Python语法与diff检查通过；运行
代码、入口、测试、common进程互斥及脚本文档已逐文件同步并拉回`/tmp`通过`cmp`。同步阶段未远程
运行GDK或发送命令；下一门仅为操作者在G2执行入口16的默认只读预览。

入口16只读预览于14:26通过：左侧动态17D名称完整，5次新鲜反馈最大漂移
`5.49e-7rad`，零命令发送。随后14:26:50执行1秒静态normal Servo，100次17D请求全部成功，
调用P95/max为`1.005/1.730ms`，未出现`Param not found`、非零返回或长阻塞；结束反馈最大偏差
`0.004951rad`位于左拇指外展轴。因此当前G2/GDK实例至少明确接受左侧动态名称17D Servo，历史
双侧34D失败不能再解释成“O10硬件绝对不支持Servo”。

下一最小门新增`scripts/17_single_side_17d_micro_motion.sh`：默认只读，`--execute`时仅把左食指
PIP从新鲜反馈以minimum-jerk增加0.05rad并返回，2秒共201个100Hz目标；左臂7轴与其他9个O10轴
保持，右侧不控制，通道仍为normal。轨迹在执行前重新检查动态名称、5次静止门和目标限位。
加入该候选后的完整回归为`159 passed, 1 skipped`；运行文件逐项同步并拉回`/tmp`通过`cmp`，
同步阶段未执行GDK动作。下一步只运行入口17默认只读预览，通过后才允许其`--execute`。

入口17的第一轮预览因瞬时`mode=5,error=2`零命令停止，第二轮健康预览通过；随后执行轮发送
201/201个左侧17D目标，调用P95/max为`1.121/1.971ms`，没有接口错误。操作者观察“什么都没动”。
本轮峰值仅0.05rad且没有峰值停留；结束时左食指PIP反馈相对起点仍高`0.03198rad`，位于起点与
0.05rad峰值之间。因此证据不支持“请求完全未到达”，但第一动态探针缺乏肉眼可见性和峰值反馈，
不能据此登记动态Servo物理通过。

为取得明确判定，新增`scripts/18_single_side_17d_visible_motion.sh`：左食指PIP以minimum-jerk
增加0.20rad、峰值保持1秒、1秒返回、基线再保持1秒，总4秒/401个100Hz目标；其他16个左侧轴
保持，右侧不控制，仍为normal通道。默认只读，执行前重新经过动态名称、健康、静止与限位门。
完整回归为`160 passed, 1 skipped`，运行文件逐项同步并拉回核对；尚未运行入口18。

入口18随后完成真机执行：401/401个左侧17D请求成功，0.20rad左食指PIP轨迹在峰值与基线各
保持1秒；调用P95/max为`0.850/4.706ms`，无接口错误，操作者评价“非常丝滑”。这确认左侧O10
不仅接受静态17D请求，也能通过100Hz Servo真实执行连续轨迹。

基于该结果，6000D新增双侧连续候选`servo-output-mode=dual-side-17d`及同步推理入口
`scripts/19_powered_sync_dual_side_17d.sh`。每个100Hz tick把模型34D按动态名称重排成两次
请求：左臂7D+左手10D、右臂7D+右手10D；双手从30Hz模型waypoint线性插值到100Hz，不调用
`move_ee_pos`，不做阈值、deadband、changed-only或手部轨迹重塑。右侧动态名称保留GDK返回的
原字符串，包含本机已见的尾空格，同时用trim后的名称映射模型维度。首次候选使用normal通道；
首跳、裁剪幅度与动力学超限沿用正式Sync的告警-only行为，不新增软件硬拒绝。仍保留34D/finite
合同、官方物理位置范围和GDK normal碰撞保护。

相关运行/CLI/路由测试为`117 passed`；当前沙箱不能创建localhost测试socket，因此全仓排除
`test_process_pipeline.py`后为`154 passed, 1 skipped`。入口19与正式Sync格式一致：默认60秒，
支持`--duration-seconds N`和`--continuous`。此前相同process pipeline在改造前完整
通过，且本轮未改变其IPC/网络实现。此处“待同步/待执行”是候选建立时状态，现已被后续结果替代。

### 2026-09-07文档核对：入口19已完成双手Sync真机验证

入口19的运行代码已在09-04同步G2，后续日志
`g2_powered_split_sync_300s_c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only_dual_side_17d_servo100_20260904_144901.jsonl`
记录双侧动态17D normal Servo同步推理，操作者确认“终于丝滑”。实际13.36秒、15个chunk、952个
segment tick，另有327个等待窗手部tick；不是300秒长稳。默认60秒、可设置时长，旧5s文件仅为兼容入口。
单次动态34D、全套平滑复评和RTC迁移仍未完成。当前状态见
[CURRENT_STATUS](../../DM0.5/docs/CURRENT_STATUS.md)，现场命令见[操作指南](00_G2_OPERATOR_GUIDE.md)。
本次仅修正文档状态，没有重新同步文件或执行真机。

### 2026-09-07右手映射核查与诊断候选同步（覆盖上文完整验证措辞）

运动控制日志确认 `idx77_hand_r_index_pip_joint `（尾空格）被拒绝；此前“丝滑”反馈
不能作为右手逐轴映射或完整抓取已验证的证据。静态分析见
[SDK_RIGHT_HAND_FINDINGS](../../DM0.5/logs/analysis/20260907_sync_servo_audit/SDK_RIGHT_HAND_FINDINGS.md)。

本次仅通过scp同步以下两个具体文件到G2当前部署目录：
- `g2_sort/operations/hardware_tests/single_side_17d_servo_probe.py`
- `scripts/20_right_index_canonical_servo_probe.sh`

新增显式 `right-index-pip-canonical-visible` profile：保留原始反馈名作证据，发送名trim后
校验完整右17轴集合，按名称定位食指并保持名称/位置配对。右食指PIP相对当前姿态+0.20rad，
1秒去、1秒保持、1秒回、1秒保持，normal/100Hz；同一Robot以10Hz记录右17轴反馈。
全零右手、位置越限、确认期间姿态变化均停止；中断/故障停止发送，不自动执行返回或复位。
完成仅记 `COMPLETED_PENDING_MAPPING_REVIEW`，不宣称映射已通过。报告写入
`logs/diagnostics/right_index_canonical_*.json`。

本地7项无GDK纯逻辑测试及Shell语法检查通过。未执行GDK、Shadow或真机动作；现场验证待办。
生产入口19的发送逻辑、Control/Policy隔离和C2参数本次未改动。旧探针profile保留历史行为，
右手现场应使用入口20，不能用仍保留错误尾空格的旧右手静态profile替代。

同日后续同步：入口20现支持 `--finger index|middle|ring|pinky`（默认index），每次
只执行所选PIP轴的相同4秒往返。实现新增 `right-finger-pip-canonical-visible` profile，
按已核对官方名称选择轴，保持其余名称/位置配对；原食指profile兼容保留。
已同步上述Python与Shell两个文件；10项纯逻辑测试及Shell语法检查通过。
现场15:37食指报告已拉回：401次发送，SDK调用最大3.004ms；食指实际增量约0.141rad，
目标增量0.20rad，返回后残差0.023rad。对应运动控制窗口无未知关节名错误；存在
vel_scale与ACTIVE/STOPED记录，不能据此宣称跟踪精度完全通过。
无名指、小指、中指候选尚待现场执行。正式推理发送逻辑与平滑参数仍未修改。

同日进一步更新（覆盖上一段待办状态）：操作者完成无名指、小指、中指测试并确认正常。
有效报告分别为154534、154602、154630，每轮401次发送，对应接收端窗口未知关节名错误0。
另有154500/154506/154511三份报告因motion error=2在发送前退出，不计入有效验证。

已通过scp同步 `g2_sort/deployment/g2_client/robot_control.py` 的名称修正：
`_read_dual_side_servo_layouts`发送trim后且已校验集合的名称，保持动态顺序及模型值配对，
原始反馈名与实际发送名分别写日志。本次同步前本地与G2原文件一致，无现场差异覆盖。
47项运维/探针纯逻辑测试及入口19 Shell检查通过；正常及反向反馈顺序均检查完整双17D配对。
同时同步现场操作指南及本结构记录。入口19参数、模型、Control/Policy隔离、C2均未修改。
Sync Docker 7892检查为Up 7 days；完整抓取修正后真机复验仍待操作者现场执行，本轮未执行GDK。

### 2026-09-07单34D与双17D受控对比入口

名称修正后的三轮Sync日志已核查，结果见
[审计报告](../../DM0.5/logs/analysis/20260907_sync_servo_audit/after_fix/REPORT.md)。
按操作者要求优先验证请求布局，不以补齐诊断反馈或调整平滑为前置。
已scp同步 `operations/hardware_tests/servo_layout_probe.py`（位于g2_sort下）、
`scripts/21_servo_layout_probe.sh`、`scripts/lib/common.sh`（增加新探针进程互斥）。
入口21单Robot、normal、100Hz：同一对body/end快照按左右各17D配对，trim且校验已知34轴集合，
支持同样命令合成单34D或拆双17D。static保持1秒；left-index/right-index仅对应食指+0.20rad
往返4秒。每次现场确认和倒计时，健康/全零/限位/静止/确认后姿态检查；异常停止发送、不自动返回。
每次调用时刻/耗时、目标与双侧反馈写 `logs/diagnostics/layout_*.json`，完成仅标记待审阅。
14项探针纯逻辑测试和两个Shell语法检查通过。单34D尚未真机执行或验收；正式入口19未改动。

### 2026-09-08 完整34D Sync候选

09-07单34D左右食指已产生对应运动；静态左手异常反馈仍未解释。09-08最近两次静态
仍因全零反馈在发送前退出，不能记为通过。操作者明确要求推进完整真机推理，不再重复静态门。
新增并scp同步 `scripts/22_powered_sync_unified_34.sh` 及 `scripts/lib/powered_split_runner.sh`：
显式 `--unified-34`，normal、100Hz，使用现有controller的canonical POLICY_JOINT_NAMES
及同顺序34D模型目标，单次请求。该完整模型路径不发送启动零反馈hold。
沿用入口19的C2/weight10/3.5mrad、120ms融合、手部平滑off、Control/Policy隔离与既有检查。
日志后缀unified_34_servo100。37项运维纯逻辑测试和Shell语法检查通过；7892 Docker Up 8 days。
本轮未远程执行GDK；完整34D模型真机验收待办。同步操作指南与本结构记录。

09-08入口22首次执行因CLI拒绝split-control-profile+normal/unified-34在解析阶段退出。
已修复并scp同步 `g2_sort/deployment/g2_client/cli.py`：允许该组合，但34D必须显式选择normal，
仍拒绝low-latency；保留profile影响的融合与告警行为。新增无硬件Shell→CLI回归，实际运行
入口19/22的参数构建，替代所有网络/GDK/倒计时调用为stub，并送入真实parse_args。
40项相关测试及Shell语法检查通过；未执行真机。原入口22命令可重试。

09-08操作者明确要求34D计时看门狗只记日志、不得因超时停止。入口22现启用
`--servo-timing-warning-only`，CLI仅允许powered-sync/unified-34/normal组合。
200ms仍作为发送和tick迟到的记录阈值，分别写servo_send_overrun/servo_tick_overrun，
超过后继续，丢弃过期调度槽、不立即补发。SDK返回错误、人工停止等原故障路径保留。
入口19与其他模式默认超时策略不变。同步cli.py/app.py/robot_control.py/servo.py及
scripts/lib/powered_split_runner.sh；同步前120项相关测试通过，补充入口隔离断言后4项入口检查通过，
Shell语法通过。模拟240ms耗时验证34D继续、默认停止及SDK错误仍停止。未执行真机。
同时修复unified-34的手部100Hz标签和发送计数，避免0Hz/0次汇总误导。
17D修正后三轮135次请求往返加权平均234.238ms；server_model_and_device_to_cpu平均144.588ms。

### 2026-09-08 DAgger POLICY/HIL恢复迁移与RTC标记

按用户要求，DAgger独立部署的src/g2dagger/powered_hil_io.py和scripts/40_g2_powered_hil.py
已通过scp同步：POLICY、等待新策略、恢复策略阶段采用同owner单canonical34D normal/100Hz。
人工HIL保持arms14 Servo＋显式O10点目标；状态切换不创建第二Robot或hand worker。
POLICY发送200ms以上只记事件继续，人工阶段恢复原发送阈值与故障行为，输入网络watchdog不变。
录制按实际SDK单次34轴名/索引/提交值/返回写入；启动profile为powered_hil_sync_34d_f710_candidate_v2。
59项沙箱回归加1项权限外本地loopback回归通过，共60项；Python编译与Shell语法检查通过。
DAgger新版本真机交接待验收，training_ready=false；已同步START_HERE和RECORDING_AND_PROGRESS。

RTC仅加TODO(RTC_UNIFIED_34D_MIGRATION)注释，目标canonical单34D normal/100Hz、唯一Control
Robot owner，当前运行行为不变。已同步g2_client/rtc.py、control_loop.py及scripts/09_powered_rtc.sh。
SUPER_IMPORTANT已记录此次衔接。未远程执行GDK/Servo。

## 2026-09-08 Home现场确认调整

03_reset_home.sh和operations/recovery/hand_home.py改用ENTER确认及5秒倒计时；旧--confirm仅保留解析，不跳过现场确认。operator_confirmation新增独立ENTER函数，其他入口原函数未改。42项纯逻辑operations测试通过、Shell语法检查通过。同步G2时不执行复位。hand_home_approved.json未修改；用户指出右小指姿态，待区分屈曲与外展后指定目标，不能声称数值修复完成。


## 2026-09-09 数据集单34D回放

G2Teleop新入口43复用本项目training_data_replay新增--unified-34/--files-only；normal100Hz，原948×34 action/30Hz，ENTER、共享PID锁。现有arms-only/Split仍保留。49项本地纯逻辑测试通过，新入口真机未验收。同步具体文件：operations/hardware_tests/training_data_replay.py与G2Teleop/scripts/43_g2_dataset_unified34_replay.sh；说明见../../DAgger/doc/REPLAY_UNIFIED34_20260909.md。

部署后补证：G2端43入口默认files-only与bash -n通过（退出0、no GDK）；未执行--execute。

## 2026-09-09 Home重复目标确认修复

hand_home.py：相同端点只发送/确认一次锚点，避免changed-only跳过重复目标后Home无限等新计数直至超时；结束增加原始手反馈对照，SDK确认不冒称物理到位。50项operations/replay测试通过；具体文件同步G2，未远程执行真机。Home目标及低延迟/Split路径不改。

2026-09-09 17点：43新增--prepare-hands，同owner统一34D normal保持臂、5秒渐进开手、最多2秒核对旧起点容差；零手反馈不伪装起点，反馈不符拒绝进入数据轨迹。53项纯逻辑测试通过；本次同步具体replay模块/43脚本，未远程执行真机。
