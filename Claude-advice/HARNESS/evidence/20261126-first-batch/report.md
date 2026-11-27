# V57 正式批次（第 1–6 次，H-34 达阈值暂停）汇总报告

本目录保存 **V57（`F1 Harness 遮挡保留桶位姿与让开视野 V57`，`de4f6b0beaea`）** 上线后的连续 3 次预检摘要、6 次正式直立试验的完整脱敏原始日志（`owner-trace.jsonl`、`planner-trace.jsonl`、`planner-final.json`、`workspace-run.json`）、由 `run_funnel.py` 生成的 `funnel.json`、`layouts.md` 及第 6 次右腕失败裁剪图与诊断。

原定 10 次直立试验在跑完 **第 6 次** 后按规则暂停：第 1–3 次全部在 16 s 内完成抓稳验稳，随后停在 S4→S5 搬运规划（`arm_R_Link5`/`body_Link5` 差 `0.04–0.88 mm` 且 `joint_space_transport` 报 `numerical_joint_search_no_valid_goal`）；桶前移 3–4 cm 后，第 4–6 次连续 3 次在完全清晰无遮挡的右腕图像上停在 S0（`mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`），已达到 **H-34 的 3 次立项阈值**。

## 关键指标（按 `review-round4.md` 与 `20261126-01-claude.md` 要求逐项报告）

| 指标 | 实测结果 | 说明 |
|---|---|---|
| V57 上线前连续预检 | **3/3 全过（5/5）** | `rmw_fastrtps_cpp` + `fastdds-udp-only.xml`，连续配对 45.5–47.6 s，左腕最大间隔 0.04 s，`head_j2=-15.0°`，`arm_l_j2=-77.7°`，0 动作发布 |
| 本批纳入试验数 | **6 次**（原定 10 次，第 6 次触发 H-34 3/3 阈值暂停） | 全部 `mode=baseline`、`version=57`、`closed_owner_traces=6` |
| 阶段到达率（`funnel.json`） | S0: 6/6，S1–S4: 3/6（50%），S5: 0/6，S6: 0/6 | 前 3 次首次动作中位数 `4.32 s`，S0→S4 总耗时中位数约 `15.8 s` |
| H-32 真机指标（让开成功 / 让开后测孔 / S6 / `static_fixture_retention_expired`） | 0/0（本批未到达 S5）/ 0/0 / 0/6 / **0 次** | H-32 软件与试验 3 零输出回放已过（见 `../20261126-H32-occluded-fixture/`），真机因前 3 次 S3/S4 搬运碰撞与后 3 次 S0 分割不确定尚未进入 S5 遮挡让开分支 |
| H-34 计数（停在 S0 且原因为分割不确定） | **3/6（已达 ≥3 阈值）** | 第 4、5、6 次全部为 `mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`，木块完全清晰无遮挡（见 `trial-06-right-wrist-crop.jpg`） |
| 通信与硬保护统计 | `motion_stalled`: **0/6**；`camera_sample_feedback_aged:right`: **0/6**；`active_fault_revocations`: **0** | `fastdds-udp` 与 H-33（`arm_r_j6 <= 50°`）表现稳定，最大实测 `arm_r_j6 = 23.45°` |
