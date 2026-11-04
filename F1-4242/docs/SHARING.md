# 部署、换机与源码交付

日常安装先按 [README](../README.md) 的四步完成；首次接入新机器人时，由部署人员在本页核对环境、机器配置和动作条件。网页启动成功与机器人动作验收是两个步骤，模板保持动作关闭。

Pico、手柄 DAgger、MP4 服务器和训练导出分别按需准备。基础工作台不要求安装全部可选环境；同机升级保留实际配置与数据，换机重新建立本机配置。

独立开发目录名为 `F1-4242`，共享 ZIP 解压根名为 `F1-infra`。两者内容相同，不需要外层 `F1/` 工程。对外使用入口统一为 `http://机器人IP:4242/`。

## 包内范围

包含工作台、ROS 适配、姿态/数据/录制模块、测试、发布工具、前端依赖和模型；可选上肢 Pico 副本位于 `integrations/pico_upper/`。不含原 F1 工程、SSH 连接目录、运行数据、凭据、备份、Git 历史或虚拟环境。

厂商 ROS/驱动、EC-Master、LXL 原工程、PCService、XR SDK 和 ARM 二进制依赖复用目标机器人现有安装。上游/模型来源及许可证留在各自清单内。历史引用见 [来源索引](history/SOURCES.md)；当前已记录部署见 [STATUS](STATUS.md)。

## 1. 配置目标机器人

新安装优先在目标机器人上的项目根目录执行 `bash setup.sh`，只确认名字、可访问 IP、数据目录和可选模块。工具检测已有环境并生成配置，规则与异常处理见 [初始化说明](INITIALIZATION.md)。以下表格供部署人员核验，不要求普通用户逐项填写。

只有需要手工适配时才从模板开始（不要覆盖已生成的文件）：

```bash
cp -n configs/installation.env.example configs/installation.env
cp -n configs/robot.example.json configs/robot.json
```

`installation.env` 为 Bash 环境文件，由 `run.sh` 自动读取，也可用 `F1_INSTALLATION_ENV` 指向另一文件。模板里的 `HERE` 由启动脚本设置为当前源码根。不要把密码、私钥或控制码写入安装配置。

| 安装配置 | 必须核对的值 |
|---|---|
| `F1_INFRA_HOME` / `F1_CONSOLE_RUNTIME` | 安装根 / 运行数据根，初始化使用实际解压目录及其 `runtime`；手工模板中的 `~/Bruno/F1-infra` 只是示例 |
| `F1_PROFILE_PATH` | 本机 `configs/robot.json` 的完整路径；网关与录制/遥操子进程共用 |
| `F1_SITE_PATH` | 初始化生成的 `configs/site.json`，仅保存显示名字与网页 IPv4；旧安装未设置时沿用 profile |
| `F1_CONSOLE_PORT` | 对外部署保持 `4242`，启动命令与自启服务使用同一端口 |
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
| `id` / `robot_id` | 稳定的机器身份；新初始化自动生成，不用昵称替换已有身份 |
| `label` / `ip` | 新安装由 `site.json` 覆盖显示和网络字段，IP 参与 HTTP Host 校验；旧安装仍读 profile |
| `expected_machine_id` | 目标机器 `/etc/machine-id` |
| `controller_binary_sha256` / `expected_motor_parameters` | 正在运行的 motor_ctl 二进制及实际参数；不是只核对 launch 文件 |
| `controller_parameter_rmw` | 读取控制器参数所用 RMW；可与反馈订阅分开配置 |
| `topics` / `camera_topics` / `groups` / `base_frame` | 消息、具名关节、三相机、底盘坐标与单位 |
| `joint_contract` | 本机22轴配置基线；F1-09绑定`f1-09-joints.json`，不能将其当成其他机器的标定，见[全身配置](JOINT_CONFIGURATION.md) |
| `joint_limits_deg` / `limits` | 两份限位一致，按本机确认方向、零位、速度和反馈阈值 |
| `command_monitor_binary` / `command_monitor_sha256` | 本机编译的命令来源监听器路径和 SHA256，可用 `${F1_CONSOLE_RUNTIME}` 路径变量 |
| `default_presets` / `full_home_source` | 本机核验过的预设、原生零位文件路径和内容 SHA |
| `startup_script` / `ec_startup_script` / `startup_script_sha256` | 本机现场启动脚本位置；基础启动脚本绑定经审阅的内容 SHA |
| `data_path` | 原 MCAP 只读根；新统一录制写入 `runtime/storage.json` 的 `root`，审核和运行记录写入 runtime |
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
source configs/installation.env
source "$F1_ROS_SETUP"
cmake -S robot/command_monitor \
  -B "$F1_CONSOLE_RUNTIME/command-monitor/build"
