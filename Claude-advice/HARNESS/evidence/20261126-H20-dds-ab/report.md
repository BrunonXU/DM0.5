# H-20 步骤 A：三种 ROS 2 DDS 环境 30 轮交替只读预检报告（2026-11-26）

本报告按 `review-v53.md` H-20 步骤 A 规格，在已上线 V53（`53af3b1d4406`）的 F1-09 上完成 30 轮严格交替（`pg → udp → cyc`）独立进程只读预检。**结论：触发停下规则（没有任何环境达到 9/10）**——三组通过次数分别为 `production-gateway` **6/10**、`fastdds-udp` **3/10**、`cyclone` **1/10**。按规格不启用步骤 B、不冻结 V54，停下并报告。

关键诊断发现：**30 轮中 0 轮出现空帧**（`fastdds-udp` 与 `cyclone` 均稳定达到约 30 fps / 1,400+ 回调，`production-gateway` 约 16–22 fps / 780–1,080 回调，三组关节反馈均约 2,750 回调）。打断 12 秒连续配对窗口的真正原因是：**三种 DDS 环境下，头部相机（Orbbec head）每 4–10 秒均会出现单次轮询的 `head: False`（`pair: None`, `n_frames: 0`）闪断，而右腕相机（`right`）同期几乎 100% 保持 `good: True`**。因此问题不在 DDS 传输层，而在头部相机驱动 / RGB-D 配对一侧。

注：本批测试期间机器人左臂停在用户新建的 `V4` 姿态（`arm_l_j2 = -71.14°`），压在桶盖上方遮挡了头部主视角看桶盖，故 30 轮的 `scene_geometry_solved` 均为 0（方块可解、桶与方孔被遮挡），不影响 H-20 按 `dual_rgbd_feedback_paired` 判定的独立性；所有 30 轮 `readonly_safety` 均为 10/10、`hardware_command_publishers=0`、`start_sent=False`。

## 1. 三环境汇总表（每组 10 次，严格交替）

| ROS 环境 | `dual_rgbd_feedback_paired` | `joints_within_limits` | `left_wrist_streaming` | `scene_geometry_solved` | `readonly_safety` | `head.rgb` 中位数 | `head.depth` 中位数 | `right.rgb` 中位数 | `right.depth` 中位数 | `joints` 中位数 | 首次配对中位数 (s) | 最长连续窗口中位数 (s) | `head=False` 样本中位数 | `right=False` 样本中位数 | RMW |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `production-gateway` | **6/10** | 10/10 | 7/10 | 0/10 | 10/10 | 781.5 | 894.5 | 1082.0 | 1024.0 | 2746.5 | 0.747 | 12.4 | 17.5 | 0.0 | `rmw_fastrtps_cpp` |
| `fastdds-udp` | **3/10** | 10/10 | 6/10 | 0/10 | 10/10 | 1428.0 | 1428.0 | 1431.5 | 1431.5 | 2762.0 | 0.734 | 10.51 | 19.5 | 0.0 | `rmw_fastrtps_cpp` |
| `cyclone` | **1/10** | 10/10 | 4/10 | 0/10 | 10/10 | 1407.5 | 1407.0 | 1410.0 | 1410.0 | 2749.0 | 1.296 | 8.43 | 26.5 | 0.0 | `rmw_cyclonedds_cpp` |

## 2. 判定规则执行结果

- 规则要求：`dual_rgbd_feedback_paired` 通过次数最高者胜出；**没有任何环境达到 9/10 时停下，把表写进报告，不改代码，回到相机和驱动一侧排查**。
- 实测得分：`production-gateway` = 6/10，`fastdds-udp` = 3/10，`cyclone` = 1/10（最高 6/10 < 9/10）。
- **结论：`STOP_BELOW_9_OF_10`**。不启用 `start_development.py` 的步骤 B 常量，不冻结 V54。

## 3. 逐轮明细（30 轮）

