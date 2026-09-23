---
doc_type: experiment
domain: rtc
status: frozen
created: 2026-08-18
updated: 2026-08-18
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/05_NEWLORA_RTC_ENGINEERING_DECISION.md]
superseded_by: [../RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md]
related: []
---

# NewLora RTC 工程决策：从时序约束到离线验证

> 状态：模型、HTTP、G2 Shadow/Powered RTC代码和离线验证已完成；真机RTC尚待操作者手动验证  
> 日期：2026-08-18  
> 适用模型：NewLora 75K，`50 x 34`，双臂 14D 相对动作、双手 20D 绝对动作  
> 当前对照：powered-sync `20/50`、Euler 10 步、模型频率 30 Hz

## 1. 本文解决什么问题

当前 powered-sync 的执行过程是：停止运动、采集观测、同步等待 DM0.5 推理、执行新
chunk 的前 20 个间隔，然后再次停止并重复。它把原来的 `49/50` 开环执行缩短到了
`20/50`，提高了重规划频率，但仍然存在两个根本问题：

1. 推理期间机器人需要等待，任务的连续性和低延迟交接受到影响；
2. 如果简单改成异步推理，新旧 chunk 在切换点可能属于不同动作模式，直接切换会产生
   跳变、碰撞或将旧结果按错误时间索引播放。

RTC（Real-Time Chunking）不是一个新模型，也不是重新训练方法。它在执行旧 chunk 的
同时生成新 chunk，并把已确定会执行的旧动作作为条件，在 Flow Matching 去噪过程中
约束新 chunk 与旧轨迹连续。原论文将这个过程描述为动作轨迹的 inpainting。

本文完成三个阶段的工程工作：

1. 审计本地 RLinf 的 RTC 实现；
2. 对照 RTC 原论文和 LeRobot，决定实现来源；
3. 设计适用于 NewLora、G2 和现有平滑/安全链路的离线实现与验证步骤。

本文刻意不提供大段实现代码。现在最重要的不是尽快让机械臂动起来，而是先冻结动作
空间、时间索引、延迟估计和迟到结果处理合同。

## 2. 最终工程决策

### 2.1 不迁移模型框架

NewLora 75K 保持使用当前 OpenDM PyTorch 模型、checkpoint、norm stats 和 HTTP
部署。不会把模型迁移到 OpenPI、RLinf 或 LeRobot，也不会尝试让这些框架直接加载
NewLora checkpoint。

原因是 NewLora 具有不能丢失的专用合同：

- 输出固定为 `50 x 34`；
- 维度 0--13 是相对当前双臂状态的动作；
- 维度 14--33 是绝对灵巧手目标；
- 使用 NewLora 自己的 34D norm stats；
- 使用现有三路图像、prompt、LoRA 和 34D projection。

### 2.2 三个参考源各司其职

| 来源 | 用途 | 不直接采用的部分 |
| --- | --- | --- |
| RTC 原论文 | 数学定义、`d ≤ s ≤ H-d`、硬/软重叠约束 | 论文不是完整的 G2 工程运行时 |
| LeRobot `713a409f` | 论文式 VJP 引导、动作队列、模型/执行空间分离、relative 重锚定 | 不安装进 DM0.5，不使用它的机器人抽象和模型类 |
| RLinf `d00fe720` | 单飞异步请求、延迟历史、未来 RL 接入参考 | 现有 `approx` guidance、Ray 运行栈和 Franka 环境不原样移植 |

LeRobot 已固定浅克隆在：

```text
/home/xza_100850113/projects/third_party/lerobot
commit 713a409faedd73bb5597481b8885f17fbee23330
```

本地 RLinf 位于：

```text
/home/xza_100850113/projects/RLinf
commit d00fe720
```

### 2.3 精确版保留作参考，首轮部署采用低延迟近似版

LeRobot 的 RTC 在每个 Euler 去噪步骤中对预测的干净动作求 VJP，并添加有界 guidance；
这更接近原论文。RLinf 当前明确只支持 `rtc_guidance_mode="approx"`，直接用目标轨迹与
当前去噪均值之差做无梯度修正，计算更便宜，但不能默认认为与论文算法等价。

实现后的实测决策如下：

