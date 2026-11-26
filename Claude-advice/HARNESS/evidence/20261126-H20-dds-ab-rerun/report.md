# H-20 重跑（V54 上线后，30/30 完整结果）：250 ms 同戳帧保留下的 DDS A/B 实测

> **结论（30/30 完整完成，满足 `PROCEED_TO_H21_WITHOUT_STEP_B`）**：
> 1. **三种环境在 `dual_rgbd_feedback_paired` 上全部达到 10/10（30/30 = 100%）**，中途空帧复现 **0 次**（`empty_frame_reproduced = false`）。按第三轮审阅与已定决策：`production-gateway` 达到 10/10（≥ 9/10），**直接用 V54 进入 H-21，不切 `fastdds-udp`**（胜出环境 `fastdds-udp` 留作后续版本或 H-21 前 3 次均因取帧停在 S0 时的止损方案）。
> 2. **H-26 真机验收通过**：在 V53 上 `duplicate_source_stamp:head` 曾造成约 600 次头部无效采样、三路仅 6/10、3/10、1/10；在 V54 上，`duplicate_source_stamp:head` 每轮中位数降至 **0**（30 轮中仅 `pg-03` 出现 3 次超过 250 ms 采样，`fastdds-udp` 与 `cyclone` 合计 **0 次**）。
> 3. **传输层对比**：`fastdds-udp` 与 `cyclone` 连续配对窗口中位数高达 **`46.15 s`** / **`46.65 s`**、头部无效采样中位数为 **0**、`head_rgb` 回调中位数 **`1,437.5`** / **`1,403.0`**；`production-gateway` 连续窗口中位数 **`25.30 s`**、头部无效采样中位数 **`2.0`**（合计 38 次）、`head_rgb` 回调中位数 **`761.0`**。
> 4. **重启后第 4 项现场观察**：前 23 轮五项全过；F1-09 重启后补跑的最后 7 轮（`cyc-08`–`cyc-10`）中，第 1、2、3、5 项全过（`dual_rgbd_feedback_paired` 7/7 通过），但第 4 项均因 `3d_geometry_missing:bucket_fixture`（`fixture_aperture_not_detected_in_search_region`，积木 95–248 次全部解出、仅桶未检出）未通过，需在 H-21 连续 3 次预检前核对桌面桶位／光照。

---

## 一、三环境 30 轮汇总（V54，`8818976408ce`，姿态“积木demoV5”）

| 环境 | 轮数 | `dual_rgbd_feedback_paired` | 五项全过（重启前/总） | 中位连续窗口 | `held_source_frames` 中位数（合计） | 头部无效采样中位数（合计） | `dup_stamp:head` 中位数（合计） | `head_rgb` 回调中位数 | `right_rgb` 回调中位数 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `production-gateway` | 10 | **10/10** | 8/10（前 8/8） | 25.30 s | 46.5 (450) | 2.0 (38) | **0.0** (3) | 761.0 | 1089.0 |
| `fastdds-udp` | 10 | **10/10** | 8/10（前 8/8） | **46.15 s** | 111.0 (1173) | **0.0** (7) | **0.0** (0) | **1437.5** | **1420.0** |
| `cyclone` | 10 | **10/10** | 7/10（前 7/7） | **46.65 s** | 115.5 (1127) | **0.0** (4) | **0.0** (0) | 1403.0 | 1406.5 |

- **决策字段**：`route = "PROCEED_TO_H21_WITHOUT_STEP_B"`，`winner = "fastdds-udp"`，`all_three_10_of_10 = True`，`empty_frame_reproduced = False`。
- **中途非有效采样原因分布**：
  - `production-gateway`：`{"observe_with_status: camera_sample_feedback_aged:right": 34, "fixture_unavailable:secondary_camera_pairing_unavailable": 12, "fixture_unavailable:capture_faults:duplicate_source_stamp:head": 3, "fixture_unavailable:feedback_expired:head": 4}`
  - `fastdds-udp`：`{}`
  - `cyclone`：`{}`

---

## 二、逐轮明细（全部 30 轮）

