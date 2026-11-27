# V60 真机 3 次批次报告（V6 预设，首次连续两次进入 S6 `align`）

## 摘要与结论

**结论：V60（`cabff256328d`，合入 H-36 + H-39 + H-40 + H-41；V59 因单测边界取消未复用）在 V6 预设下的 3 次真机试验全部通过 S0–S4，且首次在真机上同时打通 H-36（桶入右腕忽略桶身）、H-40（运输到位保住持块）、H-32（遮挡保留桶位姿）与 H-39（自动右移露出方孔），历史上首次连续两次打进 S6 `align`（精对准）！3 次全程 `model_calls=0`、0 堵转、0 右腕反馈过期、0 安全事件，收尾均实测静止。**

**两处新暴露的确定性阻塞（均已离线定位到代码行）：**
1. **S6 `align`（Trial 1 & Trial 2 同因）**：两次都在 S6 `align` 报 `numerical_plan_rejected:registered_candidates_exhausted`，其中 `variant=0/2/3` 撞关节限位报 `cartesian_no_progress`，而 **`variant=1` 报 `bounded_IK_wall_time_exceeded`**。离线在 6000D 用 V60 冻结包 + 真实规划输入复现证实：**Trial 1 与 Trial 2 的 `variant=1` 均直接规划成功（21/22 路点、`arm_r_j6 ∈ [-6.68°, 10.60°]` 与 `[-3.48°, 30.68°]`、零碰撞余量放宽），并通过正式 `revalidate_candidate` 复核！** 唯一原因是 6000D 上 `variant=1` 耗时 **3.81–4.89 s**，换到较慢的 F1-09 ARM 机载 CPU 后撞上了 `control/src/f1dagger/harness_compute.py:168` 硬编码的 **7.0 s** 单候选 IK 时限（`planning_deadline(scene, 7., 'bounded_IK_wall_time_exceeded')`），被提前截断！
2. **S5 竞态（Trial 3）**：`own[93]` (`OBS:108`) 已同时满足 H-40 持块位姿与 H-32 `retained_static_fixture`，但 `own[94]` 在 `inspect_plan('align')` 提交预览请求瞬间触发 `owner_perception_stage_advanced` 清空了 `live_scene.py:96` 的 `self.latest` 缓存；紧接着 `aperture_observation()` 读到 `self.latest is None` 返回 `no_distinct_measured_view_in_current_holding_episode`，`recover_alignment()` 未等待下一帧即直接退出，导致未执行横移。

**流程说明：**
- 上线前 3 次预检按操作者当轮明确指示（“直接开始，不需要预检”）跳过，如实登记未执行、不补写通过。
- 3 次试验后操作者明确要求整理数据交 Claude 审阅，批次按指令停在第 3 次（其中 S6 `align` 已连续 2 次同因暴露共性阻塞）。

## 1. 阶段漏斗与审阅要求指标汇总

| 指标 | V60 批次（3 次） | 备注 |
|---|---:|---|
| 到达 S0 / S1 / S2 / S3 / S4（`approach_grasp` → `transfer_above_hole` → `inspect_in_hand`） | **3 / 3 / 3 / 3 / 3** | 100% 到达 S4 到位并完成 `inspect_in_hand` |
| 到达 S5（`align` 遮挡恢复） | **3 / 3** | Trial 1 & 2 执行 H-39 右移成功；Trial 3 遇 `live_scene.latest` 清空竞态 |
| 到达 S6（`align` 实测方孔精对准规划） | **2 / 3** | 历史上首次真机进入 S6（Trial 1 & Trial 2） |
| 到达 S7 / S8（插孔 / 松爪确认） | **0 / 0** | 两次 S6 均被 `variant=1` 7 s 机载 IK 超时截断 |
| H-36 桶入右腕通过 S0 次数 | **1 / 1** | Trial 2 桶位 `x=0.7896 m` 桶身入右腕，直接通过 S0→S1 |
| H-37 近身运输结果 | **1 / 1 通过** | Trial 1 桶位 `[0.6634, -0.0332, 0.7728] m`（近身），运输顺利到达 |
| H-40 运输到位后测出持块位姿次数 | **3 / 3** | 0 次退化为 `coarse_local_environment` 或 `supported_upright_face_not_observed` 超时 |
| H-39 回退横移发布次数 / 让开后测出方孔次数 | **2 / 2** | Trial 1 & Trial 2 右移 `0.11 m` 后 100% 测出实测方孔（`rgbd_anchor_registration`） |
| H-32 保留告警次数 / `static_fixture_retention_expired` 次数 | **32 / 0** | Trial 2 触发 24 次、Trial 3 触发 8 次；0 次超时失效 |
| S0 分割失败停机次数 / `motion_stalled` / `camera_sample_feedback_aged` | **0 / 0 / 0** | Trial 3 S0 仅 1 帧瞬时不确定后自恢复；`fastdds-udp` 累计 14 次真机 0 堵转 |

