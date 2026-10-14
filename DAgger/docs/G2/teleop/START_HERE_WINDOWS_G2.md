# G2 独立遥操三端启动

先读 [G2 当前状态](../RECORDING_AND_PROGRESS.md)；最后记录仍需维修后重新验收。此页是独立 F710 遥操，HIL 用 [G2 START_HERE](../START_HERE.md)。

本地源码现在位于 `DAgger/G2/teleop`。下列完整命令沿用最后记录的 Windows / G2 已安装目录；本次没有移动远端安装。部署统一源码树后，工作目录应改为该机器的 `DAgger/G2/teleop`；脚本根据自身位置找根，不依赖开发机旧 G2Teleop 目录。

**终端 1 · Windows PowerShell（F710 接收器插在 Windows）：**

```powershell
cd "$HOME\G2Teleop"
.\scripts\windows_g2_tunnel.cmd
```

**终端 2 · Windows PowerShell：**

```powershell
cd "$HOME\G2Teleop"
.\scripts\windows_f710_sender.cmd
```

**终端 3 · G2 本机现场终端：**

```bash
cd /home/agi/xza/G2Teleop
bash scripts/12_g2_powered_f710.sh
```

此独立链路默认7904；不要混用HIL的7907→7906隧道。F1/G2参数与手部语义不混用。确认现场条件后按Enter；先释放RB再选侧，RB控臂、RT切手、Y按该入口合同Home。结束在原G2终端Ctrl+C，等写盘完成后再结束sender和隧道。

统一开发树的日志为 `artifacts/logs/g2_teleop`，可用 `G2TELEOP_LOG_DIR` 显式指定；独立安装默认自己的logs目录。旧机载版本的结构遥测仍按原脚本写DM0.5日志，不能因移动开发树就推断机器人已换路径。

## 历史现场过程（原说明完整保留）

以下“当前/最新”均属于文内记录日期，阶段选择以本页顶部和G2进度为准；历史诊断命令保留用于追溯。

# Windows + F710 到 G2：唯一入口

> **已迁移：HIL/DAgger请从 [DAgger根目录启动说明](../START_HERE.md) 开始。** 下文为历史记录，冻结F710独立保留。

> **最新：下一次真实场景运行 [40号 Powered POLICY↔HIL 候选](../POWERED_HIL_CANDIDATE_20260907.md)。** 继续使用7906 Windows候选tunnel/sender；G2执行40号shell，ENTER后将有真实policy动作。候选已离线验证，尚未真机验收。以下39及12说明为此前阶段。

> 当前下一步：[真实三路采集/state/policy 零输出入口 39](../HIL_REAL_OBSERVATION_SHADOW_20260907.md)。37 网络 Shadow 已有完整日志；用户决定跳过剩余专项测试。Windows 继续使用独立端口7906的候选 tunnel/sender。

> 当前基线：`f710-powered-v1-20260904`，direct Pink、双臂100Hz normal Servo、O10约5Hz
> changed-only；Ruckig默认关闭。HIL仅START切换，button7原始按钮只读验证已通过，尚未接Powered。
> [本轮录制离线实现与证据](../HIL_RECORDING_OFFLINE_20260907.md)。

本页是现场唯一入口。不要在 PowerShell 中重新创建 `.venv`，也不要再运行旧的
`04_windows_g2_tunnel.ps1`。已验证的 Python 固定为：

```text
C:\Users\100850113\.venv\Scripts\python.exe
```

## 启动顺序

### START只读确认（2026-09-07启动修正）

**最新结果：已通过。** 用户独立probe输出六对button=7 DOWN/UP，含1422ms长按；
确认物理START的原始键号为7。以下保留排障过程，sender/协议/G2 HIL仍需独立零输出验证。

