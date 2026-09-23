---
created: 2026-09-17
updated: 2026-09-17
type: migration_runbook
robot: F1_D070
scope: project_handoff_not_full_system_backup
---

# F1 项目迁移与交接手册

**6000D 是项目开发主副本，F1 域控保存按版本复制的任务部署；这是一项工作约定，不是自动双向同步保证。** F1 现场日志、姿态备份、临时修补以及系统已安装依赖可能只有域控上有。搬走前必须把这些差异和运行证据收回来。当天“两边实际是什么版本、哪些内容只在一边”的结论见[2026-09-17版本审计](version_audit_20260917.md)，不要用本页代替实时审计。

本页依据[机器人工作约定](../AGENTS.md)、[系统配置](../F1系统配置.md)、[接入记录](connect/README.md)、[任务当前方案](../foam_sort_whole_body/docs/training_plan.md)和实际部署源码整理。整理过程中没有执行部署、切换入口或机器人动作。当前 replay 由现场操作者及负责该任务的 agent 继续管理；交接采集不占用控制权、不终止他们的进程。

## 1. 搬走前先保全什么

优先保存“机器人离开后不能重新取得”的内容，再准备可从原始数据重建的派生缓存。以下是交接清单，完成状态应逐项附清单、哈希和备份位置；文档存在不表示备份已完成。

| 优先级 | 必须交接的内容 | 保存与验收方法 |
|---|---|---|
| P0 | F1 实际入口及引用链：`current`、`deployment_state.json`、根目录 `replay_v3.sh` / `pose_v3.sh` / 其他候选入口、引用的 release 和 profile | 记录原路径、软链接字面值及解析目标，保存实际文件、权限和 SHA256；逐入口展开依赖，不能只记录 release 名称 |
| P0 | 已结束的 `runtime/replay/`、`pose_runs/`、`pose_backups/` 及其他故障诊断 | 取回 events、commands、report、profile/config 快照、rosbag metadata/db3/相关旁文件；确认写入者已退出再做完整性与哈希检查。正在写的目录只记录“未封口”，结束后补取，不将半份数据库标为完整备份 |
| P0 | 域控现场改动、最新运行包、姿态基线 | 与6000D对应发布包逐文件比较；域控独有文件原样另存并说明来源，不直接覆盖本地主副本；`restore`实际绑定的姿态和SHA必须一起保存 |
| P0 | 底层已安装软件及机器专属配置的可恢复来源 | 定点取得运行二进制指纹、launch、消息/服务定义、标定、DDS、网络、相机身份和运动学配置；向系统维护方确认安装包/构建来源及恢复方式。没有源码或可恢复安装包时，明确列为整机重建缺口 |
| P1 | 6000D任务代码、共享模型代码、发布包、备份和清洗结果 | 保存实际文件及未提交/未跟踪改动，保留版本间差异、每版 manifest 和验证记录；Git commit 不能替代未提交文件内容 |
| P1 | 外部数据盘、基础模型、processor、容器构建信息 | 分开核对位置、大小、完整性与恢复方式；保留镜像 ID/digest 和构建文件，镜像 tag 本身不足以复现 |
| P1 | 新机器身份、账号、网络与连接交接 | 保存非秘密配置和已核对主机公钥；新设备独立创建私钥及最小权限授权，不把旧机器人私钥塞进工程备份 |
| P2 | 可重建的预览、视频、临时缓存 | 先保留生成命令、输入清单和版本；按交接空间再保留大型缓存。不得以“可重建”为由删除原有产物 |

每条交接记录至少包含：源主机/路径、采集时间与时区、目标路径、大小、SHA256、所属版本、是否仍在写入、是否含未提交修改、验证结果。原始数据和日志保持原样；交接副本与解释性报告分开。

## 2. 目录和责任：哪些是主副本，哪些是部署副本

