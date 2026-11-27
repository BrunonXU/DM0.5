# H-42 验收证据：运输前实测方孔缓存与直接对准（2026-11-27）

**已完成持块周期内运输前实测方孔缓存（`source='cached_pre_transport_aperture'`、`visible=False`、保留原证据号与测量时刻、45 秒上限、接触故障/撤权/桶位移/换闭爪即作废），同步将 H-32 遮挡桶位姿保留窗修订为 45 秒并接入同一套接触故障作废；允许缓存方孔用于 `align`/`insert`/`release`，禁止用于 `verify_placed`；H-39 横移自动降为无可用缓存时的备用路径。5 次真实走到运输的试验（V60 Trial 1–3、V58 Trial 2–3）从运输到位直接进入 `align` 零输出回放 5/5 全部不经横移、首次即解出 `v=1` 可执行路径并通过正式 `revalidate_candidate` 复核。**

## 1. 实现摘要

- **`HARNESS/src/f1_harness/contracts.py`**：`SceneObject` 新增可选字段 `source: Literal["cached_pre_transport_aperture"] | None` 与 `measured_at_ns: int | None`，用 `wrap` 序列化器在二者均为 `None` 时省略字段，完全保持历史 `Observation` / `SceneObject` 哈希兼容。
- **`HARNESS/src/f1_harness/engine.py` & `owner_bridge.py`**：新增接触故障记录 `record_contact_fault(reason, at_ns)`（覆盖 `motion_stalled`、发布后未成功/未知部分发布、撤权以及 `owner_bridge.py` 的 `harness_waypoint_tracking_check` 失败），并将 `contact_fault` 与 `hold_requested` 透传至 `perception_context`。
- **`HARNESS/src/f1_harness/scene_pipeline.py`**：
  - `CACHED_APERTURE_MAX_AGE_NS = OCCLUDED_FIXTURE_RETAIN_NS = 45_000_000_000`。
  - `_update_cached_aperture` 在持块周期内、每当 `_direct_aperture` 直接测得方孔（`directly_visible=True, head_error=None`，覆盖 2 哈希头部直接测量与 3 哈希头部深度+左腕跨视角实测，排除预测/名义值）且当前桶为实测 `rgbd_anchor_registration` 时更新缓存。
  - `_resolve_cached_aperture` 在当前帧未直接测到方孔时核验全部条件（同一闭爪请求号、`held is True`、同作用域/标定/采集实例、无接触故障或 `hold_requested`、年龄 ≤ 45 s、当前桶为偏差 ≤ `planning_margin_m` 的实测桶或 ≥ 缓存时刻的 `retained_static_fixture`），任一不满足即作废并写入 `cache_invalidated:<reason>` 诊断；满足时输出 `visible=False, source='cached_pre_transport_aperture'` 与质量告警 `aperture_from_pre_transport_cache`。
  - H-32 `_check_occluded_fixture_retention` 保留窗同步改为 45 s 并接入 `_contact_fault_since` 接触故障作废。
- **`HARNESS/src/f1_harness/observation.py`、`revalidation.py`、`verification.py`**：
  - 允许 `cached_pre_transport_aperture` 用于 `align`/`insert`/`release`（兼容与 `retained_static_fixture` 组合，且 `revalidation.py` 校验缓存方孔精确血缘 `evidence_ids`/`measured_at_ns` 不变、跳过对缓存 3 哈希方孔的当前帧左腕绑定检查）。
  - `verify_placed` 在 `observation.py` 与 `verification.py` 双层拒绝 `cached_pre_transport_aperture`（报错 `aperture_measurement_required:<hole_id>:cached_aperture_not_allowed_for_verify_placed`）。
  - H-39（`aperture_view.py`）因观测中已带有效缓存方孔而自然不生成横移候选；仅在缓存不存在或已作废时作为备用路径触发。

## 2. 单元测试

`test_cached_pre_transport_aperture.py` + `test_occluded_fixture_retention.py` + `test_held_after_transport.py` + `test_retained_fixture_revalidation.py` + `test_real_bootstrap_contract.py`：**85 passed in 2.69s**。覆盖：
- 满足全部条件时缓存生效且 `align` 可规划、有当前帧直接测量时优先用直接测量；
- 7 种作废条件（`close_request_changed`、`motion_stalled`、`waypoint_tracking_failed`、`hold_requested`、`fixture_moved`、`age_exceeded`、`calibration_changed`）全部作废并回到 `aperture_measurement_required`；
- `verify_placed` 拒绝缓存方孔；
- H-32 45 s 内保留、超 45 s 或接触故障作废；
- H-39 在有缓存时抑制横移、无缓存时自动作为备用激活；
- 3 哈希跨视角实测缓存与 `revalidate_candidate` 帧绑定/血缘校验兼容。

## 3. 五次真实试验从运输到位直接对准零输出回放（6000D，零硬件输出）

原始结果见 [five-trial-direct-align-replay.json](five-trial-direct-align-replay.json)。其中 V58 Trial 3 使用真实原始图像 blobs 经实机 SAM2/SAM3 服务（`127.0.0.1:19842`）+ H-40 + H-42 端到端恢复 `OBS:114` 后直接进入 `align`。

| 试验 | 运输前实测孔 | 到位观测 | 桶位姿来源 | 缓存年龄 | 触发横移 | 直接 `align` 可执行 | 选中变体 | `arm_r_j6` 范围 | 终点最小关节余量 | 6000D 规划耗时 | `revalidate_candidate` |
|---|---|---|---|---:|---|---|---:|---|---:|---:|---|
| V60 Trial 1 | `c50ebca5...:77` | `c50ebca5...:110` | `rgbd_anchor_registration` | 11.98 s | **否** | **是** | `v=1` | `[2.94°, 21.53°]` | 42.64° | 4.299 s | **通过** (18 段) |
| V60 Trial 2 | `078860e2...:77` | `078860e2...:101` | `retained_static_fixture` | 8.70 s | **否** | **是** | `v=1` | `[8.62°, 34.35°]` | 17.06° | 3.100 s | **通过** (18 段) |
| V60 Trial 3 | `07c07943...:87` | `07c07943...:108` | `retained_static_fixture` | 7.60 s | **否** | **是** | `v=1` | `[15.23°, 34.96°]` | 24.92° | 3.066 s | **通过** (17 段) |
| V58 Trial 2 | `90167346...:90` | `90167346...:127` | `retained_static_fixture` | 13.80 s | **否** | **是** | `v=1` | `[13.30°, 36.12°]` | 19.07° | 3.193 s | **通过** (17 段) |
| V58 Trial 3 | `19ed3d59...:76` | `19ed3d59...:114` | `retained_static_fixture` | 14.84 s | **否** | **是** | `v=1` | `[0.17°, 21.25°]` | 44.92° | 4.347 s | **通过** (19 段) |
