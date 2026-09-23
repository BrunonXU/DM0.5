---
doc_type: incident
domain: server
status: frozen
created: 2026-07-28
updated: 2026-08-24
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: []
superseded_by: []
related: [2026-07-29_01_TMUX_KILLED_TRAINING_CONTINUED.md]
---

# Launcher停止但Docker训练仍存活

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 停止外层systemd/launcher后，Docker daemon内旧容器仍运行 |
| 影响 | 新容器启动后每卡出现两套进程，可能并发写同一output/log |
| 根因 | launcher、docker client与daemon托管的container是不同生命周期 |
| 如何解决 | 启停以精确container ID、GPU PID和step为准，再启动唯一容器 |
| 当前状态 | 重叠尝试未形成checkpoint，日志已单独保留，正式训练干净重启 |

## 第一性根因

停止父进程只能证明父进程退出，不能证明外部daemon拥有的工作负载退出。训练的真实身份应由三层共同确认：监督会话、Docker container、GPU rank进程。任何一层的“没有输出”都不能单独作为停止证据。

## 预防

迁移时依次停止launcher、检查 `docker ps`、检查GPU compute PID、停止精确容器、确认0训练进程，然后才启动新实例。禁止两个实例共享checkpoint输出目录。
