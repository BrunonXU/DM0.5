# V57 正式批次（第 1–6 次，H-34 达 3 次触发收尾）现场摆位与人工介入记录

本批于 2026-11-26 在 **V57（`de4f6b0beaea`，`fastdds-udp`，固定程序基线 `planner_override=baseline`）** 连续 3 次预检 5/5 全过后启动。原计划 10 次直立试验，运行至第 6 次时，**第 4–6 次连续 3 次停在 S0（`mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`）**，已达到 H-34 的 3 次立项阈值，因此按规则于第 6 次后暂停并回报 Claude。

## 逐次摆位与结果

| 序号 | 运行 ID | 实测桶位姿 XYZ (m) | 实测方块位姿 XYZ (m) | 最远阶段 | 停止原因 | 人工介入 |
|---:|---|---|---|---|---|---|
| 1 | `web-f7c5d2a321744442929abc484b2fa545` | `[0.650, -0.051, 0.765]` | `[0.833, -0.266, 0.628]` | S4（抓稳验稳，搬运前） | `transfer_above_hole`: `registered_candidates_exhausted`（3 条 `arm_R_Link5/body_Link5` 间隙 `-0.04~-0.42 mm`；`joint_space_transport`: `numerical_joint_search_no_valid_goal`；variant 3 IK 未收敛） | 1 次：停稳后手动从夹爪取下木块，4242 复位 `积木demoV5` |
| 2 | `web-82094dda40f9450fafcbf455e7210afd` | `[0.649, -0.051, 0.767]` | `[0.851, -0.287, 0.628]` | S4（抓稳验稳，搬运前） | `transfer_above_hole`: `registered_candidates_exhausted`（3 条 `arm_R_Link5/body_Link5` 间隙 `-0.23~-0.88 mm`；`joint_space_transport`: `numerical_joint_search_no_valid_goal`；variant 3 IK 未收敛） | 1 次：停稳后手动从夹爪取下木块，4242 复位 `积木demoV5` |
| 3 | `web-f8b242850fac4062be8da247ab4b334c` | `[0.650, -0.051, 0.766]` | `[0.831, -0.307, 0.628]` | S4（抓稳验稳，搬运前） | `transfer_above_hole`: `registered_candidates_exhausted`（3 条 `arm_R_Link5/body_Link5` 间隙 `-0.36~-0.70 mm`；`joint_space_transport`: `numerical_joint_search_no_valid_goal`；variant 3 IK 未收敛） | 1 次：停稳后手动从夹爪取下木块，4242 复位 `积木demoV5` |
| 4 | `web-31d266ffe7284581966c9d2b174fb93e` | 桶前移约 3–4 cm、略右移约 2–3 cm（未出场景） | 未出场景（右腕视野清晰无遮挡，见截图） | S0 | `observation_readiness_timeout`: `mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`（15 s 无动作） | 0 次（未动臂） |
| 5 | `web-bbdfb13634e64694bfd5869611481fe2` | 同第 4 次（未出场景） | 未出场景（右腕视野清晰无遮挡） | S0 | `observation_readiness_timeout`: `mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`（15 s 无动作） | 0 次（未动臂） |
| 6 | `web-a1e9a264ec114fe4800a51d5112819dd` | 桶进一步前移并略左移（头相机顶盖孔可见，右腕画面左侧仍露出部分木纹桶壁） | 未出场景（右腕视野中木块完全清晰无遮挡，见 `trial-06-right-wrist-crop.jpg`） | S0 | `observation_readiness_timeout`: `mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`（15 s 无动作） | 0 次（未动臂） |

## 关键现场观察

1. **抓取速度与通信稳定性**：`fastdds-udp` 生效后，全部 6 次试验 **0 次 `motion_stalled`**、**0 次 `camera_sample_feedback_aged:right`**；第 1–3 次抓取至验稳耗时中位数约 **15.8 s**（用户直观反馈“二十秒内就完成了抓取，比之前快很多”）。
2. **前后两个瓶颈把可用桌面夹成了 ~1 cm 窄缝**：
   - 桶在 `[0.650, -0.051]` m 时（仅比 V56 通过的 Trial 1 `[0.660, -0.064]` m 近 1 cm），右前臂 `arm_R_Link5` 与躯干 `body_Link5` 防碰撞间隙差 `0.04–0.88 mm` 且 `joint_space_transport` 报 `numerical_joint_search_no_valid_goal`，3/3 停在搬运前（尚未进入 S5，故 V57 的 H-32 遮挡保留/让开在真机上尚未触发）；
   - 桶往前挪 3–4 cm 避开躯干碰撞后，印有木块图案的木色桶壁进入右腕相机画面左侧；尽管木块本身在右腕画面右侧完全清晰无遮挡（见 `trial-06-right-wrist-crop.jpg`，16.6 KB），无提示框的 SAM3 全图发现连续 3/3 报 `mask_uncertain_requires_new_prompt:object=square-1`（触发 H-34 3 次阈值）。
3. **操作者现场反馈（一并转交 Claude）**：
   - 木块完全清晰无遮挡，仅因画面边缘出现外来物体（木桶侧面）就全部识别失败，容错性不足，不建议简单放宽置信度阈值，而是希望在固定程序里针对方块加提示框/屏蔽桶体区域；
   - 后续机制应考虑左手遮挡头相机视野时动态调整左手姿态，而非靠人工把桶塞进极小缝隙。
