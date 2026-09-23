# 新 F1 连接配置（22029）

操作与验证范围见 [接入记录](../../../docs/connect/f1-22029.md)。

`ssh_config`、`known_hosts` 和 `systemd/f1-6000d-tunnel.service` 部署在新 F1 的 `/home/user/Bruno/connect/`。
`client_ssh_config`、`robot_known_hosts` 仅供 6000D 登录此机器人。
`tunnel_key.pub` 是新 F1 的隧道公钥，`evidence/` 保存非秘密部署与验证记录；本目录没有私钥。

禁止把旧机配置或密钥覆盖到新机，禁止整目录删除式同步。主机名相同不代表同一机器人，连接用独立的 22029 端口与主机密钥别名。
