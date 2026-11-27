# F1 Harness 第八轮审阅：V62 卡在放置前的一处误记，今天集中打通放置

- 审阅基线：`harness/round8` 头提交 `6d47dd2`（`1da83b1` 至 `6d47dd2` 共 9 个提交），已作为 PR #17 用 merge commit 合并到 `main`（`505b0a2`）。
- 回复：[20261127-06-codex.md](handoff/20261127-06-codex.md)。本轮指令在 [20261127-07-claude.md](handoff/20261127-07-claude.md)。
- 方式：只读代码与证据，未执行。新增 H-46 至 H-50。

**V62 离放置只差一行代码。** `owner_bridge.py:1399-1400` 把运动途中"还没到达路点"的正常轮询结果记成了接触故障，于是到位时缓存的方孔和保留的桶位姿一起作废了。根源在我上一轮的规格：我写的作废条件是"路点跟踪检查失败"，本意是"技能以跟踪失败结束"，这种情况 `engine.py:586` 已经会记录；但这句话字面上也可以理解为每一次轮询检查。这一处由我负责。删掉这两行之后，离线回放 V62 Trial 1 和 2 都能用缓存的方孔直接解出对准（3.0–3.1 秒），并通过复核。

**按用户要求，这一轮只做放置。** 除了修这两行，我还沿着代码把放置的后半段走了一遍，又找到一个必然会卡住的地方：程序的顺序是"松爪 → 确认放置 → 撤离"（`recovery_programs.py:457`）。确认放置时手臂还停在桶上方挡着头部，而确认放置按规定不能用缓存的方孔，所以一定停在 S8。V63 改为先撤离、再确认。

**上真机之前，先离线把对准、插入、松爪、撤离、确认这一整段零输出走一遍。** 今天每一次真机试验都很宝贵，不能再靠真机去发现下一处代码问题。

```mermaid
flowchart LR
  A[运输到位<br/>缓存方孔有效] --> B[S6 对准] --> C[S7 插入<br/>测到已插入] --> D[S8 松爪] --> E[撤离<br/>改到确认之前] --> F[头部重新测量<br/>确认放置]
```

---

# 第一部分：给人看

## 一、PR #17 审阅：通过，已合并

| 内容 | 结论 |
|---|---|
| H-42 缓存方孔 | 缓存、作废条件、`verify_placed` 拒绝缓存都与规格一致；唯一的问题就是上面那两行误记（H-46 修复） |
| H-43 | `max_wall_s` 由站点配置读取，缺省 7.0、上限 16，`planning_deadline` 没改；`DAgger/F1/src/f1dagger/harness_compute.py` 与 6000D 上的现行文件一致，改动只在这一处。ARM 实测对准规划 5.7–9.2 秒，在 15 秒以内 |
| H-44、H-45 | 符合规格，离线验证通过 |
| V61 取消、V62 上线 | 符合版本规则；`registry.json` 历史条目没有改动 |
| 预检跳过、批次停在 4 次 | 都是操作者当轮的明确要求，Codex 如实登记了 |

## 二、三个问题的裁定

| 问题 | 裁定 | 理由 |
|---|---|---|
| 1. 运动途中的轮询被误记为接触故障 | **选 A：删除 `owner_bridge.py:1399-1400`（H-46）** | 真正的接触迹象已有三处记录：堵转 `engine.py:761`、已发布动作没有成功结束 `engine.py:586`、撤权或停止 `engine.py:851`。轮询"还没到"不是接触 |
| 2. 运输规划在 ARM 上用了 17–31 秒 | **选 B：除对准以外的技能，传给数值服务的预算仍为 7 秒；对准保持 15 秒（H-49）** | 这等于让运输恢复到 V60 时的行为，那时已经在真机上验证过；不改 DAgger，不改 `budget_ms` |
| 3. 斜摆积木抓取时右腕截断 | **选 A：进 backlog（H-50）。今天的试验积木正放、边与桌边平行** | 用户明确要求先打通放置 |

## 三、"打通一条"按什么算

