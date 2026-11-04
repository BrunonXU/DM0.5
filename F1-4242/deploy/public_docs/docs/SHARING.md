# 部署与迁移

首次使用先按 [README](../README.md) 的四步完成。本页供部署人员核对环境、接入可选模块，以及处理升级和换机。

工作台启动后先确认反馈与相机。初始化生成的动作配置保持关闭，机器人动作和遥操需要分别完成本机适配与现场验收。

## 安装配置

新机优先执行 `bash setup.sh`，详细规则见 [初始化说明](INITIALIZATION.md)。仅手工适配时参考 `configs/installation.env.example` 与 `configs/robot.example.json`，不要覆盖已有本机配置。

`run.sh` 自动读取 `configs/installation.env`，也可通过 `F1_INSTALLATION_ENV` 选择其他安装配置。配置中的 `HERE` 由启动脚本设为当前源码根；在终端手工加载配置前，先在源码根执行 `HERE="$PWD"`。

| 配置 | 部署人员核对内容 |
| --- | --- |
| `F1_INFRA_HOME` / `F1_CONSOLE_RUNTIME` | 安装与运行目录；四步安装使用实际解压目录及其 `runtime/` |
| `F1_PROFILE_PATH` / `F1_SITE_PATH` | 本机机器人配置、昵称与网页 IP 配置 |
| `F1_CONSOLE_PORT` | 保持 `4242` |
| `F1_CONTROL_LOCK` | 与其他协作控制端共用的控制锁；不要为新副本另建一把锁 |
| `F1_ROS_SETUP` / `F1_ROBOT_SETUP` / `F1_AGV_SETUP` | 已安装的 ROS、F1、底盘 setup 文件 |
| `F1_CYCLONEDDS_URI` / `RMW_IMPLEMENTATION` / `ROS_DOMAIN_ID` | 与本机驱动一致的 DDS 配置、RMW 与域 |
| `F1_CONSOLE_PYTHON` / `F1_RECORDING_PYTHON` | 网关 Python、兼容 ROS 的录制环境 |
| `PICO_UPPER_*` / `F1_LXL_ROOT` | 需要 Pico 时再核对，见 [Pico 接入](PICO.md) |

本包提供工作台源码、必要前端与模型资产、配置模板和可选上肢 Pico 适配。不提供厂商驱动、XR SDK、PCService、外部 DAgger 运行端、Windows sender、模型服务或虚拟环境；对应模块使用目标机器已有或单独安装的环境。

## 本机动作配置

部署人员在 `configs/robot.json` 核对下列内容，现场验收后再启用对应能力：

- 本机 machine-id、控制器实际二进制及参数、消息话题和单位。
- 具名关节、方向、零位、限位、速度、相机话题和模型映射。
- 本机编译的命令来源监听器及 SHA256，实际启动脚本及 SHA256。
- 已核验预设、反馈新鲜度、控制来源及共用锁。

不要复制其他机器的 `*_enabled`、`*_verified` 或已验收标记。运行时驱动审阅规则在 `robot/runtime_contract.py`，仅更换 JSON 哈希不能完成适配。昵称和网页 IP 在 `site.json` 中修改，稳定机器身份及原始 profile 不随改名重写。

需要编译命令监听器时，在机器人项目根的 Bash 终端执行：

```bash
HERE="$PWD"
source configs/installation.env
source "$F1_ROS_SETUP"
cmake -S robot/command_monitor -B "$F1_CONSOLE_RUNTIME/command-monitor/build"
cmake --build "$F1_CONSOLE_RUNTIME/command-monitor/build" -j2
sha256sum "$F1_CONSOLE_RUNTIME/command-monitor/build/libf1_command_monitor_shared.so"
```

将实际路径与 SHA256 写入本机配置，不从不同架构机器直接复制编译产物。话题、消息或关节布局改变时还需同步核对录制、Pico 和模型适配。

## 可选视频服务器

新 HIL / 统一 Pico 原件保存在机器人本地，采集和保存不依赖视频服务器。当前数据页播放 MP4；没有派生视频时显示等待状态，页面没有原图浏览入口。

完整会话视频由 `scripts/session_preview_worker.py` 在独立处理机读取原件、编码并回传机器人。处理机需要 Python、FFmpeg / FFprobe、可用缓存空间和访问工作台的网络。下面为处理机上的 JSON 配置示例，IP 与绝对缓存路径都要替换：

```json
{
  "robot_url": "http://192.0.2.10:4242",
  "cache": "/srv/f1-preview-cache"
}
```

将其保存为处理机上的 `preview-worker.json`，在该机器的 F1-infra 源码根执行：

```bash
python3 scripts/session_preview_worker.py --config /path/to/preview-worker.json
```

当前 worker 无需旧访问码。它处理工作台队列中兼容的完整会话任务，不承诺自动支持所有 Pico 来源；统一 Pico 派生视频与训练导出按 [统一数据说明](UNIFIED_DATA.md) 单独处理。回传成功后，网页从机器人读取已有 MP4。

视频派生、训练导出和中央归档是独立流程。归档上传默认关闭，共享盘仅用于本源码包分发。

## 升级与换机

**同机升级：** 结束本项目动作、录制和遥操，备份当前源码、本机配置和服务单元，再更新源码。保留 `installation.env`、`robot.json`、`site.json`、runtime 与原始数据；不用重新初始化。源码路径变化时核对配置中的绝对路径。回退代码不会回滚新增录制或审核记录。

旧安装若另有被 profile 引用的 joint contract、模型注册配置或自定义配置文件，也一起保留并核对引用路径。公开包中的模板和空模型目录不替换现场已验证配置。

**换机器人：** 在新机重新运行 `bash setup.sh`，重新核对硬件和驱动合同，不复制旧机身份、启用标记或虚拟环境。需要历史数据时单独复制并校验，再调整数据入口。

**自启部署：** 四步解压安装可一直以前台方式使用。需要版本目录、`current` 入口和常驻服务时，按 [开机自启](AUTOSTART.md) 配置；源码上传或解压本身不会安装自启服务。

## 包完整性

在解压后的 `F1-infra/` 目录执行 `sha256sum -c SHA256SUMS`，核对包内文件。ZIP 旁的 `.sha256` 可用于核对整个压缩包；Windows 使用 `Get-FileHash <ZIP路径> -Algorithm SHA256`。

本机生成的配置、探测报告、日志和数据不要随源码再次分发。Windows 中转可能丢失可执行权限，普通入口继续使用 `bash setup.sh` 和 `bash run.sh`；安装 systemd 服务前确认 `run.sh` 可执行。
