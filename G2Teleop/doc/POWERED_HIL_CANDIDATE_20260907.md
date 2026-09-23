# Powered 同步 POLICY / F710 HIL 候选（2026-09-07）

## 当前状态

现场启动 `g2_powered_hil_20260907_154850`：已完成初始化、动态17D名称读取和IK预热；只读 `ss` 确认 PID2020403 监听127.0.0.1:7906，Recv-Q为0，console尚无Windows连接或policy请求记录。Windows sender反复timeout，因此目前不能说真实自动推理已发生；需先恢复Windows SSH转发。尚未获得Windows tunnel输出，不能断言具体是旧隧道、端口占用还是SSH连接问题。可用新本地7907→G2的7906转发隔离旧本地监听，并用sender的--port 7907连接；G2入口仍用40/7906。连接恢复后将自动请求policy并产生真实动作，不是无输出网络探针。

用户要求下一次直接运行完整真实场景，不再安排 Shadow 或纯 F710 替代测试。
新增 `scripts/40_g2_powered_hil.sh` / `.py`，具备真实 policy 执行与 START 人工交接路径。
**这是首轮 Powered HIL 候选，尚未真机运行验收。** 已有的 F710 与动态双侧 17D 各自真机结果不等于该交接已验收。
冻结入口 12 及其控制实现未修改；40 从现有 F710 入口派生，增加候选调度和录制，后续应避免两份实现长期漂移。

先前真实观测 Shadow：34 次真实推理、一次退出后 fresh 恢复；随后 12 号真机日志 START 计数0→9，但全部 standalone_human。不能把这些当作40验收。

## 用户实际启动

结束其他接收端/Robot owner，Windows 沿用现有两个7906候选窗口：

```powershell
cd $HOME\G2Teleop\hil_shadow_candidate
.\scripts\windows_hil_shadow_tunnel.cmd
```

另一窗口：

```powershell
cd $HOME\G2Teleop\hil_shadow_candidate
.\scripts\windows_hil_shadow_sender.cmd
```

