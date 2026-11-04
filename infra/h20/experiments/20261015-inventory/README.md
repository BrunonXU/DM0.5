# 2026-10-15 H20 只读资源盘点

当前整理结论见 [H20 系统与资源档案](../../SYSTEM_PROFILE.md)。此目录保存当时的原始证据，不维护另一份当前状态。

## 范围与时间

- 主采集：2026-10-15 10:50:47—10:52:02（北京时间）。
- 补充采集：约 10:54（北京时间）；JSON 中保留 UTC。
- 从 6000D 使用 `h20-train` → `user@192.0.2.27:6622`，BatchMode 与严格 ED25519 主机校验。
- 读取 CPU/NUMA、内存、设备与挂载、GPU/拓扑/健康计数、系统包、个人环境、网络与会话限制。
- 不使用 sudo，不安装包，不遍历其他用户数据，不运行训练、GPU 分配或带宽/磁盘压测；快照仅保存在 6000D。

## 文件与字段

| 文件 | 内容 |
| --- | --- |
| [inventory.json](inventory.json) | 主采集，35 项命令以及选定 proc/sysfs 文件、权限、目录与解释器元数据 |
| [supplement.json](supplement.json) | RDMA端口、显存 reserved、登录 shell 工具、驱动包安装状态和个人数据目录；含复查源码 |
| [collect_inventory.py](collect_inventory.py) | 可复用只读采集脚本；输出文件存在时拒绝覆盖 |

主要证据映射：

- CPU、内存：`commands.cpu/memory/swap`、`files./proc/meminfo`、NUMA sysfs、`cpu_affinity`、`limits`。
- 磁盘：`commands.block_devices/filesystems_bytes/inodes/mounts`、`paths`。
- GPU：`commands.gpu_inventory/gpu_compute_capability/gpu_topology/gpu_links/gpu_ecc/gpu_compute_processes`。
- 软件：`commands.service_states/packages/python_pip`、`path_tools`、`python_packages`、`environment_locations`。
- 网络：`commands.interfaces/route_to_6000d`、`files./proc/net/bonding/bond0`、补充快照的 `rdma_ports`。

## 读取结果时的注意事项

1. **退出码 0 不等于数据完整。** pvs/vgs/lvs 的 JSON 为空且 stderr 有权限拒绝；LVM 段、冗余和 VG 空闲量未能读取，不能据此说没有 LVM。
2. dpkg 的通配符查询部分无匹配，整体退出 1；已有版本输出仍是有效证据。空版本行不当作已安装包；补充查询用 `ii` 状态确认了驱动、utils、Fabric Manager。
3. `python_pip` 失败是当前系统 Python 无 pip 的直接结果，不是 SSH 失败。登录 shell 的补充检查同样未找到训练工具。
4. 数据来自多条先后执行的命令，动态数值可能略有变化；显存字段各自按 MiB 显示，也可能有取整差异。
5. GPU 空闲、ECC计数为0不等于完整压力/健康验收；NVLink和网卡额定字段不等于应用吞吐。
6. 原始日志未包含密码、密钥正文、shell历史或令牌；代理只记录是否设置及脱敏的主机/端口元数据。

## 复查

在 **6000D Bash** 执行；输出位置为本机 `/tmp`，不覆盖本目录的历史快照。

```bash
cd /home/user/projects
H20_INVENTORY_JSON="/tmp/h20-inventory-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 infra/h20/experiments/20261015-inventory/collect_inventory.py --output "$H20_INVENTORY_JSON"
```

主脚本不自动重新执行单独的补充探针；需要补充内容时可依据 `supplement.json` 中记录的命令逐项只读复查。