| # | Tag | 环境 | paired | joints | left | geom | readonly | head.rgb | head.depth | right.rgb | right.depth | joints_cb | left_cb | 首次配对 (s) | RMW |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 01 | `pg-01` | `production-gateway` | 1 | 1 | 1 | 0 | 1 | 792 | 908 | 1048 | 981 | 2735 | 1636 | 3.096 | `rmw_fastrtps_cpp` |
| 02 | `udp-01` | `fastdds-udp` | 0 | 1 | 1 | 0 | 1 | 1342 | 1341 | 1345 | 1345 | 2757 | 1655 | 3.273 | `rmw_fastrtps_cpp` |
| 03 | `cyc-01` | `cyclone` | 0 | 1 | 0 | 0 | 1 | 1403 | 1402 | 1405 | 1405 | 2746 | 1624 | 1.274 | `rmw_cyclonedds_cpp` |
| 04 | `pg-02` | `production-gateway` | 0 | 1 | 0 | 0 | 1 | 784 | 883 | 1071 | 1060 | 2635 | 1620 | 0.747 | `rmw_fastrtps_cpp` |
| 05 | `udp-02` | `fastdds-udp` | 0 | 1 | 0 | 0 | 1 | 1438 | 1437 | 1441 | 1441 | 2764 | 1660 | 0.365 | `rmw_fastrtps_cpp` |
| 06 | `cyc-02` | `cyclone` | 0 | 1 | 0 | 0 | 1 | 1399 | 1398 | 1412 | 1412 | 2733 | 1628 | 1.277 | `rmw_cyclonedds_cpp` |
| 07 | `pg-03` | `production-gateway` | 1 | 1 | 1 | 0 | 1 | 776 | 902 | 1080 | 1022 | 2773 | 1665 | 0.929 | `rmw_fastrtps_cpp` |
| 08 | `udp-03` | `fastdds-udp` | 0 | 1 | 0 | 0 | 1 | 1420 | 1420 | 1424 | 1424 | 2764 | 1661 | 0.729 | `rmw_fastrtps_cpp` |
| 09 | `cyc-03` | `cyclone` | 0 | 1 | 0 | 0 | 1 | 1404 | 1403 | 1406 | 1406 | 2719 | 1629 | 1.465 | `rmw_cyclonedds_cpp` |
| 10 | `pg-04` | `production-gateway` | 0 | 1 | 1 | 0 | 1 | 750 | 844 | 1026 | 1017 | 2736 | 1645 | 2.373 | `rmw_fastrtps_cpp` |
| 11 | `udp-04` | `fastdds-udp` | 1 | 1 | 1 | 0 | 1 | 1438 | 1437 | 1442 | 1441 | 2652 | 1629 | 0.367 | `rmw_fastrtps_cpp` |
| 12 | `cyc-04` | `cyclone` | 0 | 1 | 1 | 0 | 1 | 1413 | 1406 | 1410 | 1410 | 2753 | 1624 | 1.838 | `rmw_cyclonedds_cpp` |
| 13 | `pg-05` | `production-gateway` | 1 | 1 | 1 | 0 | 1 | 792 | 904 | 1138 | 1081 | 2752 | 1652 | 0.747 | `rmw_fastrtps_cpp` |
| 14 | `udp-05` | `fastdds-udp` | 1 | 1 | 1 | 0 | 1 | 1437 | 1436 | 1439 | 1439 | 2769 | 1663 | 0.739 | `rmw_fastrtps_cpp` |
| 15 | `cyc-05` | `cyclone` | 0 | 1 | 0 | 0 | 1 | 1410 | 1410 | 1444 | 1444 | 2766 | 1634 | 1.312 | `rmw_cyclonedds_cpp` |
| 16 | `pg-06` | `production-gateway` | 1 | 1 | 1 | 0 | 1 | 768 | 858 | 1084 | 1044 | 2738 | 1653 | 0.728 | `rmw_fastrtps_cpp` |
| 17 | `udp-06` | `fastdds-udp` | 0 | 1 | 1 | 0 | 1 | 1364 | 1364 | 1368 | 1367 | 2748 | 1650 | 2.563 | `rmw_fastrtps_cpp` |
| 18 | `cyc-06` | `cyclone` | 0 | 1 | 1 | 0 | 1 | 1433 | 1432 | 1406 | 1406 | 2752 | 1629 | 1.277 | `rmw_cyclonedds_cpp` |
| 19 | `pg-07` | `production-gateway` | 0 | 1 | 0 | 0 | 1 | 805 | 892 | 975 | 968 | 2741 | 1646 | 3.095 | `rmw_fastrtps_cpp` |
| 20 | `udp-07` | `fastdds-udp` | 0 | 1 | 0 | 0 | 1 | 1436 | 1437 | 1439 | 1439 | 2760 | 1658 | 0.546 | `rmw_fastrtps_cpp` |
| 21 | `cyc-07` | `cyclone` | 1 | 1 | 1 | 0 | 1 | 1408 | 1408 | 1412 | 1411 | 2725 | 1633 | 1.093 | `rmw_cyclonedds_cpp` |
| 22 | `pg-08` | `production-gateway` | 1 | 1 | 1 | 0 | 1 | 815 | 865 | 1102 | 1024 | 2753 | 1653 | 0.739 | `rmw_fastrtps_cpp` |
| 23 | `udp-08` | `fastdds-udp` | 0 | 1 | 0 | 0 | 1 | 1347 | 1346 | 1350 | 1350 | 2766 | 1661 | 3.275 | `rmw_fastrtps_cpp` |
| 24 | `cyc-08` | `cyclone` | 0 | 1 | 1 | 0 | 1 | 1407 | 1407 | 1410 | 1410 | 2762 | 1637 | 1.462 | `rmw_cyclonedds_cpp` |
| 25 | `pg-09` | `production-gateway` | 1 | 1 | 1 | 0 | 1 | 723 | 914 | 1106 | 1024 | 2768 | 1662 | 0.728 | `rmw_fastrtps_cpp` |
| 26 | `udp-09` | `fastdds-udp` | 1 | 1 | 1 | 0 | 1 | 1437 | 1437 | 1442 | 1442 | 2769 | 1662 | 0.553 | `rmw_fastrtps_cpp` |
| 27 | `cyc-09` | `cyclone` | 0 | 1 | 0 | 0 | 1 | 1407 | 1407 | 1441 | 1440 | 2729 | 1630 | 1.497 | `rmw_cyclonedds_cpp` |
| 28 | `pg-10` | `production-gateway` | 0 | 1 | 0 | 0 | 1 | 779 | 897 | 1092 | 1071 | 2768 | 1662 | 0.734 | `rmw_fastrtps_cpp` |
| 29 | `udp-10` | `fastdds-udp` | 0 | 1 | 1 | 0 | 1 | 1357 | 1356 | 1361 | 1361 | 2747 | 1650 | 2.733 | `rmw_fastrtps_cpp` |
| 30 | `cyc-10` | `cyclone` | 0 | 1 | 0 | 0 | 1 | 1430 | 1430 | 1404 | 1404 | 2759 | 1659 | 1.281 | `rmw_cyclonedds_cpp` |

