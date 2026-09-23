---
doc_type: experiment
domain: training
status: frozen
created: 2026-07-28
updated: 2026-07-29
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [10_FORMAL_50K_TRAINING.md]
superseded_by: []
related: []
---

# DM0.5 × G2 正式50K训练记录

更新时间：2026-07-29 23:03 CST

## 1. 文档职责

本文记录正式50K运行的不可变配置、里程碑、checkpoint验证和异常。实时摘要
同时写入02；500-step准入证据见09。

## 2. 正式运行合同

| 项目 | 冻结值 |
|---|---|
| 干净启动时间 | 2026-07-28 22:56:43 CST |
| tmux | `dm05-50k` |
| Docker | 官方digest `sha256:60a754dce047...` |
| GPU | 8 × RTX 6000D |
| 数据 | 800 train；80 validation；train-only norm stats |
| Batch | per-device 4，accumulation 1，global 32 |
| Steps | 50,000 |
| LR / warmup | base LR 5e-4；warmup 1,000 |
| Attention | LLM SDPA / Vision FA2 / Action SDPA |
| Checkpoint | 每5,000步；完整状态；保留10份 |

初始实际入口：

```text
tmux dm05-50k
└── infra/host/run_formal_8gpu_50k_supervised.sh
    └── infra/host/run_formal_8gpu_50k.sh
        └── 官方OpenDM容器，8-rank launcher
```

31K误杀恢复后，训练容器由Docker daemon继续持有；同名tmux改为只读日志跟随，
独立guardian负责异常退出恢复，详见本文第7节。

## 3. 最终里程碑

| Step | 落盘时间 | 合同检查 | 完整恢复状态 | 大小 |
|---:|---|---|---|---:|
| 5,000 | 2026-07-29 01:19 CST | PASS | optimizer/scheduler/8-rank RNG齐全 | 1.841GiB |
| 10,000 | 2026-07-29 03:45 CST | PASS | 齐全 | 1.842GiB |
| 15,000 | 2026-07-29 06:11 CST | PASS | 齐全 | 1.843GiB |
| 20,000 | 2026-07-29 08:37 CST | PASS | 齐全 | 1.844GiB |
| 25,000 | 2026-07-29 11:01 CST | PASS | 齐全 | 约1.84GiB |
| 30,000 | 2026-07-29 13:26 CST | PASS | 齐全 | 约1.84GiB |
| 35,000 | 2026-07-29 | PASS | 齐全 | 约1.84GiB |
| 40,000 | 2026-07-29 | PASS | 齐全 | 约1.84GiB |
| 45,000 | 2026-07-29 | PASS | 齐全 | 约1.84GiB |
| 50,000 | 2026-07-29 23:03 CST | PASS | 齐全 | 约1.84GiB |

最终训练摘要：

- 50,000/50,000步正常完成，运行约24.11小时，约1.796 epoch；
- 50,000条逐step记录均为有限值；
- 无OOM、Traceback、checkpoint损坏或rank非正常退出；
- 日志末尾NCCL `destroy_process_group()`是进程退出清理告警，发生在模型保存和
  `Training completed`之后，不影响checkpoint有效性。

## 4. 初步训练曲线结论

按每5K区间统计，loss均值从0–5K的0.084440持续下降到45–50K的0.005616；
对应区间内线性斜率全部为负。checkpoint前1000步均值从45K的0.005789降至
50K的0.005493。

最后500步均值从前一个500步窗口的0.005407小幅升至0.005579，但中位数仅从
0.004578变为0.004606，且包含step 49,974的0.09818难批次尖峰。因此这是正常
随机batch波动与平台期表现，不是持续反弹证据。结论是训练优化已经充分并进入
收益递减区；是否过拟合、哪个checkpoint泛化最好，必须由冻结validation sweep
判断。

