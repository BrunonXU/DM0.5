# H-26：V54 上线核验、250 ms 同戳帧保留验证与头部／右腕相机参数对照

> **结论**：
> 1. **V54（`8818976408ce`）已于 2026-11-26 离线全量验收并上线至 6000D 与 F1-09**（`f1-harness-workspace-v54.service`，4242 显示已切至 `F1 Harness 同戳帧保留与固定程序入口 V54`，未重启 4242、PID 保持 `2581`、档案保持 `robot-v22.json`，`start_sent=false`）。
> 2. **上线后只读预检五项全过**（`post-deploy-preflight-result.json`，即 `pg-01`）：`arm_l_j2` 实测 `[-77.317°, -77.317°]` 距 `−94.0°` 下限余量 **`16.68°`**（≥ 16°）；连续配对窗口 **`37.81 s`**（≥ 12.0 s），`duplicate_source_stamp:head` 降至 **0**；第 4 项按新条件通过（积木 `202` / 桶 `202` / SAM 增量 `339`，`hole_direct_count=0`）。
> 3. **右腕 vs 头部相机运行参数已只读导出对照**（未修改机载 `head_gemini330.launch.py` 或任何相机驱动参数）：头部奥比中光（Orbbec Gemini 330）启用 `time_domain=global`、`enable_sync_host_time=true`（`time_sync_period=6.0`）、`enable_frame_sync=true`、`color_fps/depth_fps=0`（自动）；右腕与左腕 RealSense D405 均为固定 `640,480,30`、`global_time_enabled=true`、`enable_sync=false`、`SYSTEM_DEFAULT` QoS。

---

## 一、V54 软件与回放验收回执（`release-validation.json`、`post-activation.json`）

- **版本号与身份**：`F1 Harness 同戳帧保留与固定程序入口 V54`（`bundle_id`: `8818976408ce68d0c63a5e7602b26d28ca4cf9d54f0e3214cf4ffda9d3656002`）
- **站点前置核对**：`gateway.local.json` 中 `scene.max_age_ms = 500 > 250`；`arm_l_j2` 下限收回 `[-94.0, 120.0]`。
- **测试回执**：全量 pytest **3,190 例**（项目 3,120 + 原生数值 70，0 失败，含 `test_source_stamp_hold.py` 4 例及原 8+2 个观测测试）、原生集成 **62/62**、F1-4242 关节合同 **37/37**、DAgger/F1 边界 **12/12**、**3+1 条零输出回放**（含 `--planner baseline` 且 `model_requests=0`）全部通过；所有提交净增 `raise HarnessError = 0`。
- **上线核验**：远端 373 个包文件 + 13 个站点 JSON 哈希核对一致；10 组双路线只读探针通过；4242 保持 `robot-v22.json` 未重启；`start_sent=false`。

---

## 二、头部与右腕（附左腕）相机运行参数只读对照（`camera-params-head-vs-right.json`）

只读执行 `ros2 param dump`，未修改任何启动文件或运行参数：

| 对比项 | 头部 `/camera/head/head`（Orbbec Gemini 330） | 右腕 `/camera/right_wrist`（RealSense D405） | 左腕 `/camera/left_wrist`（RealSense D405） |
|---|---|---|---|
| 时间域 / 全局时间 | `time_domain = global` | `depth_module.global_time_enabled = True` | `depth_module.global_time_enabled = True` |
| 主机时间同步 | `enable_sync_host_time = True`（周期 `6.0 s`） | 无独立主机时钟同步开关 | 无独立主机时钟同步开关 |
| 帧同步 | `enable_frame_sync = True`（`sync_mode = standalone`） | `enable_sync = False` | `enable_sync = False` |
| 彩色／深度帧率配置 | `color_fps = 0`, `depth_fps = 0`（自动） | `color_profile = 640,480,30`, `depth_profile = 640,480,30` | `color_profile = 640,480,30`, `depth_profile = 640,480,30` |
| QoS | `color_qos = default`, `depth_qos = default` | `color_qos = SYSTEM_DEFAULT`, `depth_qos = SYSTEM_DEFAULT` | `color_qos = SYSTEM_DEFAULT`, `depth_qos = SYSTEM_DEFAULT` |
| 对齐设置 | `align_mode = SW`, `align_target_stream = COLOR` | `align_depth.enable = False` | `align_depth.enable = False` |
| `use_sim_time` | `False` | `False` | `False` |

---

## 三、上线后只读预检摘要（`post-deploy-preflight-result.json`）

- 5 项检查：**全部通过**（`passed = true`，`start_sent = false`，`hardware_command_publishers = 0`）
- 关节余量（第 1 项）：`arm_l_j2` 实测 `[-77.317°, -77.317°]`，下限 `−94.0°`，`min_margin_deg = 16.683°`（满足 `>= 16°`）
- 配对窗口（第 2 项）：`contiguous_s = 37.811 s`（≥ 12.0 s），`duplicate_source_stamp:head = 0`，本轮 `held_source_frames = 39`
- 场景几何（第 4 项）：`block = 202`、`fixture = 202`、`sam_requests_delta = 339`、`hole_direct_count = 0`、`full_scene_count = 0`