## 2. S6 `align` 离线复现诊断（Trial 1 & Trial 2）

在 6000D 上使用 V60 冻结包 + `pinned-native-infra` + Trial 1 / Trial 2 原始 `harness_planning_started` 观测与关节状态逐变体复跑 4 个候选（`variant = 0, 1, 2, 3`，完整数据见 [align-diagnosis.json](align-diagnosis.json)）：

| 试验 | 变体 | 6000D 规划结果 | 6000D 耗时 | 路点数 | `arm_r_j6` 范围 | 终态关节 / 失败原因 | 正式复核 (`revalidate_candidate`) |
|---|---:|---|---:|---:|---|---|---|
| Trial 1 | `v=0` | 失败 | — | — | — | `cartesian_no_progress`（`1.94 mm`，`arm_r_j6=+20.0°, j7=-90.0°` 顶限位） | — |
| Trial 1 | **`v=1`** | **成功 (`planned=True`)** | **4.894 s** | **21** | **`[-6.68°, 10.60°]`** | `[-9.78, -29.48, 20.20, -102.13, 60.53, 9.57, 38.70]°`（全轴居中安全） | **通过 (`f1-harness-plan-revalidation-v1`)** |
| Trial 1 | `v=2` | 失败 | — | — | — | `cartesian_no_progress`（`1.15 mm`，`j2=-94.0°` 顶限位） | — |
| Trial 1 | `v=3` | 失败 | — | — | — | `cartesian_no_progress`（`1.61 mm`，`j6=-20.0°, j7=+90.0°` 顶限位） | — |
| Trial 2 | `v=0` | 失败 | — | — | — | `cartesian_no_progress`（`1.21 mm`，`j2=-94.0°` 顶限位） | — |
| Trial 2 | **`v=1`** | **成功 (`planned=True`)** | **3.806 s** | **22** | **`[-3.48°, 30.68°]`** | `[-11.53, -67.34, 58.75, -52.83, 17.28, 11.78, 79.67]°`（全轴安全） | **通过 (`f1-harness-plan-revalidation-v1`)** |
| Trial 2 | `v=2` | 失败 | — | — | — | `cartesian_no_progress`（`2.90 mm`，`j2=-94.0°, j7=-90.0°` 顶限位） | — |
| Trial 2 | `v=3` | 失败 | — | — | — | `cartesian_no_progress`（`2.64 mm`，`j4=-7.33°` 近奇异伸直） | — |

**根因结论**：`align` 在 `intent.budget_ms = 25000`（25 s）总预算下，底层 `control/src/f1dagger/harness_compute.py:168` 将单次笛卡尔 IK 上限硬编码为 **7.0 s**。`v=1` 在 6000D 上需 3.81–4.89 s，在 F1-09 ARM 机载 CPU 上实测超过 7.0 s 触发 `bounded_IK_wall_time_exceeded`，导致两次本已完全可解的 S6 `align` 被提前截断。

## 3. 原始轨迹清单

- `runs/trial1/{owner-trace.jsonl,planner-trace.jsonl}`（`web-ecd7898e2866457eb1b11def567d6381`）
- `runs/trial2/{owner-trace.jsonl,planner-trace.jsonl}`（`web-1b689a7a87dd4dedb69570ca214c910a`）
- `runs/trial3/{owner-trace.jsonl,planner-trace.jsonl}`（`web-be099b9916b341acb3cab74d1f053b5d`）
- `align-diagnosis.json`、`layouts.md`、`summary.json`、`manifest.json`
