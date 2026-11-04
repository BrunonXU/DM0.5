---
doc_type: incident
domain: rtc
status: frozen
created: 2026-08-18
updated: 2026-08-20
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/07_RTC_ROLLBACK_INCIDENT_AND_FIX.md]
superseded_by: []
related: [../RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md]
---

# RTC切换回退事故、二次根因与修复顺序

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | RTC切换新chunk时发生明显回拉，第一次修正切换点后仍存在 |
| 影响 | 真机轨迹不连续，Powered RTC不可放行 |
| 根因 | 第一层是生成假设延迟与真实切换延迟不一致；第二层是state坐标错误和full-50 smoother污染可执行suffix |
| 如何解决 | 使用真实delay/committed prefix，修正state坐标，并把平滑限制在正确处理顺序和可执行suffix |
| 当前状态 | 对应逻辑已修；后续deadline jitter属于另一事故 |

> 首次事故：2026-08-18 15:29  
> 二次分析与本地代码修复：2026-08-20  
> 当前状态：**Powered RTC NO-GO；P0/P1离线完成，尚未同步G2、重启7893或完成P2 Shadow**

本文只处理RTC双臂时序与后处理。双手碰撞、hand V1是否保留、5Hz手部跟踪和空间
防碰撞是独立问题，不与本轮RTC修复混合调参或下结论。

## 1. 先区分Sync卡顿与RTC回拉

NewLora powered-sync每轮只执行20个30Hz区间，即0.667秒，然后hold并同步等待下一次
推理约0.46--0.53秒。其arm原始边界P95约0.024--0.031rad，没有大幅位置回跳证据；
主要体感是高频stop-go。旧时间基线执行49个区间，约1.633秒，因此相同推理等待发生
得约少2.45倍。这是Sync调度问题，不应与RTC seam混为一个问题。

RTC日志则有真实的反向速度和轨迹错位，下面按发生顺序拆解。

## 2. 第一层事故：d=20生成却在d_real=14--16切换

事故日志：
`g2_robot/logs/telemetry/powered/g2_powered_rtc_continuous_20260818_152940.jsonl`。

首次Powered RTC完成9次切换。模型按固定预测延迟`d_hat=20`生成，响应实际在
`d_real=14--16`到达，旧客户端收到后立即从新chunk的14--16索引切入：

- 双臂切换max为0.072--0.190rad，均值0.132rad；
- 9次最大差异轴中6次与旧轨迹末端运动方向相反；
- 双手两次seam达到0.275/0.250rad；
- 120ms boundary blend只把offset摊到约12个100Hz tick，没有消除回拉。

已实现的第一层时序修复是：早到响应继续缓存，旧chunk执行到与服务端相同的逻辑
`d=20`后再切；若响应晚于20但仍在可续接窗口内，则按实际晚到索引切换。不能再把
“按d=20约束的轨迹”提前用于d=14--16。

## 3. 第一层修复验证：C0为0仍然明显往回拉

远端post-fix日志：
`/home/user/xza/DM0.5/logs/telemetry/powered/`
`g2_powered_rtc_continuous_19700101_080200.jsonl`。文件名1970来自当次G2时钟异常，
实际mtime为2026-08-18 15:44:30。

该轮4次响应实际为14/14/15/14步，分别缓存6/6/5/6步，均在
`old[45] -> new[20]`切换，arm/hand C0 seam为浮点零。因此早到缓存与单点C0修复已
生效。

但实际100Hz dispatch仍出现：

- 各段arm最大速度2.65/3.09/3.26/3.98rad/s；
- 最大加速度约180rad/s²；
- 最大100Hz单步0.0395rad；
- 3次`action_dynamics_warning`，但旧Split配置仅warning后继续执行。

所以“C0 seam等于0”不是RTC通过条件；速度方向C1和后续release段必须同时检查。

## 4. 第二层确定根因A：服务端RTC使用错误的state坐标

