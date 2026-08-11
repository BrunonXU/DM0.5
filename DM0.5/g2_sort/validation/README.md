# Validation utilities

本目录存放可重复执行的服务器验收脚本：

- `nccl_smoke.py`：验证多卡 NCCL 通信；
- `base_model_smoke.py`：用三路合成图像和 34D 动作执行 DM0.5
  Base/LoRA 前向，可选反向；
- `validate_checkpoint.py`：检查项目 checkpoint 的结构和关键元数据。
- `evaluate_real_loss.py`：在固定真实样本和固定 flow 噪声上比较 base/LoRA loss。

这些脚本只产生临时张量和终端日志，不修改模型权重或训练数据。
