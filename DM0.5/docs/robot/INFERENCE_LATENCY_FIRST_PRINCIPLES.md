---
doc_type: principle
domain: inference
status: canonical
created: 2026-08-21
updated: 2026-08-24
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: [denoising_compile_end_to_end_latency]
supersedes: [LoRAExperiments/NewLora/09_SYNC_LATENCY_FIRST_PRINCIPLES_AND_ROADMAP.md]
superseded_by: []
related: [experiments/DENOISING_AND_COMPILE_20260820.md, RTC_PROCESS_ISOLATION.md]
---

# Sync推理延迟：去噪步数、静态编译与后续优化的第一性原理

> 日期：2026-08-21  
> 适用范围：NewLora 75K、非RTC Sync、三路原画质图像、`50×34`动作合同  
> 当前线上：7892，Euler 5步，fixed-prefix `torch.compile` action expert  
> 不在本文范围：7893 RTC、双手V1取舍、碰撞检测和任务数据补采

## 1. 先给结论

当前延迟下降不是一个“神奇开关”造成的，而是两项彼此独立的优化叠加：

1. Euler去噪从10步减到5步，少调用5次action expert；
2. 保留5步不变，用`torch.compile`减少每次action expert调用中的框架、调度、
   kernel launch和中间操作开销。

真实G2均值证据为：

```text
旧10步：模型+回CPU约326ms
  ↓ 10步改5步
5步eager：约222ms
  ↓ fixed-prefix compile
5步compile：约139ms
```

因此必须区分：

- 10步→5步会改变ODE数值积分的离散精度，原则上可能改变动作质量；
- eager→compile不改变权重、Euler时间点和模型公式，但浮点执行顺序和kernel实现可能
  改变，所以不是bitwise等价；
- fixed-prefix padding不删除有效token、不改变图片，只把被attention mask屏蔽的右侧
  padding补到1024；但它仍必须通过数值和任务验证，不能只凭理论放行；
- 服务加速通过不等于整机Powered安全通过。动作语义、手部首跳、视觉grounding和
  low-latency无碰撞保护是另外的门。

## 2. 第一性原理：我们到底在压缩什么

一次Sync请求从“机器人开始形成新观测”到“得到可验证动作”只有一条串行关键路径：

```text
三相机采集 + 34D state
  → G2 JPEG编码
  → 请求构造和上传
  → 6000D multipart解析及图片解码
  → resize/tokenize/normalize/搬到GPU
  → vision + language/state prefix + action denoise
  → 动作反归一化和回CPU
  → HTTP响应
  → G2 JSON解析和50×34合同验证
```

如果这些阶段完全串行，总时间近似为：

```text
T_total = T_observation
        + T_encode
        + T_transport_and_parse
        + T_input_transform
        + T_model
        + T_output_and_validation
```

降低其中一项只能拿回那一项占用的时间。优化前模型是最大项，所以先减少去噪步数和
编译action expert是正确顺序。现在模型已降到约139ms，图片、上传、解析和transform
合计已接近模型本身，继续只盯去噪步数会出现明显边际收益递减。

### 2.1 Sync停顿与推理时间的关系

当前Sync每次执行模型chunk前20个30Hz区间，约0.667秒；然后重新观测并等待下一个
请求完成。等待期间保持最后安全目标，不会凭空产生新策略动作。因此：

```text
每周期墙钟时间 ≈ 0.667秒动作播放 + T_total
```

降低`T_total`会直接缩短hold，但不会让Sync变成无缝连续控制。若在旧chunk尚未播放完
就发新请求，虽然可隐藏延迟，但那已经是重叠调度/Async类方案，会改变观测新鲜度和
切换语义，不能把它伪装成“纯推理优化”。

## 3. 第一性原理：DM0.5为什么需要“去噪步骤”

### 3.1 模型不是一次直接回归50×34动作

NewLora DM0.5生成一个`50×34`动作chunk。它从一个随机高斯动作张量开始：

```text
x(t=1) ~ Normal(0, I)
```

模型根据三路图像、语言prompt、机器人state、当前动作样本`x(t)`和时间`t`，预测一个
速度场：

```text
v_theta(x, t | observation)
```

理想情况下，沿这个速度场从噪声端积分到数据端，就得到符合当前观测的动作：