1. 两种模式均已实现，Euler仍为10步；
2. `exact_vjp`单次模型时间约0.63--0.77秒，明显压缩`H=50`的调度余量，只保留为离线参考；
3. `approx_identity`约0.321秒，与无RTC的0.320秒基本相同，因此进入首轮部署；
4. 首轮权重选0.5，而非更强的1.0：它已显著降低接缝，同时验证目标MAE只增加约2%；
5. 完整结果见`RTC_IMPLEMENTATION_AND_OFFLINE_REPORT_20260818.md`。

### 2.4 RTC 参考的是实际承诺轨迹，不是模型原始旧输出

RTC 的约束目标必须是机器人真正准备执行的轨迹。NewLora 的原始模型输出还会经过：

1. NewLora norm stats 反归一化；
2. 双臂 relative 转 absolute，双手保持 absolute；
3. 关节限位；
4. 双臂内部平滑；
5. 30 Hz 轨迹到 100 Hz Servo 的插值和边界处理。

如果服务端只保存上一次的原始模型输出，RTC 约束的“旧轨迹”与机器人实际执行的轨迹
可能不同。因此 G2 客户端必须保存并反馈已通过确定性后处理、已经进入执行队列的 30 Hz
绝对目标。下一次推理前，服务端再把它转换回当前观测对应的模型空间：

```text
双臂模型前缀 = 已承诺双臂绝对目标 - 新观测的双臂状态
双手模型前缀 = 已承诺双手绝对目标
随后使用 NewLora 34D norm stats 归一化
```

## 3. 第一性原理：为什么 horizon 和推理延迟共同限制部署

### 3.1 定义

设：

- `f = 30 Hz`：模型动作频率；
- `Δt = 1/f = 33.33 ms`：一个模型动作间隔；
- `H = 50`：模型预测 horizon；
- `δ`：从观测开始形成到新 chunk 可以安全进入执行队列的端到端时间；
- `d`：把端到端延迟换算成模型步数；
- `s`：RTC 论文中的 execution horizon；在算法里是从上一次推理的逻辑起点算起，
  发起下一次推理时已经走过的动作数；
- `t`：当前 chunk 的逻辑时间索引；新 chunk 到达时不是从0开始，而是置为真实延迟
  `d_real`，因为前 `d_real` 个动作对应推理期间已经过去的时间；
- `g=s-d`：稳态下，新 chunk 到达并从索引 `d` 开始执行后，到下一次推理启动前还会
  额外执行的动作数。bootstrap 第一 chunk 是例外，它从 `t=0` 开始。

论文分析中写作 `d = floor(δ/Δt)`。机器人调度不能低估延迟，因此工程上使用：

```text
d_safe = ceil(δ / Δt)
```

在bootstrap之后，新 chunk 到达时逻辑索引已经是 `d`；RTC等待该索引达到 `s` 再发
下一次请求。因此稳态下，从一次响应切换到下一次请求的间隔是：

```text
g = s - d_hat
```

其中 `d_hat` 是发请求前对下一次延迟的预测，而 `d_real` 是请求完成后测到的真实延迟。

### 3.2 核心约束

RTC 原论文给出的执行 horizon 约束是：

```text
d ≤ s ≤ H - d
```

它来自两个不可绕开的事实。

第一，新 chunk 到达时它的逻辑时间索引已经是 `d`。如果 `s < d`，下一次推理的触发
位置在当前响应到达前就已经过去，系统只能立即重叠请求或失去定义清晰的执行周期：

```text
s ≥ d
```

第二，在旧 chunk 逻辑索引 `s` 发起新推理时，旧 chunk 只剩 `H-s` 个动作。新推理
需要 `d` 步，至少要有 `d` 个旧动作可在这段时间继续执行并作为hard prefix，因此：

```text
H - s ≥ d
s ≤ H - d
```

两式合并就是：

```text
d ≤ s ≤ H - d
```

它还直接推出 RTC 存在严格可行区间的必要条件：

```text
H ≥ 2d
```

因此，执行步数不是可以脱离模型 horizon 和推理延迟随便选择的参数。模型预测 50 步
并不意味着可以在 1--49 中任意挑一个 RTC execution horizon。

### 3.3 `20/50`不能直接当成RTC参数

当前 powered-sync `20/50` 的语义是：

```text
停止 → 采集 → 同步等待推理 → 播放新chunk前20个间隔 → 停止
```

这里的 20 是同步播放前缀长度。RTC 中则是：

```text
旧chunk持续播放 → 提前发起推理 → 新chunk到达 → 按真实时间索引切换
```

如果 RTC 选择 `s=25`、预测延迟 `d_hat=20`，正确时间轴分为bootstrap和稳态。

