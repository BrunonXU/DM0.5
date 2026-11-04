---
doc_type: reference
domain: server
status: frozen
created: 2026-07-28
updated: 2026-08-24
models: [dm05, old_lora]
hardware: [rtx6000d]
canonical_for: [rtx6000d_initial_asset_audit]
supersedes: [08_RTX6000D_SERVER_AUDIT.md]
superseded_by: []
related: [incidents/]
---

# RTX 6000D服务器资产与实施记录

更新时间：2026-07-28

## 1. 文档职责

本文保存服务器实测硬件、软件、存储和已完成事件。实时任务进度只写入
`../CURRENT_STATUS.md`，故障复盘写入`incidents/`。

本文不保存密码、token、共享盘主机名或其他凭据。

## 2. 代码基线

| 项目 | 实测值 |
|---|---|
| 私有仓库 | `BrunonXU/DM0.5`，访问已验证 |
| 分支 | `main` |
| 服务器初始HEAD | `d70aa4dd1ad44ad629a1b5ab93e126cf2774d2f9` |
| OpenDM commit | `6ce8e7f13e0c46906b141fda1773364f18a577c2` |

初始clone工作树干净。后续实验必须记录当时实际HEAD和diff，不能把初始HEAD
当作永久当前版本。

## 3. 计算资源

| 项目 | 实测结果 |
|---|---|
| GPU | 8 × NVIDIA RTX 6000D |
| 单卡显存 | 85,651 MiB，约83.6 GiB |
| Compute Capability | 12.0 |
| 驱动 | 580.105.08 |
| 驱动报告CUDA | 13.0 |
| 项目容器CUDA | 12.8 |
| GPU互联 | PCIe，无NVLink |
| CPU | 2 × AMD EPYC 9575F |
| CPU规模 | 128核/256线程 |
| NUMA | 2节点 |
| RAM | 1.5 TiB，体检时约1.3 TiB available |

拓扑：

- GPU 0–3属于NUMA 0；
- GPU 4–7属于NUMA 1；
- 相邻卡部分为`PIX`，同NUMA其他组合为`NODE`，跨NUMA为`SYS`；
- 8卡没有NVLink，因此必须用500～1000 step基准测通信开销。

GPU占用是实时状态，不能从本文推断；每次启动实验前运行`nvidia-smi`。

## 4. 存储

系统根盘：

- ext4，约3.4TB；
- 体检时只剩约276GB；
- 只放代码和小文件，不放原始数据、base或checkpoint。

项目NVMe：

| 项目 | 实测值 |
|---|---|
| 设备 | `/dev/nvme3n1p1` |
| 文件系统 | ext4 |
| 容量 | 约3.5TB |
| 体检时可用 | 约3.3TB |
| 物理挂载 | `/media/user/bd6a10fd-b6fe-428e-ade2-7f3130ea902a` |
| 项目入口 | `/home/user/projects/DM0.5/server` |

物理NVMe目录通过bind mount暴露为项目内`server/`。目录说明模板保存在
`infra/layout/nvme/`，`server/`被Git忽略。

## 5. 网络与共享盘

| 项目 | 实测值 |
|---|---|
| 活跃物理网卡 | `enx00e04c0b3fb8` |
| 链路速率 | 1Gbps |
| 共享协议 | CIFS / SMB 3.0 |
| 挂载模式 | 只读 |
| CIFS读写块参数 | `rsize=4194304,wsize=4194304` |
| 本地入口 | `/mnt/dm05_source` |

三个源归档的精确字节数已与03中的数据清单核对一致。共享盘只作为源，
训练和数据处理只在本地NVMe进行。

## 6. Docker与模型环境