G2：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/40_g2_powered_hil.sh
```

文件预检通过后按 ENTER，倒计时3秒。连接 Windows 并取得新观测/真实 policy 后，**机器人会自动执行 policy**，无需先按 RB。
默认120秒，Ctrl-C结束；物理急停由现场人员掌握。不得与12、19或其他Robot owner并行运行。

本轮场景：

1. 观察 `powered_policy_result accepted=True` 与 policy 实际动作。
2. 按 START 进入 `hil_latched`，policy 未执行后缀作废；十字键选侧并释放，RB控臂，RT控手。松RB仍在HIL，A和Y沿用人工语义。
3. 松RB、等待手部渐进目标生成完成，再按 START，看到 `hil_to_handoff_hold`，然后 `fresh_policy_to_policy`，机器人恢复真实 policy。
4. 可重复一次。不要把按住RB或手部目标尚在推进时的 `hil_exit_rejected` 当作START失效；拒绝理由会打印。

进入HIL时旧RT状态被忽略，需新的RT变化才产生手部意图，避免policy手型被旧按钮状态立即拉回拟合模板。
网络断开/超时会SAFE_HOLD，不自动恢复policy；连接恢复后START先进入人工，再按START请求新policy。
Windows ACK协议仍用已有枚举，`reason=powered_hil:...`显示接收端状态，accepted仍代表传输接收，不能据其推断物理执行成功。

## 执行与记录合同

- 一个 `gdk.Robot()`、一个 controller、一个100Hz控制线程；`RecordedRobot`串行化该Robot的SDK访问，保留实际调用起止/返回/异常。没有额外手部后台发送线程。
- POLICY用已有动态名称左右各17D normal Servo；HIL用原F710双臂normal Servo与约5Hz changed-only O10点目标。切换只由该控制线程改变候选controller输出布局，不新建Robot/controller，不用low-latency。
- 进入HIL先用上次实际提交的20D目标提交一次手部接管点目标，重建人工手状态；该锚点明确是command，不是measured。退出HIL切为双侧17D HOLD，同一目标等待新观测和推理后再执行。
- SDK是否完全按预期衔接旧点目标与17D流仍待本次真机验证。手部退出互锁检查**渐进命令生成完成**，SDK成功返回不等于物理到位；连续手反馈未验真，不能宣称实测到位门已完成。
- 同步执行：一个推理请求返回50×34，执行完该chunk再基于随后开始的新观测请求下一次。执行期间不预请求，HIL期间不发新请求；已在途请求可返回但旧epoch不可执行。推理等待期间保持上次提交目标。
- 30Hz为waypoint合同，100Hz为Servo采样；本候选线性采样，前置1/30秒“上次提交目标→首waypoint”衔接段，再执行原49个间隔，总50/30秒。不是RTC，不宣称等同生产支线的所有插值参数。
- 所有模式共用该主链，默认 `prebuffer-record`：同源10Hz目标采集、4秒有界预录、START触发持续落盘；HIL和推理阻塞时仍采集。容量不足可能缩短预录窗口，计数保留。
- `policy`、`hold`和人工控制都有journal control rows；实际SDK调用另记事件。selected与clip后submitted分开，SDK按实际分组/调用时刻记录。34D submitted是组合目标，各维本tick是否实际发送以sdk_calls为准。
- 控制行 `executed_command_34d` / `measured_34d` 保持null；独立观测流保留原始reported34D与采样时间，手20D validity=false。严禁将异步reported值或成功返回伪装为逐tick实测执行。
- 原始三路设备timestamp_ns及host monotonic保留，设备时钟域仍未映射；fresh要求新epoch、handoff后开始采集、逐路原始时间戳推进，不能证明曝光时刻一定在handoff后。
- 采集/录制队列拒绝、writer异常停止候选运行；旧response拒收和部分SDK失败都保留证据。视频封装、三流时间/34D标签严格审计未完成，始终 `training_ready=false`。
- 线程采集可能争用Python/GDK；本轮必须从实际日志评估控制时延，不宣称已达到生产共享内存进程隔离方案性能。

## 离线与G2文件验证

`logs/offline/20260907_powered_hil/`：

- `pytest.txt`：本轮全量 **149 passed / 6 skipped**（缺Ruckig/Pinocchio）；不是此前历史结果。
- 最后补充异步writer错误测试后，`final_targeted.txt`：**11 passed**，覆盖Powered策略与共享采集模块。全量149计数不包含这最后新增的一项。
- G2 `g2_validation.txt`：40 `--preflight-files-only --accept-dm05-hand-fit`成功，未导入/初始化GDK；此前对应16项无硬件测试通过。最终同步后 `g2_final_validation.txt`：同一files-only入口再次通过，**17 passed**，shell语法通过；未自动启动真机。
- 新测试覆盖同步单chunk、锚点/30Hz插值、不外推、START丢后缀、RB/手意图退出互锁、fresh请求因果、迟到响应、HIL中持续采集、watchdog不自动恢复、录制溢出/异步错误、实际SDK失败保留与单controller布局切换。
- 核对GDK 2.6.3官方 [Robot原文](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/reference/Python/robot.md)：normal Servo、100Hz、JointServoControlReq及move_ee_pos字段均沿用现有实现；官方没有据此保证上述两种执行语义交接已经验收。

## 日志与下一轮判据

- console：`/home/agi/xza/G2Teleop/logs/powered_hil_console.log`
- 遥测：`/home/agi/xza/DM0.5/logs/telemetry/powered/g2_powered_hil_*.jsonl`
- episode：`/home/agi/xza/G2Teleop/logs/powered_hil/g2_powered_hil_*/`，journal与payload。

用户启动后读取上述真实日志，核对policy SDK输出、START后无旧policy执行、人工动作、退出新观测恢复、Servo时延和三路录制持续性。此次若通过，才是Powered HIL场景验证；DAgger仍需可训练数据审计、聚合、训练和回归评估。
