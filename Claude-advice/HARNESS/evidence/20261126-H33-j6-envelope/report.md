# H-33 执行报告：右腕第 6 关节规划上限收紧至 50.0° 与固定传输 fastdds-udp（V56 候选，软件完成，待真机验收）

**状态：软件完成，待真机验收**（用户因离场关闭 F1-09，按指令跳过需上电的真机与现场目视步骤，待下次上电部署 V56 后补齐预检 3× 与 ≤3 次真机试验）。

- **版本与冻结身份**：**V56 候选**（`F1 Harness 右腕规划收紧与固定传输 V56`，`bundle_id`: `d5b1b1d2de1ae687f0d2501fcbc33404d359edc03d0d1267515406cdd1877483`，状态 `candidate`；当前已部署在用版本仍为 **V54**）。注：首个候选 V55（`0e8235c53896`）在全量回归中被发现 `PlanService` 缺少类默认属性 `planning_joint_envelope = {}`（影响 `PlanService.__new__` 单测），已按发布登记规则标为 `cancelled`（未部署），重冻为 V56。
- **净增 `raise HarnessError`**：**+1**（`planning_joint_envelope_exceeded`，属原生关节限位类硬保护收紧，按规则单列；不放宽任何限位配置、机器人档案、驱动或堵转判据）。

---

## 一、代码与配置改动

1. **`HARNESS/src/f1_harness/skill_services.py`**：
   - 将 `planning_joint_envelope_exceeded` 加入 `CANDIDATE_FAILURES`。
   - 新增 `validate_planning_joint_envelope`（用 `f1dagger.contract.ARM_NAMES` 映射 14 个手臂关节，不手写下标），在 `PlanService.__init__` 校验配置合法性，并支持针对 `numerics.local.json` `spec.gate.limits` 校验不放宽。
   - 在 `_solve_route` 中 `solve_path` 返回后、并入 `targets` 前逐行校验所有路点（含 `joint_search=True` 关节空间搜索结果），越界即作废该候选并在诊断中写入 `joint`、`joint_index`、`waypoint_index`、`target_deg`、`envelope_deg`。
2. **`HARNESS/src/f1_harness/joint_motion.py`**：将 `planning_joint_envelope_exceeded` 加入 `SEARCHABLE_FAILURES`。
3. **`HARNESS/src/f1_harness/owner_bridge.py`**：在 `numerical_failure_detail` 指标白名单放行 `joint`、`joint_index`、`waypoint_index`、`target_deg`、`envelope_deg`。
4. **`HARNESS/src/f1_harness/development.py`**：在站点校验 `validate_development` 中核对 `plan.local.json` 的 `planning_joint_envelope_deg` 严格落在 `numerics.local.json` `spec.gate.limits` 内，超出即拒绝。
5. **V56 站点 `plan.local.json`**：`fixture.planning_joint_envelope_deg = {"arm_r_j6": [-60.0, 50.0]}`（冻结脚本校验严格落在 `[-60.0, 60.0]` 内）。
6. **H-20 步骤 B（随同版本钉死 `fastdds-udp`）**：`scripts/start_development.py` 的 `ROBOT_SERVICES_ROS_ENV = 'fastdds-udp'`、`scripts/preflight_scene.py` 的 `PRODUCTION_GATEWAY_ROS_ENV = 'fastdds-udp'`。

---

## 二、离线重算：Trial #1 与 Trial #3 运输请求（`offline-replan-trial01-trial03.json`）

使用 V54 实际录制 `harness_planning_started` 原始输入与真实 Pinocchio + CollisionGate + OMPL（`ompl==2.0.1`）数值服务离线重算：