**现场后续：** 用户提供的Windows输出仅包含轴、hat、LB/RB，完全没有A/B/X/Y/START字段；
说明现场仍运行旧版只读脚本。首次未识别设备，第二次已经识别F710，不能据此判START硬件失效。
为避免升级共享控制映射，新增独立`02_windows_f710_buttons_readonly.py`：仅依赖既有pygame，
显示原始button编号、DOWN/UP、当前pressed列表及脚本路径。仅将这个文件复制到Windows即可。
开发机help/编译通过，Windows实际按钮结果仍待用户确认，不计入Powered验收。

```powershell
cd $HOME\G2Teleop
scp agi@10.130.86.13:/home/agi/xza/G2Teleop/scripts/02_windows_f710_buttons_readonly.py .\scripts\
& "$HOME\.venv\Scripts\python.exe" scripts\02_windows_f710_buttons_readonly.py
```

看到`RAW BUTTON PROBE v1`后，只按START、保持约1秒再松开，重复两次并保存输出。
预期button=7，但以实际输出为准；不需要启动隧道、sender或G2 receiver。

在插着F710的Windows经典PowerShell运行，不需要隧道或G2 receiver：

```powershell
cd $HOME\G2Teleop
$env:PYTHONPATH = "$PWD\src"
& "$HOME\.venv\Scripts\python.exe" scripts\02_windows_f710_readonly.py
```

直接使用`py scripts\02_windows_f710_readonly.py`曾报`ModuleNotFoundError: g2teleop`：
该调用未提供项目src路径，且不保证选择已验证的Python环境。上述命令复用sender的既有环境，
无需重新安装或创建venv。本地只读脚本也已补充基于自身路径的src导入；Windows未更新脚本时，
上述PYTHONPATH设置同样有效。开发机`--help`已通过，仅验证导入和参数解析，未验证Windows手柄。
单独按下/释放START，记录`start`由false变true再变false；原始button7已由后续独立probe确认，旧reader/mapper全链尚待验证。

### 强制使用经典 Windows Console Host

F710 真机摇操期间禁止使用 Windows Terminal（带标签页、`+`号的窗口）。现场在
2026-08-31 的 09:03、09:18、09:26 三次退出均由`WindowsTerminal.exe`自身崩溃造成，
事件签名一致：`Windows.UI.Xaml.dll`、异常`0xc000027b`。SSH随后以`Exit status 0`
结束，sender也没有Python traceback；因此`WinError 10054`是Terminal杀死隧道后的结果，
不是网络根因。该现象与XAML接收游戏手柄焦点导航输入的已知问题吻合，可能由LB或其他
手柄输入触发，但不是我们把LB映射成了Windows快捷键。

首次使用时先拔掉F710，在Windows Terminal的“设置 -> 启动 -> 默认终端应用程序”中选择
`Windows Console Host`并保存，然后关闭所有Windows Terminal窗口。从开始菜单单独打开
两个“Windows PowerShell”窗口；它们必须是没有标签页和`+`号的经典控制台。可执行
`Get-Process WindowsTerminal -ErrorAction SilentlyContinue`确认没有输出，再连接F710。

先在 G2 本机启动当次明确指定的 receiver。然后在 Windows 项目根目录分别开两个经典
Windows PowerShell窗口：

```powershell
cd $HOME\G2Teleop
.\scripts\windows_g2_tunnel.cmd
```

```powershell
cd $HOME\G2Teleop
.\scripts\windows_f710_sender.cmd
```

两个 `.cmd` 都不依赖 PowerShell ExecutionPolicy。隧道窗口只负责网络，异常退出后每2秒
自动重连；sender窗口只读取F710并在异常退出时`pause`。任何一个窗口退出，receiver都必须
进入HOLD，隧道恢复也不会自动重新使能动作。

SSH keepalive使用10秒间隔、连续3次失败才结束隧道；它只影响连接存活。根据现场50Hz包
间隔最大151.8ms、120ms门在23秒内误触发5次的实测，G2运动watchdog调整为200ms；以
4cm/s上限计算，完整断联后最多继续8mm才HOLD。SSH可继续恢复传输，但恢复后必须重新接管。

