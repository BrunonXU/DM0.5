# G2 × DM0.5 上机操作指南

这是现场人员唯一必须先看的文档。

09-08按操作者要求，入口22的34D发送耗时/tick迟到超过200ms仅记录告警后继续；
不再因该两项计时退出。事件为servo_send_overrun、servo_tick_overrun。SDK错误及人工停止仍有效，
normal通道不变；入口19双17D超时策略不变。34D手部汇总计数已修正。

2026-09-08单34D完整Sync候选（尚未验收）：在G2现场运行
`bash scripts/22_powered_sync_unified_34.sh --duration-seconds 60`。
normal/100Hz/单次34D，沿用入口19平滑参数和进程隔离；停止其他控制程序、确认工作区与急停，
按提示启动。该测试由模型驱动完整双臂双手，不是静态保持；异常立即停止。静态零反馈阻塞
尚未解决，不能将此完整推理测试的授权解释为静态测试通过。对照仍使用入口19。

## 0. 当前入口：双侧17D Sync（2026-09-07核对）

09-04已同步并完成双臂双手真机试跑，现场确认丝滑。当前使用入口19：每个100Hz tick两次
动态名称17D `joint_servo_control`，**normal通道**，手部不调用`move_ee_pos`。
下文原Split/05/低延迟/手部5Hz章节保留作旧入口参考，不代表19的配置。单次动态34D尚待验证，
不能复用历史手写名称的unified-34；RTC也还没有迁移新出口。

由操作者在G2终端执行，确认工作区清空、急停在手、不存在另一Robot owner；不通过SSH远程执行：

```bash
cd /home/agi/xza/DM0.5 || exit 1
./scripts/19_powered_sync_dual_side_17d.sh --duration-seconds 60
```

60可换成所需正整数；省略参数默认60秒。需要持续运行时单独使用`--continuous`，不能与时长参数
并用。时长按执行窗口换算，不保证墙钟时间恰好相等；结束不自动复位。旧`_5s.sh`仅为兼容包装。
该入口保留双臂weight10/C2/3.5mrad minimum-jerk和120ms边界融合，手部preview关闭。
2026-09-07右手四指canonical名称探针完成后，入口19发送名改为trim并校验已知集合，
原始反馈名保留在日志。保持双17D、原平滑参数及Control/Policy隔离。
本次是名称修正后的完整抓取复验候选，尚未完成在线验收；先有限60秒观察右手闭合和第二次抓取配合。
Sync Docker `NewLora-dm05-ws-75k-formal`、7892只读检查为Up 7 days。

结果边界、历史日志及后续事项见[当前状态](../../DM0.5/docs/CURRENT_STATUS.md)与
[根因总结](../../DM0.5/docs/robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)。

## 1. 历史Split入口固定配置（不适用于当前19出口）

单34D受控候选入口：`bash scripts/21_servo_layout_probe.sh --layout unified-34 --motion static --execute`。
在G2现场逐次运行；静态正常后再分别选择 `--motion right-index` 和 `--motion left-index`。
static为双侧当前位置保持1秒；index为指定食指+0.20rad往返4秒，其余轴保持。双手空载、空间清空、
急停在手，任何异常终止后续测试；故障不自动返回/复位。无需推理或相机。`--layout dual-side-17d`
提供相同轨迹的双17D对照。每轮独立输入ok；报告位于 `logs/diagnostics/layout_*.json`。
本入口仅受控候选，不能以返回0或完成提示替代动作对应与接收端日志验收。

- G2项目目录：`/home/agi/xza/DM0.5`
- 6000D：`10.130.153.114`
- 当前模型：75K checkpoint
- 正式Sync接口：`ws://10.130.153.114:7892/process_frame`，GPU7，native JPEG/PNG + binary WebSocket
- 正式RTC接口：`ws://10.130.153.114:7893/process_frame`，GPU3，native JPEG/PNG + binary WebSocket；服务已运行，仍需新配置Shadow/Powered门
- 模型合同：三路RGB + 34D state → `50×34` absolute action，约30Hz时间语义
- 当前硬件下发：Split
  - 双臂前14D：100Hz `joint_servo_control`
  - 双手后20D：正式Sync直接采样模型原始轨迹，5Hz changed-only；RTC保持独立旧配置，Async为5Hz
- 当前默认调度：sync
- RTC：服务器已部署；先Shadow重标定d/s，再arms-only，最后有限完整Split，禁止直接continuous

模型层仍是34D，但硬件层绝不把34D重新合成一次Servo请求。历史统一34D Servo在
这台G2上反复产生20个O10 `Param ... not found`，并发生过2.53秒阻塞和watchdog。

