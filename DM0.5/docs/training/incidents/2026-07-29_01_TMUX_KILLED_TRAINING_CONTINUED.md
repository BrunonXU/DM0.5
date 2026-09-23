---
doc_type: incident
domain: server
status: frozen
created: 2026-07-29
updated: 2026-08-24
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: []
superseded_by: []
related: [2026-07-28_05_LAUNCHER_STOPPED_CONTAINER_ALIVE.md]
---

# tmux误杀后训练容器仍继续运行

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | tmux、docker client和tee退出，文件日志停在step 31,759 |
| 影响 | 容易误判训练已停并从checkpoint再启，造成双训练 |
| 根因 | Docker daemon继续托管容器，日志观察链断开不等于计算链停止 |
| 如何解决 | 查容器/GPU PID/docker logs，补齐日志并把tmux只作为查看器接回 |
| 当前状态 | step连续、无回滚、参数dtype未改变，guardian保留退出码 |

## 证据与恢复逻辑

`docker logs`显示step已超过31,800，说明唯一正确动作是重新连接观察面，而非新建训练面。从31,760边界补日志并抽查相邻step各出现一次，排除了重复和缺口。

恢复顺序必须从真实工作负载向外：container → GPU rank → step → 日志视图。不能从“tmux不在”向内推断容器不存在。
