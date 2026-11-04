---
updated: 2026-09-18
robot_name: 4F-F1
robot_id: F1_D071
type: operations
---

# 4F-F1开机、推理录包与重复试验

只适用于 **4F-F1（亦称F1-4F，软件F1_D071）**。machine-id为`00000000000000000000000000000001`；旧F1_D070同样叫ms-thor，不能按hostname套脚本。机械/标定差异见[本机身份记录](4F-F1.md)，最新结果看[任务当前方案](../../foam_sort_whole_body/docs/training_plan.md)。

## 哪些是系统同事提供的

原始MD是[F1系统接口清单](../../F1系统接口清单.md)“节点启动”“整体启动”和motor_ctl启动部分。里面的`ec-master.service`、`f1-ros2node.service`是原文示例，不能直接假定D071存在同名服务。本机实际脚本、服务原文与SHA已保存在[v4发布前快照](../../foam_sort_whole_body/results/deployments/20260918-4F-F1-v4/backup/remote_state.json)。

| 层级 | 本机入口/职责 | 开机是否足够 |
|---|---|---|
| EC-Master | `/home/user/workspace/start_ec_master.sh`：先重启Codemeter，再前台运行本机aarch64 EcMasterDemoDcMT与f1-eni.xml；头部/身体EtherCAT链路 | 原脚本需现场sudo；不启动相机、模型、录包 |
| 关节驱动 | `ros2 launch motor_ctl motor_ctl.launch.xml`：HAL反馈、双臂/夹爪接口、身体UDP→EC | 与EC同时需要；手柄姿态程序不能替代 |
| 运动学与三相机 | `/home/user/workspace/start_all.sh`：kinematics、motor_ctl、head/右腕/左腕原launch；相机间隔2秒 | 不含EC、SSH模型转发、Docker、rosbag |
| 系统自启 | `/usr/lib/systemd/system/f1_startup.service`：Type=forking，User=user，ExecStart调用上项 | 本次实读 **disabled、inactive**；未替用户enable或restart |
| 手柄姿态 | `/home/user/0721.gamepad.udplog/whole_body_ctl/gamepad_pose_ctl/start_pose.sh`：whole_body_ctl＋gamepad_pose_publisher | 这是其他控制入口，会与推理争控制；不是192起姿 |
| SSH登录通道 | 机器人已有`f1-6000d-tunnel.service`，6000D通过22029进入 | 能登录不代表模型端口已转发 |
| 模型 | 6000D三个Docker，7892=5K、7894=20K、7896=25K | F1还需三端口SSH反向转发 |
| 任务记录 | 每次`execute`自动启动、监视并关闭独立rosbag | **不是需要开机常驻的系统服务** |

`/home/user/0721.gamepad.udplog/start_all.sh`与`/home/user/workspace/start_all.sh`不是同一脚本：前者的motor/camera启动段被注释，不能替代后者。`f1_startup.service`写了Restart=on-failure，也不等于逐个健康监视后台相机。当前恢复了手动启动的驱动/相机，**不要再执行整套start_all或restart服务制造重复节点**。

## 下次冷启动的顺序

1. **6000D终端**，进入新机，不用旧机22022：

```bash
cd /home/user/projects
ssh -F F1/connect/robots/f1-22029/client_ssh_config f1-22029
```

2. **F1现场终端**先检查已有进程与服务。下面启动命令只用于对应进程确实不存在的冷启动；它们会接通硬件驱动，由现场操作者执行。

```bash
systemctl is-enabled f1_startup.service
systemctl is-active f1_startup.service
pgrep -af 'EcMasterDemoDcMT|motor_ctl_node|kinematics|realsense2_camera_node|component_container'
```

**F1终端A**（缺EC时，前台终端保留）：

```bash
sudo bash /home/user/workspace/start_ec_master.sh
```

**F1终端B**（kinematics、motor_ctl、三相机均未启动时）：

```bash
bash /home/user/workspace/start_all.sh
```

若只有个别进程缺失，按原脚本的对应launch单独补齐，不重复启动整组。EC日志在`/home/user/workspace/logs/{codemeter,ec_master}.log`；整组ROS日志在`/home/user/workspace/logs2/`。本轮单独补相机的日志在Bruno任务`runtime/camera_recovery/`。遇到`arm_l_j1`无有效反馈，先查motor_ctl/EC与DDS，不反复capture或改姿态容差。

