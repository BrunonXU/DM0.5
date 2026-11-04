# 6000D ↔ H20：连接、数据同步与训练协作

> 当前连接的权威记录。更新日期：2026-10-20（北京时间；新增RTC对照入口，既有通信验收仍为历史证据）。
> 验收方法、速度和原始回执见 [2026-10-14 通信验收](experiments/20261014/README.md)。

后续 Agent 需要了解磁盘、CPU、内存、GPU、软件环境和权限时，先读 [H20 系统与资源档案](SYSTEM_PROFILE.md)（2026-10-15 只读盘点）。

多人账号、个人环境、缓存配置和双端文档同步见 [多人使用与环境准备](MULTIUSER_AND_SETUP.md)。本目录以6000D为编辑源，同步到H20个人 Projects 下的同名路径。

**积木任务已部署的双环境、官方 π0.5 base、下载渠道与激活命令见 [H20 任务环境](ENVIRONMENT.md)。** 后续优先复用此环境及锁文件；F1 数据适配和正式训练门槛单独保留。

已实测：个人账号免密 SSH、1 GiB 双向传输与 SHA256 校验、真实中断续传、SSH 退出后 tmux 任务存活均通过。当前样本上传 **38.58 MiB/s**，下载 **22.86 MiB/s**；测试数据已清理。

## 已决定的分工

- **6000D**：运行 Codex、修改代码、整理训练配置、做推理；由这里发起 H20 的 SSH 和文件传输。
- **H20**：保存数据集、运行训练、保存日志和 checkpoint。**不在 H20 部署 Codex**。
- **笔记本**：连接开发环境和查看结果；6000D 与 H20 之间的数据不经过笔记本。

```text
笔记本 ──SSH / VS Code──> 6000D（Codex、代码、推理）
                               │
                               ├──SSH──> H20：启动/查看任务
                               └──rsync over SSH──> H20：数据/代码
                                      <──────────── H20：日志/已完成 checkpoint
                                  TCP 192.0.2.27:6622
```

双向传文件均可由 6000D 发起。下载 H20 文件不要求 H20 主动连接 6000D 的 22 端口。
这里的 `/u01` 是 **H20 本机挂载的数据卷**；它没有自动挂载到 6000D。

## 当前可用连接

| 项目 | 当前值 |
| --- | --- |
| 发起机器 | 6000D，系统 hostname 为 `hq` |
| 6000D 账号 | `user` |
| 6000D SSH 别名 | `h20-train` |
| H20 地址 | `192.0.2.27` |
| H20 SSH 端口 | **6622** |
| H20 日常账号 | `user`，UID/GID `1004:1004` |
| H20 系统 hostname | `local`；不是判断机器身份的唯一依据 |
| 身份认证 | 6000D 的 ED25519 公钥已安装；个人账号批处理免密访问通过 |
| 系统管理权限 | 个人账号没有 sudo；本流程不需要 root |
| 传输程序 | 两端已有 `rsync` |

**所有下列 Bash 命令默认在 6000D 执行**，除非标注在 H20 执行。

```bash
cd /home/user/projects
ssh -o BatchMode=yes h20-train 'whoami; hostname; id; test -w /u01/user/staging && echo STAGING_WRITE_OK'

# 进入 H20 个人账号；退出返回 6000D
ssh h20-train
```

6000D 上已配置 `/home/user/.ssh/config`：

