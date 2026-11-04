# H20 多人使用、环境隔离与文档同步

> 更新：2026-10-15（北京时间）。账号与登录检查约 11:03。
> 当前资源见 [系统档案](SYSTEM_PROFILE.md)，连接配置见 [通信说明](README.md)。

## 1. 是否需要再创建账号

**两个人应各用一个独立 Linux 普通账号。你已经有 `user`，不用再创建自己的“子账号”。** Linux 的用户机制已经存在，不需要安装另一套账号系统。独立用户有各自的 HOME、shell 配置、SSH 密钥和文件权限。[Ubuntu 用户管理说明](https://ubuntu.com/server/docs/how-to/security/user-management/)

本次系统账号查询结果：

| 账号 | UID | HOME | 可见组 | 本次判断 |
| --- | ---: | --- | --- | --- |
| root | 0 | `/root` | root | 系统管理账号，不适合两人共同日常开发 |
| opsadmin | 1000 | `/home/user` | sudo、adm、lxd 等 | 已有管理用途账号，不能据名称认定属于同事 |
| huaqin | 1001 | `/home/user` | sudo | 已有账号；归属、用途未确认 |
| opsuser | 1002 | `/home/user` | sudo | **名字带 user 也仍在 sudo 组** |
| queen | 1003 | `/home/user` | sudo | 已有账号；归属、用途未确认 |
| user | 1004 | `/home/user` | 仅本人同名组 | 当前通过 6000D 使用的个人普通账号 |

这些是现场已有配置，不是 Ubuntu 为每位同事自动生成的独立开发环境。没有修改任何已有账号、组或管理员权限。

### 当前是谁用哪个账号

- 当前 `who` 有一个 root 交互登录，来源 `192.0.2.35`，开始于 2026-10-14 16:28；`loginctl` 也能看到 root 会话。
- 当前可见 `code-*` 等 VS Code 相关进程的所有者是 **root**。
- 最近登录历史有 root 与 opsadmin；opsadmin 曾通过 `192.0.2.36` 登录。
- **这些证据只能确认 Linux 账号和来源地址，不能确认具体是哪一个人。** root 会话也可能是之前保留的个人管理会话；不据此断言“同事正在用 root”。

同事若已有本人专属账号，应复用并确认目录权限；若一直共享 root/opsadmin，建议给同事新建一个以本人标识命名的普通账号，配独立公钥和 `/u01/<账号>`。尚未提供同事账号归属/拟用名称，本次没有替其占用已有运维账号或创建猜测用户名。
创建用户及分配顶层 `/u01/<账号>` 需管理员能力；当前 xza 账号无此权限。后续由已有受权管理会话完成即可，不意味着必须重新申请整套 SSH 接入。

### 如果同事继续用 opsuser，不新建账号

用户补充：两人最初领到的都是 opsuser。**现在你已改用 user，他继续用 opsuser，也已经是两个不同的 Linux 用户；他的重新建号不是你隔离个人环境的前提。** 不要再把日常训练放回共用的 opsuser HOME/环境即可。

- 他只改 `/home/user/.bashrc`、自己的 venv/Conda 包，一般不会改到你的配置和本任务独立环境。
- 若两人仍共用 opsuser，则 `.bashrc`、用户级 pip、HOME 文件都可能互相影响；仅另建目录不够。
- opsuser 当前在 sudo 组。若他获得管理员权限后改驱动、系统包、`/etc`、挂载或重启整机，你仍可能受影响；个人账号不能约束 root。
- 同时占用 GPU、CPU、内存或耗尽 `/u01` 空间也会互相影响。这些需要约定或额外资源调度，不能用一个 venv 解决。

## 2. 能隔离什么，不能隔离什么

| 层次 | 建议方式 | 边界 |
| --- | --- | --- |
| 用户配置、SSH、文件 | 两个普通账号、各自 HOME、各自密钥 | 已有 root / sudo 管理能力的人仍可能修改其他用户或系统 |
| Python 包与项目依赖 | 每人、每个项目独立 venv/Conda/等效环境 | 不共用系统 pip，不在同一个环境反复升级不同项目的包 |
| 下载与编译缓存 | `/u01/<本人>/cache`；编译缓存按环境版本再分目录 | 共用一个可写编译缓存仍可能有版本冲突 |
| 数据、输出、临时文件 | 各自目录、每次训练独立 run-id | 同一文件系统容量仍共享，建目录不等于配额 |
| GPU | 训练前协调卡号、检查实时占用 | `CUDA_VISIBLE_DEVICES` 只是选卡和编号，不是权限隔离或锁 |
| CPU、内存、端口 | 每任务指定并发、内存预算、不同分布式端口 | 普通账号和 Python 环境不会自动设置硬件限额 |

Python `venv` 的目标是隔离 Python 包，不是建立虚拟机或隔离 GPU。[Python 3.10 venv 文档](https://docs.python.org/3.10/library/venv.html)
`CUDA_VISIBLE_DEVICES` 的语义是控制 CUDA 进程可见设备。[NVIDIA CUDA 环境变量说明](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html)

对目前两个人使用的规模，先把个人账号、项目环境、目录和 GPU 使用约定落实即可；要强制执行资源限额时再评估调度器/cgroup/容器设备限制。此次没有安装 Slurm、Docker 或启用 MIG。

### GPU 和任务协作

- 每次先查看 `nvidia-smi`。看到卡空闲只表示此刻没有负载，不代表无人预约。
- 例如双方可以约定一人 0–3、另一人 4–7；这只是可选约定，没有长期分配 GPU。后续环境验收曾短时使用当时空闲的 GPU7，已退出并核对释放，见 [运行验收](experiments/20261015-bootstrap/README.md)。
- 使用 `CUDA_VISIBLE_DEVICES` 后进程内 GPU 通常会重新从 0 编号，不要将训练日志中的进程内编号直接当成物理卡号。
- 每个分布式训练使用不同的 `MASTER_PORT` / rendezvous 端口，避免两个 torchrun 任务同时争默认端口。
- 保存 run-id、GPU列表、PID、启动时间、日志路径和 checkpoint 路径；不终止不属于自己任务的进程。
- 单机 NVLink 与跨机 InfiniBand 是不同链路。当前 4 个 RDMA 口为 DOWN，不能照搬多机训练假设。

## 3. 当前还缺哪些安装和设置

**本节下表保留本日搭建前的核查清单；随后已完成双环境、基础工具、个人FFmpeg、base模型和缓存准备，当前结果以 [H20 任务环境](ENVIRONMENT.md) 为准。** 不能再根据系统默认 Python 无 pip 判断个人训练环境未安装。正式 F1 数据/reader 接入及多人资源协调仍未被环境安装替代。

机器已不是“完全裸机”：驱动、Fabric Manager、SSH、编译基础工具、git、rsync、tmux、curl/wget、zip/unzip、基础图形运行库已存在。应复用它们。

| 优先级 | 事项 | 当前情况 / 下一步 |
| --- | --- | --- |
| 必需 | 个人 Python 环境 | 当前 Python 3.10.12 无 pip、ensurepip；虽然能找到 venv 模块，但 Ubuntu 的 python3-venv/python3.10-venv 软件包未安装，不能假定 `python3 -m venv` 能完整引导 pip |
| 必需 | 确定一个环境管理方案 | 可以由管理会话补齐对应版本的 venv 支持后创建个人环境，或使用用户目录下独立的环境管理工具；不需要为了隔离同时安装多套管理器 |
| 必需 | 按首个训练项目锁定依赖 | 再确定 Python/PyTorch/CUDA runtime/transformers 等版本。当前没有确定训练项目版本，本次未盲装最新版 |
| 必需 | 大文件缓存与临时目录 | 已提供下节的任务环境脚本，显式加载后放入个人 `/u01`，避免默认缓存或解压占满根分区 |
| 必需 | GPU、端口、目录约定 | 与同事协调；目录分开并不自动分配卡号或磁盘额度 |
| 按需 | CMake、Ninja、python3-dev | 系统检查未安装；编译扩展时按项目补齐，不是所有预编译训练包都需要 |
| 按需 | FFmpeg | 未安装；处理视频数据或项目要求时再补齐 |
| 按需 | CUDA Toolkit/nvcc | 当前未发现；仅在项目需要本地 CUDA 编译时明确版本。驱动报告 CUDA 12.8 不等于必须全局安装 Toolkit 12.8 |
| 运行前 | 包/模型获取渠道 | 当前 H20 出网能力未重新验收。可在 6000D 准备兼容的离线包再 rsync，需匹配 H20 的 Linux x86_64、Python版本与目标依赖 |
| 长期 | 备份、容量、硬件配额 | 冗余/备份/目录配额尚未确认；关键原始数据保留来源，共享卷留足余量 |

禁止把 `sudo pip install` 或在两个人共用的 root 环境中升级依赖作为默认做法。驱动是整机共用层，本次已可工作；项目环境问题不应先通过重装驱动解决。
上表的任务环境安装与基础 smoke 随用户后续授权已执行，详见上述任务环境记录。硬件共享约定、备份及实际F1数据验收仍需分别处理。

## 4. 个人任务缓存配置

已提供 [job_env.sh](scripts/job_env.sh)。它只作用于显式 source 的当前 Bash 进程及子进程，不写 `.bashrc`，不设置 GPU 分配，不修改系统环境。
脚本确认当前账号及 `/u01` 挂载状态，然后在个人目录创建缓存和临时子目录。环境标签用于分开编译缓存；更换 Python/PyTorch/CUDA 组合时换一个标签。

2026-10-15 11:13（北京时间）已在 H20 用 `infra-check-v1` 完成实际验收：8 项目录变量均指向个人 `/u01`，目录归属 UID 1004、权限 700，临时文件读写及自动清理通过。目录已创建；环境变量仅在该验收 SSH 进程中加载，不会自动影响后续终端。见 [验收结果](experiments/20261015-multiuser/job-env-check.json)。

在 **H20 的个人 Bash 训练终端**运行（先由 6000D `ssh h20-train` 进入）：

```bash
source /home/user/projects/infra/h20/scripts/job_env.sh demo-env-v1

# 检查仅本进程的路径设置；不触发模型下载
printf 'HF_HOME=%s\nTMPDIR=%s\nTORCH_EXTENSIONS_DIR=%s\n' \
  "$HF_HOME" "$TMPDIR" "$TORCH_EXTENSIONS_DIR"
```

设置的变量：`HF_HOME`、`PIP_CACHE_DIR`、`XDG_CACHE_HOME`、`TORCH_HOME`、`TORCH_EXTENSIONS_DIR`、`TRITON_CACHE_DIR`、`CUDA_CACHE_PATH`、`TMPDIR`。
这不能保证每个第三方工具都遵循这些变量；项目自身的 dataset、日志和 checkpoint 路径仍需显式写进配置。

## 5. 双端文档如何保持一致

采用 **6000D 编辑 → H20 镜像**，避免两端独立编辑后互相覆盖。

| 内容 | 6000D 权威源 | H20 镜像 |
| --- | --- | --- |
| infra 目录 | `/home/user/projects/infra/` | `/home/user/projects/infra/` |
| Agent 入口 | Projects 根 `AGENTS.md` | H20个人 Projects 根 `AGENTS.md` |
| 被入口引用的规则 | 根 `PROJECT_RULES_AND_PREFERENCES.md` | 同名相对路径 |

整个 Projects 的代码、数据、模型、`.ssh`、个人 shell 配置不在本次同步范围。脚本 [sync_docs.py](scripts/sync_docs.py) 检查文件清单与 SHA256：

- 默认只比较；`--apply` 才写入 H20。
- 发现 H20 存在未知文件、独立修改或符号链接时停止，先人工合并；不自动删除或覆盖这些内容。
- 完成后逐文件校验 SHA256，并将清单和时间写入 **H20** 的 `/home/user/projects/.infra-sync-state.json`。
- `__pycache__`/`.pyc` 不属于源文档；H20镜像里出现额外文件会提示检查，避免悄悄不一致。
- 不是后台自动同步服务。后续修改 infra/入口规则后，执行同步命令并检查结果。
- 同步期间停止编辑两端这些文件。改动保护是同步前检查，不是并发锁；不能保证保留检查后、复制前出现的远端编辑。

这份镜像位于你的个人 HOME，当前 HOME 权限为 700；同事的普通账号不会自动获得读取权限。若将来需要两人共用 infra，应另外确定受控共享目录，本次不放宽个人 HOME 权限。

在 **6000D Bash** 执行：

```bash
cd /home/user/projects

# 只读比较
python3 infra/h20/scripts/sync_docs.py

# 同步并保存本次独立回执；回执放在infra之外，避免自引用哈希
H20_SYNC_RECEIPT="/tmp/h20-infra-sync-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 infra/h20/scripts/sync_docs.py --apply --receipt "$H20_SYNC_RECEIPT"
```

H20 上直接查看文档：

```bash
# 6000D 执行，内容来自H20的镜像
ssh -o BatchMode=yes h20-train 'sed -n "1,65p" /home/user/projects/infra/h20/SYSTEM_PROFILE.md'
```

同步与个人目录写操作不需要 sudo；更改别人的账号、系统包或全机资源策略是另一类管理操作，不混入文档同步。

## 6. 现场证据

- [2026-10-15 账号与环境核查记录](experiments/20261015-multiuser/README.md)
- [账号、会话、进程所有者与包状态原始结果](experiments/20261015-multiuser/accounts.json)
- 最新镜像版本以 H20 的 `.infra-sync-state.json` 和重新运行脚本的比较结果为准；历史同步回执只证明记录时刻。
