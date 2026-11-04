# 2026-10-14：6000D ↔ H20 通信验收

状态：**通过**。当前用法统一维护在 [连接与训练协作说明](../../README.md)。

## 环境与范围

- 测试时间：2026-10-14 **18:15:23—18:17:06，北京时间**；原始回执同时保留 UTC。
- 发起端：6000D，`user@hq`；目标：`h20-train` → `user@192.0.2.27:6622`。
- SSH 使用 `BatchMode=yes`、严格主机校验、ED25519 主机算法，未输入密码或 OTP。
- 两端 rsync：6000D `3.2.7`，H20 `3.2.3`，协议版本均为 31；H20 tmux `3.2a`。
- H20 文件系统：`/u01` → `/dev/mapper/vg01-data`，ext4；测试前确认挂载和写权限。
- 使用新的专属 staging 目录、不可压缩随机测试文件、独立 tmux socket。没有运行 GPU 工作负载或改动已有训练。

## 结果

| 检查 | 证据 | 结果 |
| --- | --- | --- |
| 个人账号非交互 SSH | 用户 `user`，UID/GID `1004:1004` | 通过 |
| 6000D → H20 上传 1 GiB | 26.5435 秒，**38.58 MiB/s**，约 40.45 MB/s | 通过 |
| H20 → 6000D 下载 1 GiB | 44.7984 秒，**22.86 MiB/s**，约 23.97 MB/s | 通过 |
| 往返完整性 | 本地原文件、H20 文件、下载副本的 SHA256 相同 | 通过 |
| 真正中断后的续传 | 256 MiB 文件；重试复用 61,897,544 字节，约 59.03 MiB | 通过 |
| SSH 退出后后台任务 | 新连接看到心跳从 2 条增长到 5 条；最终写满 23 条并完成 | 通过 |
| 清理 | 本地临时数据、H20 本次 staging 目录、专用测试 tmux 均清理 | 通过 |

最初还有一次独立的 **1 MiB 往返与重复同步**：SHA256 一致，文件不变时第二次上传没有变更条目。见 [最初回执](initial-connection.json)。

### 吞吐测试方法与限制

生成 1,073,741,824 字节随机文件后，使用 rsync over SSH 上传，再下载到不同的本地文件。
两端均关闭传输压缩；目标最初不存在，两次传输的 `Literal data` 都是完整 1 GiB，`Matched data` 为 0。
计时包含 SSH 建连、rsync 传输与进程退出，不包含文件生成和 SHA256 校验。

共同 SHA256：

```text
bf45937928d6a353e0e4987767e3828e4adec58cbcdee7b49f1fdf0193594388
```

这是单文件、单次有效吞吐，可能利用系统页缓存；没有清缓存、测试磁盘持久写入或压测多人并发。
**未实际传输 TB 级数据。** 若持续保持本次速度，十进制 1 TB 纯传输约需上传 **6.9 小时**、下载 **11.6 小时**；这只是估算，文件数量、磁盘、CPU、网络负载和校验都会改变总时间。

### 续传不是“重新传一遍”

1. 新建独立 256 MiB 文件，以 `--bwlimit=8192`（8 MiB/s）上传，使用 `--partial --partial-dir=.rsync-partial --no-whole-file`。
2. 约 8 秒后，仅向本次 rsync sender 发 SIGINT；退出码 **20** 是预期的主动中断。
3. 等进程退出后，确认远端 `.rsync-partial/resume.bin` 为 **61,898,752 字节**；最终 `resume.bin` 尚不存在。
4. 对相同源文件和目标路径重跑相同传输参数、取消限速。统计显示：

```text
Total file size: 268,435,456 bytes
Matched data:    61,897,544 bytes
Literal data:  206,537,912 bytes
Total bytes sent: 206,619,945
```

匹配字节大于 0，且实际新增内容少于完整文件大小，证明重用了部分文件。匹配字节略少于部分文件大小是块匹配边界所致；不能要求两者完全相等。
源文件与 H20 完成文件 SHA256 均为：

```text
a68908077294cd04f678da56217a26c15ee079a90e8ddd3cb3a7ea26b8b2e3b9
```

这是一次主动中断测试，没有模拟断电或网络黑洞。使用相同机制遇到其他中断时，仍需确认 partial 文件保留及重试退出状态。

### 断开后任务存活

在独立 socket `h20check-20261014T101523Z-00a66ba2` 下以 `tmux -f /dev/null` 启动 46 秒 Python 心跳；每 2 秒记录一条，不加载 GPU。
启动它的 SSH 已正常退出，后续使用新的 SSH 连接读取结果：

- 第一次观察：2 条，尚未完成。
- 约 6 秒后：5 条，尚未完成，PID 保持不变。
- 最终：23 条，完成标记 `{"result": "PASS", "ticks": 23}`，任务日志为空。

说明这次启动 SSH 的结束没有终止 tmux 中的任务；不代表主机重启、管理员清理、会话到期等情况已验收。实际训练的依赖、GPU、分布式通信和断点恢复不在本次测试范围内。

## 可追溯文件与复测

- [完整结果、命令、stdout/stderr、哈希、心跳](result.json)
- [可复测脚本](verify_connection.py)
- [最初 1 MiB 连通回执](initial-connection.json)

本轮专用远端目录为：

```text
/u01/user/staging/connect-acceptance-20261014T101523Z-00a66ba2
```

该目录已删除。原始回执仍保留路径用于追溯，不表示测试数据仍存在。
脚本随后补充了复测的失败保护：强制检查 `/u01` 挂载、超时终止本次进程组、清理失败也保存回执、失败时保留专属远端目录供检查；本次成功路径的测量方法未变。

需要重新验收时，在 **6000D Bash**、Projects 根目录执行：

```bash
cd /home/user/projects
H20_CHECK_RECEIPT="/tmp/h20-acceptance-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 infra/h20/experiments/20261014/verify_connection.py --output "$H20_CHECK_RECEIPT"
```

复测需要约 2.25 GiB 本地峰值临时空间、约 1.25 GiB 远端测试数据空间、约 2.25 GiB 文件传输流量，通常几分钟完成；不覆盖已有回执。
单个命令超时为 180 秒。如果传输很慢导致超时，应记录为该次吞吐测试超时，不能直接认定账号或网络完全不通。
成功时清理两端本轮临时数据，保留 JSON；失败时查看回执的 `error`、`cleanup` 和唯一远端目录，不要删除其他 staging 内容或使用全局 `pkill`/`tmux kill-server`。

实现语义参考：[rsync 官方手册](https://download.samba.org/pub/rsync/rsync.1)、[tmux 手册](https://man.openbsd.org/tmux)。性能与通过结论均来自本机实测，不来自文档推断。