当前7893运行代码的实际调用链原为：

```text
保存局部physical state
→ input_transform原地把data["state"]替换为normalized state
→ NewLora RTC hook重新读取data["state"]
→ absolute committed prefix - normalized state       # 错误
→ 再按action stats Normalize
```

正确公式必须是：

```text
absolute committed arm prefix - physical absolute arm state
→ 按action stats Normalize
```

普通非RTC输出在末端仍使用此前局部保存的physical state做`ActionAbsolute`，因此普通
推理看起来正常；错误只污染RTC的14D arm prefix。20D hands保持absolute，不直接受
这次减法错误影响。

现有真实HTTP smoke也符合该模式：arm hard-prefix MAE为0.08718rad，而走正确
`absolute_to_mixed(prefix, physical_state)`的离线链路为0.01585rad，约差5.5倍；hands
没有同方向退化。旧测试只验证shape和投影纯函数，客户端二次C0又会掩盖服务端错误。

2026-08-20本地修复：

- 在任何input transform之前复制`absolute_state`；
- 以显式关键字参数传给RTC hook；
- RTC hook不再读取已归一化的`data["state"]`；
- 新增非零、非对称physical/normalized state回归测试，分别断言arm relative与hand
  absolute语义。

## 5. 第二层确定根因B：full-50 smoother污染可执行suffix

旧客户端顺序为：

```text
clip
→ 对完整50步做fixed-endpoint jerk smoother(weight=10)
→ 客户端只把switch单点重新投影为C0
```

服务端错误坐标在raw RTC的19->20产生0.48--0.71rad巨崖。虽然19以前的新chunk前缀
不会被执行，但full-50 smoother把该巨崖扩散进20以后的可执行suffix；客户端随后只
重钉`q[20]`，没有修复`q[21]-q[20]`的速度方向。

post-fix四次切换的左臂`idx24_arm_l_joint4`为：

| 请求 | 旧seam前速度 | raw RTC 20->21 | 旧full50+C0后 |
| --- | ---: | ---: | ---: |
| 1 | +0.423 | +0.774 | -3.194rad/s |
| 2 | +0.305 | +0.704 | -3.189rad/s |
| 3 | +0.127 | +0.845 | -3.023rad/s |
| 4 | +0.188 | +0.070 | -2.808rad/s |

每次14个臂关节中都有7个发生方向反转。该实现也直接违反工程决策中的冻结合同：只
允许平滑未承诺的新后缀，RTC声明冻结的前缀不能再次进入平滑目标。

2026-08-20本地修复后的顺序为：

```text
raw absolute actions
→ 物理限位
→ 客户端防御性C0投影
→ 只取actions[switch_index:]的14D arm suffix
→ 固定suffix[0]为旧轨迹switch位置
→ 固定suffix[1]-suffix[0]为旧轨迹到达switch前的离散速度（C1）
→ 固定suffix末点并做jerk regularization
→ 二次限位和硬安全门
```

`actions[:switch_index]`和全部20D hand维度不进入新的arm smoother。RTC分支不再复用
普通Sync的full-chunk smoother。

## 6. 四组离线递归回放

输入为15:44 post-fix真机日志中的5个完整raw chunks和4次切换。每组都用本组上一轮
后处理结果构造下一轮committed prefix，不把某组的旧轨迹混给另一组。实验仅复现30Hz
客户端确定性后处理；没有重跑模型、100Hz Servo、GDK或hand V1。

产物：`DM0.5/logs/analysis/rtc_postfix_postprocess_groups_20260820.json`。可复现脚本：
`g2_robot/g2_sort/validation/evaluate_rtc_postprocess_groups.py`。

