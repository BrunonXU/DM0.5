# 2026-09-08 首次START门控、三轮审计与存储

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

## 现场证据（旧自动启动实现）

完整三轮为140711、141036、141409，全部runtime_stop=duration_complete。原代码在监听前设置120秒deadline，包含等待连接时间；到期正常关闭owner和socket，Windows继续重连。不是这三轮发生网络故障。另141004为启动mode=5/error_code=2故障，零有效观测/控制；保留.partial，不能计作完整运行。

| 轮次 | 观测组 | policy chunks | 控制行 | 人工进入/恢复 | 图像payload GB | journal GB | 遥测 GB | 合计 GB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 140711 |3540|14|9385|3/2|1.372|0.078|0.042|1.492|
| 141036 |3528|2|11321|1/0|1.332|0.079|0.065|1.476|
| 141409 |3530|15|11793|3/3|1.354|0.090|0.052|1.496|

GB为十进制。三轮合计4463917892字节=4.464GB（约4.157GiB），包含逐轮主/反馈遥测，不含共享追加console，因为不能精确分摊。约120秒/轮；按此估计持续全量录制约45GB/小时，随图像压缩大小和动作比例变化。

三轮journal严格跨流/因果审计全部valid=true；journal与payload元数据引用全部一致。control嵌套SDK调用11962/11583/14440次返回均0，无safe_hold_entered；这是提交证据，不是物理动作跟踪合格证据。100Hz控制行间隔P50约10ms，P99约13ms，最大分别21.49/20.91/26.27ms；仍有抖动，未声称所有周期严格10ms。最后一轮包含Home与人工臂手组合，审计通过。

图像严格审计未通过：三路每轮各12–33次重复帧；无倒退/时钟域变化，全部payload大小/hash检查无不一致。采集循环29.39–29.50Hz，三路独立新帧29.13–29.39Hz；通过平均频率5%诊断容差不等于严格30Hz训练时间轴认证。host收到三图最大间隔约2.2–3.2ms，不等于曝光同步。原设备时钟域仍gdk_device_unverified；手部measured有效性仍未认证。报告见data/archive/offline/20260908_three_runs/audit.json。

GDK console有重复手关节“Param ... not found”日志；SDK返回0不能解释/消除该告警，尚需结合GDK配置及有效反馈核对，不能据此伪造手反馈。

## 本轮新实现（尚待现场验收）

- 连接只建立ACK/采集，不发周期Servo/手目标、不请求推理。启动前硬件初始化与只读检查仍照常进行。
- 必须同一session先观察到START释放，再有一个新的连续toggle，才产生operator_start_authorized。连接时按住、历史计数、计数跳跃、过期包不会直接开始。首次START仅授权开始POLICY，不计一次HIL介入。
- 开始后要求START之后新采集的观测才请求policy；后续START仍为POLICY→HIL→HOLD→fresh policy；断链不会自动恢复策略。
- runtime_waiting_for_start事件使审计能拒绝开始前的策略观测/控制记录。当前hil_state字段仍是内部supervisor的policy，是否运行须结合启动事件，等待阶段没有control行。
- 默认120秒现在从首次START算起；--duration-seconds 0表示一直运行到Ctrl-C。仍可显式选择1–600秒。结束会打印duration完成原因；超时退出时sender重连属于预期。
- 无sender时仍检查采集/写盘健康，避免等连接才暴露后台错误。
- prebuffer-record在首次真正进入HIL时触发持久化（即开始后的下一次START），不是首次“开始”按钮；启动前/策略期间维持约4秒有界预录，触发后保存到进程结束。hil-record仍从采集启动开始全程保存，包括等待首次START；本轮未改变该持久化策略。

## 存储实际位置

代码仍在/home/user/xza/DAgger（系统overlay盘）。该目录data、logs是符号链接：

- /data/logs/xza/DAgger/data/raw/powered_hil/<run>/journal：四路JSONL/manifest。
- 同级payload：三路原生JPEG/PNG图像序列、frames.jsonl、manifest；目前不是编码后的MP4。
- /data/logs/xza/DAgger/logs/telemetry/{powered,feedback}：逐轮遥测。
- /data/logs/xza/DAgger/logs/powered_hil_console.log：共享追加终端日志。

本轮核对时DAgger data占7.1GiB、logs约237MiB，包含历史/失败/等待连接运行；/data卷共1.5TiB、可用约1.4TiB；系统盘仍约2GiB可用。旧G2Teleop日志回退副本未删。Windows只有sender/tunnel及其本地日志，不保存这三路图像。以上是当前G2存储，不等于已备份到开发机；本轮只回传审计报告。

## 尚未完成

新开始门控/计时需要现场一次验收；重复图像去重及按时间轴筛选、设备时钟映射、34D标签/反馈、训练视频导出、数据聚合训练与模型回归尚未完成。保留原始时间戳和真实重复记录，不通过改fps/放宽审计宣称合格。RTC不是上述问题的前置条件。本轮不替换冻结F710、不改变SDK执行路线、不调整运动门限。training_ready=false。

本轮补充验证：10620/10584/10590张图像全部完成Pillow完整load与尺寸检查，合计31794张无解码/尺寸错误。原生图像可解码不等于MP4训练时间映射通过。首次START还要求三图相对开始时缓存观测均有推进，避免只重新读取旧帧就称新观测。

部署结果：本轮本地全套59 passed（2.30s），G2全套59 passed（3.34s）；G2 files-only --duration-seconds 0预检通过，明确未初始化GDK。代码和启动文档已同步。新开始语义尚未由现场运行验收；Windows sender协议未变，不要求重新复制Windows源码。
