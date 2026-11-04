# Hall 归档只读评估（2026-09-23）

来源：用户提供 `/home/user/projects/F1/f1_hall_introduction_20260922_114704.tar.gz`；SHA256 `8f2abbf58677715172ab8f66617a929ea65d870f69ad25667c68d8e85156de4a`。归档所有成员检查过绝对路径、`..`、链接/特殊文件，未见不安全成员；只在 `/tmp/f1_hall_review_20260923/` 选择性展开74份UTF-8源码/配置，38份数据/缓存/备份文件不提取，其中20份MCAP。未执行项目入口、安装脚本、ROS进程或动作。归档没有 AGENTS.md。8份Python做AST解析、8份package.xml做XML解析均通过；这只是静态检查，不是构建/运行/实机验收。报告不记录任何登录凭据值。

最新入口决定：用户已选择`http://192.0.2.3:4242/`作为具身研发平台待部署入口；整体方案见[一体化工作台](embodied_console_integration.md)。下文10001为Hall原代码默认值。

## 推荐

**用 Hall 的网页和配置交互作为我们操作台的起点，复用 meaxiao 的遥操/数采/传输/审阅能力，自有统一后端负责机器人身份、模式/控制权、状态和任务生命周期；不要直接启动整套 Hall。**

Hall 对“输入关节角度、腰部档位、保存预设、点选运行、日志”已经做出可用的页面骨架，能省掉表格/表单/导入导出的重复工作；但它当前定位是展厅导览系统，不是具身研发控制台。现有运动执行器、全局进程清理、默认归位和认证缺口需要替换或改造。保留 `guide_robot` 的导航/TTS作为后续可选任务插件，不作为基础操作台启动依赖。

## 已实现的用户功能（源码证据，不代表F1-09已部署）

| 功能 | 已有内容 | 缺口/边界 | 主要证据（归档内相对路径） |
|---|---|---|---|
| 浏览器操作台 | Flask/Jinja网页：配置、日志、机器人控制、YAML预览、登录页；前端同源fetch | 不是本地打开一个HTML就能连机器人；后端与ROS依赖必须先部署。默认HTTP `0.0.0.0:10001`，桥接器只听 `127.0.0.1:10002` | `config_tool/web_panel/app.py:393`、`:1213`；`robot_bridge.py:22` |
| 预定义姿态/动作库 | 编辑双臂14+头3目标；具名姿态附body档位；动作类型standby / hold_pose / bag_replay | 保存的是输入目标，不是采集当前实测姿态；无机器人/标定版本绑定；固定standby来自历史MCAP首帧 | `templates/config.html:137`、`:1268`；`app.py:512`；`src/arm_replay/config/arm_replay_config.yaml:13`；`arm_move_tool.py:30` |
| 机械臂与头部点选运动 | arm14+head3目标，30Hz线性插补，默认3秒；固定复位 | 无灵巧手/夹爪网页操控；无工具坐标点动；无完整目标限位/速度/碰撞检查；回放程序另有夹爪历史消息 | `arm_move_tool.py:23`、`:39`、`:91`；`app.py:907` |
| 腰部升降档位 | Web编辑并运行body_j3/j4/j5角度组合；Ruckig腰部插补；配置存在0..6、100、200档，可改角度 | **是角度档位，不是cm/m物理高度。** `--duration` 对腰部Ruckig无效；不保证身体URDF映射、物理安全或F1-09适配 | `src/arm_replay/config/arm_replay_config.yaml:60`；`smooth_interpolate_ros_rg.py:94`、`:577`、`:673`；`app.py:937` |
| 配置保存与导入导出 | 网页表格→Excel→生成导航/动作YAML；导航方案复制/重命名/删除，Excel导入导出 | 新平台应把姿态/任务存为受版本管理的JSON/YAML；Excel作为导入导出适配；目前“部署并重启”覆写install并重启服务 | `app.py:468`、`:557`、`:997`、`:1057`、`:1082`；`templates/config.html:42` |
| 动作/导览序列 | 底盘到展位→手臂/头部动作+TTS；多语言配置；顺序/语音/手动/待机状态机；暂停、继续、跳过、重启导览 | 属于展厅任务序列，不是通用具身任务编辑器；导航地图/点位与TTS服务由外部系统提供；归档不是完整SLAM/TTS引擎 | `src/guide_robot/src/guide_robot_node.cpp:130`、`:315`、`:365`、`:1444`；`templates/control.html:15` |
| MCAP示教动作回放 | 包含20份动作MCAP；按录制时间播双臂、头部和左右夹爪，带进度反馈/起点混合 | 回放将历史关节/夹爪**反馈**重映射为命令；不是新的采集程序，也不是LeRobot训练数据审阅器 | `src/arm_replay/src/arm_replay_node.cpp:101`、`:1147`、`:1321` |
| 手动底盘移动 | C++ guide_robot的键盘W/S前后、A/D旋转，速度档位、避障开关 | **Web页没有底盘摇杆**，Web的4个键只是导览流程控制；键盘launch无TTY时不可用；未见横移UI | `src/guide_robot/src/guide_robot_node.cpp:1348`、`:1370`；`templates/control.html:21`；`readme.md:183` |
| 状态与诊断 | 电池状态、服务状态、实时SSE日志；机械臂插补日志进度；Action消息进度 | 没有全身实时关节/误差/控制源页面，无URDF、三路相机、里程计可视化。桥接器HTTP活着不等于硬件ready；电池缓存无过期判定 | `robot_bridge.py:34`、`:58`、`:103`；`templates/control.html:68`、`:118`；`app.py:976` |
| 离线流程模拟 | mock arm / TTS / guide测试节点 | 只是流程模拟；不能据此写真机动作通过。网页会注入明确带TEST的模拟日志，新操作台应把模拟状态独立显示 | `src/test_nodes/`；`app.py:118`、`:153` |

