# ROS adapter

`adapter.py` 独立于 HTTP 和页面。导入不加载 ROS；`RosAdapter.start()` 只创建 ROS 订阅和 DDS 图查询线程，不创建动作 publisher，不调用切换模式服务，不启动驱动、Pico、录制或上传。

```python
adapter = RosAdapter(profile)
adapter.start()
snapshot = adapter.snapshot()
frame = adapter.camera('head')  # (bytes, mime, monotonic_age_s) or None
result = adapter.execute(spec, cancel_event, on_progress)  # 阻塞，在动作线程调用
adapter.close()  # 应先取消并等待正在执行的动作
```

HTTP 层负责本地访问授权、控制租约、`F1_CONTROL_LOCK` 指定的共用锁、动作 ID/历史和操作日志。`execute` 再串行化动作，正常、取消、异常均销毁本次动作 publisher；空闲不会因发布者存在而阻止 Pico 取得控制权。

## 反馈合同

- `joints[name] = {position, age_s, source_stamp}`：HAL 原生角度为度，按关节独立保留接收时间，身体和手臂分包不会互相刷新年龄。
- `grippers.left/right` 同上：必须识别 `<side>_gripper_position`，0 打开、100 闭合。未知、缺失、非有限反馈保留为缺失。
- `base`：`vx/vy/wz`、四个控制模式标记、可选 `pose={x,y,yaw}`。位姿来源是 `move/State.odometry`，`orientation.w` 是标量 yaw，不做四元数解算。
- `battery`：`driver/BatteriesData` 的各电池字段。该可选消息包缺失时，其余订阅继续。
- `cameras.head/left_wrist/right_wrist`：压缩图像元信息，不把图像字节塞入 JSON。只接受 JPEG/PNG 魔数，字节通过 `camera()` 单独返回。
- 全部年龄来自本机 `time.monotonic()` 接收时间；原 ROS header 保留为证据，不跨时钟域计算新鲜度。
- `connected` 表示 ROS 订阅线程运行，**不表示所有驱动已就绪**。各流缺失/陈旧需独立显示。

## 显式动作

| `spec.kind` | 字段 | 到位 / 停止语义 |
|---|---|---|
| `joints` | `targets={具名关节:角度}`, `duration_s` | 扩展为所选侧 7 轴 / 身体 5 轴 / 头部 3 轴完整组，未指定成员取新鲜实测值。五次插补，结束后连续实测在容差内才返回 `succeeded`。 |
| `gripper` | `side=left/right`, `position=0..100` | 按 OmniPicker 语义；`max_effort=0`，不宣称提供真实力控制。连续新鲜反馈到位才成功。 |
| `base` | `vx,vy,wz`, `ttl_s<=0.3` | 已有手动模式和新鲜反馈才允许；不自动改变底盘模式。到期或取消发三次零速度，返回 `completed`，不等于实物停车验收。 |

`renew_base(spec)` 仅对尚未到期的本 adapter 底盘动作续期；同一浏览器租约的续期鉴权由 HTTP 层执行。续期不排队，过期不能让旧动作复活。初始 TTL 从 `execute` 收到请求计算，准备 publisher 的时间也计入 TTL。

默认所有动作关闭。启用须提供本机已核验的 `motion_enabled`、`control_contract_verified`，以及相应 `joints_enabled+limits_verified+joint_limits_deg` / `gripper_verified` / `base_verified`。仅复制其他机器的 profile 不构成本机核验。启动部署层应比对本机 machine-id 和对应的控制器证据。

当前轨迹规划由 `robot/motion_plan.py` 统一计算，固定速度/加速度，单次执行预算六秒；具体上限见本机 profile 和 [POSE](../docs/POSE.md)。这些是软件操作策略，**不是电机硬限位或精度承诺**。完整目标仍按本机 profile 限位拒绝越界，不剪裁。每次发命令前查询 DDS 控制 topic：存在外部 endpoint（包括 unknown）即拒绝，不把相同节点名当作自己。自身 publisher 依据创建前后独占图变化绑定 endpoint GID。

这不是碰撞规划器或硬件急停。关节取消仅在仍独占、反馈新鲜时发一次实测保持，然后停止自身输出；失去反馈或出现竞争时不编造保持目标。大幅预设与全身归位尚需独立的路径与实物验收。发命令、完成插补、反馈到位是不同结果；缺失反馈、超时和竞争返回错误。

## 验证

6000D 任意终端，从项目根目录：

```bash
python3 -m pytest -q tests/test_ros_adapter.py
```

离线测试覆盖分包/时钟域/无效反馈、完整手臂目标、陈旧反馈、限位、外部 GID、到位判据、取消保持、TTL 归零、过期拒绝续期、只读 gate。测试用假 transport，不导入 ROS，不接触机器人。真实 DDS 发现、网络稳定性、机械碰撞、力和运动效果不由这些离线测试证明。


## 启动合同核验

`runtime_contract.verify_runtime_contract(profile, timeout_s=6)` 返回报告，`ok=false` 时服务必须保留只读模式。它核验 `expected_machine_id`、同用户唯一运行 `motor_ctl_node` 的 `/proc/<pid>/exe` 哈希、`expected_motor_parameters` 所列已声明参数；不以 launch 文件猜测当前参数。

F1-09 的当前模式是 `arm_control_mode=3, arm_impedance_type=1`。本机 `_MarvinSDK.h` 明确关节位置目标同时用于位置/扭矩模式，匹配二进制的 JointState 回调直接进入此 API；无需切换到其他机器人采用的 mode 1。启动报告仍只是 ROS 配置参数快照，不是 SDK 实时模式或动作验收。

`execute()` 在每次耗时 DDS 检查后重新读反馈、取消和 TTL，在底盘锁内重取续期速度/截止时间。到位需要每个受控关节/夹爪的新接收序号跨越稳定时间；同一缓存值不会靠反复轮询“验证成功”。异常附 `commands_sent` 和 `hold_sent`；发布调用抛出异常时按可能已发送处理，HTTP 应记失败而非“未发送的拒绝”。

F1-09还在读取ROS参数前校验 `joint_contract`：22轴、双份命令限位、组序、模型映射、模型工件与默认预设必须匹配本机已记录基线。结果位于 `/api/system` 的 `contract.joint_configuration`。它是启动时配置核对，不是实物标定或动作验收；来源与未确认差异见 [全身配置](../docs/JOINT_CONFIGURATION.md)。