```text
dx/dt = v_theta(x, t | observation)
```

这里的“去噪”不是简单把像素噪声滤掉，而是在动作空间里求解一个由神经网络定义的
常微分方程。每评估一次`v_theta`，就需要调用一次action expert。

### 3.2 Euler 10步在做什么

Euler方法把连续时间切成10段。以简化写法表示：

```text
x_(k+1) = x_k + delta_t * v_theta(x_k, t_k | observation)
```

10步意味着action expert执行10次。后一步依赖前一步结果，所以这10次不能简单并行。
vision和language/state prefix通常只算一次，action expert在去噪循环中重复执行。

旧GPU剖析为：

| GPU部分 | 10步 | 5步 | 原因 |
| --- | ---: | ---: | --- |
| vision tower | 28.80ms | 28.79ms | 每请求只做一次 |
| language/VLM prefix | 67.10ms | 67.08ms | 每请求只做一次 |
| action expert合计 | 199.68ms | 100.61ms | 调用数10→5 |
| GPU总时间 | 309.40ms | 208.65ms | 固定前缀开销不变 |

这解释了为什么步数减半，模型总时间只下降约三分之一：约96ms视觉和语言前缀根本没有
随步数减少。

### 3.3 10步改5步为什么可能成立

减少Euler步数等价于增大单步`delta_t`。它能成功的必要条件不是“5这个数字更先进”，
而是模型速度场足够平滑，使5个较大步长仍能落到任务可接受的动作分布内。

可能保持质量的原因：

- 训练得到的velocity field在当前轨迹附近较平滑；
- 任务允许微小动作数值差异，控制器还会做边界融合和双臂suffix平滑；
- 10步可能已经超过当前模型/任务所需的最低数值精度；
- 模型误差、观测域差和数据覆盖误差可能远大于5步与10步的积分误差。

可能损害质量的原因：

- 大步长更容易跨过速度场局部曲率，产生积分误差；
- 动作分布尾部、快速接触阶段和手部开合时序可能比均值MAE更敏感；
- raw速度、加速度和jerk尾部可能变差；
- 不同噪声seed、域外图片或失败恢复状态可能放大差异。

因此5步必须当成“改变模型数值求解器精度”的质量变量，而不是无损工程优化。

### 3.4 5步已有证据及边界

冻结320个唯一观测、每个观测2个seed的结果：

| 指标 | Euler 10 | Euler 5 | 解释 |
| --- | ---: | ---: | --- |
| physical MAE overall | 0.010842 | 0.010702 | 均值未退化 |
| first-action jump MAE | 0.046007 | 0.045989 | 基本相同 |
| velocity RMSE | 0.20996 | 0.20477 | 均值略低 |
| acceleration RMSE | 5.70296 | 5.63767 | 均值略低 |
| jerk RMSE | 254.606 | 255.931 | 5步高0.52% |
| 训练q01--q99外比例 | 3.263% | 4.081% | 5步高0.818个百分点 |

结论只能是“5步已通过现有离线均值和短时真机候选门”，不能写成“5步理论上等价10步”
或“5步任务成功率一定不变”。MAE面对多模态动作不是完整的任务成功率指标。

## 4. 第一性原理：`torch.compile`为什么能加速

### 4.1 eager模式的成本

PyTorch eager每次执行网络时，Python逐层发起算子。GPU真正计算之外还存在：

- Python解释器和module调用；
- 为大量小算子逐次发起CUDA kernel；
- 中间tensor创建、shape检查和内存调度；
- 重复构造相同执行路径；
- CPU与GPU之间的launch间隙。

action expert在一次5步推理中重复执行5次，同样的框架开销也重复5次。batch=1时，单次
矩阵计算未必足够大，框架和kernel launch占比会比大batch训练更明显。

### 4.2 compile模式的工作

当前只编译action expert：

```python
torch.compile(
    action_expert,
    mode="reduce-overhead",
    dynamic=False,
)
```

它观察输入tensor和程序路径，生成可复用计算图，再由Inductor等后端进行算子融合、
内存规划和CUDA Graph式低开销重放。核心不是“少算模型层”，而是用更少的调度成本执行
数学上相同的层。

因此compile最理想的条件是：

