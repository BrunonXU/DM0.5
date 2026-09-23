# F1-09 本机基础设施参考（2026-09-23）

通过既有SSH22009、固定主机密钥只读取回11份当前机器文件：基础启动、旧lxl启动/IP、Nginx、DDS、原生全身预设及motor/kinematics launch。来源、时间、解析路径和SHA256见 [manifest.json](manifest.json)。

本机没有`/home/user/meaxiao`；本快照不证明6000D的新版Pico已部署。配置文件的限位/模式也不等于运行参数；本轮未启动motor_ctl或动作。全身预设源码按发布轨迹结束记完成，缺少独立实测到位验收，固定目标不自动视为适用于所有机型。

动态只读证据见 [接入审计](../../connect/robots/f1-09/evidence/infra_readonly_20260923/README.md)，功能与整合结论见 [F1-infra](../../F1-infra.md) 和 [具身工作台方案](../../docs/embodied_console_integration.md)。
