# Pico 网页启停与现场操作

4242 提供 LXL 原流程和 meaxiao 1.4.4 上肢适配两个固定入口。网页会话代码位于 `teleop_service.py`、`teleop/worker.py`；上肢源码位于 `integrations/pico_upper/`，LXL 使用机器已有安装。先按 [部署说明](SHARING.md) 核对路径、机器合同和脚本哈希。

## 网页使用

1. 先按原现场流程启动机器人基础服务，等待关节和夹爪反馈。4242 开机自启不启动驱动、Pico 或录制。
2. 打开 `http://机器人IP:4242/#pico`，输入已有控制码启用操作；释放“姿态调整”的控制权。
3. 选择“准备启动上肢版”或“准备启动 LXL”。准备阶段检查机器身份、脚本 SHA、完整关节/夹爪反馈、已有控制发布者与遥操进程。
4. 预检通过，页面显示“我已在现场，确认启动”。只有发起准备的浏览器可确认；120 秒内未确认取消本次准备。LXL 显示实机 `ip_config.py` 中的初始和复位角度；它启动后可能运动到配置初始位，并保留腰部控制。
5. meaxiao 保留原生预检和终端确认门；网页的明确确认传入原生门，随后等待 Pico 新鲜数据、两侧 Grip/扳机松开与摇杆居中，再启动遥操。状态“进程就绪”不等于实物动作验收。
6. 点击“停止本会话遥操”结束该会话；准备中同一按钮用于取消。建议先结束录制，但停止遥操不以录制结束为前提。停止属于软件进程退出，不是硬件急停或全身归位。

**刷新、关闭浏览器或重启 HTTP 网关不会自动停止运行中的遥操。** 遥操是独立 `f1-4242-teleop-session.service` 临时用户服务，`Restart=no`，不会随开机/故障自动重启。已登录的其他浏览器可以停止网页会话，但不能代替原浏览器确认启动。网关重启后未完成的确认需要取消后重新准备。

网页只停止本会话 cgroup 内的进程。现场手动启动的遥操不会被接管或结束，需从原入口停止。两套网页会话互斥；与姿态控制使用 `F1_CONTROL_LOCK` 指定的共用锁。原 LXL 不认识此锁，因此额外检查已存在进程和 ROS 发布者；人为从外部终端启动程序仍须避免抢占。

PCService：上肢版复用现存进程，或在准备时启动本会话专属实例；停止时只结束专属实例。LXL 原启动器无条件创建 PCService，网页检测到外部 PCService 时拒绝准备，避免重复实例，需从其原入口结束或选择上肢版。准备可启动 XR 通信服务，不启动电机驱动或数采。

## 脚本与日志

在 **F1 user 的现场终端**执行，不从远程验证脚本触发动作：

```bash
# 原 LXL
cd /home/user/lxl
./start_all_pico1.sh
# 原停止脚本，会按名称批量结束 LXL / PCService
./stop_all_pico.sh

# meaxiao 上肢版；原终端流程先确保 PCService 已运行
cd /home/user/Bruno/F1-infra/current/integrations/pico_upper
./check.sh
./start.sh
# 另一现场终端停止本上肢会话
./stop.sh
```

网页不会调用 LXL 原停止脚本（其使用全局 `pkill -f`）。网页日志在 `/home/user/Bruno/F1-infra/runtime/teleop/sessions/<id>/session.log`，页面可展开查看；同时可用 `journalctl --user -u f1-4242-teleop-session.service`。上肢运行日志在 `/home/user/Bruno/F1-infra/runtime/pico_upper/logs/`；LXL 原日志为 `remote_sth/remote.log` 和 `XRoboToolkit-Teleop-Sample-Python/teleop.log`。LXL 启动器原有日志写入/运行时拷贝行为照常，本次不改其代码与数采路由。

拆分前的现场脚本 SHA 保留在 [历史来源](history/SOURCES.md)。当前上肢副本位于本工程 `integrations/pico_upper/`，请按 `teleop/config.json` 核对实际脚本及 SHA。