| 分组 | C0 seam max | 速度跳变max | 每次方向反转数 | suffix速度max | suffix加速度max | suffix jerk max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 旧full50 w10 + C0 | ~0 | 3.617rad/s | 7 | 3.194rad/s | 43.83rad/s² | 557.24rad/s³ |
| C0 only，w0 | ~0 | 0.654rad/s | 最高5 | 2.765rad/s | 43.14rad/s² | 1498.46rad/s³ |
| suffix C1，w0 | 0 | 0 | 0 | 2.765rad/s | 58.79rad/s² | 2657.84rad/s³ |
| **suffix C1，w10** | **0** | **数值0** | **0** | **2.802rad/s** | **37.32rad/s²** | **356.27rad/s³** |

结论：

1. `weight=0`能证明full-50 smoother是速度反转的直接制造者，但不是最终方案；
2. 只钉C1、不平滑会把修正搬到后续点，acceleration/jerk反而更差；
3. `suffix C1 + weight10`同时保持冻结prefix、C0/C1和较低后续动态，是本轮唯一进入
   下一阶段的客户端候选；
4. 该日志来自错误服务端坐标，因此约0.92--1.05rad的可执行policy deviation主要反映
   服务端prefix错误，不能当成修复后模型质量；必须在修正服务端后重跑模型离线组。

## 7. 安全门修复

旧`--split-control-profile`同时关闭了首跳、clip和动力学hard reject，导致本次超过阈值
的RTC轨迹只记录warning后仍执行。2026-08-20本地代码增加不变量：

- `powered-rtc`无论是否Split都必须启用hard action safety gates；
- 试图以`enforce_action_safety_gates=false`构造Powered RTC直接失败；
- LOW-LATENCY仍没有GDK空间碰撞保护，日志和操作提示必须继续明确这一点。

这只保证异常轨迹reject/hold，不把RTC变成碰撞检测器。

## 8. 后续顺序与通过门槛

### P0：本地纯逻辑与合同测试

已完成：

- 服务端physical state与normalized state隔离测试；
- RTC suffix不修改冻结prefix或hands；
- suffix首点C0、第二点离散C1、末点固定；
- Powered RTC不能关闭hard action gates；
- 事故日志四组递归回放。

### P1：修复服务端后的模型离线分组

已在GPU 6完成32对固定样本，使用同一75K、冻结观测/state/seed、10步Euler、
`d=20,s=25,approx_identity guidance=0.5`。RTC模型输入由physical state正确构造；上一
chunk先按部署的full50 weight10生成committed prefix，再对同一RTC输出做客户端分组。

产物：
`/mnt/dm05_nvme/dm05/evaluations/NewLora_RTC_v1/adjacent_weight_0p5_32pairs.json`，
SHA256为`6413eaceca55e592975d48dcf6ab865ed308b21fdd6818ee01ff6385f577c5e0`。

模型guidance本身的宏观结果：

- hard-prefix arm MAE：0.01985 -> 0.01680，下降15.4%；
- hard-prefix hand MAE：0.01285 -> 0.00814，下降36.6%；
- raw target arm MAE：0.01831 -> 0.02036，上升11.2%；
- RTC推理均值0.3004秒，与baseline约0.308秒同量级。

客户端分组均值：

| 分组 | C0 seam | 速度跳变 | 方向反转数 | stitched速度 | 加速度 | jerk | executable arm target MAE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline full50 w10 | 0.09047 | 0.1455rad/s | 2.625 | 2.751rad/s | 82.20rad/s² | 4832rad/s³ | 0.02439 |
| RTC旧full50 w10+C0 | 0 | 0.3099rad/s | 5.219 | 0.780rad/s | 10.37rad/s² | 512.6rad/s³ | 0.02761 |
| RTC C0 only w0 | 0 | 0.1736rad/s | 3.406 | 0.683rad/s | 12.42rad/s² | 579.2rad/s³ | 0.02690 |
| RTC suffix C1 w0 | 0 | 数值0 | 0 | 0.692rad/s | 15.00rad/s² | 649.0rad/s³ | 0.02691 |
| **RTC suffix C1 w10** | **0** | **数值0** | **0** | **0.653rad/s** | **7.38rad/s²** | **221.4rad/s³** | **0.02693** |

