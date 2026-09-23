# Validation utilities

本目录存放可重复执行的服务器验收脚本：

- `nccl_smoke.py`：验证多卡 NCCL 通信；
- `base_model_smoke.py`：用三路合成图像和 34D 动作执行 DM0.5
  Base/LoRA 前向，可选反向；
- `validate_checkpoint.py`：检查项目 checkpoint 的结构和关键元数据。
- `evaluate_real_loss.py`：在固定真实样本和固定 flow 噪声上比较 base/LoRA loss。
- `newlora_common.py`：冻结 NewLora 的 NoAug、14D手臂相对+20D手绝对语义、
  train-only norm stats 与 mixed-to-absolute 还原合同；
- `evaluate_NewLora_fm.py`：对固定80条 validation episode 计算可配对的 NewLora
  held-out FM loss；
- `evaluate_NewLora_actions.py`：使用部署一致的 Euler sampler 完整生成 `50×34`
  chunk，并输出 episode-level 动作、稳定性与安全诊断指标。

这些脚本只产生临时张量和终端日志，不修改模型权重或训练数据。
