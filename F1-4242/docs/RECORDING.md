> 2026-10-13：独立上肢录制操作入口已从数据页移到 Pico 页；数据页用于文件、回放与审核。下文旧“数据页录制”指历史位置，接口合同不变。

> 2026-10-12：新统一录制入口已部署，30Hz原始JPEG落盘及官方v3离线出口见 [UNIFIED_DATA](UNIFIED_DATA.md)，现场状态见 [STATUS](STATUS.md)。本文以下旧内联编码/10fps配置描述保留为历史合同依据，勿覆盖现场runtime/recording.json。

---
updated: 2026-09-23
status: implemented_pending_onsite_recording_validation
contract: f1_upper_16d_v1
upstream: meaxiao Pico 1.4.4 selected recording modules
---

# 独立 LeRobot 上肢数采

用户已同意先采用 meaxiao 的 LeRobot 流程。新版为 **meaxiao 1.4.4 录制实现的独立 16D 上肢适配**，
不是未经修改的原始 27D 版本。既有 LXL 录制、启动脚本、输出目录和上传配置保持原样。

数据页的独立录制入口先准备本任务的订阅服务，服务默认空闲；条件通过后由用户明确开始录制。
升级代码不会自动替换已经运行的录制进程；只有旧 worker 空闲且没有待确认片段时才可结束并升级，正在录制/收尾时保留当前版本。
结束进入待确认；可以查看本条三路视频，再明确保存。网页刷新/断开不自动开始、结束、丢弃或提交。
命令走本任务 Unix socket，不发送原 Pico 录制 UDP，不启动 Pico 或机器人动作。

## 操作与实际边界

1. 在现场按既有脚本开启相机、驱动；Pico 需先 `cd /home/user/Bruno/F1-infra/current/integrations/pico_upper` 再 `./start.sh`。现有脚本不会替调用者切换 cwd，来源证明会拒绝从其他目录启动的 supervisor；4242 不替你启动这些进程。
2. 数据页输入控制码登录即可操作录制，不需要抢占 Pico 的动作控制权。
3. 第一次开始操作先准备独立录制进程，等待相机、反馈、真实控制命令满足就绪条件。
4. 就绪后明确开始。录制中显示帧数和时长；结束后处理视频并进入待确认。
5. 三路视频可检查后保存。正常提交为 LeRobot v3；完整性不满足的观测保留在 recovery，不混进已提交训练包。

**现场已观测头相机约 30Hz、右腕约 10Hz、左腕未出帧。左腕恢复前不能开始三路录制。**
开始按钮不能把零帧相机或缺失控制目标伪装成就绪；此版没有任何实机采集验收声明。

本机独立配置使用 10fps，匹配当前最低相机率；不修改原驱动相机设置。
三路 source skew 仍为 50ms；单路源帧最大间隔 150ms（10Hz 周期的 1.5 倍），
一个完整 10Hz 帧缺失形成的约 200ms 间隔仍不通过。开录前每路需至少 0.8 秒有效观测、
平均频率至少 9.5Hz、源戳严格递增、没有超限间隔。单帧不能通过就绪判断。

这保证的是 **10Hz 采样行与相应源覆盖**；全局 150ms 不能证明原头相机 30Hz 的每个物理帧均无缺失。
上游的 surplus frame archive 仍保存未选入同步行的到达帧。更严格的每相机全帧合同需另行验收。

## 唯一的 16D 字段合同

| 位置 | 字段 | state | action | 单位 |
|---|---|---|---|---|
| 0–6 | arm_l_j1…arm_l_j7 | `/hal/joint_states` 具名反馈 | `/motion_ctl/joint_ctl` 具名真实目标 | 度 |
| 7–13 | arm_r_j1…arm_r_j7 | 同上 | 同上 | 度 |
| 14 | left_gripper_position | 左夹爪具名反馈 | `/motion_ctl/gripper/left` 的真实绝对位置命令 | 0–100 |
| 15 | right_gripper_position | 右夹爪具名反馈 | `/motion_ctl/gripper/right` 的真实绝对位置命令 | 0–100 |

不订阅 `/lead/joint_states` 作为 action，不采头、身体或底盘为动作维度。
**绝不以 state 回填 action。** 首次没有实测过任一必要动作来源时禁止开始；
录中双臂动作缺失或因果年龄超过 50ms 时隔离保留，不能拼一条看似完整的监督。

夹爪不同：Pico 在释放单侧 Grip 后停止发送该夹爪目标，但绝对位置目标仍保持。
因此仅在 **同一可识别 DDS writer epoch** 中，允许保持最后一条已观察到的绝对目标。
每一侧首次仍必须实际收到命令；不能拿反馈初始化目标。没有任意固定超时后以反馈“续命”的路径。

实机只读验证发现该 Jazzy Python callback 的 MessageInfo 是 dict，只有源/接收时间等字段，**没有逐消息 publisher_gid**。
因此当前 profile 明确限制为已经验证的独立 Pico 上肢会话，`source_basis=verified_pico_session_and_graph`；
不是逐消息 GID 证明，也不接受 4242 手动调姿或其他遥操作为此录制器的动作来源。