这组结果确认：

1. 即使服务端坐标正确，旧full50 smoother仍把平均方向反转数从C0-only的3.4放大到
   5.2，两个bug相互独立；
2. suffix C1是消除边界反转的必要条件；weight10进一步把C1-only的后续jerk从649降到
   221rad/s³；
3. guidance 0.5不是免费收益：executable arm target MAE相对baseline约高10.4%，fresh
   arm target MAE约高2.5%。下一阶段不能只看连续性，还要确认视觉纠错没有被抑制；
4. `suffix C1 + weight10`通过P1，其他RTC客户端组淘汰。

本轮正式分组为：

1. baseline非RTC；
2. RTC旧full50 weight10 + C0；
3. RTC C0 only；
4. RTC suffix C1 w0；
5. RTC suffix C1 w10。

HTTP路径仍必须在P2证明与离线`absolute_to_mixed`数值一致，不能只验证返回shape。

### P2：Mock与RTC Shadow

6000D临时7894服务的真实HTTP smoke已完成，使用同一75K、guidance 0.5和归档三相机
观测；临时容器测试后已删除。产物：
`DM0.5/logs/http_smoke_postfix_20260820.json`。

- 返回shape仍为50×34；baseline/RTC分别约0.631/0.350秒；
- switch arm max为0.000542rad，hand为0；
- hard-prefix arm MAE由修复前smoke的0.08718降到0.04974，hand为0.000795。

hard-prefix整体不是硬投影：guidance 0.5、随机采样和action quantile clip都会影响该MAE，
所以不能把0.04974解释为坐标修复失败，也不能宣称服务端整段prefix精确相等。physical
state与normalized state的转换正确性由P0确定性回归测试保证；HTTP smoke负责验证真实
wrapper、反归一化和switch投影贯通。

2026-08-20随后重启正式`NewLora-dm05-http-75k-rtc`容器，使7893进程实际重新导入修正
后的服务代码；基线7892未改动。正式7893 smoke产物为
`DM0.5/logs/http_smoke_deployed7893_20260820.json`：

- baseline/RTC耗时约0.668/0.336秒；
- 返回shape为50×34；
- switch arm max为0，hand max为0.000039rad；
- hard-prefix arm/hand MAE为0.04266/0.000671。

RTC客户端的`app.py`、`control_loop.py`、`trajectory_smoothing.py`、对应测试、09入口和
运维文档已逐文件同步到G2 `/home/user/xza/DM0.5`。同步后在G2仅执行无GDK检查：
Python编译、shell语法通过，`tests/test_g2_client_runtime.py`为`48 passed`；未启动GDK、
Servo、Shadow或Powered进程。

15:22首次G2 30请求Shadow日志为
`logs/telemetry/shadow/g2_rtc_shadow_20260820_152246.jsonl`。29/29 RTC请求调度可行，
`d_real`分布为14步15次、15步10次、16步3次、18步1次；推理P50/P95为
0.442/0.488秒，无超时、乱序或`shadow_dynamics_warning`。这证明真实观测、HTTP服务和
调度窗口稳定。

但代码复核发现该版Shadow仍对完整50步使用普通smoother并把`used_start_index`设为
`d_real`，没有覆盖Powered的“早到缓存至20、C0投影、suffix-only C1”路径。因此这轮
Shadow不能验证本事故的核心修复，也不能放行Powered。2026-08-20已改为Shadow与Powered
共用同一纯后处理函数，并新增`switch_delay_steps`、`response_buffered_steps`、arm/hand
seam、arm velocity jump和direction reversal遥测；本地全套`85 passed`，同步后G2目标
测试`49 passed`。

P2只剩重跑部署一致的G2 Shadow：

