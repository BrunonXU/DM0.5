---
doc_type: experiment
domain: inference
status: frozen
created: 2026-08-20
updated: 2026-08-20
models: [new_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/08_NEWLORA_DENOISING_LATENCY_AND_ACCELERATION.md]
superseded_by: [../INFERENCE_LATENCY_FIRST_PRINCIPLES.md]
related: []
---

# NewLora去噪步数、端到端延迟与RTC影响

> 日期：2026-08-20  
> 模型：NewLora 75K，50×34，30Hz  
> 当前线上：7892非RTC Sync使用Euler 5步、eager action expert、服务端三图并行解码  
> RTC基线：7893仍保持Euler 10步和`d=20,s=25,approx_identity weight=0.5`，本次不修改  
> 当前结论：**真实G2输入触发`torch.compile`反复约19秒重编译，compile已从线上配置回退；5步和并行解码保留**

本文只处理推理延迟和去噪求解器。动态RTC、双手V1和空间碰撞保护仍是独立问题。

## 1. 为什么延迟是当前RTC的上游矛盾

RTC不能恢复推理期间没有看到的环境信息。当前真机请求需要约0.47--0.58秒，换算为
30Hz控制点约14--18步，个别请求达到19步。服务端按`d=20`构造冻结前缀后，
`H=50,s=25`只剩：

```text
soft overlap = H - s - d = 50 - 25 - 20 = 5 steps
```

切换后的旧意图保护区只有约167ms，之后为模型根据新图像自由生成的后缀。2026-08-20
arms-only真机日志已经证明，剩余的1--2次“回退”不是C0/C1接缝：第6次请求的raw RTC
未来10步中有8/14个臂关节反向，最大反向位移约0.386rad。C0、C1和suffix smoother
只能连续地执行这个新意图，不能把它改回旧策略。

因此降低端到端延迟同时改善两个问题：

1. 新chunk基于更近的观测，减少遮挡和任务相位变化造成的意图切换；
2. `d`下降后，`H-s-d`自然增大，RTC有更多软重叠来保持跨chunk策略一致。

目标不是仅让平均值低于400ms，而是最终让G2端到端P95进入约300ms，即`d<=9--10`。

## 2. 当前约400--500ms从哪里来

证据日志：
`/home/agi/xza/DM0.5/logs/telemetry/powered/`
`g2_powered_rtc_arms_only_10s_20260820_154052.jsonl`，11个请求。

| 阶段 | mean | P50 | P95 | 说明 |
| --- | ---: | ---: | ---: | --- |
| G2相机采集 | 23.2ms | 14.7ms | 53.9ms | 三路相机获取 |
| G2 JPEG编码 | 26.9ms | 26.1ms | 39.4ms | 在HTTP请求前发生 |
| 服务端请求解析/图片打开 | 72.4ms | 74.4ms | 84.8ms | `_prepare_input` |
| 服务端输入变换 | 17.8ms | 17.5ms | 22.2ms | resize/tokenize/normalize/device |
| **模型+device-to-CPU** | **326.1ms** | **322.4ms** | **345.3ms** | 目前最大单项 |
| 服务端完整handler | 417.1ms | 420.7ms | 431.8ms | 上述服务端阶段总和 |
| HTTP未归因部分 | 41.9ms | 17.5ms | 134.7ms | LAN、排队、框架与抖动 |
| **客户端backend总时间** | **489.4ms** | **469.7ms** | **586.4ms** | 真正影响RTC的端到端请求时间 |

当前400ms不是一个单独的“网络问题”。模型约326ms是最大项，但服务端图片准备、输入
变换和网络尾部合计也超过100ms。只减去噪步数不会让总延迟同比减半。

## 3. GPU内部性能拆解

新增只读离线剖析器：
`g2_sort/validation/profile_NewLora_inference.py`。它在同一RTX 6000D、同一模型、同一
输入和相同初始噪声上交错执行10步与5步；先warmup 3次，再各测12次。CUDA event
forward hook分别记录vision tower、language prefix和action expert，不在单步之间同步。

