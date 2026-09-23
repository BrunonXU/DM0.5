---
updated: 2026-09-23
type: robot_documentation_index
---

# F1 系统使用与迁移文档

搬走或换机先看[实际版本与已保全范围](version_audit_20260917.md)，再按[迁移手册](migration_runbook.md)核对源码、外部数据、底层依赖和运行证据。目录沿用现有 `F1/docs/`，不另建平行 `doc/`。

| 文档 | 回答的问题 |
|---|---|
| [F1-infra复用清单与控制台方案](../F1-infra.md) | 完整Pico/数采网页、域控和启动分工、协议/设备台账、HTML/URDF接入（2026-09-23） |
| [具身研发工作台一体化方案](embodied_console_integration.md) | 已选4242入口；Hall、Pico、数采、审阅、实验怎样组合及分阶段验收 |
| [Hall源码审计](hall_source_review_20260923.md) | 已有调姿/升降/配置/导览功能，执行/停止/反馈限制和具体源码行号 |
| [lxl / meaxiao Pico比较与引入](pico_comparison_and_adoption.md) | 新版选型、56文件参考快照、数据平台复用边界与F1-09适配清单 |
| [4F-F1身份与旧机差异](robots/4F-F1.md) | 新机器人名称、连接、已核对一致项/差异项、独立配置、部署及现场阻塞 |
| [2026-09-17版本审计](version_audit_20260917.md) | 服务器是否有部署原件、F1实际入口指向什么、哪些内容已取回、哪些仍未覆盖 |
| [迁移与交接手册](migration_runbook.md) | 搬离前保全顺序、外部数据/模型、不同架构依赖、连接身份、新环境验收和回退 |
| [系统架构与接口经验](system_architecture.md) | 两机分工、目录组织、ROS/SDK依赖、单位/坐标/时钟、控制与反馈关系 |
| [replay和底盘经验](replay_lessons.md) | 真实日志中的症状、原因、处理、验证边界，以及底盘/身体/相机/DDS常见坑 |
| [持久连接](connect/README.md) | SSH隧道、密钥分工、自动重连与网络排障 |
| [通用示教数据工作流](dataset_workflow.md) | 新任务的采集预检、清洗、分组、发布和验收 |

系统事实统一见[系统配置](../F1系统配置.md)，任务最新选择及完成度只看[泡沫任务当前方案](../foam_sort_whole_body/docs/training_plan.md)；具体启动和操作见[replay runbook](../foam_sort_whole_body/docs/replay_runbook.md)。本组经验文档与日期审计不替代持续更新的任务状态，也不将离线通过写成实机通过。
