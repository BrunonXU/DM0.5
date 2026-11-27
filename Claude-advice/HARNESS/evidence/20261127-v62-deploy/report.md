# V62 上线核验与发布回执（含 SAM 服务同包切换与 ARM 离线计时）

## 摘要与结论

**结论：F1 Harness 缓存方孔与对准优化 V62（`e9d6a8feb0f3`，合入 H-42 + H-43 + H-44 + H-45）已完成“全流程回放通过”、“部署核验通过”及“F1-09 ARM 离线零输出对准计时通过（5.66–9.19 s ≤ 15 s）”，目前处于“真机待验”状态。6000D 协调器（`f1-harness-workspace-v62.service`）与 SAM 服务（`127.0.0.1:19842`）运行在同一 V62 冻结源码包下，F1-09 ARM 已同步安装更新后的控制与站点配置（`control_manifest_sha256 = a62cb69272e9...`，`ik.max_wall_s = 15.0`），4242 未重启并已显示 V62，全程 `start_sent=false`、`physical_motion=false`。**

**版本说明：** 候选 V61（`2dd3292b7a64`）在全量 pytest 验收中检出 `ForwardKinematics.__init__` 提前访问 `model.idx_qs` 导致 `test_numerical_service.py` / `test_numerical_rpc_budget.py` 出现 17 例假模型 setup 错误，按版本规则标记为 `cancelled` 不复用号码；将该索引解析改为延迟执行（`c92bff7`）后重新冻结为 **V62（`e9d6a8feb0f3`）**。

## 1. 三类验收结果

| 阶段 | 状态 | 关键证据 |
|---|---|---|
| **全流程零输出回放** | **通过** | 冻结视图全量 pytest **`3,195 passed, 70 skipped (native-only), 1 deselected, 0 failed, 0 errors`**（87.29 s）；`check_native_integration.py` **`62 passed`**；`check_project_layout.py` 通过；四套冻结零输出回放（接入 V62 同包独立 SAM 服务）全部通过：`normal`（10 阶段，64.66 s）、`grasp-miss`（12 阶段，73.35 s）、`aperture-occluded`（12 阶段，71.04 s）及第 4 条 **`direct-align-from-arrival`**（V60 Trial 2 `OBS:77→101`，`retained_static_fixture` + `cached_pre_transport_aperture`，缓存年龄 8.70 s，不横移直接解出 `v=1`，18 路点，`arm_r_j6 ∈ [8.62°, 34.36°]`，6000D 规划 3.147 s，`revalidate_plan` 18 段通过），全部 `checks_all_passed=True`、`audit_passed=True`、`physical_motion=False`、`paid_model_calls=0`。 |
| **部署核验** | **通过** | F1-09 ARM `/home/user/Bruno/HARNESS/development/e9d6a8feb0f3` 373 个冻结包文件 + `/home/user/Bruno/HARNESS/site/f1-llm-e9d6a8feb0f3` 13 个站点配置 SHA256 逐一校验通过；`control_manifest_sha256` 核对为 `a62cb69272e99fdf675b413ff01e0ef5b8a1443eb0f0018722a919e07ab33dc6`（`harness_compute.py` SHA256 = `3ea34037a0ee...`）；站点 `numerics.local.json` 已确认 `ik.max_wall_s = 15.0`、`arm_r_j6 = [-60°, 50°]`；`v62_imports_ok` 通过；6000D 协调器 `f1-harness-workspace-v62.service`（`pid=1640918`）与 SAM 服务（`pid=1640925`）同源运行于 `coordinator-e9d6a8feb0f3/src`（`sam_seed.py` SHA256 = `dfbbdc7b2467`，`mask_service.py` SHA256 = `03917dcedc10`，SAM2 `7402e0d864fa`，SAM3 种子 `9999e2341cee`）；4242（`pid=716917`，未重启，`robot-v22.json` 未改动）与 19843 均显示 V62；双路线（`astra` / `vla_astra`）10 组只读配置探针全过，`start_sent=False`。 |
| **F1-09 ARM 离线对准计时（H-43 门槛）** | **通过** | 在已部署 V62 的 F1-09 ARM（`ms-thor`，`aarch64`，14 核，`ik.max_wall_s=15.0`）上实测 4 组输入均将 `v=1` 排第 1（排序仅 28.4–28.9 ms）：V60 Trial 1 横移后 **9.186 s** / 到位缓存直对准 **7.868 s**；V60 Trial 2 横移后 **7.417 s** / 到位缓存直对准 **5.664 s**；全部远低于 15.0 s 门槛并通过 `revalidate_plan`（38–41 ms），`physical_motion=False`。详见 [H-43 ARM 计时报告](../20261127-H43-ik-wall-and-order/report.md#4-f1-09-arm-实机离线零输出计时v62-已部署环境ikmax_wall_s--150)。 |
| **真机待验** | **待执行** | 待操作者确认 F1-09 上电并复位至标准预设 **V6（`04a159f3c79c4b7a8ae9f9ffb06c7b4c`）**，执行连续 3 次 5/5 预检（如操作者明确要求跳过则如实登记）及 10 次正式真机试验。 |

## 2. 原始产物索引（6000D 本机）

- 冻结目录：`HARNESS/artifacts/cached-aperture-align-v62-20261127/`
- 四套回放汇总：`HARNESS/artifacts/cached-aperture-align-v62-20261127/replay-qualification-v62.json`
- 激活与后核验回执：`HARNESS/artifacts/cached-aperture-align-v62-20261127/activation.json`、`post-activation.json`
- ARM 离线对准计时原件：[f1-09-arm-offline-align-timing.json](../20261127-H43-ik-wall-and-order/f1-09-arm-offline-align-timing.json)
