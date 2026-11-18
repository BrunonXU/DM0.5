# F1 Harness 当前状态

**截至 2026-11-18，F1 Harness 按技能复核相机来源 V42 已部署，工作台为 F1infra V56。** 上线时 6000D、机器人及 4242 已确认 Harness Agentic 可开始，推理为 Astra 6.2 high。上一个实际部署版本为 V40，V41 在候选导入检查时淘汰。[发布登记](../releases/registry.json) · [三端回执](../artifacts/optional-camera-revalidation-v42-20261118/post-activation.json)

12:26 的 V40 把一张新增的辅助左腕图误判为相机替换，在首次接近发布前撤权。V42 用两帧现场原件和原候选走通了控制器首次内存发布，以及整段接近轨迹复核。下一条现场先观察右臂能否起动并完成单块抓放，首次桶障碍定位耗时另行追踪。[现场记录与对照](experiments/20261117-local-capabilities.md#v40-首动前相机复核与现场耗时)

## 已验证的范围

| 层级 | 证据支持的结论 |
|---|---|
| V42 最新失败回放与验收 | 原候选 3 路点、76 帧内存平滑输出、140 次细分检查通过。正常 10 阶段与抓空恢复 14 阶段完整零硬件输出回放通过。ARM 新采 28 组中 25 组跨视角重建方孔。经验程序在撤权时保留控制器真实回执及发布状态。现场动作和完整抓放待验证。[现场回归](../artifacts/optional-camera-revalidation-v42-20261118/field-regression.json) · [冻结验收](../artifacts/optional-camera-revalidation-v42-20261118/release-validation.json) |
| V40 现场采样与回放 | 桶移动后，原先失效的 19 组完整输入全部重建方孔；新采 30 组中 22 组重建，8 组缺少新鲜同步输入。两组真实遮挡画面均找回方孔，结合同次运行其他时刻的持物证据后可求解当前对准。正常 10 阶段和抓空恢复 14 阶段完整模拟均通过；部署未发起动作。[现场逐组对照](../artifacts/relative-aperture-v40-20261118/field-regression.json) · [完整回放](../artifacts/relative-aperture-v40-20261118/full-validation-pinned/validation-summary.json) |
| V38 真机记录 | 11 月 18 日 10:21 的记录包含接近、抓取和向桶运输三段实测关节运动。运输净距来自固定场景下的几何复算，现场是否接触需独立证据。[运动窗口](../../../Artifacts/F1-Harness/20261118-1021-run2853/motion-window-estimates.json) · [净距复算及限制](../../../Artifacts/F1-Harness/20261118-1021-run2853/clearance.json) |
| V38 现场数据回放 | 使用 V36 的已记录反馈，补定位后接回同一候选源码，三步中只执行一次抓取。运输末路点后 22 条观测中，18 条确认持物，4 条证据不足。精确对孔仍需当前孔口几何。[经验接续与遮挡复核](experiments/20261117-local-capabilities.md#v38-经验接续与遮挡复核) |
| V38 冻结验收与现场只读采样 | 正常和抓空恢复两条全流程零硬件输出回放通过。动作链使用头部与右腕 RGB-D。机器人端 27 组只读采样中，17 组返回局部场景，10 组需要新的右腕积木提示。左腕用于录像和观察，静止样本可见方孔和三角孔。[冻结验收](../artifacts/anchor-recovery-v38-20261118/release-validation.json) · [左腕静止样本](../artifacts/anchor-recovery-v38-20261118/field-images/manual-left-1794967058960051907/left_wrist-apertures.png) |

按版本追查请看[实验索引](experiments/README.md)。整理前各阶段的状态与原证据见[截至 V38 上线的状态页归档](history/20261118-status.md)。
