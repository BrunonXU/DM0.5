# F1-09 上肢 HG-DAgger

START／→／↑／Y的控制职责保留。未↑而交还VLA或Y时，局部终态在交还前冻结，语音提醒“RLT确认按钮”，结果待审核；不再默认失败0。人工接管本身不判失败，成功标签也不自动批准全部人工尝试为专家。

同会话输入短暂失鲜先保持并重新规划；HIL仍须新鲜回中和新RB，真实断线与危险候选仍需明确恢复。当前部署与Windows版本以[STATUS](STATUS.md)为准。

## V45的新操作语义

适用范围：2026-11-03控制V45已部署；Windows V45整包已上传待安装，现场验收待完成。历史版本说明保留，勿覆盖新客户端。

开发源码入口为 `F1/src/f1dagger/`（独立发布包为 `src/f1dagger/`），复用 DAgger 的 F710 协议、接管状态机、策略 chunk 调度和异步录制。G2Teleop 与 G2 生产启动脚本未改。F1 工作台和 Pico 专用录制器不参与本流程。

> 本页保留已验证的 v15 稳定入口和完整合同；三路线版本、后续记录和当前数据目录以 [STATUS](STATUS.md) / [三端操作](HIL_WORKBENCH.md) 为准。

2026-10-23 工作区整理后，6000D 的 LeRobot 源码已移到 [Libraries](../../../workspace/Libraries/README.md)，导出环境使用下方新路径；绑定版本和源码内容保持原样。历史机器人发布包不随开发机目录调整。

2026-11-03 网络入口更新：下面仍适用于旧 v15 的连接命令改为有线优先，Wi-Fi 保留备用；不改变该旧入口的模型、协议或发布结论。当前单窗口和持久连接设置见[Windows入口](../../F1/windows_client/README.md)，不要用本页旧 sender 覆盖新版操控包。

## v15 稳定入口与连续限位

**v15 已有本地回归、ARM数值和真实反馈零输出shadow记录；原稳定入口的 `--check` 不初始化硬件。用户后续已确认顿挫消失，记录见 [STATUS](STATUS.md)，完整过程见 [验证记录](F1_VALIDATION.md)。**

v15采用可行增量QP和连续轨迹边界检查，并将目标领先约束纳入QP，消除超过门槛时整臂回拉的顿挫。版本关系以 [STATUS](STATUS.md) 为准；使用旧稳定入口前可用其 `--check` 核对配置。普通关节/腕部限位时RB可保持按住，直接反向或沿可行方向移动，不需要重新START/选臂；RT仍独立生效。真实碰撞、断线、输入/反馈失效继续停止。离线通过不等于现场动作通过，历史验收依据保留在 F1_VALIDATION.md，当前摘要在 STATUS.md，实机应核对实际accepted配置。

机载验证已通过，使用稳定入口 `bash /home/user/Bruno/DAgger/start_f1_teleop.sh`；`--check`只检查版本/配置，不创建动作输出。默认入口仍需现场Enter和手柄START。启动时检查 `F1 IDENTITY` 中release为 `20261009-f1-hgdagger-v15`，核对版本一致后再使用下列旧稳定入口命令。Windows隧道不变；sender仅更新F1包装入口，支持ACK版本字段。相机/4242基础启动引用仍保持v8。

## 数据与控制合同

固定顺序：左臂 J1–J7、右臂 J1–J7、左夹爪、右夹爪。手臂使用 HAL 绝对度数，夹爪 0–100（0 开、100 闭）。state 来自真实反馈，action 来自本控制进程实际发布目标；发布不等于机器人已到达。头部和身体反馈参与 FK、碰撞和新鲜度检查，不进入 action16。

修正后的默认三图 `head/left_wrist/right_wrist`、30 Hz 采集及动作时间轴、30 fps 导出、H=50（约1.67秒）；目标检查 50 Hz，手臂输出调度 250 Hz。操作系统调度超时会停止追目标，频率需在 F1 shadow 阶段实测。旧 v1–v4 使用了10 Hz新适配默认；用户指出现有数据合同为30 Hz后已纠正，旧记录不得补帧冒充30 Hz。网页预览频率与数据采集独立。

