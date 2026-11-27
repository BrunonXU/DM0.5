# V62 真机批次摆位与结束状态记录（2026-11-27）

| 试验 | 运行 ID | 桶位姿 `[x, y, z]` (m) | 积木位姿 `[x, y, z]` (m) / 偏航 | 到达阶段 | 积木最终位置（操作者反馈） | 备注 |
|---|---|---|---|---|---|---|
| Trial 1 | `web-9c688a6923824b3cbffa423c2331edf0` | `[0.718, -0.045, 0.772]`（中距） | `[0.886, -0.282, 0.548]` / `-85.0°` | S5 完成后卡在 S6 前 | 仍在夹爪里（抓取完成、运输路线 OK、放置前停住） | `owner_bridge.py:1399` 飞行轮询误记 `waypoint_tracking_failed` 导致 H-32/H-42/H-39 同时作废 |
| Trial 2 | `web-776cd3da7ed14c79b03e634ad0d154ba` | `[0.818, -0.068, 0.773]`（远侧，桶入右腕） | `[0.856, -0.284, 0.547]` / `-97.3°` | S5 完成后卡在 S6 前 | 仍在夹爪里（抓取完成、运输路线 OK、放置前停住） | H-36 成功过 S0；同 Trial 1 误记 `contact_fault` |
| Trial 3 | `web-264b0b4195c84d22b9fd3528d4d92504` | `[0.638, -0.040, 0.769]`（近身） | `[0.820, -0.312, 0.546]` / `-35.6°`（斜摆） | S1 `approach_grasp` 后验证超时 | 仍在桌面（未闭爪） | 到位后 `mask_view=right:mask_truncated:object=square-1` + 头部无掩码 |
| Trial 4 | `web-98ed99a8ea1b4480926824192e2010c6` | `[0.638, -0.040, 0.769]`（近身） | `[0.819, -0.312, 0.546]` / `-36.1°`（斜摆） | S1 `approach_grasp` 后验证超时 | 仍在桌面（未闭爪） | 到位后 `mask_view=right:mask_truncated:object=square-1` + `cross_camera_partial_association:camera=head` |