后续冻结validation已经完成：30K在两轮FM loss中最低，但35K在部署路径10步
完整动作生成的MAE、P90、seed稳定性和训练分位外比例上均优于30K。因此正式
选型为35K Primary、30K Fallback，而不是最后的50K。完整决策见
`CHECKPOINT_VALIDATION_AND_SELECTION.md`。

历史状态（2026-07-29 09:20 CST）：

- 超过21,400/50,000，约43%；
- 最近100步平均loss约0.01444；
- 最近观测loss 0.00870、gradient norm 0.05469；
- 8卡显存约19.5GiB/卡，利用率约99%～100%；
- loss和gradient norm全部有限；
- 无OOM、Traceback、容器重启或supervisor重试；
- 按当前速度剩余约13.5小时。

以上训练loss很低是正向信号，但不能单独解释为validation泛化或真机成功率。

## 5. 监控与证据

```bash
tmux attach -t dm05-50k
tmux capture-pane -p -t dm05-50k -S -30
tail -f server/runs/formal_50k/train.log
nvidia-smi
```

证据路径：

- 主日志：`server/runs/formal_50k/train.log`
- GPU采样：`server/runs/formal_50k/gpu_metrics.csv`
- 启动历史：`server/runs/formal_50k/launch_history.log`
- 监督器退出历史：`server/runs/formal_50k/supervisor.log`
- checkpoints：`server/checkpoints/dm05_g2_8gpu_50k/`

## 6. W&B状态

本次Trainer的`report_to=[]`，未创建原生W&B或TensorBoard run；服务器也没有
W&B登录配置。因此当前不存在可打开的W&B URL。

这不影响训练指标保存：主日志逐step记录loss、FM loss、gradient norm、LR和
epoch，每个checkpoint的`trainer_state.json`也保存完整历史。现已把这些历史
回填到TensorBoard并每5秒追写，服务只监听`127.0.0.1:6006`，由
`dm05-curves` tmux管理。若用户完成W&B登录，也可另启只读旁路回填；下次正式
实验应在step 0前冻结project/entity/run ID。

TensorBoard证据：

- events：`server/monitoring/formal_50k/`
- exporter：`infra/host/export_train_log_tensorboard.py`
- launcher：`infra/host/run_formal_tensorboard.sh`
- 使用说明：`../../CHECKPOINT_SELECTION_FIRST_PRINCIPLES.md`

## 7. 已记录的启动前事件

systemd迁移tmux时曾短暂存在两个未到checkpoint的容器；两者已停止，混合日志
独立保存在`migration_overlap_attempt.log`。本正式运行于22:56:43从step 0
重新开始，只有一个容器，该事件没有污染正式checkpoint或当前主日志。

## 8. 后续门禁

### 31K阶段tmux查看会话恢复

2026-07-29 14:17 CST左右，原`dm05-50k` tmux会话被误杀。实际Docker容器
`18f34235238c...`和8个训练rank继续运行，容器日志显示step从31,759连续增长，
因此没有从30K checkpoint恢复，也没有丢失约1,700步。

文件日志因原`tee`退出暂时停止；现已从Docker daemon日志以step 31,760为
精确边界补齐并恢复实时跟随。31,758～31,761及抽查step均各出现一次，没有
重复或缺口。恢复后：

- `dm05-50k`：只负责`docker logs -f`和写入主日志；
- Docker容器：持有实际训练，杀tmux不影响它；
- `dm05-formal-guardian.service`：等待容器退出；exit 0表示正常完成，
  非零时启动官方resume流程；
- `dm05-curves`：继续从主日志实时导出TensorBoard。

恢复确认时训练超过31,900步，8卡约99%～100%，loss/gradient有限。

1. 每新增5K checkpoint，检查合同、global step、完整恢复状态和文件大小；
2. 50K完成后确认trainer summary、全部数值有限和tmux正常退出；
3. 校验checkpoint-50000并在全新进程加载；
4. 用冻结validation索引对比base、500-step与50K checkpoint；
5. 写最终训练报告，再进入shadow部署准备。