cmake --build "$F1_CONSOLE_RUNTIME/command-monitor/build" -j2
sha256sum "$F1_CONSOLE_RUNTIME/command-monitor/build/libf1_command_monitor_shared.so"
```

将实际路径和 SHA 写入本机 profile。不要从 x86 开发机复制 `.so` 到 ARM。确认身份、反馈、控制源与限位合同后，由现场操作者逐项验收动作。

**操作入口：** 无需配置安全码或访问码。打开平台阅读一次操作须知；点击执行直接提交。升级时旧凭据文件仅留在既有备份/runtime供回退，新版不读取、不生成、不验证；源码包不再提供设置工具。

**录制：** 使用独立 Pico 录制时，配置 `F1_RECORDING_PYTHON` 的兼容 Python 3.12/ROS 环境，运行 `bash recording/run.sh --check` 检查依赖，不创建录制节点。新统一录制还需按 [UNIFIED_DATA](UNIFIED_DATA.md) 准备 runtime 中的 `storage.json` 和 `recording.json`，原图落盘与训练导出分开。旧内联编码环境的 numpy 1.26.0、pyarrow 20.0.0、ffmpeg/ffprobe 记录及来源合同保留在 [RECORDING](RECORDING.md)，不要将旧 10fps 配置套到新 30Hz 流程。

**Pico：** 需要 numpy、ruckig、placo/placo_utils、meshcat、mujoco、coal、PyYAML，和本机 XR SDK/PCService。安装环境字段完成后，从上肢目录执行 `bash check.sh --dependencies`。现场启动及按钮含义见 [Pico README](../integrations/pico_upper/README.md)。

**手柄 DAgger：** 独立部署匹配的 F1 运行端与 Windows sender，并配置 `runtime/hil.json`、`runtime/hil-routes.json` 及对应路线。工作台源码 ZIP 不包含 DAgger 运行端、Windows sender 或模型服务；使用 Pico 不需要安装它们。见 [HIL 接入](HIL.md)。

**训练导出：** 只有转换训练数据时才准备固定版本 LeRobot 及对应来源适配依赖，见 [统一数据导出](UNIFIED_DATA.md#离线转换与审核)。打开网页和保存原始录制不等于已经安装训练环境。

### 可选视频服务器

新 HIL / 统一 Pico 原图在机器人本地保存，采集和提交原件不依赖服务器。当前数据页（2026-10-22 起）默认播放 MP4，未生成视频时显示等待状态；原图接口仍保留，但当前详情页没有原图浏览入口。不能将“本地原件存在”写成“未配服务器也能直接在当前页面播放”。

需要完整会话 MP4 时，另配 `scripts/session_preview_worker.py` 的服务器环境、工作台连接地址与缓存目录，流程为读取原件、服务器编码、回传机器人。当前 worker 使用免密码兼容会话，不需要旧 `access_token_file`。已回传的视频由机器人提供，后续回看不要求服务器缓存一直保留。支持范围、运行条件与传输细节见 [DATA](DATA.md#完整会话审阅视频与目录浏览v28) 和 [直接操作接口](DIRECT_OPERATION.md)。统一 Pico 的视频派生仍按其 [数采说明](UNIFIED_DATA.md) 单独处理，不承诺此 worker 自动支持全部来源。

这条视频链路与训练导出、中央归档上传分别配置；中央归档上传默认关闭。共享盘用于源码分发。

## 4. 版本部署与自启

**同机升级：** 先结束本项目的动作、录制和遥操，备份实际在用的源码、本机配置和服务单元；检查现场修改后合入新版本。保留 `installation.env`、`robot.json`、已有 `site.json`、runtime 配置、原始数据及上一已验证版本。初始化用于新安装，不用重新运行；若源码路径改变，核对安装配置中的绝对路径。源码回退不会回滚新增数据或审核记录。

**迁移新机：** 重新运行 `bash setup.sh`，再核对驱动、DDS、话题、关节与限位，在目标架构构建监听器；不复制旧机的身份、启用标记或虚拟环境。历史数据如需迁移，单独复制、校验并调整数据入口；不要与源码升级混为一次覆盖操作。

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
  runtime/       日志、审核、录制和可选 Pico 运行数据
  systemd/       本项目用户服务
```

