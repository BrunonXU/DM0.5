# 部署、换机与源码交付

独立开发目录名为 `F1-4242`，共享 ZIP 解压根名为 `F1-infra`。两者内容相同，不需要外层 `F1/` 工程。初次使用先按 [根 README](../README.md) 启动只读网页；本文供部署时查配置。

## 包内范围

包含工作台、ROS 适配、姿态/数据/录制模块、测试、发布工具、前端依赖和模型；可选上肢 Pico 副本位于 `integrations/pico_upper/`。不含原 F1 工程、SSH 连接目录、运行数据、凭据、备份、Git 历史或虚拟环境。

厂商 ROS/驱动、EC-Master、LXL 原工程、PCService、XR SDK 和 ARM 二进制依赖复用目标机器人现有安装。上游/模型来源及许可证留在各自清单内。历史引用见 [来源索引](history/SOURCES.md)；当前已记录部署见 [STATUS](STATUS.md)。

## 1. 配置目标机器人

在目标机器人上的项目根目录：

```bash
cp configs/installation.env.example configs/installation.env
cp configs/robot.example.json configs/robot.json
```

`installation.env` 为 Bash 环境文件，由 `run.sh` 自动读取，也可用 `F1_INSTALLATION_ENV` 指向另一文件。模板里的 `HERE` 由启动脚本设置为当前源码根。不要把密码、私钥或控制码写入安装配置。

| 安装配置 | 必须核对的值 |
|---|---|
| `F1_INFRA_HOME` / `F1_CONSOLE_RUNTIME` | 安装根 / 运行数据根，默认当前用户的 `~/Bruno/F1-infra` 及其 `runtime` |
| `F1_PROFILE_PATH` | 本机 `configs/robot.json` 的完整路径；网关与录制/遥操子进程共用 |
| `F1_CONTROL_LOCK` | 所有协作控制端共用的锁；Pico 同步使用此值 |
| `F1_ROS_SETUP` / `F1_ROBOT_SETUP` / `F1_AGV_SETUP` | ROS Jazzy、F1 消息/驱动、底盘消息的现有 setup 文件 |
| `F1_CYCLONEDDS_URI` / `RMW_IMPLEMENTATION` / `ROS_DOMAIN_ID` | 与反馈/相机及相关控制端一致的 DDS 配置、网卡和域 |
| `F1_CONSOLE_PYTHON` / `F1_RECORDING_PYTHON` | 网关系统 Python / 兼容 ROS 的独立录制环境 |
| `PICO_UPPER_ROOT` / `PICO_UPPER_RUNTIME` | 默认工程内 `integrations/pico_upper` / 本项目 runtime 下的 `pico_upper` |
| `PICO_UPPER_PYTHON` / `PICO_UPPER_SDK_ROOT` | 可选上肢遥操的 Python 环境和已安装 XR SDK |
| `F1_LXL_ROOT` | 可选 LXL 原工程目录；没有该工程就不使用对应入口 |

**与 F1-09 旧部署共存时，控制锁仍须指定 `/home/user/Bruno/F1/runtime/control.lock`。** 不能为新副本另建一把锁而失去互斥。此条用于兼容原机器人现场，不要求新机创建父 F1 工程。

新机从 `robot.example.json` 开始。它默认关闭动作、清空未验证的预设；`f1-09.json` 是旧现场参考，不能直接当作另一台机器已核验的配置。

| 机器人 profile | 核对内容 |
|---|---|
| `id` / `robot_id` / `label` / `ip` | 机器身份和实际 IP；IP 也参与 HTTP Host 校验 |
| `expected_machine_id` | 目标机器 `/etc/machine-id` |
| `controller_binary_sha256` / `expected_motor_parameters` | 正在运行的 motor_ctl 二进制及实际参数；不是只核对 launch 文件 |
| `controller_parameter_rmw` | 读取控制器参数所用 RMW；可与反馈订阅分开配置 |
| `topics` / `camera_topics` / `groups` / `base_frame` | 消息、具名关节、三相机、底盘坐标与单位 |
| `joint_contract` | 本机22轴配置基线；F1-09绑定`f1-09-joints.json`，不能将其当成其他机器的标定，见[全身配置](JOINT_CONFIGURATION.md) |
| `joint_limits_deg` / `limits` | 两份限位一致，按本机确认方向、零位、速度和反馈阈值 |
| `command_monitor_binary` / `command_monitor_sha256` | 本机编译的命令来源监听器路径和 SHA256，可用 `${F1_CONSOLE_RUNTIME}` 路径变量 |
| `default_presets` / `full_home_source` | 本机核验过的预设、原生零位文件路径和内容 SHA |
| `startup_script` / `ec_startup_script` / `startup_script_sha256` | 本机现场启动脚本位置；基础启动脚本绑定经审阅的内容 SHA |
| `data_path` | 原 MCAP 只读根；新增录制/审核写入本项目 runtime |
| 各 `*_enabled` / `*_verified` | 完成本机合同与现场验收后逐项启用，不批量复制别机开关 |

`robot/runtime_contract.py` 另有 `AUDITED_BINARY_SHA256` 驱动允许列表。新二进制须审阅接口、限位与夹爪行为后再纳入，单改 JSON 哈希不会通过。

