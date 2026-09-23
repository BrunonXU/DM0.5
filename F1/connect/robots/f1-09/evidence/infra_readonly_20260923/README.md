# 2026-09-23 F1-09 在线基础设施检查

用户通知已恢复连接后，约15:11–15:19（UTC+8）通过已固定主机密钥的SSH22009读取。只读目录/文件/进程名、监听、HTTP静态资源、ROS图及底盘状态；没有启动业务服务、调用动作API、修改网络/时钟或发布动作。

| 证据文件 | 内容与边界 |
|---|---|
| `inventory.json` | 当前目录和选定启动配置；最初/proc路径扫描为空，不据此判断所有进程不存在 |
| `web_and_runtime.json` | ps进程名、Nginx代理及根页GET结果；未登录/调用业务API |
| `ros_graph.json`、`ros_graph_stderr.txt` | CycloneDDS发现62话题/145服务；节点名发现不完整，有类型哈希警告，非完整ROS拓扑 |
| `base_state_sample.json`、`base_state_stderr.txt` | 5秒接收1001条底盘State；header.sec=946686605，不是Thor当前墙钟，跨流对时待核 |
| `identity_and_ports.json` | .58仍有效；ROS Jazzy；无Pico .f1_robot_id；42/4242/8042/10001/10002等检查时未监听；机器身份保存哈希 |
| `vendor_web_sources.json` | 供应商根站/明确引用的JS静态资源URL、哈希、最小片段；URDF URL返回404 |

确认`/home/user/lxl`及其本机IP配置存在；`/home/user/meaxiao`不存在；Bruno当前只有连接目录。EC-Master/50001存在，当前未发现手臂HAL、motor_ctl和三路数采相机，未启动它们验证。

原站80/6001/6004经Nginx转发内部192.168.127.10。静态代码包含地图/底盘运维；Shaft升降未经证实匹配F1腰机构；robotModel无实时关节映射且URDF缺失。代码路径存在不等于实际操作验收。

[取回的11份本机配置/源码及哈希](../../../../../reference/20260923_f1_09_infra/README.md)，[一体化方案](../../../../../docs/embodied_console_integration.md)。
