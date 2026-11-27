# V60 真机 3 次批次摆位与过程记录（V6 预设）

**标准静态复位预设：** **V6**（`04a159f3c79c4b7a8ae9f9ffb06c7b4c`，`arm_l_j2 = -47.32°`）。
**上线预检记录：** 按操作者当轮明确要求跳过上线前 3 次预检，直接进入真机验证（已如实登记，未补写通过）。
**批次停止原因：** 操作者在完成 3 次真机（其中 2 次连续打进 S6 `align` 并同因 `numerical_plan_rejected:registered_candidates_exhausted` 停下、1 次在 S5 遇竞态停下）后明确要求整理数据交 Claude 审阅。

| 试验号 | 运行编号 | 实测初始桶位 `[x, y, z]` (m) | 实测初始积木 `[x, y, z]` (m) | 耗时 (s) | 到达阶段 | 核心机制表现 | 最终停止原因 | 收尾确认 |
|---|---|---|---:|---:|---|---|---|---|
| **Trial 1** | `web-ecd7898e2866457eb1b11def567d6381` | `[0.6634, -0.0332, 0.7728]`（近身） | `[0.8382, -0.2683, 0.5468]` | 58.22 | **S6 (`align`)** | H-37 近身运输通过；H-40 保住到位持块；H-39 右移 `0.11 m` 露出方孔 `[0.6666, -0.0328, 0.7681]`；首次进入 S6 | `align`: `numerical_plan_rejected:registered_candidates_exhausted`（`variant=1` 机载报 `bounded_IK_wall_time_exceeded`，离线 6000D 4.89 s 可解 21 路点且复核全过） | `measured_stationary`, `model_calls=0` |
| **Trial 2** | `web-1b689a7a87dd4dedb69570ca214c910a` | `[0.7896, -0.0455, 0.7723]`（**桶入右腕**） | `[0.8164, -0.2859, 0.5467]` | 53.24 | **S6 (`align`)** | **H-36 忽略桶身通过 S0**；H-40 保住到位持块；H-32 保留桶位姿 24 次；H-39 右移 `0.11 m` 露出方孔 `[0.7915, -0.0453, 0.7713]`；进入 S6 | `align`: `numerical_plan_rejected:registered_candidates_exhausted`（`variant=1` 机载报 `bounded_IK_wall_time_exceeded`，离线 6000D 3.81 s 可解 22 路点且复核全过） | `measured_stationary`, `model_calls=0` |
| **Trial 3** | `web-be099b9916b341acb3cab74d1f053b5d` | `[0.7414, -0.0603, 0.7648]`（中距） | `[0.8269, -0.3135, 0.5466]` | 38.45 | **S5 (`align` 预览)** | S0 1 帧不确定后自恢复；H-40 保住到位持块；H-32 保留桶位姿 8 次 | `observation_viewpoint_required`（`no_distinct_measured_view_in_current_holding_episode`：`own[94]` `owner_perception_stage_advanced` 瞬时清空 `live_scene.latest` 与 `aperture_observation()` 竞态） | `measured_stationary`, `model_calls=0` |