- 每 0.2 秒核对独立 Pico supervisor 的 PID/starttime/cmdline/cwd、两个直属 bridge/teleop 子进程身份，以及同共享锁 inode 的持有 FD。session 文件与锁 owner 必须一致。
- 同时核对夹爪和双臂 topic 的唯一 graph endpoint GID 与独立 Pico 节点名；整个进程/锁/图身份持续稳定 1 秒才建立来源 epoch。名称不是单独的身份证明。
- 本机 DDS `received_timestamp` 已只读验证为与 ROS/系统时钟同域的纳秒整数。早于当前 epoch、未来、非正值或排队超过 50ms 的消息拒绝；这是消息准入延迟界限，不是夹爪 hold 目标的有效期。
- 会话/子进程重启、锁不符、writer 消失/切换/歧义、消息接收时间不属于当前 epoch 或 ROS 时钟倒退会清空来源历史。
- 正在录制时来源变化置为持久故障，之后即使立刻收到一条好消息，也必须隔离本条，不跨 epoch 拼接。
- writer 观测超过 0.5 秒未更新不继续有效采样。同 GID 消失后再出现仍是新 epoch，需要新命令。

## 时钟与逐维来源

相机使用真实非零 header source 时间，不以接收时间补造曝光时间；未来或超出当前 ROS 接收窗口的 header 被拒绝。
状态历史使用本机 ROS 回调接收时间，命令历史使用已验证的 DDS received_timestamp，以不晚于所选相机 reference 的样本作因果选择。
这保留了上游的因果历史逻辑；它不是外部校时或光学曝光时延已经独立标定的声明。

Parquet 增加以下可回读字段：

- `source.action_timestamp_ns`：16 维目标的实际 ROS 接收时间。
- `source.action_age_ns`：该行 reference 减去每维实际来源时间。
- `source.action_held`：双臂始终 false；夹爪真实来源年龄超过 50ms 标 true。
- `source.gripper_writer_gid`、`source.gripper_epoch`：在本 profile 中保存左右目标对应的 **observed graph endpoint GID** 与 epoch，不是消息载荷自带的 packet GID。

`source.action_max_age_ns` 在本合同只约束双臂的新鲜指令；夹爪可能有长 hold，须读逐维年龄与 held 字段。
原始样本 journal 和数组也保留这些字段。每个 capture 记录进程 session ID、machine ID、机器 profile SHA、Pico 会话身份签名与 source_basis。LeRobot info 也写 source_identity_basis，已有数据集不能混换另一种来源证明。
`training_accepted:false` 明确表示尚未通过任务监督/训练发布验收；视频完整并不等于语义已验收。

## 目录、服务与依赖

```text
/home/user/Bruno/F1-infra/runtime/
├── recording-venv/              # 私有 pyarrow 增量环境，读取已有 Python/numpy/ROS
├── recorder/
│   ├── control.sock             # 0600，仅本地 HTTP 后端传显式录制命令
│   ├── worker.lock / worker.log
│   ├── status.json / catalog.json
│   └── previews/<episode-id>.json # 下采样状态/目标曲线
└── recordings/upper16_v1/
    ├── .staging/                # 当前或未完成录制
    ├── .transactions/           # 固定 capture→episode 的提交事务
    ├── data/ videos/ meta/      # 上游 LeRobot v3 组织
    ├── source/                  # 已提交 capture 的原始视频、数组、样本 journal
    ├── alignment/               # 同步选帧之外的源图及索引
    ├── recovery/                # 不完整/失败观测保留，不进入正式训练包
    └── discarded/               # 显式丢弃仅隔离保留，不删除原片段
```

保留 source 与已提交 videos 意味着部分视频有两份，还可能有同步剩余源图。
`size_bytes` 是训练包文件大小；`total_disk_bytes` 另计 source/alignment 实际占用，不能用前者估算总磁盘成本。

私有环境：既有 pico4ultra_py312 Python 3.12 / numpy 1.26.0 加本任务 `pyarrow==20.0.0`，
复用 ROS Jazzy 消息和 `/usr/bin/ffmpeg`、`ffprobe`。不改共享 LXL conda，不安装另一套控制驱动。
只检查依赖、不创建 ROS 节点或采集：

```bash
cd /home/user/Bruno/F1-infra/current
F1_CONSOLE_RUNTIME=/home/user/Bruno/F1-infra/runtime bash recording/run.sh --check
```

## 复用和最小修正

[上游来源清单](../recording/UPSTREAM.json) 保存原路径/原始 SHA 与适配后 SHA；
[代码差异](../recording/LOCAL_CHANGES.patch) 保留逐文件改动。原样参考的历史位置是 `F1/reference/20260923_pico_comparison`（不随源码包提供，见 [来源索引](history/SOURCES.md)）。
复用选定的 `EpisodeCapture`、CameraSyncGate、SampleHistory、alignment archive 和 LeRobot writer；
未带入原 collection 配对、SSH 传输、底盘/身体或 Pico 动作进程。

补了上游事务缺口：提交 journal 固定 capture 的 episode 槽位；stats/info 等中途失败后重试不重复入库。
全部数组/三路文件/覆盖验证先通过才建立写入事务；task/fps/字段改变不得重用相同 capture；
成功标记后源归档失败的重试仍会补归档。缺相机文件的校验失败不会留下阻塞后续录制的 pending 事务。

## 验证和待验收

纯软件验证使用合成 JPEG：三路真实 ffmpeg 编码、停止回读、16D Parquet 写入、hold 来源字段和重复保存幂等。
独立测试还注入 stats/info 写失败、缺视频、任务替换、源归档失败，以及 DDS epoch/因果选择反例。
这些测试不连接 ROS，不发布动作，不是现场数采。

这个进程/共享锁/唯一图的证明适用于受控独立 Pico 会话，不提供面对恶意同名 DDS 节点的密码学归属。

待现场：左腕出帧、独立 Pico 会话实际 writer 连续性、单臂 Grip 交替、三路视频源戳匹配、
一条完整短任务从开始→停止→预览→保存→正式 reader 回读，以及完整任务 action/state 语义验收。