Windows sender以50 Hz读取F710。读取线程不等待ACK：网络线程只发送最新状态，旧的中间帧
会被覆盖而不会在恢复后补播；ACK只用于连接健康和RTT统计。每次连接恢复都会创建新session，
清空待发状态并强制重新执行“全松开、选侧、重新按RB”，绝不自动继续旧动作。
Windows每帧会读取并清空pygame/SDL事件队列，同时启用后台手柄事件和SDL Windows joystick
消息线程，避免切换终端焦点或事件长期堆积造成输入冻结。
现场日志曾显示移动时逐帧打印轴值并同步flush控制台和文件，造成`sample_ms max=172`；现已
删除实时轴值输出，只在状态变化和每5秒时序汇总时写日志。Windows同时请求1ms timer
resolution，并在20ms deadline最后1ms精确等待，减少16/32ms调度量化。

## 不可省略的按键顺序

1. 松开 `RB/LB/LT/RT`和两个摇杆；
2. 在 **RB 松开** 时按十字键左（左侧）或右（右侧），然后松开十字键；
3. 终端必须先显示 `side=left`或`side=right`；
4. 再按住 `RB`控制机械臂；选侧后`RT`可独立开合手，无需RB；
5. 松开 `RB`立即HOLD机械臂；网络正常时手部可继续独立渐进。

按住 RB 再切十字键会被互锁拒绝，日志会出现`select_side_before_deadman`；十字键尚未松开就
按RB会出现`release_dpad_then_press_deadman`。被拒绝的十字键边沿不会延迟到松开RB后生效，
必须松开十字键并重新按一次。这不是连接失败。

## 闪退与关闭

- 若两个标签页同时消失，先查Windows应用事件；不要先增大网络watchdog。已确认本机
  `WindowsTerminal.exe 1.24.2607.10001`存在XAML崩溃，现场规避方法是使用Windows Console Host；
- 已打开的 PowerShell 中按 `Ctrl+C`会终止当前前台程序；
- 直接关闭窗口也会断开隧道或 sender；
- 从资源管理器双击旧脚本时，程序报错后窗口可能立即关闭；
- F710 的 A/B/X/Y 不负责关闭 PowerShell。

正常停止顺序：先在 sender 窗口 `Ctrl+C`，再在隧道窗口 `Ctrl+C`，最后在 G2 receiver
窗口按一次`Ctrl+C`。Powered 时先松开 `RB`并确认 HOLD，再按这个顺序关闭。G2打印
`Ctrl-C: closing powered command owner`后不要重复按`Ctrl+C`，等待出现
`Structured telemetry finalized: ...jsonl`；writer最多允许30秒排空，半文件不会上传。

## Powered 真机 receiver

### Ruckig OTG P2状态（历史实验入口）

本节保留OTG安装与实验记录，不是当前HIL工作要求；当前已验收默认链无需启用Ruckig。

Ruckig OTG已完成Shadow和第一次Powered实验，但第一次Powered仍明显抖动并触发一次
Pink/OTG lag HOLD，因此尚未成为默认基线。第一次同步代码到G2后只需安装一次：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/16_install_ruckig_g2.sh
```

该脚本固定安装官方`ruckig==0.19.4`到现有`vendor_ik`，G2的aarch64/Python 3.12没有预编译
wheel，因此会在本机源码构建；默认清华PyPI源，不使用sudo。安装结束必须看到
`Ruckig 0.19.4 G2 smoke PASS`。随后先运行100Hz零输出Shadow：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/17_g2_ruckig_otg_shadow.sh
```

