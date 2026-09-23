# DM0.5 × 智元 G2 双臂灵巧手分拣

本项目使用固定版本 OpenDM/DM0.5，通过 LoRA 适配 G2 双臂灵巧手数据，最终
在真机验证“左手抓瓶、交接右手、分别放入托盘区域”的完整任务。

## 当前状态

项目已从OldLora训练阶段推进到NewLora 75K真机、五步fixed-prefix compile与RTC控制架构优化。实时事实和下一步只在 [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) 维护；本README不复制易过时的服务、checkpoint或进度。

## 目录入口

| 目录 | 内容 |
|---|---|
| `docs/` | 原理、架构、证据、实验、事故、runbook和当前状态；先读其中AGENTS.md |
| `g2_sort/` | G2项目专用训练、数据、部署和验证代码 |
| `data/` | 真实样本、转换产物、审计报告和manifest |
| `infra/` | 本项目Docker、宿主机配置和额外依赖 |
| `opendm/` | 固定commit的上游仓库及必要34D核心补丁 |

## 文档导航

从 [`docs/README.md`](docs/README.md) 进入；写文档前必须读 [`docs/AGENTS.md`](docs/AGENTS.md)。旧数字编号材料已分类迁移，迁移关系见 [`docs/archive/README.md`](docs/archive/README.md)。

## 项目镜像

从项目根目录构建精简开发镜像：

```bash
docker build -f infra/docker/Dockerfile.opendm-g2 \
  -t opendm:g2-34d-6ce8e7f .
```

基线镜像跳过FlashAttention并使用eager/SDPA，便于兼容性验证和故障回退。
RTX 6000D正式训练使用固定官方镜像和服务器实测兼容组合：LLM `sdpa`、
视觉`flash_attention_2`、动作专家`sdpa`。只替换无法在本机完成backward的
LLM Flex内核；模型权重、训练目标和视觉FA2保持官方实现。Docker镜像不包含
DM0.5 base权重。
