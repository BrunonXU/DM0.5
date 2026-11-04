# V1训练从八卡切到前四卡

用户要求让出后四张H20。本轮在8000步完整checkpoint之后停止原训练，改为GPU0–3继续同一50K任务；global batch16不变。

迁移同时恢复模型、Adam、scheduler和数据位置。模型结构与推理合同不变；并行方式和随机流分配改变，不能声称八卡与四卡逐位等价。实际在线进度统一看[STATUS](../../STATUS.md)。

```text
8卡×micro2×累积1 → checkpoint8000 → 4卡×micro2×累积2 → 总计50000步
```

## 停止与恢复依据

- 源run：`/u01/user/runs/f1-toys-v1-formal-20261016-r3`。
- 源checkpoint：`train/v1_formal_eef_lora/checkpoints/global_step_8000/actor`；确认最后写出的`rng.pt`可读取、含8个rank后，验证PID429546的命令身份并发送SIGINT。随后进程消失、八卡显存全部归零。停止回执：`switch-four-gpu-stop.json`。原日志保留KeyboardInterrupt，属于本次主动切换，不是训练崩溃。
- 停止时旧日志最后记录8017步，8001–8017没有新完整checkpoint，切换从8000恢复后重算；原日志/W&B旧行保留，不能把这17步标成四卡结果。同run的W&B拒绝倒退step，四卡重算区间以新本地日志为准，超过旧step后恢复在线连续记录。
- 原游标`epoch=10, offset=630`；11791个训练窗口经DistributedSampler补齐为11792个，每737次global16更新结束一轮。新四卡每rank两倍样本数，micro游标转换为1260；不重读图像来跳过已训练样本。
- 新代码`f1-toys-20261019-v1-four-gpu`；新日志`train-to-50000-four-gpu.log`、启动回执`four-gpu-50k-started.json`；沿用原checkpoint目录和W&B run `f1v1formal20261016main1`。
- 原始数据、旧代码release、旧checkpoint均只读保留；不修改6000D正在提供推理的7000步模型。

## 迁移范围和验证

仅允许显式开启的F1 V1八卡→四卡、11791窗口、micro2/global16、seed20261016、num_workers0、固定sampler/drop_last布局；其他world-size变化仍报错。校验每个旧rank游标，恢复后检查所有Adam step与scheduler last_epoch均为8000。新四卡checkpoint后续同卡数恢复走原有路径。

H20现有PyTorch/torchdata环境使用CPU完成12项测试：旧游标恢复6项，真实loader迁移6项。比较step0/1/737/738/8000后直到epoch尾部的每次global16样本集合，以及下一epoch首个batch，全部一致；错误batch和损坏rank游标拒绝，原state不改写。

测试命令（H20，CPU）：

```bash
CUDA_VISIBLE_DEVICES="" PYTHONPATH=/u01/user/staging/f1-resize-check-20261019 \
  /u01/user/envs/f1-jimu-pi05-v1/bin/python -m pytest -q \
  /u01/user/staging/f1-resize-check-20261019/test_resume.py \
  /u01/user/staging/f1-resize-check-20261019/test_resume_resize.py
```

## 数值与评估边界

每次优化仍覆盖16样本，loss按两次累积平均，学习率仍1e-6；总步数、累计窗口曝光预算不变。rank0–3沿用各自旧RNG，rank4–7不再存在；增强/流匹配噪声与样本分配变化，浮点归约顺序也变化，因此训练轨迹不保证一致，卡数本身不构成模型能力退化。

验证仍覆盖固定288个窗口，但验证噪声按rank设置，四卡后不是严格同噪声对照。不要把切换点的验证loss变化全解释为模型改善/变差；需要时用固定生成评估比较候选。在线恢复实测到8038步，热态末20步中位1.58秒/步；四rank均核验Adam step8000/scheduler8000/LR1e-6，后续loss/梯度有限。GPU0–3各约55–57GiB（nvidia-smi原值56817–57789MiB），GPU4–7均0MiB；新W&B指标已通过API读回。切换后首次8500步保存尚未发生，未宣称新checkpoint保存验收完成。

## 验收产物

6000D证据保存在`artifacts/20261019-four-gpu-resume/`：`live-verification.json`包含四rank恢复日志、末20步数值、GPU进程绑定；`wandb-verification.json`含新在线指标（W&B `_step`从0开始，比完成的optimizer step小1）；`wandb-transition.json`记录持久化资源切换说明；`infra-sync.json`为89文件SHA校验。冻结release另有1534代码文件与42750数据文件校验回执。
