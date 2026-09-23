---
doc_type: experiment
domain: rtc
status: frozen
created: 2026-08-18
updated: 2026-08-18
models: [new_lora]
hardware: [rtx6000d]
canonical_for: []
supersedes: [LoRAExperiments/NewLora/06_NEWLORA_RTC_IMPLEMENTATION_AND_OFFLINE_REPORT.md]
superseded_by: []
related: []
---

# NewLora RTC 实现与离线验证报告

> 日期：2026-08-18  
> 模型：NewLora 75K，Euler 10，50×34  
> 首轮参数：`approx_identity`、guidance `0.5`、`d=20`、`s=25`  
> 状态：离线和Mock端到端通过；Powered真机结果待采集

## 1. 为什么同时使用历史rollout logs和held-out轨迹

这两种证据回答不同问题，不能互相替代。

历史真机日志直接回答部署问题：真实观测年龄、请求延迟、`d=20,s=25`是否可调度、
原始相邻chunk有多大接缝。它最接近真机，但旧日志没有逐request同步保存三路图像和
34D measured state，因此不能把同一观测重新送入模型，也无法重算RTC去噪结果。

held-out轨迹保存了模型推理必需的三路图像、34D state和后续真实动作。它用于固定seed
重跑“普通推理 vs RTC推理”，测hard prefix、切换jump、目标MAE、速度、加速度和jerk。
它不是拿验证集代替真机日志，而是补齐旧日志缺失的模型输入。

```text
历史rollout logs ──→ 调度、延迟、原始接缝
held-out完整样本 ──→ 可重跑模型、RTC前后因果A/B
Mock HTTP/Servo  ──→ 客户端与服务端连续切换合同
真机Powered RTC  ──→ 最终任务效果与实际硬件接缝
```

## 2. 历史真机日志回放

输入为最近两次continuous日志，共278对相邻chunk。结果保存于：

```text
g2_robot/logs/analysis/rtc_historical_latest2_d20_s25.json
```

| 指标 | 结果 |
| --- | ---: |
| 观测年龄mean / P95 / P99 / max | 0.490 / 0.572 / 0.658 / 0.869 s |
| 换算30Hz步数P95 / P99 / max | 18 / 20 / 27 |
| `d=20,s=25`经验可调度率 | 99.64%（278对中1对超窗） |
| 无RTC相邻chunk双臂切换max均值 | 0.07550 rad |
| 无RTC相邻chunk双手切换max均值 | 0.09802 rad |
| 无RTC hard-prefix双臂MAE | 0.01645 rad |
| 无RTC hard-prefix双手MAE | 0.02496 rad |

结论：`d=20,s=25`覆盖历史P99，但不能覆盖全部尾部；Powered代码必须在真实`d>25`
时hold并结束，不能播放已经过期的chunk。

## 3. 模型级离线A/B

样本配对使用同一held-out episode中的`t → t+25帧`，而不是稀疏验证索引中相邻的两项。
每个条件固定推理seed，避免Flow噪声掩盖RTC差异。最终统计64对。

### 3.1 为什么不部署exact VJP

`exact_vjp`保持论文式梯度引导，但模型时间约0.63--0.77秒，部分最大值超过1秒；加上
G2相机、网络和编解码后，`H=50`的`d≤25`约束很容易失效。该模式保留为数学参考，
不进入首轮Powered。

`approx_identity`不做反向传播，模型时间从0.32035秒变为0.32109秒，增量可忽略。

### 3.2 guidance权重0.5

| 指标 | 普通推理 | RTC 0.5 | 变化 |
| --- | ---: | ---: | ---: |
| hard-prefix总体MAE | 0.01375 | 0.01027 | -25.3% |
| hard-prefix双臂MAE | 0.01851 | 0.01585 | -14.4% |
| hard-prefix双手MAE | 0.01041 | 0.00637 | -38.8% |
| 双臂切换max | 0.08291 | 0.07148 | -13.8% |
| 双手切换max | 0.02791 | 0.02087 | -25.2% |
| 双臂加速度max | 75.53 | 64.79 | -14.2% |
| 双手加速度max | 55.19 | 48.56 | -12.0% |
| 后续目标总体MAE | 0.01407 | 0.01435 | +2.0% |

RTC没有把新观测完全锁死：新鲜后缀相对普通推理仍变化，双臂平均0.00148rad、双手
0.00040rad。权重1.0虽能把接缝再降低约25%--68%，但后续目标MAE增加约5%--7%，
可能进一步压制视觉纠错，因此首轮选0.5。

## 4. 实现合同

服务端在每个Euler步中加入RTC guidance。G2反馈的是已经完成限位和双臂内部平滑的
30Hz绝对承诺轨迹；服务端再按NewLora合同变换：前14维相对手臂、后20维绝对手指，
并使用同一34D norm stats归一化。

Powered时序为：

```text
bootstrap普通chunk开始执行
→ 逻辑索引25采集三图/state并发送RTC请求
→ 推理期间Servo继续执行旧chunk
→ 返回后按真实观测年龄计算d_real
→ 丢弃新chunk[0:d_real]
→ 比较旧[s+d_real]与新[d_real]接缝并切换
→ 新chunk逻辑索引达到25时重复
```

若`d_real>25`、旧chunk已耗尽、HTTP失败、request ID错误或动作检查失败，客户端hold并
结束该轮。它不会从新chunk第0步回放，也不会自动退回旧async实现。

## 5. 已通过的验证

- OpenDM RTC和mixed action模型单测：12项通过；
- G2非GDK客户端/操作测试：71项通过；
- Shell语法和Python编译检查通过；
- 真实RTC HTTP：普通0.386秒、RTC 0.371秒，均返回严格50×34；
- 完全Mock Powered RTC三请求：推理期间持续发送，按真实`d=11`完成两次切换，
  共204次Mock Servo tick；没有导入GDK或发送真机动作。

## 6. 真机日志必须回答的问题

真机运行后按`rtc_request_started`、`rtc_switch`、`action_chunk`和`dispatch_summary`统计：

1. 实际`d`的P50/P95/P99/max以及schedule miss数量；
2. RTC切换双臂/双手MAE和max是否低于历史0.0755/0.0980rad；
3. 100Hz双臂与5Hz双手实际下发接缝是否同步改善；
4. 空运行、压纸盒和双手碰撞是否减少；
5. 是否出现RTC延续了一个本来就会碰撞的旧计划。

最后一点尤其重要：RTC解决时序连续性，不是碰撞检测器。若模型意图本身错误，RTC可能
让错误动作执行得更连续；仍需物理急停、后续失败恢复数据和独立安全层。