G2终端必须显示`READ-ONLY 100 Hz RUCKIG SHADOW`和`ZERO hardware output`，再照常启动Windows
tunnel与sender。此入口只读取一次接管状态并虚拟运行与Powered相同的`PoweredArmTarget`，源码
回归测试禁止出现GDK运动输出API。完成单轴正向、回中、反向后只按一次`Ctrl-C`，日志位于
`DM0.5/logs/telemetry/shadow/`。程序封口后会提示输入一次6000D密码并上传到dashboard的
`telemetry/shadow`；打开页面时会明确显示`ZERO-OUTPUT F710 OTG SHADOW`，T6展示Pink目标、
Ruckig输出与acceleration/jerk约束利用率，不会伪装成Powered数据。

普通`bash scripts/12_g2_powered_f710.sh`仍运行无OTG基线；旧的实验OTG入口必须显式写：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/12_g2_powered_f710.sh --enable-ruckig-otg
```

缺包时程序会在GDK初始化前退出。第一版冻结参数为phase synchronization、
`a=2rad/s²`、`jerk=20rad/s³`。当前不要继续用旧入口盲调参数；下一轮必须使用下面的
`timing-ab`配对入口。

### 历史平滑诊断A/B（非当前HIL下一步）

`timing-ab`保持normal碰撞保护、双臂100Hz和O10 changed-only 5Hz，不提高速度、不修改Pink/OTG
参数。它关闭T5同步反馈，完整分层日志降到20Hz，但保留每次真实100Hz GDK dispatch时刻与
14D command，用来判断抖动是否首先来自发送时基。

同一Home下先A后B，每轮结束都回Home：

```bash
cd /home/agi/xza/G2Teleop

# A：无OTG
bash scripts/18_g2_powered_f710_timing_ab.sh direct

# B：Ruckig OTG
bash scripts/18_g2_powered_f710_timing_ab.sh otg
```

动作固定为左臂、空载、小幅单轴：X正/回中/X负/回中、Y正/回中/Y负/回中、最后单轴旋转。
每段约2秒，回中约1秒。禁止接触饮料瓶，禁止在两轮之间改速度、手柄映射、jerk、acceleration
或synchronization。任何持续振动、异响或非计划运动立即松RB并停止。

先停止旧的`10_g2_live_ik_shadow.py`窗口，确认7904端口空闲。不要同时运行 Shadow 和
Powered receiver。在 G2 本机执行：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/12_g2_powered_f710.sh
```

入口首先自动执行files-only preflight。看到
`FILES-ONLY PREFLIGHT COMPLETE: GDK was not imported or initialized`表示该阶段只检查本地
模型、依赖、端口和O10回退文件，没有触碰机器人。`O10 ZERO-FEEDBACK FALLBACK READY`表示
现场O10反馈若为零仍有合法锚点；`UNAVAILABLE`表示只有实时反馈非零才能继续，否则Powered
会安全退出并要求先跑审核Home。禁止通过延长过期时限或伪造Home绕过。

阅读范围提示、确认专人握物理急停后，直接按回车继续；`Ctrl+C`取消。入口不再使用文本
口令或5秒倒计时。

看到`POWERED receiver listening only on 127.0.0.1:7904`后，才依次启动 Windows 的
`windows_g2_tunnel.cmd`和`windows_f710_sender.cmd`。首次必须空载，每次只轻推一个方向，
先验证左右侧与正负方向，再接近饮料瓶。

Powered receiver 的硬边界：

- 仅双臂14D走`joint_servo_control(normal)`，100 Hz；
- 仅双手20D走`move_ee_pos(dual_tool, o10_t2)`：意图渐进过程约5Hz，20D不变时不调用；
- 不含底盘命令；腰部只读用于IK；不允许low-latency；
- 网络包龄超过200 ms、松开RB或断开时保持最后成功目标；
- IK失败会锁定HOLD，松开RB后才允许重新接管；
- O10反馈非零时使用严格反馈；反馈全零时只允许使用6小时内、schema/来源/关节顺序/限位
  全部通过校验的DM0.5最后成功`move_ee_pos`命令作为HOLD。唯一年龄豁免是：过期记录的20D
  必须与人工批准Home逐值完全相同；普通旧抓握命令仍拒绝，并提示执行
  `cd /home/agi/xza/DM0.5 && bash scripts/03_reset_home.sh --execute`。