```text
6000D /home/xza_100850113/projects/
├── PROJECT_RULES_AND_PREFERENCES.md
├── F1/
│   ├── AGENTS.md、F1系统配置.md、两份接口文档
│   ├── docs/                         # 机器人级流程、接入、迁移经验
│   ├── connect/                      # 隧道非秘密配置；没有私钥
│   ├── reference/<版本>/             # 原样证据及来源/SHA
│   └── foam_sort_whole_body/
│       ├── configs/ scripts/ robot/   # 合同、离线工具、机器人客户端
│       ├── models/DM0.5/             # F1任务reader、训练器和recipe
│       ├── docs/                     # 任务方案、操作、事故复盘
│       ├── deploy/                   # 静态打包工具与清单模板
│       └── results/                  # 清洗产物、实测证据、部署包及备份
└── DM0.5/opendm/                     # 任务目录外的共享模型实现

F1 /home/user/Bruno/F1/foam_sort_whole_body/
├── releases/<release_id>/workspace/F1/foam_sort_whole_body/
├── current -> releases/<id>/workspace/F1/foam_sort_whole_body/
├── replay_v3.sh、pose_v3.sh、候选入口 # 可能直接绑定另一个release
└── runtime/
    ├── replay_bundles/ replay_sets/  # 运行数据、机器profile
    ├── replay/                      # 每次执行/录制证据
    ├── pose_backups/ pose_runs/      # 基线与动作前姿态、计划和日志
    └── replay_incoming/             # 定向部署暂存、旧入口备份等

F1 /home/user/Bruno/connect/           # 隧道配置、现场私钥、日志
```

底层 ROS/驱动仍在原有 `/opt/ros/jazzy`、`/home/user/workspace/...` 等工作区。本项目没有接管它们；这些系统安装也不是从6000D整个 `F1/` 文件夹复制生成的。

**`current` 与“当前实际执行版本”不是同义词。** 已检查的 `scripts/replay_v3.sh` 和 `scripts/pose_v3.sh` 直接写明 `20260916-recorder-ready-v1`；057重定时候选的独立入口绑定 `20260916-base-retiming-057-v1`。这些只是本地文件所记录的版本，现场当前值看版本审计。09-16的定向安装脚本保存旧入口后替换根入口，并未以修改 `current` 统一选择所有执行版本。因此：

- 查看 IDE 中 `current/` 的代码，不足以确认根入口实际加载的代码。
- 修改 `current` 不会改变已有进程，也可能完全不影响写死 release 路径的入口。
- 回退必须一起还原实际入口、profile、数据包及姿态基线的对应关系；只切软链接可能没有作用。
- 后续发布应把“入口 → release → profile → bundle/retiming → pose baseline”作为一组绑定关系记录。参考[录制修复部署](../foam_sort_whole_body/results/deployments/20260916-recorder-ready-v1/README.md)及其 [install.py](../foam_sort_whole_body/results/deployments/20260916-recorder-ready-v1/install.py)。

现场改动先原样回收、保存差异，再纳入6000D新版本。每次发布只传选定文件并核验，不用删除式同步把域控的日志、密钥或其他人的工作区清掉。

本次[版本审计](version_audit_20260917.md)还发现F1文件尚未进入Git跟踪，不能通过现有commit或一次 `git clone` 恢复它。交接必须保全当前实际目录与清单，包括未跟踪文件；本页不擅自提交这些内容。任务 `results/` 已包含大型产物，整个F1目录也不能按“小型源码包”估算传输范围和耗时。

## 3. 只复制 F1 文件夹还缺哪些内容

| 依赖 | 当前位置或证据入口 | 迁移要点 |
|---|---|---|
| 原始801条录制 | `/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw/` | 任务README记录约639GB；本页未重新统计容量。位于外部数据盘，不在代码发布包内；同级 `transfer/` 保留传输与断点证据，FTP已按用户要求停止，不自动重启 |
| 清洗、mask、sidecar、FK、图像cache与统计 | 任务 `results/`，当前入口见[当前方案](../foam_sort_whole_body/docs/training_plan.md) | `.npz`、原图、视频、较大的JSON/JSONL等不会完整进入静态源码包；迁移要按使用清单保留，不能只拷 `.py/.md` |
| DM0.5共享实现 | `/home/xza_100850113/projects/DM0.5/opendm/` | F1任务代码通过 `PYTHONPATH` 引用；保留共享源码版本及实际修改，不拿G2的动作统计/adapter替代F1合同 |
| 基础模型 | `/mnt/dm05_nvme/dm05/model_cache/Dexmal/DM05` | 09-17完整模型检查实际使用的本地模型目录；需要单独保全并绑定指纹。模型目录不在 `F1/` 内 |
| 共享processor复核来源 | `/mnt/dm05_nvme/dm05/checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000` | CPU工程检查借用该目录的通用processor文件；不是借用G2模型adapter或归一化。完整模型检查使用DM0.5 base自己的processor |
| 训练环境 | `opendm:g2-34d-fa2`，复现命令见[09-17脚本](../foam_sort_whole_body/results/dm05_integration_20260917/reproduce_checks.sh) | 记录现用镜像ID、架构、依赖及挂载；`DM0.5/opendm/Dockerfile` 与 `DM0.5/infra/docker/` 是构建参考，未证明重建后与现用镜像逐项一致 |
| 审计/数值临时环境 | `/tmp/f1-foam-audit-venv`、`/tmp/ptd-eval-venv` | `/tmp`可能被清理；用依赖与检查记录重建。基础审计依赖见[requirements-audit.txt](../foam_sort_whole_body/requirements-audit.txt)，不覆盖所有reader、IK和模型依赖 |
| 运动学配置与冻结代码 | [IK/FK recipe](../foam_sort_whole_body/docs/training/ik_fk_demo_recipe.md)、[原生SDK证据](../reference/20260916_kinematics/README.md) | 保留 `ccs_m6_40.MvKDCfg`、合同、校验值和冻结算法；`libKine.so`仅有指纹不等于备份了可恢复的SDK安装 |

