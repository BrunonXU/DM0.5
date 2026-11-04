# 双臂试用适配与验证边界

2026-09-23。原工程：`/home/user/F1/PICO/pico` v1.4.4。用户确认原作者同意复用。本目录保存所需源码与模型，不修改原工程、不复制原账号凭据/venv/录制数据。第三方与原作者署名保留在源码；此文不作额外许可证授权。

## 组件边界

```text
已有PCService/ARM SDK → upstream XR/精调/IK/碰撞/独立Grip
                                      ↓ approved 18D
                            loopback UDP 127.0.0.1:42428
                                      ↓
                        upstream ApprovedArmOutput 250Hz
                                      ↓
                 仅 arm_l_j1..7 + arm_r_j1..7 JointState

夹爪：teleop → topic白名单 → 对应Grip接管侧 GripperCommand
全身反馈：仅供姿态模型/碰撞，无身体或底盘命令通道
```

新的 `pico_upper` 拥有进程生命周期、互斥锁、只读预检、输出合同和部署配置。上游 `pico_f1` 保持原模块边界，避免散抄算法。上游身体/按键类部分保留为构造和配置类型依赖，但创建publisher/线程、更新与输出路径已显式移除/禁用；不能通过改JSON重新启用。

## 有意修改的行为

1. 删除上游 `start.sh` 集成链路：本包不复制/调用录制、collection路由、传输、gamepad管理、底盘模式和零速脚本。
2. teleop使用 `GuardedNode`。不创建底盘/身体发布者和worker，不订阅外部TCP回放控制或recording状态。A/B/X/Y不起动作；XY消费端也禁用。
3. 移除旧 `f1_init_api`/UDP桥。独立桥拒绝长度1/2/14/16的所有旧协议包，仅接收本地18D有限数值、夹爪范围和布尔使能。所有 `/motion_ctl/joint_ctl` 发布必须精确等于左7+右7命名列表。
4. `GuardedNode` 拒绝其他发布topic和全部service client，阻止底盘/模式/腰升降绕行。退出只停自己的进程，不补发零速或复位。
5. 双臂/夹爪命令与4242共享 `/home/user/Bruno/F1/runtime/control.lock`。旧程序不遵守此锁，所以另查旧lxl进程与ROS命令发布者；运行时桥持续检查外部发布源，出现冲突则整会话退出。无法原子阻止非合作旧程序在检查后抢发，因此现场仍须先结束旧遥操。
6. 新增collision模型初始化失败的显式拒绝；使用现有 `feedback_timeout_s` 检查逐关节反馈新鲜度，避免缓存某个已消失关节仍算完整反馈。没有放宽碰撞或轨迹阈值。
7. 夹爪仅在对应臂Grip接管时发布，避免接管单臂却更新另一夹爪。扳机方向沿用原源码。
8. 模型缓存写入 `runtime/model_cache/<release_hash>`，日志/会话也在runtime。源码不写运行输出。

来源清单 `UPSTREAM.json` 每项含原路径、原SHA/大小、捕获时间，以及本地适配后的SHA和修改标记。`LOCAL_CHANGES.patch` 保存文本适配差异。`RELEASE_FILES.json` 覆盖当前源文件/资产哈希（不含它自己和运行产物），便于远端校验。

## 测试

`tests/test_upper_boundary.py` 验证网络包、topic/关节白名单、禁止配置、无非上肢worker、无复位/录制按钮路径及collision初始化拒绝。所选上游测试覆盖独立Grip、精调、在线轨迹、最终臂输出和XR断流/恢复。没有接入真实控制器发送命令。

6000D系统Python有numpy/pytest，未装ruckig；首次执行23项通过、1项因缺少ruckig失败；随后补齐XR恢复、实际发布包装和停止身份验证，6000D可执行范围最终37项通过、1项精调轨迹用例因ruckig依赖未执行。使用F1既有兼容环境或自有独立环境完成全套纯测试，不能把缺依赖测试记成算法失败或通过。最终实际结果由部署记录补充。

## 与数采的关系

此包不碰旧lxl文件，但也不会同时运行其 `all_remote_lxl.py`，因为该入口同时包含旧控制桥和录制。因此“旧数采逻辑不改”不等于“新遥操自动调用旧MCAP录制”。先验证操作手感；未来确定目标位置后，把录制作为独立服务接入统一状态/控制事件，明确原始动作与最终执行目标。

这次没有导入 meaxiao 的 `lerobot_recorder.py`、`transfer.py`、`collection.py` 或dashboard。不会自动创建场景、配对服务器、上传episode或修改原数据目录。