Powered启动默认低速4cm/s、10deg/s；A单击在1/2倍速度切换，松开A不切回。
20cm/20度为rolling envelope的单段积分包络，越界前同tick连续rebase，正常持续运动无需松RB。
同侧继续操作只需松开RB再按住；换侧时先松开RB，按下并释放十字键，再按RB。
RB期间或与RB同帧的十字键选择被丢弃，不会延迟应用。

每次启动的终端输出追加到`logs/powered_f710_console.log`，用于诊断连接、接管、workspace
饱和、IK HOLD与GDK异常；这仍不是DAgger训练数据。

结构化F710日志使用`.partial → .jsonl`原子封口。`12_g2_powered_f710.sh`退出时会由G2主动
上传已封口日志到6000D，并提示输入一次6000D账户密码；服务器无需为F710执行定时拉取，也
不要把上传追加到复位脚本。
看到`F710 telemetry uploaded; G2 originals retained.`表示成功；失败时真机原件仍保留，按终端
打印的主日志路径在G2重试：

```bash
cd /home/agi/xza/G2Teleop
bash scripts/13_g2_push_telemetry.sh \
  --main /home/agi/xza/DM0.5/logs/telemetry/powered/g2_powered_f710_YYYYMMDD_HHMMSS.jsonl \
  --feedback /home/agi/xza/DM0.5/logs/telemetry/feedback/g2_powered_f710_YYYYMMDD_HHMMSS.jsonl
```

2026-09-01已修复长反馈日志在Ctrl-C封口时的64KB尾部边界缺陷：尾读若从JSONL记录中间开始，
现在会丢弃首个残片并逐行容错；即使feedback校验异常，也不得再阻断主telemetry封口。现场只
按一次Ctrl-C，等待出现两条`finalized`和上传提示后再关闭终端。

feedback文件不存在时可省略`--feedback`。6000D上的仪表盘服务只监听本地新文件并重建页面，
不登录G2。

## 流畅度与时序日志

Windows sender和G2 receiver每5秒打印一行`TIMING`：

- sender启动时如果Windows尚未枚举F710，不再退出或关闭窗口，而是保持`HOLD`并每0.5秒重试；
  插好USB接收器、切到`X`、唤醒手柄，并用`joy.cpl`确认后会自动继续。运行中断连同样先发送
  disabled intent并等待重连，重新连接后必须重新选择侧、松开所有控制并重新按住RB；

- Windows `sample_ms`：F710真实采样周期，目标p50约20ms；sender完整输出永久追加到
  `logs/windows_sender_console.log`；
- SSH tunnel的verbose诊断永久追加到`logs/windows_tunnel_ssh.log`；
- Windows `ack_rtt_ms`：隧道往返时延，但不会阻塞下一次采样；
- `latest_overwrites`：网络线程尚未取走时被新状态覆盖的次数，这是有意丢旧帧，不是补播；
- G2 `packet_interval_ms`：意图包到达间隔，目标p50约20ms；
- G2 `control_interval_ms`：臂控制周期，目标p50约10ms；`takeover_ms`记录接管状态读取耗时；
- G2 `ik_ms`和`gdk_send_ms`：分别定位Pink求解和GDK同步调用耗时；
- `packet_age`与`watchdogs`：若包龄超过200ms，会明确打印`WATCHDOG HOLD`并要求重新接管；
- `arm_step_rad`和`small_reversals`：判断均匀抖动是否已经存在于IK生成的14D目标中。