`teleop/config.json` 用 `${F1_LXL_ROOT}` / `${PICO_UPPER_ROOT}` 指定脚本；仍需核对脚本 SHA、上肢 `config/control.json`、模型/夹爪合同及源身份校验。Pico 内部桥 `127.0.0.1:42428` 不等于头显要连接的机器人局域网 IP。

网关的 `camera_topics/topics` 不会自动改写录制器、预览脚本或 Pico 中全部固定话题。不同消息、话题或关节布局要同步适配 `recording/upstream/lerobot_recorder.py`、`scripts/build_mcap_previews.py` 和 `integrations/pico_upper/src/`，并回归验证。URDF/几何变化另核对 `robot/model_geometry.json` 和 `web/models/f1_wheel/manifest.json`。配置一致性核验仅证明这些受审阅的工件互相对应，不能证明机械零位或实物尺寸正确。

## 2. 启动只读工作台

目标环境为 Linux、Python 3.12、ROS Jazzy 及本机 F1/agv 消息和驱动。先确认安装环境路径，再运行：

```bash
bash run.sh --host 0.0.0.0 --port 4242
```

打开 `http://机器人IP:4242/`。检查 machine-id、反馈新鲜度和三相机；基础驱动未就绪时页面保持只读。`--offline` 不加载 ROS，适合开发机先看页面；前端/模型已在包内，不需要 npm 或联网下载。

## 3. 准备需要的功能

**动作监听器：** 在机器人本机编译，加载安装配置中的 ROS/F1 setup 后，从源码根执行以下命令。示例路径须与实际 `F1_CONSOLE_RUNTIME` 一致：

```bash
source /opt/ros/jazzy/setup.bash
cmake -S robot/command_monitor \
  -B /home/user/Bruno/F1-infra/runtime/command-monitor/build
cmake --build /home/user/Bruno/F1-infra/runtime/command-monitor/build -j2
sha256sum /home/user/Bruno/F1-infra/runtime/command-monitor/build/libf1_command_monitor_shared.so
```

将实际路径和 SHA 写入本机 profile。不要从 x86 开发机复制 `.so` 到 ARM。确认身份、反馈、控制源与限位合同后，由现场操作者逐项验收动作。

**控制码：** 在机器人本机设置，源码包没有默认控制码：

```bash
read -r -s -p '设置本机控制码: ' f1_local_code
printf '\n'
printf '%s\n' "$f1_local_code" | python3 control_auth.py \
  --runtime /home/user/Bruno/F1-infra/runtime
unset f1_local_code
```

**录制：** 配置 `F1_RECORDING_PYTHON` 的独立环境，兼容 Python 3.12/ROS。既有环境记录为 numpy 1.26.0、pyarrow 20.0.0，另需 ffmpeg/ffprobe；运行 `bash recording/run.sh --check` 检查依赖，不创建录制节点。完整合同和验收见 [RECORDING](RECORDING.md)。

**Pico：** 需要 numpy、ruckig、placo/placo_utils、meshcat、mujoco、coal、PyYAML，和本机 XR SDK/PCService。安装环境字段完成后，从上肢目录执行 `bash check.sh --dependencies`。现场启动及按钮含义见 [Pico README](../integrations/pico_upper/README.md)。

中央存储和自动上传尚未接入；共享盘只用于源码分发。

## 4. 版本部署与自启

发布工具不读取原 F1 连接目录。开发机明确指定 SSH 主机；使用现有个人 SSH 配置或显式 `--ssh-config`：

```bash
python3 deploy/release.py --release 20260928-my-f1-v1 \
  --host user@ROBOT_IP --target /home/user/Bruno/F1-infra
```

仅本地准备可加 `--prepare-only`；`--stage-only` 上传核验但不激活。已有目标升级用 `--upgrade-inactive`，先按 [AGENTS](../AGENTS.md) 保存实际旧部署、确认本项目相关进程已停止；不能强制结束其他控制任务。默认目录：

```text
/home/user/Bruno/F1-infra/
  releases/<版本>/workspace/F1-infra/
  current -> releases/<版本>/workspace/F1-infra/
  runtime/       日志、控制码摘要、审核、录制和可选 Pico 运行数据
  systemd/       本项目用户服务
```

激活 `current` 后，按 [AUTOSTART](AUTOSTART.md) 安装本机用户服务。移动开发目录或上传共享 ZIP 都不会替机器人执行上述部署步骤。

## 5. 分发与校验

从开发源码根运行：

```bash
python3 deploy/package_shared.py
```

产物位于 `results/share_packages/`，该目录不会再次进入 ZIP。包内顶层为 `F1-infra/`；进入该目录执行 `sha256sum -c SHA256SUMS`。ZIP 旁的 `.sha256` 用于核对整个压缩包；Windows 可用 `Get-FileHash <zip路径> -Algorithm SHA256`。

共享位置为 `\\shfile.huaqin.com\机器人软件共享盘\具身\F1-Infra`，Ubuntu 文件管理器可打开 `smb://shfile.huaqin.com/机器人软件共享盘/具身/F1-Infra`。打包工具只生成本地产物；复制完成后读回核验才算上传完成。

Windows 解压/中转可能丢失可执行权限，可用 `bash 脚本.sh` 或在 Ubuntu 恢复对应脚本的可执行位。日志与运行结果写在 runtime，不能混入源码或再次分发。