```text
同一程序路径 + 同一dtype/device + 固定tensor shape + 多次重复执行
```

NewLora action expert恰好被每请求重复调用5次，所以首次约18.8秒的编译成本可以在随后
大量请求中摊销。

### 4.3 为什么旧compile在真机失败

旧判断把“图片shape固定、action shape固定”误当成“所有输入shape固定”。实际G2 state
先被离散成34个十进制bin字符串，再进入Gemma tokenizer。一位、两位、三位数字产生的
token数不同：

```text
机器人state变化
  → 十进制字符串位数变化
  → input_ids长度变化
  → VLM prefix KV cache长度变化
  → action expert输入shape变化
```

`dynamic=False`看到新shape时不能安全复用旧图，只能重新编译。每个新图约需19秒，
单进程HTTP又会在编译期间排队，G2先触发10秒read timeout。表面像网络失败，根因其实是
计算图签名变化。

归档压力测试此前只重复固定state，遗漏了真实机器人state引起的token长度变化。这说明
“输入内容在变”与“tensor shape在变”必须分开验证。

### 4.4 fixed-prefix为什么修复了它

训练collator本来就把token序列padding到最大长度。现在只在compile模式复用同一合同：

```text
input_ids        右补pad_token到1024
attention_mask   右补0到1024
token_type_ids   右补0到1024
pixel_values     完全不改
有效token         完全不改
```

attention mask为0的位置不应参与有效注意力，所以语义内容仍由原来的有效token和图片决定。
与此同时，VLM prefix和action expert看到的shape恒定为1024，编译图可以跨不同state复用。

超过1024时系统直接报错，不截断有效输入。这是重要的安全属性：宁可拒绝未知长输入，也
不能把prompt、state或图片标记静默切掉后继续控制机器人。

### 4.5 为什么compile不是严格“输出完全相同”

数学公式相同不等于浮点每一位相同。compile可能：

- 融合多个算子，改变中间结果舍入时机；
- 改变加法或归约顺序；
- 选择不同但数学等价的CUDA kernel；
- 使用CUDA Graph和不同的内存布局。

浮点加法不满足严格结合律：

```text
(a + b) + c 可能不逐bit等于 a + (b + c)
```

flow求解又是递归的，前一步微小差异会进入下一步。因此正确目标是“固定seed数值近等价，
统计质量和任务质量不退化”，而不是承诺bitwise一致。

## 5. compile可能造成的全部影响

### 5.1 对推理质量

已有结果：

| 验证 | 结果 | 能说明什么 |
| --- | --- | --- |
| padding有效token/图片单测 | 有效部分不变，超长拒绝 | 实现没有截断或改图 |
| 8样本固定seed padding对照 | MAE 0.000328rad，max 0.004693rad | 数值差异很小但非零 |
| compile/eager固定seed对照 | MAE 0.000356rad，max 0.004652rad | compile非bitwise等价 |
| 320样本compile验证 | overall MAE 0.010690；5步eager为0.010702 | 均值无可见退化 |
| compile尾部分布 | q01--q99外4.202%；eager 4.081% | 尾部略高，需持续观察 |

这些数据支持继续Shadow和有限Powered，不足以独立证明完整抓取、交接和放置成功率相同。

### 5.2 对延迟和可用性

- 容器首次图编译约18.8秒，必须在无机器人动作的本机warmup完成；
- 容器重启、GPU进程重建或新图签名都可能重新承担编译成本；
- 编译缓存和CUDA Graph会占用额外GPU内存；
- 450请求压力中出现过一次1.033秒模型离群，但Dynamo没有recompile，说明compile之外
  仍可能有GPU调度、系统抖动或同步长尾；
- 运行时必须记录服务身份、compile开关、固定prefix长度、进程启动时间和recompile
  计数，不能以后继续靠耗时猜服务版本。

### 5.3 对工程维护

- 编译错误和graph break比eager堆栈更难定位；
- 改模型forward、dtype、shape或attention实现后必须重新走压力测试；
- 开发调试可保留eager回退，正式服务不能在一次Powered运行中自动切换两种实现；
- 不能因为compile快，就放宽HTTP timeout或动作安全门来掩盖异常。

## 6. 当前真实耗时账本

