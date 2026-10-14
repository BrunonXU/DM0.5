> 历史：这是 2026-10-09 收编前的 G2Teleop README 和原文档索引。阶段性“当前”不作为现役状态；现状见 [G2 进度](../RECORDING_AND_PROGRESS.md)。

# G2Teleop

G2 饮料瓶分拣的 Logitech F710 人工介入与 DAgger 数据采集项目。

本项目与 `DM0.5`、`g2_robot` 同级，避免把手机通信、运动学、人工接管状态机和
纠正数据暂存混入训练仓库或现场机器人仓库。

最重要的设计、接口和操作流程见：

- [完整文档索引](README.md)
- [Windows/G2 唯一启动入口](START_HERE_WINDOWS_G2.md)
- [F710 → Pink → OTG 当前状态与实施路线](F710_PINK_OTG_CONTROL_ARCHITECTURE.md)（当前控制架构权威文档）
- [F710 DAgger 全流程](../G2_F710_DAGGER_FULL_FLOW.md)
- [运动学与 Pink IK 计划](G2_KINEMATICS_AND_PINK_IK_PLAN.md)
- [Windows 到 G2 Shadow 手册](WINDOWS_TO_G2_SHADOW_RUNBOOK.md)
- [iPad 备用方案](../G2_IPAD_DAGGER_FULL_FLOW.md)

当前已完成 Windows F710 输入、直接 SSH tunnel、Shadow、Live IK Shadow、Pink IK 和限定作用域的
Powered 双臂/O10 控制。当前主要阻塞不是“链路尚未接通”，而是持续移动时 Pink reference 小反转、
固定 workspace 球面、求解后裁剪以及命令/反馈缺少闭环观测造成的抖动与偶发重新接管。

> **安全阻塞：禁止把 F710 接收器再次插入当前 G2。** 2026-08-27 现场确认厂商常驻
> `teleop_main_node`会自动消费 joystick 并控制底盘；即使本项目脚本只读，也无法阻止另一个
> 已运行进程同时处理手柄。见 [事故记录](INCIDENT_20260827_F710_BASE_MOTION.md)。

## 当前状态

- [x] 核对正式 G2 Split 输出接口；
- [x] 冻结第一版 F710 逻辑按键映射；
- [x] G2 已识别 F710 `046d:c21f` XInput、`js0/event5`；
- [!] 发现厂商常驻遥操进程会自动将 F710 映射到底盘，G2直连方案已安全阻塞；
- [x] 建立不依赖第三方包的 Linux joystick 只读探测器；
- [x] 建立Windows pygame F710只读探测器和互锁映射；
- [x] 建立版本化意图协议、单包ACK、sequence/session校验和watchdog；
- [x] 建立仅监听G2回环地址、无GDK的Shadow receiver；
- [x] 建立Windows到G2的直接SSH tunnel脚本；
- [x] 建立 HEBI Mobile I/O 只读接收器；
- [x] 建立不依赖 GDK 的输入状态机和测试；
- [x] 在Windows完成F710实际轴号、范围、方向与LB/RB只读验证；
- [x] 完成Windows-G2 Shadow通信、ACK、HOLD与watchdog基础测试；
- [x] 获取并验证G2厂商模型结构、左右7DOF与对称腕/掌frame；
- [x] 定位并哈希冻结G2厂商`G2_t2_crs + o10_t2`模型来源；
- [x] 用G2 Pinocchio 3.7验证完整MJCF与左右7DOF reduced model结构；
- [x] 从3条代表H5均匀生成75帧、无图像无action的FK数值样本；
- [x] 经用户明确授权将FK数值样本传入G2并运行候选frame/固定变换审计；
- [!] H5已确认xyzw、左右顺序和方向；绝对`bottle_grasp_frame`位置仍未达到2mm门槛；
- [x] 锁定Pink/Pin/ProxQP并完成相对微增量IK严格回放1200/1200；
- [x] 实现并测试读取真机状态但绝不下发的Live IK Shadow receiver；
- [x] 完成Live IK Shadow现场验收与日志审计；
- [x] 完成适配当前任务的O10双协同控制与安全保持；
- [x] 完成人工授权后的限定作用域Powered微动作验证；
- [!] 持续平移仍明显抖动；最新证据优先指向真实100Hz发送时基和近端关节动力学，而非单一Pink反转；
- [x] 完成input/Pink/OTG/GDK/feedback T1–T6分层日志与可视化；
- [x] 完成约束感知Pink和官方Ruckig关节OTG实验实现；第一次Powered未通过，尚非默认；
- [x] 增加真实GDK dispatch时间、轻量`timing-ab`档和离线配对分析器；
- [ ] 完成同一Home的direct/OTG单轴Powered timing A/B并决定下一层根因；
- [ ] 接入 DM0.5 DAgger 采集与训练。