| 试验 | 配置 | 是否规划成功 | 选中候选与路线 | `arm_r_j6` 最大值 / 终点 (deg) | 规划耗时 (s) | 备注 |
|---|---|---|---|---:|---:|---|
| Trial #1 (`web-dbe926ed`) | 不设范围 | 是 | variant 1 (`orientation_preserving_transport`, 11 路点) | **60.00 / 59.8686** | 3.2041 | 精确复现真机 `59.8686°` 目标 |
| Trial #1 (`web-dbe926ed`) | `[-60.0, 50.0]` | **是** | variant 2 (`joint_space_transport`, 10 路点) | **22.3917 / 19.4233** | 7.5667 | variant 1 因 `52.46° > 50.0°` 被 `planning_joint_envelope_exceeded` 作废，OMPL 0.46 s 找到解，远低于 50.0° |
| Trial #3 (`web-5653aac6`) | 不设范围 | 是 | variant 2 (`joint_space_transport`, 9 路点) | 17.8917 / 16.9705 | 4.9074 | 原方案 |
| Trial #3 (`web-5653aac6`) | `[-60.0, 50.0]` | **是** | variant 2 (`joint_space_transport`, 10 路点) | 25.3012 / 16.9705 | 5.0748 | 终点 `16.9705°` 不受影响，全程低于 50.0° |

---

## 三、四项只读确认结果（`readonly-j6-diagnosis.json`）

全程未发送任何试探动作；结论标为 **“来源未确认”**，**`50.0°` 上限保持不变**：

| 检查项 | 状态 | 实测结论 |
|---|---|---|
| 1. 读 `ccs_m6_40.MvKDCfg` | 已读 6000D 本地两份同哈希副本（ARM 在线文件因关机待补核） | 左右臂 J6（第 7/20 行）配置均为 `[-60.0, 60.0]` deg，非 55° 配置截断 |
| 2. Trial #1 堵转前后 3 s `effort`/`motor_current`/`error_code` | 原始持久化日志不含该三字段 | `observations.jsonl`/`control.jsonl`/`events.jsonl` 仅记录 `state16`/`full22` 角度，未落盘电流与错误码 |
| 3. 翻历史统计 `arm_r_j6` 实测极值 | 已完成（扫描 174 源 / 157 份非空 HARNESS 与 DAgger 录制） | 全局最小 **`-16.2075°`**（`0` 条 `<-52°`，负向无平台）；全局最大 **`55.5841°`**（`0` 条 `≥56°`）；`>52°` 共 14,663 采样，其中 **`[54.5, 55.0)` 占 11,838 条（80.7%）**、`[55.0, 55.5)` 占 2,200 条（15.0%） |
| 4. 静止目视右腕干涉 | 因关机暂缓 | 待下次机器人上电静止时请操作者目视确认并记录结论 |

---

## 四、软件与零输出冻结回放验收（`software-qualification.json`、`replay-qualification-v56.json`）

- **专项单测**：`test_planning_joint_envelope.py` + `test_preflight_scene.py` 12/12 通过。
- **全量 pytest（V56 `snapshot-test-view`）**：`3,124 passed`（70 skipped 由原生数值环境运行，`*_native.py` 另跑 65 passed / 80 subtests passed；仅 1 例 `test_bundle_owns_control_and_imports_without_old_source` 为冻结测试视图软链既有现象，与 V54 一致）。
- **原生跨工程集成（`check_native_integration.py`）**：**62/62 passed**。
- **三条 V56 冻结零输出回放与审计（含 `planning_joint_envelope_deg = {"arm_r_j6": [-60.0, 50.0]}`）**：
  - `normal`：`success`（10 阶段，`checks_all_passed=True`，`audit_passed=True`，`physical_motion=False`，`paid_model_calls=0`，66.08 s）
  - `grasp-miss`：`success`（12 阶段，`checks_all_passed=True`，`audit_passed=True`，`physical_motion=False`，`paid_model_calls=0`，72.84 s）
  - `aperture-occluded`：`success`（12 阶段，`checks_all_passed=True`，`audit_passed=True`，`physical_motion=False`，`paid_model_calls=0`，68.83 s）
- **待真机验收**：上线部署 V56（核对 4242 后改 `deployed`）、`fastdds-udp` 连续 3 次预检五项全过、最多 3 次真机试验（桶摆在 Trial #1 位置 `[0.670, -0.043]` m 附近，有一次到 S5 且 `arm_r_j6 ≤ 50.0°` 无堵转即通过并停下，同时并列统计 `camera_sample_feedback_aged:right` 次数与静止目视检查）。