- 由Bruno在G2本机终端运行`rtc-shadow`，不创建Servo；
- 校验`d_real`分布、乱序/超窗hold、server/client timing和所有安全门事件；
- Shadow通过前不得执行Powered。

### P3：首次Powered复验

P0--P2全部通过后，仍只允许Bruno在G2终端手动运行10秒，专人握物理急停。至少满足：

- `switch_delay=20`，提前响应缓存到相同时间基；
- arm C0接近浮点零，C1速度跳变不高于批准的离线/Sync门槛；
- 无arm方向反转、clip/start/dynamics warning或reject；
- 无Servo/GDK/HTTP故障；
- 人工确认无回拉后才能讨论continuous。

2026-08-20 15:29在P2重跑前误以`--continuous`启动两轮，均已人工停止。日志：

- `g2_powered_rtc_continuous_20260820_152910.jsonl`；
- `g2_powered_rtc_continuous_20260820_152959.jsonl`。

共4次已完成切换均为`switch_delay=20`、提前缓存5--6步、arm/hand C0 seam为0；实际
100Hz双臂boundary first delta最大0.0000059rad，blend-window最大速度约1.10rad/s，
没有复现事故中的反向拉动。第二轮request 3随后被hard gate拒绝：左手中指实测状态到
目标的start jump为0.316rad，超过0.05rad门限；此前5Hz手命令单步最大0.234rad。安全门
正确hold，但整机因此仍为NO-GO。该结果只初步通过双臂RTC连续性，不批准continuous或
完整双手10秒复验；下一步先隔离arm RTC与hands V1链路。

隔离方案已实现为`g2_robot/scripts/10_powered_rtc_arms_only_10s.sh`：固定最多11次请求、
约10秒，不接受参数或continuous；硬件`servo-output-mode=arms-only`只发送14D双臂，
不创建手线程、不调用`move_ee_pos`。安全门新增`executed_action_dim=14`，因此start jump、
clip和动力学reject只评价实际会执行的双臂；未执行的20D hand仍保留在模型请求、RTC
prefix与诊断遥测中。原`09_powered_rtc.sh`已硬封锁，避免再次误启动完整双手。

该arms-only轮于15:40执行完成，日志为
`g2_powered_rtc_arms_only_10s_20260820_154052.jsonl`。11个chunk、10次切换均无reject、
超时、乱序或Servo故障；`d_real=14--19`，全部缓存到`switch_delay=20`，C0 seam为0，
hand send count始终为0。操作者仍观察到1--2次回退。

按部署代码递归重建postprocessed suffix后，所有切换的C1速度跳变均为数值0，但新suffix
随后会改变方向。request 6在切换后10个30Hz点内有8/14个臂关节相对入射方向反转，
左臂joint4反向位移约0.386rad；request 5/7/9的最强反向位移约
0.126/0.174/0.120rad。request 6实际100Hz步进P95/max为0.0216/0.0283rad，blend-window
速度约2.19rad/s。结论是旧时间基、C0和C1问题已修复，剩余回退来自新观测后的policy
意图改向；继续加大平滑只会延迟该改向，不能解决其语义。

下一阶段采用interaction-aware dynamic RTC，而非按时间门控：free-space允许RTC；抓取、
双手交接和放置的近距离窗口进入precision sync/hold。门控必须同时使用空间交互风险
（手/物/另一只手/目标区距离、夹爪开合意图、持物状态）和新旧计划分歧（task-space
correction、prospective reversal、动态上界），并用hysteresis避免模式抖动。

## 9. 手部V1明确分离

本轮不修改V1，也不以RTC实验决定V1去留。当前已知RTC入口固定V1 off、手线程5Hz可能
跳过120ms Servo blend、Sync 20/50又会每0.667秒重置V1边界阶段。手部需要单独做
`20/49 × V1 on/off × checkpoint`实验，并增加Cartesian fingertip/hand-hand最小距离；
在这些证据完成前不能宣称V1应删除或保留。
