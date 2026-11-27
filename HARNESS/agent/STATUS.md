# F1 Harness 当前状态

**F1 Harness 同戳帧保留与固定程序入口 V54**（`8818976408ce`）已部署，前一版本是 V53。4242 仍用 `robot-v22.json`，没有重启。V54 中期批 3 次固定程序基线真机试验分别到达 S4、S0、S5，全程 `model_calls = 0`、每次均 `measured_stationary` 停稳，尚无放置成功，现已暂停并转入第四轮审阅（H-35 → H-33 → H-32）。[第四轮审阅](../../Claude-advice/HARNESS/review-round4.md) · [中期批证据](../../Claude-advice/HARNESS/evidence/20261126-first-batch-interim/report.md)

H-20 的 30 轮已经跑完：配对检查三路 10/10，五项全过 8/10、8/10、7/10（未过的 7 轮均为重启后桶内散块遮挡第 4 项，清桶后连续 3 次预检五项全过）。本批沿用默认环境（production-gateway）；按判定规则胜出的是 fastdds-udp，随下一个版本钉死。左腕线缆已在 2026-11-26 21:40（北京时间）确认紧固。

下一步按第四轮审阅顺序一次只做一条：先完成 H-35 换分支补证据与漏斗修正，再实施 H-33（右腕第 6 关节规划上限收紧到 `50.0°` 并随同版本钉死 `fastdds-udp`），验收后再实施 H-32（持块遮挡头部时保留 20 秒内实测桶位姿以完成让开动作），随后在新版本重新计 10 次直立试验。H-27 的 1.037 µm 下探豁免范围仅限第一批，后续记在 H-31。[第三轮审阅](../../Claude-advice/HARNESS/review-v53-round3.md) · [H-20 重跑](../../Claude-advice/HARNESS/evidence/20261126-H20-dds-ab-rerun/report.md) · [H-27 报告](../../Claude-advice/HARNESS/evidence/20261126-H27-v5-preset/report.md)

用户已确认拆掉的是左侧夹爪且未碰到左腕相机支架，碰撞模型保守保留原夹爪包络（`arm_tip_extension=0.10m`）。Cyclone 与 Fast DDS 的 `/hal/joint_states` 实测对照表明：控制端（Cyclone）经 RTPS/UDPv4 接收 `motor_ctl`（Fast DDS）反馈，早期 0 包属跨 RMW 短窗发现偶发漏配，而本机默认 Fast DDS 存在 `/dev/shm` SHM 卡死风险（切 UDPv4-only 即恢复 ~50 Hz）。11 月 25 日已记录的两次 V52 任务均停在 S0，技能动作首发为零，收尾均实测静止。[现场复盘](../../Claude-advice/HARNESS/evidence/20261125-B-first-run/report.md) · [部署回执](../../Claude-advice/HARNESS/evidence/20261125-B-deployment-V52/deployment.json)

现场采用自然随机摆放的直立方块，按运行编号记录 S0–S9，场景编号可空。首次无人介入且有同请求放置回执计 M0，避桶复位另记 S9。[发布登记](../releases/registry.json)

## 已验证的范围