**绝对路径是当前迁移的一项实际限制。** [机器配置](../foam_sort_whole_body/configs/machine.6000d.yaml)列了路径，但它们仍是部署元数据，现有训练器/客户端没有统一从这些YAML自动读取。工程训练清单使用6000D绝对路径，容器按相同路径挂载；部分数据脚本还有硬编码原始数据路径。最快复现可保持原目录与挂载；更换根目录时，明确修改生成新清单和受影响路径，并重新验证引用，不批量改写历史证据或旧版 manifest。

可在**新服务器终端、工作目录 `/home/xza_100850113/projects`**先只读定位路径引用；输出在终端，不启动任务：

```bash
rg -n '/home/xza_100850113/projects|/home/user/Bruno|/mnt/dm05_nvme|/tmp/.*venv' \
  F1/foam_sort_whole_body/configs \
  F1/foam_sort_whole_body/scripts \
  F1/foam_sort_whole_body/models \
  F1/foam_sort_whole_body/results/dm05_integration_20260917/reproduce_checks.sh
```

正式策略目前为 state19 / EEF23 / pad32，来源数据和关节replay仍有 state21 / joint-action19；维度19重名不代表语义相同。迁移时将模型、动作合同、归一化、FK映射、数据范围与split一起绑定。当前正式数据、统计、模型服务与客户端的完成度只看任务当前方案；不能把模型目录被复制或单卡前反向工程检查当成正式模型部署完成。

## 4. 不同架构与底层安装：换机前必须明确

| 系统 | 已核对环境 | 对迁移的影响 |
|---|---|---|
| 6000D | x86_64、Ubuntu22.04、系统Python3.10 | 负责开发、清洗、训练与计划中的模型服务 |
| F1 D070 | aarch64、Ubuntu24.04、系统Python3.12、ROS2 Jazzy | 复用现有ROS/SDK，负责现场replay及后续请求客户端 |

来源及核对日期见[系统配置](../F1系统配置.md)。**不能跨机直接拷venv、conda目录、ROS编译产物、驱动或SDK二进制并当作已验证环境。** 新机器按目标架构准备最小依赖，再检查导入与行为；镜像也要核对架构、GPU驱动兼容性和运行挂载。GPU/JetPack/CUDA/TensorRT完整清单仍没有在系统配置中核定，不从主机名或 `tegra` 内核后缀推断。

当前 `/home/user/workspace/f1` 只看到 `build/install/log`，没有 `src`。若这台域控也一起离开，仅打包任务源码不能重建以下系统能力：

- `motor_ctl`、`kinematics_node`、`f1_msgs`及相应头文件、launch和实际已装SDK。
- `move.msg.State`来自 `/home/user/workspace/agv/install/setup.bash`；只source Jazzy和f1工作区会缺类型支持。
- 双腕RealSense、头部Orbbec的实际驱动、设备序列号、launch与标定；源码候选存在不代表当前运行版本。
- 身体/升降控制入口、自动启动关系、底盘网络与当前控制参数。

保全应以定点文件和版本清单为先，不去复制整个其他用户home或全部 `build/install`。如果目标是“重装一台裸机即可还原整机”，必须额外取得底层可恢复安装包/源码、设备标定与系统恢复方案；本项目已有 `reference/` 是证据快照，不是完整系统备份。

机器相关设置尤其不能原样盲搬：CycloneDDS目前绑定 `enP3p1s0f1`；09-17恢复记录的底盘静态地址为 `192.168.127.170/24`，无网关。新机器接口名、拓扑或地址不同应核对后独立适配，保留旧配置。实际控制/反馈用CycloneDDS，motor_ctl参数探测另用Fast DDS子进程；单一DDS“能看到话题”不能代表全部链路可用。依据见[系统配置](../F1系统配置.md)和[09-17恢复记录](../foam_sort_whole_body/results/live_feedback_20260917/repair/README.md)。

