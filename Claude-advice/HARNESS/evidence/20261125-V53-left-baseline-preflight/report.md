# F1 Harness V53：左臂 J2 新基线、只读预检脚本与 Cyclone 反馈核查

**V53 候选（`53af3b1d4406`）已完成冻结并通过 6000D 全量软件回归与三条零硬件输出回放；当前 F1-09 离线（管理隧道断开），ARM 上线、home 预设写入、现场原位 `preflight_scene.py` 实测及真机单块试验待机器人联机后接续。** 本版仅调整左臂第 2 关节（`arm_l_j2`）下限由 `−94.0°` 至 `−94.7°`（右臂与其他 21 轴、碰撞包络及控制源码均未改动），并新增可重复运行的只读预检脚本 [`HARNESS/scripts/preflight_scene.py`](../../../../HARNESS/scripts/preflight_scene.py)。

用户已确认拆掉的是**左侧夹爪**，且拆装时**未碰到左腕相机支架**；碰撞模型保守保留原夹爪几何（`arm_tip_extension = 0.10 m`）。本次离线验收未发起任何真机动作，硬件输出为 0；4242 当前运行版本仍为 V52，`review.md` 各条目保持 `[~]`，待真机结果产生后再回写。

```mermaid
flowchart LR
  A[拆左夹爪后 arm_l_j2 静置约 -94.156°] --> B[核查机械极限: URDF / M6 均为 -120°]
  B --> C[仅放宽 arm_l_j2 下限至 -94.7°<br/>右臂与其他 21 轴 / 碰撞不变]
  C --> D[新增只读 preflight_scene.py<br/>冻结 V53 并通过 6000D 全量离线验收]
  D --> E[待 F1-09 联机: 上线 V53 + 同步 home 预设<br/>+ 跑原位 preflight_scene.py]
```

## 1. 左臂第 2 关节（`arm_l_j2`）新基线核查与依据

### 1.1 `−94.000°` 实际分布与机械极限核查

| 层级 | 文件 / 组件 | 原 `arm_l_j2` 下限 | V53 改动 | 性质说明 |
|---|---|---:|---:|---|
| 机械 / URDF | `web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf`（`arm_L_joint2`） | `−120.0°`（`−2.094395 rad`） | 不变 | 真实模型下限，`−94.7°` 距此尚有 `25.3°` 余量 |
| 天机 M6 运动学配置 | `motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg` | `−120.0°` | 不变 | 硬件运动学极限；`MarvinSDK.h` 的 `OnSetJointLmt_A/B` 仅设速度/加速度比例，不改位置限位 |
| 厂商驱动 `motor_ctl_node` | 二进制 SHA-256 `898ef58f…ce0c8` | `−94.0°` | 不变 | `applyJointLimits()` 仅钳位下发给 `/motion_ctl/joint_ctl` 的目标指令，**不是机械硬限位**，不阻断低于 `−94.0°` 的真实反馈 |
| ARM 运行档案 | `robot-v22.json` → 新建不可变 `robot-v23.json` | `−94.0°` | 仅 `arm_l_j2 → −94.7°` | 保留 `robot-v22.json` 不可变；`robot-v23.json` SHA-256 `f5c9dae8…82032` |
| 22 轴合同与依赖锁 | `F1-4242/configs/f1-09.json`、`f1-09-joints.json`、`DAgger/F1/configs/f1_infra_binding.json` | `−94.0°` | 仅 `arm_l_j2 → −94.7°` | 其余 21 轴（含 `arm_r_j2 = [−94.0, 120.0]`）与所有网格/安全配置哈希不变 |
| HARNESS 站点配置 | `f1-llm-53af3b1d4406/numerics.local.json`、`launch.local.json` | `limits[1] = [−94.0, 120.0]` | `limits[1] = [−94.7, 120.0]` | `limits[8]`（`arm_r_j2`）维持 `[−94.0, 120.0]` |

**漂移物理原因：** `motor_ctl` 运行在力矩＋关节阻抗模式（`arm_control_mode=3, arm_impedance_type=1`），重力补偿参数 `end_tool_dyn_a` 按 `0.622 kg` 夹爪标定。拆除左侧夹爪后左臂变轻，在目标 `−93.345°` 附近静置时略微外偏至约 `−94.156°`。

### 1.2 支撑读数与新下限计算

| 样本来源 | 样本量 / 时长 | `arm_l_j2` 最小值 | `arm_l_j2` 最大值 |
|---|---:|---:|---:|
| `v52-formal-service-c`（2026-11-25 10:01 UTC） | 294 个独立反馈 / 24.74 s | **`−94.155884°`** | `−94.155807°` |
| `web-bcfd8730`（2026-11-25 09:24 UTC） | 5,487 条反馈 | **`−94.153969°`** | `−93.921211°` |
| `web-e992a2d9`（2026-11-25 09:23 UTC） | 158 条反馈 | `−93.901512°` | `−93.681076°` |
| 4242 归位后实时采样（14:01 UTC） | 201 条反馈 / 5 s | `−93.667130°` | `−93.667046°` |

