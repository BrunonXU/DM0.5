# 实验产物：需要追证时再看

这里保存可复算的原始结果、索引、图像、JSON/CSV、测试回执与日志。它们主要供Codex诊断和复现使用；人类先看所属版本报告的结论，出现疑问再沿链接检查具体证据。

产物不作为新的“当前状态”，也不要求每个检查都写一份MD。完整来源和失败记录保留，关键影响汇入[训练记录](../training/README.md)、[推理记录](../inference/README.md)或[Eval](../Eval/README.md)。

2026-10-23工作区整理按用户决定保留本机全部F1模型包，原路径与配套合同、统计、tokenizer一同保留；不把模型当作缓存清理。两套本机权重及完整包的校验身份见[模型保留清单](../../../../Artifacts/WorkspaceReports/storage-audit-20261023/cleanup/retained-models.json)，H20上的其他checkpoint仍以对应训练run与模型登记为准。

## 最近证据入口

| 内容 | 位置 |
|---|---|
| Full1K训练分析 | `20261021-full-review/`，结论在[Full复盘](../training/experiments/20261020-v1-fullsft.md) |
| Full625/1K部署 | [验收回执](20261021-full-h20/acceptance.json) |
| Home/time-mask数据 | `20261020-home-time-mask/`，规则在[预处理验收](../DataPrehandle/reports/20261020-home-time-mask.md) |
| 本次文档整理前原文 | [originals.json](20261021-doc-review/originals.json)，各原文以`.txt`保存，避免被误识为当前AGENTS |

除README外生成产物由工作区忽略规则管理；不把大模型、原始图片或凭据提交Git。冻结代码副本里的MD保持其历史身份，不批量改成当前状态。

## 原始输入

本轮使用开发机已有的只读历史副本：

`DAgger/artifacts/results/20261013-resampling-fix/raw-copies/`

其中 `f1_hil_20261013T073059Z_be04bba2` 是讨论中的约 29.28 秒条目；`073143` 是仅保持/Home 的短条目，不算任务成功示教。原件、旧候选和旧验证报告都保留原位置。

## 2026-10-16正式V1

`20261016-v1-formal-audit/`保存15条来源清单、用户成功确认、原件哈希审计、30/20Hz计数，以及本轮实际生成的30Hz索引压缩包和`time-index-summary.json`。原件副本完成以`raw-transfer-verification.json`为准；方法和边界见[预处理报告](../DataPrehandle/reports/20261016-v1-formal-time-indexes.md)。

## 历史产物入口

| 产物 | 位置 |
|---|---|
| 逐帧来源绑定、P99 与 pack 校验后的时间诊断 | [最终时间审计](20261014-timing-audit-verified-p99/timing-audit.json) |
| 四条原件 × 20/30Hz 动作索引候选 | [对照汇总](20261014-preprocessing/anchor_action_candidates/comparison.json) |
| 独立逐值回查原始 state/action、时间与边界 | [独立回读结果](20261014-preprocessing/anchor_action_candidates/independent_readback.json) |
| 显式配置文件复验，073059 仍为 823 个窗口 | [配置绑定 manifest](20261014-preprocessing/config-bound-check/073059-30hz/manifest.json) |
| 一个动态时刻的原主图、候选主图、原右腕图 | [视觉抽查来源](20261014-timing-visual-check/provenance.json) |

这些链接指向本机忽略提交的生成产物；其他工作站需按报告命令重现。初版和中间版时间审计保留原目录，未覆盖。

## 输出原则

- 每次运行使用新的输出路径，工具拒绝覆盖已有结果。
- 记录原件 manifest 哈希、配置、代码版本/文件哈希、采样频率、相机选择与统计口径。
- 时间诊断只说明时间字段与来源；不能证明曝光同步或任务成功。
- 历史技术索引只说明其当时验收范围；当前训练包是否已接入真实reader以对应版本报告为准，不把旧候选状态套到新Full数据。
- `training_ready=false` 与审核状态须随产物保留；技术选择不自动转为人工批准。
- 大型原始图像、模型和日志不复制进 Git。
