# F1 动作映射依赖核对

核对日期：2026-09-14。承接[第一批四文件证据](../20260914_action_mapping/README.md)，本轮只读提取同一遥操作包内 4 个依赖文件，共 **78,756 字节**。逐文件主机、原路径、解析路径、UTC 时间、大小、远端与本地 SHA256、Git 获取结果见 [manifest.json](manifest.json)。4/4 文件校验一致；另将前后两批共 8 个文件在 F1 重新计算哈希，8/8 与快照一致，见 [交叉核验](cross_snapshot_hash_verification.json)。这不是文件系统原子快照，也未取得 Git 历史或采集版本证据。

**已闭合的是这组候选源码中普通双臂遥操作的数值链路：`/lead` 和 UDP 双臂值都源于同一组已通过上游保护的 `_last_safe_left/right`，单位为度；UDP 桥按左 7、右 7 原值发布到 `/motion_ctl/joint_ctl`。两个发送路径异步取样，因此不能把某条 `/lead` 消息当作某个实际执行指令的逐帧确认。**

没有修改 F1、启动节点、导入/执行控制源码、发送 ROS/UDP/机器人动作，未更改原数据、清洗阈值或模型合同。文件名中的 simulation、sim 不足以判断是否会驱动真机：这条路径实际包含 UDP `sendto` 和执行器 ROS 发布。

## 原样文件

| 文件 | 字节 | 作用 |
|---|---:|---|
| [base_teleop_controller_ros2_mixin.py][ros] | 39,745 | `/lead`、夹爪发布与外部 TCP 回放回调 |
| [send2real_robot_f1_udp.py][udp] | 6,586 | Placo 到硬件角度映射和 UDP 序列化 |
| [placo_teleop_controller.py][placo] | 4,212 | 具体 Placo 子类及主循环 |
| [base_teleop_controller_ik_mixin.py][ik] | 28,213 | 普通遥操作中真正的目标生成和 UDP 发送调用 |

这些文件均不是软链接，未取得 Git 元数据。获取工具 [capture_selected.py](capture_selected.py) 仅用于定点读文件并校验，拒绝覆盖已保存源码，未导入参考文件；其启发式凭据扫描不等同完整安全审计。

## 调用链和具体行证据

1. [Base][base] 第 19–21 行导入 UDP 发送模块及两个 mixin，第 91 行继承 `IkVizMixin, Ros2TeleopMixin`，第 233 行构造 UDP 发送器。`_send_command` 在第 1387 行为抽象方法。
2. [Placo 子类][placo] 第 13 行继承 Base，第 57–59 行 `_send_command` 实现为 `pass`；第 83–98 行主循环先调用 `_update_ik()` 再调用该空方法。因而查找 `_send_command` 本身会漏掉真实发送点。**本次没有证明实际启动入口选择了这个子类或这些文件就是采集版本。**
3. [IK mixin][ik] 第 283–294 行为普通 `_update_ik` 路径入口；第 376–406 行完成初始 IK 偏差扣除、弧度转度/符号/固定偏置和启动/复位动态偏置。第 415–416 行提交原始双臂目标并取回安全缓存，第 418–419 行附加夹爪值，第 421 行发 UDP，第 426 行再更新 ROS 发布缓存。
4. [Base][base] 第 777–810 行 `_stage_and_get_safe_arm_command` 校验 14D 和有限值，将新目标写入 `_raw_arm_joints`，但返回 `_last_safe_left/right` 的副本。它不直接返回刚提交的原始 IK 目标。
5. [ROS mixin][ros] 第 686–719 行单独按 `command_hz` 发布 `/lead`：先复制 `_latest_both_arm_grip`，再于第 705–709 行从 `_last_safe_left/right` **重新取样并覆盖前 14D**；去掉两个夹爪值后发布。名字来自 [Base][base] 第 328–332 行。
6. [UDP 模块][udp] 第 56–66 行把输入原值序列化为 `float64` 字节发送，没有角度再转换或位置裁剪。桥接 [udp2topic][bridge] 第 195 行按 `float64` 解码，第 271–293 行检查有限值并分拆左/右臂，第 44–48、111–116 行以硬件关节名原值写入 `JointState.position`。