以下均为mean。旧10步来自arms-only基线；新两列来自2026-08-20两轮真实G2 Sync，
因此旧10步适合看量级，不是完全同场景A/B。

| 阶段 | 旧10步 | 5步eager | 5步compile | eager→compile |
| --- | ---: | ---: | ---: | ---: |
| 相机+state观测 | 约23.2ms | 19.2ms | 21.0ms | +1.8ms |
| G2 JPEG编码 | 26.9ms | 24.9ms | 25.6ms | +0.7ms |
| 服务端上传解析+图片打开 | 72.4ms | 59.1ms | 52.1ms | -7.0ms |
| 服务端输入transform | 17.8ms | 22.9ms | 20.4ms | -2.4ms |
| **模型+回CPU** | **326.1ms** | **222.0ms** | **138.9ms** | **-83.1ms** |
| 服务端handler | 417.1ms | 304.6ms | 212.5ms | -92.1ms |
| HTTP/Flask未归因 | 41.9ms | 21.3ms | 16.7ms | -4.6ms |
| G2响应JSON+合同验证 | 未单列 | 3.1ms | 3.6ms | +0.5ms |
| **backend总时间** | **489.4ms** | **354.1ms** | **258.5ms** | **-95.6ms / -27.0%** |
| **采集开始到合法action** | **约512.6ms** | **约373.2ms** | **约279.5ms** | **-93.7ms / -25.1%** |

当前compile轮可近似拆成：

```text
观测                         21.0ms
backend                     258.5ms
  ├─ G2 JPEG编码             25.6ms
  ├─ payload构造              0.1ms
  ├─ HTTP POST              229.2ms
  │   ├─ 服务端解析+解码       52.1ms
  │   ├─ 输入transform         20.4ms
  │   ├─ 模型                 138.9ms
  │   └─ 输出/框架/网络等       约17.8ms
  └─ JSON解析+50×34验证         3.6ms
总计                         约279.5ms
```

注意：`server_handler`包含解析、transform、模型和输出；`client_backend_total`又包含
JPEG、HTTP、响应解析和合同验证。表中这些行有包含关系，不能把所有行再次相加。

## 7. 后续还能减少什么：按第一性原理排序

每个候选都必须回答五个问题：它删除了哪段必需工作、是否改变模型输入或数学公式、理论
上限是多少、可能带来什么副作用、用什么证据放行。

### 7.1 P0：消除G2图像的重复解码/重编码

#### 当前浪费从哪里来

GDK相机常已返回JPEG，但当前链路为：

```text
GDK JPEG字节
  → G2 OpenCV解码RGB
  → Pillow重新编码JPEG
  → 网络上传
  → 服务器再次解码RGB
```

这不仅消耗CPU，还引入一次额外有损JPEG代际。归档三图约456KB；G2重新编码均值25.6ms，
服务器解码约10ms，相机阶段还包含原JPEG解码成本。

#### 候选修改

让Observation同时保留原始压缩字节和解码RGB；模型HTTP路径在编码确认为JPEG且合同完整时
直接上传原始字节，诊断/显示仍可使用RGB。PNG和未压缩输入继续走安全回退路径。

#### 影响

这不是严格的“输入不变”。服务器将看到相机原始JPEG解码结果，而不是“解码后再次JPEG
压缩”的结果。它减少压缩损失，直觉上可能更清晰，但仍然改变像素，必须当作模型输入
变更验证。

#### 理论收益和门

- 理论可回收：约20--40ms，取决于相机原始格式和GDK读取成本；
- 先保存同一帧的原始直传版与当前重编码版；
- 比较字节、解码像素、JPEG尺寸和固定seed动作；
- 对冻结样本跑动作指标和视觉反事实；
- 真实G2只先跑Shadow，不能直接替换Powered。

### 7.2 P0：拆开并优化真实G2上传与multipart解析

#### 当前证据

真实G2的`server_request_parse`均值约42ms，而6000D localhost归档回放约1.2ms。两者差异
说明这42ms不是纯Flask CPU解析，很可能包含服务端等待请求体、socket接收、multipart
边界扫描和内存复制。

#### 候选修改

先增加不改变合同的分段观测：每张JPEG字节数、请求体总字节、首字节/末字节到达、
multipart材料化和文件读取耗时。证据确认后再比较：

