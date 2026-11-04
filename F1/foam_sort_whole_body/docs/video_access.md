# 三路视频访问

视频服务在6000D的8876端口，同时监听127.0.0.1与::1，支持MP4字节范围请求。
2026-09-14验证：两个地址的HTML均200，MP4范围请求均206。服务名为 `f1-foam-videos-8876.service`。
这些是服务器侧验证，不代表本地IDE隧道已通。

## 本地浏览器访问

IDE端口面板转发8876后，使用面板中的实际本地地址；它可能不是本地8876。
若拒绝连接或超时，可以在 **Windows本地PowerShell**（不要在远程6000D终端）运行独立隧道：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:18876:127.0.0.1:8876 -p 22 user@192.0.2.1
```

按通常的SSH方式登录，保持该窗口运行；连接成功后没有输出是正常现象。
本地浏览器打开 `http://127.0.0.1:18876/index.html`。这里本地18876转发至服务器8876，
绕开本地已有8876转发；关闭该PowerShell隧道后，此地址不再可用，服务器视频服务继续运行。

服务器地址与SSH端口由本轮SSH_CONNECTION核对。命令尚未从用户本地实际执行；
若SSH连接失败，应检查其错误与本地现有SSH登录方式，不能据此认定HTTP服务失效。

## 服务器侧状态

在6000D终端执行：

```bash
systemctl --user status f1-foam-videos-8876.service --no-pager
journalctl --user -u f1-foam-videos-8876.service -n 20 --no-pager
```

实现与安装单元分别在 `scripts/serve_videos.py`、`scripts/f1-foam-videos-8876.service`。
