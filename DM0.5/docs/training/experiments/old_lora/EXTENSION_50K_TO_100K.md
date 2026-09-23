---
doc_type: experiment
domain: training
status: frozen
created: 2026-07-30
updated: 2026-07-30
models: [old_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [14_DM05_G2_50K_TO_100K_EXTENSION_TRAINING.md]
superseded_by: []
related: []
---

# DM0.5 × G2 50K-to-100K Extension Training Record and Decision Boundary

更新时间：2026-07-30 11:05 CST

## 1. 文档职责

本文记录正式50K训练之后的独立延长实验，包括为什么不能直接修改总步数、
学习率连续性处理、运行合同、证据位置和结果使用边界。原50K训练见
`FORMAL_50K_TRAINING.md`，25K–50K最终选型见
`CHECKPOINT_VALIDATION_AND_SELECTION.md`。

## 2. 实验定位

该运行是探索“低学习率继续训练是否还能改善泛化”的独立实验，不是对原50K
结果的覆盖。当前部署候选保持：

- Primary：`checkpoint-35000`
- Fallback：`checkpoint-30000`

50K之后的checkpoint只有通过与13文档完全相同的validation和动作生成协议，
并且实质优于35K，才允许发布新的Primary决策。

## 3. 为什么不直接把max_steps改成100K

原50K运行使用`cosine_with_min_lr`，在step 50,000已经降到`5e-5`。如果只把
`max_steps`改成100,000并原样恢复，新的scheduler会按100K总长度重新计算，
可能把学习率从`5e-5`抬回约`2.8e-4`。这会在续训边界产生非计划的学习率冲击，
无法解释后续质量变化。

延长分支因此：

1. 独立复制正式`checkpoint-50000`；
2. 保留optimizer动量和8-rank RNG；
3. 把optimizer/scheduler边界基准冻结为`5e-5`；
4. 使用`cosine_with_min_lr`且`min_lr_rate=1.0`，使延长段保持固定`5e-5`；
5. 原始50K checkpoint的optimizer和scheduler SHA256复核未改变。

## 4. 冻结运行合同

| 项目 | 值 |
|---|---|
| 启动时间 | 2026-07-30 00:22:50 CST |
| tmux | `dm05-50k-to-100k` |
| 起点 | 正式`checkpoint-50000`独立副本 |
| 目标 | 累计step 100,000 |
| GPU | 8 × RTX 6000D |
| Batch | per-device 4 × 8，global 32 |
| LR | 固定5e-5；无第二次warmup |
| Attention | LLM SDPA / Vision FA2 / Action SDPA |
| Checkpoint | 每5,000累计step；总保留11份 |
| Docker | 官方固定digest `sha256:60a754dce047...` |

## 5. 启动验收

首批验收已经确认：

- Trainer从累计step 50,000恢复，而不是从0重新训练；
- step 50,001至50,019连续产生；
- 每个step的learning rate均为`5e-5`；
- loss、FM loss和gradient norm全部有限；
- 8个rank和8张GPU均存在，显存约25GiB/卡，GPU进入稳定训练负载；
- 无OOM、Traceback、rank退出或scheduler学习率回跳。

恢复瞬间进度条显示的极高`it/s`来自Trainer跳过已完成的50K历史step，不是实际
训练吞吐；进入新step后恢复正常约1.7秒/step。

## 6. 证据路径

- 输出：`server/checkpoints/dm05_g2_8gpu_50k_to_100k/`
- 运行日志：`server/runs/formal_50k_to_100k/train.log`
- 启动历史：`server/runs/formal_50k_to_100k/launch_history.log`
- GPU采样：`server/runs/formal_50k_to_100k/gpu_metrics.csv`
- tmux：`dm05-50k-to-100k`
- 分支准备器：`infra/host/prepare_50k_to_100k_branch.sh`
- 运行器：`infra/host/run_formal_8gpu_50k_to_100k.sh`

## 7. 后续决策点

延长实验每新增5K checkpoint执行完整性检查。训练结束后，55K至100K的全部
5K间隔checkpoint与原25K至50K候选一起进入13号文档修订后的统一协议：FM loss
只作诊断，所有完整候选均执行10步Euler动作生成，近邻候选再做配对加密复测。
如果所有延长checkpoint均不优于当前Primary，则保留35K并把50K后的结果记录为
无额外离线泛化收益证据；是否属于过拟合需要结合统一验证趋势判断，不能只依据
train loss。

不得因为延长段train loss继续下降就替换Primary。最终仍以未见episode的FM loss、
部署路径完整动作生成、约束指标和后续shadow表现共同决策。

## 8. TensorBoard监控

2026-07-30已把续训日志接入本机TensorBoard，服务仍只监听
`127.0.0.1:6006`，不影响训练进程，也不占用GPU。仪表盘采用“同一页面、两个
独立Run”的结构：

- `formal_50k`：正式0–50K历史曲线；
- `extension_50k_to_100k`：从累计step 50,000恢复后的实时续训曲线。

两个Run可在同一指标图中叠加，也可单独勾选。保留独立Run是为了明确恢复边界和
固定`5e-5`学习率实验边界，避免把两次运行伪装成一条没有断点的原生100K运行。
事件目录位于`server/monitoring/`，数据源始终是只读训练日志。

## 9. 75K计划停训与恢复合同

因真机推理需要释放8张GPU，已设置独立tmux守护任务
`dm05-pause-at-75k`。守护任务不会在step 75,000计算完成时立即硬杀，而是等待
`checkpoint-75000`的以下状态完整写盘：

- adapter权重；
- optimizer状态；
-固定`5e-5` scheduler状态；
- trainer state且`global_step=75000`；
- 8个rank各自的RNG state；
- 文件在20秒稳定窗口内大小和修改时间不再变化。

验收完成后守护任务向正式训练tmux发送`Ctrl-C`，等待当前Docker训练容器退出并
释放GPU。即使保存后已经计算了少量75K之后的step，恢复时仍以75K checkpoint
为唯一边界，未保存的step不计入后续训练。

暂停证据写入：

- `server/runs/formal_50k_to_100k/pause_at_75000.log`
- `server/runs/formal_50k_to_100k/.paused_at_75000`

恢复时重新运行同一`run_formal_8gpu_50k_to_100k.sh`。Trainer会在输出目录中
选择最新的完整checkpoint，即75K，并恢复模型、optimizer、scheduler、global
step和各rank RNG；启动历史会动态记录实际`resume_step`，不再固定写成50K。

恢复后必须验收：

1. 日志明确从累计75K恢复，而不是从0或50K开始；
2. 首批新step大于75,000且连续；
3. learning rate仍为`5e-5`，没有第二次warmup；
4. 8个rank、global batch 32和attention组合不变；
5. loss、FM loss及grad norm有限，无scheduler回跳；
6. TensorBoard续训Run继续使用累计step，不新建伪0起点。

完整checkpoint恢复在优化语义上是连续训练，不会把模型“还原成未训练状态”。
但由于CUDA kernel、FSDP通信和dataloader worker调度并不保证逐bit确定，恢复后
的后续batch顺序或浮点末位可能与完全不停机运行略有差异；这属于正常随机分叉，
通常不构成可测的模型质量损失。严禁只加载adapter而丢弃optimizer/scheduler/RNG
来冒充连续恢复。
