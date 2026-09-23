---
doc_type: archive
domain: rtc
status: superseded
created: 2026-08-24
updated: 2026-08-31
models: [new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: [02_IMPLEMENTATION_STATUS.md, ../../g2_robot/docs/Bruno.md]
superseded_by: [../CURRENT_STATUS.md]
related: [robot/RTC_PROCESS_ISOLATION.md, robot/INFERENCE_LATENCY_FIRST_PRINCIPLES.md, robot/ACTION_SMOOTHING_FIRST_PRINCIPLES_AND_PRIORITY.md, robot/JOINT_COMMAND_FEEDBACK_DIAGNOSTICS.md, robot/MOTION_DIAGNOSTICS_DASHBOARD_IMPLEMENTATION.md, robot/experiments/O10_TACTILE_AND_COMMAND_RATE_20260825.md, robot/experiments/HAND_V11_OFFLINE_SWEEP_20260827.md, robot/experiments/SHARED_HAND_TRAJECTORY_OFFLINE_ABLATION_20260827.md, robot/experiments/SYNC_ARM_MICRO_TREMOR_ABLATION_20260827.md]
---

# 截至2026-08-31的历史进度（已被替代）

> 2026-09-07归档；正文完整保留旧实验与当时判断，不代表当前方案。当前状态见 [CURRENT_STATUS](../CURRENT_STATUS.md)。正文原相对路径以 docs/ 为基准。

## 已确认

- NewLora 75K已经完成训练、离线checkpoint选拔、视觉增强配对验证和真机Powered运行；右手抓不紧属于当前模型/数据质量问题，暂不作为RTC架构阻塞。
- 五步Euler + fixed-prefix `torch.compile`已把服务端 model+CPU 从约222.0ms降到约138.9ms；最近基准E2E约279.5ms。离线均值质量未见可见退化，但没有完成compile单变量的严格真机任务成功率A/B。
- RTC旧调度追赶补发bug已修复；剩余卡顿证据更指向控制deadline miss，而非单纯chunk接缝或模型推理时间。
- 原线程版RTC把Camera/OpenCV/JPEG/HTTP/JSON与100Hz Servo放在同一Python进程；最新旧架构对照中RTC出现68次 `>15ms` 间隔，而Sync为0。
- 新架构代码已经改为Control唯一拥有Robot、Policy唯一拥有Camera，并用带sequence/timestamp的34D shared-memory ring传state。Policy不再创建第二个Robot reader。
- 同一720p三相机+JPEG+HTTP负载的离线A/B中，同进程Servo interval P95/max为12.429/14.547ms，隔离后为10.066/10.542ms；负载耗时0.702/0.704s基本相同。它证明Python工作隔离有效，不等于真机100Hz已经通过。
- 约140ms non-model已经改为可对账计时：客户端观测/编码/请求、传输残差、服务端multipart/state/image/RTC解析、transform、响应编码和最终残差分别记录。
- 原始GDK JPEG/PNG透传、persistent WebSocket binary和`50×34 float32`响应已进入两个固定正式端口：Sync为GPU7/7892纯Sync服务，RTC为GPU3/7893独立服务。两者使用同一镜像、75K checkpoint、五步Euler和fixed-prefix compile；7893额外启用`approx_identity/0.5` RTC guidance。7896/7894停止，不再增加端口。
- 同GPU独占匹配服务中，current JPEG+HTTP的P50/P95为163.04/171.11ms；native JPEG+WebSocket最终100次为152.15/163.06ms，请求图像字节减少28.23%，响应约32.6KB降到7.8KB。N3是Shadow候选，不是生产默认。
- G2 N3真实三相机Shadow已运行68.37秒、完成348请求：E2E-to-Control P50/P95/P99为184.95/211.37/260.69ms，native三图平均262434 bytes，camera decode与JPEG encode均为0，Servo发送为0。手动终止时的ConnectionClosedOK/worker failure属于关闭race。
- 10Hz Sync中出现一次1.852秒策略请求，其中服务端模型/CUDA段1.708秒；7892当时与他人OpenPI训练共享GPU4且GPU为100%，没有recompile、重启或OOM。GPU1阶段7896候选无新重负载时暖态P50/P95为163.4/174.0ms；`zxl_100881968`约9.4GB外部任务出现后退化为260.0/294.0ms，促使正式Sync的计算迁到已释放的GPU7，同时端口恢复为7892。
- 左右末端均已确认是每侧10主动关节的O10 T2；当前HAL无有效触觉阵列。50Hz因高频振动声否决，30Hz出现强烈震荡，20Hz仍然抖动，三档均已否决；10Hz成为Sync和RTC唯一正式候选频率。Async、复位和回放仍保持5Hz。7893已部署但新配置尚未重新Shadow、d/s标定或带电验收。
- 新网络下两轮10Hz目标500秒运行均由操作者主动暂停，共56个请求；合并E2E P50/P95/max为225.8/260.0/279.5ms，模型段P50约141.8ms，手部调度约10.0Hz且无deadline miss。这些是人工截断样本，不构成500秒长稳结论。
- 当前Sync平滑均有实际运行证据：双臂120ms边界融合和14D jerk weight 10、双手400ms边界guide与V1速度/加速度tracker都在两轮10Hz telemetry中启用。但“启用”不等于已经最优：最新56个chunk的双手实际最大jerk分布仍有约14--25rad/s³的中位数和约132--157rad/s³的P95，最坏约250rad/s³；V1历史配对证据也显示内部步进P95约增加2.1%，会把少量边界冲击转移为chunk内部追赶。
- 随后两轮10Hz复测共30个chunk，操作者明确报告“很抖”。相对此前56个chunk，手部实际jerk P95从137.9升至157.0rad/s³，`jerk>100`的chunk比例从21.4%升至26.7%，手部最大单步P95仍约0.20rad；但边界首步P95仅约0.002--0.003rad，证明主要残余位于chunk内部而非边界。第一轮末尾另有一次O10 `move_ee_pos failed`，第二轮一次模型/CUDA长尾1.420s导致E2E 1.495s。当前Sync手部V1不再视为可接受正式基线，暂停继续重复Powered。
- 双臂Sync插值已用最新28个和此前54个可严格重建边界的weight10 chunk复验五种方法。C2 spline相对linear在两组分别把blend后内部jerk P95降低40.4%/39.7%，chunk jerk max P95降低54.3%/62.1%，最大相对轨迹偏差约0.00145rad且无物理限位违反；C2在两组均为jerk最优。正式Sync和实验Async入口现均显式选择C2；Async保留60ms/weight0/双手5Hz，并由`c2_spline`日志tag避免与旧linear实验混淆。
- 首轮正式C2 Sync取得37个完整chunk：同轨迹反事实中C2相对linear继续降低内部jerk P95约39%，但本轮原始模型轨迹比此前更激烈。实际双手单步P95约0.200rad，V1命中速度/加速度限制26/54次；双臂最大单步0.01909rad且存在内部尖峰。当前剩余抖动由双手内部追赶主导，同时包含少量双臂内部和边界动力学，不是Servo deadline miss主导。
- 手部V1.1已经完成一次46-chunk Powered Sync。相对旧V1的非同轨迹运行，per-chunk手部步幅P95从约0.200降到0.175rad，实际jerk峰值P95从221.5降到142.9rad/s³，等待窗累计位移最大值从0.400降到0.172rad；但tracking error峰值P95从0.128升到0.188rad。42/45个可定位chunk的最大内部jerk落在最后三条10Hz命令，证明400ms guide后释放残差形成尾部追赶。V1.1保留为Sync可回退基线，不直接复制为RTC最终手部方案。
- RTC客户端已把已验证的双臂结论统一到正式入口：08 Shadow、09完整Split和10 arms-only均显式使用weight10+C2 cubic spline；正式日志tag为`c2_spline_hand_rtc_cap_v2`。网络、进程隔离、Servo禁止补发追赶帧和10Hz均已共享。`rtc_cap_v2`仍只是0.15/0.30rad位置cap，不是最终共享手部轨迹器。
- Sync/RTC共用的纯10Hz手部轨迹器首轮离线消融覆盖6份Powered Sync日志、169个chunk和321个关键事件；P2P因意图遗漏淘汰，preview封装为唯一`shared_preview_v1`，Sync/RTC CLI共用同一实现，09 RTC仍保持旧cap。初版8个文件曾完成G2静态核验并进行首次Powered，但其硬导数约束结论已被下述真机反例否决。
- 首次共享preview Powered实际配置100秒，但第6次请求因QP输出左手joint[5] `-0.000186366rad`、越过0下限而被GDK拒绝，不是写死时长。根因是QP后缺少20D位置约束；同时首段前两次手命令仅隔49.60ms，且原80rad/s³硬盒约束会压制模型意图。修正版仅硬约束物理位置，速度/加速度/jerk均warning-only，并把首个周期对齐100ms。
- 软约束复验在同一169个chunk上选择acceleration penalty 0.02、jerk penalty 5.0、decay 0.2：321事件零遗漏、jerk P95 14.60rad/s³、tracking error P95 0.0932rad、最大步幅自然为0.1700rad、1519步零不收敛；penalty 10因遗漏6个事件否决。修正版随后完成43-chunk/286-QP-step Powered：零位置越界、零不收敛、零Servo failure，首段手命令间隔恢复97.89/101.79ms。相对V1.1非同轨迹run，手部单步P95从0.1750降到0.1315rad、实际jerk P95从142.9降到62.1rad/s³；但手部仍主导大幅位置突跳，双臂实际jerk P95为4337rad/s³且最大jerk仅4/43位于边界blend，当前细碎高频抖动主要仍需从双臂chunk内部排查。
- 第二轮共享preview Powered完成75个chunk/约66.6秒，仍无Servo/QP失败；E2E-to-Control P50/P95为203.3/234.8ms。手部实际小反向约1.28次/chunk、jerk P95 49.6rad/s³，确认相对V1.1的手部改善仍在；但手部单步P95随本轮轨迹回升到0.1630rad。双臂原始30Hz waypoint小反向为90.8次/chunk，weight10后14.5、C2目标后约3.84；request 61右臂joint3在净位移0.004°时累计往返0.471°，直接复现操作者报告的原地颤动。C2相对linear仍将同轨迹chunk jerk-max P95降低57.2%，换插值不是答案，下一步转向双臂waypoint层近静止反向抑制的离线意图保真消融。
- 双臂近静止反向离线消融已覆盖V1.1和两轮共享preview共161个可严格重建chunk。play/deadband会延迟或遗漏真实动作，局部均值/中值及简单速度门会增加C2后的反向，均淘汰；唯一通过离线门的候选是在最终C2+边界融合100Hz目标上做3.5mrad minimum-jerk投影。首次Powered两轮共60个完整chunk，零Servo/QP/安全失败；58个可严格重建chunk中，冻结近静止小反向36→22（-38.9%）、全部小反向179→165（-7.8%），触发轴累计往返下降81.6%，端点误差0且显著意图零遗漏。它证明触发时有效且安全，但没有达到预设Powered效果门50%；正式05 Sync暂时保留候选，4mrad仍否决，RTC及其他入口不启用。
- 轻量运动诊断仪表盘代码已完成：6000D已为129份历史JSONL生成按日期倒序的静态HTML/PNG报告，支持热刷新、完整性筛选与同页双栏A/B；Sync现与RTC一样保存双臂100Hz逐条命令、发送时间和调用耗时。34维feedback sidecar、5/10/20Hz只读probe和慢读/失败自动关闭已逐文件同步G2；本地完整无硬件测试144 passed/1 skipped，G2运行时测试76 passed。尚未做真机probe，因此当前报告仍没有`q_measured`，不能宣称三层闭环已经通过。

## 当前架构决策

当前实现为：Control process唯一拥有Robot命令和measured state；Policy process唯一拥有Camera、图像和网络；每次Policy完成相机采集后向Control请求一次state，Control采样并写入带timestamp的34D ring，再只返回sequence。没有新增连续100Hz Robot reader，因此不会为了IPC再制造一个高频GDK竞争源。详见 [`robot/RTC_PROCESS_ISOLATION.md`](../robot/RTC_PROCESS_ISOLATION.md)。

## 下一步

1. 在G2手动运行`14_joint_feedback_rate_probe.sh`；10/20Hz至少一档通过才允许Powered反馈，否则保持command-only，禁止绕过probe或新增Robot reader。
2. probe通过后做一轮短Powered Sync，要求反馈零失败/零drop、arm interval P95相对基线增加不超过1ms且不新增`>20ms`间隔；随后拉回日志生成首份三层报告。
3. 保留正式05 Sync的`stationary_minjerk_v1/0.0035rad`作为安全的局部候选，待三层诊断定位残余后再设计下一版；不得直接扩大到4mrad。
4. 用任务动作/完成度确认共享手部preview没有通过软化动作换取观感；手部仍重点看0.13--0.15rad的大单步事件，不再盲目增大jerk penalty。
5. Sync诊断后再决定是否把同一profile接入RTC；RTC必须另画旧/新chunk计划分歧和soft-mask覆盖图，不维护第二套底层平滑算法。
6. 在已运行的GPU3/7893上先跑RTC Shadow，按真实observation age和schedule margin重新标定d/s；随后依次做10秒arms-only C2门和有限完整Split RTC，禁止直接continuous。
7. RTC通过后成为正式主模式；Sync保留为可诊断fallback。EMA/低通、模型训练和C++ 1kHz只在共享轨迹器与RTC之后仍有证据时再启动。
8. 20/30/50Hz均已否决，不再试15Hz；手部保持10Hz。

## 证据边界

普通Linux离线压测可以验证进程隔离和调度趋势，不能证明G2上的GDK调用、DDS、内核调度和硬件下发实际稳定100Hz。最终100Hz结论必须来自连接G2后的真实发送时间戳；不要求机器人完成完整任务，但必须至少让真实控制发送路径运行。