2026-10-08 v8–v11 修复持续图片写盘积压、补充 F1/Windows 执行模式与队列诊断，并将两腕彩色/深度启动参数固定为 640×480×30；头部仍使用原 30 fps 模式。实机部署、持续帧率、零输出 shadow 与现场动作验收分别记录于 [验证记录](F1_VALIDATION.md)，不能将配置频率当作实测结果。模型端点尚未提供。

发布模板保持 `ik.verified=false`；通过绑定版本的 ARM 数值验收和真实反馈 shadow 后，使用独立的机器运行配置记录证据并启用该门。该标记只表示软件数值/保护配置验收，不表示物理标定、Windows 手柄或实际运动已验收。真实动作由现场操作者通过本地 Enter 和 F710 START 启动。

## 依赖与发布

- 显式 `F1_INFRA_ROOT` 选择完整 F1-infra release，`F1_PROFILE_PATH` 选择 F1-09 当前机器配置。加载时校验 `configs/f1_infra_binding.json` 中的源文件和资产 SHA256，不隐式跟随工作台的源码变动。
- 数值依赖见 `configs/f1_runtime_requirements.txt`；ARM64/Python 3.12 解析锁为 `configs/f1_runtime_arm64.lock`，Ruckig 需目标机构建或核验复用现有 ARM 构建，不可搬运 x86 venv；另需现有 ROS 环境的 rclpy、机器人/AGV 消息与实际命令监测库。机器人 Python 版本须匹配其 ROS（当前启动文件使用 Jazzy）。本地 Python 3.10 数值验证不代表 ARM64/Jazzy 环境已验收。
- LeRobot 导出在 6000D 的独立 Python 3.12 CPU 环境运行，依赖见 `configs/f1_export_requirements.txt`；源码绑定 `configs/f1_lerobot_binding.json`。
- 运行数据写入独立的 `/home/user/Bruno/...` 目录；源代码与机器工作台数据目录分开。

开发机在 DAgger 根目录生成包（不部署，不启动）：

```bash
python3 F1/scripts/53_package_f1.py --output /tmp/f1-dagger-release.tar.gz
```

包中 `F1_RELEASE_MANIFEST.json` 逐文件绑定哈希；外部 F1-infra、LeRobot 和 ROS 不重复打包。目标机器必须具备匹配的依赖，否则启动报出具体不一致文件。外部依赖升级应重新核对合同和数值测试，再生成新绑定与新发布包。

## F1 终端：先检查和 shadow

将包放入选定的 DAgger release。通用安装复制 `configs/f1.env.example`；既有 F1-09 部署使用已现场核对的 `configs/f1_09_site.env.example`，其中独立 Python 环境已在 ARM64 上安装并通过数值/ROS 只读验证。`F1_CONTROL_LOCK` 必须与当前工作台 installation.env 的值完全一致，不能另建一把锁。下面路径明确指向保留的v15入口，ARM、shadow和后续用户反馈分别见验证记录与STATUS，不用它推断三路线当前版本。

```bash
cd /home/user/Bruno/DAgger/releases/20261009-f1-hgdagger-v15/DAgger
export F1_DAGGER_ENV="$PWD/configs/f1_09_site.env.example"
set -a
source "$F1_DAGGER_ENV"
set +a
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
bash scripts/50_f1_hil.sh inspect
bash scripts/50_f1_hil.sh preflight
bash scripts/50_f1_hil.sh shadow --task "双臂夹取纠正"
```

`inspect` 仅校验本地文件和依赖可用性；`preflight` 核对本机 runtime contract，不创建动作 publisher。`shadow` 订阅新反馈和三图、运行 IK/安全检查与虚拟输出，始终不创建动作 publisher，原始记录标记 shadow，不能导入专家纠正集。shadow 首次 START 进入人工，不请求模型。

三个终端准备好后，用 shadow 逐侧检查十字键、RB、摇杆方向、A、RT、Y、释放保持、断线及重连。检查 `journal/control.jsonl` 的 `ik_diagnostics`、候选与 selected，以及 `events.jsonl` 的拒绝原因。shadow 使用真实反馈，所以 Home 无真实到位会超时，这是零输出下的预期；真实到位须现场纯遥操验收。

## Windows 终端 1：直连隧道

