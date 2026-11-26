# H-20 重跑（V54 上线后，23/30 已完成）：250 ms 同戳帧保留下的 DDS A/B 实测

> **阶段结论（完成 23/30 轮，第 24 轮 `cyc-08` 时 F1-09 离线断连）**：
> 1. **已完成 23 轮全部五项通过（23/23 = 100%）**：`production-gateway` **8/8**、`fastdds-udp` **8/8**、`cyclone` **7/7**，中途空帧复现 **0 次**（`empty_frame_reproduced = false`）。
> 2. **H-26 真机效果已证实**：在 V53 上 `duplicate_source_stamp:head` 造成约 600 次头部无效采样、三路通过率仅 6/10、3/10、1/10；在 V54 上，`duplicate_source_stamp:head` 每轮中位数降至 **0**（23 轮中仅 `pg-03` 出现 3 次超过 250 ms 的采样，`fastdds-udp` 与 `cyclone` 合计 **0 次**），每轮 `held_source_frames` 中位数为 `46.5`（默认）/ `114.5`（`fastdds-udp`）/ `119`（`cyclone`）。
> 3. **传输层差异清晰暴露**：`fastdds-udp` 与 `cyclone` 的连续配对窗口中位数高达 **`46.15 s`** / **`46.57 s`**、头部无效采样中位数为 **0**、`head_rgb` 回调中位数 **`1,437.5`** / **`1,403`**；而 `production-gateway` 的连续窗口中位数仅 **`25.30 s`**、头部无效采样中位数 **`2.5`**（合计 36 次）、`head_rgb` 回调中位数 **`755.5`**（约为另两种的一半）。
> 4. **缺口**：第 24 轮 `cyc-08` 时 F1-09 反向 SSH 隧道断开（`Connection refused`），尚缺最后 7 轮（`cyc-08`、`pg-09`、`udp-09`、`cyc-09`、`pg-10`、`udp-10`、`cyc-10`）以凑齐完整 30/30。待机器人重连后可一键续跑补齐。

---

## 一、三环境汇总（V54，`8818976408ce`，姿态“积木demoV5”）

| 环境 | 已跑 / 目标 | 五项全过 | 配对通过 | 中位连续窗口 | `held_source_frames` 中位数（合计） | 头部无效采样中位数（合计） | `dup_stamp:head` 中位数（合计） | `head_rgb` 回调中位数 | `right_rgb` 回调中位数 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `production-gateway` | 8 / 10 | **8/8** | **8/8** | 25.30 s | 46.5 (377) | 2.5 (36) | 0.0 (3) | 755.5 | 1089.0 |
| `fastdds-udp` | 8 / 10 | **8/8** | **8/8** | **46.15 s** | 114.5 (975) | **0.0 (0)** | **0.0 (0)** | **1437.5** | **1404.0** |
| `cyclone` | 7 / 10 | **7/7** | **7/7** | **46.57 s** | 119 (875) | **0 (4)** | **0 (0)** | 1403 | 1406 |

中途（首帧有效配对之后）非有效采样原因分布：
- `production-gateway`：`{"observe_with_status: camera_sample_feedback_aged:right": 14, "fixture_unavailable:secondary_camera_pairing_unavailable": 10, "fixture_unavailable:capture_faults:duplicate_source_stamp:head": 3, "fixture_unavailable:feedback_expired:head": 4}`
- `fastdds-udp`：`{}`
- `cyclone`：`{}`

---

## 二、逐轮明细（已完成的 23 轮）

| 轮次 | 环境 | 结论 | 连续窗口 | `held_source_frames` | 头部无效采样 | `dup_stamp:head` | `head_rgb` 回调 | `right_rgb` 回调 |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `pg-01` | `production-gateway` | PASS | 37.81 s | 39 | 1 | 0 | 756 | 1026 |
| `udp-01` | `fastdds-udp` | PASS | 44.86 s | 102 | 0 | 0 | 1349 | 1353 |
| `cyc-01` | `cyclone` | PASS | 45.85 s | 151 | 0 | 0 | 1401 | 1404 |
| `pg-02` | `production-gateway` | PASS | 26.68 s | 46 | 2 | 0 | 790 | 1086 |
| `udp-02` | `fastdds-udp` | PASS | 47.42 s | 117 | 0 | 0 | 1441 | 1445 |
| `cyc-02` | `cyclone` | PASS | 46.79 s | 146 | 4 | 0 | 1403 | 1436 |
| `pg-03` | `production-gateway` | PASS | 20.47 s | 69 | 5 | 3 | 747 | 1046 |
| `udp-03` | `fastdds-udp` | PASS | 45.01 s | 107 | 0 | 0 | 1443 | 1368 |
| `cyc-03` | `cyclone` | PASS | 46.57 s | 115 | 0 | 0 | 1402 | 1405 |
| `pg-04` | `production-gateway` | PASS | 26.55 s | 47 | 18 | 0 | 721 | 1071 |
| `udp-04` | `fastdds-udp` | PASS | 45.20 s | 112 | 0 | 0 | 1432 | 1361 |
| `cyc-04` | `cyclone` | PASS | 46.77 s | 119 | 0 | 0 | 1405 | 1408 |
| `pg-05` | `production-gateway` | PASS | 23.69 s | 23 | 2 | 0 | 755 | 1092 |
| `udp-05` | `fastdds-udp` | PASS | 47.10 s | 109 | 0 | 0 | 1438 | 1441 |
| `cyc-05` | `cyclone` | PASS | 46.41 s | 116 | 0 | 0 | 1405 | 1409 |
| `pg-06` | `production-gateway` | PASS | 24.05 s | 49 | 3 | 0 | 789 | 1101 |
| `udp-06` | `fastdds-udp` | PASS | 47.52 s | 128 | 0 | 0 | 1442 | 1446 |
| `cyc-06` | `cyclone` | PASS | 46.76 s | 128 | 0 | 0 | 1403 | 1406 |
| `pg-07` | `production-gateway` | PASS | 29.99 s | 58 | 2 | 0 | 766 | 1096 |
| `udp-07` | `fastdds-udp` | PASS | 47.31 s | 129 | 0 | 0 | 1436 | 1440 |
| `cyc-07` | `cyclone` | PASS | 46.55 s | 100 | 0 | 0 | 1402 | 1405 |
| `pg-08` | `production-gateway` | PASS | 19.40 s | 46 | 3 | 0 | 742 | 1114 |
| `udp-08` | `fastdds-udp` | PASS | 45.05 s | 171 | 0 | 0 | 1437 | 1358 |
