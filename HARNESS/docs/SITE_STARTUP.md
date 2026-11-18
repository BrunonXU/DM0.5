# F1 Harness 现场装配与启动

**当前人类入口为 [F1infra任务页](http://192.0.2.7:4242/#tasks)，默认执行一次网页“开始”完成单块 `development_coarse` 任务。** 固定 `baseline` 不消耗模型 API，使用已接受的粗标定，按用户决定跳过单独 shadow。页面操作、执行服务接线及部署状态统一见[完整真机运行](DIRECT_ROLLOUT.md)。

本页供维护人员装配和排障，保留包、站点、服务与底层命令的技术说明。开发包固定 Harness、RPent、原 F1 owner 与共享源码；粗标定明确 `precision_certified=false`，参数分别记录实测／估计／配置来源。原控制锁、碰撞、反馈和取消保持保护不变；Harness 页面路线不依赖手柄或 Enter。

**配置文件或按钮存在不表示执行服务已部署、机械已动作或任务已成功。** 当前事实看[STATUS](../agent/STATUS.md)。用户日常运行按页面入口操作；下文维护命令不要求用户另开一套多终端流程。

```mermaid
flowchart LR
    A[粗标定与参数来源] --> B[站点与源码身份检查]
    B --> C[F1infra执行后端]
    C --> D[辅助服务与原 owner]
    D --> E[owner 接受当次网页授权]
    E --> F[完整单块任务与有界恢复]
    F --> G[日志与结果封存]
```

## 1. 在6000D生成配置草稿

工作目录为 `/home/user/projects/HARNESS`。输出目录必须不存在，已有文件不覆盖：

```bash
cd /home/user/projects/HARNESS
.venv/bin/python -m f1_harness.site init \
  artifacts/site-draft-20261105 \
  --runtime-root /home/user/Bruno/HARNESS/runtime
.venv/bin/python -m f1_harness.cli readiness artifacts/site-draft-20261105
```

草稿统一 owner／plan／verify／numerics 的 Unix socket，并复制已确认的相机 topic。相机时间来源仍未认证，深度比例仍是只读模板值；它们不能因为复制成功自动变成测量结论。预期 readiness 返回未就绪。

| 文件 | 填入或绑定的内容 |
|---|---|
| `owner.local.json` | `admission_profile=development_coarse`、开发档案路径、反馈／停稳参数；正式精密bundle与未完成集成标志不伪造 |
| `task.local.json` | `goal=placed`、完整技能、本次物块／孔口／粗标定身份、两次局部修正与一次重抓 |
| `plan.local.json` | 与 task 完全一致的任务、坐标链、抓取偏移、净空、夹爪、场景障碍 |
| `verify.local.json` | 同一 task，技能结果来自新观测 |
| `numerics.local.json` | 当前验证过的模型、映射、IK 与保护 spec |
| `gateway.local.json` | RGB-D／时间合同、场景与坐标链、mask 服务地址、各 socket |
| `masks.local.json` | 检测后端、绝对权重路径或已验证轻量检测参数、实际提示区域 |
| `launch.local.json` | 该候选的控制配置、infra、profile、runtime、共同控制锁和兼容端口字段；harness-web 不创建手柄服务 |
| `development.local.json` | 用户接受粗标定与跳过shadow的记录；任务、九份站点文件、当前源码、模型及既有IK复用身份；参数来源 |

`launch.local.json` 的 `controller_config` 必须指向独立候选配置；硬件模式使用的 `accepted_release_manifest_sha256` 必须绑定本包 `control/F1_RELEASE_MANIFEST.json`。既有生产配置的接受身份不能直接借给新源码。控制锁使用 F1 原有共同锁路径；不创建第二把锁来绕开占用。所有路径使用所在机器的绝对路径。

当前完整开发档案不要求正式九报告或“已认证绝对误差小于孔隙”。`calibration_bundle=null`、`integration_validated=false` 与 `precision_certified=false` 真实保留；开发授权来自显式、身份绑定的档案，运动由绑定当次 task／run／epoch 的浏览器 POST /start 明确授权。绝对误差未知可为null，规划余量和相对持物阈值另行配置；所有实际使用的插入、夹爪、对象及孔口参数必须有来源。

旧 `approach_grasp` 空载／20mm档案只保留兼容，当前默认任务为完整 `placed`。`precision` 与 shadow 的独立检查能力仍供后续认证或排障使用，不作为本轮页面运行的默认前置。

## 2. 固定开发包与安装

在6000D打包，复用已下载的 ARM Python3.12 wheel：

```bash
cd /home/user/projects/HARNESS
.venv/bin/python scripts/package_native.py \
  --wheels artifacts/native-baseline-20261104/wheels \
  --output artifacts/native-candidate-20261105
```

输出包含 `bundle.tar.gz`、内容身份和控制 manifest 身份。打包复用 DAgger 现有源码收集器，固定 `f1dagger`、`dagger_core`、控制配置与音频；不包含 API 密钥、站点私密配置、模型权重、训练数据或虚拟环境。未分配产品 V 号的包仍称开发候选。

将包及 `scripts/install_native_bundle.py` 定向传入 F1 后，在F1本地终端安装到新的内容身份目录。以下变量应填写实际传输路径，安装器不激活生产服务：

```bash
source /opt/ros/jazzy/setup.bash
harness_native_python=/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python
"$harness_native_python" /实际传输目录/install_native_bundle.py \
  /实际传输目录/bundle.tar.gz \
  /home/user/Bruno/HARNESS/development/实际内容身份 \
  --python "$harness_native_python"
```

安装器先核验成员、内容哈希与完整清单，再解包到新目录。依赖通过离线 wheel 安装在包内 `deps`，原生虚拟环境不被修改。每个 native 进程启动还会核验源码／配置哈希；安装后修改源码应重新打包。

### 原生静态配置准备：维护参考

将生成的 site 草稿放到 F1 后，以下命令从包内控制配置和现有 infra／profile 生成独立 `controller.local.json`、数值服务 spec 及 launch 路径。这是纯模型构造，使用 profile 的静态 preset；不订阅 ROS，不创建 owner／输出器，不发动作。先设置本节实际包路径、site 路径，再在F1终端执行：

```bash
"$harness_native_python" "$harness_bundle/native/entry.py" \
  --run /home/user/Bruno/HARNESS/runs/prepare-$(date +%Y%m%dT%H%M%S) \
  prepare-native "$harness_site" \
  --infra-root /home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20/startup-read-v2/infra \
  --profile /home/user/Bruno/F1/F1-4242/runtime/robot-v22.json \
  --control-lock /home/user/Bruno/F1/runtime/control.lock --owner-port 7906
```

命令删除复制配置里的旧 release 接受身份，并将候选 `ik.verified` 保持 false；不自动授权硬件模式。数值终止条件沿用此前纯数值探针的0.0005米／0.005弧度／最多3000步，**这是求解阈值，不是实物精度声明**。receipt 保存来源哈希、模型映射与URDF身份、静态输入和零动作边界。

后续完整开发站点核对既有生产IK设置、模型与来源回执，才沿用已有IK验证并绑定本候选的实际控制manifest。这个继承只证明同一控制配置的来源，不证明新相机精度、Harness shadow或物理任务通过；不能手改true代替回执。

已有 `controller.local.json` 或非空 `numerics.spec` 时拒绝重写；已填 launch 字段与新参数冲突时也拒绝。标定、场景、时间合同和 owner 集成准入字段完全保持原状。后续源身份或参数变化应生成新候选及新准备记录。

## 3. 页面入口与服务职责

页面执行后端负责衔接本地感知、独立控制／图像通道、原生数值／计划／验证／网关及原owner。用户点击“开始”是完整任务的显式授权；后端将当次 run、task、session、owner 身份和 epoch 送入 owner 的有界消息队列，不创建或伪造 F710 输入；停止请求必须由owner完成撤销与保持，网页返回不冒充实测停稳。具体可用能力与接线状态见[页面操作说明](DIRECT_ROLLOUT.md)。

<details>
<summary>维护附录：手动检查已有组件，不作为用户日常启动流程</summary>

以下是完成配置后的操作合同；当前未测字段仍会拒绝。`harness_bundle` 与 `harness_site` 分别指向实际安装目录和已经审阅的现场配置目录，不能把示例路径当作已部署结果。

**原生辅助服务组。** 用户结束自己的原控制会话后准备新候选；`services` 只启动数值、计划、验证、相机网关，不启动 owner：

```bash
source /opt/ros/jazzy/setup.bash
harness_native_python=/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python
harness_bundle=/home/user/Bruno/HARNESS/development/实际内容身份
harness_site=/home/user/Bruno/HARNESS/site/已审阅目录
harness_group_run=/home/user/Bruno/HARNESS/runs/services-$(date +%Y%m%dT%H%M%S)
"$harness_native_python" "$harness_bundle/native/entry.py" \
  --run "$harness_group_run" services "$harness_site" --mode hardware
```

**F1 另一个现场终端：独立 owner。** 重复设置本终端的三个变量后执行：

```bash
"$harness_native_python" "$harness_bundle/native/entry.py" \
  --run /home/user/Bruno/HARNESS/runs/owner-$(date +%Y%m%dT%H%M%S) \
  owner "$harness_site" --mode hardware --web-control
```

以上为已绑定完整开发站点的维护命令。`--web-control` 只选择网页授权路线，不授予运动权限；只有可信执行后端处理真实浏览器 POST /start 后，才能向 owner Unix socket 提交绑定当前运行的一次性授权。没有手柄隧道、F710 sender 或 Enter 步骤。共同控制锁和原控制保护继续生效。辅助服务就绪只表示进程已监听，实际观测和任务状态另行检查；无需先切shadow再回来。若为独立诊断主动选择shadow，该次记录保持零动作含义，不混入真机完成统计。

**6000D 感知与规划端：** 感知使用独立环境，控制和图像使用两个独立 SSH 连接，mask 服务通过图像连接的反向通道访问。沿用本机已登记的 F1 SSH 路径，分别在两个终端运行：

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
  -o UserKnownHostsFile=/home/user/projects/HARNESS/.cache/robot_known_hosts \
  -i /home/user/.ssh/id_ed25519_f1 -p 22009 \
  -L 127.0.0.1:19840:127.0.0.1:19840 user@127.0.0.1
```

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
  -o UserKnownHostsFile=/home/user/projects/HARNESS/.cache/robot_known_hosts \
  -i /home/user/.ssh/id_ed25519_f1 -p 22009 \
  -L 127.0.0.1:19841:127.0.0.1:19841 \
  -R 127.0.0.1:19842:127.0.0.1:19842 user@127.0.0.1
```

mask 服务用本地感知环境启动，所有路径按6000D配置；无需把 ARM wheel 或 ROS 运行时复制到6000D：

```bash
cd /home/user/projects/HARNESS
perception/.venv/bin/python native/entry.py \
  --run artifacts/masks-$(date +%Y%m%dT%H%M%S) \
  serve masks /本地已审阅目录/masks.local.json
```

以上mask单服务命令用于维护其对应配置。当前完整任务的多视感知与全部服务由页面执行后端按已登记站点统一装配，不照抄旧单相机快照。固定程序 `baseline` 使用完整技能链且不调用模型API；Astra是后续同条件对照。

</details>

## 4. 停止、失败与证据

辅助组按依赖顺序启动，等待各服务明确响应；任一子进程退出则停止本组，先发送 SIGINT 让服务关闭自己的 socket，再有界等待并清理本组进程。它不删除其他进程的 socket、不杀占用者。下次启动使用新 run；遗留占用需要核对进程身份，不能盲删。

页面“停止”先请求 owner 撤权与保持，后端分别记录取消已请求、owner 已保持和实测已停稳，再关闭本次辅助服务与隧道。该路线没有 sender。辅助组退出不会伪造机器人已停稳；反馈不足时保留未确认状态。

每组 `--run` 下保存服务记录、日志和独立 trace。静态配置、进程监听、真实观测、实际发布和物理任务结果分别记录。当前默认明确跳过单独shadow；页面接线、安装身份和首条真实结果均以[STATUS](../agent/STATUS.md)为准，不由本页的维护命令或软件测试推定。