## 数值映射表

下面第一张表说明 **Placo 内部量生成硬件目标前** 的转换；不是对已经录制的 `/lead` 再做一次转换。

令 `rL/rR = placo.state.q[12:19 / 19:26] - delta_init_and_solver`，单位为弧度；`D(r)=r*180/pi`。
初始化偏差定义见 [Base][base] 第 248–272、280–285 行；映射本体见 [UDP 模块][udp] 第 99–113 行。

| 关节 | 左臂 angle_map 输出（度） | 右臂 angle_map 输出（度） |
|---|---|---|
| J1 | `D(rL1) + 90` | `D(rR1) - 90` |
| J2 | `-D(rL2) - 90` | `D(rR2) - 90` |
| J3 | `D(rL3) - 90` | `D(rR3) + 90` |
| J4 | `-D(rL4) - 90` | `D(rR4) - 90` |
| J5 | `D(rL5)` | `-D(rR5)` |
| J6 | `-D(rL6)` | `-D(rR6)` |
| J7 | `D(rL7)` | `D(rR7)` |

随后每个关节还减去当次启动或复位确定的 `origin_delta` 或 `reset_delta`，再经过上游保护。偏置来自启动姿态和会话复位目标，不是单靠上表就能确定的通用常数。XR 断开后重连还会根据实测反馈重新建立 origin 偏置，见 [IK mixin][ik] 第 48–72 行。

| 录制/发送位置 | 目标位置与名字 | 数值含义及候选转换 |
|---|---|---|
| `/lead.position[0:7]`，`arm_L_joint1..7` | UDP `[0:7]` → `arm_l_j1..7` | 均取左臂安全缓存，硬件目标角度；同一数值时刻无需符号或偏置变换 |
| `/lead.position[7:14]`，`arm_R_joint1..7` | UDP `[7:14]` → `arm_r_j1..7` | 均取右臂安全缓存，硬件目标角度；同一数值时刻无需符号或偏置变换 |
| IK 拼接 `[14] / [15]` | `/motion_ctl/gripper/left`、`/right` 的 `position` | 普通路径为 `100 - trigger*100`；ROS 直接取缓存原值，不进入 `/lead` 的 14D |
| 夹爪 `max_effort` | 两个 GripperCommand | 读取 `control_config["gripper_max_effort"]`；本次未读取生效配置值 |

夹爪证据：[IK mixin][ik] 第 408–426 行、[ROS mixin][ros] 第 724–742 行。桥的 `send_both_arms_and_grippers` 名字虽然包含夹爪，其当前第 98–120 行只发布双臂；实际夹爪发布来自 ROS mixin。不要误用桥函数参数中的默认 `15.0` 作为当前夹爪力度。旧 `_update_ik_lxl` 中另有 `trigger*100`（第 203–204 行），但它不是这里的普通 `_update_ik` 发送路径，不能只搜索到一处公式就推定当前语义。

**相同缓存来源不等于相同消息/发送周期。** UDP 在每个激活控制器的 IK 分支内调用；`/lead` 在另一线程按配置周期重新取安全缓存。缓存可能在两次取样之间更新，UDP 还可能因为非阻塞发送缓冲区满而丢帧。这里没有硬件接收确认或一一对应序列号，也没有测量实际时间差。清洗中的消息时间对齐不能补出此类执行确认。

## 保护发生在哪一层，J6 能否直接放行

