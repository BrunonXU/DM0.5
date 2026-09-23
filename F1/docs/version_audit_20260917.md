---
created: 2026-09-17
type: version_and_preservation_audit
robot: F1_D070
status: verified_snapshot_not_automatic_sync
---

# 6000D 与 F1 实际版本核对（2026-09-17）

**结论：6000D 是开发主副本，F1 的 Bruno 任务目录确实是复制发布的客户端副本；两边不是实时镜像，也不能保证服务器“始终自动最新”。** 本次实查的两个现用 replay 发布版本，所有工作区文件都与6000D已有对应发布 payload 的 SHA256 一致。开发树已包含后续功能和训练工程，与旧部署不同；远端新产生的运行日志需要另外取回。

本次通过已有 SSH 隧道只读访问 `ms-thor/user/aarch64`，**文件盘点时间为北京时间 2026-09-17 13:08:57**，随后定向取回文件，13:13:16核对已安装控制器指纹。没有发布 ROS 命令、修改模式、停止进程、更新远端源码或切换入口。另一 agent 的现场工作不由本文接管；结论只覆盖这些采样时刻及列出的文件。

**13:21:56追加核对：另一agent已新增 `20260917-base-home-v1`。** 新增的底盘归位及其他样本诊断发布清单30个文件，逐项与6000D `results/base_home_delivery_20260917/payload/` 及manifest三方SHA一致；它依赖的17个旧基线文件也全部符合发布清单。`current`、原replay_v3/pose_v3及057候选未变化。新入口为根目录 `base_home.sh`、`pose_review.sh`、`replay_review.sh`，详见[发布说明](../foam_sort_whole_body/results/base_home_delivery_20260917/README.md)与[本次独立核验](../foam_sort_whole_body/results/migration_audit_20260917/base_home_followup_verification.json)。后文15个release和726文件是第一次快照的统计，不能冒充追加后的总数。

本次又只读取回47个新增/变动文件副本，11,516,584字节，独立保存于[增量快照](../foam_sort_whole_body/results/migration_audit_20260917/followup/remote_snapshot/manifest.json)，所有SHA通过、读取期间变化0。包含新发布与小型日志，未重复传旧的大型bag。归档2,316,194字节，SHA256为`ec1bc97e5c67008120e32f38bbc6abce388dd4393984b27c6ba1c129b97e0d0f`；[增量原始清单](../foam_sort_whole_body/results/migration_audit_20260917/followup_delta.json)保留新增51项与变化1项，未选择的5项记录于[回收报告](../foam_sort_whole_body/results/migration_audit_20260917/followup/recovery_report.json)。新归位只是当前开机局部里程计XY工具，现场归位效果未验收，不修复原回放的停车协同问题。

## 1. 当前入口实际指向什么

F1任务根目录：`/home/user/Bruno/F1/foam_sort_whole_body`。

| 入口 | 实查引用版本 | 与服务器开发树的比较 | 与服务器已有发布包的比较 |
|---|---|---|---|
| `current` 及 `configs/robot/scripts/docs/models` 等链接 | `20260914-bruno-source-v1/workspace/F1/foam_sort_whole_body` | 该release全部81个workspace文件：63相同、13不同、5个旧路径现已不存在 | 是历史静态源码副本，不能用它代表现在replay版本 |
| 根 `replay_v3.sh`、`pose_v3.sh` | `20260916-recorder-ready-v1` | release的17个workspace文件：13相同、4不同 | **17/17逐文件SHA一致** |
| 根 `replay_057_candidate.sh` | `20260916-base-retiming-057-v1` | release的16个workspace文件：13相同、1不同、2个候选专用文件不在开发主目录 | **16/16逐文件SHA一致** |

`deployment_state.json`仍记录09-14首次静态部署。不能据这个文件或 `readlink current` 宣称机器人只在运行09-14代码；也不能因为目录内有较新release就推断已使用它。两个普通根脚本与本地 `scripts/replay_v3.sh` / `pose_v3.sh` 字节相同；057候选根脚本与本地对应发布payload一致。

