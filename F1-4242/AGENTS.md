# F1-infra Agent 工作约定

适用于本项目全树。项目独立于原 `F1/`：不读取父目录的 AGENTS、连接配置或运行目录，也不复制原 F1 工程来补齐依赖。用户当前指令优先；日常源码修订、离线检查与授权范围内的打包可直接完成。

## 文档与事实

1. 先读根 README、[文档索引](docs/README.md)、[版本状态](docs/STATUS.md)；按任务阅读相关主题。
2. 根 README 给人类，只保留项目用途、必改配置和最短启动入口。Agent 流程写在本文；接口/部署/验收细节写入 `docs/`，旧设计移入 `docs/history/`。
3. `docs/STATUS.md` 区分已部署机器人版本、开发副本与待验收项。开发目录移动或 ZIP 生成不等于机器人部署成功。
4. 私有 `results/`、原 F1 来源快照和其他账号原路径是历史证据，见 [来源索引](docs/history/SOURCES.md)。不要保留指向包外文件的 Markdown 链接；可保留带明确“历史来源”标注的路径文字。
5. 不将测试成功、依赖就绪、ROS 发现、发布哈希和真机动作验收混写为同一个“通过”。

## 工程与配置入口

| 位置 | 责任 |
|---|---|
| `server.py`、`core.py` | HTTP、认证、控制租约、预设与动作生命周期 |
| `robot/` | ROS 反馈、运行合同、命令来源监听与动作适配 |
| `web/` | 无 npm 构建的前端、本地 Three.js、URDF/STL |
| `recording/`、`recording_service.py` | 独立16D数采（新配置30Hz）、来源校验、统一v3导出、提交与恢复 |
| `teleop/`、`teleop_service.py` | 固定脚本合同、Pico 会话监督与明确确认 |
| `integrations/pico_upper/` | 随包提供的可选独立上肢适配及必要上游源码 |
| `configs/installation.env.example` | 路径、ROS/DDS、Python 与可选集成的安装模板 |
| `configs/robot.example.json` | 新机只读模板，动作默认关闭 |
| `configs/f1-09.json` | 原现场已核验配置的参考；不可作为另一台机器的默认验收 |
| `deploy/`、`scripts/` | 定向发布、源码打包、服务安装和运维工具 |

安装配置复制为 `configs/installation.env`，本机 profile 复制为 `configs/robot.json`，通过 `F1_PROFILE_PATH` 选用。`run.sh` 加载安装环境并把 profile 传给网关/子进程；不要重新写死 `configs/f1-09.json`。

安装根默认 `F1_INFRA_HOME=$HOME/Bruno/F1-infra`（机器人 `user` 账号对应 `/home/user/Bruno/F1-infra`）；运行数据在 `F1_CONSOLE_RUNTIME`，控制台和 Pico 必须使用同一个 `F1_CONTROL_LOCK`。`PICO_UPPER_ROOT` 默认指向本工程 `integrations/pico_upper`，`PICO_UPPER_RUNTIME` 与源码分离。具体环境字段以安装模板为准。

开发主副本在 `/home/xza_100850113/projects/F1-4242/`；ZIP 解压根名为 `F1-infra/`。两者是同一项目，不得额外生成 `F1/F1-4242/` 包装层或依赖兼容软链接。

## 开发与验证

- 先检查现有代码和测试，用 `rg` 查相关接口；不为阅读源码安装机器人整套环境。
- `bash run.sh --offline --host 127.0.0.1 --port 18442 --runtime /tmp/f1-infra-preview --control-lock /tmp/f1-infra-preview.lock` 可离线预览。此入口不加载 ROS，不创建机器人动作。
- 运行受影响测试；常规回归为 `python3 -m pytest -q tests`。可选 Pico 测试从其目录独立运行，需要专用依赖的跳过项必须明确记录。
- 前端变化检查页面、静态资源/模型加载及需要的交互。纯文档调整检查相对链接和命令，不为排版编写镜像测试。
- 涉及独立性时，在任意临时目录解压交付包并启动离线网页；验证无需父 `F1/`、固定开发用户名或原共享盘路径。
- 在目标架构构建原生监听器，核对产物 SHA；不将 x86 二进制、ROS build/install、venv 或 conda 直接搬到 ARM。
- 保留第三方署名、LICENSE 和 UPSTREAM 清单。修改已复用文件时更新本地适配清单/差异；不改写原始来源 SHA 来伪装原样复用。

