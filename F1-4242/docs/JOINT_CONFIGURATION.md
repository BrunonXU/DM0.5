# 全身关节配置

这是 F1-09 的关节配置阅读入口。运行配置与模型的核对基线是 [`configs/f1-09-joints.json`](../configs/f1-09-joints.json)：它覆盖双臂 14 轴、身体 5 轴、头部 3 轴，并分别记录命令范围、原始 URDF 范围、模型方向及标定状态。

**配置一致不等于机器人已完成标定。**当前双臂只核对了相对原生 FK 的角运动关系；身体、头部、机构尺寸、工具坐标、编码器零位和物理硬限位尚未通过实物验收。不能把 JSON 中的 `limits_verified` 或离线报告中的 `ok` 解读成整机标定完成。

## 配置各自负责什么

| 文件 | 用途 |
|---|---|
| [f1-09-joints.json](../configs/f1-09-joints.json) | F1-09 的完整来源合同：22 轴、驱动身份、原始模型哈希、预设来源及验收边界 |
| [f1-09.json](../configs/f1-09.json) | 现场参考 profile；`joint_limits_deg` 与 `limits` 两份兼容字段必须同时符合合同 |
| [model_mapping.json](../configs/model_mapping.json) | HAL 度数到模型角度的规范配置，真实运动命令不应用这些符号转换 |
| [模型清单](../web/models/f1_wheel/manifest.json)、[生成几何](../robot/model_geometry.json) | 前端/后端模型数据；必须与规范配置、原始 URDF 和资产哈希一致 |
| [joint_contract.py](../robot/joint_contract.py) | 离线只读核对器；不初始化 ROS、不打开网络连接、不发布命令 |

profile 通过 `"joint_contract": "f1-09-joints.json"` 明确启用合同。只允许 `configs/` 下白名单文件名，不能传任意路径。其他机器人与 [只读模板](../configs/robot.example.json) 未配置时返回 `not_configured`，不能复制 F1-09 的验收标志冒充完成换机核验。

当前绑定的控制器 SHA-256 是 `898ef58fb7118ecbf416af134e01700ed38ffdd3480293cf06fe95da14bce0c8`。合同同时绑定 F1-09 的 machine-id；本模块只核对 profile 中的期望值，真实运行进程、机器身份及模式仍由 [运行合同](../robot/runtime_contract.py) 另行读取。版本号、显示名称不作为硬件依据。

## 22 轴对照

全部命令与反馈单位为 **HAL 度数**。URDF 原文使用弧度，下表仅换算为度便于比较；原始数值仍保存在合同中。模型角度公式为 `rad(sign × HAL_deg + offset_deg)`。表内偏置 0° 表示当前没有额外模型偏置，**不表示编码器零位已标定**。

“命令范围”绑定已审计控制器的下发限制，不宣称是物理硬限位。旧 URDF 范围有大量不同，不能用它放宽或替换命令范围，也不能简单凭范围方向猜测身体 J3 应反号。

| HAL 关节 | URDF 关节 | 命令范围（°） | 原始 URDF 范围（°，约） | 模型 sign / offset | 标定状态 |
|---|---|---|---|---|---|
| `arm_l_j1` | `arm_L_joint1` | -178 至 178 | -349.887339 至 349.887339 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_l_j2` | `arm_L_joint2` | -94 至 120 | -120 至 120 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_l_j3` | `arm_L_joint3` | -178 至 178 | -178 至 178 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_l_j4` | `arm_L_joint4` | -145 至 60 | -145 至 145 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_l_j5` | `arm_L_joint5` | -178 至 178 | -178 至 178.000798 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_l_j6` | `arm_L_joint6` | -60 至 60 | -90 至 90 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_l_j7` | `arm_L_joint7` | -90 至 90 | -60 至 60 | -1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j1` | `arm_R_joint1` | -178 至 178 | -349.887339 至 349.887339 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j2` | `arm_R_joint2` | -94 至 120 | -120 至 120 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j3` | `arm_R_joint3` | -178 至 178 | -178 至 178 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j4` | `arm_R_joint4` | -145 至 60 | -144.998429 至 120 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j5` | `arm_R_joint5` | -178 至 178 | -178.000798 至 178.000798 | -1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j6` | `arm_R_joint6` | -60 至 60 | -90 至 90 | -1 / 0° | 仅原生 FK 角运动已核对 |
| `arm_r_j7` | `arm_R_joint7` | -90 至 90 | -60 至 60 | +1 / 0° | 仅原生 FK 角运动已核对 |
| `head_j1` | `head_joint1` | -40 至 40 | -45 至 45 | +1 / 0° | 未验收 |
| `head_j2` | `head_joint2` | -25 至 25 | -28 至 28 | +1 / 0° | 未验收 |
| `head_j3` | `head_joint3` | -15 至 15 | -15 至 15 | +1 / 0° | 未验收 |
| `body_j1` | `body_joint5` | -130 至 130 | -165 至 165 | +1 / 0° | 未验收 |
| `body_j2` | `body_joint4` | -10 至 10 | -20 至 20 | +1 / 0° | 未验收 |
| `body_j3` | `body_joint3` | -70 至 0 | -5 至 88 | +1 / 0° | 未验收 |
| `body_j4` | `body_joint2` | -95 至 0 | -94 至 0 | +1 / 0° | 未验收 |
| `body_j5` | `body_joint1` | -40 至 0 | -36.000027 至 0 | +1 / 0° | 未验收 |