```sshconfig
Host h20-train
    HostName 192.0.2.27
    Port 6622
    User user
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ForwardAgent no
    StrictHostKeyChecking yes
    ConnectTimeout 10
    ConnectionAttempts 1
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

`ForwardAgent no` 不影响本次密钥登录：6000D 自己使用私钥认证；H20 不会获得使用本机 SSH agent 的能力。此链路无需转发笔记本的 VPN/7890 代理。H20 下载依赖是否有外网权限是另一项条件，不能从 SSH 成功推断。

### 主机身份与凭据位置

- 用户从 H20 受信任 root 会话提供的 **ED25519 主机公钥指纹**：
  `SHA256:REDACTED_HOST_FINGERPRINT`。
- 6000D 已记录 `[192.0.2.27]:6622` 的对应主机公钥到 `~/.ssh/known_hosts`。
- 6000D 登录公钥指纹：`SHA256:REDACTED_HOST_FINGERPRINT`。
- 登录私钥留在 **6000D 的 `~/.ssh/id_ed25519`**；公钥位于 H20 个人账号 `~/.ssh/authorized_keys`。不把私钥、密码、OTP 放入项目或同步包。
- `known_hosts` 修改前的备份：`~/.ssh/known_hosts.before_h20_train_20261014T060718`；文件名按当时主机时区生成。

两种指纹用途不同：主机指纹确认“连的是哪台机器”，登录公钥确认“哪个用户可以进去”。若再出现主机密钥变更，先通过可信 H20 会话核对新指纹，再定点更新；不要关闭严格检查或删除整份 `known_hosts`。

### 旧堡垒机入口与新入口的区别

以前使用 `company.example.invalid:54321`、复合用户名 `000000001/192.0.2.27/opsadmin`，由堡垒机执行密码和 OTP 登录。旧记录里的 exec 不支持、转发被拒、SFTP 断开，是当时该入口的结果。

**本流程使用 IT 后来开通的 `192.0.2.27:6622`。** 已验证标准远程命令和 rsync，不再依赖旧入口的 exec/转发能力。旧端口的指纹不能用于验证新端口。Windows 上的 `h20-BAOLEI` 是另一个本地别名，曾使用 root；不要与 6000D 的 `h20-train` 混用。

直连开通不等于取消所有审计。仍使用已授权的账号、端口和目录；此次验证没有调整防火墙、审计、sudo 或堡垒机策略。

## H20 文件放在哪里

| H20 路径 | 用途与状态 |
| --- | --- |
| `/home/user` | 个人 HOME，mode `700`，与同事的 shell 配置分开 |
| `/home/user/projects` | 个人代码；建议每次训练使用独立版本目录 |
| `/u01/user/datasets` | 数据集与冻结版本 |
| `/u01/user/runs` | 每次训练的配置、日志、checkpoint、版本记录 |
| `/u01/user/cache` | 模型/数据处理缓存；需要按实际框架配置路径 |
| `/u01/user/staging` | 接收区、临时包、传输验收 |

`/u01/user` 及四个子目录已经创建，所有者为 `user:user`，mode `750`。
`/u01` 为 `/dev/mapper/vg01-data` 的 ext4 数据卷。2026-10-15 已进一步确认底层为 8 块 NVMe SSD；容量、可用空间和存储拓扑统一维护在 [系统资源档案](SYSTEM_PROFILE.md#3-系统盘与数据盘的真实结构)，备份与冗余仍未确认。

大数据和模型缓存放 `/u01`，避免占满系统根分区。`/dev/shm`、其他 `tmpfs` 是内存文件系统，不是持久数据盘。
大批传输前先确认数据卷确实挂载：

```bash
ssh -o BatchMode=yes h20-train 'mountpoint /u01 && df -hT /u01 && test -w /u01/user/staging'
```

## 日常同步方式

已验证的是 **rsync 通过 SSH**，无需单独开放 rsync daemon 端口，也不依赖 SFTP 子系统。
先同步代码/小配置并验证训练环境，再传正式数据。下面是模板，需要把源路径和数据集版本替换为实际值；不要对整个 Projects 或 HOME 不加筛选地同步。

### 上传数据集：先预览，再传输

在 **6000D 的 Bash** 执行；源目录以 `/` 结尾表示同步其中内容。

```bash
# 替换这两个值；源数据应已停止写入，版本目录不要与现有训练共用
H20_DATA_SOURCE="/绝对路径/已经冻结的数据集"
H20_DATA_VERSION="替换为数据集版本名"

