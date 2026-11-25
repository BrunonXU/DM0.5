# F1 Harness 当前状态

**F1 Harness 运输目标区域与分阶段补验 V52 在用，暂缓在 4242 点击「开始任务」。** 候选 **F1 Harness 左臂新基线与场景预检 V53**（`53af3b1d4406`）已冻结并通过 6000D 全量软件回归与三条零硬件输出回放，仅调整左臂第 2 关节下限（`−94.0° → −94.7°`，远在 URDF 与驱动机械极限 `−120.0°` 内；右臂与其他 21 轴及碰撞模型不变）并新增独立只读预检脚本 [preflight_scene.py](../scripts/preflight_scene.py)。当前 F1-09 离线（管理隧道断开），按用户决定本轮先提交 V53 离线验收与 PR；待联机后完成 ARM 上线、同步 home 预设并在原位执行 `preflight_scene.py`。[V53 验收与核查](../../Claude-advice/HARNESS/evidence/20261125-V53-left-baseline-preflight/report.md) · [V52 网关与连接实测](../../Claude-advice/HARNESS/evidence/20261125-B-gateway-check/report.md)

用户已确认拆掉的是左侧夹爪且未碰到左腕相机支架，碰撞模型保守保留原夹爪包络（`arm_tip_extension=0.10m`）。Cyclone 与 Fast DDS 的 `/hal/joint_states` 实测对照表明：控制端（Cyclone）经 RTPS/UDPv4 接收 `motor_ctl`（Fast DDS）反馈，早期 0 包属跨 RMW 短窗发现偶发漏配，而本机默认 Fast DDS 存在 `/dev/shm` SHM 卡死风险（切 UDPv4-only 即恢复 ~50 Hz）。11 月 25 日已记录的两次 V52 任务均停在 S0，技能动作首发为零，收尾均实测静止。[现场复盘](../../Claude-advice/HARNESS/evidence/20261125-B-first-run/report.md) · [部署回执](../../Claude-advice/HARNESS/evidence/20261125-B-deployment-V52/deployment.json)

现场采用自然随机摆放的直立方块，按运行编号记录 S0–S9，场景编号可空。首次无人介入且有同请求放置回执计 M0，避桶复位另记 S9。[发布登记](../releases/registry.json)

## 已验证的范围

| 层级 | 证据支持的结论 |
|---|---|
| V53 冻结软件与零硬件回放（待部署） | 仅将左臂第 2 关节下限由 `−94.0°` 调整为 `−94.7°`（右臂与其他 21 轴、碰撞模型与控制源码不变），并新增独立只读 [preflight_scene.py](../scripts/preflight_scene.py)。3,176 项全量用例（项目 3,106 + 数值 70）、62 项原生集成、41 项 F1-4242 关节合同、31 项 DAgger 数值边界与 10 组路由快照通过；正常、抓空恢复、孔口遮挡三条回放分别完成 10／12／12 阶段，输出审计全过，硬件输出为零。ARM 上线、`presets.json` 同步与现场预检待 F1-09 联机。[V53 证据报告](../../Claude-advice/HARNESS/evidence/20261125-V53-left-baseline-preflight/report.md) |
| V52 软件、回放与部署 | 正式任务最多可做六次只读补验，运输规划可选取孔口上方通过扫掠检查的偏移终点。3,173 项软件用例及另计的 62 项原生集成用例通过。正常、抓空恢复、孔口遮挡三条回放均取得同一请求的放置回执并退回，硬件输出为零。[统一验收](../../Claude-advice/HARNESS/evidence/20261125-B-batch/report.md) |
| V51 冻结回放与 ARM 只读 | 3,155 项软件用例通过。正常、抓空后重抓及遮孔后侧移分别走完 10／12／12 步，硬件发布为零。真机空爪采样 32 组，31 组形成场景，左腕在这次采样中为 0 帧。精确对孔与放置待现场验证。[验收证据](../../Claude-advice/HARNESS/evidence/20261125-B-deployment-V51/report.md) |
| V50 实录与模拟回放 | V49 直立真机运到桶上方，侧移后测到三维方孔。V49 现场第 149 帧能解出 24 路点的对准候选，第 152 帧缺孔时保留原候选。独立模拟场景在内存中首发，实物放置待验。[现场与回放对照](experiments/20261117-local-capabilities.md#v50-冻结基线与现场证据) |
| V42 上线前失败回放与验收 | 该次工作台为 F1infra V56，前一个实际部署版本为 V40，V41 在冻结导入检查时取消。V40 的首动因新增左腕图受阻。原候选 3 路点、76 帧内存平滑输出、140 次细分检查通过。正常 10 阶段与抓空恢复 14 阶段完整零硬件输出回放通过。ARM 新采 28 组中 25 组跨视角重建方孔。经验程序在撤权时保留控制器真实回执及发布状态。当次上线时现场动作和完整抓放待验证。[V40 现场与 V42 回放](experiments/20261117-local-capabilities.md#v40-首动前相机复核与现场耗时) |
| V40 现场采样与回放 | 桶移动后，原先失效的 19 组完整输入全部重建方孔；新采 30 组中 22 组重建，8 组缺少新鲜同步输入。两组真实遮挡画面均找回方孔，结合同次运行其他时刻的持物证据后可求解当前对准。正常 10 阶段和抓空恢复 14 阶段完整模拟均通过；部署未发起动作。[现场逐组对照](../artifacts/relative-aperture-v40-20261118/field-regression.json) · [完整回放](../artifacts/relative-aperture-v40-20261118/full-validation-pinned/validation-summary.json) |
| V38 真机记录 | 11 月 18 日 10:21 的记录包含接近、抓取和向桶运输三段实测关节运动。运输净距来自固定场景下的几何复算，现场是否接触需独立证据。[运动窗口](../../../Artifacts/F1-Harness/20261118-1021-run2853/motion-window-estimates.json) · [净距复算及限制](../../../Artifacts/F1-Harness/20261118-1021-run2853/clearance.json) |
| V38 现场数据回放 | 使用 V36 的已记录反馈，补定位后接回同一候选源码，三步中只执行一次抓取。运输末路点后 22 条观测中，18 条确认持物，4 条证据不足。精确对孔仍需当前孔口几何。[经验接续与遮挡复核](experiments/20261117-local-capabilities.md#v38-经验接续与遮挡复核) |
| V38 冻结验收与现场只读采样 | 正常和抓空恢复两条全流程零硬件输出回放通过。动作链使用头部与右腕 RGB-D。机器人端 27 组只读采样中，17 组返回局部场景，10 组需要新的右腕积木提示。左腕用于录像和观察，静止样本可见方孔和三角孔。[冻结验收](../artifacts/anchor-recovery-v38-20261118/release-validation.json) · [左腕静止样本](../artifacts/anchor-recovery-v38-20261118/field-images/manual-left-1794967058960051907/left_wrist-apertures.png) |

按版本追查请看[实验索引](experiments/README.md)。整理前各阶段的状态与原证据见[截至 V38 上线的状态页归档](history/20261118-status.md)。