bootstrap第一chunk A：

```text
A从索引0开始执行
执行A[0..24]，t达到25时采集观测并发起B推理
推理20步期间继续执行A[25..44]
B到达时，A还剩A[45..49]
```

生成B时，RTC使用A从请求点开始的尾部A[25..49]：

```text
B[0..19]：hard prefix，对应推理期间已经执行的A[25..44]
B[20..24]：soft overlap，对应仍可执行的A[45..49]
B[25..49]：主要由新观测生成的新动作
```

B到达后不会回放B[0]，而是把逻辑索引置为真实延迟20，从B[20]开始执行。稳态下再
执行B[20..24]这5个动作，`t`重新达到25，立即发起C推理：

```text
g = s - d_hat = 25 - 20 = 5
```

因此“第5步发请求”只适用于bootstrap之后、从新响应切换时刻开始计数；第一次请求仍在
初始chunk执行25步后发出。每个稳定周期最终仍由一个chunk贡献约25个实际动作。

不同项目中还有几个名字相似但语义不同的参数，不能混用：

| 参数 | 实际语义 |
| --- | --- |
| G2 `execute_prefix_steps=20` | powered-sync 每次同步推理后播放多少间隔 |
| 论文 `s` | 从上一次推理逻辑起点到下一次推理启动的execution horizon |
| RLinf `min_exec_horizon` | 至少执行多少步后才允许发异步请求 |
| LeRobot `RTCConfig.execution_horizon` | RTC guidance 使用的前缀/权重终点，不等同于论文 `s` |
| LeRobot `queue_threshold` | 队列剩多少动作时触发补充请求 |

## 4. 当前真实延迟：P99、P100、20步、25步和40步

### 4.1 数据来源

使用最近一次连续 powered-sync 真机日志：

```text
g2_robot/logs/telemetry/powered/
g2_powered_split_sync_continuous_20260818_115831.jsonl
```

共有 183 个 `action_chunk` 请求。这里必须区分两种延迟：

- `latency_seconds`：HTTP 请求开始到响应完成；
- `observation_age_seconds`：观测采集开始到响应完成，更接近 RTC 论文中的端到端 `δ`。

| 指标 | HTTP请求延迟 | 30 Hz步数 | 观测到结果端到端 | 30 Hz步数 |
| --- | ---: | ---: | ---: | ---: |
| P50 | 0.4370 s | 13.11 | 0.4622 s | 13.87 |
| P95 | 0.4802 s | 14.41 | 0.5142 s | 15.43 |
| P99 | 0.6123 s | 18.37 | 0.6337 s | 19.01 |
| 样本最大值/P100 | 0.8138 s | 24.41 | 1.0011 s | 30.03 |

因此此前讨论的“P99 大约 18 步、P100 大约 25 步”对 HTTP 请求部分基本成立。按安全
向上取整：

```text
HTTP P99: 19步
HTTP样本最大值: 25步
```

但 RTC 的调度应从观测时间算到结果可用，所以当前更完整的数值是：

```text
端到端 P99: ceil(19.01) = 20步
端到端样本最大值: ceil(30.03) = 31步
```

表中的P50/P95/P99使用与NumPy默认方法一致的线性分位数。若使用nearest-rank，端到端
P99会得到约18.86步；两种估计只差约0.15步，但安全调度不能向下取整，因此第一版仍
按20步设计。

这也说明为什么不能只盯着模型前向时间或 HTTP 时间：相机采集、JPEG 编码、请求组装、
网络、服务端变换、模型、响应解析和队列提交共同决定机器人看到的真实 `d`。

### 4.2 P99到底表示什么

在这 183 个样本中，经验 P99 表示约 99% 的请求在 0.6337 秒、也就是 19.01 个模型
步内从观测走到结果可用。调度上按 20 步处理。

它不表示未来所有 rollout 都天然获得 99% 的服务保证，原因包括：

- 183 个样本只能粗略描述尾部，P99 附近实际上只有约 2 个样本；
- GPU 竞争、相机阻塞、网络抖动和容器状态会改变分布；
- 加入论文式 VJP guidance 后，每个 Euler step 多了反向 VJP，延迟分布必然需要重测。

### 4.3 为什么正常节奏不应由P100决定

样本最大值对偶发抖动和样本数量极敏感。每增加一批运行，它都可能继续变大。若为了
一个最大值让所有正常请求都变慢，会牺牲整个系统的重规划频率。