## 5. 静态同步工具真正做了什么

[deploy/sync_project.py](../foam_sort_whole_body/deploy/sync_project.py)是有限范围源码快照工具，当前支持如下动作：

| 子命令 | 实际能力与限制 |
|---|---|
| `prepare` | 只读取6000D本地选定文件，冻结真实字节、权限、SHA及Git信息；不连接F1，不自动收回现场修改，也不启动训练或控制 |
| `check-package` | 验证本地包与manifest；加 `--compare-source` 比较当前所选文件是否仍与冻结快照一致 |
| `deploy` | 只对**不存在的全新目标目录**创建release/runtime/current；同版本重跑只核验；遇到其他已部署版本或不符合托管约定的现场状态会拒绝，不提供自动升级 |
| `verify` | 核验远端release文件集合、大小、模式、SHA及入口元数据；保存校验结果，不安装依赖、不证明运行或动作验收通过 |

默认选取任务目录、`F1/reference/`、`F1/docs/`及若干固定根文档；只收允许扩展名的文本，每文件不超过2MiB、总选取不超过20MiB。`results/`内还有限制：主要保留Markdown、`summary.json`、`validation.json`；`deployments`、原始数据、权重、venv、图像视频等跳过。**F1/connect不在工具的选取根目录中**，需要单独保全非秘密配置。`prepare`也不会自动读取远端runtime；不能把静态包称为任务全部日志/数据的备份。

工具对目标路径固定为 `/home/user/Bruno/F1/foam_sort_whole_body`，并核对远端 `ms-thor/user/aarch64` 身份及 `f1-d070` 主机密钥别名。新机器人更换身份后，需要经审查调整身份/目标约束，不能通过关闭校验来勉强使用。`release_manifest.example.yaml`只是填写模板，不是部署程序；其中state21/action19示例也不能代替当前模型合同。

以下是**6000D终端、工作目录 `/home/xza_100850113/projects`**的本地准备例子，输出在新的 `results/deployments/<ID>/`，不连接F1。已有同名目录时换ID，不覆盖旧包：

```bash
python3 F1/foam_sort_whole_body/deploy/sync_project.py prepare \
  --workspace /home/xza_100850113/projects \
  --release-id migration-source-20260917-v1
python3 F1/foam_sort_whole_body/deploy/sync_project.py check-package \
  --package-dir F1/foam_sort_whole_body/results/deployments/migration-source-20260917-v1 \
  --compare-source
```

这里的命令仅作手册示例，本次文档整理没有执行。源码还在被其他agent修改时，`--compare-source`发现差异是正常且有用的结果；等目标版本明确后重新冻结，不能把进行中的源码变更覆盖进旧包。

已有F1目标的升级须先完成实际备份和引用链核验，再定向放入新release；现有各次 `results/deployments/<ID>/install.py` 为固定一次发布编写，不能直接拿旧ID脚本给新版本通用升级。

## 6. SSH连接和密钥单独交接

现用路径是 F1 的systemd用户服务主动连接6000D SSH22，6000D再经 `127.0.0.1:22022` 进入F1；不是个人电脑或Tailscale转发。完整诊断命令及权限见[连接手册](connect/README.md)。

有两套不同私钥：F1 `Bruno/connect/keys/id_ed25519_6000d_tunnel`负责建隧道；6000D `~/.ssh/id_ed25519_f1`负责登录机器人。项目只保留非秘密配置、主机公钥与证据。新设备在本机新建身份、独立核对双方主机公钥、按需要注册受限授权；旧机器撤场时逐条撤销旧身份，保留其他人的授权。不要将旧 `authorized_keys` 备份整份覆盖回来。

同时保全 `ssh_config`、`known_hosts`、systemd服务文件、用户服务链接的目标、`Linger`和enabled状态；只有复制服务文件不会自动注册和启用服务。新网络必须仍能到6000D，若服务器也换了地址，要更新经核定的目标及主机身份；自动重连不能解决无路由或防火墙阻断。持续服务启用时，不另起同端口手动 `ssh -R`。

09-16连接记录保留了当时未测重启的边界；09-17[现场恢复证据](../foam_sort_whole_body/results/live_feedback_20260917/repair/README.md)已记录一次重启后隧道自动恢复。该证据不等于所有网络下的长期稳定性、传输吞吐或模型服务延迟均已验收。隧道只有SSH入口，未来模型服务端口和超时需要另行配置验证。

## 7. 新环境最短恢复顺序

