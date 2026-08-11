# G2 × DM0.5 上机操作指南

这是现场人员唯一必须先看的文档。

## 1. 固定配置

- G2项目目录：`/home/agi/DM0.5`
- 6000D：`10.130.153.114`
- 当前模型：75K checkpoint
- 当前推理接口：`http://10.130.153.114:7892/process_frame`
- 模型合同：三路RGB + 34D state → `50×34` absolute action，约30Hz时间语义
- 当前硬件下发：Split
  - 双臂前14D：100Hz `joint_servo_control`
  - 双手后20D：5Hz `move_ee_pos(dual_tool, o10_t2)`
- 当前默认调度：sync

模型层仍是34D，但硬件层绝不把34D重新合成一次Servo请求。历史统一34D Servo在
这台G2上反复产生20个O10 `Param ... not found`，并发生过2.53秒阻塞和watchdog。

Split中的每个100Hz逻辑tick先生成完整有限34D目标，硬件适配层再发送固定的
14个双臂`joint_names`和14个`joint_positions`，`control_period=0.01s`。
`joint_velocities=[]`是GDK 2.6.3保留字段的合法取值，不代表位置命令缺失。后20D
不会混入Servo请求，而是由独立5Hz线程构造`nums=20`的双手命令。最新
`dispatch_summary`会逐chunk记录14D完整性、理论/实际100Hz覆盖率、有效频率、
最大发送间隔及边界窗口实际动力学。

## 2. 安全底线

- G2域控禁止执行任何`apt`、`apt-get`及其`sudo`形式。
- 专人全程握住物理急停；软件按键不能代替物理急停。
- 不同时运行两个G2控制进程。
- 当前正式Split的双臂使用low-latency，没有GDK碰撞保护。
- `02_servo_hold.sh --execute`、旧H5复位和拇指微动execute均已软件锁定。
- 唯一批准的复位入口是`03_reset_home.sh --execute`：先恢复O10双手，再恢复双臂14D。
- Powered结束只停止发送，不会自动复位。

首次部署或覆盖脚本后，终端执行：

```
cd /home/agi/DM0.5 || exit 1
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

默认10秒模型动作：

```
./scripts/05_powered_sync.sh
```

指定约45秒模型动作：

```
./scripts/05_powered_sync.sh --duration-seconds 45
```

输入`ok`后倒数5秒开始。该入口固定：

- 75K/7892；
- Split硬件下发；
- 双臂low-latency 100Hz；
- 双手`move_ee_pos` 5Hz；
- 120ms cubic boundary smoothstep（正式默认）；可用
  `--boundary-blend-profile quintic_smootherstep`做严格单变量A/B；
- 双臂chunk内部固定端点jerk正则权重10；
- 双手5Hz启用V1：400ms cubic边界guide、持续速度/加速度追赶、大幅反转保护和
  0.40rad最终单步cap；
- 首跳、裁剪幅度和动力学超限记录告警但不拒绝chunk；
- shape、finite、物理关节限位、HTTP/GDK失败停止仍然有效。

一个chunk的50个waypoint形成49个区间，动作时间约`49/30=1.633秒`。同步模式在
chunk结束后hold，再采集并等待约0.47～0.52秒的新动作。因此可见的周期性停顿来自
同步HTTP等待，不代表100Hz Servo线程降频。

新版本在`action_chunk.request_timing_seconds`和`run_summary.request_timing`中分段
记录相机、状态、JPEG、HTTP、服务端输入transform、模型+回传CPU、输出transform和
未归因传输耗时。该字段只读，不参与动作接受、拒绝或Servo下发。延迟优化先运行
Shadow采样，禁止为了测时重新启用Async。

### 4.1 已冻结的120ms + weight 10平滑基线

两轮powered、共47个实际chunk已经确认这组配置稳定：内部chunk最大步进P95离线
反事实下降约11.5%，内部jerk P95下降约70%，未出现Servo故障、safe hold、裁剪或
动力学告警。因此正式Sync入口已经固定注入120ms和权重10，日常运行不再需要手写
这两个参数：

```bash
./scripts/05_powered_sync.sh --duration-seconds 45
```

低层Python CLI仍以0关闭内部平滑；Async实验入口也继续使用自己的0.06/0默认，
不会自动继承正式Sync配置。只有需要显式回退对照时才运行：

```bash
./scripts/05_powered_sync.sh \
  --duration-seconds 45 \
  --boundary-blend-seconds 0.06 \
  --arm-internal-jerk-weight 0