1. 保留HTTP但减少Python层文件复制；
2. 使用版本化的紧凑二进制请求，把三图长度、34D state和prompt显式编码；
3. 保持持久连接并检查TCP/代理/容器路径，而不是盲目换HTTP/2。

#### 影响

若图片字节、state数值、prompt和顺序完全相同，传输封装变化原则上不改变模型输出；但新
协议会扩大客户端/服务端合同面，错误长度、顺序和兼容性必须fail closed。

#### 理论收益和门

- 可见上限约30--40ms，但必须以新增分段数据为准；
- 旧multipart与新协议对同一请求必须产生逐元素相同的服务器输入tensor；
- 完成错长度、错顺序、截断、重复图片和断线测试；
- 先双端Shadow A/B，再决定是否替换7892。

### 7.3 P1：减少约20ms输入transform中的重复工作

#### 当前工作

每次请求都执行固定prompt模板、34D state离散化、tokenization、padding到1024、图片
resize/normalize和CPU→GPU搬运。其中固定prompt、mask尺寸和部分小tensor每次相同。

#### 候选修改

- 缓存固定prompt和聊天模板的token片段，只重建state相关片段；
- 预分配1024长度`input_ids/attention_mask/token_type_ids`；
- 复用固定mask、position ID、action mask和time tensor；
- 评估pinned memory和non-blocking copy；
- 把“图片CPU处理”和“token CPU处理”并行，但不并发调用未经验证线程安全的GDK。

#### 影响

缓存边界若切错一个特殊token就会改变模型语义。验收不能只看shape相同，必须要求旧/新
token tensor和归一化图片tensor逐元素一致。只有逐元素一致的机械优化才可视为低质量
风险。

#### 理论收益和门

- 总预算只有约20ms，现实目标为5--10ms；
- 先对归档请求逐tensor对照；
- 再做30/500请求延迟和内存稳定性；
- 任何token差异都转入“质量变更”流程，不按无损优化上线。

### 7.4 P1：优化尚未编译的vision和language/VLM prefix

#### 为什么它成为新瓶颈

10→5步前action expert约200ms，是最大项；compile后重复去噪部分显著下降，vision约29ms
和language/VLM prefix约67ms的固定成本占比上升。继续减少去噪步数无法删除这约96ms。

#### 候选修改

- 单独验证language attention从eager到SDPA/FlashAttention；
- 对固定1024 prefix路径做局部`torch.compile`；
- 验证更完整的静态KV cache/CUDA Graph；
- 保持vision输入448和三图内容不变，先不碰分辨率。

#### 影响

attention kernel变化和更大编译范围都可能造成浮点差异、graph break、额外显存和新长尾。
完整prefix还包含变化图片与state，固定shape不代表固定内容；绝不能缓存上一次请求的视觉
或state结果。

#### 理论收益和门

- 96ms是被优化部分的上限，不是可实现收益；第一轮目标应保守设为15--30ms；
- 必须在7894独立端口验证，不能直接改7892；
- 固定seed逐元素/动作差、320样本质量、至少500个动态请求、显存和recompile计数全部过门；
- 然后才进入真实G2长Shadow。

### 7.5 P2：去噪循环外预计算小tensor和复用buffer

每个flow step若重复创建suffix mask、position ID、time tensor或临时buffer，就在5次循环
里重复支付分配和launch成本。把与step无关的tensor移到循环外、预分配输出buffer，理论
上可以保持公式不变。

这类收益通常是个位数毫秒，但风险比减少步数低。验收要求固定seed数值近等价、无buffer
别名污染，并覆盖连续请求，防止上一请求内容泄漏到下一请求。

### 7.6 P3：进一步把5步减少到4步或3步

compile后，减少一个action expert调用的边际收益已从eager时代约20ms下降到约7ms量级。
4步改变ODE积分精度，却可能只换来个位数毫秒，因此当前性价比低于图片、解析和prefix
优化。

若未来仍需尝试，必须从第一性原理重新看作求解器质量实验：

- 4/3/5步用相同观测和seed配对；
- 不只看MAE，还看首跳、q分布外比例、速度/加速度/jerk尾部；
- 增加接触、遮挡、失败恢复和域外场景；
- Sync真机按Shadow→arms-only/空场景→有限任务逐级推进。