1. **恢复开发基线。** 放回F1任务、工作区规则、共享DM0.5代码；核验实际源码和发布包哈希，保留当前方案及未提交差异。原始数据盘、模型目录及所需结果按清单挂载；先只读检查清单路径与来源，不重新提取已完成的全量MCAP。
2. **重建最小环境。** 在目标架构准备审计/IK或模型环境；记录解释器、镜像与依赖。先做reader/合同检查和已验证的工程检查，当前正式发布状态不因迁移自动改变。
3. **恢复机器人依赖与连接。** 核定机器身份、消息类型、ROS工作区、DDS、底盘网络及相机绑定；独立建立连接密钥和服务。系统底层变更需要具体备份和验证，不与任务源码传输混为一项。
4. **放置完整任务部署。** 在Bruno约定目录恢复所选release、入口、profile、运行包与姿态基线；校验所有引用，保留旧已验证版。单拷 `current` 或源码目录不算完成。
5. **执行不发控制指令的验收。** 静态文件核验 → 导入/类型与依赖检查 → `inspect`/`dry-run` → 短时实际反馈 → 执行前 `preflight`。这些操作可能写自己的日志，但不得启动控制发布者。多agent并行时避免重复启动高负载录制或修改共享配置。
6. **现场验收。** 由现场操作者在明确控制权、起始姿态、地面场景与中断方式后执行已批准的动作入口，保存本次版本、输入、事件及反馈。发生异常就按日志复盘，不以放宽门槛代替恢复。

现场不发控制指令的入口示例，**F1的user终端、工作目录 `/home/user/Bruno/F1/foam_sort_whole_body`**。结果打印到终端；`dry-run`另在 `runtime/replay/<新运行ID>/`写 `commands.jsonl`和`report.json`，ROS日志在 `runtime/replay/ros_logs/`。这些命令应在已恢复并核验入口后使用：

```bash
./replay_v3.sh check-env 057
./replay_v3.sh inspect 057
./replay_v3.sh dry-run 057
./replay_v3.sh preflight 057
```

不能通过SSH远程启动 `execute`、`to-start`、`restore` 或切换硬/软控制模式。09-16至09-17存在身体起姿震动与硬自动模式变化的现场记录；迁移手册不将这些问题标为已修复，也不因“到位”“preflight通过”就推定实际运动平稳。最新允许执行范围由[任务当前方案](../foam_sort_whole_body/docs/training_plan.md)与现场操作者掌握。

## 8. 回退时必须回退整条引用链

1. 现场操作者结束新任务并确认静止；不要终止未知控制器、其他agent的replay或系统业务服务。代码入口替换不会结束已运行进程。
2. 留下失败版本及现场日志，再核验6000D部署前备份的文件和SHA。首次部署没有旧版时，记录“无旧部署”，不能声称能切回一个不存在的旧版本。
3. 恢复实际启动入口、所引用release、profile、bundle/retiming和绑定姿态；若该入口经 `current` 选择版本，再恢复其原字面目标。不要只改current而留下仍指向新版的根脚本，也不覆盖与回退无关的日志或配置。
4. 重新做文件/导入检查、离线检查和实时preflight，再由现场操作者按旧已验证方式启动。旧代码不自动恢复机器人姿态、地面位置、物料、模式或未结束的系统状态。
5. 连接服务回退见[连接手册](connect/README.md#停用与回退)：会中断隧道，应该从现场终端完成。保留私钥；永久撤销仅移除对应授权。系统网络/驱动的回退需要它们自己的具体备份，任务release不能恢复整台域控。

`pose_v3.sh restore`当前代码绑定一个特定09-16基线；09-17新保存的 `before_move_pose.json`不会自动成为该命令的新目标。交接时应把“最近一次快照”与“当前restore绑定基线”分别记录。换机器人后的旧关节姿态也必须重做接口、几何和现场核验，不能自动沿用。

## 9. 交接完成的判据与现有缺口

交接完成至少要能回答：6000D保存了哪份实际源码；F1各入口实际绑定哪组文件；哪些现场独有内容已经取回；外部数据/模型从哪里恢复；新环境如何准备依赖；如何以文件证据回退；尚不能还原哪些底层能力。最新一致性结论统一引用[版本审计](version_audit_20260917.md)。

本页本地审计已确认的限制：没有自动双向同步；静态工具不负责既有版本升级和完整数据备份；机器YAML没有统一驱动路径配置；底层f1工作区缺src；域控SDK完整恢复源未核定；运行日志持续增长，封口后还需补取；正式模型服务与客户端未完成。它们应保留在迁移验收中，不能仅凭“服务器上有F1文件夹”宣布可在另一台机器人上完整复现。
