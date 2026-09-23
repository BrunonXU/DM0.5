# G2现场脚本

所有脚本自动加载`/home/agi/app/env.sh`。Shadow/回退仍可连接7892 HTTP；正式Sync固定连接
GPU7上的`ws://10.130.153.114:7892/process_frame`并使用native JPEG/PNG binary WebSocket。
新产物只写入项目`logs/`分类目录。
可通过环境变量`POLICY_ENDPOINT`显式覆盖服务地址。

## 日常顺序

```text
00_preflight → check_hand_state → 01_shadow → 05_powered_sync
```

| 脚本 | 作用 | 是否发送动作 |
|---|---|---|
| `00_preflight.sh` | 端口、GDK、34D和三路相机 | 否 |
| `check_hand_state.sh` | O10双手原始状态 | 否 |
| `01_shadow.sh` | 真实观测与75K推理 | 否 |
| `03_reset_home.sh` | 唯一正式Home：双臂后双手 | 预览否；execute是 |
| `04_powered_guarded_smoke.sh` | 1/3/6个30Hz区间的严格安全门诊断 | 是 |
| `05_powered_sync.sh` | 当前正式75K Split同步推理 | 是 |
| `06_powered_async_experimental.sh` | Split异步实验 | 是 |
| `07_training_data_replay.sh` | 完整948帧训练示教双臂回放；绕过模型、禁用双手 | 预览否；execute是 |
| `08_rtc_shadow.sh` | RTC端到端时序/合同Shadow；不创建Servo | 否 |
| `09_powered_rtc.sh` | 完整Split RTC；默认有限60秒 | 是 |
| `10_powered_rtc_arms_only_10s.sh` | 固定10秒RTC双臂隔离；硬件层不发送双手 | 是 |
| `11_rtc_process_isolation_preflight.sh` | Control唯一Robot、Policy唯一Camera、34D state ring只读门 | 否 |
| `14_joint_feedback_rate_probe.sh` | 5/10/20Hz 34维反馈只读耗时门；不发送命令 | 否 |
| `15_powered_sync_arms_only_10s.sh` | 与正式Sync相同模型/手臂参数的10秒隔离；完全禁用双手输出 | 是 |
| `16_single_side_17d_servo_probe.sh` | 单侧7D手臂+动态10D O10 Servo；默认只读，execute固定左侧静态1秒 | 预览否；execute是 |
| `17_single_side_17d_micro_motion.sh` | 左食指PIP的+0.05rad minimum-jerk往返；默认只读 | 预览否；execute是 |
| `18_single_side_17d_visible_motion.sh` | 左食指PIP +0.20rad并在峰值/基线各保持1秒；默认只读 | 预览否；execute是 |
| `19_powered_sync_dual_side_17d.sh` | 完整模型34D经双侧动态17D normal Servo连续执行；支持时长/continuous | 是 |
| `update_motion_dashboard.sh` | 6000D拉新日志并增量生成静态科研图/网页 | 否 |
| `serve_motion_dashboard.sh` | 6000D在localhost提供已生成页面 | 否 |
| `stop_runtime.sh` | 向正式客户端发送SIGINT | 不新增动作 |

首次采集连续硬件反馈前，在G2保持静止并手动运行14；10/20Hz均未通过时，Powered自动保持
command-only。每次真机结束后回到6000D运行：

```text
./scripts/update_motion_dashboard.sh
./scripts/serve_motion_dashboard.sh --port 8765
```

页面按日期和时间倒序，重新生成manifest后浏览器每2秒自动刷新。

当前正式硬件下发固定为Split：模型仍输入/输出34D，但双臂前14D以100Hz发送给
`joint_servo_control`；正式Sync直接保留模型原始20D手部轨迹，以5Hz采样，并且只在
目标相对上次成功提交值发生变化时调用
`move_ee_pos(group=dual_tool,target_type=o10_t2)`。RTC入口仍保持独立旧配置；GPU3/7893
服务已运行新网络配置，但尚未用当前C2/进程隔离组合重新Shadow、标定d/s或带电验收；
Async保持5Hz。两部分
不会重新合成一个34D Servo请求。

`09_powered_rtc.sh`现默认使用`--rtc-process-isolation`。它在显示Powered授权前自动
调用11：Control按请求读取34D state并写共享ring，策略子进程完成三相机、ring读取、native
JPEG/PNG和7893 binary WebSocket且不创建Robot；全程不创建Servo、不调用任何机器人命令。只有该门通过后，09才进入人工`ok`和
5秒倒计时。Powered时控制进程仍发送完整14D双臂+20D双手，不是arms-only。

同步完整任务示例：

```text
./scripts/05_powered_sync.sh --duration-seconds 45
```

若要判断连续O10点目标是否导致手臂卡顿，只运行固定10秒的配对隔离入口：

```text
./scripts/15_powered_sync_arms_only_10s.sh
```