## 机器人控制与数据合同

启动/导入/GET/离线预览只能读取状态，不得隐式创建控制输出、启动 Pico、录制或驱动。实际运动与遥操由现场操作者明确启动；不能用 SSH 动作作为部署验证。

换机必须核对 machine-id、运行控制器的二进制哈希与参数、监听器哈希、DDS/话题/单位/限位、关节与模型映射。`robot/runtime_contract.py` 的已审计驱动列表是独立条件，不能只改 JSON 哈希绕过审计。

保留控制码、同源请求校验、共享锁、租约、反馈新鲜度、运行合同、外部命令检测、明确确认及有界执行。按2026-09-28用户确认，`kind:pose`六秒为内部监测窗口，整项任务允许超过六秒，连续轨迹保持固定速度；总时限独立受限（详见POSE_TASK）。旧动作接口仍保留原六秒预算。缺失/陈旧值不能填零；发过命令不等于到位。软件停止不等于硬件急停。旧 LXL 不遵守共享锁，仍要检查其实际输出和进程。

新 profile 初始关闭动作并清空未经本机验证的预设。不得通过放宽头部起点容差、限位、哈希或控制合同来修复现场越界状态。用户2026-09-28明确授权的头部回程已通过原ROS合法边界目标完成一次实机恢复；仅已启用且驱动绑定的profile可按head_boundary_recovery合同继续使用，不能在启动或GET中自动恢复。首步不宣称固定速度或可撤回，正常范围内后续轨迹沿用原合同。事实见 STATUS / POSE_TASK。

原始 MCAP 只读；审核、预览、录制、recovery 写入独立 runtime。16D 的 state/action、时间戳、来源与保存事务见 RECORDING，禁止用 state 回填 action 冒充真实命令。自动上传尚未实现，不把共享盘分发当成程序上传功能。

## 发布、备份与回退

1. 明确目标 SSH 主机、用户、安装根和 profile；使用 `deploy/release.py --release <版本> --host <主机> [--ssh-config <文件>] [--target <目录>]`。`--prepare-only` 仅形成本地发布。不要从父项目寻找隐含 `f1-09` 连接配置。
2. 检查现有 `current`、服务、任务/录制/遥操和现场修改。将会受影响的源码、配置与服务单元先保存到本地 `results/deployments/<版本>/backup/` 并记录 SHA。首次部署无旧版本时据实记录。
3. 新版本放 `releases/<版本>/workspace/F1-infra/`，上传清单内文件并验证哈希，再按发布工具的显式流程设置 `current`。上传或验证失败保留旧入口。不可用删除式同步清理其他目录。
4. 服务切换前工作台自身任务应已结束。只维护本项目服务，不能停止厂商控制器、相机、旧 Pico 或其他任务来强行接管。`current` 切换不自动切换现有进程。
5. 持久服务安装见 AUTOSTART。核对 machine-id、profile、Linger、共用控制锁和服务所有权；保留原单元备份，不覆盖未知自定义服务。
6. 发布记录分开写：源码/配置 SHA、远端核验、服务结果、只读状态、现场验收、待验收。整机未重启不能写成自启已实测。
7. 保留旧 release 和运行配置备份。回退代码不恢复机器姿态，也不自动回滚新数据或合并审核记录。

## 共享源码包

从项目根执行 `python3 deploy/package_shared.py`。范围仅本项目，顶层必须是一个 `F1-infra/`，含 README、AGENTS、源码、配置模板、必要资产和 docs。

排除 `.git`、运行数据、凭据、私钥、访问/控制码、日志、缓存、venv、历史发布备份和整个父 F1 工程。实际安装配置用模板交付，本机秘密值不可进入 ZIP、清单或工具输出。

核验 ZIP 路径/符号链接边界、SHA256 清单、模型完整性及解压后离线启动。上传授权已包含替换时，将新 ZIP 和 SHA/说明一起放到指定 `具身/F1-Infra`；移除仅本项目被替换的旧交付物，保留无关共享文件。读回核验成功后再报告上传完成。
