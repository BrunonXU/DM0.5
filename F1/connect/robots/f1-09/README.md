# F1-09 连接配置（22009）

状态与现场事实见 [接入记录](../../../docs/connect/F1-09.md)。

2026-09-22 已部署并验证：免密登录、开机配置和进程退出后自动恢复通过，Wi-Fi DHCP 地址保留已由用户配置，2026-10-12已验证重新获取保留地址；旧接口名绑定已清除并观察到自动连接，修复后冷启动待验收。
`client_ssh_config` 是 6000D 登录入口，`robot_known_hosts` 保存本机独立主机公钥。
`ssh_config`、`known_hosts`、`systemd/f1-6000d-tunnel.service` 已定向部署到 F1 `/home/user/Bruno/connect/`。
`tunnel_key.pub` 是机器人独立隧道公钥，私钥仅在机器人，禁止复制 F1 `keys/` 回项目。
`evidence/` 保存部署前、切换、权限限制、哈希与自动恢复证据。
`bootstrap.sh` 保留首次接入流程；临时隧道已停，正式服务启用后无需再执行。
禁止整目录覆盖或删除式同步机器人连接目录。
