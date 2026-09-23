# G2Teleop 文档索引

本目录集中保存 G2Teleop 的设计、运行、安全和历史文档。仓库根目录只保留项目入口
`README.md`，以符合常规仓库浏览和安装工具的默认行为。

## 当前应先阅读

**2026-09-07交接入口：** [当前状态、证据边界与下一步](HANDOFF_20260907_F710_HIL_RECORDING.md)。
本页下方9月1日/2日记录是历史阶段；当前F710已冻结，HIL是纯逻辑原型，视频sink仍待实现。

1. [Windows/G2 唯一启动入口](START_HERE_WINDOWS_G2.md)：现场启动命令、窗口职责和退出方式。
2. [F710 → Pink → OTG 当前状态与实施路线](F710_PINK_OTG_CONTROL_ARCHITECTURE.md)：当前问题证据、目标控制链、日志契约、P0–P4 路线和验收门。
3. [F710 DAgger 全流程](G2_F710_DAGGER_FULL_FLOW.md)：按键、通信、安全边界和最终采集流程。

## 专项设计与操作

- [G2 运动学与 Pink IK 计划](G2_KINEMATICS_AND_PINK_IK_PLAN.md)
- [Windows 到 G2 Shadow 操作手册](WINDOWS_TO_G2_SHADOW_RUNBOOK.md)
- [iPad DAgger 备用方案](G2_IPAD_DAGGER_FULL_FLOW.md)

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
