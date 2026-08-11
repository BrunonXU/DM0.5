# DM0.5 × 智元 G2 双臂灵巧手分拣

本项目使用固定版本 OpenDM/DM0.5，通过 LoRA 适配 G2 双臂灵巧手数据，最终
在真机验证“左手抓瓶、交接右手、分别放入托盘区域”的完整任务。

## 当前状态

- OpenDM 固定于 commit `6ce8e7f13e0c46906b141fda1773364f18a577c2`；
- 34D合同、模型动作头、ZIP/tar索引和episode数据工具已实现；
- RTX 6000D服务器、Docker、NVMe、SMB和8卡环境已打通；
- 数据、DM0.5 base、正式容器和8卡500-step门禁已冻结并通过；
- 8卡50K正式训练正在`dm05-50k` tmux会话运行；
- 实时进度只在`docs/02_IMPLEMENTATION_STATUS.md`维护。

## 目录入口

| 目录 | 内容 |
|---|---|
| `docs/` | 主计划、实施状态、数据审计、运行合同和改造索引 |
| `g2_sort/` | G2项目专用训练、数据、部署和验证代码 |
| `data/` | 真实样本、转换产物、审计报告和manifest |
| `infra/` | 本项目Docker、宿主机配置和额外依赖 |
| `opendm/` | 固定commit的上游仓库及必要34D核心补丁 |

## 文档导航

| 文档 | 唯一职责 |
|---|---|
| [01 项目目标与路线图](docs/01_REPRODUCTION_PLAN.md) | 目标、范围、阶段和验收 |
| [02 当前实施状态](docs/02_IMPLEMENTATION_STATUS.md) | 唯一动态进度页 |
| [03 数据审计](docs/03_DATA_AUDIT.md) | 数据来源、证据和统计 |
| [04 训练与部署合同](docs/04_TRAINING_DEPLOYMENT.md) | 冻结模型、训练和安全边界 |
| [05 上游改造索引](docs/05_CUSTOMIZATION_MAP.md) | 相对OpenDM的代码变化 |
| [06 经验与踩坑](docs/06_OPERATIONS_LESSONS.md) | 已验证问题与处理方法 |
| [07 服务器执行交接](docs/07_RTX6000D_SERVER_HANDOFF.md) | 新会话恢复和执行步骤 |
| [08 服务器资产记录](docs/08_RTX6000D_SERVER_AUDIT.md) | 实测硬件、软件和事件 |
| [09 8卡500步门禁报告](docs/09_8GPU_500STEP_BENCHMARK.md) | 多卡基准、恢复和validation证据 |
| [10 正式50K训练记录](docs/10_FORMAL_50K_TRAINING.md) | 正式训练里程碑、checkpoint和监控 |
| [11 Checkpoint到真机闭环](docs/11_CHECKPOINT_SELECTION_TO_ROBOT.md) | checkpoint选型、离线验收和分级真机流程 |
| [12 G2 Client DM0.5运行时](docs/12_G2_CLIENT_DM05_RUNTIME.md) | 客户端模块、HTTP合同、30/100Hz调度和三种模式 |
| [13 Checkpoint选型证据](docs/13_DM05_G2_CHECKPOINT_VALIDATION_AND_SELECTION_DECISION.md) | 25K–50K验证方法、证据边界和35K/30K决策 |
| [14 50K到100K延长训练](docs/14_DM05_G2_50K_TO_100K_EXTENSION_TRAINING.md) | 独立延长实验的冻结训练合同 |
| [15 35K HTTP到G2操作手册](docs/15_G2_35K_HTTP_TO_ROBOT_RUNBOOK.md) | 实习生按阶段执行的网络、推理、上传和shadow验收 |

继续当前服务器工作时，先读02，再读07；需要判断配置时读04。

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
