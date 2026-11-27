# H-45 验收证据：排除桌面以外的远处候选（2026-11-27）

**在 H-36 桶体投影排除路径上增加中心像素深度无效与低于桌面支撑面 5 cm 以上的排除规则后，V58 Trial 1 的两帧实拍失败快照 2/2 精确排除远处背景误检（`remaining_candidates = 1`），并由真实 SAM3/SAM2 恢复出确认的积木掩码与三维位姿。** 桌面支撑面直接读取站点 `plan.local.json` 中已有的 `table` 半空间参数（`point` / `normal`），未另设阈值或数值，也不放宽任何分数、面积或面拟合门槛。

## 1. 改动摘要

- `HARNESS/src/f1_harness/services.py`：在网关启动加载 `plan.local.json` 时，提取已有 `role='table', type='halfspace'` 支撑面参数（`frame_chain` 一致时）注入 `config['scene']['geometry']['table_support_plane']`。
- `HARNESS/src/f1_harness/scene_pipeline.py`：在 `measure_primary()` 的未初始化 `discover` 路径下构造 `candidate_depth_filters`（ROI 对齐深度、裁剪内参、当前相机世界位姿、桌面支撑面与 `0.05 m` 阈值）并随 `exclusion_boxes` 一并传给 `_segment_view`。
- `HARNESS/src/f1_harness/mask_service.py`：在 `_segment_state` 候选过滤阶段按中心像素 `(round(cx), round(cy))` 检查对齐深度有效性及世界坐标相对桌面的有符号高度；若深度无效（越界 / 非有限 / ≤ 0）或低于桌面 5 cm 以上则排除；排除后仍要求恰好剩 1 个候选并通过原 `minimum_score` 和 `reseed=True` 确认流程。

## 2. 单元测试

`HARNESS/tests/test_fixture_exclusion.py`：**14 passed**（新增 4 条 H-45 用例：深度无效排除、低于桌面 > 5 cm 排除、两块桌上积木均保留且保持 unresolved、与 H-36 桶排除联合生效）。

## 3. V58 Trial 1 两帧真机快照离线回放（真实 SAM3/SAM2 `127.0.0.1:19842`）

原始结果见 [v58-trial1-replay.json](v58-trial1-replay.json)。

| 快照 | 候选总数 | 桶投影排除 | 深度无效排除 | 低于桌面 > 5 cm 排除 | 剩余候选 | 确认状态 | 掩码面积 / 分数 | 积木世界位姿 (m) |
|---|---:|---:|---:|---:|---:|---|---:|---|
| `6c2bc8f6cb994ee2ae4bf0debd51fa47.json` (`:5`) | 33 | 31 | 0 | 1 | **1** | `confirmed` | 7075 px / 0.99995 | `[0.8199, -0.3127, 0.5462]` |
| `e47022f1debd44ac90eeb796dd26dddb.json` (`:4`) | 34 | 32 | 0 | 1 | **1** | `confirmed` | 7061 px / 0.99995 | `[0.8200, -0.3126, 0.5459]` |
