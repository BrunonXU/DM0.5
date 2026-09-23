---
doc_type: incident
domain: g2_hardware
status: frozen
created: 2026-08-24
updated: 2026-08-24
date_confidence: unknown
models: [dm05]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../../../../g2_robot/docs/G2_TROUBLESHOOTING.md]
---

# 头部相机持续返回空帧

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | 关节DDS、mode=5和腕部相机正常，头相机连续 `Frame is null` |
| 影响 | 三路RGB合同不成立，禁止Shadow和Powered |
| 根因 | 本次为上半身重启前头相机发布链路未正常工作 |
| 如何解决 | 按机器人正常流程重启上半身，三路相机恢复 |
| 当前状态 | 已解决；复发时按同样门禁诊断，不能自动假定同一根因 |

## 第一性根因与安全边界

模型训练合同包含三路真实RGB。用黑图补位会把“传感器缺失”伪装成“合法观测”，服务仍可能返回数值正常但语义未知的动作。因此正确失败语义是阻止请求/动作，而不是保持接口形状。

重启后还需核对系统时间；日志进入1970目录说明时钟未同步，不能将其与相机恢复混为一项成功。