新下限 = 近期静止最小值 `−94.155884°` 再向外留 `0.5°` 余量 = `−94.655884°` → 取 **`−94.7°`**（远在 `−120.0°` 机械极限内）。

### 1.3 拆夹爪侧、碰撞模型与 infra home 预设

- **拆除侧确认**：左侧夹爪已拆除（`controller.local.json` 中 `left_gripper_present: false`、`right_gripper_present: true`、`left_wrist_camera_present: true`）。
- **左腕相机支架**：用户已明确确认拆左夹爪时**未碰到左腕相机支架**。
- **碰撞模型**：`f1_safety_config.yaml`（SHA-256 `83ad2c6b…25422`）完全不变，继续对左右末端统一保留 `arm_tip_extension = 0.10 m` 和 `arm_diameter_inflation = 0.02 m`（对无夹爪的左臂更保守）。
- **Infra home 预设同步（待 F1-09 联机执行）**：ARM 上 `presets.json` 的活跃预设「积木demoV3-左腕观测」（id `5f080cd1a61f4b098a6f33041a4574b3`）原 `arm_l_j2 = −93.344543°`。待机器人联机后，先备份原文件，仅将该预设的 `arm_l_j2` 更新为现场实测停放角（约 `−93.667°`，既在 `−94.0°` 驱动命令下限内、不会被 `motor_ctl` 钳位，即便静置漂至 `−94.156°` 偏差也仅 `0.49° < 1.0°` 归位容差）；两个出厂默认预设（受 `joint_contract.py` 绑定）保持不动。详细记录见 [joint-limit-rebaseline.json](joint-limit-rebaseline.json)。

## 2. 只读场景预检脚本 `HARNESS/scripts/preflight_scene.py`

新增独立只读脚本 [`HARNESS/scripts/preflight_scene.py`](../../../../HARNESS/scripts/preflight_scene.py) 及离线单元测试 [`HARNESS/tests/test_preflight_scene.py`](../../../../HARNESS/tests/test_preflight_scene.py)：

- **严格只读、不进入运行路径**：不被任何运行时模块导入；在隔离的临时本地环回端口与 Unix Socket 上启动冻结 `verify`、`numerics`、`gateway`，被动订阅 `/hal/joint_states` 与左腕图像，拒绝全部控制 IPC（`PermissionError`），显式校验 `hardware_command_publishers == 0`、`final_authorized == False`、`request_publications == 0`、`start_sent == False`。
- **5 项逐条核查与明确结论**：
  1. `joints_within_limits`：核对全 22 轴实测范围是否在 profile 与 numerics 审核范围内，并用 live 反馈向冻结 `numerics` 发一次镜像 `forward_pose` 校验；
  2. `dual_rgbd_feedback_paired`：头部与右腕 RGB+Depth 与同刻关节反馈配对（skew ≤ `frame_state_skew_ms`），连续窗口 ≥ 10 s（默认 12 s），四路图像均随时间推进；
  3. `left_wrist_streaming`：左腕相机在连续窗口内持续出帧（跨度 ≥ 10 s、帧数 ≥ 10、最大间隔 ≤ 2 s）；
  4. `scene_geometry_solved`：接入正式 SAM 服务解出方块（`square-1`）、桶/夹具（`rgbd_anchor_registration`）和方孔（`square-hole-1`）的有效三维位姿；
  5. `readonly_safety`：零命令发布者、零控制 IPC、零运动授权、零动作发布。
- **支持两种调用方式**：在 6000D 上单命令编排（自动通过隔离 `-R` 隧道连接本机 SAM 并拉回 `result.json`），或在 ARM 上以 `--local` 直接执行。

## 3. Cyclone 环境下控制端关节反馈来源与「0 发布者」根因说明

