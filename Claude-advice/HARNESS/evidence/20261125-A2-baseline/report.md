# F1 Harness 历史运行漏斗（A-2）

6000D 的 129 个运行目录中，106 次能用同一任务的控制端与规划端记录核对。39 次有首动发布，19 次有抓持成功回执，6 次运输到位。历史记录尚无一次具备 M0 所需的放置回执。其中 105 条能从控制端收尾判定结果，另 1 条在首发后截断，S2–S9 与最终成败未知。另有 23 个目录缺控制端记录，其中 5 个仅有规划端记录，18 个两端均缺。

该样本跨版本、布局及运行模式，含 Astra 61 次、固定程序 33 次和 VLA/Astra 12 次。55 次未能从包身份映射到发布版本；已映射的记录最晚到 V49。建立可比的 V50 与候选差异，还需要用户在同一批已登记的直立布局上分别独立启动。每次有人工重摆、扶正或提前停止，应随该次布局记录。

## 阶段与用时

| 阶段 | 可核查的到达次数 / 试验 | 到达率 | 与上个阶段的中位间隔 | 有效间隔数 |
|---|---:|---:|---:|---:|
| S0 已授权并准备 | 106/106 | 100.0% | 0.00 秒 | 106 |
| S1 首动发布 | 39/106 | 36.8% | 22.95 秒 | 39 |
| S2 接近完成 | 28/105 | 26.7% | 3.83 秒 | 28 |
| S3 闭爪完成 | 20/105 | 19.0% | 8.45 秒 | 20 |
| S4 确认抓住 | 19/105 | 18.1% | 1.07 秒 | 19 |
| S5 运输到位 | 6/105 | 5.7% | 24.69 秒 | 6 |
| S6 对准完成 | 0/105 | 0.0% | — | 0 |
| S7 插入完成 | 0/105 | 0.0% | — | 0 |
| S8 放置确认、有同请求回执 | 0/105 | 0.0% | — | 0 |
| S9 避桶复位 | 0/105 | 0.0% | — | 0 |

授权到首动的中位数为 22.95 秒（39 次）。授权到 S8 的任务耗时缺少样本。M0 单独要求全程无人工、无辅助，S8 回执的请求号须匹配。逐个技能请求的耗时、阶段间隔、每次停点和两端原因见[逐次结果](funnel.json)。

## 失败与撤权

下表按控制端 `skill_result.outcome == failure` 的事件数排名，同一次运行可能贡献多条。第五位有并列，合列同次数的原因。只统计每次运行末因的分布另见 `summary.last_failure_reasons`。

| 技能失败原因 | 事件数 |
|---|---:|
| `plan_revalidation_rejected:revalidation_object_or_hole_changed` | 16 |
| `motion_stalled` | 12 |
| `plan_revalidation_rejected:revalidation_goal_changed` | 6 |
| `numerical_plan_rejected:planning_measurements_expired` | 3 |
| `grasp_not_attached` | 2 |
| `plan_revalidation_rejected:ValueError` | 2 |
| `plan_revalidation_rejected:revalidation_actual_path_sweep_blocked` | 2 |
| `plan_revalidation_rejected:revalidation_camera_identity_changed` | 2 |
| `plan_revalidation_rejected:revalidation_endpoint_outside_remaining_clearance` | 2 |
| `plan_revalidation_rejected:revalidation_measurements_expired` | 2 |
| `plan_revalidation_rejected:revalidation_scene_object_set_changed` | 2 |
| `revalidation_proof_rejected:revalidation_live_scene_invalidated` | 2 |
| `revalidation_proof_rejected:revalidation_proof_expired` | 2 |

明确零发布且属于非物理原因的失败为 42 次，均值 0.396 次/运行；另有 2 次记录为路径扫掠保护。按用户本轮确认，含糊原因按 Claude 的口径计入非物理。计入截断运行时每次取已记录的拒绝数，在 105 次闭合记录中均值为 0.400。V40 抓取请求新增左腕图后首发前被拒；另一次抓取候选的端点复算比净距门限超出 0.105 mm。两条均有零发布回执，现有记录均未证实实物碰撞。两张来自规划帧的现场图像共 118,734 字节，占本批附件约 0.66%。[两条样例与留给 Claude 的问题](cases/README.md)。这是统计分类，执行端仍按原安全合同处理。映射与依据见[原因表](../../../../HARNESS/scripts/funnel_reasons.json)。

控制端写入 477 条撤权事件，按规格剔除网页停止后余 376 条，其中包含正常结束、关闭会话和 owner 收尾。有效授权期间记录到 22 次故障撤权。各运行均保留原始事件、原因分布及各自计数，以便在新旧布局间沿用同一口径。

## 模型调用与取样

模型逻辑请求中位数为 1 次（105 次运行）。按 `model_request_started` 的请求身份识别一次逻辑请求，实际传输尝试、源站写入证据和错误分别统计。完整等待时长可核对的 93 次运行中，每次总等待的中位数为 5.44 秒，含零请求运行。未闭合的请求仍保留已观测时长。

五次独立抽查覆盖达到 S5、S4、S0 以及模型连接重试的记录。每条有双端原件 SHA-256 和事件行，可逐项复核。[五条抽查](manual-audit.json)

## 复现与本次交付

在 6000D 用下面的命令对原件重新计算；每次排除的目录和理由列在 JSON 的 `runs` 中。

```bash
python3 HARNESS/scripts/run_funnel.py --workspace HARNESS/runs/workspace --output /tmp/harness-a2-funnel.json
```

证据随分支提交，`git ls-files` 核对本批目录共有 50 个跟踪文件。按原始开始时间取最近十次，每次上传两端轨迹、计时报告和运行快照的脱敏副本。另外选取两条含糊原因的实际业务数据与对应现场关键帧供审阅。完整视频、深度载荷和凭据由原件保管，数据来源及双哈希见[清单](manifest.json)，布局记录见[取样说明](layouts.md)。

本批只增加离线统计和证据，执行路径新增拒绝原因 **0**。历史漏斗用于诊断。候选的软件验收、10 次固定布局实测及改动前后对照另行登记。