激活 `current` 后，按 [AUTOSTART](AUTOSTART.md) 安装本机用户服务。移动开发目录或上传共享 ZIP 都不会替机器人执行上述部署步骤。

## 5. 维护者本地打包与共享盘分发

对外源码包只使用 `deploy/package_public.py`、已审核的 clean staging 与 `deploy/public_docs/`。`deploy/package_shared.py` 用于内部快照，可能包含内部文档和开发记录，不能直接分发；用户当前授权的发布范围优先。

当前已审核 V1.0 的 ZIP、SHA 和同内容解压目录均在 `/home/user/Artifacts/F1-Infra-V1.0-20261023/`。本次上传直接使用下列冻结文件，不从当前并行开发源码重新打包，也不混入之后的功能改动：

```text
F1-infra-V1.0-20261023.zip
F1-infra-V1.0-20261023.zip.sha256
```

后续新版本先准备独立 clean staging，审阅代码与配置变动，再维护公开文档。包内仅保留必要代码、资源、模板和使用/配置/结构说明；排除 `AGENTS.md`、内部文档、`history/`、`results/`、实机配置、个人记录、日志、运行数据、凭据、缓存、虚拟环境及历史备份。保留第三方署名、许可证和必要来源清单，清除其中的个人路径，不改写原始来源 SHA。

以下命令模板仅用于下一次发布，需先替换版本和路径；审核报告放在私有 results 中，不进入公开包：

```bash
python3 deploy/package_public.py \
  --source-dir "results/<新发布版本>/clean/F1-infra" \
  --docs-dir deploy/public_docs \
  --output-dir "results/share_packages/<新发布版本>" \
  --extract-dir "results/public_previews/<新发布版本>" \
  --audit-report "results/<新发布版本>/public-audit.json" \
  --version "<版本>" --stamp "<YYYYMMDD>"
```

新包须完成隐私扫描、人工复核、路径/符号链接检查、模型资源校验和独立解压后的初始化与离线启动。进入包内唯一顶层 `F1-infra/` 执行 `sha256sum -c SHA256SUMS`；ZIP 旁的 `.sha256` 校验整个压缩包，Windows 可用 `Get-FileHash <zip路径> -Algorithm SHA256`。离线检查不能替代新机器上的模块接入和现场动作验收。

共享位置为 `X:\具身\F1-Infra`，无映射时为 `\\company.example.invalid\机器人软件共享盘\具身\F1-Infra`；Ubuntu 文件管理器使用 `smb://company.example.invalid/机器人软件共享盘/具身/F1-Infra`。按以下流程交付：

1. 上传前核对共享目录和已有文件。只上传已审核 ZIP、对应 `.sha256` 和简短共享 README；本地发布核对、审核报告、原始记录与解压预览目录不随传。
2. 替换共享 README 前，将旧文件及 SHA 保存到本地本次发布记录。遇到同名 ZIP 或 SHA 文件先比较内容：相同则复用并核验；不同则保留原件，查清版本归属并按用户授权处理，不能静默覆盖。
3. 复制完成后从共享盘读回 ZIP、SHA 和 README，核对与本地待交付文件一致，再记录共享位置、文件名和校验结果。完成读回前只能标“待上传/待核验”；认证不可用或传输失败不算上传成功。
4. 只有用户授权清理旧包时才删除本项目对应旧交付物；不做删除式同步，不删除无关共享文件。

Windows 解压/中转可能丢失可执行权限，可用 `bash 脚本.sh` 或在 Ubuntu 恢复对应脚本的可执行位。日志与运行结果写在 runtime，不能混入源码或再次分发。