3. **6000D另一终端**，检查模型并建立/补齐转发（不启动机器人动作）：

```bash
cd /home/user/projects
python3 F1/foam_sort_whole_body/deploy/launch_inference_ab.py status --checkpoints 5k 20k 25k
bash F1/connect/robots/f1-22029/inference_forward.sh
```

转发脚本会从F1逐一访问`/health`并核对checkpoint，SSH master存在不再被当作模型健康。当前转发是独立SSH进程，机器人重启/网络断开后可能退出，需要重跑该脚本；没有声称它已做成自恢复服务。若某Docker确实停了，沿用启动器，只指定该checkpoint，例如：

```bash
python3 F1/foam_sort_whole_body/deploy/launch_inference_ab.py start --gpu 1 --checkpoints 25k
```

启动器检查GPU其他计算进程、已有服务身份和冻结源码，不重建训练环境；勿用`docker stop`停止训练容器。

4. **F1现场终端**，准备起姿、保存场地起点、验证录包，再运行一个模型：

```bash
cd /home/user/Bruno/F1/foam_sort_whole_body
./pose_192.sh to-start
./trial_home.sh capture
./inference_20k.sh preflight
./inference_20k.sh record-check
./inference_20k.sh execute --chunks 0
```

已经到达192并完成capture-body时，不必重复to-start。`pose_192.sh`只处理双臂14轴、身体5轴、夹爪，不将底盘搬回料架位置；到达后自动只读保存本机身体参考。`capture-body`只读身体参考；`trial_home capture`保存试验前关节和底盘位置，二者作用不同。

## rosbag：每次发生了什么

正常execute顺序：模型health及身份→ROS观测/参数/身体检查→C++录包类型检查→现场Enter→创建控制端点但不发送→启动rosbag并确认所有topic订阅/数据库就绪→请求模型并执行。结束/Ctrl-C/错误：停车/保持并确认反馈→关闭本次rosbag、落metadata。

`record-check`复用同一个Recorder，创建空控制发布端点用于发现，但禁止发送；录3秒后正常关闭并检查三图与必要反馈消息数量。它不会跑模型，也不代表模型轨迹会通过所有检查。`preflight`现在能检查消息库，但不启动录包进程，不能替代第一次`record-check`。

| 事件 | 直接证据 | 原因与处理 |
|---|---|---|
| 旧D070“录制未准备好” | 旧包9.62秒、4048条消息，但夹爪命令订阅没建立 | 客户端等录包就绪才创建命令发布端点，相互等待；旧v4已调整顺序，新机继承该修复 |
| D071 `20260917T181519Z_fd84a367` Connection refused | 请求/执行0，hardware_published=false，尚未启动Recorder；6000D两个Docker仍在 | F1本地模型转发缺失；恢复7892/7894，后加7896。不是此轮rosbag崩溃，不证明整网或Docker掉了 |
| D071 `20260917T182254Z_c1546432` rosbag退出 | exit1；日志`Something went wrong loading ... driver/State`；只有4条消息 | AGV旧driver包优先，库虽存在却没有State的C++符号/IDL；不是时间预算小，也不是录包未开机自启 |
| 当前4F-F1 v4 | 18topic就绪、正常关闭、driver/State等有消息 | 项目单独将`lxl/interface_20260623/install/{driver,server}`消息包置于对应旧包前；不替换系统库、不改变move或电机控制器 |

旧机也遇到过录包问题；两次根因不同。三路相机/关节驱动在某次能用，只代表当时进程运行，不能推断下次开机自启已齐全。`Failed to parse type hash ... USER_DATA '(null)'`在本次成功录包时仍出现，不是此次致命退出原因。

`hardware_published=true`也可能只指故障清理发出的零速/保持命令。18:22这轮chunks_requested/executed均0，不能据此说模型已运动。`metadata_present=true`也不证明成功，必须同时看error、退出码、duration及各topic消息数。

