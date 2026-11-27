# HARNESS 当前待办

**V57（`de4f6b0beaea`）已部署，前一版本是 V56（`d5b1b1d2de1a`）。H-33（V56 右腕 J6 ≤ 50° + 钉死 `fastdds-udp`）真机一次通过；H-32（V57 遮挡保留桶位姿）软件/回放/上线/3×预检全过，真机正式批次跑至第 6 次触发 H-34 3/3 阈值暂停，尚无真机放置成功。** [最新交接](../../Claude-advice/HARNESS/handoff/20261126-02-codex.md) · [V57 批次报告](../../Claude-advice/HARNESS/evidence/20261126-first-batch/report.md) · [H-33 真机摘要](../../Claude-advice/HARNESS/evidence/20261126-H33-j6-envelope/v56-real-robot-summary.json)

当前等待 Claude 审阅本轮 PR 并给出下一轮规格：先解决 H-34（已达 3/6 阈值：右腕清晰无遮挡木块因桶壁入镜报 `mask_uncertain`）与 S4 搬运躯干防碰撞/`joint_space_transport` 无有效目标问题，再重跑正式 10 次直立试验验证 H-32 遮挡让开与 S6 放置。[当前状态](STATUS.md) · [历史待办快照](history/20261118-todo.md)

## 现在做

- [ ] **H-34（已达 3/6 阈值，待 Claude 出规格）：右腕木块分割在桶壁入镜时连续报 `mask_uncertain_requires_new_prompt:object=square-1`。** 不降低分割置信度阈值，等待 Claude 裁定固定程序提示框/桶体屏蔽方案。
- [ ] **S4 搬运可达性排查（待 Claude 裁定）：桶在 `[0.650, -0.051]` m 时 4/4 次停在 `transfer_above_hole`（`arm_R_Link5/body_Link5` `-0.04~-0.88 mm` 且 `joint_space_transport` 报 `numerical_joint_search_no_valid_goal`）。**
- [ ] **H-32 真机验证补完（待上述阻塞解除后）：重新计正式 10 次直立试验，实测 S5 让开成功比、让开后测孔比与 S6 到达数。**[任务页操作](../docs/operations/DIRECT_ROLLOUT.md)
- [ ] **H-31（P1，第一批之后做）：修复 `support_lift` 首步下探。** 不改审计代码、不改 `1e-9` 与 `1.0 mm` 阈值，修完后 Replay A 必须审计全过。

## 单块全流程跑通后

- [ ] **固定左腕仍看不清时的主动观察。** 根据实际遮挡规划左臂视点，并验证双臂、被夹积木和桶的整段扫掠。用现场记录比较固定视角与主动视角得到的新测量。[现场持块视野](../../../Artifacts/F1-Harness/20261118-1042-119441/align-preview-left.jpg)
- [ ] **复核平滑后的下发轨迹。** 对照同一运行的规划参考和 Ruckig 输出，检查夹爪、持块和前臂经过桶沿的扫掠，比较弯段局部减速、换路各自的最小净距与耗时。[现场净距复算](../../../Artifacts/F1-Harness/20261118-1021-run2853/clearance.json)
- [ ] **单独比较运输速度。** 固定路径和同一次技能调用，在本地对照 40°/秒与 25°/秒，比较夹爪线速度、转弯加减速、净距和整段耗时。[参考时序对照](../../../Artifacts/F1-Harness/20261118-1021-run2853/reference-schedule-ablation.json)
- [ ] **验证自主扶正、抓走与经验复用。** 由 Astra 针对横躺积木探索策略，记录实物姿态变化、抓持及程序来源。在另一条独立运行验证并晋升，再用冻结程序复用。开发侧负责通用能力和证据，具体抓法留给实际探索。[程序与经验](../docs/ARCHITECTURE.md#程序与经验)

## 按条件推进

| 何时开始 | 工作与完成依据 |
|---|---|
| 正常任务积累可比样本 | 按原开发计划封存 5 条完整 rollout，分别报告自主完成、恢复、人工介入和失败。固定代码，改变位置或朝向；需要正式验收时冻结 20 个独立可达场景，按已有 18/20 自主成功目标报告结果。[原验收计划](history/20261118-todo.md#p2基线之后的对照与正式验收) |
| 现场证据指向感知、精度或制动缺口 | 用连续 RGB-D 和实测反馈区分遮挡、滑落、相机掉线和配对错误。精确插孔另核孔隙、TCP、在手偏移和独立姿态误差。停止问题用实际输出及下游反馈复核制动和停稳。[原子能力复盘](experiments/20261117-local-capabilities.md) |
| 独立 VLA 服务恢复，并安排混合路线验证 | 检查受控交权、外部持物证据、精放置和返程，保留原 owner 的停止与实测回执。[混合路线复盘](experiments/20261111-vla-astra.md) |
| 单块、经验和布局对照有独立结果 | 再扩展多块、堆叠与同类遮挡。BC／RLT 继续由[原任务待办](../../tasks/F1_ComplexyToys/TODO.md)管理。 |

## 旧项待复核

设备时间戳与右腕 USB、求解或模型连接长尾、预热与服务恢复等旧条目，保留原输入、当时版本及处理决定。遇到同类现象时先对照当前冻结版本和复盘，仍能复现再列为当期行动。用户暂缓的 Fast 服务档位对照待明确重启。[完整历史清单](history/20261118-todo.md) · [实验索引](experiments/README.md)
