# Pico 接入

需要头显遥操时再配置本模块；只使用基础工作台或手柄 DAgger 时可跳过。初始化选择 `pico` 会准备配置，但不会安装 Pico 依赖或开启动作。

包内提供独立上肢 Pico 适配，双臂与夹爪的模型、限位和运行环境需要在目标机器人核对。已有 LXL 流程可作为另一入口接入，其工程和 SDK 由使用者单独准备。

## 部署人员准备

在 `configs/installation.env` 中核对：

| 配置 | 用途 |
| --- | --- |
| `PICO_UPPER_ROOT` | 包内 `integrations/pico_upper` |
| `PICO_UPPER_RUNTIME` | 本机 Pico 运行目录，与源码分开 |
| `PICO_UPPER_PYTHON` | 已准备依赖的 Python 环境 |
| `PICO_UPPER_SDK_ROOT` | 本机 XR SDK 路径 |
| `PICO_UPPER_CONTROL_LOCK` | 与 `F1_CONTROL_LOCK` 使用同一把锁 |
| `F1_LXL_ROOT` | 仅已有 LXL 流程需要填写实际工程目录 |

上肢环境需要 numpy、ruckig、placo / placo_utils、meshcat、mujoco、coal、PyYAML，以及本机 XR SDK / PCService。环境检测不能替代本机控制适配。部署人员还需核对 `integrations/pico_upper/config/control.json`、`teleop/config.json` 及其中绑定的启动脚本、机器合同和 SHA256。

在机器人项目根的 Bash 终端检查依赖：

```bash
HERE="$PWD"
source configs/installation.env
cd "$PICO_UPPER_ROOT"
bash check.sh --dependencies
```

Pico App 连接机器人在头显所在网络的地址。网页 IP 不会自动改写头显应用或外部 LXL 工程的网络配置。

## 现场使用

1. 按现场原流程启动基础驱动与相机，确认反馈和运动空间。
2. 打开工作台 Pico 页，选择已完成本机适配的上肢版或 LXL。
3. 点击启动会提交本次启动操作；预检通过后进入对应流程。上肢版等待新鲜 Pico 数据、Grip / 扳机释放与摇杆居中；LXL 可能执行其配置的初始姿态。
4. 上肢版用左右 Grip 独立接管，接管后扳机控制对应夹爪；其他按钮以已安装流程的现场说明为准。
5. 使用页面的“停止本会话遥操”结束本会话。需要采集时另按 [本地录制](UNIFIED_DATA.md) 操作。

打开页面、刷新和开机不会自动启动遥操。关闭浏览器也不会结束已经运行的 Pico 会话，需使用明确停止入口；软件停止不等于硬件急停。

工作台只管理从本入口启动的会话。外部手动启动的遥操从原入口结束，不同时运行多个控制程序。网页会话日志位于 `runtime/teleop/sessions/`，上肢运行目录由 `PICO_UPPER_RUNTIME` 指定，均留在本机维护。
