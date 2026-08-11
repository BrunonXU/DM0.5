# 智元GDK 2.6.3：本项目相关接口摘要

来源：智元机器人开发套件官方文档，版本2.6.3。整理日期：2026-07-31。
本页是项目相关摘要，不替代官方原文。

官方入口：

- [GDK支持站](https://support.agibot.com/?gdk_version=2.6.3)
- [Python Common](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/reference/Python/common.md)
- [Python Robot](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/reference/Python/robot.md)
- [Python Camera](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/reference/Python/camera.md)
- [PTP](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/howto/ptp.md)
- [关节错误码](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/appendices/joint_error_code.md)

## 生命周期

每个进程按顺序：

```python
import agibot_gdk as gdk

assert gdk.gdk_init() == gdk.GDKRes.kSuccess
try:
    robot = gdk.Robot()
    # 等待DDS稳定后使用接口
finally:
    gdk.gdk_release()
```

`source /home/agi/app/env.sh`只准备动态库、`PYTHONPATH`和配置路径，不等于
`gdk_init()`。

## Robot状态

本项目使用：

- `get_joint_states()`：手臂关节反馈；
- `get_whole_body_status()`：双臂、末端型号、错误和急停；
- `get_motion_control_status()`：mode、错误和碰撞对；
- `get_end_state()`：左右o10_t2的10个手指关节。

官方特别说明：`get_joint_states()`当前应使用`motor_position`和
`motor_velocity`获取电机位置/速度，`position/velocity`为低速电机预留。本客户端
读取手臂时优先`motor_position`。

G2伺服健康状态当前按：

```text
mode=5
error_code=0
left_end_model=o10_t2
right_end_model=o10_t2
```

状态刚初始化时可能为空，不能一次失败就认定硬件异常。

## `joint_servo_control`

官方合同：

- 需要100 Hz控制；
- `JointServoControlReq.control_period`单位秒；
- `joint_names`和`joint_positions`非空且等长；
- position单位弧度并必须处于官方限位；
- `joint_velocities`为预留字段，可以为空；
- `enable_low_latency`默认`False`；
- normal有碰撞保护；
- low-latency适合更高实时性，但没有碰撞保护。

官方原文确认：

```text
joint_servo_control:
  支持机械臂与末端同时控制
  官方示例展示单侧手臂7 + 该侧末端

move_ee_pos:
  group=dual_tool支持双末端
  o10_t2双末端需要左10 + 右10，共20个position
```

官方支持机械臂与末端同时进入`joint_servo_control`，但当前G2实测统一34D请求会让
20个O10名称反复报`Param ... not found`，并发生过2.53秒阻塞。项目正式路径因此按
真机验证结果改为Split：双臂前14D以100Hz走`joint_servo_control`，双手后20D以
5Hz走`move_ee_pos(dual_tool,o10_t2)`。统一34D和仅双臂只保留为显式诊断模式。

该Split选择是本项目针对当前机器人/GDK配置的工程结论，不表示官方接口普遍不支持
手臂与末端合并。

低延迟不得作为normal碰撞失败后的自动回退。

## `move_ee_pos`与O10零命令

官方合同：

- `group`必须是`left_tool`、`right_tool`或`dual_tool`；
- `target_type=o10_t2`时单手10个`JointState.position`；
- `dual_tool`时左右各10个，共20个，前半为左、后半为右；
- 官方典型状态把左右O10的10个全零都定义为“开手”。

因此G2重启后读到的20D全零反馈不能自动当作“保持当前物理姿态”的安全命令回发。
旧客户端曾在模型动作到达前把零反馈放进统一34D current-hold，真实触发双手开手，
拇指向上接触相机。当前客户端首个模型目标接受前不发送动作，并锁定current-hold；
唯一复位使用人工确认的20D命令空间Home，不使用反馈零点。

官方Robot 2.6.3页面没有把`set_control_mode()`列为独立公开章节。因此当前客户端
不会自动切换模式：powered前必须已经是健康的mode 5，否则直接拒绝。以后如需自动
切换，必须先在G2探测签名或向官方确认，不得根据其他版本猜测。

## Camera

本项目使用`Camera.get_latest_image()`读取：

```text
kHeadColor
kHandLeftColor
kHandRightColor
```

相关官方接口还包括：

- `get_nearest_image()`；
- `get_image_shape()`；
- `get_image_fps()`；
- `get_image_latency()`；
- `get_camera_intrinsic()`；
- `close_camera()`。

客户端必须把JPEG/PNG或未压缩消息解码成RGB HWC `uint8`。任一路失败整次观测
无效，不得用黑图替代。

## PTP和错误码

当前30→100 Hz播放使用G2本机monotonic时钟，PTP警告不直接阻塞单机控制。
进入跨设备时间对齐前，应按官方PTP指南配置。

关节错误必须同时记录：

- whole-body错误码；
- 关节`error_code`；
- 末端`status/err_code`；
- motion `error_code/error_msg`；
- collision pairs。

遇到官方摘要未覆盖的接口，先打开相应2.6.3原文，再更新本页。