```

新jerk正则只处理双臂14D。手部不是“完全没有连续化”：34D逻辑目标先按模型30Hz
waypoint做线性时间采样，但Split手线程只以5Hz取当时最新20D目标并调用
`move_ee_pos`，两个命令之间没有客户端显式100Hz手部轨迹。手部没有使用同一个
weight 10，因为其正常开合幅度和接触时序与双臂不同；全chunk双向正则可能造成提前
闭手或延后张开。手部若继续优化，应单独评估5Hz边界ramp或有界变化量。

截至2026-08-05，400ms cubic手部边界guide、因果速度/加速度跟踪和cap40组合已作为
首个可回退候选接入正式Sync入口，但尚未完成首次真机验证。第一次只允许10秒空场景，
专人握住物理急停。显式回退手部、保留双臂冻结配置：

```bash
./scripts/05_powered_sync.sh \
  --duration-seconds 10 \
  --hand-smoothing-profile off
```

第一条手部命令没有本进程内可信成功锚点时不做V1衔接；不会把重启后的20D零反馈当作
起点。从第二个chunk开始使用本进程上一条已确认成功命令作为边界锚点。

## 5. 异步实验

```
./scripts/06_powered_async_experimental.sh --duration-seconds 10
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

有限时长不是固定chunk上限：例如45秒会按最接近目标时长换算为
`round(45×30/49)=28`个请求，因为50个waypoint只有49个30Hz运动区间。若确实需要
无请求上限，使用`./scripts/05_powered_sync.sh --continuous`；该模式不会自动识别
任务完成，禁止无人值守。

SSH终端丢失时，在另一个G2终端执行：

```
cd /home/agi/DM0.5 || exit 1
./scripts/stop_runtime.sh
```

脚本只向核验过命令行的客户端PID发送SIGINT，不使用`kill -9`。

## 8. 每轮结束后的复位

每次VLA都是独立运行，不恢复旧chunk。固定流程：

```text
VLA终端按q完全退出
→ 托住手中物体
→ Split复位灵巧手和双臂
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
`move_ee_pos`新会话已被真机证明会拒绝有效20D命令，因此脚本复用正式Split：双手
先以5Hz最多8步回批准Home，随后双手保持Home、双臂以100Hz在默认5秒内回到800条
H5第0帧14D中位数。若VLA仍在运行、
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
cd /home/xza_100850113/projects/DM0.5/g2_robot || exit 1
./scripts/pull_new_g2_logs.sh
```

该脚本递归检查G2的`/home/agi/DM0.5/logs/`，把新增日志拉到6000D当前仓库的
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

## 10. 代码同步原则

6000D与G2已在同一公司内网。只同步代码、脚本、文档和测试，不同步6000D历史
`logs/`、`.pytest_cache`、`__pycache__`、旧tar包或SHA文件。所有真机命令仍由
Bruno在G2终端手动执行，6000D不得远程启动GDK动作。

6000D终端在`g2_robot`根目录执行：

```
scp -r AGENTS.md README.md g2_sort scripts docs tests agi@10.130.86.13:/home/agi/DM0.5/
```

同步是覆盖/合并，不会删除G2上的历史`logs/`。首次切换新脚本名后，只在G2终端
清理两个明确废弃入口：

```
cd /home/agi/DM0.5 || exit 1
rm -f scripts/04_powered_sync.sh scripts/05_powered_legacy_10s.sh
chmod +x scripts/*.sh
```