本轮运行端使用v15（已通过ARM与真实反馈shadow，现场动作待验收）；相机与 4242 基础启动已保存的引用继续使用验证过的 v8，不需要重启相机、4242 或本体驱动。先完成下方“F1 终端 3”的本地启动，再打开这两个 Windows 窗口；三个窗口均保持运行。

Windows 与 F1 在同一现场网络时，在 PowerShell 执行：

```powershell
cd "$HOME/DAgger-F1"
ssh -F NUL -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:7907:127.0.0.1:7906 user@192.0.2.7
```

输入 F1 的 user 密码后没有新输出是正常现象；此窗口就是隧道。`-F NUL` 跳过已有 SSH 配置中的无关 7890 转发，不要在用户名、下划线或 `@` 前加反斜杠。2026-10-08 已重新核对 F1 Wi-Fi 地址为 `192.0.2.3`；若不在同一网络，直连命令不能替代站点已验证的跨网 SSH 路径。

上句 Wi-Fi 地址核对是历史记录；2026-11-03 已启用的有线地址为 `192.0.2.7`。有线不可达时，先退出旧隧道，再将 SSH／SCP 目标显式改为 `user@192.0.2.3`；运行中不自动切换。

转发关系为 Windows `127.0.0.1:7907` → F1 `127.0.0.1:7906`；F1 接收端仅监听 loopback。同一台 Windows 保留一个该隧道窗口，旧隧道仍占用 7907 时应结束旧窗口后再启动。

## Windows 终端 2：更新并启动 F710 sender

另开 PowerShell。现有 `DAgger-F1` 客户端只需替换 F1 包装入口；从已部署的 Windows 模式提示补丁目录复制并核对 SHA256（机器人运行端用 v15，Windows sender 更新版本字段解析），其余 reader、mapper、协议和 CMD 入口沿用现有版本：

```powershell
cd "$HOME/DAgger-F1"
scp -F NUL user@192.0.2.7:/home/user/Bruno/DAgger/releases/20261009-f1-hgdagger-v15/DAgger/windows_client/scripts/03_windows_f1_sender.py ./scripts/03_windows_f1_sender.py
if ($LASTEXITCODE -ne 0) { throw "F1 sender 更新失败，请保留报错" }
$senderHash = (Get-FileHash ./scripts/03_windows_f1_sender.py -Algorithm SHA256).Hash.ToLowerInvariant()
if ($senderHash -ne "b4a65812e2251e530e13054d6dcf008e735cd18b075c41237cbede3dccd9ba82") { throw "F1 sender 版本校验失败" }
./scripts/windows_f1_sender.cmd
```

默认使用 `$HOME\.venv\Scripts\python.exe`，沿用之前已能识别 F710 的 Python/pygame 环境；只有安装位置不同才在启动前设置 `$env:F1_SENDER_PYTHON` 为实际 Python 路径。F710 顶部 X/D 开关使用 X；正面 MODE 使用摇杆与十字键不互换的模式（MODE状态灯灭）。在 joy.cpl 中推左圆形摇杆应移动 X轴/Y轴十字光标；若移动“视角头盔”，先停止sender并按MODE恢复。启动时先释放 START、RB 和摇杆。

等 sender 收到应用 ACK，确认模式为 `F1 POWERED` 或 `F1 SHADOW` 且 acked 持续增加。仅 `TCP Connected` 不证明接收端已工作。`POWERED + waiting_for_teleop_start` 时，释放 START 后单击进入人工，正常回执变为 `hil_latched`；SHADOW 的同样交互只验证输入，不会运动。若仍为等待态，先查看本轮持久日志 `C:\Users\user\DAgger-F1\logs\windows_sender_console.log` 和 F1 终端，不要通过延长 ACK/watchdog 掩盖故障。

sender 使用现有 reader、mapper、意图协议、ACK 和重连。F1 包装入口先完成手柄初始化再建立 TCP，并关闭传统 Windows 控制台的 QuickEdit（退出时恢复），运行提示显示 F1。冻结 G2 sender 不变。操作：