Split中的每个100Hz逻辑tick先生成完整有限34D目标，硬件适配层再发送固定的
14个双臂`joint_names`和14个`joint_positions`，`control_period=0.01s`。
`joint_velocities=[]`是GDK 2.6.3保留字段的合法取值，不代表位置命令缺失。后20D
不会混入Servo请求，而是由独立频率线程构造`nums=20`的双手命令。正式Sync线程
以5Hz采样模型30Hz waypoint之间的原始连续目标；若目标与上次成功提交值完全相同则跳过，
不会周期重发。RTC保持独立旧配置，Async仍使用5Hz。最新
`dispatch_summary`会逐chunk记录14D完整性、理论/实际100Hz覆盖率、有效频率、
最大发送间隔及边界窗口实际动力学。

## 2. 安全底线

- G2域控禁止执行任何`apt`、`apt-get`及其`sudo`形式。
- 专人全程握住物理急停；软件按键不能代替物理急停。
- 不同时运行两个G2控制进程。
- 旧Split/05的双臂使用low-latency，没有GDK碰撞保护；当前19为normal，不得混淆或自动切换。
- `02_servo_hold.sh --execute`、旧H5复位和拇指微动execute均已软件锁定。
- 唯一批准的复位入口是`03_reset_home.sh --execute`：先恢复双臂14D，再恢复O10双手。
- Powered结束只停止发送，不会自动复位。

首次部署或覆盖脚本后，终端执行：

```
cd /home/agi/xza/DM0.5 || exit 1
chmod +x scripts/*.sh scripts/lib/*.sh
```

脚本会自动`source /home/agi/app/env.sh`；不需要操作者单独执行。Python内部仍会调用
`gdk_init()`并在结束时调用`gdk_release()`。

## 3. 每次上机固定顺序

### 3.1 只读预检

```
./scripts/00_preflight.sh
```

成功标准：

- endpoint返回405；
- motion mode=5且error_code=0；
- 34D状态全部finite；
- 头部、左腕、右腕三路相机均有正常图片；
- 全程没有发送动作。

报告与图片位于：

```text
logs/diagnostics/preflight_<时间>/
```

### 3.2 O10双手只读检查

```
./scripts/check_hand_state.sh
```

它只检查左右`o10_t2`名称、位置和健康字段，不发送动作。重启后20个手指反馈可能
全部为0；官方GDK 2.6.3同时把O10全零定义为开手命令，因此零反馈不能直接当成
安全hold目标回发。

### 3.3 Shadow

```
./scripts/01_shadow.sh 10
```

成功标准：10次严格`50×34`，无相机/state/HTTP/shape/NaN错误，Servo发送为0。
日志位于`logs/telemetry/shadow/`。

延迟字段解释：

- P50：一半请求不超过该值，即中位数；
- P95：95%的请求不超过该值，用来观察较慢但常见的尾部延迟。

75K当前实测HTTP RTT约P50=0.47秒、P95=0.52秒。该时间包含三图编码上传、网络、
服务器预处理、模型推理、响应返回和客户端解析，不是纯网络延迟。

## 4. 当前正式Powered：75K Split Sync

raw 5Hz changed-only + native WebSocket先运行5秒：

```
./scripts/05_powered_sync.sh --duration-seconds 5
```

通过5秒门禁并分析日志前，不运行10秒、45秒或continuous。该入口固定：

- 75K/GPU7/7892；
- native GDK JPEG/PNG透传、persistent binary WebSocket、`50×34 float32`响应；
- Split硬件下发；
- 双臂low-latency 100Hz；
- 双臂30→100Hz显式使用C2 cubic spline插值；
- 双臂最终100Hz目标启用`stationary_minjerk_v1/0.0035rad`：仅当单轴范围不超过
  0.20°、至少反向两次且净位移很小时，改成同端点minimum-jerk曲线；
- 双手`move_ee_pos` 5Hz，直接对模型30Hz原始手部轨迹采样；
- 120ms cubic boundary smoothstep（正式默认）；可用
  `--boundary-blend-profile quintic_smootherstep`做严格单变量A/B；
- 双臂chunk内部固定端点jerk正则权重10；
- 双手关闭`shared_preview_v1`/QP；目标与上次成功`move_ee_pos`值相同则跳过，变化目标与模型原始20D一致；
- 首跳、裁剪幅度和动力学超限记录告警但不拒绝chunk；
- shape、finite、物理关节限位、HTTP/GDK失败停止仍然有效。

模型仍输出50个waypoint，但正式同步模式每次只执行前20步，约`20/30=0.667秒`，随后hold、重新采集三路图像和34D state并请求新chunk。这样能更快看到偏位或抓空后的新观测；等待HTTP期间的停顿仍不代表100Hz Servo线程降频。