## 4. 说明与排查建议

1. **为什么 `fastdds-udp` / `cyclone` 帧率翻倍（30 fps vs `pg` 16–22 fps）反而连续窗口更短？** 每次轮询中 `head` 偶发失去 RGB-D 配对（`pair: None`, `n_frames: 0`），每轮约占全部轮询的 7–14%；在 30 fps 下头部 RGB 与 Depth 到达时间抖动更频繁触发配对失配，从而更常打断 12 秒连续计时器。
2. **30 轮无一例空帧**：与 11 月 25 日 V52 连续 160 次空帧不同，本次三组环境首次配对中位数均为 0.73–1.30 秒、有效配对样本占比均 > 86%。
3. **同步修补的预检脚本影子问题**：`preflight_scene.py` 首次在真机重启后运行时暴露两处影子目录漏项（转发 SAM 端口重写影子 `masks.local.json` 后未重签影子 `development.local.json`，以及机器人重启后未在影子目录跑只读 `boot-recheck`），已在本分支修复，未改动任何通过判据或运行时控制逻辑。

## 5. 追加根因定位：头部相机驱动时间戳重复（`duplicate_source_stamp:head`）

针对 H-20 30 轮中三组环境一致出现的 `head: False` 单轮询闪断，随后在 F1-09 上进行了两段只读实测（零改动、零动作）：

