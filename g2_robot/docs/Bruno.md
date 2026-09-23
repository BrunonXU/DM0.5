# Bruno：当前进度与下一步

> **现场历史页：** 本页保留当时操作进度，其中“双GDK Preflight”等内容已经被
> 2026-08-24单Robot-owner决策替代。跨服务器与G2的当前权威状态见
> [`../../DM0.5/docs/CURRENT_STATUS.md`](../../DM0.5/docs/CURRENT_STATUS.md)。后续只在这里写尚未归档的现场临时状态。

更新时间：2026-08-21。

## 最新状态：RTC进程隔离待现场Preflight

线程版RTC调度器追赶bug已修复，但最新RTC/Sync日志的最大控制间隔为24.14/14.96ms，
`>15ms`间隔为68/0；RTC长间隔与三相机/JPEG周期重合。相机/JPEG `sleep(0)`实验反而
退化，已撤回。

2026-08-24架构已升级为单Robot owner：控制进程保留完整Split双臂100Hz+双手5Hz、
RTC时钟、安全门、Hold和唯一Robot state reader；spawn策略worker只执行三相机、共享
state ring读取、OpenCV、JPEG、7893和JSON，不创建Robot，图片不进入控制进程。09现
自动先运行11单Robot-owner Preflight，失败则不会进入Powered授权。
无GDK process/HTTP/RTC完整装配与既有客户端相关测试为66 passed，全仓104 passed；
真机尚未运行新代码。

下一步在G2终端执行：

```bash
cd /home/agi/xza/DM0.5
./scripts/11_rtc_process_isolation_preflight.sh
```

只有结果`ok=true`、`policy_robot_reader=false`、Control state读取大于0且命令发送为0，
才运行完整双手10秒：

```bash
./scripts/09_powered_rtc.sh --duration-seconds 10
```

09本身也会重新自动执行11。现场必须清空工作空间并由专人握住物理急停。首轮验收重点是
`>15ms`间隔接近0、最大间隔候选门≤15ms、无追赶补发/worker错误/schedule miss；不能只看
任务是否完成。

## 12:03后RTC平滑消融：推荐cubic C1 + 稳态0ms blend + hand cap V2

最新100秒轮由操作者在约27秒因手指接近碰撞主动停止；39个chunk、38次切换，无服务、
Servo或安全门故障。C0 seam全部为0，但100Hz下发内部jerk显著高于同日Sync。递归离线
消融定位到30Hz线性插值的waypoint换挡，以及RTC C1之后重复120ms从静止boundary blend
两个主因。推荐组保留suffix weight10，改为带旧计划入口速度约束的C1三次Hermite，
稳态RTC blend关闭；相对当前回放jerk P95/最大值P95降低约81%/86%，加速度P95降低74%，
物理限位违反为0。三份8月20日志也得到同方向结果。

双手off的边界P95为0.215rad、全局最大步0.425rad。新增`rtc_cap_v2`只限制每个RTC段首条
0.15rad、内部0.30rad，不做旧V1的反转hold和时序改写；离线边界P95降至0.15rad、最大
步0.30rad且P95时序偏移为0。实现和09入口已更新，完成无GDK回归后只允许先跑10秒并
拉日志。完整证据见`RTC_SMOOTHING_ABLATION_20260821.md`。

随后新增的`120701`和`121312`旧配置日志将样本扩至101次切换，推荐组仍降低
80.7%/86.5%的jerk P95/最大值P95。`120701`因原始手部30Hz jerk 5564触发旧hard gate，
与实际5Hz V2下发不一致；Powered RTC动作动力学现按现场要求改为warning-only，避免
再次提前结束。物理限位、HTTP、Servo/GDK故障不变。

## 11:56 两次完整Split RTC提前结束的根因与修复

最新两轮`g2_powered_rtc_10s_20260821_115625.jsonl`和
`g2_powered_rtc_100s_20260821_115716.jsonl`分别只完成5和4个action chunk。第二轮
确实收到100秒配置，入口据此给出约151个最大请求，并没有把时长或请求数写死；它在
request 4因左手中指PIP的物理反馈与RTC逻辑边界相差0.4815rad、超过旧0.35rad手部门
限而进入safe hold。第一轮同理，在request 5因左手小指PIP相差0.6709rad结束。两轮
双臂首跳只有0.0018/0.0033rad，HTTP与模型耗时也正常。