**本次最新已结束的现场日志走的是原840帧/28秒057路径，不是45.233秒重定时候选。** 原始程序报告：来源时间轴完成、零速反馈确认、录制exit 0且metadata存在；`onsite_task_success=not_assessed`。这证明程序完成，不替代现场任务评价、身体起姿平顺或精确距离验收。详见[经验汇总](replay_lessons.md)及[取回的原始report](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/files/runtime/replay/20260917T045324Z_325ada5e/report.json)。

随后replay agent补完[本轮诊断](../foam_sort_whole_body/results/replay_review_20260917_045324/README.md)：首次后退反馈积分约8.4cm，松爪时速度仍约0.139m/s，**停车协同未通过**；原任务还缺最后前进一段，不能把未回原地全部归为跟踪故障。这是基于反馈快照的新分析，未改原始report，也不是本次独立读取大型db3得出的全率/外部测距结果。

证据：[远端原始盘点](../foam_sort_whole_body/results/migration_audit_20260917/remote_inventory.json)、[逐文件比较](../foam_sort_whole_body/results/migration_audit_20260917/comparison.json)。统计使用当时开发树，不把后续其他agent的修改倒推成当时状态。

## 2. 不一致分别是什么，不要盲目覆盖

普通replay release与开发树的4项差异为 `configs/replay_f1.json`、`robot/f1_replay/cli.py`、`ros_backend.py`、`runtime.py`。开发树增加了重定时、底盘参数绑定和诊断相关代码；远端这些旧字节全部已在原release的本地payload保存，本次又取回一次，**没有在这17项里发现丢失于服务器的现场源码修改**。

057候选的 `replay_f1.json` 是专门的底盘参数/诊断profile；`configs/retiming_057.json` 和 `scripts/replay_057_candidate.sh` 原本就保存在本地 `results/deployments/20260916-base-retiming-057-v1/payload/`，不是服务器遗失。它们没有放进通用开发配置/脚本目录，不能用主目录同名文件缺失判断“远端比服务器多了未保存修改”。该候选其余13项全部与开发树一致。

具体差异另存[diffs目录](../foam_sort_whole_body/results/migration_audit_20260917/diffs/)。保留旧profile、原时间轴与诊断候选各自用途，不把候选profile回写通用配置，不在另一个agent工作时统一切 `current`。**新开发代码、已部署代码、已验收代码是三种版本状态。** “最新版”只有指定用途和入口后才有准确含义。

## 3. 这次已保全什么

在6000D独立目录 `F1/foam_sort_whole_body/results/migration_audit_20260917/` 保存：

- 全部15个远端任务release、根入口/元数据等**435个部署文件**，以及选定的**291个runtime文件**，合计726个文件、72,391,445字节。
- runtime范围包括运行包/配置、姿态基线与小型诊断、最新replay的events/report/metadata、最近一次起姿日志，以及被profile引用的命令监听 `.so`。保留原远端相对路径；ARM库仅作目标架构部署证据。
- 压缩归档 `remote_artifacts.tar.gz` 为14,075,304字节，SHA256：`87b1075daa116b7c0f7e651297df3ce9e8ecb271ec7abe58d33d7638f2a0e019`。解包到 `remote_snapshot/files/`；每个文件都与远端读取时的SHA一致。
- 726个选定文件均未在读取期间变化，也未在盘点到读取之间变化；错误0。详细范围见[回收报告](../foam_sort_whole_body/results/migration_audit_20260917/recovery_report.json)和[带来源哈希清单](../foam_sort_whole_body/results/migration_audit_20260917/remote_snapshot/manifest.json)。

这是**定点项目保全，不是完整runtime或整机备份**。未取回项已逐一保留路径、大小及mtime，尤其：