F1每轮输出：`runtime/inference/<ID>/{report.json,events.jsonl,rosbag_console.log,rosbag/metadata.yaml}`。只在rosbag关闭后用数据库核对内容，避免就绪探针与写盘竞争。详细证据见[v4记录](../../foam_sort_whole_body/results/deployments/20260918-4F-F1-v4/README.md)。F1系统时钟与6000D相差约16小时，原始运行ID保留，不按不同主机的字符串时间直接排序；本轮未在运行系统上改时钟。

## 保存起点与重复5K/20K/25K

新入口`trial_home.sh`复用已有`base_home.py`、`pose_cli.py`与192消抖调度，没有另写底盘/IK控制器。

```bash
cd /home/user/Bruno/F1/foam_sort_whole_body
# 仅在试验开始前、现场已经摆好时保存；不要在推理终点capture覆盖起点。
./trial_home.sh capture
./trial_home.sh status
./inference_20k.sh execute --chunks 0
# 上一条结束并完成停车后，现场Enter逐段确认底盘和姿态恢复。
./trial_home.sh restore
./inference_5k.sh execute --chunks 0
./trial_home.sh restore
./inference_25k.sh execute --chunks 0
```

当前使用 home-v2：一个 `./trial_home.sh restore` 完成朝向、位置、双臂14轴恢复，不需要单独运行手臂脚本。独立处理可用`align`（只转向）、`return-base`、`restore-pose`；capture/status不发动作。home不发送腰部、头部和夹爪目标，也不自动覆盖本机身体参考。`status`给出底盘误差和关节恢复计划；`restore`先底盘归位，再恢复关节，每一段使用已有现场Enter。每次capture独立保存，不改旧归档；latest只指向最近成功的完整快照。机器人ID、boot-id、文件SHA与profile绑定，旧D070备份不会拿来恢复D071。

**能力边界：** 所谓原“绝对位置复位”实际是本次开机局部里程计XY归位；不是SLAM全局位置。沿用原0.10m/s、2.5cm位置容差、3m范围、1小时有效期；新home独立版本支持先恢复朝向：初始误差≤15°，转向最大0.05rad/s、加速度0.05rad/s²，朝向到0.3°内并连续静止0.5秒后才平移；转向与平移分开。旧D070的纯平移入口仍保持2°限制，不能混用。新版本[发布与验证](../../foam_sort_whole_body/results/deployments/20260918-4F-home-v2/README.md)通过50项测试、19文件部署核验和实机只读规划，尚待现场动作验收。不能保证任意推理结果都一键返回。底盘里程计重置/重启后需现场重新建立起点；跨两次进程间的里程计重置不能仅靠boot-id完全识别。没有障碍绕行规划。关节恢复只覆盖双臂14轴；身体、夹爪、头部反馈仍备份，但不发这些部位的恢复动作。

09-18已按用户要求保存右移后的当前位置和已恢复双臂，朝向绑定最近一次home操作之前的 -1.35262949 rad。当前索引为 `20260917T193346Z_8d92029a_heading_corrected`；原始实测快照及旧home都保留。现场执行restore前的只读残差约右转1°，尚未宣称已实际转正。新home已保存，本次无需再次capture。

本机快照路径：`runtime/base_home/4F-F1/<ID>/`含`pose.json`、`anchor.json`、`restore.json`和manifest，`latest.json`是当前索引。日志在`runtime/base_home/runs/`和`runtime/pose_runs/`。完整闭环仍由现场验证；`--chunks 0`是不限段数的同步stop_and_observe，不是忽略错误或无缝连续推理。

## Wi-Fi中断后恢复

2026-09-18用户确认Wi-Fi曾被关闭。恢复Wi-Fi后，必要时在F1本机以user执行`systemctl --user restart f1-6000d-tunnel.service`；这是用户级SSH服务，不加sudo、不重启整机。6000D再运行前述inference_forward.sh，三个模型health均核对后才恢复模型测试。整机重启会改变boot-id并使原home失效。

本次重新连接后确认原boot-id未变，但EC和motor_ctl进程均已不存在，home只读status报缺关节控制器订阅。恢复SSH不等于恢复电机驱动；由现场按本页原命令补EC，并单独补motor_ctl，避免整组start_all重复启动仍存活的相机。
