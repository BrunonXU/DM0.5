# 推理记录索引

当前框架、RTC参数、延迟和平滑统一查阅[`F1_INFERENCE_CURRENT`主文档](../INFERENCE_RECIPE.md)。现场要运行时直接打开[STARTUP](STARTUP.md)，运行与验收看[STATUS](../STATUS.md)。

下面按问题集中列出证据。历史HTTP、绝对关节smoke和旧模型入口只用于追溯；你无需从这些报告重新拼出当前配置。

## 按问题查证

| 问题 | 入口 |
|---|---|
| 最新各部分延迟、RLinf组件是否替换 | [2026-10-23离线优化结论与数据表](experiments/20261023-rlinf-offline.md) |
| RTC与延迟如何演进、哪些候选没采用 | [RTC汇总实验](experiments/20261019-rtc.md) |
| 新Full625/1K部署与shadow | [任务状态](../STATUS.md)、[安装回执](../artifacts/20261021-full-h20/installation.json)、[验收回执](../artifacts/20261021-full-h20/acceptance.json) |
| Full250的6000D回退 | [Full250验收](experiments/20261020-full-250-6000d.md) |
| 真机模型对比与IK | [Eval结论](../Eval/README.md)、[首次拒绝前配对重放](experiments/20261019-prerejection-paired-replay.md) |
| IK保持、重试、暂停恢复 | [恢复状态机记录](experiments/20261016-ik-recovery.md) |
| telemetry包含哪些证据 | [telemetry记录](experiments/20261016-telemetry.md) |
| checkpoint绑定与隧道错误 | [绑定排查](experiments/20261019-checkpoint-binding-tunnel.md) |

## 原理与历史

[Why&How当前推理](http://192.0.2.1:4243/?doc=f1-inference) · [倍速教学动画](http://192.0.2.1:4243/?html=rtc-speed)。教学中的候选机制不自动等于上线功能，尤其倍速与训练时RTC。

[整理前推理recipe完整快照](experiments/20261021-inference-recipe-snapshot.md)保留历史推导；最早HTTP/绝对关节首页保存在[原文留证](../artifacts/20261021-doc-review/before/tasks/F1_ComplexyToys/inference/README.md.txt)，不再作为当前合同。