| 轮次 | 环境 | 第 2 项配对 | 第 4 项几何 | 连续窗口 | `held_source_frames` | 头部无效采样 | `dup_stamp:head` | `head_rgb` 回调 | `right_rgb` 回调 |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| `pg-01` | `production-gateway` | PASS | PASS | 37.81 s | 39 | 1 | 0 | 756 | 1026 |
| `udp-01` | `fastdds-udp` | PASS | PASS | 44.86 s | 102 | 0 | 0 | 1349 | 1353 |
| `cyc-01` | `cyclone` | PASS | PASS | 45.85 s | 151 | 0 | 0 | 1401 | 1404 |
| `pg-02` | `production-gateway` | PASS | PASS | 26.68 s | 46 | 2 | 0 | 790 | 1086 |
| `udp-02` | `fastdds-udp` | PASS | PASS | 47.42 s | 117 | 0 | 0 | 1441 | 1445 |
| `cyc-02` | `cyclone` | PASS | PASS | 46.79 s | 146 | 4 | 0 | 1403 | 1436 |
| `pg-03` | `production-gateway` | PASS | PASS | 20.47 s | 69 | 5 | 3 | 747 | 1046 |
| `udp-03` | `fastdds-udp` | PASS | PASS | 45.01 s | 107 | 0 | 0 | 1443 | 1368 |
| `cyc-03` | `cyclone` | PASS | PASS | 46.57 s | 115 | 0 | 0 | 1402 | 1405 |
| `pg-04` | `production-gateway` | PASS | PASS | 26.55 s | 47 | 18 | 0 | 721 | 1071 |
| `udp-04` | `fastdds-udp` | PASS | PASS | 45.20 s | 112 | 0 | 0 | 1432 | 1361 |
| `cyc-04` | `cyclone` | PASS | PASS | 46.77 s | 119 | 0 | 0 | 1405 | 1408 |
| `pg-05` | `production-gateway` | PASS | PASS | 23.69 s | 23 | 2 | 0 | 755 | 1092 |
| `udp-05` | `fastdds-udp` | PASS | PASS | 47.10 s | 109 | 0 | 0 | 1438 | 1441 |
| `cyc-05` | `cyclone` | PASS | PASS | 46.41 s | 116 | 0 | 0 | 1405 | 1409 |
| `pg-06` | `production-gateway` | PASS | PASS | 24.05 s | 49 | 3 | 0 | 789 | 1101 |
| `udp-06` | `fastdds-udp` | PASS | PASS | 47.52 s | 128 | 0 | 0 | 1442 | 1446 |
| `cyc-06` | `cyclone` | PASS | PASS | 46.76 s | 128 | 0 | 0 | 1403 | 1406 |
| `pg-07` | `production-gateway` | PASS | PASS | 29.99 s | 58 | 2 | 0 | 766 | 1096 |
| `udp-07` | `fastdds-udp` | PASS | PASS | 47.31 s | 129 | 0 | 0 | 1436 | 1440 |
| `cyc-07` | `cyclone` | PASS | PASS | 46.55 s | 100 | 0 | 0 | 1402 | 1405 |
| `pg-08` | `production-gateway` | PASS | PASS | 19.40 s | 46 | 3 | 0 | 742 | 1114 |
| `udp-08` | `fastdds-udp` | PASS | PASS | 45.05 s | 171 | 0 | 0 | 1437 | 1358 |
| `cyc-08` | `cyclone` | PASS | FAIL(bucket) | 46.76 s | 81 | 0 | 0 | 1436 | 1407 |
| `pg-09` | `production-gateway` | PASS | FAIL(bucket) | 33.48 s | 24 | 0 | 0 | 790 | 1064 |
| `udp-09` | `fastdds-udp` | PASS | FAIL(bucket) | 45.12 s | 88 | 7 | 0 | 1363 | 1400 |
| `cyc-09` | `cyclone` | PASS | FAIL(bucket) | 46.66 s | 89 | 0 | 0 | 1399 | 1403 |
| `pg-10` | `production-gateway` | PASS | FAIL(bucket) | 17.83 s | 49 | 2 | 0 | 795 | 1101 |
| `udp-10` | `fastdds-udp` | PASS | FAIL(bucket) | 47.42 s | 110 | 0 | 0 | 1442 | 1445 |
| `cyc-10` | `cyclone` | PASS | FAIL(bucket) | 46.64 s | 82 | 0 | 0 | 1407 | 1410 |
