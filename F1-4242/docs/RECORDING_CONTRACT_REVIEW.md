---
date: 2026-09-23
status: local_contract_and_transaction_checks_passed
scope: meaxiao_1.4.4_upper16_branch
---

# 上肢录制分支的独立合同审查

本次只复用已保存的自有 meaxiao 1.4.4 参考快照，审查新的 `recording/` 分支；未运行原目录程序、未更改 lxl 数采、未启动实机录制或机器人动作。源自 `F1/reference/20260923_pico_comparison/files/meaxiao/` 的 `lerobot_recorder.py`、`lerobot_v3.py`、`sample_history.py`、`camera_sync.py`、`alignment_archive.py` 已逐项阅读。

## 必须保持的合同

| 项目 | 合同 |
|---|---|
| 16D 顺序 | 左臂 `arm_l_j1..7`、右臂 `arm_r_j1..7`、`left_gripper_position`、`right_gripper_position` |
| 单位 | 双臂绝对硬件角度，度；OmniPicker 绝对位置百分比，0 张开 / 100 闭合 |
| state 来源 | `/hal/joint_states` 的具名臂轴及左右夹爪反馈；不把 `hand_*` 占位字段当夹爪 |
| action 来源 | 实际 `/motion_ctl/joint_ctl` 和左右 `/motion_ctl/gripper/{left,right}`；不混入 `/lead/joint_states`，不把反馈当目标 |
| 夹爪 hold | 必须先在已验证的本地 Pico 会话及唯一 graph writer / epoch 中看到真实命令；保持它的原值、DDS 接收时间及 age。不能刷新旧目标时间，也不能用当前夹爪反馈初始化 |
| 来源变化 | 进程会话变化、writer 消失 / 改变 / 多源、graph GID 缺失、时钟回退等清除旧 hold；正在录制时隔离该条，避免跨来源边界无声拼接 |
| 对齐 | 使用实际相机源时间，选择不晚于参考时刻的 state/action。保留原始相机时间、接收时间、每维动作来源时间 / age / held、夹爪 graph GID / epoch |
| 缺流 | 缺任一路真实相机、缺真实臂目标或首条夹爪命令时阻止开录；录制中故障保留恢复材料，不补零、不复制旧图冒充新帧 |
| 生命周期 | `idle → recording → finalizing → review → 保存/丢弃`；网页断连不等于停止或丢弃，保存明确确认后入库 |
| 隔离 | 独立进程、锁、状态和数据根目录；不接管原 Pico 录制 UDP 端口，不自动上传或启动运动 |

夹爪 hold 是必要的语义适配：现有上肢 Pico 在 `base_teleop_controller_ros2_mixin.py` 中仅在该侧 Grip 接管时发布夹爪目标；松开 Grip 后目标保持，但不继续发布夹爪命令。若沿用上游 50 ms 的动作统一超龄判定，常见单臂交替接管就会中断录制。新的 hold 必须有来源证明，而不是放宽所有动作的时效。

## 上游发现与分支要求

1. 上游 `_ready_snapshot` 的最新值路径和因果历史路径均存在 action 缺失 → state 回填；同时监听主端和最终目标话题。新分支删除两处回填，并限定最终目标来源。
2. 上游相机零时间戳用本机 now 替代，再把替代值记作 integrity 时间。新分支拒绝零、未来和超出本机时钟窗口的 header，不能称“修复”为原始时间。
3. 上游夹爪反馈取 `position[0]`，新分支改为具名读取并验证唯一、有限和 `[0,100]`。
4. 上游 writer 逐文件 rename，并非完整 episode 的原子事务。`meta/episodes` 成功而 `stats/info` 失败后的保存重试会重复入库；新分支须使用 capture ID / 固定槽位 / 幂等 journal。
5. 上游 dataset 内任务文本未做一致性校验，所有行又使用 `task_index=0`；新任务不能覆盖旧 `tasks.parquet` 的含义。
6. 新增 journal 的校验失败路径也需检查：不允许失败的新 pending 永久阻断后续录制；签名必须覆盖 task/fps，提交后的重试须补完成源材料归档。
7. 多路编码器部分创建失败应关闭已经创建的子进程。录制失败只隔离自身 capture，不能停止相机驱动或旧数采。

## 当前独立验证

```bash
PYTHONPATH=/tmp/f1-recording-testdeps:$PWD python3 -m pytest -q \
  tests/test_recording_contract_review.py tests/test_source_session_review.py \
  tests/test_gateway_review.py tests/test_recording_gateway.py tests/test_data_auth.py
```

