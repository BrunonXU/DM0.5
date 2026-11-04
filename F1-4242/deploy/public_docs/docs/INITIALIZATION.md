# 四项初始化

在机器人项目根运行 `bash setup.sh`，确认名字、访问 IP、数据目录和可选模块，即可生成本机配置。最短使用步骤见 [README](../README.md)。

工具探测已有环境并写入新配置，不安装依赖、不启动服务，也不启用机器人动作。出现待处理项时，由部署人员按本页核对；已有安装升级时保留配置，无需重新初始化。

## 自动检测与生成

工具检测账号、HOME、机器标识、架构、Python、IPv4、磁盘空间，以及已有 ROS / F1 / 底盘环境、DDS 配置和共用控制锁。网页使用的 IP 可以与 DDS 网卡不同，工具不会用网页 IP 覆盖 DDS 网卡。

| 生成文件 | 内容 |
| --- | --- |
| `configs/installation.env` | 本机路径、已有环境、共用锁、4242 端口 |
| `configs/site.json` | 显示名字 `label`、网页 IPv4 `ip` |
| `configs/robot.json` | 稳定机器身份与动作默认关闭的配置 |
| `runtime/storage.json` | 新统一录制的数据目录，默认 `~/F1-data` |
| `runtime/recording.json` | 选择 Pico 时生成的统一 30Hz 本地录制配置 |
| `runtime/setup-report.json` | 本次检测结果与待处理项，仅供本机维护 |

初始化不创建数据目录或搬动已有数据。新录制目录与旧 MCAP 只读入口 `data_path` 分开；手柄录制的目录还需在 DAgger 运行端配置，见 [手柄接入](HIL.md)。

已有且内容不同的配置、符号链接配置、不能确定的共用锁会阻止写入。先确认现有部署，不通过删除配置来绕过提示。改名或换 IP 只编辑 `site.json`，工作台空闲时重启即可，不改 `id`、`robot_id` 或控制字段。

## 待处理项

| 提示 | 怎么处理 |
| --- | --- |
| 多个 IPv4 | 选电脑可访问的地址，不直接使用第一项 |
| 缺少或存在多个 setup 路径 | 在 `installation.env` 核对 ROS、F1、底盘环境的实际 setup 文件 |
| DDS 配置未确定 | 核对现有驱动的域、RMW 与 DDS 配置，在线启动前完成配置 |
| 共用锁冲突 | 核对各控制端实际使用的同一把锁，不另建锁绕过互斥 |
| Pico / DAgger 待接入 | 按 [Pico](PICO.md) / [手柄](HIL.md) 完成独立模块配置 |
| 动作待验收 | 按 [部署说明](SHARING.md#本机动作配置) 完成硬件、驱动与现场验收 |

只预览网页可在项目根执行 `bash run.sh --offline --host 0.0.0.0`，访问 `http://机器人IP:4242`。离线模式没有真实相机与机器人反馈；已有 4242 服务时不要另开同端口副本。

## 批量配置与只读探测

在机器人项目根的 Bash 终端可显式传入四项。下面的名字、文档示例 IP 和目录请换成本机信息：

```bash
bash setup.sh --name F1-09 --ip 192.0.2.10 \
  --data-dir ~/F1-data --modules none
```

仅探测、不写配置：

```bash
bash setup.sh --probe-only
```

`--inventory <清单.json>` 用于隔离模拟，生成报告标记 `simulation: true`；`--output-dir <目录>` 仅指定配置输出位置，不复制应用源码。探测结果、配置生成和网页启动均不能代替真实采集或机器人动作验收。探测报告包含本机环境信息，留在本机，不随源码再次分发。
