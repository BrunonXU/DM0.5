---
doc_type: incident
domain: rtc
status: active
created: 2026-08-21
updated: 2026-08-24
models: [new_lora]
hardware: [g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../RTC_PROCESS_ISOLATION.md, ../RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md]
---

# RTC同进程工作导致Servo deadline jitter

| 快速摘要 | 内容 |
|---|---|
| 遇到什么 | RTC比Sync更卡；旧追赶bug修复后，运动内部仍周期性出现长发送间隔 |
| 影响 | 平滑轨迹在物理时间上被不均匀采样，加速度和jerk上升 |
| 根因 | 强证据指向Camera/OpenCV/JPEG/HTTP/JSON与100Hz Servo同一Python进程竞争；架构修复尚未真机验证 |
| 如何解决 | 目标为双进程且Control唯一拥有Robot，Policy只拥有Camera/网络，通过34D timestamped ring buffer取state |
| 当前状态 | 根因定位完成，目标架构已冻结；实现与真机A/B待后续 |

## 证据

| 指标 | 线程RTC | Sync |
|---|---:|---:|
| 实际双臂频率均值 | 95.36Hz | 97.80Hz |
| interval P95 | 12.07ms | 11.16ms |
| interval max | 24.14ms | 14.96ms |
| `>15ms` | 68 | 0 |

长间隔集中在每段相机观测阶段；添加线程级 `sleep(0)` 后反而增加，说明礼让不构成deadline保证。旧 `<5ms`追赶补发已降为0，所以不是同一个scheduler bug复发。

## 第一性根因与边界

同进程线程共享解释器调度、CPU份额、内存分配/native库和内核调度。释放GIL只提供运行机会，不提供10ms期限保证。进程隔离预期降低直接竞争，但普通Linux仍非硬实时，且GDK调用可能自身阻塞。

因此根因状态是“证据充分的主要候选”，不是“修改已证明成功”。最终修复必须在G2真实发送路径上比较interval分布。架构及验证门见 [`RTC_PROCESS_ISOLATION.md`](../RTC_PROCESS_ISOLATION.md)。