- **今天的目标**：一次试验里，对准成功、插入成功（测到已插入）、松爪成功，并且操作者确认积木确实进了桶。
- 撤离后由头部重新测量确认放置。积木掉进桶里、头部看不到它时，确认会停在"待确认"（`object_not_observed`）。这时如实记为"物理放置成功（人工确认），系统确认待补"，不能写成系统成功。让系统能自己确认"积木已经掉进桶里"，下一轮根据今天撤离后的头部图像再定（H-48 第 3 项）。

---

# 第二部分：给 Codex 的执行规格

## 0. 通用约束

沿用 [review-round7.md 第 0 节](review-round7.md#0-通用约束)。本轮追加：

1. 顺序：H-46 → H-49 → H-48 → H-47 离线走完整段 → 冻结 V63 → 真机。H-50 不做。
2. 离线走整段时如果发现新的阻塞：属于 H-42 提交 `cfd539e` 里与 review-round7.md 不符的缺陷，像 H-46 这样的，可以直接修，单独提交并在交接里列出来；其他任何阻塞，**立即停下写交接**，不要带着已知会卡住的版本上真机。
3. 不放宽：碰撞余量和碰撞模型、关节限位、H-33 的 50°、所有感知阈值、插入判定的容差（`xy_tolerance_m`、`insertion_depth_m`、`yaw_tolerance_rad`、`tilt_tolerance_rad`）、`verify_placed` 不得使用缓存。

## H-46 `[x]` P0：删除运动途中轮询的接触误记

- 删除 `owner_bridge.py:1399-1400` 的 `record_contact_fault('waypoint_tracking_failed', ...)`。其他三处记录不变。
- 测试：轮询先返回 `accepted=False`、再返回 `True` 并成功到达时，不记录接触故障，缓存和保留都有效；`motion_stalled` 会记录；已发布动作以非成功结束时会记录；撤权会记录。
- 用 V62 Trial 1–2 的原始输入从运输到位接续回放：缓存方孔有效，`align` 规划成功并通过复核。
- **执行记录（2026-11-27，Codex，`b5c4922`）**：已完成。删除 `owner_bridge.py:1399-1400`，补齐 2 例测试（44/44 通过，原生集成 62/62 通过）；V62 Trial 1–2 回放 `align` `v=1` 分别 3.064 s / 3.003 s 通过复核与 `F1Owner`/Ruckig 审计。详见 [H-46/H-49 报告](evidence/20261127-H46-H49-fixes/report.md)。

## H-49 `[x]` P1：除对准外，IK 预算仍为 7 秒

- 在 `skill_services.py` 调用 `solve_path` 时，对 `align` 以外的技能，在 `scene` 中传 `planning_budget_s = min(剩余预算, 7.0)`；`align` 不变（由站点 `max_wall_s = 15.0` 和剩余预算决定）。不改 DAgger。
- 测试：`transfer_above_hole` 拿到的预算 ≤ 7.0；`align` 拿到的预算可以超过 7.0。
- 用 V62 Trial 1–2 的运输输入在 6000D 上重算，报告耗时和选中的路线。
- **执行记录（2026-11-27，Codex，`4f73665`）**：已完成。未改 DAgger；新增单测 16/16 通过；V62 Trial 1–2 运输输入在站点 `max_wall_s=15.0` 下 6000D 重算耗时分别为 **9.702 s**（原 12.205 s）与 **6.632 s**，均命中 `variant=2`（`joint_space_transport`）。详见 [H-46/H-49 报告](evidence/20261127-H46-H49-fixes/report.md)。

## H-48 `[!]` P0：先撤离，再确认放置

1. `recovery_programs.py` 的 `place_held_block`：当 `withdraw=True` 时，顺序改为 `release → withdraw_reset → verify_placed`。`withdraw=False` 时保持原顺序。`verify_placed` 的判据不变，仍然必须有当前帧直接测到的方孔。
2. `withdraw_reset` 失败时，照原逻辑返回并停下，不再做 `verify_placed`。
3. 撤离后每次 `verify_placed` 的观测都保存一张头部图像裁剪（桶盖附近，≤300 KB），放进证据目录。`verify_placed` 结果为 `object_not_observed` 或 `placement_evidence_incomplete` 时，在漏斗里单列为"放置待确认"。这些图像是下一轮设计"积木掉进桶里"确认方式的依据。
4. 测试：新顺序生效；`withdraw=False` 时保持原顺序；撤离失败时不做确认；确认仍然拒绝缓存的方孔。
- **执行记录（2026-11-27，Codex，阻塞）**：H-47 离线实证显示调整顺序直接触碰 4 处生产硬阻塞——`release` 遮挡态运动后验证被拒（`observation.py:309,323`）、`engine.py:322` 硬编码 `withdraw_reset` 前置 `verify_placed`、`scene_pipeline.py:890` + `return_observation.py:10,66-84` 要求 `placement_verified=True` 且头部未遮挡、`remote.py:826`/`adapters.py:589` 在 `withdraw_reset` 成功即置 `_solved=True`，另叠加 `return_scene.py:86` 真实 IPC `ndarray` 崩溃。按第 0 节止损规则停下交接，待 Claude 在 [20261127-08-codex.md](handoff/20261127-08-codex.md) 裁定后一次改净。

## H-47 `[x]` P0：上真机前，离线零输出走完放置整段

- 输入：V62 Trial 1–2、V60 Trial 1–2 的原始记录，从运输到位接续。
- 用 `scripts/replay_full_owner.py` 的 `approved-output`（真实 F1Owner／Ruckig 输出，接收端只记录、不向硬件输出），模拟执行反馈：积木随手运动，插入时随手下降。依次走 `align → insert → release → withdraw_reset → verify_placed`。
- 每一段报告：是否规划成功、选中的候选、规划耗时、复核是否通过、实际输出审计是否通过、技能结果和原因码；以及插入后是否得到 `inserted=True`，缓存方孔的年龄是否始终在 45 秒以内。
- 用模拟反馈得到的 `verify_placed` 结果只能说明流程接得上，不能说明物理上成功，报告里要标明。
- 全量 pytest、`check_native_integration.py`、三条标准零输出回放，加上这四条整段回放。
- **执行记录（2026-11-27，Codex）**：已完成四条真实记录离线零输出整段回放（模拟执行反馈，已标明），实证截获 4 处非 H-42 硬阻塞（B1 `insert` 4/4 因 `success_posture_prior` 低仰角抓姿在真实抓深波动下 `box0_z = -0.7..-1.3 mm` 撞桶壁/盖边；B2 `release` 遮挡验证 4/4 被拒；B3 `withdraw_reset` 提前触碰 4 处合同；B4 `return_scene.py:86` 真实 IPC `ndarray` 崩溃）。按第 0 节规则立即停下写交接，未上真机。详见 [H-47 报告](evidence/20261127-H47-placement-dry-run/report.md) 与 [20261127-08-codex.md](handoff/20261127-08-codex.md)。

## H-50 `[ ]` P2：斜摆积木抓取（backlog）

- V62 Trial 3–4：积木偏航约 −36° 时，`approach_grasp` 到位后右腕画面截断了积木（`mask_truncated`），右臂又挡住了头部，S1 超时。
- 放置跑通之后再开规格。今天的试验积木正放，边与桌边平行。

## V63 真机（今天）

1. 冻结 V63（H-46、H-48、H-49；这几处都不改 `control`，`control_manifest_sha256` 与 V62 相同，发布回执里核对），部署，核对 4242。ARM 上的对准计时 V62 已经通过，不用重做。
2. 预检连续 3 次（操作者要求跳过时照办并登记）。
3. 摆位：桶在 x = 0.66–0.76 m、不进右腕画面；积木正放、边与桌边平行，位置在右臂可达范围内随意。不安排近身桶位和斜摆。
4. 一直跑到第一次"打通一条"（第一部分第三节的定义），然后再跑最多 5 次看能否重复；操作者可以随时叫停。同一阶段、同一原因连续 3 次就停，安全事件立即停。
5. 每次报告：最远阶段；缓存方孔的使用、年龄和作废原因；对准、插入、松爪、撤离、确认各自的结果和原因码；对准在 ARM 上的耗时；插入时是否测到已插入；操作者看到的积木最终位置；`verify_placed` 的结果。

## 版本登记

H-46、H-48、H-49 合为 V63（目前最大 62）。先登记 `candidate`，上线核对后改为 `deployed`。历史条目不改。
