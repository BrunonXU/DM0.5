# H-43 验收证据：IK 墙钟上限站点配置与对准终点余量排序（2026-11-27）

**将 `harness_compute.py:168` 硬编码的 `7.` 改为读取站点配置 `spec['ik']['max_wall_s']`（缺省 `7.0`，上限 `NUMERICAL_RPC_TIMEOUT_S - 2 = 16.0`，F1-09 V61 设为 `15.0`），并在对准规划前增加仅针对四个变体终点的 Pinocchio DLS IK 最小关节限位余量排序（耗时约 15 ms），V60 Trial 1–2 原始输入均一次将 `v=1` 排第一并在首次尝试即解出可执行对准路径并通过正式 `revalidate_candidate` 复核。**

## 1. 改动摘要

- **独立控制提交（`6b04361`）**：仅改 `DAgger/F1/src/f1dagger/harness_compute.py`：
  - `HarnessNumerics.__init__` 读取 `spec['ik'].get('max_wall_s', 7.0)`，校验 `0 < max_wall_s <= NUMERICAL_RPC_TIMEOUT_S - 2`（即 `16.0`），非法则抛 `ValueError('invalid_ik_max_wall_s')`。
  - `solve_path` 第 168 行改为 `planning_deadline(scene, self.max_wall_s, 'bounded_IK_wall_time_exceeded')`，`planning_deadline` 逻辑完全不变。
  - `src/f1dagger/harness_compute.py` SHA256：`c6bc63c0cf3e43735f5a12db4bf7cf93c2d72cda6399f737cb512e3f7f2d45ea` → `3ea34037a0ee7c680ff958e81a82d206e6000d08a25b4d04c14166a95f16ea21`。
  - `control_manifest_sha256`：`8c0d2f850dec41017c8095eedc46edd7e481685649c5dd1595387afea712e861` → `a62cb69272e99fdf675b413ff01e0ef5b8a1443eb0f0018722a919e07ab33dc6`。
- **HARNESS 对准候选排序**：
  - `HARNESS/src/f1_harness/numerical_service.py` 与 `numerical_pool.py`：在 `ForwardKinematics` 新增只读 `endpoint_ik` RPC（Pinocchio 阻尼最小二乘，多起点，检查关节限位、规划包络及 `1.025*|j6|+|j7|<=110.5°` 腕部耦合约束，返回收敛状态与最小关节余量，不改动控制状态）。
  - `HARNESS/src/f1_harness/transport.py`（`rank_alignment_feeds`）与 `skill_services.py`：对 `align` 四个变体调用 `endpoint_ik`，按终点最小关节余量降序排列、终点 IK 失败者排最后；保留全部 4 个变体，记录排序耗时，超过 1000 ms 时记录告警但不阻塞。

## 2. 单元测试

`HARNESS/tests/test_align_ik_config_and_order.py` + `test_numerical_pool.py`：**15 passed**（覆盖缺省 7.0 s、15.0/16.0 s 合法、>16/≤0/bool/NaN 报错、调用方剩余预算仍优先收紧、排序只改顺序不删候选、终点失败排最后、超 1 s 告警不阻塞）。

## 3. V60 Trial 1–2 原始输入 6000D 离线回放（零硬件输出）

原始结果见 [v60-trial1-2-align-replay.json](v60-trial1-2-align-replay.json)。

| 试验 | 观测 ID | 排序耗时 | 排序后顺序 | `v=1` 排第一 | `v=1` 终点最小余量 | 尝试次数 | 总规划耗时 (6000D) | 路点数 / `arm_r_j6` 范围 | `revalidate_candidate` |
|---|---|---:|---|---|---:|---:|---:|---|---|
| V60 Trial 1 | `c50ebca5...:131` | 15.08 ms | `[1, 0, 2, 3]` | **是** | 42.86° | **1** (`v=1`) | 4.930 s | 21 / `[-6.68°, 10.60°]` | **通过** (21 段) |
| V60 Trial 2 | `078860e2...:117` | 14.87 ms | `[1, 0, 2, 3]` | **是** | 23.89° | **1** (`v=1`) | 3.861 s | 22 / `[-3.48°, 30.68°]` | **通过** (22 段) |

## 4. F1-09 ARM 实机离线零输出计时（V62 已部署环境，`ik.max_wall_s = 15.0`）

V62（`e9d6a8feb0f3`）部署到 F1-09 ARM（`ms-thor`，`aarch64`，14 核）后，直接在已安装的 V62 冻结包和站点配置（`ik.max_wall_s=15.0`、`arm_r_j6=[-60°, 50°]`、`budget_ms=25000`）上对 V60 Trial 1–2 的横移后现测输入与 H-42 运输到位缓存方孔输入执行了离线零输出 `plan_skill('align')` + `revalidate_plan` 计时（`physical_motion=false`，`start_sent=false`）。原始回执见 [f1-09-arm-offline-align-timing.json](f1-09-arm-offline-align-timing.json)。

| 场景 | 观测 ID | 方孔来源 | ARM 排序耗时 | 排序后顺序 | `v=1` 终点最小余量 | ARM 总规划耗时 | 路点数 / `arm_r_j6` 范围 | `revalidate_plan` | ≤ 15 s 门槛 |
|---|---|---|---:|---|---:|---:|---|---|:---:|
| V60 Trial 1（横移后） | `c50ebca5...:131` | 现测 | 28.37 ms | `[1, 0, 2, 3]` | 42.86° | **9.186 s** | 21 / `[-6.83°, 10.27°]` | **通过**（21 段，40.7 ms） | **通过** |
| V60 Trial 1（到位缓存直对准） | `c50ebca5...:110` | `cached_pre_transport_aperture` | 28.86 ms | `[1, 0, 2, 3]` | 42.62° | **7.868 s** | 18 / `[2.94°, 21.53°]` | **通过**（18 段，37.9 ms） | **通过** |
| V60 Trial 2（横移后） | `078860e2...:117` | 现测 | 28.85 ms | `[1, 0, 2, 3]` | 23.89° | **7.417 s** | 22 / `[-4.53°, 31.46°]` | **通过**（22 段，39.9 ms） | **通过** |
| V60 Trial 2（到位缓存直对准） | `078860e2...:101` | `cached_pre_transport_aperture` | 28.73 ms | `[1, 0, 2, 3]` | 24.37° | **5.664 s** | 18 / `[8.62°, 34.35°]` | **通过**（18 段，40.0 ms） | **通过** |

**结论**：4 组 F1-09 ARM 实机规划全部将 `v=1` 排第 1（排序仅 28.4–28.9 ms，远低于 1,000 ms 告警线），1 次尝试即解出并通过正式 `revalidate_plan`；横移后原输入在 ARM 实测 7.42–9.19 s（印证 V60 被旧 7.0 s 上限截断），而 H-42 到位缓存直对准在 ARM 仅需 **5.66–7.87 s**，全部远低于 15.0 s 门槛，可直接进入预检与真机试验。