新版本在`action_chunk.request_timing_seconds`和`run_summary.request_timing`中分段
记录相机、状态、JPEG、HTTP、服务端输入transform、模型+回传CPU、输出transform和
未归因传输耗时。该字段只读，不参与动作接受、拒绝或Servo下发。延迟优化先运行
Shadow采样，禁止为了测时重新启用Async。

### 4.1 C2 + 120ms + weight 10双臂平滑基线

两轮powered、共47个实际chunk已经确认这组配置稳定：内部chunk最大步进P95离线
反事实下降约11.5%，内部jerk P95下降约70%，未出现Servo故障、safe hold、裁剪或
动力学告警。随后用最新28个和此前54个可重建chunk复验五种30→100Hz方法，C2 spline
在两组中均为jerk最优：相对linear把内部jerk P95降低40.4%/39.7%、chunk jerk max
P95降低54.3%/62.1%，最大轨迹偏差约0.00145rad且无物理限位违反。因此正式Sync入口
固定注入C2、120ms和权重10，日常运行不再需要手写这些参数：

```bash
./scripts/05_powered_sync.sh --duration-seconds 45
```

低层Python CLI为兼容旧诊断仍默认linear；正式脚本不依赖该默认值。Async实验入口也
显式改为C2，但继续使用自己的0.06/0和双手5Hz，不继承Sync的其余参数。Sync与Async
日志文件名均带`c2_spline`，启动信息和`runtime_start`也记录实际profile。只有需要
显式回退对照时才运行：

```bash
./scripts/05_powered_sync.sh \
  --duration-seconds 45 \
  --arm-interpolation-profile linear \
  --boundary-blend-seconds 0.06 \
  --arm-internal-jerk-weight 0
```

正式Sync的微颤候选日志另带`arm_micro_minjerk_v1`，每个chunk用
`arm_micro_motion_projection`记录触发关节、原始反向数和最大轨迹修改量。它不做速度限幅，
不改变双手、频率或chunk端点。首次只运行5秒；若动作意图变弱或出现新停顿立即退出。

新jerk正则只处理双臂14D。34D逻辑目标仍按模型30Hz waypoint连续采样；正式Sync手线程
以5Hz读取当时最新20D原始目标，仅在变化时调用`move_ee_pos`。它不修改模型waypoint、
顺序或1.667秒horizon，也不再用preview/QP改变手部意图。50Hz、30Hz、20Hz及动态10Hz
连续重提均已有负面真机证据，不恢复为正式配置。

旧V1的400ms cubic手部边界guide、2rad/s、10rad/s²和0.40rad emergency cap在10Hz下
仍允许约0.20rad一步。三批123个chunk离线扫参后，正式Sync改为V1.1短时候选：
0.175rad、1.75rad/s、7.5rad/s²，反转hold仅限边界；233个关键半程事件0遗漏，但一批
tracking error P95增加约0.072rad，尚未真机验收。日志名带`hand_v11`。两轮50Hz运行虽达到约50Hz且没有deadline miss，但出现明确高频振动声，
因此20/30/50Hz均不得恢复。10Hz在新网络链路上先重复5秒，专人握住物理急停；任何声音或抖动立即
停止。显式回退旧V1、保留双臂冻结配置：

```bash
./scripts/05_powered_sync.sh \
  --duration-seconds 10 \
  --hand-smoothing-profile v1
```

完全关闭手部tracker只用于诊断，改传`--hand-smoothing-profile off`，不得与V1.1真机
单变量验证混为一轮。

第一条手部命令没有本进程内可信成功锚点时不做V1衔接；不会把重启后的20D零反馈当作
起点。从第二个chunk开始使用本进程上一条已确认成功命令作为边界锚点。V1.1第一次只
运行10秒；闭手明显变慢、物体抓不住、异响或抖动时立即停止并回退V1。

## 5. Powered RTC实验

GPU3/7893已经运行与Sync同代的native binary WebSocket、五步compile服务；08可用于
只读Shadow。09/10仍是Powered入口，只有完成Shadow与`d/s`重标定后才运行，禁止跳过
门禁或直接continuous。

2026-08-21完整Split Powered RTC入口已改为`H=50,d=16,s=20`，使用独立7893五步
fixed-prefix compile服务。双臂suffix C0/C1和weight10保持；双臂/双手首跳参考阈值为
0.05/0.35rad但按现场要求只告警。首次只运行有限时长，不运行continuous。

