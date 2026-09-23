---
doc_type: incident
domain: data
status: frozen
created: 2026-07-28
updated: 2026-08-24
time_order_confidence: unknown
models: [dm05, old_lora, new_lora]
hardware: []
canonical_for: []
supersedes: []
superseded_by: []
related: [../DATA_AUDIT.md]
---

# H5帧号不能直接作为视频帧号

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | H5顶层帧号从0连续，但头/腕视频起始偏移不同，腕部还有重复画面 |
| 影响 | 图像与state/action错帧，训练得到视觉与动作的错误因果对应 |
| 根因 | H5样本索引、各相机视频编码帧号和采集时间戳是三个不同坐标系 |
| 如何解决 | 每路读取 `.txt` 时间戳，用H5 `timestamp/camera/*` 精确查表；匹配失败即拒绝episode |
| 当前状态 | 数据工具已按时间戳对齐，禁止退化为帧号猜测 |

## 第一性根因

多传感器样本的共同坐标不是“第几个元素”，而是采样时间。两个流都从索引0开始，只说明各自数组的起点，不说明物理世界中的同一时刻。静默按索引配对会产生格式正确、语义错误的数据，这是比显式报错更危险的训练污染。
