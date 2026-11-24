# DAgger（F1 控制与采集运行时）审阅与改进执行规格

- 审阅基线：`36314a8`（2026-11-24 17:42，北京时间），对应 F1 控制当前部署版本 `F1 RTC＋局部RLT V47`；本文所有行号以该提交为准。
- 分工：只做只读审阅、不改业务代码；Codex 按第二部分逐条执行。分工原则与执行、回写约定见 [Claude-advice 说明](../README.md)，DAgger 专属规则见 [DAgger/AGENTS.md](../../DAgger/AGENTS.md#claude_advice-审阅建议的执行)。
- 状态：带 `[!]` 的条目须先由用户确认，汇总见第一部分末尾的[待人工确认决策表](#待人工确认决策表)。
- 配套审阅：模型训练、Critic/Actor 算法与 DAgger 数据导出部分见 [F1 积木 DAgger 数据与 RLT 训练审阅](../tasks/F1_ComplexyToys/review.md)。

**结论：DAgger/F1 的安全底座和 HG-DAgger 因果边界（唯一 owner、跨进程锁、`hil_latched` 严格隔离、实际发布留证）做得很扎实；当前卡住“连续 30 分钟稳定采集”和“RLT 流畅执行”的，不是缺保护，而是几处把单次最坏值或可恢复异常永久锁存的机制。** 典型表现为：RLT 首请求冷启动耗时锁死后续全部预取（退化成同步一卡一顿）、人工贴近边界时标量缩步失效导致反复暂停、人工计算子进程崩溃后无法重建、以及已被模型屏蔽的左腕相机仍通过三图硬同步拖死整条策略。

**最快跑顺连续采集的顺序：** 先修两处无需人工拍板的确定性 P0 缺陷——把 RLT 预取延迟估计从“含首请求/撤销请求的窗口最大值”改成稳态估计（D-01），并给人工计算子进程补上有界后台重建（D-03）；同步向用户确认两项现场手感/参数决策——RLT 预取窗口配合快路径（D-02）与人工边界缩步/切向投影（D-04）。随后解耦录制写盘失败的安全撤离（D-06）、可选左腕相机同步（D-05）和运动中 50ms/watchdog 诊断恢复（D-07）。

---

# 第一部分：给人看

## DAgger / RLT 现场运行逻辑大白话

一条 F1 局部 RLT / HIL 采集从开始到保存大致分 6 个环节。完整操作与三端命令见 [F1 状态](../../DAgger/docs/F1/STATUS.md) 与 [STARTUP](../../tasks/F1_ComplexyToys/inference/STARTUP.md)，这里只标出当前最容易卡断的地方。

```mermaid
flowchart LR
  C1[1 三图同步采集<br/>左腕掉帧仍拖死策略] --> C2[2 B1250 RTC 接近]
  C2 -->|十字键→| C3[3 RLT C16 预取<br/>首请求max锁死后续]
  C3 -->|START 接管| C4[4 人工计算子进程<br/>崩溃后不可重建]
  C4 --> C5[5 边界检查与缩步<br/>仅0.5/0.25标量缩步]
  C5 --> C6[6 250Hz发布与落盘<br/>写盘失败直接锁死]
  classDef stuck fill:#f8d7da,stroke:#c00;
  classDef weak fill:#fff3cd,stroke:#c90;
  class C3,C5 stuck;
  class C1,C4,C6 weak;
```

| 环节 | 大白话 | 现状 | 对应条目 |
|---|---|---|---|
| 1 三图同步采集 | 从 ROS 同步取头/左腕/右腕三帧 | 模型已屏蔽左腕，但左腕掉帧或超 skew 仍触发 `CameraUnavailable` 中断整条 | D-05 |
| 2 B1250 RTC | 主模型带前缀引导连续接近 | 有线中位 173ms，基本可用；长尾仍受无线/手柄 ACK 抖动影响 | D-07 |
| 3 RLT 预取调度 | 边跑当前 C16 前缀，边提前算下一段 | **卡点**：首请求冷启动耗时进 `max()` 队列，把后面 10 次预取全推迟 | D-01、D-02 |
| 4 人工计算进程 | 独立子进程跑 Pink IK 和碰撞预检 | **短板**：进程一旦异常退出，整场会话人工接管永久瘫痪，必须重开 | D-03 |
| 5 人工边界保护 | 贴近桶沿/躯干代理边界时先试缩步 | **卡点**：只试 0.5/0.25 标量缩步，贴边斜推仍连续 9 次拒绝并暂停 | D-04 |
| 6 发布与录制 | 250Hz 输出平滑与落盘 | 队列满或写盘报错直接抛 `RecordingUnavailable` 截断安全撤离；运动中 >50ms 抖动不可恢复 | D-06、D-07 |

## 核心问题汇总

| 严重度 | 问题 | 现场表现与代码位置 |
|---|---|---|
| S0 | RLT 预取延迟用 `max(rlt_request_durations_ns)` 且混入 bootstrap 首请求与已撤销请求 | V45/V46 现场 12 次预取全部推迟，RLT 退化为同步停顿（`rtc.py:112,126`） |
| S0 | RLT `C16` 下 `D6/S8` 窗口物理余量仅 ~160–200ms，缺少快路径或窗口配合 | 首请求外稳态偶发波动也会撞上 `maximum=8` 上限（`rlt_hil.py:352-358`、`rtc.py:130-134`） |
| S0 | 人工计算子进程死亡后无重建机制 | 子进程退出后重新松 RB/按 RB 永远报 `manual compute worker unavailable`（`manual_compute.py:61-62`） |
| S0 | 人工边界缩步仅尝试 `0.5` 和 `0.25` 两个标量比例 | 贴近桶沿或躯干代理边界时连续 9 次拒绝、反复暂停重握手（`manual_candidate.py:27-40`） |
| S1 | 三图硬同步仍强依赖左腕相机 | `right_only_v1` 已不用左腕图，但左腕 USB 偶发掉帧仍中断整条策略（`observation.py:45-46,291-358`） |
| S1 | 录制写盘/队列满直接抛 `RecordingUnavailable` 中断控制循环 | 虽已规划 `recording_failed_manual_exit_only`，但控制循环抛出异常阻止了受保护人工收尾（`runtime.py:160-163,651-652`） |
| S1 | 运动中 >50ms 输出抖动与 >200ms 输入包龄缺少分段诊断与有限恢复 | 12:44 实录 203/205ms 触发 watchdog，Windows 500ms ACK 重连 6 次（`runtime.py:591-629`、`rlt_hil.py:260-262`） |

## 改进方向与执行优先级

| 优先级 | 条目 | 一句话 |
|---|---|---|
| P0 | D-01 | RLT 预取延迟估计剔除 bootstrap/撤销请求，改用稳态分位/去极值估计，解除首请求锁死 |
| P0 | D-02 `[!]` | 确认 RLT RTC 的 `rlt_rtc_delay_steps`/`rlt_rtc_min_steps` 与共享图 75.6ms 快路径是否联用 |
| P0 | D-03 | `ManualPlanner` 增加有界后台进程重建，重建期间保持安全暂停、不自动恢复运动 |
| P0 | D-04 `[!]` | 人工候选边界拒绝增加更细缩步及可选安全切向投影，减少贴边连续暂停 |
| P1 | D-05 `[!]` | 按配置 `required_cameras` 区分必需/可选相机，左腕掉帧记缺帧掩码但不拖死策略 |
| P1 | D-06 | `RecordingUnavailable` 切换为“封存可用前缀 + 禁止入训 + 允许受保护人工低速撤离” |
| P1 | D-07 `[!]` | 补全同序号收发/ACK 诊断，并确认运动中 >50ms 输出抖动的静止后重新授权边界 |
| — | D-08 | 明确不改的硬保护清单（防止误改） |

## 待人工确认决策表

下表汇总全部 `[!]` 项。“推荐”是审阅建议，**用户逐项确认后，Codex 才能把对应条目改为 `[ ]` 并执行**，答复写入该条目。

| 编号 | 问题 | 选项 | 推荐 |
|---|---|---|---|
| D-02 | RLT RTC 预取窗口吃紧：是先启用 V40 验收过的 75.6ms 共享图加速，还是调整 `D/S` 参数，还是两者一起？ | 仅开快路径 / 仅调 `D/S` / 先开快路径再按实测调 `D/S` | 先启用已验的 75.6ms 共享图快路径（与 D-01 配合），稳态延迟压到 ~100ms 内后无需激进放宽 `D/S` |
| D-04 | 人工贴近代理边界时，是否允许在标量缩步失败后做“单轴/切向安全分量保留”（类似贴墙滑动）？ | 只加更细标量缩步（0.5/0.25/0.125）/ 缩步失败后再试剔除越界方向分量 | 先加 `0.125` 缩步 + 可开关的逐轴/切向安全分量回退（须通过同款 `check_preventive` 中点与终点双验），默认开启并保留事件日志 |
| D-05 | 是否允许 `right_only_v1` 策略运行时把左腕相机降为可选流（缺帧记掩码、不填假图）？ | 允许（仅主图+右腕必需）/ 维持三图强制同步 | 允许；但涉及 schema 演进（与 `tasks/F1_ComplexyToys` 的 R-08 联动），需显式版本号 |
| D-07 | 运动中 >50ms 输出时序异常：是否允许“立刻撤销旧目标→发布 HOLD→实测静止后由操作者重新授权”？ | 维持硬停重开 / 允许静止核验后重新授权 | 先补只读诊断确认根因；待拿到驱动保持证据后再决定是否开放静止后重新授权 |

---

# 第二部分：给 Codex 的执行规格

## 0. 通用约束（每条都适用）

**执行规则不在这里重复。** 通用状态标记、执行顺序和回写格式见 [Claude-advice 说明](../README.md#codex-执行与回写约定)；DAgger 的冲突优先级、执行范围和硬保护清单见 [DAgger/AGENTS.md](../../DAgger/AGENTS.md)。

**硬保护在代码中的位置**（只供定位，清单以 `DAgger/AGENTS.md` 为准）：

| 硬保护 | 代码位置 |
|---|---|
| 唯一输出 owner 与发布前最终安全检查 | `DAgger/F1/src/f1dagger/robot_io.py`、`DAgger/F1/src/f1dagger/runtime.py:590` |
| 真实 F710 START 唯一开条与接管语义 | `DAgger/F1/src/f1dagger/rlt_hil.py:241-368`、`DAgger/shared/src/dagger_core/hil_supervisor.py` |
| 反馈新鲜度与 200ms watchdog | `DAgger/F1/src/f1dagger/observation.py:267-269`、`DAgger/F1/src/f1dagger/rlt_hil.py:260-262` |
| 代理碰撞/关节限位/耦连腕限位锁存 | `DAgger/F1/src/f1dagger/rlt_hil.py:216-220`、`DAgger/F1/src/f1dagger/runtime.py:64-80,480-489` |
| RTC 期限检查与不延长旧动作权限 | `DAgger/F1/src/f1dagger/rtc.py:45-56,193-200` |
| Shadow 严格零输出、零录制 | `DAgger/F1/src/f1dagger/rlt_hil.py:64-67,395-402` |

**验收环境**：在 DAgger 根目录使用已验证 Python 解释器运行受影响单元测试与跨模块回归，涉及 F1 打包或运行端发布时必须按 `DAgger/AGENTS.md` 执行 ARM 核对与零输出 SHADOW。

```bash
cd DAgger
python -m pytest F1/tests shared/tests -q
```

---

## D-01 `[ ]` P0：解除 RLT 预取延迟估计被首请求和已撤销请求的最大值锁死

**位置**：`DAgger/F1/src/f1dagger/rtc.py:35,99-149,289-296`。

**当前行为**：

```python
        # DAgger/F1/src/f1dagger/rtc.py:100-134
        ready = timing.get('completed_monotonic_ns', now)
        duration = max(0, ready-timing['requested_monotonic_ns'])
        self.rlt_request_durations_ns.append(duration)
        ...
        elapsed_ns = max(0, now-origin)
        estimate_ns = max(self.rlt_request_durations_ns, default=0)
        required = max(self.minimum_delay_steps,
                       math.ceil((elapsed_ns+estimate_ns)/1e9*self.waypoint_hz)+1)
        maximum = min(shift, self.rtc_horizon-shift,
                      self.s_min, self.rtc_horizon-self.s_min)
        if required <= maximum:
            return required
```

问题链条：
1. `timing`（`:291-294`）没有记录 `bootstrap` 标志，导致冷启动首请求（通常包含建连/预热长尾）与 `revoked_epoch=True` 的异常/取消请求全部进入 `self.rlt_request_durations_ns`；
2. `_rlt_prefetch_delay` 取窗口内绝对最大值 `max(...)`，只要有一个 >200ms 的首请求滞留在 `maxlen=10` 队列里，后续所有预取都会因 `required > maximum` 被推迟，进而永远没有新的稳态预取样本把旧最大值顶出队列，形成**自锁死循环**。

**目标行为**：
- `bootstrap=True` 或 `revoked_epoch=True` 的请求耗时照常写入诊断事件 `rtc_request_duration_observed`（新增 `bootstrap` 与 `included_in_estimate` 字段），但**不污染稳态预取延迟队列**；
- 稳态估计采用抗单点离群值的稳健统计量（如队列长度 `>= 3` 时取去掉最高 1 个离群值后的最大值，即 `sorted(d)[-2]`；队列为空或 `< 3` 时取 `max(d, default=0)`），并保持 `maxlen=8`；
- 连续发生预取推迟达到一定次数后不会永久锁死：稳态 bootstrap 重规划若耗时本身已明显低于旧离群值，允许用独立的最近稳态窗口衰减或只计入非冷启动（`request_id > 1` 且非撤销）的完成时长，确保恢复顺畅后队列能自然更新。

**具体改法**：
1. 在 `rtc.py:291-294` 的 `timing` 字典中增加 `bootstrap=bootstrap` 和 `is_first_rlt_request=(len(self.rlt_request_durations_ns) == 0 and bootstrap)`；
2. 在 `_observe_rlt_request_duration`（`:99-116`）中：
   - 若 `timing['epoch'] != self.supervisor.policy_epoch`（即 `revoked_epoch=True`），仅 `emit` 诊断事件，不 `append` 到 `self.rlt_request_durations_ns`；
   - 若 `timing['bootstrap']` 为真且为该段首次进入 actor 的冷启动请求（可在 `rlt_hil.py:_reset_base` 重置计数，首个 bootstrap 标记为 `cold_start=True`），仅 `emit` 诊断事件，不 `append` 到稳态队列；后续非冷启动 bootstrap（因迟到/暂停后重规划）允许 `append`，避免全推迟后队列永远为空；
3. 在 `_rlt_prefetch_delay`（`:126`）中，将 `estimate_ns = max(self.rlt_request_durations_ns, default=0)` 改为：
   ```python
   samples = sorted(self.rlt_request_durations_ns)
   estimate_ns = samples[-2] if len(samples) >= 3 else (samples[-1] if samples else 0)
   ```
4. 在 `rtc_prefetch_deferred` 事件中补充 `estimate_strategy='trimmed_max_v1'` 与 `sample_count=len(samples)`，便于现场日志核对。

**硬保护（不得改动）**：
- `rtc.py:45-56` 的 `inference_deadline` 与 `plan_exhausted` 期限检查保持不变；
- `rtc.py:193-200` 的 `late_adoption` 检查保持不变；
- 绝不平移观测网格或延长旧动作有效期。

**验收与测试**：
- 在 `DAgger/F1/tests/` 新增回归用例：注入 1 次 450ms 冷启动首请求 + 后续 120ms 稳态请求，验证不会触发后续预取全推迟；再注入 1 次单点 400ms 抖动，验证在 3 个以上稳态样本下不会锁死后续预取；再注入连续 3 次 350ms 慢请求，验证会正确推迟并安全走 `plan_exhausted -> HOLD`。
- 使用 V45/V46 现场实录做离线重放，核对预取采纳率与零超时退化。

**风险与回滚**：
- 风险：若网络出现持续高延迟但前 2 次被忽略，可能多发生 1 次 `inference_deadline` 暂停后重规划（安全，已由硬保护兜底）。
- 回滚：还原 `rtc.py` 的 `_observe_rlt_request_duration` 与 `_rlt_prefetch_delay` 即可。

---

## D-02 `[!]` P0：RLT RTC `C16` 窗口余量与 75.6ms 共享图快路径联用确认

**状态**：`[!]` 待人工确认。

**位置**：`DAgger/F1/src/f1dagger/rlt_hil.py:352-358`、`DAgger/F1/src/f1dagger/rtc.py:130-134`。

**当前行为**：

```python
        # DAgger/F1/src/f1dagger/rlt_hil.py:352-358
        if self.rlt_rtc:
            self.minimum_delay_steps = int(self.rlt_policy.config.get('rlt_rtc_delay_steps', 6))
            self.s_min = int(self.rlt_policy.config.get('rlt_rtc_min_steps', 8))
            if not 1 <= self.minimum_delay_steps <= self.s_min <= 16-self.minimum_delay_steps:
                raise ValueError('RLT RTC requires committed-prefix coverage within C16')
```

在 `C16`（533ms）下，`s_min=8`、`minimum_delay_steps=6` 时，`maximum = min(shift, 16-shift, 8, 8)` 封顶 8 步（266ms）。扣除 `elapsed_ns`（1~2 步）和 `+1` 步余量，实际允许的完整请求耗时仅 ~166–200ms。而未开共享图时服务端推理本身就需要 ~185–207ms，物理上几乎不可能不推迟。

**需要用户确认的问题**：
1. 是否优先在 GPU3 生产装配开启 V40 已离线验收的 75.6ms 共享图加速（`--rlt-reference-compute`，平移差 0.0236mm、姿态差 0.00130°、零新增 IK 拒绝），先用算力把稳态总延迟压进 130ms？
2. 在开启快路径后，是否保持 `rlt_rtc_delay_steps=6, rlt_rtc_min_steps=8` 不变，还是允许在有线环境下单独做 `D5/S7` 零输出对照？

**目标行为与改法（待确认后执行）**：
- 若用户确认“先开快路径 + 保持 D6/S8”：仅按既有装配流程在 H20/F1 配置中显式绑定 `--rlt-reference-compute` 与 manifest 身份，跑真实相机零输出 SHADOW 验收连续采纳率，不改 `rlt_hil.py` 硬约束。

**硬保护（不得改动）**：
- `1 <= minimum_delay_steps <= s_min <= 16 - minimum_delay_steps` 合法性校验不变；
- `committed_actions16` 逐位一致性校验（`rlt_policy.py:206`）不变。

---

## D-03 `[ ]` P0：人工计算子进程崩溃后的有界后台重建

**位置**：`DAgger/F1/src/f1dagger/manual_compute.py:22-149`、`DAgger/F1/src/f1dagger/runtime.py:523-532`。

**当前行为**：

```python
        # DAgger/F1/src/f1dagger/manual_compute.py:60-62
        now = self.clock()
        if self.closed or not self.process.is_alive():
            raise ManualReferenceChanged('manual compute worker unavailable; release RB')
```

子进程一旦因任何异常退出，控制循环永远只收到 `ManualReferenceChanged`，不会启动新进程；操作者松 RB、回中、再按 RB 也无法恢复人工控制，只能强退整场会话。

**目标行为**：
- 检测到 `not self.process.is_alive()` 或管道断开（`EOFError`/`OSError`）时，若 `not self.closed` 且重建次数未超上限（`max_restarts=3`）：
  1. 立即 `invalidate()`、丢弃所有旧 `pending`/`accepted`、清理死进程句柄；
  2. 在独立后台守护线程中 `spawn` 新子进程并等待 `ready`（不阻塞 250Hz 控制循环！）；
  3. 后台重建期间，`poll()` 继续抛出 `ManualReferenceChanged('manual compute worker restarting; release RB')`，保持安全暂停；
  4. 新进程就绪后，原子替换 `self.process` 与 `self.connection` 并递增 `self.generation`，发出诊断事件；操作者仍须按既有流程**松 RB、回中、新 RB 边沿**显式重新授权后才恢复运动；
  5. 若超过 `max_restarts=3` 或 `close()` 已调用，则锁存不可恢复状态并给出明确提示。

**具体改法**：
1. 在 `ManualPlanner.__init__` 保存 `self._spec = spec`、`self._factory = factory`、`self._startup_timeout = startup_timeout`、`self._max_restarts = 3`、`self._restart_count = 0`、`self._restart_lock = threading.Lock()`、`self._restarting = False`、`self._restart_error = None`；
2. 抽取 `_spawn_worker()` 与 `_trigger_restart()`；在 `poll()` 遇到 `not self.process.is_alive()` 或 `EOFError`/`OSError` 时调用 `_trigger_restart()` 再抛 `ManualReferenceChanged`；
3. 在 `close()` 中加锁标记 `self.closed = True`，确保正在后台启动的进程也会被安全 `terminate()`/`kill()`，不留孤儿进程。

**硬保护（不得改动）**：
- 重建绝不发生在 250Hz 控制循环线程内；
- 重建完成后绝不自动恢复运动，必须经过 `runtime.py:129-158` 的 `recovery_anchor` 与新 RB 边沿授权；
- 不改变子进程 `SIG_IGN` 与“无 ROS / 无输出权限”边界。

**验收与测试**：
- 在 `DAgger/F1/tests/` 新增单测：用假 worker 模拟运行中 `os.kill(pid, SIGKILL)`，验证 (a) `poll()` 立即抛 `ManualReferenceChanged` 且耗时 < 5ms（不阻塞控制线程），(b) 后台重建成功后下一次新 generation 的 `poll()` 返回合法结果，(c) 连续崩溃超过 3 次后停止无限重启并报错，(d) `close()` 能干净回收正在重启的子进程。

**风险与回滚**：
- 风险：后台 `spawn` + Pink/URDF warmup 在 ARM 上会短时占用 CPU，须确保重启期间机器人处于静止 HOLD 且有 `max_restarts=3` 上限。
- 回滚：回退 `manual_compute.py` 提交即可。

---

## D-04 `[!]` P0：人工候选边界拒绝细化缩步与可选安全切向投影

**状态**：`[!]` 待人工确认。

**位置**：`DAgger/F1/src/f1dagger/manual_candidate.py:7-41`、`DAgger/F1/src/f1dagger/manual_compute.py:170-185`、`DAgger/F1/src/f1dagger/runtime.py:491-512`。

**当前行为**：

```python
    # DAgger/F1/src/f1dagger/manual_candidate.py:27-41
    for scale in (.5, .25):
        target = candidate.copy()
        target[active] = held[active] + scale*(candidate[active]-held[active])
        mapping.check_action(target)
        midpoint = target.copy()
        midpoint[active] = (held[active]+target[active])*.5
        try:
            evidence = gate.check_preventive(target, full)
            gate.check_preventive(midpoint, full)
        except CandidateSafetyRejected:
            continue
        return target, dict(scale=scale, clearance_m=evidence['clearance_m'],
                            collision_pair=evidence.get('collision_pair'),
                            rejected_clearance_m=gap)
    raise error
```

当操作者贴近桶沿/躯干代理边界且摇杆略带向内分量时，`0.5` 和 `0.25` 依然向边界内移动，直接穿透 `raise error` 触发强制暂停；现场 V45 一次短条即连续出现 9 次边界拒绝暂停。

**需要用户确认的问题**：
1. 在 `(.5, .25)` 之后增加 `.125` 标量缩步是否同意？
2. 若标量缩步全失败，是否允许启用**逐关节/切向安全分量回退**（按每个关节增量对 `clearance_m` 的符号影响，仅保留使距离不减小或平行移动的安全分量、清零导致越界的关节分量，且最终候选仍必须通过 `mapping.check_action` + `gate.check_preventive` 终点与中点双验）？

**目标行为与改法（待确认后执行）**：
1. 扩展缩步序列为 `(.5, .25, .125)`；
2. 若用户批准切向安全分量回退：仅当 `allow_axis_projection=True` 且所有标量缩步失败时，逐个试探保留活动臂 7 个关节中不会恶化 `clearance_m` 的增量组合（最多 7 次单轴筛选 + 1 次合成终点与中点双验），若合成结果与 `held` 差异大于最小步长且通过 `check_preventive` 则返回，并在 `evidence` 中显式标记 `strategy='safe_axis_projection'`；否则仍原样 `raise error` 进入人工暂停。

**硬保护（不得改动）**：
- 绝不放宽 `gate.check_preventive` 阈值或跳过中点/终点检查；
- 绝不修改非活动臂或身体/头部目标（`:25-26` 保持不变）；
- 当前实测姿态或 `held` 本身不安全时仍立即硬停（`manual_compute.py:177-180` 与 `runtime.py:497` 保持不变）；
- 绝不应用于策略（VLA/RLT）或 Home 规划。

**验收与测试**：
- 使用 V45/V42 现场保存的 9 次真实人工边界拒绝事件做数值回放，验证贴边斜推时可安全切向滑行而不越界，且垂直顶向障碍物时仍正确拒绝。

---

## D-05 `[!]` P1：按配置 `required_cameras` 解耦可选左腕相机同步

**状态**：`[!]` 待人工确认。

**位置**：`DAgger/F1/src/f1dagger/observation.py:45-46,290-359`。

**当前行为**：

```python
        # DAgger/F1/src/f1dagger/observation.py:45-46
        if tuple(f.camera for f in self.frames) != CAMERAS:
            raise ValueError('ordered three-camera observation required')
```

`RosObservationSource.queued_rows`（`:291,300,305`）与 `capture`（`:331-358`）无条件遍历 `CAMERAS = ('head', 'left_wrist', 'right_wrist')`，左腕相机哪怕已不在 `right_only_v1` 模型输入内，只要掉帧或超 skew 仍会直接报 `CameraUnavailable` 中断整条策略。

**需要用户确认的问题**：
1. `right_only_v1` 模式下，是否允许 `RosObservationSource` 按配置 `required_cameras = ('head', 'right_wrist')` 采图，左腕作为可选流（到则录、缺则显式标记 `left_wrist` 缺失、绝不伪造旧帧或黑图）？
2. 若允许，原始录制 schema 是否同步升为 `schema_version=5` 并先同步 `F1-4242` / `tasks/F1_ComplexyToys` 读取器？

**目标行为与改法（待确认后执行）**：
- 仅在用户确认 schema 升级路径后实施；`required_cameras` 保持严格同步与时钟单调性检查；可选相机缺失时在 `Observation` 记录 `camera_present_mask`，严禁复制上一帧或填充占位字节冒充实测。

---

## D-06 `[ ]` P1：录制写盘失败解耦，保全可用前缀并允许受保护人工安全撤离

**位置**：`DAgger/F1/src/f1dagger/runtime.py:160-163,637-652,785-795`、`DAgger/F1/src/f1dagger/rlt_hil.py:135,293-301`。

**当前行为**：

```python
        # DAgger/F1/src/f1dagger/runtime.py:650-652
                **{k: v for k, v in evidence.items() if k != 'command_sequence'}):
            from .faults import RecordingUnavailable
            raise RecordingUnavailable('control journal full/failed')
```

`rlt_hil.py:293-301` 已经写了 `recording_failed_manual_exit_only` 分支（禁止交回策略和 Y Home，只允许人工撤离），但 `runtime.py:163` 和 `:652` 在 `record_event`/`control` 失败时直接抛 `RecordingUnavailable` 打断整个 `step()`，导致操作者无法以受保护人工模式安全把手臂移离桶口。

**目标行为**：
- 首次捕获 `RecordingUnavailable` 时：
  1. 立即置 `engine.recording_failed = str(exc)`、封存此前已完整落盘的前缀并写入明确标记 `recording_incomplete=True, training_ready=False`（确保无证据动作绝不进入 SFT 或 RL buffer）；
  2. 撤销全部策略授权（`policy_epoch += 1, policy_enabled = False, invalidate(now)`），进入 `HIL_LATCHED` 的 `recording_failed_manual_exit_only` 状态并播报系统告警；
  3. 后续控制循环不再因 `recorder` 返回 False 再次崩溃，允许操作者按住 RB 在完整碰撞/限位/反馈保护下安全把手臂撤离后按 Ctrl+C 退出。

**硬保护（不得改动）**：
- 策略与自动 Home 必须被彻底锁死禁止（`rlt_hil.py:135,293-301` 保持不变）；
- 硬件碰撞、关节限位、反馈新鲜度和唯一 owner 检查全部保持；
- 标记 `recording_incomplete=True` 的条目必须被 `rlt_data_policy.py` 与 SFT 导出器硬拒绝，不得自动入训。

**验收与测试**：
- 在 `DAgger/F1/tests/` 注入 `recorder.control` 在第 N 步返回 False，验证：(a) 策略立即停止并撤权，(b) 已落盘前缀安全封存且标记 `training_ready=False`，(c) 人工 RB 仍可在完整碰撞保护下撤离，(d) 无法再交回策略或触发 Y Home。

**风险与回滚**：
- 风险：降级后操作者可能误以为还在正常录制；必须通过终端高亮提示和语音明确提示“录制已中断，仅允许人工安全撤离后退出”。
- 回滚：还原 `runtime.py` 中对 `RecordingUnavailable` 的降级分支即可。

---

## D-07 `[!]` P1：运动中 >50ms 输出抖动与 >200ms 输入包龄的诊断补全与恢复边界

**状态**：`[!]` 待人工确认。

**位置**：`DAgger/F1/src/f1dagger/runtime.py:591-629`、`DAgger/F1/src/f1dagger/input_recovery.py:34-68`、`DAgger/F1/src/f1dagger/rlt_hil.py:260-262`。

**当前行为**：
- `runtime.py:595-596` 仅允许 `StationaryOutputTimingFault` 在静止且未发布时恢复；运动中任意一次 >50ms 调度毛刺即锁存硬停；
- `rlt_hil.py:260-262` 在包龄 >200ms 时直接 `disconnect(now, 'operator_transport_stale')`，现场 12:44 实录出现 203/205ms 包龄触发暂停。

**需要用户确认的问题**：
1. 在不放宽 50ms 与 200ms 门槛的前提下，是否先补齐 Windows sender 与 F1 receiver 同 `sequence` 的发送排队/写 socket/收包/ACK 分布统计？
2. 对于运动中偶发 >50ms 输出抖动，是否需要先核实下位驱动在 50–100ms 间隙的真实保持行为，再决定是否开放“撤销旧目标→发布 HOLD→反馈静止 200ms 后人工重新授权”的恢复路径？

**硬保护（不得改动）**：
- 严禁放宽 200ms 手柄 watchdog 或 50ms 输出时序门槛；
- 严禁在超时后自动重放旧 START 或旧摇杆包。

---

## D-08 `—`：明确不改的项（防止 Codex 走偏）

以下机制属于 DAgger/F1 的核心安全与数据真实性边界，**任何条目执行期间均不得削弱或绕过**：

1. **真实 F710 START 唯一开条授权**：不得改回终端 Enter，不得通过 SSH/脚本远程伪造 START 或手柄心跳。
2. **唯一输出 owner 与跨进程 `F1_CONTROL_LOCK`**：不得删锁、不得跨进程强抢控制权。
3. **不向缺失左夹爪发布命令、不用反馈回填 action**：保持 `state_valid_mask` 与 `action_valid_mask` 的物理语义。
4. **不延长旧动作有效期、不平移观测锚点**：RTC 迟到只能撤销或等原计划到期后重规划。
5. **Shadow 严格零输出、零录制**：不得为了测试方便让 shadow 写入 episode 或发布硬件指令。
6. **不混用 F1 与 G2 的控制合同**：F1 保持 16D（度数 + 0–100 夹爪），不动 `shared/` 的无机器人依赖边界和 G2 代码。