根因是旧实现把物理首跳检查用于bootstrap和每次RTC重规划。稳态RTC的新chunk已经被
C0投影到旧chunk的已承诺逻辑前缀，O10约5Hz反馈却滞后于30Hz逻辑轨迹；手部差值可
代表跟踪滞后，不等于RTC生成了接缝。按现场测试决定，完整Powered RTC现对bootstrap
和稳态重规划的双臂0.05rad、双手0.35rad首跳均只记录`start_jump_warning`而不终止；
稳态手部超限另记`rtc_hand_feedback_gap_warning`便于识别。首跳保护因此不再代替操作者
急停；clip、动力学、HTTP、Servo/GDK故障hard gate不变。

## 当前现场：完整Split RTC已切到d16/s20，7893五步compile已预热

32对冻结验证样本、相邻噪声耦合、五步、`approx_identity/0.5`的离线分组中，
`d=16,s=20`的C0+C1+suffix weight10结果优于`18/22`和`20/25`：平均双臂最大
速度/加速度/jerk为0.488rad/s、4.25rad/s²、125.6rad/s³。完整Split入口09已改为
该参数并解除旧arms-only软件封锁；双臂首跳hard gate保持0.05rad，双手单独为
0.35rad，clip和动力学hard gate保持。

7893已切为五步fixed-prefix compile并完成18.841秒首次预编译。预热后归档HTTP
smoke的baseline/RTC总耗时0.185/0.159秒，模型0.140/0.139秒，switch arm max为0、
hand max为0.0000477rad，多次baseline/RTC请求无recompile。6000D容器无GDK定向测试
为16 passed。下一步由Bruno在G2终端手动运行09的有限时长完整双臂+双手RTC，专人
握住物理急停；结束后立即拉取日志，不直接转continuous。

## 当前现场：7892五步eager稳定；03改为先臂后手

7892和7893当前均为五步fixed-prefix `torch.compile`推理，不降低三路图像质量。
正式7892已在本机完成首次18.825秒预编译，后续44组变化state请求P50/P95/max为
0.160/0.162/0.168秒，模型均值0.136秒，零recompile；G2端口检查为预期405。此前五步
eager的最新30秒Powered同步运行45/45请求成功：端到端HTTP延迟
P50 0.351秒、均值0.371秒、P95 0.508秒；模型主体稳定在约0.221秒，长尾主要来自
服务端请求解析/上传阶段。旧compile事故已定位为十进制state token数量变化导致prefix/KV
shape变化；新实现只在compile模式把tokenized prefix右补到训练时相同的1024长度，有效
token和图片不变。下一步由Bruno手动跑同步真机并立即分析Powered日志；如出现HTTP秒级
尖峰或timeout，停止运行并回退eager。

`scripts/03_reset_home.sh --execute`已改为严格的“先双臂、后双手”：双臂回H5初始
Home期间Split手线程输出闸门关闭，不向O10发送命令；双臂播放完成且Servo无故障后才
打开闸门，先重发最后成功手指命令作锚点，再以5Hz最多八步回批准Home。默认生产控制
路径保持原行为。6000D无GDK测试为77 passed；尚未真机验证，任何03执行仍只能由Bruno
在G2终端手动完成，并由专人握住物理急停。

## 当前状态：Powered RTC NO-GO，先完成离线分组

首次Powered RTC完成9次切换，实际`d=14--16`却立即切入按`d_hat=20`生成的新chunk，
双臂接缝max为0.072--0.190rad（均值0.132rad），产生肉眼可见的往回拉。修复后提前
响应缓存到逻辑20步再切后，4次C0 seam均为0，但仍出现2.65--3.98rad/s实际arm速度
和明确反向拉动。

2026-08-20确认两个更深的实现错误：

1. 7893服务先把`data["state"]`归一化，再拿它与absolute committed prefix做
   `ActionRelative`，直接污染14D arm RTC条件；
2. G2客户端对完整50步做weight10 smoother，再只重钉单点C0，把服务端19->20巨崖
   扩散到可执行suffix并制造约-3rad/s反向速度。

本地代码已改为显式使用physical absolute state，并只对未承诺arm suffix执行C0/C1
约束和jerk平滑；Powered RTC现在强制hard action safety gates。15:44事故日志四组递归
回放中，旧链路速度跳变最大3.617rad/s、每次7个方向反转；`suffix C1 + weight10`组
速度跳变和反转均为数值0，30Hz suffix jerk最大值由557降至356rad/s³。32对固定75K
模型分组也得到同方向结论：该组零速度跳变/零反转，平均stitched arm jerk为221rad/s³，
但arm target MAE相对baseline约高10%，后续Shadow必须同时观察连续性和视觉纠错。

