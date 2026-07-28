# DM0.5 × 智元 G2 双臂灵巧手分拣

本项目使用固定版本 OpenDM/DM0.5，通过 LoRA 适配 G2 双臂灵巧手数据，最终
在真机验证“左手抓瓶、交接右手、分别放入托盘区域”的完整任务。

## 当前状态

- OpenDM 固定于 commit `6ce8e7f13e0c46906b141fda1773364f18a577c2`；
- 34D absolute-joint合同、ZIP/episode数据工具和模型动作头适配已实现；
- 本地镜像 `opendm:g2-34d-6ce8e7f` 已通过4090D、单元测试和真实数据smoke；
- 本地未下载DM0.5 base权重；
- 正迁移到RTX 6000D服务器；tar全量工具、全量归档处理、正式训练和真机
  接入尚未开始。

## 目录入口

| 目录 | 内容 |
|---|---|
| `docs/` | 主计划、实施状态、数据审计、运行合同和改造索引 |
| `g2_sort/` | G2项目专用训练、数据、部署和验证代码 |
| `data/` | 真实样本、转换产物、审计报告和manifest |
| `infra/` | 本项目Docker、宿主机配置和额外依赖 |
| `opendm/` | 固定commit的上游仓库及必要34D核心补丁 |

优先阅读：

1. [RTX 6000D服务器迁移与新对话交接](docs/07_RTX6000D_SERVER_HANDOFF.md)
2. [主计划](docs/01_REPRODUCTION_PLAN.md)
3. [当前状态](docs/02_IMPLEMENTATION_STATUS.md)
4. [数据审计](docs/03_DATA_AUDIT.md)
5. [训练与部署合同](docs/04_TRAINING_DEPLOYMENT.md)
6. [相对上游改造索引](docs/05_CUSTOMIZATION_MAP.md)

## 本地镜像

从项目根目录构建精简开发镜像：

```bash
docker build -f infra/docker/Dockerfile.opendm-g2 \
  -t opendm:g2-34d-6ce8e7f .
```

本地默认跳过FlashAttention并使用eager/SDPA。服务器型号确定后，才按需添加
`--build-arg INSTALL_FLASH_ATTN=1`。Docker镜像不包含DM0.5 base权重。
