# G2 当前进度与录制合同

> **2026-09-10：本阶段暂停，G2右手拇指故障送修（操作者现场确认）。** 待维修后重新验收再恢复真机采集；已有数据保留，`training_ready=false`。详见 [暂停交接记录](PAUSED_HAND_REPAIR_20260910.md)。

文档整理：2026-10-12；最后现场记录仍为2026-09-10，本次未核验维修或恢复状态。当前说明只在本页维护；启动见 [START_HERE](START_HERE.md)，按序阅读见 [文档索引](00_INDEX.md)。历史测试数量属于各自版本，不代表本轮重测。

## 1. 已完成与尚未完成

真实三图/state→DM0.5→同owner执行→START人工介入→新观测/新策略恢复已多轮运行；完整DAgger训练迭代尚未完成，始终 `training_ready=false`。

- POLICY/等待/恢复：single canonical **34D normal Servo，目标100Hz**。34D顺序是左臂7、右臂7、左手10、右手10；手部不走独立5Hz点目标。
- 人工HIL：冻结F710语义，双臂Servo100Hz、O10 changed-only约5Hz渐进点目标；direct Pink，Ruckig默认off。
- 仅START切控制权。首次START启动策略，随后START进入HIL；RB仅人工控臂，松RB仍在HIL。退出需松RB、等手部/Home完成，再START→HOLD→新观测policy。
- 同源采集与推理解耦，HIL暂停推理时仍采集。相机采集目标30Hz、action waypoint30Hz、Servo100Hz、推理请求频率是四个不同概念。
- 200ms operator watchdog和500ms sender待ACK超时仍存在；连接恢复后SAFE_HOLD需要START重进人工。发送耗时/调度超时告警与输入watchdog不是一回事。
- Windows sender v2中文流程指引依据G2回执；需下载windows_client更新本地。ACK不是SDK执行或实测反馈。

## 2. 最近证据（不是新一轮训练认证）

|运行|已核对结果|限制|
|---|---|---|
|09-08 152050|3541组观测，5次HIL/4次恢复；journal通过、预览生成|图像重复/时钟/反馈标签仍有训练阻塞|
|09-08 161753|3次约1秒输入断流并重建会话；SAFE_HOLD期间RB已收到但不执行|Windows确认待ACK超时，未确定底层断流来源|
|09-08 165733|清单跨度611秒，17823组观测，10次HIL/9次恢复，0 SAFE_HOLD|抽查拇指/关键图像，未完整训练审计|
|09-08 175457|6690组观测，5次HIL/4次恢复，0 SAFE_HOLD；journal通过；227.634秒三图视频完整解码通过|本轮完成于09-09，预览不等于训练数据导出|

175457是最近已导出的一轮；此表不声称远端之后不会产生新录制。

## 3. 三种录制模式的真实行为

|模式|采集与journal|原始图像保存|
|---|---|---|
|inference-only|持续采集，并有journal/遥测|不持久化图像|
|hil-record|持续采集|从采集开始一直保存到进程结束，包括首次START前、POLICY、HIL、等待|
|prebuffer-record（代码默认）|持续采集|约4秒有界内存预录；**首次实际进入HIL**触发提交，之后一直保存到进程结束|

首次START仅启动策略，不等于首次进入HIL。prebuffer没有触发就没有持久化图像；触发前超过缓冲窗口的journal引用也可能无payload。缓冲上限128MiB/180项先到时，保留时间可能不足4秒。当前退出HIL后不会自动回到纯预录；“前4秒＋HIL＋恢复后几秒，然后再次预录”尚是计划。

建议当前排查显式使用hil-record，方便回看因果过程；保存全部不代表训练全部。默认运行限时120秒，从首次START计时；标准命令显式duration-seconds=0，Ctrl-C结束。

## 4. 数据、存储与审计

以下短路径描述当时机载布局；开发机同一批原始文件现位于 `artifacts/data`、`artifacts/logs`、`artifacts/results`，内容不改写。原机器人路径未由此次源码整理迁移。

- data/raw/powered_hil：原始三路JPEG/PNG、各自设备时间戳/单位/时钟域、host monotonic、34D reported状态/有效性、policy请求/chunk、控制事件与SDK证据。
- data/exports：42脚本生成的三图预览MP4/JPG/阶段JSON，原速按host到达时间显示最近帧；不是训练视频。
- data/archive/offline：本地审计和分析产物。logs：遥测和console。
- G2的data/logs实际位于 `/data/logs/xza/DAgger/`，`/home/user/xza/DAgger/data`、logs是符号链接。6000D与G2是不同副本，不把本地有文件当作远端已备份。
- 保留policy proposal、human candidate、selected、submitted、SDK返回与measured的区别；手部reported有效性仍未认证，不伪造反馈或由位置差推算握力。

## 5. 下一步顺序（维修后）

当前不继续安排真机抓瓶或反复replay。操作者已确认右拇指硬件问题并送修，具体维修诊断/故障起始时间尚待补充。

1. 记录维修与标定结果，先验收空载开闭、Home和反馈，再验证右手独立抓瓶。
2. 用尽量一致的场景重跑数据集与双手交接，先排除硬件影响，再讨论姿态/策略修改。
3. 恢复HIL采集前区分修前/修后版本；旧失败段保留作诊断，不直接加入专家训练集。
4. 完成图像时间映射、34D反馈/标签及片段导出审计；再训练、部署并比较成功率。

此前的抓位/拇指模板排查是历史假设，不能用来补偿已损坏硬件；也不能将网络超时、Home确认bug等独立问题全归因于拇指。详见 [阶段暂停记录](PAUSED_HAND_REPAIR_20260910.md)。

## 6. 数据集回放对照

开发源码入口在 `G2/teleop/scripts/43_g2_dataset_unified34_replay.sh --execute`；现场已安装入口按原运行目录执行，实现复用G2上的DM0.5客户端。单34D normal/100Hz，冻结episode `sorting_20260409161901`，948个30Hz **action**，约31.567秒。不是模型推理，不需要Windows sender，不自动录制三图。具体见 [回放说明](REPLAY_UNIFIED34_20260909.md)。新入口仅离线验证，真机待用户启动。