| 按键 | 行为 |
|---|---|
| START | run 首次启动策略；之后进入人工／申请恢复策略。teleop/shadow 首次进入人工，之后人工与 HOLD 暂停之间切换 |
| 十字键 | 选左／右臂 |
| RB + 摇杆 | 相对末端控制选中臂；松 RB 保持人工模式并停止该臂目标推进 |
| A | 沿用原速度档交互 |
| RT | 选侧并释放十字键后，按放一次切换夹爪开/闭，映射为 `grip_target × 100`；不需 RB，持续按住只触发一次 |
| Y | 人工模式且 RB 松开时，渐进回到「积木demo-V0」双臂角度，腰、头和夹爪保持现状 |

接管/重新选侧以已下发夹爪目标为基准，不回放旧 RT latch；需要新的 RT 开合变化。 v12修复已观察到该侧空RT基线后第一次按键被吞掉的问题；未知侧首次到达的非空旧锁存仍不自动执行。Y完成后需重新选侧。退出人工需释放 RB，并等 Home/夹爪命令完成；被拒绝的 START 需条件满足后再按。断线 SAFE_HOLD，重连不会自动恢复；新 START 先返回人工。Home 不计入专家纠正。

v14在QP内处理关节/耦合腕部约束，可行方向继续运动，不积累不可达目标。输出提前验证整条轨迹，普通同步不可行时尝试相位同步，仍不可行时连续制动；保持HIL、选侧、RB授权和RT。`ik_diagnostics.constraints`记录约束及丢弃增量，`limit_control`记录`certified_target/limit_braking/limit_at_rest`。真正断线、输入过期、反馈失效、碰撞及未知求解/输出故障仍保持SAFE_HOLD或停止，不自动恢复。

如果按 START 后不确定是否生效，先查看 sender 回执：`waiting_for_teleop_start` 表示仍等待启动；`hil_latched` 表示已进入人工；`safe_hold` 表示保护保持。shadow 运行时机器人始终不运动。发布失败会在原始事件流记录本次失败阶段和已经完成的 topic，运行异常另存 `failure.txt`；不要只凭 TCP Connected 判断控制状态。

## F1 终端 3：纯人工与策略

v15 使用独立运行配置 `/home/user/Bruno/DAgger-runtime/f1-09-accepted-v15.json`，必须绑定本版发布manifest、ARM数值和真实反馈shadow报告；最终安装状态见[验证记录](F1_VALIDATION.md)。软件验收不代替现场触限反向和实际到位检查。

正常结束占用 7906 的旧运行端后，在机器人旁的 F1 本地终端执行完整命令：

```bash
bash /home/user/Bruno/DAgger/start_f1_teleop.sh --check
bash /home/user/Bruno/DAgger/start_f1_teleop.sh
```

该命令不会请求模型。看到 `Press Enter, then use F710 START to begin` 后，由机器人旁的操作者按 Enter；随后等 `F1 POWERED (teleop): 127.0.0.1:7906; waiting for START` 才启动 Windows sender。收到 POWERED 模式 ACK 后再单击 START。手柄使用方式见上表；第一次按左/右十字键选臂并释放，再按住 RB 小幅推动摇杆，松开 RB 保持。

同一进程取得工作台共用控制锁、检查外部实际命令、校验机器合同；本地 Enter 后仍等待新的 F710 START。4242 网页可保持打开并只读查看，不需要关网页或降低正式采集率；在 DAgger 控制期间不要从工作台发姿态、夹爪、腰部或其它运动命令。工作台、Pico 与 DAgger 共用控制锁，其它遥操／姿态控制会互斥。手臂、双夹爪和 Home 都由该进程发布，身体／头部／底盘没有输出。第一次 START 再次读取新鲜反馈，防止初始化后等待期间机器人状态变化。

v8 原始图片采用三个 `.pack` 文件，JPEG/PNG 字节保持原样，`frames.jsonl` 保存每帧偏移、长度和 SHA256。每 15 组观察或 0.5 秒批量同步图片与索引，256 项/192 MiB 的在途上限保持不变；写入失败仍终止控制并保留 `.partial`。旧逐图记录仍可审计和导出。v10 使用每路32帧、合计64MiB的有界真实回调队列，保留成批到达的图像；仅首次采集时启用，等待Enter不积压。`frame_id`来自实际ROS回调序号，主机接收时间严格递增，原始设备时间戳照存。头部SDK偶尔为不同像素帧给相同global stamp；只有声明真实回调身份的记录允许相等。v11 对单个源时间戳回退帧明确丢弃并记录断流，后续正常帧可以继续；接收时钟回退、无效时间戳仍停止，持续两秒无合格新三图仍超时。旧记录的严格时间规则保留。容量溢出明确报错，age/skew/源时间戳回退淘汰有计数并形成训练片段边界。v11 的 F1 采集使用绝对节拍，避免每轮线程唤醒延迟累积；G2 默认等待方式不变。策略仍要求整个图区间和 state16 晚于 START/交接；导出要求整个观察区间已有同一人工 epoch 的实际控制。

