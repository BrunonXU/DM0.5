---
doc_type: reference
domain: g2_control
status: canonical
created: 2026-08-24
updated: 2026-09-14
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [robot_index]
supersedes: []
superseded_by: []
related: []
---

# Robot：部署、延迟、RTC和真机

## 控制根因与交接

- [控制根因与最新基线](G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)：完整保留原 `SUPER_IMPORTANT/` 文档；第0节记录09-08单34D工作基线、双17D回退和证据限制。
- [09-07控制待办快照](G2_DM05_CONTROL_TODO.md)：Servo布局、平滑重评、RTC迁移三项原始待办；布局部分已被09-08记录更新。
- [09-07新对话交接快照](G2_DM05_NEW_CHAT_PROMPT.md)：当时的背景、阅读顺序和协作约束，使用前先看上述最新基线。

以上三篇于2026-09-14归位；保留历史正文，未重新进行真机验收。

## 原理与架构

最重要的七份文档：

- [`INFERENCE_LATENCY_FIRST_PRINCIPLES.md`](INFERENCE_LATENCY_FIRST_PRINCIPLES.md)：五步去噪、fixed-prefix compile、E2E耗时与网络优化。
- [`RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md`](RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md)：30/100Hz、chunk接缝、deadline与卡顿。
- [`RTC_PROCESS_ISOLATION.md`](RTC_PROCESS_ISOLATION.md)：Control唯一拥有Robot、Policy唯一拥有Camera的目标架构。
- [`TRANSPORT_BINARY_WEBSOCKET.md`](TRANSPORT_BINARY_WEBSOCKET.md)：原始JPEG透传、WebSocket长连接和float32动作协议。
- [`ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md`](ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md)：从chunk内部、chunk之间、执行端与底层实时性拆解平滑问题，并维护当前优化优先级。
- [`JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md`](JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md)：定义模型目标、实际命令和34维硬件反馈的采集合同，以及定位抖动所需的科研图与判读规则。
- [`MOTION_DIAGNOSTICS_DASHBOARD_IMPLEMENTATION.md`](MOTION_DIAGNOSTICS_DASHBOARD_IMPLEMENTATION.md)：把三层诊断落成每次真机后的静态科研图、倒序run页面、反馈probe和安全降级流程。

延迟、RTC、平滑或真机A/B数据在 [`experiments/`](experiments/README.md)。G2/GDK/控制问题在 [`incidents/`](incidents/README.md)。

现场执行命令只看 [`../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md`](../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md)，本目录不复制命令。