左臂 J7、右臂 J5/J6 的模型反号来自 2026-09-28 的 16 组纯 FK 虚拟姿态对照，原生 FK 响应与 DH 配置的摘要保存在规范配置中。它不改变控制界面的真实角度、不改变机器人接收的关节命令，也不代表精确工具位姿已标定。详见 [模型审计](MODEL_AUDIT.md) 与 [模型显示](MODEL.md)。

原始 URDF 为 meaxiao 1.4.4 的 `f1_wheel_0602_new.urdf`，SHA-256：`bc21c6f36446aa2741d6ba20c2190836be967b1f3043dfa39eaae0983c95d5c0`。保留其全部关节原点、轴、范围和 31 份 STL；不通过改写原模型消除表中差异。离线审计发现左臂两个名义 18 mm 轴距、两侧 DH 314 mm 与 URDF 311 mm 的差异；沿轴方向的 3 mm 不能唯一分配到某个 link 原点，机构与工具尺寸仍需实物确认。 标准pose_2的独立离线计算还显示，现用原始URDF的左Link7原点比右侧高约24.154mm；原生FK在名义共同安装坐标下预测两侧TCP等高。左两处18mm差异可解释模型高度差，但Link7与TCP并非同一测量点，肩与工具安装也未实测，几何修正尚未落地。私有复算记录在 `results/pose_symmetry_20260928/`；不要据图中高差改真机目标或标零。

## 待考察项：F1-09-MODEL-001

复位左右高度/模型一致性差异归档为 **待考察；当前固定关节 demo 非阻塞**，前提是底盘、工装、工具安装不变且现场确认实物重复回位满足任务。保留原生 pose_2、驱动命令范围及原始几何，不追加视觉调平角度。当前约 24.154 mm 仅为标准目标下模型 Link7 原点高差，不能填入实物精度指标，也不代表左侧高是设计零位要求。

精确 TCP/IK、模型碰撞/仿真对齐需求，或实物偏差影响抓取时，重新核查本机机构与安装、同次到位角度和同实体点测量；详细依据及处理原则见 [模型审计](MODEL_AUDIT.md)。v22 的成功到位记录有助于后续区分关节跟随与模型差异，但不补齐旧数据或替代实物测量。

## 哪些不属于这 22 轴

URDF 共 30 个 joint。剩余 8 个是四个轮组各两个 `revolute` 关节，目前只作静态外观，没有 HAL 动态映射，不参与 22 轴复位。完整名称记录在合同 `non_hal_model_joints`。

2026-09-28只读反馈还看到12个 `hand_l_*` / `hand_r_*` 灵巧手字段（每侧thumb、index、middle、ring、little、thumb_rot），读数均为0。它们没有进入本机22轴姿态或URDF映射；字段存在和数值为0不证明这台机器装有对应灵巧手，也不证明这些数据有效。

左右夹爪通过独立 OmniPicker 反馈和命令接口控制。这份 URDF 没有活动手指关节，所以没有用 22 轴或虚构指关节表示夹爪开合。底盘平移、旋转和里程计也不属于本地姿态模型。详见 [机器人接口](API.md) 与 [模型范围](MODEL.md)。

## 复位姿态来源

复位是一个姿态目标，不是标零指令，更不是所有关节设成 0°。

| 预设 | 关节目标（HAL 度数） | 来源及范围 |
|---|---|---|
| 双臂复位 | 左 `[110,-90,-90,-110,0,0,0]`；右 `[-110,-90,90,-110,0,0,0]` | LXL `reset_left/reset_right`，仅 14 轴 |
| 全身复位 | 上述双臂；身体 `[0,0,-5,-10,-5]`；头部 `[0,0,0]` | 原生 `whole_body_config.ini` 的 `pose_2`，22 轴；不包含夹爪和底盘位移 |

