# H-21 第一批前 3 次直立真机试验阶段复盘（V54，固定程序基线）

> **核心结论（3 次实测已跑出真实首动与抓举运输，定位到 2 个极清晰的可复现物理／软件根因，不是“桶放太后”）**：
> - **实际运行模式**：3 次全部为 V54（`8818976408ce`）固定程序基线（`planner_effective = "baseline"`，**`model_calls = 0`**，4242 页面显示 Astra 不作数）；全程 **0 左臂碰撞、0 关节越限撤权、0 取帧掉线、0 硬件主动故障撤权**，每次停止后均确认 `measured_stationary`。
> - **漏斗到达**：S0 `3/3`、**S1–S4 `2/3`**（真实接近、闭爪抓起、验证持块）、**S5 `1/3`**（真实运到桶上方并完成在手观测）、S6 `0/3`。**未触发“前 3 次均停在 S0”止损条件**，但已暴露两个阻断落孔的明确根因，按用户要求先整理交 Claude 审阅决策再决定是否继续补满 10 次。
> - **两个已定位根因**：
>   1. **Trial #1（停在 S4 `transfer_above_hole` → `motion_stalled`）**：规划器把右腕第 6 轴 `arm_r_j6` 规划到了 **`+59.87°`**（紧贴软件上限 `+60.0°`），而**真机物理／固件在 `+54.86°` 顶死不动**（其余 6 个右臂关节全部追到 `0.00°–0.33°` 以内，唯独 `arm_r_j6` 差 `5.01°`、末端差 `16.6 mm > 15 mm`，触发 `motion_stalled`）。
>   2. **Trial #3（停在 S5 `align`）**：`transfer_above_hole`（此处 `arm_r_j6 = 17.0°`）与 `inspect_in_hand` 均成功；到达桶上方后，右臂持块遮挡了**头部相机**看桶盖地标（`fixture_aperture_not_detected_in_search_region`、`fixture_collision_geometry = False`），尽管**左腕相机清晰看到全部三孔**（`square=[346,358]`、`triangle=[325,218]`、`circle=[383,292]`）。固定程序随后尝试执行 `move_tcp` 侧移让开视野，但 `move_tcp` 预览被 `full_scene_required_for_motion` 拒绝（要求头部先给出实时桶位姿才允许侧移），形成**“挡住桶 → 没桶位 → 不让侧移让开视野”的恢复死锁**。
>   3. **Trial #2（停在 S0 `approach_grasp`）**：无动作，右腕 SAM 报 `mask_uncertain_requires_new_prompt:object=square-1`（固定程序无模型框选提示，属单次视觉置信度不足）。

---

## 一、3 次试验总表（`funnel.json`、`trials-summary.json`）

| 试验 | `run_id` | 最远阶段 | 首次出动 | `model_calls` | 执行链与停点原因 |
|---|---|---|---:|---:|---|
| Trial #1 | `web-dbe926ed282e` | **S4** | 4.40 s | 0 | `approach_grasp` ✓ → `grasp` ✓ → `verify_grasp` ✓ → `transfer_above_hole` **失败（`motion_stalled`：`arm_r_j6` 目标 `+59.87°`、实测卡死 `+54.86°`）** |
| Trial #2 | `web-9da20b8369f9` | **S0** | — | 0 | `approach_grasp` 未动（`mask_view=right:mask_uncertain_requires_new_prompt:object=square-1`） |
| Trial #3 | `web-5653aac6c68e` | **S5** | 8.51 s | 0 | `approach_grasp` ✓ → `grasp` ✓ → `verify_grasp` ✓ → `transfer_above_hole` ✓ → `inspect_in_hand` ✓ → `align` **停下（头部被右臂遮挡失桶位，且 `move_tcp` 侧移被 `full_scene_required_for_motion` 拒绝死锁）** |

---

## 二、Trial #1 `arm_r_j6` 顶死实测证据（`harness_motion_checkpoint`，路点 10）

| 关节 | 目标角（deg） | 实测角（deg） | 跟踪误差（deg） | 软件限位（deg） |
|---|---:|---:|---:|---|
| `arm_r_j1` | -72.57 | -72.57 | 0.00 | `[-178, 178]` |
| `arm_r_j2` | -92.13 | -91.80 | -0.33 | `[-94, 120]` |
| `arm_r_j3` | 57.55 | 57.77 | -0.22 | `[-178, 178]` |
| `arm_r_j4` | -134.03 | -133.79 | -0.24 | `[-145, 60]` |
| `arm_r_j5` | 8.79 | 8.71 | 0.08 | `[-178, 178]` |
| **`arm_r_j6`** | **+59.87** | **+54.86** | **+5.01** | **`[-60, 60]`** |
| `arm_r_j7` | -31.68 | -31.79 | +0.12 | `[-90, 90]` |

---

## 三、桶位与参考位姿对比（回应“桶是否放太后”）

- `plan.local.json` 标称参考桶位：`[0.6519, -0.0646, 0.7685]` m（世界系：`+x` 向前远离机身、`+y` 向左、`+z` 向上）。
- Trial #1 实测桶位：`[0.6705, -0.0429, 0.7717]` m（相对参考：`dx = +18.6 mm`, `dy = +21.7 mm`, `dz = +3.2 mm`）。
- Trial #3 实测桶位：`[0.6606, -0.0629, 0.7724]` m（相对参考：`dx = +8.7 mm`, `dy = +1.7 mm`, `dz = +3.9 mm`）。
- **结论**：桶并没有“放太后”——仅比标称前偏 9–19 mm，且 Trial #3 已顺利到达桶上方（`arm_r_j6 = 17.0°`）。真正导致卡住的是：（1）Trial #1 运输构型贴近 `arm_r_j6 = +59.87°` 撞上真机约 `+54.9°` 的硬/固件止位；（2）Trial #3 到达桶上方后右臂挡住头部视野，且 `move_tcp` 侧移恢复要求头部先重检出桶而死锁。