更重要的是，当前端到端样本最大值换算后是 31 步，而 `H=50` 要求：

```text
H ≥ 2d
50 ≥ 62  （不成立）
```

也就是说，若强行要求覆盖这个 P100，当前 `H=50` 根本不存在满足论文严格约束的 `s`。
这不是把执行步数调大就能解决的问题。

正确分层是：

1. P99用于离线判断常态容量和选择初始参数；在线延迟预测采用论文的短窗口近期最大值，
   避免把P99误当成每次请求都精确相同；
2. P100/max 用来证明尾部降级路径必须存在，而不是决定所有请求的固定节奏；
3. 实际延迟超过设计上界时，继续执行仍有效的旧承诺轨迹、丢弃过期结果，并在队列耗尽
   前进入安全 hold，绝不从新响应的第 0 步重新播放。

### 4.4 20步、25步和40步的结论

以端到端 P99 的安全值 `d=20`、模型 horizon `H=50` 计算：

```text
20 ≤ s ≤ 30
```

| 候选 `s` | 响应切换后到下次请求 `g=s-d` | 结论 |
| ---: | ---: | --- |
| 20 | 0 | 只在数学边界可行；每次响应到达后立即请求下一chunk，没有延迟余量 |
| 22 | 2 | 可作为激进对照，但仍需RTC实测延迟支持 |
| 25 | 5 | 第一版最合理；在当前P99估计下两侧各有约5步余量，且 `H=50` 时结构对称 |
| 30 | 10 | P99下的严格上界，切换更慢、软重叠余量为零 |
| 40 | 20 | 不可行；`H-s=10 < d=20`，没有足够重叠轨迹满足RTC冻结约束 |

因此第一版不是把现有 `20/50` 机械改成“RTC 20/50”，也不是选择 40 来给推理更多时间。
初始设计应保留 `H=50`，用 `d_hat=20` 和论文变量 `s_min=25`。bootstrap第一请求在
初始chunk第25步发；稳态下每次响应切换后约5步发下一请求。
最终参数必须由 RTC 开启后的新延迟分布决定。

如果只看 HTTP 样本最大值 `d=25`，严格可行区间退化成唯一一点：

```text
25 ≤ s ≤ 25
```

这也是“25步”看起来保守的来源之一，但不应该把它误解为P100驱动的长期固定方案。完整
端到端最大值已经超过 `H/2`，只能由尾部策略处理。

## 5. 阶段一：RLinf RTC审计

### 5.1 已确认的能力

RLinf 当前已经提供：

- PyTorch OpenPI π0.5 路径；
- 仿真 LIBERO 与真机 Franka RTC 示例；
- bootstrap 后的单飞异步请求；
- `executed_horizon`、`predicted_delay_steps` 和 `chunk_id`；
- 长度 8 的延迟历史，并使用历史最大值预测下一次延迟；
- hard mask 和指数衰减 soft mask；
- RTC 开关、guidance clip 和运行指标。

关键参考文件：

- `RLinf/docs/source-zh/rst_source/guides/rtc.rst`；
- `RLinf/rlinf/workers/env/rtc_env_worker.py`；
- `RLinf/rlinf/workers/rollout/hf/rtc_huggingface_worker.py`；
- `RLinf/rlinf/models/embodiment/openpi/rtc_guidance.py`。

### 5.2 不能原样移植的部分

#### 近似 guidance

RLinf 当前只接受 `rtc_guidance_mode="approx"`。它在 `torch.no_grad()` 中直接计算：

```text
目标轨迹 - 当前去噪均值
```

并进行有界修正，没有实现论文中的 clean-action estimate 对当前噪声动作的 VJP。它适合
作为低延迟备选，但必须用实验验证，不能把名字相同当作数学等价。

#### 没有解决NewLora mixed-relative重锚定

RLinf 保存的是前一轮 `model_actions`，然后按已执行长度切片。NewLora 双臂相对动作是
相对“生成该 chunk 时的状态”定义的；下一轮观测状态已经改变，不能直接把旧 relative
数值当作新模型空间的前缀。必须先回到绝对轨迹，再相对新状态重锚定。

#### 没有以G2实际后处理轨迹为唯一真值

G2 当前会对双臂动作限位和平滑。RLinf 的模型端旧动作没有包含这些客户端改动。如果
直接复用，RTC 会约束到机器人没有真正执行的轨迹。

#### 尾部处理不适合直接用于G2

