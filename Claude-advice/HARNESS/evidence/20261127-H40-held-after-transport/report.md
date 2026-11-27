# H-40 离线诊断与条件修复验收报告：运输到位后持块位姿与保留桶位姿恢复

## 摘要与结论

**结论：V58 Trial 3（`web-a4d290783627423bbd8b8020d2468138`）5 项离线诊断同时证实了审阅规格中的条件 A 与条件 B（且排除了物理视角不可见的条件 C）。两项条件均已完成最小修复、通过 13 项新增正/反例单测及 135 项场景/夹爪回归，并在 Trial 3 真实 `OBS:49 → [250]/[251] 瞬时失败 → OBS:114` RGB-D + SAM2/SAM3 零输出回放中端到端恢复为 `geometry_scope='coarse_static_task'`、`held=True`、`square-1` 位姿 `[0.6626, -0.0349, 0.8183] m`、`eef_object_pose` 有效、`fixture.source='retained_static_fixture'`（年龄 `7.562 s`），可直接进入 `align`/`inspect_in_hand`。**

**对当前决定的影响：** H-40（A+B）已完全通过离线验收，可纳入 V59 冻结包。

## 1. 五项离线诊断事实（V58 Trial 3）

| 诊断项 | 真实数据结论 |
|---|---|
| 1. 相机 | 运输前（`OBS:48–49`）与到位后（`OBS:114–147`）均使用右腕相机 **`right`**。 |
| 2. `in_hand_reference` 为何丢失 | 至事件 `[245]`（运输途中）时 `held=True`、`attachment_geometry_available=True`、`surface_association` 正常（`inliers = 7,935 / 9,666 = 82.1%`）。事件 **`[250]`** 出现单帧瞬时失败 `mask_view=head:mask_truncated:object=square-1`，触发 `scene_pipeline.py:process()` → `reject_measurement()`；其内 `if self.in_hand_reference is not None and self.tracking_history and self.contact_attachment is None:` 因 `measured_gripper_contact=True` 导致 `self.contact_attachment` 非空而跳过隔离保存，随即 `invalidate()` 清空 `in_hand_reference`、`in_hand_gripper`、`contact_attachment` 与 `measured_fixture`。此后 `[251]` 起 `in_hand_reference=None`、`in_hand_quarantine=None`，`surface_association` 0 次触发。 |
| 3. 夹爪 CAN 变化 | 全程 CAN 实测闭合度恒定 **`87.0588%`**（`span = 0.0` < `gripper_stationary_delta = 0.5`），无张开或滑动。 |
| 4. 到位后掩码/深度/法向夹角 | `OBS:114` 右腕掩码 `11,798–11,991 px`，有效深度 `9,958–10,495` 点。由 `OBS:49` `eef_object_pose × OBS:114 base_eef` 预测的面法向 `[-0.3304, 0.0841, 0.9401]` 与相机反视线夹角仅 **`20.78°`**（`cos = 0.9350`，正对相机），但与世界 `+Z` 夹角 **`19.93°`**（> `normal_tolerance_deg = 15°`），因此丢失 `surface_association` 回退世界 `+Z` 后持续报 `supported_upright_face_not_observed`；恢复 `surface_association` 后实测深度拟合直接成功（`7,944` 内点，`80.72%`）。 |
| 5. `_partial` 中 `fixture=None` 的原因 | 两重原因：(a) `[250]/[251]` 瞬时失败与 `_partial` 入口的 `reject_measurement() → invalidate()` 清空了 `self.measured_fixture`；(b) `_partial_observation` 直接调用原始 `locate_fixture()`（跳过 `lid_registration.measure` 直接报 `fixture_relocalization_anchor_missing:bucket-triangle`），且未套用 H-32 的 5 条件保留逻辑。 |

## 2. 修复内容（`HARNESS/src/f1_harness/scene_pipeline.py`）

- **条件 A**：`reject_measurement()` 与跨相机 `changed_view` 不再因 `self.contact_attachment is not None` 放弃隔离保存 `in_hand_reference`/`in_hand_gripper`；在 `_process()` 中对 `measured_gripper_contact` 显式校验当前 CAN `held is True`，凡夹爪张开、持物判定为 `False` 或 `None`、夹爪位移 ≥ `gripper_stationary_delta`、换任务/授权周期时立即 `_clear_following()` 清空。参考仍仅用于新深度点面关联（`prediction_only=True, pose_fitted_from_current_depth=True`），绝不直接充当位姿。
- **条件 B**：提取共享方法 `_check_occluded_fixture_retention()` 严格执行 H-32 的 5 项条件（2 个允许原因码、`held=True`、上次 `rgbd_anchor_registration`、晚于闭爪、≤ 20 s）；在 `reject_measurement()` 中跨瞬时视觉故障保留原始 `rgbd_anchor_registration` 引用（显式 `invalidate()` 仍清空），并在 `_partial_observation()` 中调用 `_locate_bucket_for_object()` 并在遮挡失败时套用同一套 5 条件逻辑，输出 `retained_static_fixture` 与同名告警（超时输出 `static_fixture_retention_expired`）。

## 3. 验收结果

- **单元测试**：`HARNESS/tests/test_held_after_transport.py` 新增 13 例（条件 A 瞬时失败保持、张开/CAN 中断/空闭/换 epoch 清除；条件 B `_partial` 5 条件满足保留与 6 种反例拒绝/超时告警），连同既有场景/夹爪/保留套件共 **135+ 项全过**。
- **Trial 3 真实零输出回放（`OBS:49 → [250] mask_truncated → [251] cross_camera → OBS:114`）**：
  - **条件 A 完整链路**：`geometry_scope='coarse_static_task'`，`held=True`，实测深度拟合恢复 `square-1` 位姿 `[0.6626, -0.0349, 0.8183] m` 与 `eef_object_pose`，`surface_association`（`prediction_only=True, pose_fitted_from_current_depth=True`），`fixture.source='retained_static_fixture'`（来自 `gw[245]`，年龄 `7.562 s ≤ 20 s`），可进入 `align`/`inspect_in_hand`。
  - **条件 B `_partial` 兜底链路**（无 `in_hand_reference` 强制触发 `supported_upright_face_not_observed`）：`_partial_observation` 返回 `geometry_scope='coarse_local_environment'`、`held=True`、`fixture.source='retained_static_fixture'`（`7.562 s`）并携带 `static_fixture_retained_while_held_arm_occludes_head`。
- **对照组**：V58 Trial 2 回放与 V59 冻结三套标准零输出回放结果不变（`physical_motion=False`）。
