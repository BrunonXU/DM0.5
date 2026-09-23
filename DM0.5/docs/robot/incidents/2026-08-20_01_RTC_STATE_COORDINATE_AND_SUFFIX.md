---
doc_type: incident
domain: rtc
status: frozen
created: 2026-08-20
updated: 2026-08-24
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [2026-08-18_01_RTC_ROLLBACK.md, ../RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md]
---

# RTC state坐标错误与full-50平滑污染suffix

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 修正RTC切换真实delay后，C0差为0仍出现回拉 |
| 影响 | 表面位置连续但后续速度/轨迹方向错误，真机仍不安全 |
| 根因 | 服务端RTC使用错误state坐标；对完整50步先平滑，改变了本应由RTC约束的可执行suffix |
| 如何解决 | 统一relative/absolute坐标合同；RTC约束和suffix选择后再做受限平滑 |
| 当前状态 | 离线递归回放与安全门已修；现场后续问题转为deadline jitter |

## 第一性根因

C0连续只约束切换点位置相同，不约束下一时刻速度和更长suffix的方向。relative action必须相对与训练/推理一致的state原点转换；坐标原点错了，即使第一个点被投影到当前值，后续点仍指向错误绝对位置。

同理，RTC把committed prefix当作不可改变的边界条件。若先对完整50点做全局平滑，算法会让prefix之外的点反过来影响可执行suffix，等于在RTC之后修改了它保证过的解。正确顺序本身是安全合同，而不只是实现细节。