启动脚本变化时拒绝执行，先审查并更新固定配置。原 LXL 包含加密模块，文档 v5 不代表已解密核验其全部实现。

## 两套手柄与数据行为

| 功能 | LXL（用户提供的 v5 文档） | meaxiao F1-09 上肢适配 |
|---|---|---|
| 双臂接管 | 扳机按下跟随，松开停止，重新接管基于当前位姿 | 左右 Grip 独立接管、会话实测姿态对齐 |
| 夹爪 | 默认闭合；扳机按下张开、松开闭合 | 接管后扳机控制，未接管不改变夹爪 |
| A / B | 开始 / 结束 MCAP 录制 | 禁用；数据页独立 LeRobot 录制 |
| X / Y | 降腰 / 升腰，按键超过 0.3s | 禁用 |
| XY | 按配置复位，回 home 时禁止遥操 | 禁用 |
| 初始/复位角度 | `ip_config.py` 的 `target_left/right`、`reset_left/right` | 不复用 LXL 固定初始动作 |
| 数据目录 | `/home/user/lxl/pico_data/all_data/` | 见数据页显示的独立 output_root |
| 频率 | 文档 v5 标称 30 Hz；不能据此认定每路实测频率 | 当前 16D / 10 FPS 独立录制 |
| 上传 | 原有逻辑不调整，本入口不加上传 | 未配置中央服务器与自动上传 |

Pico App 连接同局域网 F1，选择 Head、Controller、Hand、Send。LXL 的 `ip_config.py:local_ip` 应为 Pico 所在局域网地址，当前固定配置为 `192.168.31.58`。网页只读取该文件，不自动改写。

## LXL 文档补充

以下来源为用户本次提供的文档，未把说明日期等同于实机部署版本：v1=20260612；v2=20260618 新增初始/复位配置；v3=20260626 扩为 13 类数据、开放主端 TCP 接口、修改夹爪及数采启停；v4=0716 优化卡顿/复位跳变、增加双臂与身体碰撞检测；v5=0804 更新 URDF、修复双臂跟随不对称并标称 30 Hz 采集。

13 个录制主题：

```text
/hal/joint_states
/motion_ctl/gripper/left/state
/motion_ctl/gripper/right/state
/state/right_arm/tcp_pos
/state/left_arm/tcp_pos
/camera/left_wrist/color/image_raw/compressed
/camera/right_wrist/color/image_raw/compressed
/camera/head/color/image_raw/compressed
/lead/joint_states
/motion_ctl/gripper/left
/motion_ctl/gripper/right
/lead/left_tcp
/lead/right_tcp
```

原轨迹工具 `/home/user/lxl/remote_sth/dist/ros2_bag_tool.py`：`record -n hello` 保存到 `/home/user/lxl/pico_data/ros2bags/hello`；`play -b <包路径>` 可驱动手臂/夹爪跟随。`ros2 bag play <mcap>` 也可能重发包里的命令主题，因此不是无动作的视频预览，未接为网页一键回放。

主端原始 TCP 下发到 `/lead/right_tcp_control`、`/lead/left_tcp_control`，消息 `sensor_msgs/msg/JointState`，`name=['x','y','z','rx','ry','rz']`、对应 `position`。自定义数据 `header.frame_id='press_pico'`；复用录制时保留原 frame_id。需先用 `/lead_tcp/replay_mode` 的 `std_msgs/msg/Bool` 切换 True，退出为 False。单位和坐标系仍需结合部署实现核对，不在新网页开放这个动作接口。

碰撞检测文档范围：动态双臂对静态身体；检测到阈值时该臂停在上帧安全位置，输入退出危险区后恢复。文档明确没有动态双臂之间检测，不能视为完整碰撞保护。

### 当前基础启动器的冲突提示

2026-09-24 只读检查发现 `waist_updown_controller` 与 `whole_body_controller` 发布 `/motion_ctl/joint_ctl`。新网页会显示名字并拒绝启动，不把“尚未按手柄按钮”当作没有其他控制器。需由现场操作者从原入口退出这两个控制程序；不要结束 `motor_ctl` 或相机驱动来清除提示。此说明是当时观测，实际以页面实时图为准。
