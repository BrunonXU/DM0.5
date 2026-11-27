# H-44 验收证据：S5 读取场景的竞态修复（2026-11-27）

**在 `live_scene.py` 的 `aperture_observation()` 中对“作用域一致且 `viewpoint.move` 存在、仅 `self.latest` 刚被 `owner_perception_stage_advanced` 清空”的瞬间最多等待 1.5 s，V60 Trial 3 原始输入（关闭缓存）在等待 0.36 s 拿到下一帧后成功给出 `0.119 m` 右移横移候选，不再误报 `no_distinct_measured_view_in_current_holding_episode`。** 场景失效触发条件与 `recovery_programs.py` 均未改动。

## 1. 改动摘要

- 仅修改 `HARNESS/src/f1_harness/live_scene.py` 的 `LiveSceneObserver.aperture_observation()`：当 `self.latest is None`、`viewpoint.move is not None` 且当前 owner `perception_context` 作用域与 `viewpoint.scope` 一致时，按 20 ms 间隔最多等待 1.5 s 直至下一帧写回 `self.latest`（或 `stop` 置位 / `viewpoint.move` 清空）；随后重新加锁读取 `self.latest`、`viewpoint.move`、`viewpoint.scope` 并再次校验作用域。超时或作用域变化时照原逻辑返回 `no_distinct_measured_view_in_current_holding_episode`。
- `[!]` 风险备忘：`remote.py:532` 调用 `aperture_observation` 时带 `timeout_s=1`；正常下一帧到达耗时约 0.3–0.5 s 不受影响，若偶发帧间隔落在 1.0–1.5 s 之间会先触发远端调用 1 s 超时。按规格本轮不改 `remote.py`，特此标注。

## 2. 单元测试

`HARNESS/tests/test_held_aperture_view.py`：**13 passed**（新增 4 条 H-44 用例：清空后 0.3 s 到帧返回 `observation_move`；1.5 s 无新帧超时返回原原因；调用前作用域已变立即拒绝不等待；等待期间作用域变化拒绝返回候选）。

## 3. V60 Trial 3 离线竞态回放（关闭缓存，零硬件输出）

原始回放结果见 [v60-trial3-race-replay.json](v60-trial3-race-replay.json)。

- 来源：V60 Trial 3 (`web-be099b9916b341acb3cab74d1f053b5d`) `OBS:108` (`32e8197eb9c14f2b9d2a9c45e06820ae:108`)，真实 `full_reference` / `held_reference` 经 Pinocchio FK 与 `HeadKinematics` 计算 `world_eef` 与 `head_pose`。
- 模拟竞态：`owner_perception_stage_advanced` 将 `observer.latest` 清空为 `None` 后立即调用 `aperture_observation()`，0.35 s 后下一帧写入 `observer.latest`。
- 结果：等待 `0.3616 s` 返回 `observation_move`（`purpose = noncontact_lateral_aperture_reobservation_without_prior_view`，位移 `[-0.0435, -0.1111, -0.0051] m`，步长 `0.1194 m`，`robot_actions = 0`）。