| 层 | 已读代码中的行为 | 尚不能推出的结论 |
|---|---|---|
| 目标生成 | IK 全姿态路径包含半径 1.5 的笛卡尔工作区限制（IK 第 349–366 行） | 这不是 J6 关节角限位 |
| 原始关节入队 | Base 第 784–805 行校验 14D/有限值；大步变化只记诊断，目标仍入队 | `max_raw_target_step_deg` 不是拒绝/裁剪阈值 |
| 异步轨迹与碰撞 | Base 第 637–677 行按 Grip 授权轴并调用轨迹规划；第 646–663 行过期反馈冻结；第 709–757 行根据碰撞结果按臂提交安全缓存 | `JerkLimitedTrajectory`、`SafetyProtector` 内部本次未读取，不能声称全部位置限位已经闭合 |
| UDP 打包和桥接 | 原值传输；桥检查有限值、复位时暂不接受双臂帧 | 发送成功不等于电机已执行 |
| 桥内 `limit_joint_left/right` | 桥第 215、244 行存在定义，但第 271–313 行处理路径未调用 | 不能以这两段定义证明 J6 被裁剪；更不能运行/套用其中公式修标签 |
| 已安装 motor_ctl 配置 | [launch][launch] 第 21–22、37–38 行双臂 J6 写为 `[-60,60]` 度；J2 为 `[-94,120]` | 尚未读运行参数和驱动处理实现；超界时是拒绝、截断、告警或其他行为未知 |

因此，先前 5 条 J6 目标超过文档范围、最大约 68.75 度的录制**仍然需要复核，不能因本次找到上游保护而自动通过，也不能把原始目标直接截到 60 度后当成真实执行动作**。原数据与既有掩码本轮均未更改。

## 与清洗有关的额外边界：复位不一定经过 /lead

[IK mixin][ik] 第 93–120 行 X+Y 复位明确保留录制状态，同时调用 `home_position()` 发送标量 `1`；[UDP 模块][udp] 第 121–124 行可见该标量发送。
[桥][bridge] 第 157–176、303–304 行将其交给 `plan_and_move_session_pose`，这条内部动作生成路径本次未展开。与此同时，[ROS mixin][ros] 第 694 行在 `_reset_in_progress` 时跳过普通 `/lead` 发布。

这只能证明 **候选代码允许“录制继续、复位由另一条动作路径执行”的情况**，不能认定任何特定 episode 已发生此事。后续清洗应把复位候选区间与 `/lead` 缺口、HAL 运动和实际命令来源一并核对；不能跨缺口补持有目标来代表复位轨迹。现有 100 ms 新鲜度掩码有助于隔离长缺口，但不能替代复位事件证据或捕获缺口起始处全部异常。

外部 TCP replay 回调（ROS mixin 第 540–576、631–667 行）也会经过角度映射、动态偏置、安全缓存后发 14D UDP；它与普通 IK 的更新/缓存节奏不同。本轮没有把该路径当作泡沫棉数据的来源。

## 下一步需要的最小证据

1. 采集时实际遥操作入口及版本：确认这里普通 `_update_ik` 和两路 mixin 是当时使用的实现；今天文件哈希一致不证明历史一致。
2. 当前及采集时 motor_ctl 有效参数和超限处理实现：解释 5 条 J6 超界样本，确定回放时的硬件约束。配置文件数值和实际运行参数分开记录。
3. 从录制可用 topic / 事件与图像中查复位区间；若没有真实执行命令，继续隔离无法证明的监督，不把 `/lead` 当执行 ACK。
4. 候选映射可供后续适配器设计使用；在版本和运行约束核对前，不以此直接发布真机动作、改数据标签或开启训练。

本轮在普通发送链路闭合后停止，没有扩展到相机、系统组件、其他用户项目或完整安全库审计。

[ros]: files/home/user/meaxiao/pico/vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller_ros2_mixin.py
[udp]: files/home/user/meaxiao/pico/vendor/xrobotoolkit/xrobotoolkit_teleop/common/send2real_robot_f1_udp.py
[placo]: files/home/user/meaxiao/pico/vendor/xrobotoolkit/xrobotoolkit_teleop/simulation/placo_teleop_controller.py
[ik]: files/home/user/meaxiao/pico/vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller_ik_mixin.py
[base]: ../20260914_action_mapping/files/home/user/meaxiao/pico/vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller.py
[bridge]: ../20260914_action_mapping/files/home/user/meaxiao/pico/vendor/f1_bridge/send2real_robot_f1_udp2topic.py
[launch]: ../20260914_action_mapping/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/launch/motor_ctl.launch.xml
