---
updated: 2026-09-21
type: connection_runbook
robot: 3F-JIMU
status: deployed_and_verified
---

# 3F-JIMU 持久连接

用户命名 **3F-JIMU**，用于复杂积木识别、分拣与投放 demo；现场 SSH 地址为 `192.0.2.24`。
2026-09-21 已部署并验证独立反向入口 `22165`：免密登录、开机服务配置、进程退出后自动恢复均通过。
6000D 直连现场 IP 超时，因此由 F1 主动连接 `192.0.2.1:22`；日常连接不依赖笔记本或 IDE。
实机为 `ms-thor / user / aarch64 / Ubuntu 24.04.4 LTS`，`wlan0` 地址与现场一致。
用户终端截图有 `F1_D072` 标签，尚未独立核实硬件编号；ROS、控制接口和 demo 运行环境不在本次连接验收范围。
不能沿用其他 F1 的系统清单。

## 首次接入

用户已在 F1 执行 [bootstrap.sh](../../connect/robots/3f-jimu/bootstrap.sh)，追加 6000D 登录公钥并建立临时隧道。
经隧道获取的主机公钥指纹与用户截图及认证后本机文件一致：
`SHA256:REDACTED_HOST_FINGERPRINT`。
该主机公钥固定保存于 `robot_known_hosts`，SSH 别名为 `3F-JIMU` / `3f-jimu`。
临时隧道已精确停止，正式服务已接替，不需要再执行 bootstrap，原现场终端可关闭。

## 持久配置与验证

- 新 F1 原地生成独立隧道私钥 `/home/user/Bruno/connect/keys/id_ed25519_6000d_tunnel`，权限 600；私钥未取回 6000D。
- 隧道公钥指纹：`SHA256:REDACTED_HOST_FINGERPRINT`。
- 6000D 追加限制：`restrict,port-forwarding,command="/bin/false",permitlisten="127.0.0.1:22165",permitopen="127.0.0.1:22"`；shell 和未授权端口 22167 均实测拒绝。
- 首次部署前仅有 bootstrap 与临时主机公钥文件，无旧隧道配置或同名服务。`Linger=yes` 原已启用，未修改。
- `f1-6000d-tunnel.service` 为 `enabled / active / running`；服务文件位于 Bruno 内，注册链接位于 `~/.config/systemd/user/`。
- `Restart=always / RestartSec=10s`；SSH 每 30 秒保活、连续 3 次无响应后退出重连。仅向正式 SSH 主进程发 SIGTERM，**11.42 秒**后恢复免密登录，PID 改变且重启计数由 0 增至 1。
- 临时验证服务 `f1-3f-jimu-staging.service` 已停止，22166 授权已移除；实测再次申请该端口被拒绝。
- 配置 SHA256 与部署副本一致，原有其他机器的服务器授权完整保留。
- 尚未实测整机重启、物理 Wi-Fi 断连、长期稳定性与吞吐；未启动机器人动作或修改 ROS/驱动。

证据保存在 [evidence](../../connect/robots/3f-jimu/evidence/)，配置见 [连接目录](../../connect/robots/3f-jimu/README.md)。

6000D 任意目录的连接命令为：

```bash
ssh -F /home/user/projects/F1/connect/robots/3f-jimu/client_ssh_config 3F-JIMU
```

6000D 登录私钥沿用 `~/.ssh/id_ed25519_f1`，指纹为
`SHA256:REDACTED_HOST_FINGERPRINT`。
其历史注释 `6000D-to-F1-D070` 只是标签，4F-F1 已共用该登录公钥。
公钥授权默认无过期时间，但单纯授权不能让私网地址自动可达；持续访问还依赖 F1 有网络、SSH 服务可用及反向隧道运行。
6000D 主机公钥指纹为 `SHA256:REDACTED_HOST_FINGERPRINT`。

## 排障、停用与恢复

F1 的 `user` 终端，任意目录：

```bash
systemctl --user status f1-6000d-tunnel.service --no-pager
loginctl show-user user -p Linger
tail -n 80 /home/user/Bruno/connect/tunnel.log
ip route get 192.0.2.1
```

网络恢复后会自动重试；如需手动重启，执行 `systemctl --user restart f1-6000d-tunnel.service`。
显式停用为 `systemctl --user disable --now f1-6000d-tunnel.service`，会断开该入口，应从现场终端操作。
恢复为 `systemctl --user enable --now /home/user/Bruno/connect/systemd/f1-6000d-tunnel.service`。
Linger 原已启用且存在其他用户服务，撤销本连接时不要关闭 Linger。
永久撤销授权时仅删除 6000D `~/.ssh/authorized_keys` 中 `3f-jimu-reverse-tunnel` 的公钥行。
原授权备份：`~/.ssh/authorized_keys.before_3f_jimu_20260921T023610Z`，不要整体恢复旧备份覆盖后续授权。