产物：
`/mnt/dm05_nvme/dm05/evaluations/NewLora_denoise_steps_v1/`
`profile_steps10_5.json`，SHA256
`15ed4d61bff548eb2b98493289bd5991a71303922566d6884897b8fb758709ec`。

| GPU阶段 | Euler 10 | Euler 5 | 变化 |
| --- | ---: | ---: | ---: |
| GPU总时间 | 309.40ms | 208.65ms | **-32.6%** |
| Vision tower | 28.80ms | 28.79ms | 不变 |
| Language/VLM prefix | 67.10ms | 67.08ms | 不变 |
| Action expert总计 | 199.68ms | 100.61ms | **-49.6%** |
| 每次action expert | 19.97ms | 20.12ms | 基本不变 |
| Action expert调用数 | 10 | 5 | -50% |

结论：每减少一个Euler步，当前GPU大约节省20ms；固定的视觉和语言前缀约96ms不能由
减少去噪步数消除。单独5步预计只能从真实端到端均值中拿回约100ms，因此继续叠加
编译和图片解码优化。第10节给出最终真实HTTP回放结果。

## 4. Euler 5与Euler 10动作质量

冻结验证使用同一75K、80个episode、320个唯一观测、每个观测2个固定seed。10步结果
沿用正式75K验证产物；5步结果为：

`/mnt/dm05_nvme/dm05/evaluations/NewLora_denoise_steps_v1/actions_steps5.json`，
SHA256 `4983e37da3f365400ea69fec8b6c428e7743c8f2f2fb76ab88c5dbda36aa97bf`。

| 指标 | Euler 10 | Euler 5 | 5相对10 |
| --- | ---: | ---: | ---: |
| physical MAE overall | 0.010842 | 0.010702 | -1.29% |
| left/right arm MAE | 0.012397 / 0.015857 | 0.012254 / 0.015479 | -1.15% / -2.38% |
| left/right hand MAE | 0.003153 / 0.013930 | 0.003182 / 0.013791 | +0.91% / -1.00% |
| first-action jump MAE | 0.046007 | 0.045989 | -0.04% |
| seed dispersion MAE | 0.005468 | 0.004647 | -15.0% |
| velocity RMSE | 0.20996 | 0.20477 | -2.47% |
| acceleration RMSE | 5.70296 | 5.63767 | -1.14% |
| jerk RMSE | 254.606 | 255.931 | **+0.52%** |
| 超出训练q01--q99比例 | 3.263% | 4.081% | **+0.818个百分点** |

5步没有出现整体动作质量下降，多个均值指标还略好。不能据此宣称5步优于10步，因为
验证目标是一条示范轨迹，多模态任务中MAE不是成功率；需要关注的负项是分布外比例和
jerk尾部。当前证据足以让5步进入RTC离线验证，不足以直接替换线上10步。

## 5. Euler 5与Euler 10的RTC相邻观测结果

配置固定为32对、`d=20,s=25,approx_identity weight=0.5`，只改变Euler步数。5步产物：

`/mnt/dm05_nvme/dm05/evaluations/NewLora_RTC_v1/`
`adjacent_weight_0p5_steps5_32pairs.json`，SHA256
`30f937d8e86f66415a1dc50836b5c04a8510065c12d2e06ba8c4315193c8ca50`。

| 指标 | Euler 10 | Euler 5 | 5相对10 |
| --- | ---: | ---: | ---: |
| RTC离线模型时间 | 300.4ms | 202.3ms | **-32.6%** |
| hard-prefix arm MAE | 0.01680 | 0.01616 | -3.82% |
| raw target arm MAE | 0.02036 | 0.02011 | -1.24% |
| fresh arm change from baseline | 0.002557 | 0.002403 | -6.02% |
| raw stitched accel / jerk max | 12.43 / 579.4 | 15.40 / 761.8 | +23.9% / +31.5% |
| suffix-C1-w10 executable target MAE | 0.02693 | 0.02654 | -1.47% |
| suffix-C1-w10 accel / jerk max | 7.378 / 221.35 | 7.366 / 220.99 | -0.16% / -0.16% |