不能因为5步成功，就线性推断4步和3步也一定成功。

### 7.7 P4：改变图片分辨率、JPEG质量、量化或TensorRT

这些方案可能有更大性能空间，但都改变输入信息或模型数值表示：

- 降分辨率会删除空间细节，尤其影响小目标、遮挡和手眼接近阶段；
- 降JPEG质量会增加块效应，可能削弱本就不足的视觉grounding；
- BF16/FP16/FP8/量化会改变权重和激活数值；
- TensorRT或自定义kernel扩大部署复杂度和数值验证面。

当前模型仍有压纸盒、交接近碰撞和视觉支持不足的证据，所以不应优先通过删除视觉信息
换速度。只有低风险机械优化耗尽且任务级验证体系完善后，才讨论这些路线。

## 8. 推荐实施顺序与放行标准

### 阶段A：只增加观测，不改变输入

1. 在服务响应/telemetry记录：checkpoint、Euler步数、compile开关、prefix长度、进程启动
   时间、代码版本和recompile计数；
2. 分解真实G2上传：请求字节、首末字节、multipart材料化、图片读取；
3. 连续Shadow至少500请求，记录P50/P95/P99/max和GPU显存；
4. 解释或给出1秒非recompile长尾的可检测/回退策略。

成功标准：零recompile、零timeout、无未知服务身份、所有请求严格`50×34`。

### 阶段B：低风险机械优化

1. 输入buffer/token固定片段复用；
2. 去噪循环小tensor预计算；
3. 保证旧/新服务器输入tensor逐元素一致；
4. 独立7894完成30次功能和500次压力A/B。

成功标准：无新图签名、无显存持续增长、质量指标不退化，且P95而非仅mean有可重复收益。

### 阶段C：原始JPEG直传

因为它改变服务器实际像素，单独走质量门：像素对照→动作对照→冻结验证→真实Shadow。
不能与attention或求解步数同时修改，否则无法归因。

### 阶段D：prefix计算优化

单变量比较language SDPA/FlashAttention、局部compile和更完整CUDA Graph。每个候选独立保存
数值、质量、延迟、recompile和显存结果，只选择一个进入G2 Shadow。

### 阶段E：有限Powered

服务性能门通过后仍需独立满足机器人门：

- 正式Sync首跳和动力学从warning-only改为可信hard gate；
- 先空工作区、短时、专人握物理急停；
- 保存逐request三图、state和raw/filtered action；
- compile/eager或新旧输入链使用固定场景、相同初始姿态做配对；
- 任务语义异常立即停止，不能靠更多重复Powered“统计出来”。

## 9. 当前建议目标

在不改变5步、448输入、三图内容和checkpoint的前提下，近期合理目标是：

```text
当前采集到合法action mean：约280ms
第一阶段目标：             230--250ms
```

优先收益来源应是：

1. 图片重复解码/重编码；
2. 真实G2上传与multipart解析；
3. 输入transform机械复用；
4. vision/language prefix低风险kernel与compile验证。

只有这些路线得到真实P95收益后，才重新评估4步、低精度或重叠调度。平均时间下降但P95、
P99或最大值恶化，对机器人Sync没有价值。

## 10. 证据索引

- 去噪、compile、HTTP和fixed-prefix原始报告：
  [DENOISING_AND_COMPILE_20260820.md](experiments/DENOISING_AND_COMPILE_20260820.md)
- fixed-prefix摘要：`logs/compile_static_prefix_summary_20260820.json`
- 正式7892 compile HTTP：`logs/http_7892_5step_compile_30_20260820.json`
- 正式7892 eager HTTP：`logs/http_7892_5step_eager_30_20260820.json`
- 真实G2 eager Powered：
  `/home/user/projects/g2_robot/logs/telemetry/powered/`
  `g2_powered_split_sync_60s_20260820_170722.jsonl`
- 真实G2 fixed-prefix compile Powered：
  `/home/user/projects/g2_robot/logs/telemetry/powered/`
  `g2_powered_split_sync_30s_20260820_174436.jsonl`
- 当前跨系统状态：`../CURRENT_STATUS.md`；G2现场原始状态仍保留在
  `/home/user/projects/g2_robot/docs/Bruno.md`