# 版本名只使用字母、数字、下划线、点、短横线；避免路径或 shell 字符
[[ "$H20_DATA_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "版本名不合法"; return 1 2>/dev/null || exit 1; }
test -d "$H20_DATA_SOURCE" || { echo "源目录不存在"; return 1 2>/dev/null || exit 1; }

ssh -o BatchMode=yes h20-train \
  "mountpoint -q /u01 && mkdir -p /u01/user/datasets/$H20_DATA_VERSION" \
  || { echo "挂载或目标目录检查失败"; return 1 2>/dev/null || exit 1; }

# 预览：-n 不传内容
rsync -anvi -e 'ssh -o BatchMode=yes' \
  "$H20_DATA_SOURCE/" \
  "h20-train:/u01/user/datasets/$H20_DATA_VERSION/"

# 确认预览后执行；网络中断时重跑同一条命令
ssh -o BatchMode=yes h20-train 'mountpoint -q /u01' &&
rsync -a --partial --partial-dir=.rsync-partial --info=progress2 \
  -e 'ssh -o BatchMode=yes' \
  "$H20_DATA_SOURCE/" \
  "h20-train:/u01/user/datasets/$H20_DATA_VERSION/"
```

同一版本目录用于恢复本次传输；新数据版本另起目录。默认不加 `--delete`，避免删除目标上已有文件；也不对 checkpoint 使用“只追加就算完成”的假设。
`--partial-dir` 保留未传完文件供下一次重用，具体行为见 [rsync 官方手册](https://download.samba.org/pub/rsync/rsync.1)。正式发布数据版本应有文件清单、总大小和完整性校验；退出码为 0 后再启动消费它的训练。

### 传代码、查看文件与取回结果

- 代码只传本次任务需要的源码、配置和启动脚本。排除 `.git`、本地环境、数据、缓存及权重；有相对导入/符号链接时确认依赖也包含在版本中。
- 正在训练的代码目录不要覆盖。记录源码 commit、未提交改动的快照/补丁、配置、环境版本与数据集版本，放到对应 `runs/<run-id>`。
- H20 的环境在 H20 配置；不要假定直接复制 6000D 的 Python 环境就可运行。
- 下载使用相同 rsync 参数，把 `h20-train:远端路径` 放到源位置即可。先确认 checkpoint 已写完；若需要推理，只取所需的已完成 checkpoint，不必回传整个数据集。

只查看文件，不上传内容：

```bash
ssh -o BatchMode=yes h20-train 'ls -lah /u01/user; ls -lah /home/user/projects'
rsync --list-only -e 'ssh -o BatchMode=yes' h20-train:/u01/user/runs/
```

6000D 上的长时间 rsync 同样建议放在 6000D 的 tmux 中，避免笔记本或 IDE 退出导致本地发送进程结束。断点续传用于恢复传输，不等于后台传输会自动重连。

## 长训练如何运行

先在 H20 上验证项目依赖、GPU 和启动参数，再在 **H20 的 tmux** 中运行项目自己的训练脚本：

```bash
# 6000D：进入 H20
ssh h20-train

# 以下在 H20 执行：先查看已有会话，按需要创建新的个人会话
tmux ls
tmux new-session -s train-main

# 进入会话后，切到本次冻结代码目录、激活已验证环境，执行项目训练命令。
# 把日志和checkpoint写入 /u01/user/runs/<run-id>/。
# Ctrl-b，松开后按 d：分离会话，训练继续。

# 下次登录 H20 后重新进入
tmux attach-session -t train-main
```

`tmux ls` 在第一次使用、没有会话时提示没有服务器是正常的。不要用 `tmux kill-server` 清理训练环境。
本次只验收轻量心跳任务在启动 SSH 结束后仍运行并正常完成。**这不是实际训练验收，也不保证主机重启、会话强制清理后继续运行**；训练自身仍需定期保存 checkpoint。

## 故障定位与结论边界

| 现象 | 优先检查 |
| --- | --- |
| 超时 / 无法连接 | 从 6000D 到 `192.0.2.27:6622` 的网络；不要回到旧 54321 配置排查新链路 |
| `Permission denied (publickey)` | 实际用户和 IdentityFile、公钥安装位置、个人 HOME/.ssh 权限；主机可达不等于认证成功 |
| 主机指纹变更 | 在可信会话核对新主机公钥；不要跳过严格校验 |
| `/u01` 不可写 | 挂载状态、个人子目录所有者、剩余空间；不对整个 `/u01` 修改权限 |
| rsync 中断 | 保留 `.rsync-partial`，源内容与目标路径不变，再执行同一命令 |
| H20 无法连接外部模型/包源 | 这是 H20 出网/代理/依赖问题，单独处理；不影响已验证的内网 SSH 传输 |

1 GiB 样本速度只代表本次测试条件，不代表 TB 数据集、小文件密集场景或多人并发时的速度。未验证主机重启恢复、备份、共享存储挂载、6000D→H20 的 VS Code 插件运行，也没有安装/运行 H20 Codex。当前方案使用 6000D Codex 调用已验证的 SSH 命令与文件同步。

## 相关证据

- [2026-10-14 验收记录与复测方法](experiments/20261014/README.md)
- [本轮机器可读结果](experiments/20261014/result.json)
- [最初 1 MiB 连通与重复同步回执](experiments/20261014/initial-connection.json)

后续端口、账号、公钥、目录或工作流发生变化时更新本页；日期实验记录保留当时事实，不覆盖旧结果。


## 2026-10-20 F1直达H20的RTC对照入口

用户本轮授权评估H20推理，并要求不使用其他人的GPU4–7。原50K训练已正常完成，候选只占GPU0；Codex和开发仍在6000D。原6000D现场入口保留，是否迁移以[任务状态](../../tasks/F1_ComplexyToys/STATUS.md)为准。

F1可直连`192.0.2.27:6622`，通过独立受限公钥将F1 localhost8137转发到H20 localhost8137。私钥只在F1任务runtime的`h20-ab/id_ed25519`，不复制到6000D/H20；H20本人authorized_keys保留旧内容并先备份，新key comment为`f1-rtc-h20-ab-20261020`，禁止shell/PTY/agent/X11，只允许指定回环端口。它是本轮候选测试入口，不改变6000D日常SSH别名或其他用户权限。

H20服务tmux `f1-rtc-h20-ab`，源码`/home/user/projects/task-releases/f1-toys-20261020-rtc-h20`，日志/缓存`/u01/user/runs/f1-toys-rtc-h20-20261020`。用户未授权恢复训练，本轮不自动启动训练。测试结果与环境限制见[RTC实验](../../tasks/F1_ComplexyToys/inference/experiments/20261019-rtc.md#2026-10-20-h20-gpu0直接推理对照)。


### 同日正式迁移与50K入口

用户进一步授权H20作为RTC推理入口。现5K/50K分别占本人GPU0/1，GPU4–7不操作；F1使用8147→H20 8137、8148→8138的独立守护隧道，两路重连已实测。原受限key增加且仅增加`permitopen="127.0.0.1:8138"`，先备份`authorized_keys.before_rtc_50k_20261020T110411`，其他限制保留。旧F1 8137候选连接保留兼容。

当前源码release为`f1-toys-20261020-rtc-h20-v3`；tmux `f1-rtc-h20-5000-graph`、`f1-rtc-h20-50000-graph`，run分别`/u01/user/runs/f1-toys-rtc-h20-{5000,50000}-graph-20261020`，入口`start-v3.sh`。采用已验证的精度保留CUDA Graph，未修改训练环境/冻结核心。现场命令、原6000D回退及H20重启恢复步骤统一见[启动说明](../../tasks/F1_ComplexyToys/inference/STARTUP.md)。这是积木任务按用户授权的分工调整，Codex仍仅在6000D。

### 2026-10-21 独立IK恢复候选端口

用户明确批准仅为同一F1受限钥匙增加`permitopen="127.0.0.1:8160"`，其余限制逐字保留。修改前备份为H20个人`.ssh/authorized_keys.before_ik_recovery_20261021T030933Z`；没有新增shell、PTY、agent或X11权限。原8137/8138现在是Full625/1K，保持原服务身份。

独立旧50K恢复候选使用H20 GPU2/8160，F1本地8161，经专用隧道连接；新进程为tmux `f1-ik-recovery-20261021`。已完成F1 r2的20秒、30请求、零控制发布检查，现场动作仍由用户本机Enter执行。当前命令、日志和验收统一见[STARTUP](../../tasks/F1_ComplexyToys/inference/STARTUP.md#旧50k软件恢复独立候选2026-10-21)，[许可变更回执](../../tasks/F1_ComplexyToys/Eval/artifacts/20261021-ik-recovery-next/deployment/tunnel-permission-receipt.json)不含钥匙内容。
