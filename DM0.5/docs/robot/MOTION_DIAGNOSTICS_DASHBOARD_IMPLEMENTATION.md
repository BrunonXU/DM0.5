---
doc_type: architecture
domain: g2_control
status: active
created: 2026-08-28
updated: 2026-09-01
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: [motion_diagnostics_dashboard, post_run_motion_analysis]
supersedes: []
superseded_by: []
related: [JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md, RTC_PROCESS_ISOLATION.md, ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md]
---

# G2运动抖动科研仪表盘实施合同

## 1. 目标与边界

每次真机结束后，在6000D执行一条命令，增量拉取G2日志、生成科研图并更新按日期倒序的
本地页面。诊断严格区分：

```text
模型输出 q_model → 实际下发 q_command → 硬件反馈 q_measured
```

三层信号的物理含义、Figure A--H和证据边界以
[`JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md`](JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md)为准，本篇只冻结实现。

本轮不增加动作意图标签、不先修改模型训练，也不把“更平滑”偷换为压小、限幅或消灭模型
动作。页面只能定位异常首次出现的信号层，不能仅凭不同真机rollout宣称因果。

## 2. 轻量架构

```text
G2唯一Control进程
  ├─ 原有模型/调度JSONL
  ├─ Sync/RTC实际arm+hand命令
  └─ 通过probe后才启用的34D feedback sidecar
             │ Sync/RTC可按原流程拉取；F710退出后主动上传已封口JSONL
             ▼
6000D logs/ ── NumPy + Pillow静态生成器
             └─ index.html / report.html / JSON / PNG
```

- G2只控制与采集，不运行网页、数据库或分析任务；Powered退出路径不自动出图。
- 6000D只用Python标准库、现有NumPy和Pillow，不增加React、Node、Pandas、Matplotlib或服务。
- 页面是静态HTML/CSS和少量原生JavaScript；默认`127.0.0.1`提供，远程使用IDE端口转发。
- 原始日志永不覆盖；run报告先写临时目录，再原子替换。
- F710不依赖6000D轮询G2：G2将`.partial`完整封口为`.jsonl`后，操作者输入一次6000D账户
  密码，脚本主动上传到固定接收器。接收器同样先落临时文件再原子发布；失败时G2原件保留，
  网页不会看到半文件。当前按现场偏好不配置专用上传密钥。

调研取舍：LeRobot Dataset Visualizer的episode浏览和同步曲线适合作为交互参考；PlotJuggler
适合作为复杂时序的可选专家出口；Rerun适合后续视频/三维多模态回放；OpenPI的rollout说明
逐控制步保存state/action/video的重要性。首版均不作为运行依赖，避免格式迁移与前端维护成本。

