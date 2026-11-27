# H-39 离线验收报告：持块遮挡且无历史视点时的确定方向横移

## 摘要与结论

**结论：H-39 已通过全部 9 项单元测试（含新增 5 项回退规则与 H-32 `retained_static_fixture` 组合测试）及 V58 Trial 2（`web-7fce1b965f2f4ca497c7d93b505486e9`）运输到位后（`OBS:127`）的正式零输出回放：`ApertureViewpoint` 生成 `0.1100 m`、沿 `[-0.2267, -0.9731, -0.0404]`（右臂负 `y` 侧、垂直于头→积木桌面视线）的 `move_tcp` 候选；正式 `PlanService.plan_skill` 预览 `planned=True`（5 路点，`arm_r_j6 ∈ [28.54°, 31.01°]`），正式 `revalidate_candidate` 通过（`f1-harness-plan-revalidation-v1`），保留桶位姿在预览与复核时年龄分别为 `2.415 s` 与 `2.715 s`（≤ 20 s）。**

**对当前决定的影响：** H-39 仅修改 `HARNESS/src/f1_harness/aperture_view.py`（其余文件未动），离线完全达标，可纳入 V59 冻结包。

## 1. 改动范围与关键实现发现

- **仅改 `HARNESS/src/f1_harness/aperture_view.py`**：
  - 当 `target.held is True`、位姿有效且证据属于当前帧、方孔无位姿、`self.anchor is None`、无动作发布、且有实测 `head_pose` 与头部观测时，沿头→积木视线在桌面平面（去除 `envelope['normal']` 分量）的垂直负 `y` 单位向量生成步长 `(width + hole_width) / 2` 的纯平移候选，`purpose='noncontact_lateral_aperture_reobservation_without_prior_view'`。
  - 每个持块周期至多执行一次：当候选已发布（`context['action_published']`）或 TCP 相对发出点位移超过 `resolution` 后标记 `fallback_used=True`，本持块周期不再二次生成；换任务/换授权/松爪/重抓时重置。有 `self.anchor` 时行为 100% 不变。
  - **真机回放发现的接线细节**：H-32 在头部被遮挡走 `object_only` 返回 `retained_static_fixture` 时，`scene.frames` 仅保留右腕帧，但 `live_scene.py` 仅当本帧存在已配对头部帧与实测头部运动学时才传入非空 `head_pose`。因此在 `aperture_view.py` 内将头部观测条件写为 `any(f.camera == 'head' for f in scene.frames) or scene.fixture.source == 'retained_static_fixture'`（同时仍强制 `head_pose is not None`），既不改动 `scene_pipeline.py`/`live_scene.py`，又保证真机 H-32 场景下回退横移能真正触发。

## 2. V58 Trial 2 运输到位零输出回放结果

| 项目 | 实测值 |
|---|---|
| 来源运行与观测 | `web-7fce1b965f2f4ca497c7d93b505486e9` / `OBS:127` |
| 桶位姿来源 | `retained_static_fixture`（H-32 保留） |
| 保留桶位姿年龄（预览 / 复核） | `2.4152 s` / `2.7152 s`（+0.300 s 复核帧） |
| 起点 TCP (`world`, m) | `[0.7196, -0.0214, 0.8683]` |
| 目标 TCP (`world`, m) | `[0.6947, -0.1285, 0.8639]` |
| 位移与方向 | `0.1100 m`，`delta = [-0.0249, -0.1071, -0.0044] m`，单位方向 `[-0.2267, -0.9731, -0.0404]` |
| `PlanService.plan_skill` 预览 | **`planned = True`**，5 路点，`arm_r_j6 ∈ [28.538°, 31.006°]` |
| `revalidate_candidate` 正式复核 | **通过**（`f1-harness-plan-revalidation-v1`） |
| `physical_motion` | `False` |

## 3. 单元测试

- `pytest -q HARNESS/tests/test_held_aperture_view.py` → **9 passed**（原有 4 例 + 新增 5 例：无 anchor 负 `y` 垂直公式步长、有 anchor 行为不变、每持块周期仅执行一次且重抓重置、缺 `head_pose`/缺头部帧拒绝与 H-32 保留兼容、未持块或无位姿拒绝）。