## 启动与依赖

- `readme.md:5,75`称ROS Humble，但实际 `start_all.sh:68-72`固定ROS Jazzy、Domain 0、CycloneDDS系统配置；必须以F1-09已安装版本适配，不能按README安装Humble。
- C++ ROS2包：`guide_robot`、`arm_replay`、`arm_interface`、本地同名`move/server/common_msgs`接口；依赖rclcpp/action、sensor/control msgs、rosbag2 MCAP、yaml-cpp、spdlog/fmt；不要覆盖机器人已安装的同名接口包，先核对定义/类型哈希。
- Python网页Flask/openpyxl；机器人侧rclpy/sensor_msgs；升降依赖ruckig。`install_web_panel.sh:43-53`会pip安装依赖，`:59`写systemd；不应原样作为我们的部署步骤。
- `start_all.sh:74-88`会广泛pkill同名`arm_replay_node`/`guide_robot`进程；`stop_all.sh:38-41`同样全局清理。`app.py:773-794`为拉起桥接器也pkill所有`robot_bridge.py`。这些缺乏任务目录/PID身份约束。
- `start_all.sh:104`传`-m 0`。**并非启动立刻自动导航：**`guide_robot_node.cpp:229-235`先进入STANDBY等待RT，但STANDBY会取消任务、发底盘零速度并请求`action_id=0`机械臂归位（`:266-305`）。所以“打开完整服务不运动”并不成立。
- Hall Web访问`/api/robot/status`就会启动bridge子进程（`app.py:814-824`），不是纯状态读取；本次没有请求它。

## 必须先改的执行边界