| 项目 | 实测值 |
|---|---|
| Docker CLI/Engine | 29.3.0 |
| NVIDIA Container Toolkit | 1.19.0 |
| NVIDIA runtime | 已注册并验证 |
| 项目镜像标签 | `opendm:g2-34d-6ce8e7f` |
| 项目镜像ID | `sha256:REDACTED_HOST_FINGERPRINT` |
| FA2镜像标签 | `opendm:g2-34d-fa2` |
| FA2镜像ID | `sha256:REDACTED_HOST_FINGERPRINT` |
| 官方镜像 | `docker.1ms.run/dexmal/opendm:latest`，ID `sha256:60a754dce047...` |
| PyTorch | 2.11.0+cu128 |
| 可训练attention | LLM SDPA、视觉FlashAttention2、action SDPA |

Docker socket最初因账号不在`docker`组而不可访问；组权限和新登录会话完成后
已恢复。已有容器实测能够识别8张RTX 6000D。

## 7. 服务器目录

```text
DM0.5/server/
├── raw_archives/       原始归档，完成校验后只读
├── staged_episodes/    训练必需字段
├── processed/
│   ├── videos/
│   ├── jsonl/
│   ├── audits/
│   └── manifests/
├── model_cache/        DM0.5 base
├── checkpoints/        LoRA与34D密集模块
└── runs/               日志、配置和评估证据
```

## 8. 已完成事件

| 时间 | 事件 | 结果 |
|---|---|---|
| 2026-07-28 | 私有仓库clone与基线记录 | 通过 |
| 2026-07-28 | 8卡、驱动、CPU、RAM和拓扑体检 | 通过 |
| 2026-07-28 | NVMe选择及项目内bind mount | 通过 |
| 2026-07-28 | Docker组权限和NVIDIA runtime验证 | 通过 |
| 2026-07-28 | SMB只读挂载及三个源文件大小核对 | 通过 |
| 2026-07-28 | 8张GPU重新核查 | 全部空闲 |
| 2026-07-28 | 项目Docker镜像构建 | 完成 |
| 2026-07-28 | 新镜像8卡可见性与当前工作树回归测试 | 8卡；5 passed |
| 2026-07-28 | GPU 0/1/4/5四卡NCCL all-reduce smoke | 通过 |
| 2026-07-28 | `record.tar.gz`复制与精确大小校验 | 完成 |
| 2026-07-28 | `record.tar.gz`单遍索引 | 57条，55条预检查通过 |
| 2026-07-28 | 长下载从IDE子进程迁移到用户级systemd | 自动重试且不依赖IDE |
| 2026-07-28 | DM0.5 base固定revision与SHA256复核 | 通过 |
| 2026-07-28 | FA2镜像及RTX 6000D视觉FA2前反向 | 通过 |
| 2026-07-28 | 官方镜像拉取和LLM Flex backward复测 | 镜像完成；Flex受shared-memory上限失败 |
| 2026-07-28 | 单episode 20步LoRA训练与新进程加载 | 通过 |
| 2026-07-28 | 固定真实样本base/LoRA loss对照 | 1.4818降至0.7440 |
| 2026-07-28 | checkpoint-20恢复并续训至22步 | 通过 |
| 2026-07-28 | 三归档训练必需字段提取 | 887/887，约97GB，零失败 |
| 2026-07-28 | 24进程完整数据审计 | 887完成；880有效、7排除 |
| 2026-07-28 | 分层split、转换和train-only norm stats | 800 train / 80 validation；880/880转换 |
| 2026-07-28 | 8卡global batch 32正式门禁 | 500/500；19.83 samples/s；无OOM/非有限值 |
| 2026-07-28 | checkpoint-500合同、新进程加载 | 通过 |
| 2026-07-28 | 新容器从500断点续训至502 | 通过 |
| 2026-07-28 | 50K正式训练在`dm05-50k` tmux启动 | 单一容器；8卡global batch 32 |
| 2026-07-29 | 正式checkpoint 5K/10K/15K/20K合同复验 | 全部通过；完整恢复状态 |
| 2026-07-29 | 正式日志回填并实时导出TensorBoard | localhost:6006；不占GPU |
| 2026-07-29 | 31K阶段误杀tmux后无损接回Docker训练 | step连续；日志补齐；guardian启用 |

尚未完成事项不在本事件表中提前标记为成功，统一查看02。
