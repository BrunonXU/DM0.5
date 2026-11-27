# H-36 离线验收报告：全候选（≤64）桶体投影排除

## 摘要与结论

**结论：H-36 实现与全部单元测试（10/10）、正式 HTTP 通信（35 候选 `X-SAM-Tracking-Result` = 3,716 字节）及 V57 Trials 4–6 离线实测（5/6 帧确认、3/3 试验可恢复，实测积木位姿 `[0.820, -0.312, 0.606] m`）均已通过；V58 Trial 1 桶体投影排除本身 100% 命中（分别排除 31/31 与 32/32 个桶身印刷候选），但因画面右上角桌面外远景（`[377, 27, 401, 49]`，深度 `1.81–1.84 m`，世界 `z = −0.14 至 −0.16 m`）存在 1 个无关低分远景误检（`score = 0.258–0.311`、`area = 267–268 px`），排除后 `remaining = 2`，按规格保守保持 `unresolved`（未放宽任何阈值或增加未授权过滤）。**

**对当前决定的影响：** H-36 严格遵守“只减不增、剩余恰好 1 个才重播、不放宽任何阈值”的安全契约，可直接纳入 V59 冻结包（既解决纯桶身印刷干扰，又为所有 S0 失败保留完整 64 候选诊断）。V58 Trial 1 暴露的桌面外远景低分误检作为新事实如实上报，交由下一轮审阅决定是否按工作空间/桌面范围或阈值治理。

## 1. 改动范围

- `HARNESS/src/f1_harness/sam_seed.py`：`SEMANTIC_CANDIDATE_SUMMARY_LIMIT = 64`，按 `score` 降序输出完整候选摘要（保留原 `index`、整数框、3 位小数分数）及 `semantic_candidate_summary_truncated` 标记。
- `HARNESS/src/f1_harness/mask_service.py`：仅在 `unresolved`、`semantic_candidates >= 2`、`len(summary) == semantic_candidates` 且未截断时启用桶体投影排除；排除中心落入桶投影矩形的候选后，恰好剩 1 个时用其框按 `sam_tracking_padding_px` 经 `_segment_box(..., reseed=True)` 重播一次，仍过 `minimum_score`、`minimum_area_px` 与服务端唯一性检查。
- `HARNESS/src/f1_harness/services.py`：gateway 启动时从 `plan.local.json` 读取 `role == 'bucket'` 障碍物并集 AABB 注入 `scene.geometry.fixture_exclusion_box`。
- `HARNESS/src/f1_harness/scene_pipeline.py`：新增 `_project_fixture_exclusion_box()`（对 3D AABB 12 条边做近平面 `z >= 0.01 m` 裁剪并外扩 `prompt_padding_px`，防止桶沿跨相机平面时投影发散）；S0 阶段右腕未播种、未持块且无提示时先消费同帧头部 `rgbd_anchor_registration` 桶位姿；失败诊断写入 `fixture_exclusion` 与完整候选摘要。
- `HARNESS/tests/test_fixture_exclusion.py`：10 项测试（原 6 例 + 35 候选排除 34 确认 + 截断不启用 + 摘要条数不足不启用 + `Sam3SeedProvider` 排序/截断 + 真实 HTTP `X-SAM-Tracking-Result` 35 候选往返）。

## 2. 真实 SAM3/SAM2 离线实测结果

在临时 V59 `sam_seed.py` SAM 服务（`sam3.pt` + `sam2.1_hiera_tiny.pt`）上跑完 V58 Trial 1（同帧头部实测 `rgbd_anchor_registration` 桶位姿，完整 `CalibratedScenePipeline.process()`）与 V57 Trials 4–6（因 V57 在 V5 预设下头部未测出桶，离线采用同一远端桶区、V6 预检 `probe-v6-01` 的头部实测桶位姿作为代理，下表逐帧标明）：

| 试验与快照 | 桶位姿来源 | 投影排除框 `[x0,y0,x1,y1]` | 排除前候选 | 桶内排除 | 剩余候选 | 是否确认 | 实测积木位姿 `[x,y,z]` (m) | `X-SAM-Tracking-Result` 字节数 | 说明 |
|---|---|---|---:|---:|---:|---|---|---:|---|
| V58-T1 `6c2bc8f6` | 同帧头部实测 (`rgbd_anchor_registration`) | `[0, 0, 186, 480]` | 33 | 31 | 2 | 否 (`unresolved`) | — | 3,671 | 真实块 `[390,195,486,297]` (`0.973, 7082px, z=0.591m`) + 桌面外远景 `[377,27,401,49]` (`0.258, 268px, depth=1.841m, z=-0.159m`) |
| V58-T1 `e47022f1` | 同帧头部实测 (`rgbd_anchor_registration`) | `[0, 0, 183, 480]` | 34 | 32 | 2 | 否 (`unresolved`) | — | 3,766 | 真实块 `[391,196,488,298]` (`0.977, 7061px, z=0.592m`) + 桌面外远景 `[378,28,402,49]` (`0.311, 267px, depth=1.811m, z=-0.144m`) |
| V57-T4 `3f20c958` | V6 预检同桶区代理 (`probe-v6-01`) | `[0, 0, 193, 480]` | 32 | 31 | 1 | **是** (`score=1.0, 7065px`) | `[0.8199, -0.3120, 0.6064]` | 3,433 | 唯一剩余真实积木并重播确认 |
| V57-T4 `518e8615` | V6 预检同桶区代理 (`probe-v6-01`) | `[0, 0, 195, 480]` | 35 | 33 | 2 | 否 (`unresolved`) | — | 3,716 | 真实块 (`0.973, 7079px`) + 画面底边低分误检 `[231,441,262,481]` (`0.241, 344px`) |
| V57-T5 `5e8def28` | V6 预检同桶区代理 (`probe-v6-01`) | `[0, 0, 187, 480]` | 22 | 21 | 1 | **是** (`score=1.0, 7039px`) | `[0.8199, -0.3124, 0.6060]` | 2,513 | 唯一剩余真实积木并重播确认 |
| V57-T5 `9b3e6505` | V6 预检同桶区代理 (`probe-v6-01`) | `[0, 0, 185, 480]` | 34 | 33 | 1 | **是** (`score=1.0, 7028px`) | `[0.8194, -0.3121, 0.6062]` | 3,617 | 唯一剩余真实积木并重播确认 |
| V57-T6 `0b437a50` | V6 预检同桶区代理 (`probe-v6-01`) | `[0, 0, 176, 480]` | 25 | 24 | 1 | **是** (`score=1.0, 7002px`) | `[0.8199, -0.3116, 0.6060]` | 2,780 | 唯一剩余真实积木并重播确认 |
| V57-T6 `f101da22` | V6 预检同桶区代理 (`probe-v6-01`) | `[0, 0, 176, 480]` | 26 | 25 | 1 | **是** (`score=1.0, 6973px`) | `[0.8197, -0.3117, 0.6063]` | 2,869 | 唯一剩余真实积木并重播确认 |

## 3. 正式通信验收（35 候选）

- 35 候选时 `X-SAM-Tracking-Result` HTTP 响应头实测长度 **3,716 字节**（34 候选为 3,766 字节），远低于 HTTP 头上限（常见 8 KiB / 16 KiB）。
- `test_http_tracking_header_roundtrip_35_candidates` 经真实 `ThreadingHTTPServer` + `httpx` 客户端完整取回全部 35 条候选摘要（无截断、字段齐全）。
- 回归测试：`pytest -q tests/test_fixture_exclusion.py tests/test_sam_identity_recovery.py tests/test_scene_pipeline.py` → 19 passed。