6000D临时7894真实HTTP smoke已通过50×34和RTC字段，switch arm max 0.000542rad、hand
为0；临时容器已删除。2026-08-20正式7893也已重启并加载修正代码，其HTTP smoke
baseline/RTC耗时约0.668/0.336秒，switch arm max为0、hand为0.000039rad。

这些仍不是修复后真机结论。15:22首次30请求Shadow的服务与调度均稳定：29/29 RTC
请求可行，`d_real=14--18`，无超时、乱序或动态warning。但检查日志时确认当时Shadow
仍使用旧full-50 smoother并按`d_real`分析，没有覆盖Powered实际采用的固定`d=20`、
C0和suffix C1，因此该轮不能作为Powered放行依据。

Shadow已修为与Powered共用同一后处理函数，并新增记录`switch_delay`、buffered steps、
C0 seam、C1速度跳变和方向反转数。更新已同步G2；G2端无GDK测试现为`49 passed`。
下一步严格为Bruno在G2本机终端重跑RTC Shadow；通过后才允许讨论手动10秒Powered。
手部V1另案处理，本轮不修改也不决定去留。完整证据见NewLora
`07_RTC_ROLLBACK_INCIDENT_AND_FIX.md`。

G2本机下一条命令（固定约10秒，只发送14D双臂，完全不发送双手）：

```bash
cd /home/agi/xza/DM0.5
./scripts/10_powered_rtc_arms_only_10s.sh
```

运行前清空双臂工作空间并由专人握住物理急停；脚本自身检查残留控制进程。运行结束
保留`g2_powered_rtc_arms_only_10s_*.jsonl`路径，先分析日志。`09_powered_rtc.sh`已被
代码硬封锁，禁止完整双手或continuous。

### 15:29误运行continuous后的状态

`--continuous`表示无限运行，不是10秒。两轮误运行均已停止，共得到4次真实RTC切换：
全部`switch_delay=20`、提前缓存5--6步、arm/hand seam为0；双臂边界首步最大仅
0.0000059rad，未复现旧版反向回拉。这是双臂RTC修复生效的正证据。

但第二轮request 3因左手中指start jump 0.316rad超过0.05rad hard gate而reject并hold；
reject前手部实际5Hz单步最大0.234rad。当前整机继续NO-GO，禁止再次运行continuous，
也不批准完整双手的10秒Powered。下一步应先把RTC双臂验证与hands V1问题隔离。

### 15:40 RTC arms-only 10秒结果

日志`g2_powered_rtc_arms_only_10s_20260820_154052.jsonl`完成11个chunk和10次RTC切换，
无reject、超时、乱序或Servo故障；`d_real=14--19`且全部缓存到`switch_delay=20`，C0
seam为0。双手send count全为0，确认硬件隔离生效。

操作者仍感到1--2次回退。递归重建实际C0/C1 suffix后确认它不是边界bug：request 6
在切换后未来10个30Hz点内有8/14臂关节相对入射方向反转，左臂joint4反向位移约
0.386rad；request 5/7/9同一指标约0.126/0.174/0.120rad。C1只保证第一个速度连续，
不能阻止新观测下的policy在随后0.2--0.33秒改变意图。后续方向转为interaction-aware
dynamic RTC：接触/近距离阶段进入precision sync，free-space才允许RTC，并以空间交互
风险与新旧计划分歧双门控；不得按固定时间切换。

## 历史状态：2026-08-17 NewLora 75K Powered NO-GO

停止继续运行`04_powered_guarded_smoke.sh`、`05_powered_sync.sh`和continuous。6000D
75K HTTP服务本身可以保留用于Shadow；当前G2没有残留VLA控制进程。

用户报告三轮中两轮接近碰撞：左臂不直接抓雪碧/橙汁，而向下压黄色纸盒；交接阶段
手指多次将要撞击并由人工紧急停止。对应新日志：

- `g2_powered_split_sync_10s_20260817_170314.jsonl`；
- `g2_powered_split_sync_120s_20260817_170725.jsonl`；
- `g2_powered_split_sync_continuous_20260817_171907.jsonl`。

## 已确认事实

