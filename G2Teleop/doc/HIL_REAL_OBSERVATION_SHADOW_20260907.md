# 真实采集与 policy HIL Shadow（2026-09-07）

## 最新现场结果与真机入口

后续用户运行了 `g2_powered_f710_20260907_152634.jsonl`，反馈 START 再按无切换。完整遥测已拉到 `logs/offline/20260907_powered_review/`：10519 条，10395 条 control sample 全部为 `hil_state=standalone_human`；`hil_toggle_id` 从 0 推进到 9，说明按键计数已到达 G2。12 号代码仅记录 START 字段，没有调用 HILSupervisor，因此并非 HIL 退出卡住，而是根本没有 policy 切换执行路径。运行由 operator_ctrl_c 结束，summary closed、drop_count_total=0、writer_error=null，无 runtime_fault 事件。日志 endpoint 字符串仍写7904，不能仅凭该字段判断实际 CLI 监听端口。此轮只能证明人工遥操路径，不能升级为 Powered HIL 验收；不应再要求用户用 12 验证 START 切换。

用户已完成 `run_20260907_152311_1891023`，并明确要求停止继续安排 Shadow，提供真机控制。
读取完整 runtime 日志得到：1654 条事件，1560 个输入包，34 次真实 policy 请求与返回，34 个结果 `accepted=true, execution=none`；一次 POLICY→HIL→HANDOFF_HOLD→fresh POLICY。末尾断开原因 `operator_disconnected`，最终 completed。未做视频/34D 全量训练审计，不是 Powered 验收。
本地事件证据：`logs/offline/20260907_real_hil_run/runtime.jsonl`。

已有人工 Powered F710 可复用当前候选 Windows 7906 tunnel/sender：结束 39 与其他 Robot owner 后运行 `bash scripts/12_g2_powered_f710.sh --port 7906`，按 ENTER。
本轮在 G2 对该 Python 入口运行 `--preflight-files-only --accept-dm05-hand-fit --port 7906` 成功，未导入/初始化 GDK；没有自动启动真机。
真实控制遥测在 `/home/agi/xza/DM0.5/logs/telemetry/powered/g2_powered_f710_*.jsonl`。
**12 是已有人工遥操，START 不切 policy；不可称为 Powered HIL 或 DAgger。** 用户所需 policy 自动执行与人工接管同 owner 的 Powered HIL 仍待实现，不应再以 Shadow 或旧 F710 冒充该阶段完成。

## 当前结论

用户明确决定跳过上一轮网络 Shadow 剩余现场专项测试，直接推进下一阶段。
RT、退出互锁、迟到 response 的现场覆盖仍未补齐，不视为通过；不要求先重跑 37。

39 候选入口已实现并同步到 G2：真实三路相机、原始 34D reported state、真实 WebSocket policy，**零动作输出**。
本轮只做离线与无 GDK 检查，没有启动相机、Robot 或真实推理；现场采集与模型响应待用户启动。
冻结 F710 与已有双侧 17D Servo 支线保持各自语义，Powered HIL 尚未接入。

## 实现与边界

- `real_observation_source.py` 复用 G2 已有 camera/state adapter；每路消息只取一次，保留原始 JPEG/PNG、`timestamp_ns`、host 接收时间、状态采样时间。尺寸缺省时从同一消息解码取得。
- 原始时间戳单位 ns，域记为 `gdk_device_unverified`，不能与 host monotonic 直接相减。重复时间戳保留重复 frame ID，供审计发现，不伪造新帧。
- `hil_real_shadow.py` 一条持续采集线程供 recorder 与 policy 共享观测，一条单请求推理线程；HIL 或推理阻塞期间仍采集。默认采集目标 10Hz，最短请求间隔 0.7 秒，**30Hz 仍只是 action waypoint 合同**。
- 退出 HIL 要求新 epoch、handoff 后开始采集、各相机时间戳相对交接锚点推进，才绑定新请求；旧 response 不恢复 POLICY。未完成设备时钟映射，因此不能据此声称物理曝光必在 handoff 后。
- `inference-only / hil-record / prebuffer-record` 共用这一候选主链，区别为 payload sink。默认预录 4 秒，ring 上限 128MiB / 180 条；容量先到时不足 4 秒。START 介入触发提交，之后持续写入。异步 writer 有界、溢出显式失败。
- 只创建一个 Robot 并交给严格状态读取器，不调用动作 API；没有人工 IK、selected/提交目标/SDK 反馈/执行标签。真实 policy chunk 仅为推理证据。
- 手部原值保留，20D validity 为 false，连续反馈语义未确认。任一手全零时明确拒绝 policy 输入，不补目标、不伪装反馈；非零也不自动升级为可信连续测量。
- journal 记录观测、原始 policy chunk 与事件。此阶段没有 control rows，完整 episode 审计的 control 非空门不会通过；预录前 journal 引用也未必有持久化 payload。三种模式均不得标为可训练。
- 这是采集接入诊断入口，不是对生产共享内存/进程隔离调度实现的替换；未证明线程方案满足 Powered 控制时延。

## 本轮验证证据

- 本地：`logs/offline/20260907_real_hil_shadow/pytest.txt`，**143 passed / 6 skipped**。跳过项缺少 Ruckig/Pinocchio。
- G2：`logs/offline/20260907_real_hil_shadow/g2_validation.txt`，39 入口 imports/preflight 成功，真实采集适配与网络 Shadow 单元测试 **9 passed**；均未初始化 GDK。
- 新测试覆盖单消息原始时间戳、重复帧 ID、全零手值保留与拒绝输入、阻塞推理/HIL 中持续采集及旧 response 拒收、采集错误退出。
- G2 访问 `http://10.130.153.114:7892/process_frame` 返回 **426**，只证明 WebSocket listener 可达，不证明模型推理成功。
- shell 入口显式传 `/home/agi/app` 给 `env.sh`，避免运行参数被误作 SDK 路径。

## 用户启动

结束 37 接收端和其他 Robot/Powered owner。Windows 沿用候选版两个 CMD（7906 tunnel 与 sender）。
G2 终端执行：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/39_g2_real_observation_hil_shadow.sh
```

按 ENTER 初始化只读采集。默认 120 秒自动结束，也可 Ctrl-C。
关注 `real_policy_result` 的 `accepted=true`；可按 START 进入 HIL，稍后再按 START 退出，验证真实观测下暂停/恢复。
本阶段 RB、RT、Home 都不会驱动机器人，不能据此判断遥操故障。

输出：`/home/agi/xza/G2Teleop/logs/real_hil_shadow/run_*/`，内含 `runtime.jsonl`、`journal/`、`payload/`。
异常可能留下 `.partial`，不能当完整数据。运行结束后直接读取 G2 日志，不要求用户复制。

## 随后工作

读取这一轮真实采集日志，确认三路帧/state、实际 policy 返回及暂停录制行为；再接同一 owner 的 Powered POLICY/HIL 执行交接。
最终 DAgger 还需要真实人工纠正执行证据、严格视频/时间戳/34D 标签审计、数据聚合和至少一轮训练及回归评估；当前不能称 DAgger 已完成。