## 当前下一步

当前下一步按[控制架构与实施路线](F710_PINK_OTG_CONTROL_ARCHITECTURE.md)执行P2.1：在同一
Home、同一动作下依次运行`direct timing-ab`和`otg timing-ab`，先验证真实GDK dispatch
P95/P99和长周期，再决定是否做进程级控制隔离或继续调整Cartesian/Pink/OTG。稳定性验收前
不提高最大速度，也不切换无碰撞保护的low-latency。

## Linux F710 只读测试（保留诊断，禁止在G2运行）

F710 背面置于 `X` 模式。优先使用稳定的 by-id 路径：

```bash
G2Teleop/scripts/01_f710_readonly.sh \
  --device '/dev/input/by-id/usb-Logitech_Wireless_Gamepad_F710_1BF994E3-joystick'
```

脚本只用 Python 标准库读取 Linux joystick event，不导入 GDK、不发送动作。逐个操作
左右摇杆、LT/RT、十字键和 A/B/X/Y/LB/RB，并保存输出用于冻结真实轴号和方向。

## 只读 iPad 测试（备用）

先阅读完整流程。测试环境不是 G2 域控时，可在独立 Python 环境安装：

```bash
python3 -m pip install -e './G2Teleop[ipad]'
python3 G2Teleop/scripts/01_ipad_readonly.py
```

iPad 与电脑必须在同一局域网，HEBI Mobile I/O 保持前台，设备标识为：

```text
Family: HEBI
Name:   mobileIO
```

脚本只打印 ARKit 位姿、模拟量和按钮，不连接机器人。


---

## 原文档索引（完整历史正文）

# G2Teleop 文档索引

本目录集中保存 G2Teleop 的设计、运行、安全和历史文档。仓库根目录只保留项目入口
`README.md`，以符合常规仓库浏览和安装工具的默认行为。

## 当前应先阅读

**2026-09-07交接入口：** [当前状态、证据边界与下一步](../HANDOFF_20260907_F710_HIL_RECORDING.md)。
本页下方9月1日/2日记录是历史阶段；当前F710已冻结，HIL是纯逻辑原型，视频sink仍待实现。

1. [Windows/G2 唯一启动入口](START_HERE_WINDOWS_G2.md)：现场启动命令、窗口职责和退出方式。
2. [F710 → Pink → OTG 当前状态与实施路线](F710_PINK_OTG_CONTROL_ARCHITECTURE.md)：当前问题证据、目标控制链、日志契约、P0–P4 路线和验收门。
3. [F710 DAgger 全流程](../G2_F710_DAGGER_FULL_FLOW.md)：按键、通信、安全边界和最终采集流程。

## 专项设计与操作

- [G2 运动学与 Pink IK 计划](G2_KINEMATICS_AND_PINK_IK_PLAN.md)
- [Windows 到 G2 Shadow 操作手册](WINDOWS_TO_G2_SHADOW_RUNBOOK.md)
- [iPad DAgger 备用方案](../G2_IPAD_DAGGER_FULL_FLOW.md)

## 安全与历史

- [2026-08-27 F710 触发底盘误动作事故记录](INCIDENT_20260827_F710_BASE_MOTION.md)

历史文档中的阶段性状态可能已经被后续真机验证覆盖。发生冲突时，控制架构文档中的“已冻结决策”、
根目录 `README.md` 的当前状态以及唯一启动入口优先；Powered 操作始终以现场安全条件为最高优先级。

2026-09-01：Ruckig velocity OTG已完成历史回放、zero-output Shadow和第一次Powered实验；
Powered仍明显抖动并出现一次lag HOLD，未通过验收。当前进入发送时基优先的`timing-ab`阶段：
真实100Hz dispatch完整保留、重型分层降到20Hz、T5反馈显式关闭；OTG仍是显式实验开关。

2026-09-02：新增可归因的BC对照链，将冻结示教的双臂关节轨迹先经FK转换为左右EEF，再完整
经过input shaping、Pink、约束和Ruckig。原31.6秒时标会大量削顶，因此唯一Powered入口采用
91秒速度重参数化并固定low-latency双臂Servo；详见控制架构文档14.3节。
