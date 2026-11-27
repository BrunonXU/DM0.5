# F1 Harness 当前状态

**F1 Harness 缓存方孔与对准优化 V62**（`e9d6a8feb0f3`，合入 H-42 + H-43 + H-44 + H-45；V61 `2dd3292b7a64` 因 `ForwardKinematics.__init__` 假模型单测边界取消未复用，前一在用版本 V60 `cabff256328d`）已完成全部离线验收（3,195 例 pytest、62 例原生集成、四套冻结零输出回放含到位缓存直对准）并部署至 6000D（`f1-harness-workspace-v62.service` + 同包 SAM 服务）与 F1-09 ARM（`control_manifest_sha256 = a62cb69272e9...`，`ik.max_wall_s = 15.0`，4242 未重启仍用 `robot-v22.json` 并已显示 V62，全程 `start_sent=false`）。**H-43 机载 ARM 离线零输出对准计时门槛已通过**：在已部署 V62 的 F1-09 ARM（`ms-thor`，`aarch64`）上实测 V60 Trial 1–2 四组输入全部一次将 `v=1` 排第 1（排序 28.4–28.9 ms），横移后规划 **9.186 s / 7.417 s**、H-42 运输到位缓存方孔直对准规划仅 **7.868 s / 5.664 s**（全部远低于 15 s 门槛并通过 `revalidate_plan`）。[V62 上线回执](../../Claude-advice/HARNESS/evidence/20261127-v62-deploy/report.md) · [H-42 验收](../../Claude-advice/HARNESS/evidence/20261127-H42-cached-aperture/report.md) · [H-43 验收与 ARM 计时](../../Claude-advice/HARNESS/evidence/20261127-H43-ik-wall-and-order/report.md) · [H-44 验收](../../Claude-advice/HARNESS/evidence/20261127-H44-scene-race/report.md) · [H-45 验收](../../Claude-advice/HARNESS/evidence/20261127-H45-far-field/report.md) · [第七轮审阅](../../Claude-advice/HARNESS/review-round7.md)

`fastdds-udp` 已自 V56 起钉死为生产网关与预检默认环境，累计 14 次真机试验（V56 2 + V57 6 + V58 3 + V60 3，其中 V60 3 次首次连续两次到达 S6 `align`）全程 **0 次 `motion_stalled`**、**0 次 `camera_sample_feedback_aged:right`**、尚无放置成功。标准静态复位预设为 **V6（`04a159f3c79c4b7a8ae9f9ffb06c7b4c`）**（H-41，H-38 静态部分已关闭）。[V60 批次报告](../../Claude-advice/HARNESS/evidence/20261127-v60-batch/report.md)

下一步：请人类确认 F1-09 已上电并复位至标准预设 **V6**，完成连续 3 次 5/5 预检（如明确跳过则如实登记）后启动 **V62 正式 10 次真机批次**（3 次桶入右腕、2 次近身、5 次中距），冲击首次 S6→S7→S8 完整放置与撤离确认。[最新交接](../../Claude-advice/HARNESS/handoff/20261127-05-codex.md)

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
