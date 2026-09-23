---
doc_type: incident
domain: server
status: active
created: 2026-08-26
updated: 2026-08-26
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [gpu_inference_training_contention_20260826]
supersedes: []
superseded_by: []
related: [../../CURRENT_STATUS.md, ../INFERENCE_LATENCY_FIRST_PRINCIPLES.md, ../TRANSPORT_BINARY_WEBSOCKET.md]
---

# 2026-08-26：推理服务与他人训练争用GPU导致Sync长停顿

| 项目 | 结论 |
|---|---|
| 遇到什么 | 10Hz手部Sync中出现一次约1.87秒的整机末位保持 |
| 影响 | 第15个chunk之前等待新策略动作，操作者感知为一次明显长时间卡顿 |
| 根因 | 7892所在GPU4同时运行他人OpenPI四卡训练；模型CUDA段出现单次1.708秒长尾 |
| 解决 | 原正式Sync端口7892迁到GPU7并升级native-image binary WebSocket；7896候选停止，7893/7894停止；正式RTC后续仍使用原7893端口 |
| 状态 | Sync迁移完成；GPU7仍有外部teleop的230MiB跨卡上下文，带电前必须复核没有重计算负载 |

## 1. 现场证据

G2日志`g2_powered_split_sync_60s_20260826_103903.jsonl`使用手部10Hz，共35个chunk。手部实际频率中位数约10.00Hz、最大间隔101.9ms；双臂约98Hz、最大间隔13.9ms，均无超过1.5倍周期的deadline miss。

唯一明显卡顿发生在请求15：

- 上一chunk于`10:39:33.482`结束；新动作于`10:39:35.354`到达，末位保持约1.873秒；
- 客户端策略请求延迟1.852秒，observation→Control为1.871秒；
- 服务端handler为1.810秒，其中`model_and_device_to_cpu`为1.708秒；
- 同次相机采集约17ms、prepare约70ms、输入transform约28ms；
- 本次运行模型段中位数约348ms，因此异常约为中位数的4.9倍。

7892服务日志记录同一请求正常HTTP 200，没有recompile、协议错误、容器重启或OOM。等待发生在同步架构的chunk间策略请求阶段，而不是10Hz手部线程或100Hz Servo发送阶段。

## 2. 资源争用

故障时7892固定在GPU4，自身占约12.3GB。用户`xx_100872445`的OpenPI训练从09:35开始，在GPU4占约64.35GB且GPU利用率100%；同一训练还占用GPU0、5、6。GPU4总显存约77.9/85.7GB。

因此证据支持：训练kernel与推理kernel共享GPU4，推理请求在CUDA执行/同步阶段排队，产生1.708秒长尾。它不能被归因于手部命令从5Hz升到10Hz。

## 3. 迁移结果

最初曾把7892、7893、7896都收敛到GPU1。旧HTTP启动脚本暴露出一次与GPU无关的Python入口错误：直接执行`python g2_sort/training/NewLora_dm05_lora.py`会因模块遮蔽报`g2_sort is not a package`；改为`python -m g2_sort.training.NewLora_dm05_lora`后恢复。三个compile模型可以在显存上共存，但共享故障域、外部任务和未来并发风险使该布局不再合理。

GPU3和GPU7释放后，当前改为：

| 端口 | 角色 | 当前GPU | 状态 |
|---:|---|---:|---|
| 7892 | 正式Sync native JPEG/PNG binary WebSocket | 7 | 常驻；20/30Hz抖动否决后回退10Hz |
| 7893 | 正式RTC保留端口 | 规划GPU3 | 已停止；等Sync结果后原端口升级 |
| 7896 | native binary WebSocket候选 | 原GPU7 | 已停止并保留历史容器，不再作为操作入口 |
| 7894 | 重复compile诊断副本 | 原GPU6 | 已停止并保留可恢复容器，不再监听 |

GPU1阶段的7896候选首次compile约20.08秒；暖态无外部重负载时10次P50/P95/max为163.4/174.0/178.1ms，模型段P50约137.8ms。RTC binary合同也已离线通过，但本轮正式运行只放行GPU7上的Sync。

GPU7上的7896候选首次compile约19.73秒；10次暖态请求P50/P95/max为158.5/182.3/188.1ms，模型段P50约139.7ms，未复现秒级长尾。随后把进程从兼容Sync+RTC收紧为纯Sync启动参数，并最终恢复到原正式端口7892。7892重新compile约20.03秒，3次暖态请求P50/P95/max为204.4/204.7/204.7ms，`50×34 float32`合同通过，模型段P50约141.2ms，容器无重启/OOM。该数字是6000D到本机服务的基准，不等于G2真实observation→Control；真实端到端结果必须由本轮10秒Powered Sync日志确认。

## 4. 尚未解决的独占问题

GPU1迁移时已有用户`hwy_100878104`的`V5_pico_teleop_g2.py`创建约230MiB CUDA上下文。迁移后用户`zxl_100881968`又启动`evaluate_f1_rlt_dataset_replay.py`，占GPU1约9.4GB。后者存在时，7896的10次暖态P50/P95/max退化到260.0/294.0/303.7ms，模型段P50约218.5ms。

因此“所有服务放在GPU1”虽可运行但不再采用。未经进程所有者协调不得终止他人任务；任何Powered Sync前必须确认GPU7除约230MiB跨卡上下文外没有外部重计算进程，并重新做暖态基准。RTC将来单独使用GPU3但保持原端口7893，避免与Sync共享服务进程和故障域，也不再增加端口记忆成本。