历史来源：F1-09 `/home/user/lxl/ip_config.py`，SHA-256 `0a7626afc7f3e854720f767fb79f2d755c530d00cdfe0a8b0e29897fc8d4d78f`；原生 `/home/user/workspace/whole_body_ctl/whole_body_config.ini`，SHA-256 `5bd2a4cb8c6c91ce918b19f822bd5f55421f7716772dd74149f8c1390ed98cd1`。这些路径只描述历史证据，离线核对不会读取机器人文件或其他账号目录。

核对器检查 profile 默认预设的 key、范围、单位、值和来源，不管理用户 runtime 中自定义/删除的预设，也不会重新植入已删除项。全身目标的定义与实机整套复位验收分开；动作是否可执行仍需当前反馈、控制权、路径与运行合同。参见 [一键任务](POSE_TASK.md)、[复位问题](RESET_REPAIR.md)、[当前状态](STATUS.md)。

## 头部回程配置与现场结果

v20 / 0.6.4 的 F1-09 profile 启用 `head_boundary_recovery`：`enabled=true`、`max_outside_deg=3`、`timeout_s=3`。这三个值只约束经明确确认的异常起点回程，不更改表中的±25°命令范围，不代表新的机械极限。通用模板默认关闭。具体执行合同见 [复位修复](RESET_REPAIR.md) 和 [API](API.md)。

2026-09-28 15:19，按用户明确指令执行仅头部J2回到0°：HAL实测从−27.644617°到−0.002370°，约4.17秒完成；后续稳定读数约−0.000108°。原生EC日志也从Raw−256660变为Raw−1。未修改驱动、编码器零位、URDF或命令范围，双臂与身体未接收本次运动目标。

15:19后两种默认复位预检通过，当时全身预计8.62秒；该数值只是当时起点的历史预检。15:27实际整机重启后工作台自启恢复（见 [AUTOSTART](AUTOSTART.md)），用户15:28另行执行原生pose_2全部22轴，后台动作计时4.265430秒、四组HAL到位；15:29再次执行返回 `no_motion=true`，继续观察反馈后确认。

用户15:36另存“积木demo”（来源 `measured_feedback`），15:37执行7.525472秒成功。它是用户自定义22轴目标，不是替换默认pose_2，也不是严格镜像姿态。用户后来又修改了积木预设；升级按实际runtime内容与哈希保留，不硬编码旧名称或ID，也不得用历史值覆盖。私有原始记录在 `results/pose_completion_20260928/post-user-full-reset.json`。

完整复位与该demo已有现场动作记录，但到位成功不等于模型/编码器/机械限位完成物理标定；表内标定状态不因此升级，任意姿态碰撞路径和物理停止仍未验收。v21后续修补已部署并通过ARM 400项回归和线上只读核验，当前版本见 [STATUS](STATUS.md)。

## 离线核对与失败处理

在工程根执行：

```bash
python3 -m robot.joint_contract --profile configs/f1-09.json
```

只核对打包文件，成功退出 0；错配、缺失或未配置退出 1，打印 JSON 报告。API 为：

```python
from robot.joint_contract import verify_joint_contract
report = verify_joint_contract(profile)
# report['status']: verified / mismatch / not_configured
# report['ok']: 仅在已配置且全部一致时为 True
```

核对包括两份限位与绑定合同、完整 22 轴分组和映射、角度单位、驱动绑定、默认复位预设、规范模型映射、生成关节树、原始 URDF 和 32 个模型资产的 SHA。不一致返回 `errors`；不会自动放宽限位、修改模型、改标定参数或发送恢复动作。

**这是启动时对模块所在工程 `ROOT` 中打包配置和模型的核对。**它不是持续文件监控；若 `F1_WEB_ROOT` 指向独立 dev 目录，那个热更新目录不因此得到同一份核验。发布时应核对实际服务与网页入口，不能凭报告推断所有热更新内容均未变化。

报告里的 `physical_calibration_verified`、`motion_acceptance_verified`、`live_driver_verified` 始终为 `false`，表示这个离线核对器不作实机验收声明，不否定另行保存的15:28现场复位成功记录。真实运行身份需运行合同另查，新的 HAL 接收时间也不独立证明编码器硬件反馈新鲜。

升级驱动或模型时，先核对新版本资料与差异，再更新合同及对应配置；不能只替换哈希跳过依据。模型角度配置更新后用 `python3 scripts/prepare_model.py --check` 检查生成物，原 URDF/STL 保持原字节。定向错配回归：

```bash
python3 -m pytest -q tests/test_joint_contract.py tests/test_joint_configuration_startup.py
```

[部署与换机](SHARING.md) · [模型审计](MODEL_AUDIT.md) · [姿态调整](POSE_TASK.md) · [文档索引](README.md)