09默认启用进程隔离：控制进程只拥有Robot、RTC安全状态机和完整Split输出；spawn策略
worker独立拥有三路Camera、OpenCV、JPEG、HTTP和JSON，但不创建Robot；Control读取
34D state并通过带timestamp/sequence的共享ring交给worker。图片不跨进程。
09在询问Powered授权前会自动运行以下单Robot-owner只读门：

```bash
./scripts/11_rtc_process_isolation_preflight.sh
```

Preflight不创建Servo且命令发送严格为0；失败时09立即退出。第一次部署可先单独执行11，
确认结果文件中`ok=true`、`policy_robot_reader=false`、
`control_state_reads_during_inference>0`和`robot_commands_sent=0`。

RTC服务上线后先运行Shadow；默认旧候选为`d=16,s=20`，也可显式指定候选，日志文件名
自动带d/s：

```bash
./scripts/08_rtc_shadow.sh --requests 60 --d 16 --s 20
```

Shadow用于获得RTC自己的observation age和调度miss，禁止直接用Sync E2E决定d/s。

运行使用独立7893服务，推理期间继续执行旧chunk；早到响应缓存到服务端相同的
逻辑`d=16`后再切换。
完整双臂+双手60秒验证：

```bash
./scripts/09_powered_rtc.sh --duration-seconds 60
```

该入口当前候选固定`H=50,d=16,s=20`、guidance 0.5；客户端先做防御性C0/C1，再只对
未承诺arm suffix执行weight10 jerk平滑，并使用C2 cubic spline 30→100Hz插值。正式
日志tag为`c2_spline_hand_rtc_cap_v2`。bootstrap使用120ms接入，稳态RTC已有C0/C1，
不再重复boundary blend。硬件层使用Split，双臂14D/100Hz并发送双手20D/10Hz；
双手当前仍用0.15/0.30rad的`rtc_cap_v2`临时对照，尚未继承V1.1，也不是最终共享
jerk-limited轨迹器。首跳和动力学超限告警，物理限位clip超阈值、合同、HTTP、IPC、Servo/GDK故障仍停止。若真实
`d>30`、旧chunk耗尽、
HTTP或动作合同失败，程序hold并结束，不回播第0步，也不回退到Async。LOW-LATENCY
仍无GDK空间碰撞保护，RTC也不是碰撞检测器。

## 6. 异步实验

```
./scripts/06_powered_async_experimental.sh --duration-seconds 60
```

异步在旧chunk剩20个30Hz区间时提前请求，返回后通常跳过14～16个陈旧步。它能隐藏
大部分同步等待，但当前真机任务效果没有明显优于同步，跨chunk切换跳变更大，并出现
过一次迟到响应回退。因此它不是默认入口，不应与新的平滑算法同时改动测试。

## 6. 严格安全门短诊断

`04_powered_guarded_smoke.sh`只用于1/3/6个30Hz区间诊断，默认normal和0.05rad
首跳门，不用于完整任务：

```
./scripts/04_powered_guarded_smoke.sh 1 normal
```

它同样使用Split硬件接口。如果首跳超限，应显示Servo发送次数为0。

## 7. 软件中断

Powered终端按键无需按Enter：

| 按键 | 行为 |
|---|---|
| `p` | 丢弃当前chunk并保持最后安全目标 |
| `r` | 从最新图像/state重新请求，不恢复旧chunk |
| `q` | 优雅结束并释放GDK |
| `Ctrl+C` | 与q相同 |

正式同步入口支持`--continuous`持续请求。连续模式没有“任务已经完成”的停止语义，
只能由`q`/`Ctrl+C`、Servo/GDK故障或进程异常结束，必须始终有人看守。

有限时长不是固定chunk上限：例如45秒按`ceil(45×30/20)=68`个请求计算，预计执行约45.3秒模型动作。若确实需要
无请求上限，使用`./scripts/05_powered_sync.sh --continuous`；该模式不会自动识别
任务完成，禁止无人值守。

SSH终端丢失时，在另一个G2终端执行：

```
cd /home/agi/xza/DM0.5 || exit 1
./scripts/stop_runtime.sh
```

脚本只向核验过命令行的客户端PID发送SIGINT，不使用`kill -9`。

## 8. 每轮结束后的复位

每次VLA都是独立运行，不恢复旧chunk。固定流程：

```text
VLA终端按q完全退出
→ 托住手中物体
→ Split先复位双臂、再复位灵巧手
→ 平板仅恢复身体/底盘（如需要）
→ Preflight
→ 启动下一轮新VLA
```

批准Home来自2026-08-04人工确认的第9条成功命令，与800条H5第0帧中位数最大只差
`0.007194rad`。Powered只持续更新命令空间
`logs/runtime/last_hand_command.json`，不再把第一条近零命令保存为Home。