RLinf 真机循环在动作索引超过 chunk 长度后，会继续使用 chunk 最后一项。对 G2 双臂
交接任务，不能把无限重复最后目标作为默认迟到策略。必须有明确的过期响应丢弃、队列
余量和安全 hold。

#### 运行栈过重

RLinf RTC 示例依赖 Ray、Env Worker、Rollout Worker 和 Franka 环境。为一个现有 HTTP
DM0.5 服务引入整套运行时，会扩大故障面，并不能自动解决34D动作语义。

### 5.3 阶段一结论

保留 RLinf 作为异步状态机和未来 RL 集成参考；不把其 RTC 类或 Ray 真机栈直接接入
G2，不把 `approx` 当作第一版唯一算法。

## 6. 阶段二：LeRobot与原论文审计

### 6.1 LeRobot更适合作为主要工程参考的原因

LeRobot 当前实现了几个正好对应 NewLora 风险点的组件：

1. `RTCProcessor`：在每个 Flow Matching Euler step 内通过 autograd 求 VJP；
2. `ActionQueue`：分别保存模型空间原始动作和机器人执行空间处理后动作；
3. `reanchor_relative_rtc_prefix`：把绝对剩余动作相对当前状态重锚定，再重新归一化；
4. `LatencyTracker`：记录真实推理时间并换算成动作步；
5. RTC 后台线程：持续消费最新观测并按真实延迟跳过新 chunk 的过期前缀；
6. reset epoch：reset期间生成的旧chunk不会泄漏回新队列。

关键参考文件：

- `third_party/lerobot/src/lerobot/policies/rtc/modeling_rtc.py`；
- `third_party/lerobot/src/lerobot/policies/rtc/action_queue.py`；
- `third_party/lerobot/src/lerobot/policies/rtc/relative.py`；
- `third_party/lerobot/src/lerobot/rollout/inference/rtc.py`。

### 6.2 仍然不能照搬的地方

#### 参数命名与论文变量不同

LeRobot 的 `RTCConfig.execution_horizon` 是 guidance 权重的前缀终点，不等于论文中的
推理execution horizon `s`。如果真实延迟约20步，却使用文档常见的 guidance horizon 10，
实现会把 hard prefix 截到10步，无法完整冻结推理期间必然执行的19步。

对G2，guidance前缀至少要覆盖预测 `d`，并且不能超过请求时实际剩余的旧轨迹。调度
变量与guidance长度必须分开命名。

#### 默认延迟最大值并非真正滚动最大值

LeRobot `LatencyTracker` 内部样本使用长度100的 deque，但 `max_latency` 只增不减；
旧最大值即使已经从 deque 淘汰，也不会重新计算。因此它更接近“本次进程生命周期
最大值”，不是严格的滚动最大值。G2需要同时记录滚动 P50/P95/P99、滚动最大值和
生命周期最大值，并分别用于正常调度和诊断。

#### 通用机器人处理器不理解我们的14+20语义

LeRobot 的 relative processor 根据动作名字构造 mask。G2 必须继续使用已经冻结的
`NEWLORA_STATE_DESC`：前14维 relative，后20维 absolute，不能让第三方自动推断。

#### 它不能替代G2的安全和Servo链路

G2已有请求ID、防乱序、限位、双臂内部平滑、100 Hz插值、急停和telemetry。LeRobot
的机器人包装、queue和rollout CLI不能直接替换这些经过真机验证的部分。

### 6.3 阶段二结论

以原论文作为算法规范，以 LeRobot 的 VJP、双空间队列和 relative 重锚定作为主要代码
参考；吸收 RLinf 的单飞请求和延迟历史思想；最终在 OpenDM/G2 现有结构中做小范围
适配，而不是选择某个第三方框架整套接管机器人。

## 7. 阶段三：NewLora RTC详细实现设计

### 7.1 总体数据流

```text
G2执行上一轮已承诺的30 Hz绝对轨迹
        │
        ├── 当前逻辑索引t到达s时发起下一请求
        │
        ├── 采集三图与34D measured state
        │
        ├── 提取上一轮仍未执行的“后处理绝对轨迹”
        │
        └── 异步发送观测、旧轨迹、时间戳、chunk/request ID
                         │
                         ▼
DM0.5服务端：旧绝对轨迹按新state重锚定为14D relative + 20D absolute
                         │
                         ▼
用NewLora norm stats归一化，得到RTC模型空间前缀
                         │
                         ▼
10步Euler Flow Matching + RTC VJP guidance
                         │
                         ▼
反归一化 → 双臂relative转absolute → 双手保持absolute
                         │
                         ▼
G2收到结果，按真实d_real跳过已经过期的前缀
                         │
                         ▼
限位 → 只平滑尚未承诺的新双臂后缀 → 原子提交队列
                         │
                         ▼
30 Hz目标插值到100 Hz Servo；双手按现有命令节奏执行
```

