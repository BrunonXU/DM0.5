---
doc_type: archive
domain: operations
status: archived
created: 2026-07-28
updated: 2026-08-24
models: [dm05, old_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: [../../CURRENT_STATUS.md, ../../incidents/]
related: []
---

# DM0.5 × G2 当前实施状态（历史快照）

> 本文冻结于2026-07-30左右，不再是当前状态。当前状态见 [`../../CURRENT_STATUS.md`](../../CURRENT_STATUS.md)。

更新时间：2026-07-30 00:23 CST

## 1. 当前结论

数据、模型、官方容器、500-step多卡门禁、checkpoint加载和断点续训全部通过。
50K正式训练已于2026-07-29 23:03 CST正常完成；50,000/50,000步连续，
10份5K间隔checkpoint均具备完整恢复状态，训练指标全部有限。冻结validation
选型已经完成：`checkpoint-35000`为Primary，`checkpoint-30000`为Fallback。
独立50K→100K低学习率延长实验已从累计step 50,000恢复并正常运行。

本文是唯一动态状态页。训练合同见`04_TRAINING_DEPLOYMENT.md`，服务器资产见
`08_RTX6000D_SERVER_AUDIT.md`，完整门禁工作记录见
`09_8GPU_500STEP_BENCHMARK.md`。

## 2. 已完成

| 项目 | 结果 |
|---|---|
| 原始数据 | 三个归档共459,747,472,724 bytes，本地NVMe只读 |
| 完整审计 | 887/887；880有效、7排除 |
| 数据划分 | 800 train / 80 validation；分层、确定性、episode无交叉 |
| 转换与统计 | 880/880转换；0失败；train-only 34D norm stats |
| Base和容器 | 固定revision、关键SHA256、官方镜像digest |
| 模型短闭环 | forward/backward、LoRA、保存、加载、评估、续训 |
| 8卡正式门禁 | global batch 32，500/500，19.83 samples/s，无OOM/NaN/Inf |
| checkpoint闭环 | checkpoint-500合同/新进程加载通过；恢复至502通过 |
| Validation门禁 | 固定8样本base 1.538436，checkpoint-500 0.102662 |
| 正式checkpoint选型 | 两轮FM loss + 部署路径10步动作生成；35K Primary / 30K Fallback |
| 延长实验 | 独立分支从50K恢复；8卡；固定LR 5e-5；首批step连续 |

冻结hash：

- split：
  `0664ba3d7684e916c83f0e37a9a5721f3e03602d26808dd9a7500502214ec11b`
- audit：
  `db672078f22c4f825d394609433c316f5232af1b020a99b8d0768222afd77866`
- conversion：
  `e27ce8e23f94a0a6c6c51ec98937e72c0c825b552a30efa37f43d20fec0f2805`
- norm stats：
  `7e9c150885f5bc305dba3107bc7657bf1d97f5e14751f022e0ce36b0f3203f44`

## 3. 正式50K与checkpoint选型

```text
tmux session               dm05-50k
official image digest      sha256:60a754dce047...
GPU                        8 × RTX 6000D
batch                      4 × 8 × accumulation 1 = global 32
steps / warmup             50,000 / 1,000
base LR                    5e-4
attention                  SDPA / FlashAttention2 / SDPA
checkpoint                 每5,000步，保留10份完整恢复状态
```

查看：

```bash
tmux attach -t dm05-50k
# 不进入会话，只看最近输出
tmux capture-pane -p -t dm05-50k -S -30
```

训练以正常退出完成，最终日志明确记录`Training completed`。最终统计为：

- 运行时间约24.11小时，50,000步，约1.796 epoch；
- 全程train loss/FM loss和gradient norm均有限，无OOM或rank失败；
- 45K前1000步loss均值0.005789，50K前1000步降至0.005493；
- 后段已进入收益递减平台，但没有持续train-loss反弹；
- 训练过程未运行完整80-episode validation，因此不能按train loss直接宣布
  `checkpoint-50000`为最佳。

训练完成后使用冻结80-episode validation执行三级选型：

1. 25K–50K六个候选第一轮FM loss；
2. 30K、35K、25K第二轮加密FM loss；
3. 30K与35K使用部署一致的10步Euler完整动作生成。

第一轮与第二轮FM loss均由30K最低，但35K在最终生成动作指标上全面优于30K：

- normalized全关节MAE：0.03562115 vs 0.04013571；
- physical episode-macro MAE：0.01311568 vs 0.01449428；
- episode P90：0.01760667 vs 0.01923388；
- seed dispersion：0.00871312 vs 0.01106946；
- q01/q99外比例：16.7092% vs 25.2551%。

因此冻结`checkpoint-35000`为Primary、`checkpoint-30000`为Fallback。完整协议、
全部候选排名和后续真机门禁见
`13_DM05_G2_CHECKPOINT_VALIDATION_AND_SELECTION_DECISION.md`。

证据：

- `server/runs/formal_50k/train.log`
- `server/runs/formal_50k/gpu_metrics.csv`
- `server/runs/formal_50k/launch_history.log`
- `server/runs/formal_50k/supervisor.log`
- `server/runs/formal_50k/guardian.log`
- `server/checkpoints/dm05_g2_8gpu_50k/`

已生成并通过完整性检查：

- `checkpoint-5000`至`checkpoint-50000`，每5,000步一份，共10份；
- 每份均包含adapter、34D norm stats、optimizer、scheduler和8-rank RNG；
- `checkpoint-50000`是有效候选，但最终选型必须服从冻结validation结果。

## 4. Systemd到tmux迁移记录

第一次正式启动使用用户级systemd。按用户要求迁移tmux时，停止外层service
没有立即停止Docker daemon中的旧容器，曾短暂出现两个8卡容器。两者均未到
首个checkpoint；已按精确container ID停止，并确认Docker容器数和GPU进程数
均为0。

混合日志完整保存在：

- `server/runs/formal_50k/migration_overlap_attempt.log`
- `server/runs/formal_50k/migration_overlap_gpu_metrics.csv`

随后只启动一个全新tmux容器。当前正式`train.log`从step 1开始，不含迁移期
混杂输出。该经验已写入06。

## 5. 实际耗时与监控

正式训练实际约24.11小时；checkpoint约每2小时25分生成一份，每份约1.84GiB。

本次启动参数`report_to=[]`，没有原生W&B run。完整训练日志已回填到本地
TensorBoard，可通过SSH/VS Code转发服务器`127.0.0.1:6006`查看。

## 6. 2026-07-29 tmux误杀恢复

14:17 CST左右`dm05-50k`原会话被误杀。外层runner和`tee`退出，但Docker
daemon中的训练容器与8个rank没有退出，训练从31,759继续推进。恢复过程：

1. 同时核对`docker ps`、GPU PID和容器自身日志，确认不能启动第二套训练；
2. 从Docker日志精确回填31,760之后的文件日志，边界step无重复、无缺口；
3. 重建同名tmux为只读日志跟随会话；
4. 增加独立guardian处理未来非零容器退出；
5. TensorBoard exporter自动追上恢复后的日志。

该事件没有丢失训练步、没有恢复checkpoint、没有改变optimizer或dtype。

## 7. 真机边界

G2 SDK关节索引、单位、限位、急停和watchdog仍须在任何powered motion前确认。
这不阻塞离线50K训练，但确认前部署只允许shadow mode。

## 8. 正在运行：50K→100K延长实验

2026-07-30 00:22 CST在独立`dm05-50k-to-100k` tmux中启动。该实验：

- 不覆盖原50K输出或35K/30K选型结论；
- 保留50K optimizer、scheduler边界和8-rank RNG；
- 固定LR为5e-5，避免修改总步数造成scheduler回跳；
- global batch仍为32，累计目标100,000，每5,000步保存；
- 已核验step 50,001之后连续、8 rank齐全、LR保持5e-5。

运行合同和证据路径见
`14_DM05_G2_50K_TO_100K_EXTENSION_TRAINING.md`。
