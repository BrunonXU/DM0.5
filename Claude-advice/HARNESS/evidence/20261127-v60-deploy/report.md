# V60 上线核验与发布回执（含 SAM 服务同包切换）

## 摘要与结论

**结论：V60（`cabff256328d`，合入 H-36 + H-39 + H-40）已完成“全流程回放通过”与“部署核验通过”，处于“真机待验”状态。协调器（`f1-harness-workspace-v60.service`）与 SAM 服务（`127.0.0.1:19842`）均运行在同一 V60 冻结源码包下，4242 未重启并已显示 V60，全程 `start_sent=false`、`physical_motion=false`。**

**版本说明：** 候选 V59（`c9ca71437c76`）在全量 3,169 例 pytest 中检出 `test_measured_head` 的 1 处边界（`object.__new__` 未初始化 `measured_fixture` 时异常分支取属性报错），按版本规则标记为 `cancelled` 不复用号码；修复该 1 行（`8722e59`）后重新冻结为 **V60（`cabff256328d`）**。

## 1. 三类验收结果

| 阶段 | 状态 | 关键证据 |
|---|---|---|
| **全流程零输出回放** | **通过** | 全量 pytest **`3,168 passed, 70 skipped, 0 failed`**；`check_native_integration.py` **`62 passed`**；`check_project_layout.py` 一致；三套冻结零输出回放（接入 V60 同包独立 SAM 服务）全部通过：`normal`（10 阶段，64.87 s）、`grasp-miss`（12 阶段，72.73 s）、`aperture-occluded`（12 阶段，69.49 s），全部 `checks_all_passed=True`、`audit_passed=True`、`physical_motion=False`、`paid_model_calls=0`。 |
| **部署核验** | **通过** | F1-09 ARM 373 个冻结包文件 + 13 个 site 配置 SHA256 逐一校验通过；`v60_imports_ok` 通过（`SEMANTIC_CANDIDATE_SUMMARY_LIMIT=64`、`ApertureViewpoint._reset_fallback`、`CalibratedScenePipeline._check_occluded_fixture_retention`、`OCCLUDED_FIXTURE_RETAIN_NS=20s`、`JOINT_TRANSPORT_YAW_OFFSETS_DEG=(0,90,180,270)`）；协调器 `f1-harness-workspace-v60.service`（`pid=1157999`）与 SAM 服务（`pid=1158006`）同源运行于 `coordinator-cabff256328d/src`，`sam_seed.py` SHA256 = `dfbbdc7b2467`，SAM2 模型 `7402e0d864fa`、SAM3 种子模型 `9999e2341cee`；4242（`pid=716917`，未重启，`robot-v22.json`）与 19843 均显示 V60；双路线（`astra` / `vla_astra`）10 组只读配置探针全过，`start_sent=False`。 |
| **真机待验** | **待执行** | 需操作者确认真机就绪并复位至标准预设 **V6（`04a159f3c79c4b7a8ae9f9ffb06c7b4c`）**，执行连续 3 次 5/5 预检及 10 次正式真机试验。 |
