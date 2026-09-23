---
doc_type: architecture
domain: rtc
status: canonical
created: 2026-08-24
updated: 2026-08-24
models: [dm05, new_lora]
hardware: [g2, rtx6000d]
canonical_for: [rtc_process_ownership, robot_state_ipc]
supersedes: [../archive/legacy_mixed/RTC_DUAL_GDK_ARCHITECTURE_20260821.md]
superseded_by: []
related: [RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md, ../CURRENT_STATUS.md]
---

# RTC进程隔离架构：Control唯一拥有Robot

## 1. 决策

已实现两个进程，但不采用两套Robot GDK：

```text
CONTROL PROCESS                         POLICY PROCESS
sole Robot owner                        sole Camera owner
├─ demand-driven Robot state            ├─ Camera GDK
├─ Robot command                        ├─ decode / resize / JPEG
├─ safety + RTC state machine            ├─ HTTP / response parse
├─ 100Hz arms / 5Hz hands               └─ policy request assembly
└─ timestamped 34D state ring                   ▲
             └──── tiny shared-memory IPC ──────┘
```

Control process是Robot的唯一所有者：唯一初始化Robot reader/command相关GDK对象，读取34D measured state并发送动作。Policy process不创建任何Robot对象，只读取Control发布的时间戳状态快照；它独占Camera和所有高方差图像/网络工作。对应实现是`g2_robot/g2_sort/deployment/g2_client/process_pipeline.py`与`state_ring.py`。

## 2. 为什么这是更干净的边界

Python进程隔离只能隔离解释器/GIL，不能隔离两个GDK实例在DDS、kernel、网卡、SDK内部线程上的潜在竞争。让Policy再创建read-only Robot虽然不发送命令，但仍引入第二个Robot生命周期、第二组DDS订阅及“双GDK是否能共存”的验证变量。

state只有34维。即使以100Hz发布float64：

```text
100 × 34 × 8 = 27,200 bytes/s
```

再加timestamp和序号也远小于一帧图片。为了节省几十KB/s而创建第二套Robot GDK没有资源上的合理性；所有权清晰和可验证性更重要。

## 3. 状态ring buffer合同

每条state记录包含：

```text
sequence: uint64
sample_monotonic_seconds: float64
state: float64[34]
```

当前不是让Control额外跑一个100Hz state-reader线程。每轮Policy先完成Camera采集，再发`state_request(sequence)`；Control调用唯一Robot state source一次，把完整34D+采样时间写入ring，并仅回`state_sequence`；Policy按精确sequence读取。这样保持旧链路“图片后取state”的时间顺序，也不新增会与Servo争用GDK的高频读取任务。

ring是固定容量、进程共享、单写者/单读者结构；写入和读取受进程锁保护，并同时校验sequence，禁止把不同版本的timestamp与数组拼接。旧sequence被覆盖、state非finite或超过新鲜度门都会令本轮推理失败。

状态新鲜度是合同而非提示。设Policy读取时刻为 `t_read`，采样时刻为 `t_sample`：

```text
age = t_read - t_sample
```

超过冻结门限时，请求必须失败，不能静默发送陈旧观测。每轮记录Control state read、IPC wait和sample age，便于之后区分机器人状态陈旧与网络慢。

## 4. 数据流与所有权

| 资源/决策 | Control | Policy |
|---|---|---|
| Robot GDK生命周期 | 唯一拥有 | 禁止创建 |
| 34D measured state | 读取、校验、发布 | 只读IPC快照 |
| Robot命令与安全门 | 唯一拥有 | 无权限 |
| Camera GDK | 不创建 | 唯一拥有 |
| 图像decode/resize/JPEG | 不执行 | 旧HTTP执行；binary候选原始压缩帧透传 |
| HTTP/WebSocket/反序列化 | 不执行 | 执行 |
| committed prefix和RTC切换 | 唯一权威 | 只接收请求副本 |
| 过期/错序响应 | 拒绝 | 不决定执行 |

图片不跨进程；否则大帧IPC和复制会重新污染Control。Control→Policy只发request ID、RTC prefix及state sequence引用；Policy→Control只回动作chunk、请求ID和分段timing。

## 5. 失败语义

- Policy慢或HTTP超时：Control不等待到错过servo deadline；只在可执行窗口内接受结果，否则Hold。
- Policy崩溃：Camera/网络侧终止，Control停止接收新chunk并有界Hold/退出；崩溃进程没有Robot命令权限。
- state过期或读到写入中版本：Policy拒绝本轮请求。
- response request ID/epoch不匹配：Control拒绝，防止pause/resume后的旧动作复活。
- Control异常：它先关闭动作闸门；Policy无能力继续驱动Robot。

## 6. 这次架构修改解决什么、不解决什么

预期解决：Python同进程的GIL/分配器/native workload对Servo deadline的直接干扰；双Robot GDK带来的额外DDS/SDK竞争变量；故障责任边界不清。

不保证解决：Linux本身的调度长尾、Camera与Robot在系统总线上的竞争、GDK发送调用内部阻塞、模型抓握质量、chunk轨迹本身不连续、网络E2E延迟。每项都需要独立telemetry和A/B。

## 7. 验证顺序

1. **无G2单元/集成测试，已通过**：原子state快照、sequence单调、过期/覆盖拒绝、state失败传播、worker通信和关闭；G2完整套件111 passed。
2. **离线负载A/B，已通过趋势门**：相同720p Camera/JPEG/HTTP负载耗时不变，Servo P95从12.429ms降到10.066ms、max从14.547ms降到10.542ms。
3. **G2只读preflight，待连接**：一个Control Robot owner和一个Policy Camera并存，确认状态/图片持续更新且Robot command count为0。此时不再需要“双Robot GDK Preflight”。
4. **短时完整Split，待连接**：14D双臂100Hz + 20D双手5Hz，检查interval分布、`>15ms`、追赶补发、worker错误和Hold。
5. **固定场景Sync/RTC A/B**：时序门通过后再评价任务质量。

## 8. 网络优化是下一层，不与本改动混做

当前约279.5ms E2E中，model+CPU约138.9ms，non-model约140.6ms。JPEG约25.6ms、upload+parse+open约52.1ms、transform约20.4ms、未归因HTTP约16.7ms，说明transport/image pipeline值得优化。

控制隔离代码与离线A/B已经先完成，transport候选随后以显式开关实现，旧HTTP仍为默认，因此仍能单变量对照。详细协议、图像候选和质量影响见[`TRANSPORT_BINARY_WEBSOCKET.md`](TRANSPORT_BINARY_WEBSOCKET.md)。