5步raw RTC中间轨迹的高阶动态更差，但已部署候选的suffix-only C1 + weight10后处理把该
差异压回与10步相同水平，并保持C0/C1、零边界方向反转。5步没有加重prefix/target
误差。基于目前证据，**无需因为5步质量不划算而追加6步**；6步保留为真机Shadow若发现
5步尾部异常时的回退候选。

## 6. 不减少10步，哪些优化可能保持结果

“完全bitwise相同”和“任务质量不变”必须区分。GPU kernel、融合顺序或低精度变化都
可能造成最后几位浮点差异；以下优先追求固定seed下数值近等价及任务指标不退化。

### 6.1 静态图编译与CUDA Graph（真机发现重编译，线上回退）

当前batch=1、输入shape固定、chunk固定50、action expert每轮重复同一结构10次，适合：

- 单独`torch.compile` action expert/denoise step；
- `mode=reduce-overhead`或经验证后的`max-autotune`；
- 静态KV cache和CUDA Graph capture；
- 检查graph break、recompile和首次编译warmup，绝不能把编译时间计入实时请求。

本次只编译重复调用的action expert，使用`torch.compile(mode="reduce-overhead",
dynamic=False)`。它保留原去噪时间网格，并由Inductor/CUDA Graph减少Python和kernel
launch开销。首次编译约19秒，必须由部署smoke提前完成，绝不能让真机首个请求承担。

| 配置 | eager | compile | 加速比 |
| --- | ---: | ---: | ---: |
| Euler 10步模型时间 | 298.64ms | 176.53ms | 1.69x |
| Euler 5步模型时间 | 200.79ms | 141.35ms | 1.42x |

5步固定seed下，compile与eager的物理动作MAE为0.000356rad、最大差0.004652rad。它不是
bitwise等价，因此又对compile版本跑完同一组320样本：overall physical MAE为0.010690，
对照5步eager的0.010702和10步eager的0.010842；jerk RMSE为255.20。均值指标没有退化，
但训练q01--q99外比例为4.202%，仍需在真机日志持续观察尾部。

2026-08-20正式G2测试发现，归档回放没有覆盖真实在线输入的所有Dynamo guard/图签名。
部分请求的模型段从约0.141秒跃升到18.87--19.37秒；单进程HTTP在编译期间形成队列，
G2的10秒read timeout先触发，因此表面显示为“HTTP请求失败”。两轮Powered日志分别
出现2次和6次timeout，服务端最终均返回200，且网络GET仍为约13ms。基于不可控的在线
重编译风险，7892默认关闭compile，不能依赖有限预热覆盖动态签名。后续若恢复此路线，
必须先记录recompile guard、构造部署一致动态输入集，并在长时Shadow中证明零重编译。

### 6.2 第二优先级：保持数学公式的机械优化

当前每个flow step都会重建suffix attention mask、position IDs、time tensor和部分小张量。
可在10步循环外预计算或复用静态shape结果；同时复用action mask和预分配buffer。该类
修改理论上不改变模型函数，但仍需固定seed逐元素对照。

当前部署已经使用vision FlashAttention 2、action SDPA，但language attention仍显式为
`eager`。可单独验证language SDPA/FlashAttention兼容性；它针对约67ms固定prefix，不能
未经数值与HTTP测试直接替换。

### 6.3 端到端非模型优化（服务端图片解码已实现）

- 服务端三图JPEG decode/`Image.open`约72ms，是第二大可见单项；
- 客户端JPEG约27ms；
- HTTP尾部P95抖动约135ms。

对同一批真实G2 JPEG，服务端三图并行解码由17.67ms降到12.08ms，且新增测试验证并行
与原串行路径像素完全相同，因此部署服务端并行解码。G2端并行JPEG编码的微基准反而
更慢，故本次不修改客户端编码。改变分辨率或JPEG质量会改变模型输入，不属于“结果
不变”优化。

