---
doc_type: incident
domain: transport
status: frozen
created: 2026-08-24
updated: 2026-08-24
date_confidence: unknown
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../../../g2_robot/docs/G2_TROUBLESHOOTING.md]
---

# G2直连6000D超时与非对称路由

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 6000D本机curl正常，但G2请求超时；早期只能经笔记本转发 |
| 影响 | HTTP推理链路不可用或引入额外中转变量 |
| 根因 | bridge容器回复被服务器策略路由送入Meta虚拟接口，请求与响应路径不对称 |
| 如何解决 | 推理容器使用host network监听7892，G2公司内网直连6000D |
| 当前状态 | GET 405证明路由/端口可达，multipart POST Shadow已成功 |

## 第一性根因

一个连接成立要求请求到达服务并且响应沿可达路径返回。服务器本机curl只验证loopback/本机路由，不能证明远端G2的回程。HTTP 405在这里不是推理成功，但证明TCP和HTTP服务可达且请求方法错误；`000/timeout/refused`才属于更底层失败。

直接通信恢复后，重复维护“笔记本中转版”和“直连版”完整文档已没有必要；历史拓扑只留在事故记录。