该入口仍请求同一75K Sync服务并接收完整34D模型输出，但硬件层只执行前14D；不会创建
O10线程，也不会调用`move_ee_pos`。除禁用双手外，双臂通道、chunk执行长度、boundary
blend、jerk正则、C2插值和近静止投影均与正式Sync一致。

双侧连续O10首次推理验收：

```text
./scripts/19_powered_sync_dual_side_17d.sh --duration-seconds 30
```

该入口与正式Sync一样默认60秒并支持`--duration-seconds N`或`--continuous`，模型仍输出50x34。
每个100Hz tick分别发送左/右两个动态名称17D Servo请求；
手部20D按模型30Hz时间线线性插值到100Hz，不调用`move_ee_pos`，不使用阈值、changed-only或
手部平滑器。首次验收使用normal碰撞保护通道；当前`05_powered_sync.sh`仍保持旧Split回退路径。

正式Sync入口默认固定使用双臂C2 cubic spline 30→100Hz插值、120ms boundary blend、
chunk内部jerk权重10、最终100Hz `stationary_minjerk_v1/0.0035rad`近静止反向投影和
双手raw 5Hz changed-only；无需重复传入这些参数。微颤投影只处理范围不超过约0.20°、
至少反向两次且净位移很小的双臂单轴，保持起终点；每次触发写入telemetry。手部不再经过
`shared_preview_v1`/QP重塑，日志名带`c2_spline_arm_micro_minjerk_v1_hand_raw5_changed_only`；
重复目标会记为`hand_command_skipped`而不调用GDK。RTC仍保持独立配置，不随本次Sync改造变化。

持续运行直到按`q`/`Ctrl+C`或发生控制故障：

```text
./scripts/05_powered_sync.sh --continuous
```

RTC服务器已在原正式GPU3/7893运行native JPEG/PNG binary WebSocket、五步compile和
RTC guidance。客户端08/09/10已统一双臂weight10+C2 spline和手部10Hz；09日志名带
`c2_spline_hand_rtc_cap_v2`。`H=50,d=16,s=20`仍是旧候选，必须先用08取得新链路真实
observation age后重标定。双臂/双手首跳参考分别为0.05/0.35rad。完整Split有限测试：

```bash
./scripts/09_powered_rtc.sh --duration-seconds 60
```

该入口同时发送14D双臂100Hz C2 Servo和20D双手10Hz `move_ee_pos`。当前手部仍为
`rtc_cap_v2`临时对照，不是Sync V1.1，也不是最终共享轨迹器。仍需专人握物理
急停；首次新参数测试只用有限时长，日志通过前不运行`--continuous`。

`08_rtc_shadow.sh`默认以旧候选`d=16,s=20`运行30请求，也支持显式单变量组合，例如
`./scripts/08_rtc_shadow.sh --requests 60 --d 9 --s 17`。日志名自动包含d/s；候选必须
先由7893真实observation age筛选，再做固定seed轨迹A/B，不能把Sync延迟直接代入。

省略`--duration-seconds`时默认60秒模型动作。异步只通过明确标为experimental的
入口运行：

```text
./scripts/06_powered_async_experimental.sh --duration-seconds 60
```

Async也显式使用双臂C2 cubic spline，但保留自己的60ms boundary blend、jerk权重0和
双手5Hz，避免把多个变量一起改掉。两种入口的启动信息、`runtime_start`配置及日志文件名
都会记录插值方案；C2文件名包含`c2_spline`，防止后续把Async误认为linear数据。

同步是当前基线。异步能隐藏多数约0.47～0.52秒HTTP等待，但真机任务效果没有优于
同步，跨chunk切换反而更大。

`02_servo_hold.sh --execute`、旧H5复位和拇指微动execute仍有软件锁。O10全零在
GDK 2.6.3命令语义中是开手姿态，不能把重启后的零反馈直接当作安全hold目标发送。

当前批准Home来自人工确认的第9条成功命令，与H5首帧最大仅差0.007194rad。Split
Powered每次成功命令更新`logs/runtime/last_hand_command.json`。只读查看Home：

```text
./scripts/03_reset_home.sh
```

VLA按`q`完全退出后，执行Split双手+双臂复位：

```text
./scripts/03_reset_home.sh --execute
```

它要求有效的最后成功手指命令。先初始化GDK并显示双臂行程，再输入`ok`倒数5秒；
手部输出闸门保持关闭，双臂先以100Hz在默认5秒内回H5第0帧14D中位数；确认双臂
完成后才打开闸门，让双手以5Hz最多8步回Home。若曾重启、
人工掰动或物理急停，不得使用旧锚点复位。

所有真实Powered入口只接受`ok/OK`确认，并倒数5秒。当前Split配置的双臂使用
low-latency，GDK不提供碰撞保护，必须由专人全程握住物理急停。