### 6.4 会改变数值、但可能保持任务质量的优化

- BF16/FP16/FP8或权重量化；
- TensorRT/自定义Triton kernel；
- 更少Euler步或非均匀时间网格；
- 更小视觉/VLM/action expert。

这些必须走完整离线和Shadow门，不能归为无损加速。

## 7. DDIM、DPM-Solver和“固定噪声”是否适用

NewLora DM0.5不是经典DDPM逐步加随机噪声；它是flow-matching velocity model，当前从
一个高斯初始`x_t`出发，用固定时间网格的Euler方法积分10次。

### 固定或共享初始噪声

固定seed只让结果可复现，跨chunk共享/相关噪声可能减少随机模式跳转，但不会减少任何
一次网络前向，所以**不加速**。此前32对coupled-noise实验也只带来轻微改善，不能替代
延迟优化。

### DDIM

DDIM把经典扩散采样改为可确定的非马尔可夫路径，并允许跳过许多扩散时间步。它很可能
就是“指定噪声、很快去噪”的记忆来源。但DM0.5已经是确定性ODE式flow积分，DDIM不能
直接套到当前velocity接口上。

### DPM-Solver / DPM-Solver++

DPM-Solver利用扩散ODE的半线性结构做高阶解析近似，以较少NFE取得高质量。它对标准
noise/data prediction diffusion模型很有效，但不是任意flow velocity的无修改加速器。
若适配到DM0.5，输出必然与10步Euler不同，而且RTC guidance也必须重新推导/验证。

### Heun、midpoint、RK4和自适应ODE solver

这些可以直接用于一般flow ODE，但成本应按NFE而不是“宏观步数”计算：5步Heun需要
10次模型前向，不比10步Euler快；3步midpoint需要6次前向，才可能形成速度收益。高阶
方法可能以更少NFE逼近10步Euler或连续ODE解，但不保证对当前未完美velocity field更好。

### Consistency / progressive distillation / shortcut model

这类方法可把多步teacher蒸馏为1--4步student，是若5步和工程优化仍达不到延迟目标时
最有潜力的长期路线，但需要重新训练和任务级验证，不是当前checkpoint的无损开关。

## 8. 当前决策与下一门

1. 7893的10步RTC服务保持不动，作为独立基线；
2. 7892非RTC Sync保留5步和服务端并行解码，线上compile回退为eager；
3. 替换服务后须重新完成本机30请求及G2非Powered连续请求验证；
4. 5步真机若出现分布外、jerk或任务语义退化，再测6步，不预先铺开大网格；
5. 本次只验证低延迟Sync，不把结果外推为RTC、hands V1或动态RTC结论。

## 9. 组合方案的真实HTTP回放

在独立7894临时端口，用真实G2归档请求（三路原始JPEG尺寸与线上一致）连续回放30次。
首个约19秒编译请求在计时前完成。产物：
`logs/http_5step_compile_parallel_decode_30_20260820.json`。

| 阶段 | mean | P50 | P95 | P99/max |
| --- | ---: | ---: | ---: | ---: |
| 客户端HTTP总时间 | 162.44ms | 161.37ms | 163.64ms | 189.71/200.09ms |
| 服务端图片解码 | 9.19ms | - | - | - |
| 服务端模型+回CPU | 138.34ms | - | 139.23ms | - |
| 服务端handler总时间 | 158.88ms | - | 160.04ms | 186ms级单次输入变换离群值 |

相较旧7892/7893日志中的backend mean约489ms、服务端模型约326ms和handler约417ms，
组合方案把归档HTTP请求稳定压到约162ms。按30Hz换算约4.9个控制点，Sync每20步一次
重规划时的等待预计从约0.49秒降到约0.16秒。它显著降低hold，但Sync架构仍会在chunk
播放完后等待请求返回，并非真正无停顿异步控制。

正式7892上线后再次回放30请求，产物为
`logs/http_7892_5step_compile_30_20260820.json`。首次compile warmup为19.150秒，已在真机
连接前完成；计时请求mean/P50/P95/max分别为163.01/161.66/171.85/183.13ms，模型段
mean为135.71ms。容器无重启，30次均返回HTTP 200；7893 RTC容器身份和启动时间未变。

