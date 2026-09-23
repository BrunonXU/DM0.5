# 3F-JIMU 连接配置（22165）

状态与操作统一见 [接入记录](../../../docs/connect/3F-JIMU.md)。

2026-09-21 已部署至新 F1 `/home/user/Bruno/connect/`，正式端口为 `22165`。
`ssh_config`、`known_hosts` 和 `systemd/f1-6000d-tunnel.service` 为机器人部署配置。
`client_ssh_config` 与 `robot_known_hosts` 为 6000D 登录入口及已核对的机器人主机公钥。
`tunnel_key.pub` 是机器人独立隧道公钥，`evidence/` 保存部署前状态、切换、权限限制、哈希和自动恢复验证。
`bootstrap.sh` 保留首次接入过程，正式服务运行后无需再次执行。
6000D 主机公钥来自服务器本机 `/etc/ssh/ssh_host_ed25519_key.pub`；机器人主机指纹已与用户现场截图核对。
私钥仅留在所属机器；禁止取回 F1 `keys/` 或整目录覆盖同步。