| 层级 | 证据支持的结论 |
|---|---|
| V54 冻结回放、上线、H-20 重跑与中期批 3 次试验（在用） | 已部署至 ARM 与 6000D（4242 显示 V54，未重启 4242，保持 `robot-v22.json`，`arm_l_j2` 下限收回 `−94.0°`）。离线 3,190 例 pytest、62/62 原生集成、37/37 4242 关节合同、12/12 DAgger 边界及 3+1 条零输出回放全过。H-20 补满 30 轮：配对检查三路 10/10，本批沿用默认环境（production-gateway）；按判定规则胜出的是 fastdds-udp，随下一个版本钉死。清桶后连续 3 次预检五项全过；固定程序中期批 3 次真机试验分别到达 S4（`arm_r_j6` 实测在 `54.86°` 停住，来源未确认）、S0（右腕分割不确定）、S5（右臂遮挡头部导致桶位姿清零、侧移预览被拒），全程 `model_calls=0`、均实测静止，尚无放置成功。H-27 的 1.037 µm 下探只豁免第一批。[中期批报告](../../Claude-advice/HARNESS/evidence/20261126-first-batch-interim/report.md) · [H-26 报告](../../Claude-advice/HARNESS/evidence/20261126-H26-stamp-hold/report.md) · [H-20 重跑](../../Claude-advice/HARNESS/evidence/20261126-H20-dds-ab-rerun/report.md) · [H-27 报告](../../Claude-advice/HARNESS/evidence/20261126-H27-v5-preset/report.md) |
| V53 冻结回放、上线与现场实测 | 前一版本，已被 V54 替代。当时的 H-20 初测是配对 6/10、3/10、1/10。左腕 5 分钟采样零断连；接口在 V53 当时尚未紧固，已在 V54 期间确认紧固。[V53 验收](../../Claude-advice/HARNESS/evidence/20261125-V53-left-baseline-preflight/report.md) · [H-20 初测](../../Claude-advice/HARNESS/evidence/20261126-H20-dds-ab/report.md) |
| V52 软件、回放与部署 | 正式任务最多可做六次只读补验，运输规划可选取孔口上方通过扫掠检查的偏移终点。3,173 项软件用例及另计的 62 项原生集成用例通过。正常、抓空恢复、孔口遮挡三条回放均取得同一请求的放置回执并退回，硬件输出为零。[统一验收](../../Claude-advice/HARNESS/evidence/20261125-B-batch/report.md) |
| V51 冻结回放与 ARM 只读 | 3,155 项软件用例通过。正常、抓空后重抓及遮孔后侧移分别走完 10／12／12 步，硬件发布为零。真机空爪采样 32 组，31 组形成场景，左腕在这次采样中为 0 帧。精确对孔与放置待现场验证。[验收证据](../../Claude-advice/HARNESS/evidence/20261125-B-deployment-V51/report.md) |
| V50 实录与模拟回放 | V49 直立真机运到桶上方，侧移后测到三维方孔。V49 现场第 149 帧能解出 24 路点的对准候选，第 152 帧缺孔时保留原候选。独立模拟场景在内存中首发，实物放置待验。[现场与回放对照](experiments/20261117-local-capabilities.md#v50-冻结基线与现场证据) |
| V42 上线前失败回放与验收 | 该次工作台为 F1infra V56，前一个实际部署版本为 V40，V41 在冻结导入检查时取消。V40 的首动因新增左腕图受阻。原候选 3 路点、76 帧内存平滑输出、140 次细分检查通过。正常 10 阶段与抓空恢复 14 阶段完整零硬件输出回放通过。ARM 新采 28 组中 25 组跨视角重建方孔。经验程序在撤权时保留控制器真实回执及发布状态。当次上线时现场动作和完整抓放待验证。[V40 现场与 V42 回放](experiments/20261117-local-capabilities.md#v40-首动前相机复核与现场耗时) |
| V40 现场采样与回放 | 桶移动后，原先失效的 19 组完整输入全部重建方孔；新采 30 组中 22 组重建，8 组缺少新鲜同步输入。两组真实遮挡画面均找回方孔，结合同次运行其他时刻的持物证据后可求解当前对准。正常 10 阶段和抓空恢复 14 阶段完整模拟均通过；部署未发起动作。[现场逐组对照](../artifacts/relative-aperture-v40-20261118/field-regression.json) · [完整回放](../artifacts/relative-aperture-v40-20261118/full-validation-pinned/validation-summary.json) |
| V38 真机记录 | 11 月 18 日 10:21 的记录包含接近、抓取和向桶运输三段实测关节运动。运输净距来自固定场景下的几何复算，现场是否接触需独立证据。[运动窗口](../../../Artifacts/F1-Harness/20261118-1021-run2853/motion-window-estimates.json) · [净距复算及限制](../../../Artifacts/F1-Harness/20261118-1021-run2853/clearance.json) |
| V38 现场数据回放 | 使用 V36 的已记录反馈，补定位后接回同一候选源码，三步中只执行一次抓取。运输末路点后 22 条观测中，18 条确认持物，4 条证据不足。精确对孔仍需当前孔口几何。[经验接续与遮挡复核](experiments/20261117-local-capabilities.md#v38-经验接续与遮挡复核) |
| V38 冻结验收与现场只读采样 | 正常和抓空恢复两条全流程零硬件输出回放通过。动作链使用头部与右腕 RGB-D。机器人端 27 组只读采样中，17 组返回局部场景，10 组需要新的右腕积木提示。左腕用于录像和观察，静止样本可见方孔和三角孔。[冻结验收](../artifacts/anchor-recovery-v38-20261118/release-validation.json) · [左腕静止样本](../artifacts/anchor-recovery-v38-20261118/field-images/manual-left-1794967058960051907/left_wrist-apertures.png) |

按版本追查请看[实验索引](experiments/README.md)。整理前各阶段的状态与原证据见[截至 V38 上线的状态页归档](history/20261118-status.md)。