Ctrl+C 正常关闭，先释放动作端点，再结束采集并排空写盘；不要直接断电中止写盘。失败运行保留不完整证据，不会自动标为可训练。`--recording-mode` 默认 `hil-record` 保存连续原始数据；也可使用 `prebuffer-record` 或 `inference-only`，后者不具备完整训练导出条件。

真实策略模型地址、模型身份及16D合同确认后，在同一已加载环境的 F1 本地终端配置 `policy.endpoint/model_id`，再用：

```bash
bash scripts/50_f1_hil.sh run --execute \
  --config /home/user/Bruno/DAgger-runtime/f1-09-accepted-v15.json \
  --task "双臂夹取纠正"
```

恢复策略会先 HOLD，丢弃旧 chunk/平滑器待执行目标，仅用交接之后接收的新三图和状态发起新 epoch 请求。过期或旧 epoch 响应无法重新取得控制权。F1还逐字段要求state16的本机接收时间晚于首次START／交接边界；新图配旧状态会继续等待，既不请求模型，也不改变录制频率。

## 模型服务接口

`GET {endpoint}/health`：

```json
{"contract":"f1_upper_16d_v1","ready":true,"model_id":"your-model-version"}
```

`POST {endpoint}/process_frame` 为 multipart：`metadata` JSON，加 `head`、`left_wrist`、`right_wrist` 三个原始压缩图片字段。metadata 包含：

```json
{"contract":"f1_upper_16d_v1","request_id":1,"observation_id":12,"policy_epoch":3,
 "observation_monotonic_ns":123456789,"state16":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
 "task":"任务文本","chunk_steps":50,"frequency_hz":30.0}
```

响应必须回显 `contract/request_id/observation_id/policy_epoch/observation_monotonic_ns/frequency_hz`，并提供已绑定 `model_id` 和有限、完整、合法限位的 `actions: H×16`。频率、H 可配置；默认 30 Hz，50 步。不做隐式单位、偏置、手型或维度转换。时间戳来自机器人主机 monotonic 时钟，服务回显即可。默认超时和观测最大年龄均 2 秒，超过则 SAFE_HOLD。

## 6000D：审计、人工审阅、导出

将完整 `raw/<run_id>`（含 journal、payload、模型 binding 和 preflight）复制到 6000D `artifacts/data/` 内相应原始目录，保留不变。优先复用已有独立导出环境；首次准备时，在开发机 `/home/user/projects/DAgger/F1` 执行：

```bash
python3.12 -m venv /tmp/f1-export-env
/tmp/f1-export-env/bin/pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
/tmp/f1-export-env/bin/pip install -r configs/f1_export_requirements.txt
/tmp/f1-export-env/bin/pip install --no-deps -e /home/user/Libraries/third_party/lerobot
export F1_EXPORT_PYTHON=/tmp/f1-export-env/bin/python
export F1_LEROBOT_ROOT=/home/user/Libraries/third_party/lerobot
bash scripts/51_f1_export.sh audit /path/to/raw/run_id
bash scripts/51_f1_export.sh review-template /path/to/raw/run_id --output /path/to/review.json
```

使用离线审阅入口提取原始图片并生成同步浏览页面（新旧格式均支持，输出到独立新目录）：

```bash
PYTHONPATH=src:../shared/src /tmp/f1-dagger-check/bin/python -m f1dagger.review /path/to/raw/run_id --output /path/to/new_review
```

打开生成的 `index.html`，审阅三路原始图、`control.jsonl` 的 human_candidate/selected/submitted/measured 和事件边界，在 review.json 填写 `reviewer`、明确接受的 `accepted_interventions`（例如 `[1,3]`）及 notes。模板默认不批准任何段；单个 intervention 内质量不合格时不要批准该 intervention。

