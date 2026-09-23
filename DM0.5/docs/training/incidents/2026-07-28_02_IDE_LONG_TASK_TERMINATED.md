---
doc_type: incident
domain: server
status: frozen
created: 2026-07-28
updated: 2026-08-24
models: [dm05]
hardware: [rtx6000d]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../archive/legacy_mixed/OPERATIONS_LESSONS_20260728.md]
---

# 长任务绑定IDE终端后被终止

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | base下载和两个rsync在切换工作区后收到SIGTERM |
| 影响 | 任务停止，但断点文件仍存在 |
| 根因 | 任务是IDE/Codex终端子进程，生命周期继承自交互会话 |
| 如何解决 | 迁移到独立后台任务管理并保留可恢复下载参数 |
| 当前状态 | 已恢复；长期训练后来使用tmux/容器监督 |

## 第一性根因与预防

一个计算任务能否长期存活，取决于它的监督者和进程生命周期，而不是命令本身“看起来在后台”。终端关闭时，其子进程可能收到挂断或终止信号。正确合同是：任务由独立监督器持有，日志有独立持久位置，失败可重启，断点可验证。

恢复时短时间文件大小不增长可能是在校验断点，不能仅据此判死。应同时观察进程、I/O和日志。
