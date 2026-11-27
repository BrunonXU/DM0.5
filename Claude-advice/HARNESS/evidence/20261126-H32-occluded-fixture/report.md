# H-32 遮挡保留桶位姿与让开视野（V57）验证记录

本目录记录 H-32（`F1 Harness 遮挡保留桶位姿与让开视野 V57`，`bundle_id`: `de4f6b0beaea03eab6c2b6c9c34901c4cf04e1b44d3846f4c834133c126e7d6d`）的软件实现、试验 3 原始输入让开动作零输出回放、V57 三条标准冻结回放审计及部署核验结果。正式 10 次直立真机试验结果另归 `Claude-advice/HARNESS/evidence/20261126-first-batch/`。

改动仅涉及单个源码文件 `HARNESS/src/f1_harness/scene_pipeline.py`（净增 `raise HarnessError` = `0`），在持块遮挡头部且 `independent_fixture.reason` 属于允许列表（`bucket_landmark_registration_unavailable`、`partial_lid_landmarks_unavailable`）、上次桶位姿为实测 `rgbd_anchor_registration`、测量晚于本次闭爪发布时刻且不超过 `20 s` 时，将原实测桶位姿以 `retained_static_fixture` 输出（用于 `move_tcp` 让开路径碰撞扫掠，对准/插入/释放/放置验证仍要求实测方孔）；超过 `20 s` 清除并发出 `static_fixture_retention_expired` 告警。

## 关键验证结论

| 检查项 | 结果 | 证据文件 |
|---|---|---|
| 试验 3 ARM 实际遮挡原因码 | 全部遮挡帧为 `partial_lid_landmarks_unavailable`（在允许列表内） | `../20261126-H33-j6-envelope/arm-scene-timing-summary.json` |
| H-32 专项单测 | `13 passed in 0.19s`（覆盖双允许原因、5 个排除原因、未持块、早于闭爪、不链式保留、超 20s 清除+过期告警、允许 `move_tcp` 拒绝 `align`/`insert`/`release`） | `HARNESS/tests/test_occluded_fixture_retention.py` |
| 试验 3 原始输入零输出让开回放 | `planned=True`，`move_tcp` 让开位移 **`0.1329 m`**（13.3 cm），`arm_r_j6` `[4.13°, 16.88°]`，保留桶位姿预览龄 `13.569 s`、重验龄 `14.169 s`，正式重验 **`revalidation_ok=True`**（`f1-harness-plan-revalidation-v1`） | `trial-03-sidestep-replay.json` |
| 全量回归与跨工程集成 | `3,137 passed, 70 skipped`（+13 条新测，0 新增失败）；`check_native_integration.py` `62 passed` | `v57-qualification.json` |
| V57 三条冻结零输出回放 + 审计 | `normal`（10 阶段）、`grasp-miss`（12 阶段）、`aperture-occluded`（12 阶段）全过，`physical_motion=False`，`paid_model_calls=0` | `v57-qualification.json` |
| 部署与 4242 只读核验 | `f1-harness-workspace-v57.service` 上线，6000D `19843` 与 ARM `4242`（`robot-v22.json`，未重启）均显示 V57，10/10 路由探针通过，`start_sent=False` | `v57-qualification.json` |