### 7.2 请求必须携带的逻辑信息

具体字段名留到实现阶段冻结，但语义至少包括：

- 单调递增 `request_id`；
- 当前正在执行的 `source_chunk_id`；
- 观测采集开始的 monotonic 时间；
- 当前34D measured state；
- 三路图像与prompt；
- 预计延迟 `predicted_delay_steps`；
- 当前 chunk 已执行到的模型网格索引；
- 仍未执行的已承诺30 Hz绝对轨迹；
- 动作合同标识：`arm_relative_14_hand_absolute_20`；
- NewLora checkpoint/norm-stats身份。

服务端必须验证 shape、时间索引、chunk世代和动作合同。任何不一致都返回错误，不能默认
补零、截断成32D或冒充其他robot type。

### 7.3 服务端改动边界

当前 DM0.5 `inference_action()` 在 `torch.no_grad()` 下完成10次Euler更新。论文式RTC
需要把一次去噪步骤整理为可调用函数，并仅在RTC开启时，用 `torch.enable_grad()` 对
当前噪声动作求VJP。模型参数不更新，不发生训练。

实现必须保持以下不变量：

1. RTC关闭时，同一checkpoint、输入、seed的结果与当前实现数值对齐；
2. bootstrap第一chunk没有旧轨迹时，退化为普通DM0.5推理；
3. guidance只作用于有效的34D action mask和有效时间前缀；
4. 14D双臂与20D双手在同一个模型tensor内生成，但重锚定规则不同；
5. norm stats只使用NewLora训练集对应版本；
6. 服务器返回完整 `50 x 34`，并附带服务端分阶段计时。

### 7.4 客户端调度状态机

第一版只允许一个在途请求。建议状态如下：

```text
BOOTSTRAP
  └─ 同步获得第一chunk，完成安全处理并提交

EXECUTING
  ├─ 每个30 Hz模型tick推进承诺队列
  ├─ 每个100 Hz Servo tick插值并发送当前目标
  └─ 到达 r 时采集观测，进入INFERENCE_PENDING

INFERENCE_PENDING
  ├─ 旧承诺轨迹继续执行
  ├─ 收到匹配request/chunk世代的结果 → VALIDATE_AND_SWAP
  ├─ 收到旧世代/乱序结果 → DISCARD
  └─ 队列进入安全余量且仍无结果 → SAFE_HOLD

VALIDATE_AND_SWAP
  ├─ 计算真实端到端 d_real
  ├─ 跳过新chunk中已经过期的前 d_real 步
  ├─ 检查剩余长度、限位、首跳、速度、加速度、jerk
  ├─ 只平滑新鲜且未承诺的双臂后缀
  └─ 原子替换未承诺队列，返回EXECUTING
```

客户端不能复用旧 powered-async 中“结果来晚后按墙钟猜一个起始位置并尽量播放”的
宽松语义。RTC请求必须带明确的观测时间轴和chunk世代，过期结果宁可丢弃也不能错位。

### 7.5 RTC与平滑的正确顺序

“先RTC还是先平滑”必须按跨轮次闭环理解。

上一轮：

```text
模型输出 → 绝对化 → 限位 → 双臂平滑/边界处理 → 承诺执行
```

下一轮：

```text
上一轮已经后处理的承诺轨迹
    → 重锚定和归一化
    → RTC guidance
    → 新模型输出
    → 绝对化和限位
    → 只平滑尚未承诺的新后缀
```

因此，对新生成部分可以说“先RTC、后平滑”；但RTC的输入必须来自上一轮“已经平滑后
并真正承诺执行”的轨迹。已冻结前缀不能在RTC后再次平滑，否则会修改已经对模型声明
不会改变的动作。

当前手指平滑和手部V1均关闭，第一版保持这一状态：

- 双臂：RTC后只对新后缀使用现有内部平滑；
- 双手：RTC后做绝对目标限位和原有命令保持，不新增低通；
- 双手碰撞：RTC只能改善时间连续性，不能证明空间无碰撞，仍需独立安全指标和后续
  数据/RL方案。

