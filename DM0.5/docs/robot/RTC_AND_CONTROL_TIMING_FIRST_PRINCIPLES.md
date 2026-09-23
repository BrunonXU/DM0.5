---
doc_type: principle
domain: rtc
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, new_lora]
hardware: [g2]
canonical_for: [rtc_timing, servo_deadline, chunk_continuity]
supersedes: []
superseded_by: []
related: [RTC_PROCESS_ISOLATION.md, experiments/RTC_ENGINEERING_DECISION_20260818.md, experiments/O10_TACTILE_AND_COMMAND_RATE_20260825.md, incidents/2026-08-18_01_RTC_ROLLBACK.md]
---

# RTC、chunk与100Hz控制时序：第一性原理

## 1. 为什么必须把三个问题分开

“机器人看起来卡”不是一个根因，而是可由三类误差产生：

1. **模型轨迹误差**：模型给出的动作本身不合理，例如右手闭合不足；
2. **轨迹拼接误差**：新旧chunk在位置、速度或加速度上不连续；
3. **物理时间执行误差**：轨迹数学上平滑，但目标没有按稳定周期送到机器人。

三者在视觉上都可能表现为顿、跳或不顺。如果不先用telemetry拆开，会用平滑算法去修deadline miss，或用网络优化去修模型抓力，最终参数越来越多却没有因果闭环。

## 2. 三个不可混淆的时钟

- 模型输出50个waypoint，相邻点的训练时间语义是30Hz；50个点之间只有49个完整区间。
- G2双臂命令目标频率是100Hz，即每10ms采样并下发一次轨迹目标。
- 推理请求频率由观测、服务延迟和RTC调度决定，不是30Hz或100Hz。

30→100Hz插值不是“增加模型知识”。它只是在相邻模型点之间定义连续函数 `q(t)`，让控制器在更密的时间点采样。若相邻模型点是 `A_i,A_{i+1}`，线性插值为：

```text
q(t) = (1-u) A_i + u A_{i+1},  0 <= u <= 1
```

它不改变端点，也不会凭空提高任务质量；它减少的是离散命令台阶。

## 3. 100Hz真正约束的是deadline，不是平均值

第 `k` 个目标deadline为 `D_k=D_0+k·10ms`，实际发送时刻为 `S_k`：

```text
lateness_k = max(0, S_k - D_k)
interval_k = S_k - S_(k-1)
```

平均97Hz并不等于“接近稳定100Hz”。一个20ms停顿后紧跟一个很短的补发间隔，平均频率可能仍好看，但机器人经历的是：先较久保持旧目标，再突然跨过较大的轨迹时间。这会放大物理速度、加速度和jerk，并形成肉眼可见的周期性顿挫。

因此验收至少看 interval P50/P95/P99/max、`>15ms`次数、是否出现 `<5ms`追赶补发，以及这些异常是否与相机/HTTP阶段对齐。

## 4. chunk接缝为什么会跳

旧chunk在切换时已经承诺到真实轨迹位置 `q_old(t_switch)`。新模型却基于较早观测生成动作。若直接从新chunk索引0开始，两个坐标系的“现在”不同：

```text
实际现在 = 已执行旧轨迹的状态
模型假设现在 = 发起请求时的观测状态
```

RTC的作用不是简单把两段平均，而是告诉新生成过程：一段前缀已经不可撤销地承诺，新的suffix必须与这段事实兼容。核心可行性约束是：

```text
真实推理延迟对应的步数 d_real
必须落在生成时承诺前缀和仍可执行suffix允许的窗口内
```

若生成按 `d=20`约束，却在 `d_real=14`切换，或把relative/absolute state坐标混用，新轨迹会试图回到模型认为的过去，从而产生回拉。

## 5. chunk内部不平滑与接缝不平滑的区别

- 接缝跳：异常集中在chunk switch附近，C0位置差或C1速度差明显；
- chunk内部卡：异常分布在运动内部，常与servo interval长尾或模型waypoint局部高曲率相关；
- 模型问题：在均匀离线时间轴回放中仍出现同方向、同语义动作不足。

平滑算法只能在“保真”和“连续”间重新分配误差。更强的blend、spline或jerk限制可能降低局部尖峰，但也会增加相位滞后、削弱抓握幅度或污染尚未执行的RTC suffix。算法必须只处理明确的区域，并以模型动作偏差为副作用指标。

## 6. 为什么同一Python进程会破坏时序

相机获取、OpenCV解码、Pillow/JPEG、HTTP、JSON和100Hz Servo若在同一Python进程，即使分线程，仍共享：

- 同一个解释器和GIL调度；
- 进程CPU时间与内存分配器；
- native library线程、cache和内存带宽；
- 同一进程内同步点与垃圾回收影响。

某些native调用会释放GIL，只说明其他线程“可以”运行，不保证Servo线程会在每个10ms deadline前获得CPU。通用Linux和Python线程都不提供硬实时保证。进程隔离的价值不是使策略计算必然更快，而是把高方差工作从控制解释器中移走，降低它直接阻塞控制路径的概率。

## 7. 当前证据能证明什么

2026-08-21现场对照中，线程RTC的实际双臂频率均值约95.36Hz、interval max 24.14ms、`>15ms` 68次；Sync约97.80Hz、max 14.96ms、`>15ms`为0。旧追赶补发bug已经消失，而长间隔集中在相机请求阶段。因此“同进程竞争是当前主要根因”是强证据支持的工程判断，但还不是完成架构改造后的证明。

离线测试可以证明IPC、状态新鲜度、deadline scheduler和失败语义；只有真实G2路径能测DDS/GDK/内核/硬件共同作用下是否满足近100Hz。最终结论必须来自真机发送时间戳，而非离线 `sleep(0.01)`。

## 8. 对应行动

1. 先实现/验证单Robot-owner进程边界，避免同时改变trajectory算法；
2. 用真实interval判断deadline问题是否消失；
3. deadline稳定后，才分别对chunk接缝和chunk内部轨迹做单变量A/B；
4. 任务抓取质量最后用固定场景Sync/RTC配对rollout判断，不能由servo统计替代。

历史完整推导与实验数据见 [`RTC_ENGINEERING_DECISION_20260818.md`](experiments/RTC_ENGINEERING_DECISION_20260818.md) 和 [`RTC_SMOOTHING_ABLATION_20260821.md`](experiments/RTC_SMOOTHING_ABLATION_20260821.md)。

O10手部的30/50Hz候选、5-200Hz接口阶梯和触觉实测见
[`O10_TACTILE_AND_COMMAND_RATE_20260825.md`](experiments/O10_TACTILE_AND_COMMAND_RATE_20260825.md)。