- [LeRobot Dataset Visualizer](https://github.com/huggingface/lerobot-dataset-visualizer)
- [PlotJuggler](https://github.com/facontidavide/PlotJuggler)
- [Rerun](https://github.com/rerun-io/rerun)
- [Physical Intelligence OpenPI](https://github.com/Physical-Intelligence/openpi)

## 3. 日志和run合同

每个源JSONL以“相对路径+SHA256”生成稳定`run_id`。wall time只用于日期排序；所有层内对齐
使用单调时间。首页为每轮显示`MODEL/COMMAND/FEEDBACK`徽标：

| 完整性 | 可做什么 |
|---|---|
| MODEL | 模型活动、chunk内部和chunk边界分析 |
| MODEL+COMMAND | 判断插值、preview、投影或发送时序是否新增异常 |
| MODEL+COMMAND+FEEDBACK | 延迟对齐tracking error和可观测频带分析 |

Sync的`dispatch_summary`现在与RTC一样保存14D `arm_commands_rad`、相对发送时间、调用耗时和
segment单调起点；旧日志字段缺失时仍能出MODEL或手部COMMAND图，不回填伪数据。

连续反馈写到：

```text
logs/telemetry/feedback/<主telemetry同名>.jsonl
```

每条保存schema、run/request/playback、sequence、此前drop count、读取起止单调时间、耗时、
34维位置、34维有效位和错误。发送路径只做`put_nowait`；不调用GDK的writer线程负责写盘。
队列溢出必须增加drop count，本轮相应统计不能伪装完整。

### 3.1 F710遥操兼容扩展

F710不是chunk式VLA，禁止把Pink输出写成`q_model`。平台按控制模式隔离语义：

```text
Sync/RTC：q_model → q_command → q_measured
F710：input → Cartesian target → Pink q/dq → OTG/command → q_measured
```

`powered-f710`主日志沿用`logs/telemetry/powered/`和同名单独feedback sidecar，使用
`f710_control_sample`保存映射后输入、Cartesian速度、Pink reference、constraint信息和实际
34D command。OTG未接入时必须显示“尚未接入”，不得根据位置差分伪造OTG内部状态。

首页按控制家族固定配色：Sync蓝、RTC紫、F710橙。颜色只表达运行模式；曲线仍按
reference/command/measured的固定颜色表达信号层，避免同一种颜色同时承担两种语义。
F710详情使用独立T1--T6覆盖表；原Sync/RTC的A--H定义和历史报告保持不变。

### 3.2 F710日志到页面的数据流

```text
G2 Powered运行
  → 主/feedback写入*.jsonl.partial
  → writer完整结束并原子改名*.jsonl
  → scripts/13_g2_push_telemetry.sh主动上传
  → 6000D受限接收器写临时文件并原子发布
  → 已常驻的serve_motion_dashboard.sh本地每2秒发现变化
  → 静态报告重建，浏览器自动刷新
```

因此，F710日常运行不需要在6000D重复执行`update_motion_dashboard.sh`，也不应把上传挂到
`03_reset_home.sh`：复位不等于一轮日志已经安全结束，运行退出才是正确的封口和上传边界。
`update_motion_dashboard.sh`仍保留给Sync/RTC既有拉取、历史补录或人工全量重建。
服务器`pull_new_g2_logs.sh`明确排除`*.partial`；只有原子封口后的`.jsonl`能够进入本地日志树
和仪表盘。启动门失败且没有任何有效feedback sample时，空sidecar会在G2退出路径中丢弃。

## 4. 反馈probe与自动降级

首次使用或GDK/硬件环境变化后，由操作者在G2手动运行：

```bash
./scripts/14_joint_feedback_rate_probe.sh
```

它不发送Servo或`move_ee_pos`，依次测试5/10/20Hz各300次。每档必须零失败、读取P95不超过
2ms、最大不超过5ms、周期超时率小于1%。选择通过的最高频率；仅5Hz通过时仍保持
command-only，因为不足以支撑10Hz手部命令跟踪判断。

Powered运行只读取已落盘且通过schema校验的probe结果。采样由唯一`G2RobotController`在
成功arm发送之后执行，不创建第二个Robot或GDK读取线程。单次读取超过5ms、任意失败或连续
两个arm发送间隔超过15ms时，只锁存关闭反馈诊断，动作继续。probe只证明静止读取耗时，
不等于真机100Hz已通过；首轮短Powered还必须满足arm interval P95相对基线增加不超过1ms、
不新增`>20ms`间隔，否则删除启用资格并回到command-only。

## 5. 页面、图表与判读

生成目录位于`logs/analysis/motion_dashboard/`，其中首页按日期分组、运行时间倒序；支持模式、
数据完整性和文本过滤。对比不在首页维护全局长下拉框：先进入某一轮详情，把该轮作为当前
基线，再由“选择另一轮对比”弹窗搜索候选；界面优先推荐上一轮同模式运行。每轮详情同时显示
A--H诊断覆盖表，区分“完整、部分、不可计算、尚未实现”，避免用缺图暗示功能已经完成。

图表只把`used_start_index`到`used_start_index + used_intervals`的inclusive waypoint视为实际
提交的`q_model`，不得混入50点chunk中未执行的未来预测。三层轨迹使用真实单调时间，同一关节
共享横纵坐标，并在推理空档断线；旧日志缺少segment开始时刻时，必须标注使用响应时刻近似。
累计路程、反转和jerk按每个已提交片段计算，jerk不得跨推理空档求差分，chunk连接另由边界图
单独分析。每轮报告固定包含可计算的：

1. 34DoF model/command/measured活动热图；
2. 双臂Top-2加双手Top-2问题关节三层轨迹；
3. 延迟对齐的command--measured误差热图；
4. chunk边界前后对齐的中位数和P05--P95；
5. 推理、arm和hand发送时序；
6. 净位移、累计路程、反转、P95步幅和jerk排名表。

反馈时间均匀且Nyquist足够时才允许PSD/coherence；只有RTC保存重叠新旧计划时才画plan
disagreement；只有同轨迹反事实时才画配对A/B。条件不满足必须显示“不可计算”，不能将
10Hz命令解释为5Hz以上机械频率。

判读顺序固定为：先看热图定位身体区域和chunk位置，再看Top-4中哪一层首先反转，最后用
tracking error和时序排除闭环与deadline。`q_model`先反转指向模型/chunk候选；只有
`q_command`新增反转才指向插值/拼接；命令稳定而反馈振荡才指向闭环或机构；编码器稳定但
视频仍抖时需外部视频、IMU或光学测量。

## 6. 日常流程

在6000D运行：

```bash
cd /home/xza_100850113/projects/g2_robot
./scripts/update_motion_dashboard.sh
./scripts/serve_motion_dashboard.sh --port 8765
```

`update`默认先调用现有只增不覆盖的G2日志拉取，再只分析新增或hash变化的run。可用
`--no-pull`只处理本地、`--run LOG.jsonl`处理指定文件、`--all`重建全部。`serve`默认仅监听
localhost并每2秒监视telemetry JSONL和生成器；检测到变化后离线增量重建，浏览器根据
manifest自动刷新。`--no-watch`可关闭监视。页面生成失败保留上一版页面，不影响原日志和
下一轮真机。

故障处理：缺图先看数据徽标，不手工补数组；报告失败可指定`--run`重试；sidecar有drop、
读取失败或自动关闭事件时，本轮反馈结论作废但命令层仍有效；probe失败不绕过门槛。

## 7. 验收状态与下一真机门

代码侧已完成静态生成器、增量manifest、历史日志降级、Sync arm trace、反馈sidecar、只读
probe和自动关闭逻辑。6000D历史Powered Sync已能生成MODEL+COMMAND报告；本地完整无硬件
测试为144 passed/1 skipped，G2同步后的运行时测试为76 passed。

本轮具体代码已逐文件同步G2，但仍未完成任何新的真机动作。下一门依次是：G2运行只读probe；
执行一轮短Powered Sync；检查feedback sample/drop/read duration与arm interval回归；拉回日志生成
首份三层报告。通过前不据此修改滤波、模型或RTC参数。