- **各进程实际 RMW**：`motor_ctl_node`（底层驱动）与相机节点运行 **Fast DDS**（`rmw_fastrtps_cpp`，`SUBNET`，映射 `/dev/shm/fastrtps_*`）；控制端 `owner`（`web_process.py`）和 `4242`（`server.py`）运行 **Cyclone DDS**（`rmw_cyclonedds_cpp`，绑定 `enP3p1s0f1`，`SUBNET`）。
- **控制端如何确认停稳**：`owner` 通过 `F1-4242/robot/adapter.py` 中的 `RosTransport` 在 Cyclone DDS 下订阅 `/hal/joint_states`（`qos_profile_sensor_data`），经标准 RTPS/UDPv4 与 `motor_ctl`（Fast DDS）互通。实查 11 月 25 日 `web-bcfd8730` 的 `owner.log`：`owner` 启动后 `1.006 s` 收到 `first_valid_feedback`，全程收到 `5,487` 条真实 `/hal/joint_states` 样本，因此 `measured_stationary` 来自真实的底层 HAL 反馈。
- **为何早期 05:01 探测在 Cyclone 下看到 0 发布者**：复查 `joint-topic-raw.json`，Cyclone 进程当时在 `graph_matching_topic_at_end` 中**已发现话题名 `/hal/joint_states`**，但 `publisher_info_at_end` 为空且回调为 0，属于 3 个并发探测进程同时在 `enP3p1s0f1` 启动时的跨 RMW SPDP/SEDP 短窗偶发发现漏配。
- **今日 14:01–14:05 UTC 单进程实时对照实测**：
  - Cyclone DDS 单进程稳定收包：`cyc_sys` 3 s 收 **103 包**、`cyc_venv` 3 s 收 **149 包**（~50 Hz）、`cyc_owner_sensor` 2.5 s 收 **75 包**；
  - 反而是**默认 Fast DDS（启用 SHM）**在同机多次实测均出现 **`pubs=1 ('motor_ctl', 'RELIABLE')` 但 `msgs=0`**（本机 `/dev/shm/fastrtps_*` 共享内存传输卡死）；
  - 将 Fast DDS 显式切为 **UDPv4-only**（禁用 SHM）后立即恢复 3 s **149 包**（~50 Hz）。

## 4. V53 离线软件与零硬件输出回放验收结果

| 验收项 | 结果 | 详情 |
|---|---|---|
| V53 冻结全量 pytest（`snapshot-test-view`） | **通过（3,176 / 3,176）** | 项目 venv 通过 3,106（含 `test_preflight_scene.py` 3 项），跳过 70 项原生数值用例；数值 venv 针对 V53 `pinned-native-infra` 补跑全部 70 项通过；精确 100% nodeid 并集，失败 0，未解跳过 0 |
| 跨工程原生集成（`check_native_integration.py`） | **通过（62 / 62）** | 0 失败、0 跳过 |
| 项目布局校验（`check_project_layout.py`） | **通过** | 公开模式与 `--with-evidence` 均通过 |
| 跨仓关节合同与数值边界 | **通过（41 + 31）** | `F1-4242` 关节合同与启动测试 41/41；`DAgger/F1` 数值与连续限位边界测试 31/31（验证 `arm_l_j2` 新下限 `−94.7°` 且 `arm_r_j2` 仍拒绝 `−94.001°`） |
| 冻结站点路由快照 | **通过（10 / 10）** | `astra` 与 `vla_astra` × 修正预算 0..4；全部绑定 `arm_l_j2=[−94.7, 120.0]`、`arm_r_j2=[−94.0, 120.0]` |
| 零硬件输出全流程回放 + 输出审计 | **通过（3 / 3）** | `normal`（10 阶段 / 2,405 输出 / 52.09 s）、`grasp-miss`（12 阶段 / 2,766 输出 / 59.74 s）、`aperture-occluded`（12 阶段 / 2,702 输出 / 57.85 s）；全部 `replay_output` / `grasp_output` / `cycle_output` 审计通过，硬件输出 0 |
| ARM 上线核验与现场 `preflight_scene.py` | **待执行** | F1-09 当前离线（`127.0.0.1:22009` Connection refused）；待联机后执行并补充回执 |
| 真机单块试验与 `run_funnel.py` | **未开始** | 须待 V53 上线且原位 `preflight_scene.py` 通过后再由操作者点击「开始任务」 |

明细见 [software-checks.json](software-checks.json) 与 [replays.json](replays.json)。

## 5. 待 Claude 审阅确认的两项开放问题（用户 `/grill-me` 保留）

1. **`preflight_scene.py` 的默认 `--ros-env` 策略**：当前脚本默认沿用 `production-gateway`（与正式部署网关一致，不静默切换），同时提供 `--ros-env cyclone` 和 `--ros-env fastdds-udp` 显式诊断参数。鉴于今日实测本机默认 Fast DDS 存在 `/dev/shm` SHM 卡死（`pubs=1, msgs=0`）而 `fastdds-udp` 与 `cyclone` 均可稳定 ~50 Hz 收包，请 Claude 确认：预检脚本是保持 `production-gateway` 作严格默认（真实暴露网关环境的 SHM 问题），还是默认切到 `fastdds-udp` / `cyclone`？
2. **F1-09 联机上线时 F1-4242 是否同步切换至 `robot-v23.json`**：HARNESS V53 已独立绑定不可变 `robot-v23.json`（`arm_l_j2: [−94.7, 120]`）；而 F1-4242（当前 V57）的 `robot-v22.json` 已带 `feedback_limit_recovery_deg: 1.0`（容忍反馈至 `−95.0°`），且 home 预设更新到 `−93.667°` 后仍在 `−94.0°` 内。请 Claude 确认：上线时是保持 F1-4242 沿用 `robot-v22.json` 不重启 4242（仅更新 `presets.json`），还是单列 F1-4242 发布将 4242 一并切换到 `robot-v23.json`？
