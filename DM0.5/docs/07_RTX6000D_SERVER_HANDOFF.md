# DM0.5 × G2：RTX 6000D服务器执行交接

更新时间：2026-07-28

## 1. 用途

本文是新会话接手服务器工作的执行入口，只保存“先读什么、在哪里做、如何
恢复、下一步是什么”。项目目标、数据证据、冻结配置、踩坑和服务器资产不在
这里重复。

本文不保存用户名、密码、token、共享盘主机名或其他凭据。

## 2. 接手阅读顺序

1. `02_IMPLEMENTATION_STATUS.md`：唯一实时状态；
2. `04_TRAINING_DEPLOYMENT.md`：冻结训练与部署合同；
3. `03_DATA_AUDIT.md`：数据事实和准入证据；
4. `08_RTX6000D_SERVER_AUDIT.md`：服务器实测资产；
5. `06_OPERATIONS_LESSONS.md`：已验证的踩坑处理；
6. `05_CUSTOMIZATION_MAP.md`：相对上游改造；
7. `01_REPRODUCTION_PLAN.md`：目标、路线与验收；
8. `g2_sort/config/action_contract.json`：34D机器可读最终合同。

如有冲突，遵循01中定义的权威顺序。不得用本文中的旧进度覆盖02。

## 3. 固定代码边界

- 项目仓库：`BrunonXU/DM0.5`；
- 项目分支：`main`；
- OpenDM固定commit：
  `6ce8e7f13e0c46906b141fda1773364f18a577c2`；
- 项目镜像标签：`opendm:g2-34d-6ce8e7f`；
- 默认attention：eager/SDPA；
- 不跟随OpenDM最新`main`。

每次实验前记录：

```bash
git rev-parse HEAD
git status --short --branch
docker image inspect opendm:g2-34d-6ce8e7f
```

不得覆盖未知工作树修改。

## 4. 项目与存储入口

项目根目录：

```text
/home/xza_100850113/projects/DM0.5
```

选定NVMe物理目录通过bind mount统一暴露为：

```text
DM0.5/server/
├── raw_archives/
├── staged_episodes/
├── processed/
│   ├── videos/
│   ├── jsonl/
│   ├── audits/
│   └── manifests/
├── model_cache/
├── checkpoints/
└── runs/
```

共享盘只读入口：

```text
/mnt/dm05_source/具身/数采数据/sort_g2
```

原则：

- 所有模型训练相关内容从`DM0.5`项目管理；
- 大文件只进入`server/`；
- 不从SMB训练；
- 不向共享盘写文件；
- 不把`server/`内容提交Git；
- 新目录必须带README说明用途和可恢复性。

## 5. 会话恢复检查

进入项目后先执行只读检查：

```bash
git status --short --branch
findmnt -T server
findmnt -T /mnt/dm05_source
df -h server
nvidia-smi
docker ps
docker images opendm:g2-34d-6ce8e7f
```

再检查后台任务和目标大小：

```bash
ps -eo pid,stat,etime,cmd | \
  rg 'download_dm05_base|rsync .*record|index_tar_archive|docker build'

stat -c '%n %s' \
  server/raw_archives/record.tar.gz \
  server/raw_archives/record_20260409.zip \
  server/raw_archives/record_20260413.tar.gz \
  server/model_cache/Dexmal/DM05/model.safetensors
```

进度以实际目标字节数为准，不以聊天中的旧百分比为准。

## 6. 数据迁移

精确源大小：

| 文件 | bytes |
|---|---:|
| `record.tar.gz` | 50,394,926,225 |
| `record_20260409.zip` | 193,356,130,953 |
| `record_20260413.tar.gz` | 215,996,415,546 |

复制必须使用支持断点的参数：

```bash
rsync \
  --human-readable \
  --info=progress2 \
  --partial \
  --append-verify \
  --no-perms --no-owner --no-group \
  SOURCE DESTINATION
```

本服务器实测两个不同归档并发优于单流，经验和安全边界见06。绝不能让两个
进程写同一目标文件。每个归档完成后：

1. 核对精确字节数；
2. 做归档可读性检查；
3. `chmod a-w`设为只读；
4. 从本地NVMe启动索引。

tar.gz索引示例：

```bash
python -m g2_sort.data_tools.index_tar_archive \
  server/raw_archives/record.tar.gz \
  --output server/processed/manifests/record.index.json
```

报告采用原子落盘。不要在SMB上反复扫描压缩tar。

## 7. Base下载

下载入口：

```bash
./infra/host/download_dm05_base.sh
```

要求：

- 固定脚本中记录的Hugging Face revision；
- 保存到`server/model_cache/Dexmal/DM05/`；
- 支持续传；
- 完成后校验所有必需文件、主权重精确大小和LFS SHA256；
- 不提交权重。

## 8. Docker验收

构建命令：

```bash
docker build \
  -f infra/docker/Dockerfile.opendm-g2 \
  -t opendm:g2-34d-6ce8e7f .
```

镜像完成后按顺序：

1. 记录image ID和大小；
2. 容器查询PyTorch、CUDA和8卡可见性；
3. 运行`g2_sort/tests`；
4. 挂载当前工作树运行新加入的测试；
5. 运行Git内真实smoke loader。

FlashAttention不阻塞上述验收。

## 9. Base完成后的模型smoke

严格按照04的十步验收顺序执行，不从多GPU训练开始。最小顺序：

```text
官方demo
→ G2单batch forward
→ backward与第33/34维梯度
→ 10～100 step LoRA
→ checkpoint保存与新进程恢复
→ HTTP 50×34
```

每次失败都保留实际命令、配置、日志和最小复现。

## 10. 数据完成后的处理

1. 归档级索引；
2. 一次性提取训练必需字段；
3. episode H5、时间戳和视频轻量审计；
4. 分层画面抽查；
5. 冻结有效manifest；
6. 生成固定800 train / 80 validation分层split及hash；
7. 只用train split计算norm stats；
8. 转换三路MP4和显式34D JSONL；
9. OpenDM loader抽样复验。

不人工逐帧观看全部视频，也不解压与训练无关的传感器。

## 11. 正式训练门槛

以下全部满足后才能启动50k正式训练：

- base校验完成；
- 真实forward/backward和短LoRA通过；
- checkpoint round-trip通过；
- 最终manifest、split和norm stats冻结；
- 500～1000 step多卡基准完成；
- 实际global batch、吞吐、显存和通信记录完整。

4卡和8卡都合法，按实测选择，不按GPU型号猜训练时间。

## 12. 安全边界

在G2 SDK索引、单位、命令频率、限位、急停和watchdog核对完成前：

- 只允许shadow mode；
- 不向执行器发送模型动作；
- 不开环执行完整50步chunk；
- 不因训练数据可读就推断真机映射正确。

Thor移植、EEF、relative action、多prompt和额外传感器均不属于当前主线。