### 9.1 compile回退后的正式验收

7892关闭compile后保留5步和并行解码。本机归档30请求产物：
`logs/http_7892_5step_eager_30_20260820.json`。首次eager warmup为0.570秒；之后30/30
成功，mean/P50/P95/max为243.94/244.00/248.19/250.80ms，模型段mean为218.52ms，
无长尾编译。

随后从G2运行30请求Shadow，使用真实在线三路相机和状态、零硬件动作，日志为：
`/home/agi/xza/DM0.5/logs/telemetry/shadow/g2_shadow_20260820_170208.jsonl`。
30/30成功、零timeout，端到端P50/P95/max约为330/365/378ms，未出现19秒尖峰。
因此5步eager通过网络与在线输入门；Powered仍应先从有限10秒开始，不直接continuous。

### 9.2 compile重编译根因与固定prefix修复（2026-08-20）

真实G2 state会在`ChatTokenization`中变成34个十进制bin字符串。Gemma tokenizer按数字位
切分；一位、两位和三位bin产生不同token数，因此机器人状态变化会改变`input_ids`长度，
继而改变VLM prefix KV cache形状。旧配置对action expert使用`dynamic=False`，归档离线
测试又只重复一个固定state，所以没有覆盖这个动态shape；真实G2运行则为新prefix长度
重新编译约19秒。这是“网络失败”的直接计算图来源，图片像素和JPEG质量不是根因。

修复仅在`compile_action_expert=true`时启用：tokenization后将`input_ids`、
`attention_mask`和`token_type_ids`在右侧补到训练时相同的1024长度。有效token一个不改，
padding全部被attention mask屏蔽，三路图片原始像素、分辨率和JPEG字节不变；超过1024
则fail closed而不是静默截断。eager正式7892不经过这个新分支。

独立7894/GPU6以归档原画质三图和最新Powered日志中45组连续变化state验证：首请求正常
编译18.890秒；其后44请求client P50/P95/max为0.163/0.169/0.190秒，model均值
0.140秒，Dynamo零recompile。继续循环450请求仍为零recompile，client P50/P95/P99为
0.163/0.165/0.168秒；仅request 71出现一次1.033秒model侧离群，但没有新计算图，需在
G2 Shadow前继续定位系统/GPU长尾。

固定seed的8样本padding前后eager对照得到物理动作MAE 0.000328rad、最大0.004693rad，
与此前compile/eager数值差量同量级；没有降低图像质量。纯逻辑相关测试为19 passed。
完整摘要为`logs/compile_static_prefix_summary_20260820.json`。当前结论从“compile方案失败”
提升为“根因已命中、独立压力测试通过，但尚未获准替换7892”；下一门槛是诊断服务接收
真实G2 Shadow流，并解释或约束1秒非recompile长尾。

随后经操作者授权，7892同步服务已在保持5步、原始三图质量和GPU4不变的前提下启用
fixed-prefix compile；7893 RTC服务未改。正式7892本机预热首请求18.825秒，此后44组
变化state的有效prefix为960--980 token，但编译shape恒为1024；client P50/P95/max为
0.160/0.162/0.168秒，model均值0.136秒，Dynamo零recompile。G2到7892的GET检查返回
预期405。首次编译已在无机器人动作的本机请求中完成，下一步为操作者手动运行同步真机，
运行后必须复核G2端端到端P95及是否存在任何recompile/秒级离群。

## 10. 参考

- RTC论文：https://www.physicalintelligence.company/download/real_time_chunking.pdf
- Flow Matching：https://arxiv.org/abs/2210.02747
- DDIM：https://arxiv.org/abs/2010.02502
- DPM-Solver：https://arxiv.org/abs/2206.00927
- Consistency Models：https://arxiv.org/abs/2303.01469
- PyTorch `torch.compile`：https://docs.pytorch.org/docs/stable/generated/torch.compile
