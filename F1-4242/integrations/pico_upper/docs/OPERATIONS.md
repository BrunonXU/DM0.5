# 上肢 Pico 现场操作与依赖

基于 meaxiao Pico **1.4.4**，只开放双臂14轴和两个夹爪。当前副本位于 `integrations/pico_upper/`；以下以 `/home/user/Bruno/F1-infra/current/` 的新部署为例，实际路径由安装配置决定。原 LXL 保留其自己的控制/数采流程。

## 现场运行

先完成工作台 `configs/installation.env`、`config/control.json` 和脚本哈希的本机核对；`scripts/env.sh` 自动读取上层工作台安装配置。`PICO_UPPER_CONTROL_LOCK` 与工作台的 `F1_CONTROL_LOCK` 必须指向同一文件。

**机器人本机的现场终端**（不可从 SSH 启动动作），先在原运行终端退出旧 lxl 遥操/回放，保留已经运行的 PCService 及机器人驱动。新脚本会检查控制冲突，不自动杀旧工程。

```bash
cd /home/user/Bruno/F1-infra/current/integrations/pico_upper
./check.sh
./start.sh
```

`check.sh` 只读取依赖、控制占用、实测关节及碰撞模型，不创建命令发布者。它需要完整22轴 `/hal/joint_states` 和两路夹爪状态，新鲜且静止；身体/头部反馈仅供模型碰撞检测。只测依赖用 `./check.sh --dependencies`。

启动流程：持有与4242工作台共用的控制锁 → 检查旧进程及现有发布者 → 当前实测姿态与碰撞检查 → 现场按 Enter → 等待新鲜XR输入且全部控制松开 → 再次检查并捕获当前姿态 → 独立臂桥/遥操。启动不走固定姿态、复位或控制模式切换。

若PCService尚未运行，在**另一个F1本机终端**只启动已有XR服务（不要启动lxl整套遥操）：

```bash
mkdir -p /home/user/Bruno/F1-infra/runtime/pico_upper/logs
/opt/apps/roboticsservice/runService.sh > /home/user/Bruno/F1-infra/runtime/pico_upper/logs/pcservice.log 2>&1
```

这是供应商已有服务的启动脚本；它不启动我们的遥操或录制。确认服务运行后再执行本包`start.sh`。

Pico App 连接机器人实际局域网 IP（原 F1-09 为 **192.168.31.58**），选 Head + Controller 并 Send。内部臂桥使用 `127.0.0.1:42428`，**不要把内部 IP 改成局域网地址**。仅检查并复用已经运行的 PCService（63901/60061）；脚本不启动/停止它。

等待日志出现 `UPPER_TELEOP_READY` 后接管。启动终端保持前台运行；Ctrl+C 或另一个 F1 终端执行：

```bash
cd /home/user/Bruno/F1-infra/current/integrations/pico_upper
./stop.sh
```

只结束本试用包进程，不发送腰部/底盘零速，不回姿态，不打开夹爪。停止不等于硬件急停。日志：

```bash
tail -f /home/user/Bruno/F1-infra/runtime/pico_upper/logs/teleop.log
# 另一终端检查输出桥：
tail -f /home/user/Bruno/F1-infra/runtime/pico_upper/logs/bridge.log
```

## 保留的升级与本次范围

| 项目 | 当前试用行为 |
|---|---|
| 独立 Grip | 左右臂分别接管/松开保持；再次接管重新建立参考 |
| 细调 | 保留上游自适应增量：慢移平移0.2倍、旋转0.25倍，快移连续恢复；这不是实物毫米精度保证 |
| 轨迹与输出 | 50Hz IK/碰撞审批；Ruckig轨迹；250Hz最终臂重采样。上游参数不变：30°/s，输出100°/s²、600°/s³ |
| 碰撞与断流 | 保留单臂碰撞锁存、反馈/输入超时保持、释放Grip恢复；模型初始化失败额外明确拒绝启动 |
| 菜单键 | 左三横菜单重建局部坐标参考，不发复位 |
| 夹爪 | 沿用上游 `100 − Trigger × 100`：Trigger松开=100，按到底=0；硬件口径100闭、0开。本次仅对应Grip接管时发该侧夹爪命令，另一侧保持 |
| X/Y/A/B、摇杆 | 全部不产生动作/录制请求；**X+Y复位暂不提供** |
| 身体/底盘 | 不创建发布者、不起worker、不发启动/退出零速、不切模式、不调用腰升降或body_j3复位；仍读取全身反馈用于碰撞 |
| 数采/上传 | 不启动录制器、相机、传输器、中央服务和数据网页；此试用会话不录制。旧数据与旧录制逻辑未修改 |

开始时先松开扳机/Grip。Trigger继承上游反向定义，首次接管某臂时要注意对应夹爪目标；不要在持物情况下凭旧版按键记忆操作。不可同时运行旧lxl遥操、新版试用或4242动作会话。

## 依赖和边界

- Python3.12、ROS Jazzy和现有F1消息/驱动；numpy、ruckig、placo/placo_utils、meshcat、mujoco、coal、PyYAML；XR SDK及PCService沿用F1本机安装。
- 默认先用 `runtime/venv/bin/python`，若无则只读使用现有 `pico4ultra_py312`。通过 `PICO_UPPER_PYTHON` 指定独立环境，脚本不安装/升级原环境。
- 本包不需要 move 消息、pyarrow、MCAP或相机。它不是全身版的替代部署，独立录制由工作台管理，见 [录制合同](../../../docs/RECORDING.md)。
- 上游模型及臂映射沿用源码，F1-09机械映射和实物操作仍需现场验收。离线测试/只读检查不能代替真机验收。若反馈、模型或控制源不满足要求，保持拒绝，不跳过保护。
- 上游关闭CoM质心保护的选择保持原样；保留其碰撞保护。模型碰撞检查不等于全工作空间环境感知。

变更证据见 [适配说明](adaptation.md)、[上游来源](../UPSTREAM.json)。依赖、文件哈希、离线测试和现场验收分开记录；真实部署结果以主任务部署清单为准。