G2调度错过周期时会丢弃旧tick，不会连续追赶下发。末端速度使用约150ms的矢量斜坡，消除
摇杆阶跃；松开RB仍立即HOLD，不经过斜坡。平移/旋转幅值不变，接管包络由10cm调整为仍然
有限的20cm。O10采用正常速度完整replay已真机验证的5Hz changed-only语义：目标变化才提交，
禁止恢复10Hz周期重提或用提高手频率掩盖顿挫。

旧固定包络曾在持续运动约5秒后出现WORKSPACE SATURATED；rolling envelope已真机修复并冻结。
若再次停住，应按日志区分recenter、workspace saturation、WATCHDOG HOLD和SDK/IK错误，不能默认
归因为网络。当前WATCHDOG HOLD仍表示包龄超过200ms。

## Powered 操控表

| 操作 | RB松开时 | RB按住时 |
|---|---|---|
| 十字键左/右 | 选择左/右臂+同侧手 | 忽略并告警 |
| 十字键上/下 | 未分配 | 未分配 |
| 左摇杆上下 | 无动作 | 平移`+X/-X`前后 |
| 左摇杆左右 | 无动作 | 左推`+Y`、右推`-Y` |
| LB / LT | 无动作 | 垂直`+Z/-Z`，可与XY同时输入 |
| 右摇杆上/下 | 无动作 | pitch负/正（自然视角，非飞行反向） |
| 右摇杆左/右 | 无动作 | yaw正/负（2026-09-01真机反馈后整体反号） |
| X / B | 无动作 | roll负/正 |
| RT单击 | 第一次闭合所选手，再次单击张开 | 同左；RT独立于RB，必须释放后才接受下一次 |
| A单击 | 低速/高速切换，初始为低速 | 同左；松开A不改变已选择速度档 |
| Y | 双臂5秒回H5 Home，再以最多8个5Hz步进复位双手 | 必须先松开RB，否则拒绝 |
| 松开RB | HOLD | — |

平移和旋转可以同时输入，向量超过单位球时按方向等比例归一化，不再触发速度越界HOLD。
低速为4cm/s和10deg/s；A切换后的高速为8cm/s和20deg/s。加速斜坡仍按原速率变化，后级
0.35rad/s关节速度限制、奇异位形连续降速和20cm接管包络不变。再次单击A回低速；程序启动、
断联或Y Home完成后均恢复低速。
RT使用0.65按下/0.35释放的迟滞去抖，左右手各自保存二值状态；RT只要求事先选好活动侧，
不要求按住RB。某侧首次单击前不产生任何开合目标，因此仅接管或换侧绝不会自动张手。
未选中的另一只手和另一条臂始终保持最后成功
目标。A不再改变手型。Y在当前唯一F710 owner内复用审核过的全局Home目标与顺序：双臂先用
5秒回H5 Home，随后双手最多8个5Hz changed-only步进回批准Home；完成后重新选侧即可继续，
不退出receiver、不重新初始化GDK。任何按键都不能控制底盘。

## 当前录制状态

`12_g2_powered_f710.py`是Powered手动遥操验收入口，不是DAgger采集器。它记录控制诊断telemetry
和最后成功O10命令用于恢复，但没有三路真实图像及严格的policy/action/feedback训练对齐。
按RB不会自动生成训练episode。

正式DAgger必须把F710 intent接入唯一Control owner。只有START切换POLICY/HIL；RB没有快速
接管入口，松RB仍在HIL，可换侧和操作RT。START退出要求RB松开且手动作完成，之后HOLD，基于
退出后新采集观测请求新policy；旧chunk不能恢复。START不是episode开始键，BACK尚未定义保存语义。

三种旁路录制模式及同源观测、异步payload、离线审阅视频已有离线实现，详见本轮报告；当前
Powered入口尚未接入。HIL暂停推理期间采集仍需持续。视频、时间戳、34D与标签严格审计前，不得
将任何输出标为可训练。现场确认继续只用ENTER。