只读查看批准Home：

```
./scripts/03_reset_home.sh
```

VLA按`q`退出后，在30分钟内执行Split双手+双臂复位：

```
./scripts/03_reset_home.sh --execute
```

执行时先初始化DDS/GDK约10秒并显示双臂最大行程，再要求输入`ok`并倒数5秒。独立
`move_ee_pos`新会话已被真机证明会拒绝有效20D命令，因此脚本复用正式Split。执行时
手部输出闸门保持关闭，双臂先以100Hz在默认5秒内回到800条H5第0帧14D中位数；
确认双臂完成后才打开闸门，先重发最后成功手指命令作为锚点，再以5Hz最多8步回
批准Home。若VLA仍在运行、
最后命令超过30分钟、机器人重启、人工掰动或使用过
物理急停，禁止运行该复位；改走人工恢复流程。复位全程使用low-latency且没有GDK
碰撞保护，必须由专人握住物理急停。双臂最大行程超过1.5rad时脚本拒绝执行。

旧复位Shell入口已删除；`02_servo_hold.sh --execute`仍是NO-GO。不要把O10反馈全零
回发：官方定义全零为开手，曾导致拇指接触相机。现场复位只使用
`03_reset_home.sh`。

## 9. 日志

```text
logs/
├── telemetry/shadow/   # Shadow JSONL
├── telemetry/powered/  # Powered JSONL
├── diagnostics/        # preflight和状态报告
├── runtime/            # 最后成功O10命令；供退出后短时复位锚定
├── gdk_native/         # GDK/C++ glog
└── bundles/            # 仅显式collect_logs时生成
```

运行异常时先物理急停，再按`q`或执行`stop_runtime.sh`。不要立即重启Powered；先保存
本次Powered JSONL和对应时间段的GDK原生日志。

### 9.1 从6000D增量拉取G2日志

不再手工查找最新JSONL文件名。每次Shadow或Powered结束后，切换到6000D终端，
在`g2_robot`根目录执行：

```bash
cd /home/xza_100850113/projects/g2_robot || exit 1
./scripts/pull_new_g2_logs.sh
```

该脚本递归检查G2的`/home/agi/xza/DM0.5/logs/`，把新增日志拉到6000D当前仓库的
`logs/`并保持相对目录结构。日常默认跳过大体积`gdk_native`：平滑分析使用
`telemetry/powered`或`telemetry/shadow` JSONL即可。出现DDS、mode、Servo/GDK
发送失败或原生崩溃时，再执行：

```bash
./scripts/pull_new_g2_logs.sh --include-gdk-native
```

筛选规则以“相同相对路径是否已存在”为准：本地已存在的文件直接跳过，绝不覆盖或
删除；只增加6000D尚未见过的文件。日志文件均使用时间戳唯一命名，正式运行结束后
再拉取，避免把仍在追加的文件误认为已经完整同步。

脚本优先使用`rsync --ignore-existing`。如果6000D或G2任一端没有`rsync`，会自动
退回到只读`ssh find`加逐文件`scp`，不需要也禁止为此在G2安装软件。脚本只读取G2
日志，不会远程启动GDK、Servo、复位或Powered动作。

### 9.2 每轮生成科研图和本地页面

在6000D运行：

```bash
./scripts/update_motion_dashboard.sh
./scripts/serve_motion_dashboard.sh --port 8765
```

打开`http://127.0.0.1:8765/`；远程IDE需转发8765。首页按时间倒序并标明
MODEL/COMMAND/FEEDBACK数据层，重新生成后浏览器自动刷新。只处理本地可加`--no-pull`，
指定重建可用`--all --run logs/telemetry/...jsonl`。

首次允许Powered连续反馈前，机器人保持静止且没有控制进程，在G2手动运行：

```bash
./scripts/14_joint_feedback_rate_probe.sh
```

该命令只读GDK状态，不发送Servo或手部命令。10/20Hz均未通过时不得绕过结果；Powered保持
command-only，页面仍可分析模型和实际命令。

## 10. 代码同步原则

6000D与G2已在同一公司内网。只同步代码、脚本、文档和测试，不同步6000D历史
`logs/`、`.pytest_cache`、`__pycache__`、旧tar包或SHA文件。所有真机命令仍由
Bruno在G2终端手动执行，6000D不得远程启动GDK动作。

6000D终端在`g2_robot`根目录只对本次核准的具体文件逐个执行`scp`，目标固定为`agi@10.130.86.13:/home/agi/xza/DM0.5/`。禁止递归覆盖整个目录，也不远程删除G2文件。