这里的“承诺轨迹”指已经确定会发给Servo的命令轨迹，不是未来不可知的measured state。
measured state只用于新一轮双臂relative重锚定，并另行计算命令跟踪误差。否则手部本体
滞后会被误当成RTC应该复现的动作规划。

现有120 ms边界融合也必须纳入合同。优先做法是在提交时把内部平滑和边界融合后的
Servo目标按30 Hz模型时刻物化，作为下一轮反馈的承诺轨迹；不能一边让机器人执行融合
轨迹，一边把融合前轨迹交给RTC。如果实现阶段无法准确物化，则必须把“RTC后关闭常规
切换边界融合、bootstrap仍保留安全起步”作为单独的离线/Shadow方案验证，不能静默忽略
这项差异。

### 7.6 正常调度与尾部安全策略

第一版初始值不是最终常量，而是shadow起点：

```text
H = 50
模型频率 = 30 Hz
Euler = 10
d_hat初始值 = 20
s_min初始值 = 25
bootstrap首次请求 = 初始chunk索引25
稳态响应到下次请求 g = 5
```

延迟统计需要经过服务warmup，并按端到端观测年龄计算：

- 滚动窗口至少100--200次；
- 记录P50、P95、P99、滚动max、生命周期max；
- P99用于判断常态容量和选择初始参数，不把它假装成每次请求的精确延迟；
- 在线 `d_hat` 优先采用论文做法：最近短窗口（初始8次）的最大端到端延迟；
- 生命周期P100只做诊断，不能永久污染正常节奏；超过25步时进入本轮尾部降级，且在
  服务恢复前暂停新的严格RTC切换；
- 每次使用 `ceil` 换算动作步；
- RTC exact/approx、Euler步数或图像链路改变后全部重测。

尾部策略必须独立存在：

1. 如果结果仍能按严格时间索引安全接入，则跳过全部过期前缀；
2. 如果世代不匹配、剩余后缀不足或安全门失败，丢弃结果；
3. 继续执行旧的已承诺轨迹，不回播旧动作；
4. 在队列耗尽前进入hold，而不是无限重复最后一个动态目标；
5. telemetry记录迟到程度、丢弃原因和hold触发时的剩余步数。

## 8. 实施与验证步骤

### Gate 0：冻结基线

在任何RTC改动前记录：

- NewLora 75K checkpoint和norm stats路径；
- 当前三图、34D state和prompt合同；
- Euler 10步；
- powered-sync 20/50；
- 手指平滑off、手部V1 off；
- 当前双臂内部平滑和边界处理参数；
- 最近183请求的延迟统计。

RTC关闭必须继续走完全相同的现有路径。

### Gate 1：纯逻辑动作空间测试

不加载模型、不连接G2，构造合成34D轨迹，验证：

1. `absolute → mixed-relative → norm → denorm → absolute`往返；
2. 只有前14维减去当前state，后20维保持absolute；
3. 新旧观测state不同时，旧绝对双臂轨迹能正确重锚定；
4. 手指目标不因重锚定发生变化；
5. 过期步数、剩余长度和chunk世代计算无off-by-one；
6. `d=20,H=50`时只接受严格可行的论文`s`区间20--30；
7. `s=40`明确失败，而不是静默截断mask。

### Gate 2：固定seed的模型离线测试

加载NewLora 75K，但不运行真机。使用冻结验证集样本和固定噪声：

1. 生成普通chunk A；
2. 假设A已执行到指定时间，构造其后处理绝对剩余轨迹；
3. 使用下一时刻观测生成普通chunk B和RTC chunk B-RTC；
4. 对比相同seed下B与B-RTC。

至少统计：

- hard prefix误差；
- soft overlap加权误差；
- 切换点位置jump；
- velocity、acceleration、jerk的P95/P99/max；
- 双臂和双手分组指标；
- 限位命中数、最大裁剪量；
- RTC correction范数和guidance clip命中率；
- 新观测相对旧观测的动作响应量，防止RTC把策略完全锁死在旧轨迹。

### Gate 3：视觉响应保持测试

RTC提高连续性不能以失去视觉响应为代价。继续使用NewLora已有的固定seed视觉配对方法，
比较真实图、轻度aug、错配图、时序打乱图和黑图下：