本审查维护的合同 **42 项通过**：16D 精确顺序、因果历史、禁止缺失填补、夹爪命令 hold 原始 age、未来命令排除、来源切换 / 消失 / 歧义 / 零 GID / 回退、同 GID 重现后的 epoch、左右隔离、夹爪反馈具名读取与异常拒绝、相机异常源时间拒绝。

其中回调测试提取实际方法体并注入假的时钟和消息，不初始化 ROS。新增故障注入验证了 `stats/info` 失败后的保存幂等、任务文本不被替换、首次 info 尚未生成时的重试签名、源归档 rename 失败后的重试、视频缺失不留下 pending；来源 epoch 故障即使随后收到好命令也不能再产生新数据行。

F1-09 本次独立 profile 为 **10 fps**，匹配已观察到的头部约 30 Hz / 右腕约 10 Hz；左腕无帧时仍阻止开录。测试覆盖缺相机、只到一帧、5 Hz 低频和 200 ms 缺口均不能通过就绪判定。每路需要至少 0.8 秒源时间窗口，允许最大源帧间隔 150 ms；这保证目标 10 Hz 合同，不宣称原头相机 30 Hz 全帧无缺失。夹爪 hold 之外的臂动作与状态仍使用 50 ms 因果时效。

`recording/worker.py` 与 HTTP 服务的独立进程、文件锁、串行本地命令、连接断开不触发 discard 的路径已只读审查。此次独立测试包含实际 Parquet 事务，但使用预验证视频占位字节来隔离提交测试；**没有执行实机数采，也没有以该夹具证明完整 ffmpeg 编码或官方 LeRobot 训练 reader 兼容性**。这些仍由后续端到端验收分别确认。证据见 历史 `results/recording_contract_review.json`（不随包提供，见 [来源索引](history/SOURCES.md)）。


## Jazzy 来源证明边界

实机 Jazzy Python callback info 未暴露 `publisher_gid`，且 ROS graph endpoint GUID 不能直接当作每包 RMW GID。因此此分支采用明确限定的 `verified_pico_session_and_graph` 合同，不声称逐包 GID 归因，也不支持任意发布者或另一套 Pico 混入。

证明同时检查独立 Pico supervisor 与两个子进程的 PID、进程启动 tick、父子关系、`-m` 模块参数、部署目录，以及三者持有同一个已加锁的控制锁 inode；再检查三条动作话题各只有一个预期节点及非零 graph endpoint。身份稳定至少 1 秒后建立 epoch，每 0.2 秒刷新，超过 0.5 秒未复查不准入。新回调必须有当前 epoch 内、非未来、排队年龄不超过 50 ms 的整数 DDS 接收时间；已验证的夹爪 hold 可以继续保持原始较老的时间。来源变化或 ROS 时钟回退使本条录制故障锁存，后来的好命令不能解除它。

`capture.json` 保存进程证明、graph GID、会话签名、epoch 和来源基础；数据集 `meta/info.json` 保留 `source_identity_basis`，拒绝混入另一种归因合同。字段名中的 `gripper_writer_gid` 在此 profile 指已观察的 graph endpoint，不是包级发送者证明。该方法用于受控本地 Pico 会话，不是对抗伪装 DDS 发布者的身份认证。启动 Pico 需先进入部署目录再运行 `./start.sh`，否则 supervisor 工作目录检查会拒绝来源。

此证明新增 **37 项独立测试通过**，覆盖完整进程树、PID 重用、错误父进程、重复 / 缺失子进程、锁未持有 / 锁 inode 缺失、错误目录、模块参数伪装、session / lock 非对象 JSON、单源稳定期、身份改变、同 graph 重现、过期证明、排队旧命令和即时 ROS 时钟回退。测试使用临时 `/proc` 结构与真实临时文件锁，没有启动 Pico 或 ROS。

## 网关与媒体审查

新增 **4 项独立网关测试通过**：慢启动超时后再次准备不会创建第二个 worker，也不会顺带发录制 start；GET 状态 / 列表不启动 worker；录制 POST 必须登录且同源但不占用运动控制锁；媒体根目录父路径中的符号链接被拒绝。既有网关和数据认证的 **25 项回归通过**，覆盖状态 NaN / 超时 / 死进程不可 ready、首次启动只准备订阅、字节 Range / 416、旧原始资料与衍生视频路径隔离、坏分支 catalog 不影响 LXL、源文件变更使旧审核失效等。

本次合计 **108 项通过**，其中独立审查维护 83 项。生产文件已只读复核；只更新独立测试、本文和证据。文件系统 hash 记录的是本次实际受测版本。网页或 HTTP 服务退出不触发 recorder 停止 / 丢弃；录制仍由独立 worker 管理。尚未执行实际 F1 录制验收。