1. **反馈缺失不允许继续假设零位。** `arm_move_tool.py:48`初始化全0；`:60-68`收到任意一帧即got_joint，没有确认17轴完整；`:74-80`等待10秒超时仍以0插补。移动中没有spin更新反馈，结束仅表示命令序列发送完毕（`:101-120`），不是到位验收。C++ standby/hold_pose也有部分轴保持0、反馈无新鲜度检查，并优先采用3秒内最后指令作为起点（`arm_replay_node.cpp:916-958`、`:1362-1399`）。
2. **停止语义不一致。** Web `/api/motion-stop`只terminate当前Web子进程（`app.py:964-973`），不停止Hall action队列/Pico/其他发布者，也不确认实际停止；关闭运动弹窗只断SSE（`templates/config.html:1438`），浏览器断连无服务端控制租约。C++ action取消会打断手臂插补/回放，但升降取消故意等脚本自然走完（`arm_replay_node.cpp:734-759`），不能把按钮叫“整机急停”。
3. **控制权范围太窄。** Web只用单进程锁和`_motion_proc`防重复（`app.py:919`、`:946`）；检查与后台线程创建间还存在竞争窗口。C++ action server始终接收排队（`arm_replay_node.cpp:278-305`），只管理自己的队列。Pico、手柄、Web点动、Hall和policy直接发布会争抢；新平台应建立统一模式/所有权，未改造的Pico先作为独占模式运行，其间禁用Web运动。
4. **后台动作边界需统一。** Web API手臂只检查有targets，没有服务端逐轴类型/finite/限位/持续时间充分校验；字符串拼shell的运行器应改为固定argv和数值schema（`app.py:861-890`、`:907-959`）。全局kill/安装目录覆写/重启系统服务不能留在普通动作页。
5. **认证不完整。** 归档有硬编码默认认证值（不在此记录）；多个写API缺`login_required`：`/api/excel/save` `:468`、`/api/generate` `:1057`、`/api/deploy` `:1082`、`/api/restart` `:1108`；其余页面登录不等于这些路由受保护。迁入时使用自有登录配置和统一API认证，不原样发布到LAN。
6. **升降时间/成功语义需重做。** `smooth_interpolate_ros_rg.py:87-89`是该工程自定150deg/s、200deg/s²、600deg/s³，不代表F1-09厂家限制；`:577-618`Ruckig按限值生成时长，`:680`明确duration仅非腰部生效；缺库或计算失败回退固定3秒S曲线。`run_hybrid`限位拒绝返回False后main没有统一处理返回码，Hall父进程可能只凭exit0误判成功；动作成功应结合完整新鲜反馈/目标误差判定，参数以F1-09现场验证为准。
7. **状态不能伪装ready。** `robot_bridge.py:104-110`status只答自身ok，电池旧缓存不会过期；Web显示“ROS已连接”仅据此（`control.html:75-78`）。新页面分开网络可达、ROS发现、驱动在线、反馈新鲜、控制权可用和可执行；目标角度、已发命令、实测姿态三种信息分别显示。

## 集成落点

- **Hall负责省前端工作：**保留配置表单/预设库/导入导出/运行日志的交互，把“展厅点位”从主页面移到可选demo任务。移植时统一后端路径和JSON schema，机械臂/身体实际发送由我们适配后的执行器负责。
- **meaxiao负责采集链路：**Pico遥操、精调/离合、数采、质量诊断、自动传输和视频曲线审阅；lxl保留原链路对照与历史MCAP兼容，不并行启动三套控制发布者。
- **自己的现有工具负责实验闭环：**已做的具名实测姿态capture/restore、局部里程计归位、replay检查、控制源监测、数据合同与训练验收接统一后端，逐台确认后再启用。
- **统一操作台：**①连接/健康+实测URDF/三相机；②关节/夹爪/腰档位/底盘按住移动；③实测姿态捕获、预设/demo、复位；④Pico与数采任务；⑤数据传输/审阅/预处理状态；⑥日志/版本/实验记录。第一版先做①②③+采集生命周期，再接完成episode审阅页；policy控制另作为同一控制源管理下的插件。
- **输入192.0.2.3：**可作为最终局域网入口，但必须在F1部署独立网关服务且确认LAN路由/端口开放。Hall原入口为`http://192.0.2.3:10001`（仅说明源代码默认端口，**不说明现在存在服务**）；若希望省端口，可用80/443反向代理，但不能覆盖F1现有厂家80网页。6000D主机目前反向SSH可达不代表浏览器可直接路由到31网段。
- **统一入口而非立即融合全部运行时：**同源网关提供固定入口，机器人侧实时控制/录制本地运行；6000D保留重数据处理与审阅。审阅站连不上时控制台仍显示本地采集状态；不要把控制网络请求依赖6000D的文件扫描/转码。

## 不应复用成“通用基础设施”的内容

历史展厅文案/导航点位/固定standby与身体角度、20份动作MCAP均为任务和机型相关资产；只作格式参考，不直接在F1-09执行。TTS外部服务、SLAM/导航内核、本地同名底层接口包不是此归档自研出的完整能力，不需要重新搬运其底层实现。URDF/相机/数据审阅/真实姿态快照属于需要接入其他已有模块或新增的能力，不能写成Hall现成支持。