三轮都使用同一NewLora 75K服务：
`NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000`、10步Euler、
14D手臂relative+20D手absolute。每个新请求都从index 0执行20/50，无旧chunk跳过。

控制层：

- 双臂120ms cubic边界融合和内部jerk weight10均真实启用；
- 120秒轮关闭手部V1；continuous轮启用手部V1；
- 双臂约100Hz、手部约5Hz，维度与finite合同正常；
- 无Servo/GDK、HTTP、观测、shape或映射错误；
- 双臂平滑相对模型原始轨迹最大只改0.00603rad（120秒）和0.00864rad（continuous），
  不能解释明显错误的抓取方向；
- 当前日志没有Cartesian末端位姿和逐request图像，所以不能从日志直接数值重建“Z向下”；
  但错误方向已存在于模型原始14D目标，而非平滑器制造。

120秒轮在request 64取得chunk后、完成dispatch前硬中断，只有64个chunk完整播放：

- 42/65个raw chunk在前20步内手指变化超过0.4rad，22个超过0.5rad；
- 45/64个实际5Hz dispatch存在大于0.2rad单步，25个大于0.3rad，11个大于0.4rad；
- 最大实际手指单步0.5057rad；手部V1为off。

continuous轮在request 34中途停止，共35个chunk：

- raw手指前20步14次超过0.4rad、5次超过0.5rad；
- request 23/24/25/31出现手指raw jerk超过5000rad/s³；
- V1将实际单步限制到不超过0.4rad，但发生4次cap、40次方向反转hold；
- 最大手部跟踪滞后0.3553rad，说明过滤器在追赶快速反转的模型目标；
- V1只能缓解单步，不能把错误交接语义变成安全轨迹，还可能改变开合时序。

正式Sync当前`enforce_action_safety_gates=false`：所有chunk都有超过0.05rad的
`start_jump_warning`，动态超限也只是warning，仍继续下发。low-latency通道没有GDK
碰撞保护。这是危险动作没有被软件挡住的确定集成原因。

## 模型/数据判断

当前最符合证据的组合原因：

1. 测试场景的黄色纸盒、瓶位、光照和腕部遮挡与训练支持不同；
2. 数据瓶位窄、全是成功轨迹、没有错误目标/碰撞/抓空恢复示范；
3. 官方几何augmentation保持原动作标签，会鼓励对小幅像素位置变化不敏感，不能扩展
   真实3D瓶位支持；
4. 75K held-out FM在35K后已退化，但完整动作MAE继续改善，可能得到更确定、更像示范
   的宏轨迹，却没有更好的视觉目标选择；
5. 现有E6只证明对轻微crop/rotate/color扰动稳定，不能证明模型按瓶子位置做视觉定位。

因此不能把问题归为机器人本体，也不能认为换成70K就会自动解决；70K/75K的离线视觉
增强结果基本并列。

## 完整训练数据双臂回放已就绪（尚未真机执行）

已冻结训练集`sorting_20260409161901`全部948帧（31.567秒），脚本
`scripts/07_training_data_replay.sh`完全绕过DM0.5，只以normal碰撞保护回放14D双臂；
双手不下发，V1不参与。源JSONL SHA256为
`54a253329827dda9687d017f578b562006476e7dc7b5f06f367795c16a1facfa`。
6000D纯逻辑测试3项通过，四个运行文件已逐一同步到G2。

此实验只判断示教双臂轨迹在本体上的方向、映射和跟踪；后段含交接臂姿，必须清空
桌面并全程握物理急停。若示教回放正确而75K仍压纸盒，主因指向模型/视觉泛化；
若示教也错误，转查数据、关节映射和控制链。

## 下一步

1. 保留75K服务仅做Shadow，停止Powered。
2. 先把正式Sync的手臂/手指首跳与动态warning改成hard reject，并为低延迟无碰撞保护
   增加显式NO-GO门；修改前不再真机。
3. 在G2客户端增加每request三图、34D state、raw/filtered chunk的诊断快照，先Shadow
   重现黄色纸盒场景。
4. 固定当前场景对35K/70K/75K做原图、黑图、错配图和瓶位移动Shadow；量化前20步手臂
   目标是否随瓶位系统变化，再决定checkpoint。
5. 模型仍不随瓶位变化时，结论是数据/视觉grounding不足，需要补更宽瓶位、纸盒场景和
   失败恢复数据，而不是继续放宽控制器安全门。
