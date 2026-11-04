---
doc_type: experiment
domain: training
status: frozen
created: 2026-07-28
updated: 2026-07-28
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [09_8GPU_500STEP_BENCHMARK.md]
superseded_by: []
related: [../../incidents/]
---

# 8卡500-step正式训练门禁报告

更新时间：2026-07-28 22:52 CST

## 1. 结论

正式门禁通过。8张RTX 6000D在官方固定OpenDM镜像中，以global batch 32完成
500/500 optimizer steps；无OOM、无NaN/Inf，完整checkpoint、全新进程加载和
断点续训均通过。该结果允许启动50K正式训练。

本报告是本阶段冻结工作记录。动态状态只在`../../../CURRENT_STATUS.md`维护，
冻结训练合同见`../../../core/TRAINING_AND_DEPLOYMENT_CONTRACT.md`。

## 2. 冻结输入

| 项目 | 值 |
|---|---|
| 项目HEAD | `d70aa4dd1ad44ad629a1b5ab93e126cf2774d2f9`，另有本阶段未提交工作树diff |
| OpenDM commit | `6ce8e7f13e0c46906b141fda1773364f18a577c2` |
| 官方镜像digest/ID | `sha256:REDACTED_HOST_FINGERPRINT` |
| DM0.5 base revision | `581279826a339f0d90de15f145ec1a96aeea081e` |
| 数据 | 800 train / 80 validation；880条全部转换成功 |
| split SHA256 | `0664ba3d7684e916c83f0e37a9a5721f3e03602d26808dd9a7500502214ec11b` |
| norm stats SHA256 | `7e9c150885f5bc305dba3107bc7657bf1d97f5e14751f022e0ce36b0f3203f44` |

数据审计使用24进程完成887/887条检查，结果为880条有效、7条排除。基准只读取
NVMe中的冻结转换数据，normalization只使用800条train计算的34D统计。

## 3. 正式门禁配置

```text
GPU/world size             8 × RTX 6000D / 8
optimizer steps            500
per-device batch           4
gradient accumulation      1
global batch               4 × 8 × 1 = 32
effective samples          16,000
base LR                    5e-4
warmup                     1,000 steps
attention                  LLM SDPA / Vision FA2 / Action SDPA
dataloader workers         4 per rank
checkpoint                 every 250 steps, keep 2
checkpoint contents        adapter + dense modules + optimizer/scheduler/RNG
```

模型、LoRA、优化器、warmup和数据管线沿用官方实现。唯一执行内核覆盖是
`LLM FlexAttention -> SDPA`，原因和语义影响见04与06。

## 4. 训练结果

| 指标 | 实测 |
|---|---:|
| 完成步数 | 500/500 |
| runtime | 806.8秒（13分27秒） |
| step/s | 0.62 |
| samples/s | 19.83 |
| 平均train loss | 0.4726 |
| 最终loss / FM loss | 0.0550 / 0.0550 |
| loss范围 | 0.05155～3.71595，全部有限 |
| gradient norm范围 | 0.4043～12.0625，全部有限 |
| 每卡峰值显存 | 19,526～19,543MiB |
| 每卡活跃期平均利用率 | 76.5%～82.1% |

`checkpoint-250`和`checkpoint-500`均正常生成。`checkpoint-500`约1.9GB，
其中adapter约648.8MB、optimizer约1.293GB，并包含scheduler、8个rank的RNG、
trainer state和34D norm stats。

adapter SHA256：
`63911e298e199af3eec2e301818e2422dcae5ec54b6bb0c11e4f85a4ac579ddb`。

早先global batch 8的500-step运行只作为链路预热：1.29 step/s、10.32
samples/s、387.6秒。它不满足冻结的global batch 32合同，因此没有替代本报告
的正式门禁结果。

## 5. Checkpoint闭环

### 5.1 合同与新进程加载

`checkpoint-500`通过机器合同检查：

- adapter配置和34D密集action模块齐全；
- state/action的mean、std、q01、q99全部为34D；
- optimizer、scheduler、trainer state齐全；
- 8个rank的RNG状态齐全；
- 全新容器成功加载base、合并adapter并完成真实forward；
- 新进程forward loss为0.200863，峰值显存11.107GiB。

### 5.2 断点续训

使用同一官方镜像启动全新8卡容器，trainer自动发现`checkpoint-500`，恢复
optimizer、scheduler和RNG后实际训练到502并保存`checkpoint-502`。因此验证的
是完整恢复，不只是adapter可读。

恢复后观察到官方Trainer/PEFT路径将104,848,384个LoRA参数从BF16提升为FP32；
参数键数仍为1422、总训练参数仍为324,285,474，训练和再次保存正常。这会增大
恢复后的adapter/optimizer文件，但没有缺键。正式首跑不中断时不会触发；
若服务故障后自动恢复，按官方行为继续并在后续报告记录dtype变化。

## 6. Validation方向性门禁

用固定seed和8个固定validation索引，在全新进程中比较相同flow噪声：

| 模型 | 平均FM loss |
|---|---:|
| Base | 1.538436 |
| checkpoint-500 | 0.102662 |
| 相对变化 | -93.33% |

validation明确复用train-only norm stats；第一次评估暴露出上游文件名digest会
按dataset name寻找validation统计，评估器已修正为强制指向train统计后重跑。

8个样本只证明数据、normalization、adapter加载和学习方向一致，不能据此宣称
泛化成功或机器人任务成功率。模型质量仍由完整validation离线指标和后续
shadow/真机安全验收决定。

## 7. 证据路径

- 主日志：`server/runs/full_8gpu_gb32_benchmark/train.log`
- GPU采样：`server/runs/full_8gpu_gb32_benchmark/gpu_metrics.csv`
- 恢复日志：`server/runs/full_8gpu_gb32_benchmark/resume_500_to_502.log`
- 新进程加载：`server/runs/full_8gpu_gb32_benchmark/checkpoint500_reload.log`
- Base评估：`server/runs/full_8gpu_gb32_benchmark/validation_base.json`
- Adapter评估：`server/runs/full_8gpu_gb32_benchmark/validation_checkpoint500.json`
- checkpoint：`server/checkpoints/benchmark_full_8gpu_gb32_500/checkpoint-500/`

## 8. 下一阶段

启动`dm05-50k` tmux会话：

- 官方固定镜像；
- 8卡、global batch 32；
- 50,000 steps、1,000 warmup、base LR 5e-4；
- 每5,000步保存完整可恢复checkpoint，最多保留10份；
- tmux supervisor在进程失败时等待30秒重启，官方trainer自动恢复最新
  checkpoint。

按本次吞吐，纯训练约22.4小时；考虑5K checkpoint写盘与验证，预计23～25小时。
首个正式checkpoint预计启动后约2.2～2.5小时。
