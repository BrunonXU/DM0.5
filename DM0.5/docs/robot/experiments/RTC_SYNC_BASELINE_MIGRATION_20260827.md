---
doc_type: experiment
domain: rtc
status: active
created: 2026-08-27
updated: 2026-08-27
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [rtc_sync_baseline_migration]
supersedes: []
superseded_by: []
related: [../ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md, HAND_V11_OFFLINE_SWEEP_20260827.md, RTC_SMOOTHING_ABLATION_20260821.md, NETWORK_TRANSPORT_AB_20260825.md]
---

# Sync已验证配置迁入RTC审计（2026-08-27）

## 问题与决定

目标不是再创建一套RTC参数，而是确认正式Sync已经验证的网络、控制和双臂结论是否真实
进入RTC，并把必要差异与配置漂移分开。结论：网络、进程隔离、Servo调度和10Hz已经共享；
正式RTC仍默认C1 Hermite是配置漂移，现已改为C2 spline；Sync V1.1与RTC cap V2都不是
最终手部方案，下一步合并为同一个共享轨迹器。

## 服务器现场事实

2026-08-27只读检查`nvidia-smi`、监听端口和容器完整启动参数，未重启或修改服务：

| 端口 | 角色 | GPU | 网络/模型共同项 | RTC特有项 | 状态 |
|---|---|---:|---|---|---|
| 7892 | 正式Sync | 7 | 75K、五步、34D、fixed-prefix compile、native binary WebSocket | 无RTC guidance | running |
| 7893 | 正式RTC | 3 | 75K、五步、34D、fixed-prefix compile、native binary WebSocket | RTC enabled、`approx_identity/0.5` | running |

两个服务使用同一`opendm:g2-34d-fa2-ws`镜像，各自占用约12.6GB显存；GPU3/7检查时
利用率均为0%。因此网络优化已经迁入RTC，不需要新端口，也不需要重启7893。GPU占用是
瞬时只读快照，Powered或Shadow前仍必须重新检查外部负载。

## 客户端逐项审计

| 项目 | Sync正式值 | RTC审计前 | 本次决定 |
|---|---|---|---|
| transport | native JPEG/PNG + binary WebSocket | 已相同 | 保持 |
| process isolation | Control唯一Robot、Policy唯一Camera | 已相同 | 保持 |
| hand rate | 10Hz | 10Hz | 保持；20/30/50Hz继续否决 |
| Servo miss语义 | 丢过期tick、不补追赶帧 | 公共Servo实现 | 保持 |
| arm internal smoothing | weight10 | weight10 | 保持 |
| arm interpolation | C2 cubic spline | 正式09默认C1 Hermite | 统一为C2 |
| bootstrap blend | 120ms cubic | 120ms cubic | 保持 |
| steady RTC blend | Sync每chunk 120ms | RTC为0 | 保持差异；RTC已有承诺前缀C0/C1 |
| hand smoothing | V1.1 | rtc_cap_v2 | 两者留作对照，等待共享轨迹器 |
| d/s | 不适用 | 16/20旧候选 | 先Shadow重标定，不从Sync延迟推算 |

本次修改三个G2入口：

- `08_rtc_shadow.sh`显式传入weight10和C2 spline；
- `09_powered_rtc.sh`默认C2 spline，日志名固定包含
  `c2_spline_hand_rtc_cap_v2`；仍允许显式Hermite回退对照；
- `10_powered_rtc_arms_only_10s.sh`统一为d16/s20、weight10和C2 spline。

## 为什么没有把V1.1直接搬进RTC

V1.1 Powered把单步P95钳到0.175rad并降低极端jerk，但45个可定位segment中42个最大
内部jerk落在最后三条10Hz命令；400ms guide后集中释放tracking error是结构性问题。
RTC cap V2则只限制首条/内部位置变化0.15/0.30rad，没有速度、加速度或jerk约束。直接
选择任一方案都会把临时补丁固化成两套技术债。

正式方向是一个Sync/RTC共享的state-continuous preview轨迹器：跨segment携带`q/v/a`，
显式限制jerk，不强迫chunk末端清偿误差；同步等待受限减速，RTC则连续接入新预测。

## 验证与证据边界

新增静态回归锁定正式RTC三个入口的WebSocket/7893、10Hz、weight10和C2参数；目标测试
通过，三个Shell入口均通过`bash -n`。本轮只修改6000D代码和文档，没有初始化GDK、
Camera、Robot或Servo，也没有发送真机动作。C2在Sync和历史RTC离线回放中成立，但新的
7893+进程隔离+C2组合仍需Shadow和短Powered，不能仅凭配置一致宣布真机通过。

## 下一步顺序

1. 补齐active/wait逐条手部telemetry。
2. 离线实现并比较jerk-limited point-to-point与preview-QP共享轨迹器。
3. 冻结网络、10Hz和双臂基线，只做一次短Sync共享轨迹器Powered门。
4. 原样接入RTC，在7893跑Shadow并根据真实observation age重标定d/s。
5. 依次运行10秒arms-only C2和有限完整Split RTC；通过后RTC成为主模式，Sync保留fallback。
