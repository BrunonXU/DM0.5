# F1 Windows 操控入口

F1 连接采用有线优先、Wi-Fi 备用。开发中的单窗口启动器会在启动前选择可达地址，并将本次会话的终端、手柄隧道和日志回传固定到同一目标；运行中断线不会触发跨地址切换或重新启动机器人动作。

自动候补目前仅在开发源码中，随下一次正式操控包发布生效；不能为这次网络设置直接安装包含未部署协议的开发包。已安装 V38／V43 可使用下面的环境变量切换目标，控制版本与动作授权保持原状。实际部署和完整现场流程见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)与[现场命令](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#single-window)。

## 已安装客户端：设置有线目标

在 Windows PowerShell、任意目录执行以下两行，分别应用于当前窗口与后续新窗口：

```powershell
$env:F1_SSH_HOST = 'user@192.0.2.7'
[Environment]::SetEnvironmentVariable('F1_SSH_HOST', $env:F1_SSH_HOST, 'User')
```

首次使用有线地址时先按[现场命令的只读SSH步骤](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#wired-preferred)核对主机指纹。启动前TCP探测不代表已经认证；后台手柄隧道不会跳过未知主机检查。

先按原流程保存并退出旧操控窗口，再从 Windows PowerShell 启动；现有连接不会因修改变量而迁移：

```powershell
cd "$HOME/DAgger-F1"
.\start_f1_rlt.cmd
```

需要 Wi-Fi 备用时，在退出上一会话后显式指定：

```powershell
.\start_f1_rlt.cmd --robot-host user@192.0.2.3
```

若要长期使用备用地址，将上面的环境变量值改为 `user@192.0.2.3`。现场 START 授权和原收尾流程保持不变；日志在客户端 `runtime/operator-sessions/`。

## 开发源码的目标选择

| 条件 | 启动行为 |
|---|---|
| 提供 `--robot-host` | 固定为命令行目标，不探测其他地址 |
| 未提供参数但设置 `F1_SSH_HOST` | 固定为环境变量目标，不探测其他地址 |
| 两者均未设置 | 先探测有线 `192.0.2.7:22`，不可达时再探测 Wi-Fi `192.0.2.3:22` |
| 两个默认地址均不可达 | 启动失败，排查网络后由操作者重新启动 |

每个探测最多 1.5 秒，只测试 TCP 可达性；实际连接保留 OpenSSH 的主机密钥校验与认证，认证失败不自动换地址。启动窗口和会话日志会显示最终目标与选择来源。已经设置持久环境变量的用户，安装后续正式包后如要启用自动候补，需要清除该变量；显式配置始终优先。地址以[当前网络记录](../../../tasks/F1_ComplexyToys/inference/experiments/20261023-rlinf-offline.md#network-wired-20261103)为准。

三端模式使用 `scripts/windows_f1_tunnel.cmd`，默认有线；通过 `F1_SSH_HOST` 显式选择 Wi-Fi。此独立脚本不自动候补。sender 继续连本机 `127.0.0.1:7907`，不把它改成机器人地址。目录中遗留的 `windows_hil_shadow_tunnel.cmd`／`windows_tunnel.cmd` 是旧 G2 入口，F1 不使用；正式单窗口包不包含它们。

安装器／更新器模板也默认有线。使用发布好的安装脚本时可显式传 `-RobotHost user@192.0.2.3`，仅改变本次下载和密钥设置目标；不自动修改已保存的 `F1_SSH_HOST`。发布须使用已登记的新候选身份，完整验证后由统一发布流程生成包；冻结包及其哈希不可覆盖。