- 普通推理与RTC推理的视觉动作差；
- 前20步双臂reach方向余弦；
- RTC约束外的新鲜后缀是否仍随图像变化；
- 不同guidance权重是否把视觉差异过度压缩。

黑图只做shadow，不执行。

### Gate 4：延迟基准

在6000D、相同容器和相同服务路径依次测：

1. 普通DM0.5、Euler 10；
2. 论文式VJP RTC、Euler 10；
3. 如严格版不可行，再测明确标注的RLinf式approx RTC；
4. 每种至少完成warmup后数百个端到端请求。

输出：

- 相机采集、编码、HTTP、输入变换、模型、RTC VJP、输出变换、队列提交耗时；
- P50/P95/P99/max；
- 对应30 Hz `d_safe`；
- 根据 `d ≤ s ≤ H-d`计算的可行区间；
- bootstrap请求点、稳态间隔 `g=s-d_hat`；
- 迟到和丢弃率。

只有RTC实测P99不超过25步，`H=50`才存在严格可行区间。如果超过25，优先优化
推理或评估approx guidance；不能通过把`s`改成40绕过公式。

### Gate 5：连续shadow状态机

连接真实三图和34D状态，但不发送Servo命令，持续运行至少数百个chunk：

- 模拟30 Hz消费队列；
- 验证始终只有一个在途请求；
- 注入延迟、乱序、暂停、reset和服务错误；
- 验证旧世代结果全部丢弃；
- 验证没有从第0步播放迟到chunk；
- 验证队列耗尽前进入安全hold；
- 生成可审计的请求/响应/切换时间线。

### Gate 6：分级真机验证

只有前五个gate通过后，才由操作者在G2终端手动启动：

1. 双臂远离桌面、短时、低风险轨迹；
2. 单瓶抓取，不进行双手交接；
3. 固定位置重复，比较RTC off/on的边界jump和延迟；
4. 小范围位置变化，确认视觉响应没有被guidance压制；
5. 最后才开放双手交接，全程保留物理急停。

首轮带电参数从经过shadow验证的 `s_min=25` 开始。`s=20/22`只能在RTC实测P99、
迟到率和轨迹指标证明安全后作为响应性A/B；`s=40`不进入测试矩阵。

## 9. 验收标准

RTC实现进入真机前必须同时满足：

- RTC off固定seed结果与当前NewLora推理数值对齐；
- 所有请求和响应具有可验证的时间轴与chunk世代；
- 14D relative和20D absolute在全部往返中保持不变；
- RTC目标来自实际后处理承诺轨迹；
- 已承诺前缀不会被再次平滑或覆盖；
- 无乱序、无过期chunk从第0步播放；
- hard prefix误差、边界jump和jerk相对无RTC显著改善；
- 视觉counterfactual响应未被guidance压到接近零；
- RTC端到端P99对应的`d`存在非空可行区间；
- 超出正常P99的尾部请求稳定进入丢弃/hold路径；
- powered测试仍需人工启动、急停在手，程序不得自动从shadow切到powered。

## 10. 这项工作能解决什么，不能解决什么

RTC直接解决的是：

- 推理和动作执行重叠；
- 新旧chunk的时间连续性；
- 迟到结果的正确时间索引；
- 同步等待造成的停顿。

RTC不能单独解决：

- 数据集中没有失败恢复示范；
- 模型视觉依赖弱或瓶子超出训练分布；
- 双手在三维空间发生碰撞；
- 手指本体跟踪滞后；
- 错误的34D映射、norm stats或相机输入；
- 抓取成功与否的显式判断。

因此RTC应作为独立的推理时序实验，与后续失败恢复数据、物体持有检测、安全碰撞层和
RL分别评估。不能把真机成功率的所有变化都归因于RTC。

## 11. 参考资料

- Kevin Black, Manuel Y. Galliker, Sergey Levine, [Real-Time Execution of Action Chunking Flow Policies](https://arxiv.org/abs/2506.07339), 2025。
- Physical Intelligence, [RTC论文PDF](https://www.pi.website/download/real_time_chunking.pdf)。
- 本地RLinf RTC指南：`RLinf/docs/source-zh/rst_source/guides/rtc.rst`。
- 本地LeRobot RTC实现：`third_party/lerobot/src/lerobot/policies/rtc/`。
- 当前OpenDM去噪入口：`opendm/opendm/model/dm05/dm05_arch.py::inference_action`。
- 当前G2同步控制：`g2_sort/deployment/g2_client/control_loop.py::_run_powered_sync`。
