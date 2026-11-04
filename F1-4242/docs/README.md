# 文档索引

首次使用只需先读 [项目介绍与四步部署](../README.md)。部署人员按 [SHARING](SHARING.md) 完成本机核对；Pico、手柄 DAgger、视频服务器和训练导出各自按需阅读，无需通读全部技术资料。

当前行为与验收范围以 [STATUS](STATUS.md) 为准。本文保留技术主题和历史证据入口，遇到具体问题再查；Agent 工作流程在 [AGENTS](../AGENTS.md)。

| 目的 | 文档 |
|---|---|
| 四项初始化、自动检测与排错 | [INITIALIZATION](INITIALIZATION.md) |
| 部署、换机、配置与打包 | [SHARING](SHARING.md) |
| 本地原件与可选视频服务器的区别 | [可选视频服务器](SHARING.md#可选视频服务器) |
| 免密码与单击执行、入口须知 | [DIRECT_OPERATION](DIRECT_OPERATION.md) |
| 四页布局、采集流程与发布边界 | [WORKSPACE_LAYOUT](WORKSPACE_LAYOUT.md) |
| 日常使用：状态、接管、相机、调姿、数据 | [USER_GUIDE](USER_GUIDE.md) |
| 常驻启动、日志与资源 | [AUTOSTART](AUTOSTART.md) |
| 全身关节、模型/驱动配置与标定状态 | [JOINT_CONFIGURATION](JOINT_CONFIGURATION.md) |
| 姿态交互与一键任务 | [POSE_TASK](POSE_TASK.md) |
| 六页原风格精修、接口增量和交付验收 | [UIUX_UPGRADE](UIUX_UPGRADE.md) |
| 当前复位卡点、范围依据与修复记录 | [RESET_REPAIR](RESET_REPAIR.md) |
| 域控重启后的头部异常与首次执行时间误报 | [2026-10-26 现场诊断](incidents/20261026-head-feedback.md) |
| 姿态后抽动、域控失联与风扇停转 | [2026-10-29 只读取证与未定因边界](incidents/20261029-domain-power-loss.md) |
| 运动规划、限位、底盘与升降 | [POSE](POSE.md) |
| F710 HIL 启动指引、状态与诊断 | [HIL](HIL.md) |
| Pico 网页启动/停止、两种流程 | [PICO](PICO.md) |
| 可选上肢 Pico 的现场操作和依赖 | [Pico README](../integrations/pico_upper/README.md) |
| 独立 16D 录制及 action/state 合同 | [RECORDING](RECORDING.md) |
| 录制合同专项复核 | [RECORDING_CONTRACT_REVIEW](RECORDING_CONTRACT_REVIEW.md) |
| 统一落盘、官方 v3 转换、队列与训练发布 | [UNIFIED_DATA](UNIFIED_DATA.md) |
| 数据目录、首帧、可迁移备注与回放 | [DATA](DATA.md) |
| 实机 Replay、起点与执行记录 | [REPLAY](REPLAY.md) |
| Replay 本机停顿、GC 与锁等待排查 | [延迟排查](incidents/20261023-replay-latency.md) |
| HTTP API | [API](API.md) |
| 系统架构 | [ARCHITECTURE](ARCHITECTURE.md) |
| ROS 适配器 | [robot/README](../robot/README.md) |
| URDF、模型与显示 | [MODEL](MODEL.md) |
| URDF与原生运动学的独立核验 | [MODEL_AUDIT](MODEL_AUDIT.md) |
| 版本、现场事实、未验收项 | [STATUS](STATUS.md) |
| 早期设计和外部证据来源 | [初始整合记录](history/initial_integration.md)、[来源索引](history/SOURCES.md) |

`history/` 是归档，不作为当前部署命令。文中 `results/` 和原 F1 工作区的路径用于追溯历史，未随源码包提供，详见来源索引。

- [HIL三路线交付与验收](HIL_ROUTES_RELEASE.md)

视频链路历史排查：[MP4传输优化](incidents/20261016-server-preview-transfer.md)、[旧版认证故障](incidents/20261016-preview-worker-auth.md)。当前免密码连接以 [DIRECT_OPERATION](DIRECT_OPERATION.md) 为准。