1. **15 秒到达间隔与配对差采样**：
   - `/camera/head/color/image_raw` 314 帧、`/camera/head/depth/image_raw` 347 帧；
   - 硬件源时间戳差 `|rgb.source_ns - depth.source_ns|` 中位数仅 **0.02 ms**，但系统到达间隔 p95 约 **100–101 ms**、最大 **200.5 ms**（RGB 85 次 / Depth 62 次超过 60 ms）；
   - 在 `ObservationBuffer(capacity=8, max_skew_ns=20ms)` 离线回放模拟中 660 次检查失败 0 次，说明**仅靠到达抖动并没有打穿 20 ms 缓冲窗口**。
2. **12 秒源时间戳唯一性采样（决定性证据）**：
   - RGB 219 帧中出现 **4 次**、Depth 241 帧中出现 **2 次**“前后两帧像素内容不同（`blob_sha256` 不同）、但 ROS `header.stamp`（`source_ns`）完全相同”（`dup_diff`）；`dup_same=0`、`rev=0`。
   - 核查机载启动文件 `gemini_330_series.launch.py`：默认开启 `enable_sync_host_time=true`、`time_sync_period=6.0` 秒、`time_domain=global`，且 `orbbec_camera` 节点单进程占 CPU **41.6%**（整机 load average 9.27–9.75 / 11 核）；同时 `preflight-92.5` 告警明确记录了 `fixture_unavailable:capture_faults:duplicate_source_stamp:head`。
   - 触发链路：`Orbbec head` 每 2–6 秒发出同 `source_ns` 但不同像素的两帧 → `ObservationBuffer.add()`（`observation.py:106-110`）置 `transient_faults[("head", stream)] = "duplicate_source_stamp:head"` → 该轮询 `head` 判为 `invalid` 并从 `obs` 剥离 → `preflight_scene.py` 12 秒连续窗口清零重来。这也完全解释了为何 30 fps 的 `fastdds-udp`（3/10）与 `cyclone`（1/10）反而比 16–22 fps 的 `production-gateway`（6/10）更容易踩中重复时间戳并打断窗口。

**结论与待 Claude 审阅事项**：问题 100% 定位在头部 Orbbec 相机驱动的 `time_domain=global` + `time_sync_period=6.0` 引发的 `duplicate_source_stamp:head`（非 DDS 传输层问题）。在收到 Claude 审阅结论前，不擅自修改相机驱动参数或放宽 `ObservationBuffer` 判据。

3. **15 秒深查（同戳事件逐条采样 + 实时运行参数 dump）**：
   - 运行中 `/camera/head/head` 实测参数：`enable_frame_sync: true`、`enable_sync_host_time: true`、`color_fps/depth_fps: 0`（默认）、`time_domain: global`（见 `gemini_330_series.launch.py`）。
   - 15 秒内共抓到 **17 次** `dup_diff`（RGB 13 次 + Depth 4 次），呈现清晰的 **100.12 ms 阶梯跳变**：相机实际每 33.3 ms 输出一帧新像素（`same_hash=false`），但 `header.stamp` 连续 **3–4 帧完全冻结不变**（`prev_dt_stamp_ms=0.0`），随后一次性跳变 **`+100.12 ms`**（正好 3 帧周期）。
   - 根因高度指向 OrbbecSDK_ROS2 在 Jetson 上开启 `time_domain=global` + `enable_sync_host_time=true` 时的全局时间戳阶梯冻结（候选处理方向：`time_domain=device` 或 `system`，或关闭 `enable_sync_host_time`，需 Claude 审阅拍板后按正式流程验证）。