| 未覆盖内容 | 具体边界 |
|---|---|
| 大型rosbag数据库 | 最新运行 `runtime/replay/20260917T045324Z_325ada5e/rosbag/rosbag_0.db3` 401,645,568字节，本次没有重复传输；metadata已取回。其他db3/mcap也未纳入本次归档 |
| 大型历史事件/运动学测试输出、构建树、ROS日志 | 按回收脚本的选取规则跳过，不能因有manifest就声称内容全部备份 |
| 13:08:57之后新增/变更的运行产物 | 需要现场工作结束后补一次封口回收；本次清单不会自动追更 |
| 底层ROS/控制器/相机SDK完整安装 | 本次复核指纹，不复制整个系统或其他用户工作区；已有reference也不足以重装裸机 |
| 私钥、完整home、系统镜像 | 不在项目归档范围，连接身份独立交接 |

最新db3若已被replay agent完整取回，以其原路径和SHA记录为准，避免重复大传输。若未取回，搬离前应在录制结束后补取；不要把正在写的数据库当作完整备份。

## 4. 底层版本有没有变

13:13:16只读计算已安装文件SHA，与09-16已有证据对照：

| 文件 | 本次SHA256 | 与先前核定版本 |
|---|---|---|
| `motor_ctl_node` | `898ef58fb7118ecbf416af134e01700ed38ffdd3480293cf06fe95da14bce0c8` | 相同 |
| `kinematics_node` | `c53411708ea0a7f94c3df54d5e333de4ccd79f610ad81a099bca4adb2af79fc6` | 相同 |
| `tj_sdk/lib/libKine.so` | `7f03a33964123d2b37d249a51cbdb748c54f5445021ac10e128b27f349dc4d83` | 相同 |
| `/etc/ros/cyclonedds/cyclonedds.xml` | `a1abd1e03f2b4a5a153f9c16aca5bd609095e57ae4e24be55a5d3d0eedac37cb` | 本次记录指纹 |

原始输出：[system_fingerprints.txt](../foam_sort_whole_body/results/migration_audit_20260917/system_fingerprints.txt)。这是已安装文件比较，本次没有重新遍历全部运行进程的exe和已加载库，也没有重新读取全部ROS参数。**没有查询厂商最新固件/SDK发行版，不能称为厂家最新整机系统。** 底盘控制器身份、底层构建/安装恢复源仍按[系统配置](../F1系统配置.md)保留未知。

## 5. 服务器主副本的实际风险与后续操作

本次 `git ls-files F1` 返回0：**F1目前整体是未跟踪目录。** 工作区还存在其他项目大量已有修改，本次没有提交或整理它们。现有Git commit、远端仓库、一次 `git clone` 都不足以代表/恢复本地F1成果。任务结果约占整个F1目录的大部分；本机 `du` 显示F1约14GiB，原始数据盘约596GiB（十进制约639GB量级），不能按几份脚本估计完整迁移。

为保存当前真实字节，本次另制作[最终服务器源码快照](../foam_sort_whole_body/results/migration_audit_20260917/server_source_final/server_source_snapshot.tar.gz)，范围与逐文件SHA见[源码manifest](../foam_sort_whole_body/results/migration_audit_20260917/server_source_final/server_source_manifest.json)：包含F1非results源码/文档/配置/参考文本、连接非秘密配置、工作区规则、共享DM0.5源码及选定构建说明。第一次源码快照后另一agent更新了两份任务文档，故保留原快照并另存本份增补归位/新诊断后的版本。它不包含全部results、原始数据、模型权重、容器镜像、系统安装或私钥；应与前面的远端部署归档及迁移清单配套使用。这是一个明确时刻的本地副本，并非异机容灾或自动追更机制。

维护“服务器始终有最新内容”的实际做法是：开发以6000D为源；现场修改先取回另存并审核；每次发布保存实际文件与SHA；每轮replay结束收回完整日志；搬走前做最后一次差异核对。没有自动双向同步工具替我们保证这些条件。

本次完成审阅、实际版本核验和定点回收，**未把开发树强行覆盖到机器人**。用户现在另有agent负责replay，入口升级要在其现场运行结束、版本明确后按[迁移手册](migration_runbook.md)和[既有发布约定](../AGENTS.md)处理。无需为了“新”而改写已有验收基线或丢失旧版；服务器开发/发布副本都应保留。