```bash
bash scripts/51_f1_export.sh export /path/to/raw/run_id \
  --review /path/to/review.json --output /path/to/new_dataset --fps 30
```

仅已审核、持续 HIL 且有真实发布来源的片段进入训练。POLICY、Home、交接、SAFE_HOLD、断流均切断 episode；正常 human_hold 保留。时间对齐使用本机实际接收时间，取不晚于 observation 的最新发布目标，保留夹爪上次真实发布时间、原始图像时钟、记录索引。采用 30 fps 最近观测网格，不复制缓存图，不用反馈冒充 action，不插造观测。

输出为固定版本 LeRobot v3、H.264 三视频、float32 state/action16；`f1_source.jsonl` 逐帧追溯原记录，`f1_review.json` 保存人工审阅，`f1_audit.json` 分开记录原始结构、人工接受、writer 绑定及 reader 回读结果。所有帧的视频和 state/action 成功回读后才原子发布目录并置 `training_ready=true`。失败保留 `.partial`，同名输出不覆盖。原始数据始终 `training_ready=false`；相机曝光时钟未标定，不能将主机接收对齐解释为硬件同步。

训练窗口必须在单个 LeRobot episode 内构造，不可跨 episode 拼接。模型训练另开任务。


## Wi-Fi DDS 拥堵与相机启动

2026-09-28 抓包确认原三相机 FastDDS 在 wlan0 向 `239.255.0.1:7401` 大量发送数据多播（约32Mbps），阻塞手柄 ACK；4242 HTTP 不是主要流量，当时网页每路约1.33fps。相机数据应留在内部网络，不能靠降低 DAgger 采集频率处理。

`configs/f1_camera_fastdds_internal.xml` 和 `scripts/60_f1_camera_internal.sh` 提供三相机专用内部网口 UDP 白名单并保留 SHM。只用于原相机 FastDDS 环境，不应用到整个系统所有驱动；头部启动参数不变，两腕明确使用彩色/深度 640×480×30；不通过降采集频率处理网络拥堵。2026-10-08 三相机已应用该隔离并完成真实新帧核对；4242 profile 的 `startup_script` 与 SHA 已绑定经过备份的 v8 Bruno 启动副本，原厂脚本未修改。未来整机重启或重新基础启动后的恢复仍需当次验证，不能将已保存配置写成已完成重启验收。

临时 `scripts/59_f1_wifi_camera_guard.py` 使用独立 nft 表，只令已确认三个相机 socket 的 WLAN 数据多播 TTL=0，保留本机多播；相机重启端口变化后不能沿用它作为长期配置。原始规则备份及新三图/状态验证失败自动回退均在脚本中。它需现场 sudo，不启动动作或重启进程。只删除本表的回退命令为 `sudo python3 /home/user/Bruno/DAgger-runtime/59_f1_wifi_camera_guard.py rollback`。当前应用、证据和待办以验证记录为准。

每个源码包的 `startup/` 是该版本的候选入口；历史 v13 运行端升级时，不重新应用随包 `profile_patch.json`，现役相机与4242保持已验证的 v8 引用。只要当前三路反馈正常，无需重新执行相机启动、旧临时 nft 规则或网页基础启动。后续若更新相机配置，应重新备份并验证原启动脚本SHA、逐路新帧及网络；不要重启包含电机节点的整组 base-start 服务来试相机。


## 持续零输出验收

在 F1 上加载前述站点和 ROS 环境后运行：

```bash
"$F1_DAGGER_PYTHON" scripts/63_f1_shadow_soak.py \
  --runtime /home/user/Bruno/DAgger-runtime/shadow-soak-new-run \
  --minimum-capture-hz 29.5
```

使用独立 19398 端口，只启动 shadow，先持续录制等待 START 75 秒，再连续模拟输入 120 秒及断连重连。工具校验监听 PID、ACK 会话/序号/SHADOW 身份、真实三图帧率、图像尺寸、原始审计和无动作发布。输出 `console.txt` 与 `soak_report.json`；模拟输入不能作为专家纠正或真实 F710 验收。正式 7906 端口留给现场操作者。
