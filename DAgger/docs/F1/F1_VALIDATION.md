# F1-09 HG-DAgger 验证记录

始建：2026-09-28；更新：2026-10-09。范围：软件实现、离线/ARM数值验证、真实相机零输出shadow、授权配置部署及用户现场动作记录；实际动作由用户本地启动。

## v15：连续领先约束（2026-10-09 已验收部署，现场手感待验证）

修复v14-r2持续输入时的整臂目标硬回拉。领先门槛仍为0.03rad，改为QP内约束每个关节的新增参考增量，已接受的可行参考保持连续；任意次轴反号不再重置整臂。关节位置、腕部耦合、速度/单步、输出加速度/jerk和完整轨迹认证不放宽，G2公共实现、Windows输入和相机/4242启动保持不变。

输出制动时发布位置可能因已有动量暂时远离参考；此时允许零增量或向内增量，不额外累积已有超额领先，也不瞬间重置参考。该规则不宣称所有时刻绝对领先都小于0.03rad。合并同向QP约束避免活动平面重复；早期重复平面候选在长时间−Z数值验证中出现求解失败，修正后的9组14秒闭环验证通过，失败日志保留。

复现使用现场full22与真实Pink 50Hz、Ruckig 250Hz；连续10秒输入后直接反向2秒，再回中2秒。双臂六方向、现场右臂正X、次轴交替反号和变步长共9组通过，首个反向候选向内、实际发布轨迹反向恢复、回中收敛、原动力学和HAL约束均通过。现场姿态右臂−X在1–2秒的发布末端速度最小/中位/最大约69.19/71.38/73.87mm/s；v14-r2诊断基线约13.59/26.48/59.79mm/s。此处是数值模型中的发布轨迹，不是实物速度测量。证据在 `results/smooth_lead_20261009/`；`scripts/65_f1_validate_smooth_lead.py`及绑定现场姿态用于本地和ARM同项验收。

最终本地完整回归 **328 passed, 3 skipped**（27.01秒）；跳过项不作为本轮训练导出重验。旧v14-r2发布和日志保留。

机载补记：v15已上传至 `/home/user/Bruno/DAgger/releases/20261009-f1-hgdagger-v15/DAgger`，99文件逐项核验。包SHA256 `2315717bcd9aba2792e457edacf0333b9b2563680fee33cbd67e48e0425e374f`；manifest SHA256 `9dc98bd5ff6f4b6dfa1cb92f956d19c08a5187601f5bb89c3b2af556348707c8`；源码SHA256 `8f29cc4370fa31b93207025794c7f949343f1a4fefd37aede2d8b63753f2ea31`。实际ARM的57模型/碰撞、64恢复与65平滑性验证全部通过；报告在 `results/deployments/20261009-f1-hgdagger-v15/`。与v14-r2清单比较，G2公共源码、Windows客户端、全部配置和相机/4242启动文件均未变化。

最初本次开机后motor_ctl_node为0，两次只读preflight失败。用户随后现场完成4242基础启动，2026-10-09 03:32:29 UTC重新preflight通过：机器ID `90429b8a96e04ef79dbc12175b538115`，唯一同用户motor_ctl_node PID430607，原控制二进制哈希一致；未创建动作端点或切换模式。证据为部署目录 `preflight_started.log`。

已启动独立19398端口的零输出shadow，runtime为 `/home/user/Bruno/DAgger-runtime/shadow-soak-20261009-v15`。本地日志收到 `SOAK waiting for START with continuous recording`、`SOAK connected phase=0 duration=120` 后SSH超时，管理进程退出255。随后新连接也在banner阶段超时，用户确认“F1掉了”，要求先完成其余本地工作。因此本轮最终报告、进程退出/写盘状态及帧率尚未回收；这是管理连接中断，不能直接认定机载shadow通过或失败。未远程启动powered或驱动，也未修改网络和超时保护。

恢复后的最终验收：2026-10-09 06:53 UTC重新核对机器与新boot，旧shadow无完成报告且无残留DAgger进程；当前驱动preflight通过。旧runtime原样保留，新测试使用 `/home/user/Bruno/DAgger-runtime/shadow-soak-20261009-v15-retry1`，脱离管理SSH运行。约200秒真实反馈shadow完整结束：**5958组观察、29.954742Hz、26928条control、6127次ACK，最大RTT10.614ms**；审计valid=true，所有submitted/publications为空。3次SAFE_HOLD均为探针主动断连或换session，无target_rejected/publication_failed。头1280×720、双腕640×480；按age/skew淘汰头1、左腕16、右腕15帧，源stamp重复与11个断流边界按现有合同保留，不宣称相机零丢帧。shadow原始记录 `f1_hil_20261009T065352Z_7198dc8e` 不属于专家训练数据。

已创建 `/home/user/Bruno/DAgger-runtime/f1-09-accepted-v15.json`，SHA256 `d02316e83811a6537223417bce3637d6005cab580a5045a0988fd6c64824e97e`，绑定本版manifest、ARM57/64/65和retry1 shadow报告。2026-10-09 06:58 UTC在无DAgger活动进程时原子切换稳定入口 `/home/user/Bruno/DAgger/start_f1_teleop.sh`，新SHA256 `3dfd6e9d2c238036e5cd34bdc05829c427fa52ffd5ff41c4ca6f1088a8ac6569`。旧入口及accepted配置备份在机载 `/home/user/Bruno/DAgger-runtime/entry-backup-before-v15` 和6000D部署目录；v14-r2不可变发布保留。

最终稳定入口 `--check`通过，release=v15、ik_verified=true、hardware_initialized=false；7906/19398均无监听、shadow已退出，未远程启动powered。Windows无需更新；三图30Hz、G2、相机/4242保持原配置。真实手感由现场操作者按[三个终端命令](F1_HG_DAGGER.md)通过Enter和START验证，离线平滑性及零输出shadow不代替实物验收。新配置模型端点仍为空，仅用于当前人工遥操。

完整部署和回退备份证据在 `results/deployments/20261009-f1-hgdagger-v15/`，最终索引为 `final-evidence.json`；`handoff.json`保留此前掉线时快照。不可变发布文档为打包前状态，当前状态以本文、已安装入口和机器accepted配置为准。

## v14-r2：连续可行限位（2026-10-09 已部署，现场顿挫待修复）

现场后续：用户确认不再需要重新按 START/RB，但持续推杆出现周期性顿挫。实际 powered 会话 `f1_hil_20261009T024556Z_eb0f1f8d` 的 manifest 绑定下述 v14-r2 source/release/config；完整关闭，6848组观察、35324条control、8313条event，写盘drop为0。两次Home有反馈完成事件；唯一SAFE_HOLD发生于末尾操作端断连，没有中途target_rejected或publication_failed。这些证据支持本次接管保持改善，不代表所有现场动作已验收。

顿挫主因已通过现场记录及独立数值对照定位：`teleop.py` 在 IK 虚拟目标领先最后发布位置超过 `0.03 rad`（约1.72°）时，将整臂参考瞬间重置到发布位置。目标被回拉后，保留原速度/加速度的 Ruckig 减速；持续输入再累积领先量，重复触发回拉。任意单个 Cartesian 输入分量反号也会重置整臂，是另一项待改进机制。不能把该处理当作平滑的连续领先约束。

现场最清晰片段在control起点后43.078–44.528秒：66个输入包完全一致，RB持续按住、右臂、平移 `[1,0,0]`、角输入为0、速度2倍、无Home或切换。`control.jsonl`第7777行右J4候选目标回跳1.390°，领先量1.798°降至0.407°；IK速度系数为1、无活动限位平面、输出为 `certified_target`。该重复回拉序列内，实际发布位置的40ms差分速度从10.972降至6.263°/s，下降42.92%，峰谷间隔225ms，峰值后456ms恢复至其90%。命令及反馈仍总体向前，不能将候选回拉称为机械实际反转。全程335次符合源码机制的候选回跳特征是推断统计，不是显式分支计数；全程仅另外3条 `limit_braking`，不能解释普遍顿挫。来源行号、时间和计算端点见 [现场代表片段](../../artifacts/results/stutter_20261009/trace_audit/representative_dip.json)。

独立离线对照使用现场首条full22、2倍恒定输入、真实Pink 50Hz及Ruckig 250Hz。−X方向10秒出现47次参考重置；1–2秒速度中位数为26.48mm/s，仅禁用领先门槛重置的诊断对照为79.70mm/s，全部样本均为 `certified_target`，没有限位制动或故障。−Y方向首次重置前两组相同，进一步支持因果定位。见 [复现摘要](../../artifacts/results/stutter_20261009/code_audit/summary_onsite_pose_scale2.json) 和 [复现脚本](../../artifacts/results/stutter_20261009/code_audit/reproduce_onsite_pose.py)。禁用门槛仅用于诊断，不能直接作为上线修复。

下一步应保留有限领先、QP约束及完整输出认证，将新增参考增量连续限幅，避免跨门槛时整臂领先量清零；反向处理应只消除与新意图冲突的积累。验收需新增恒定输入下的目标连续性及速度谷值检查。本轮只读调查、离线复现及补充验证记录，未修改生产控制代码、部署新版本或启动机器人；此前数值/状态机通过不能替代平滑性验收。下方保留发布时验证历史。

本版只修改 F1 适配层。Pink QP 同时约束关节盒、速度/步长和腕部四个半空间；每周期将 Cartesian 参考重置为可行 FK，丢弃不可实现的增量。反向输入从最后发布位置重新建立增量，保留输出速度/加速度；普通触限不通过异常清空接管。真实碰撞、失联、反馈过期及未知数值故障仍保留保护恢复流程。

输出层验证从当前 q/v/a 到零速度目标的完整 Ruckig 轨迹：单关节使用解析极值，腕部先用包络快速检查，必要时合并 jerk 分段并求四个耦合平面的三次曲线极值。普通时间同步轨迹越界时先尝试相位同步并重新检查全曲线，仍不可行则继续上一条已验证轨迹制动，不重置动态状态。速度、加速度、jerk、HAL 范围和腕部约束均未放宽。

本地原问题复现位于 `results/continuous_limits_20261009/`：连续顶住10秒后直接反向，IK下一次20ms求解即反向，未积累不可达参考。该时间是离线IK增量，不是实机末端响应。完整状态机＋真实IK/输出测试覆盖RB全程按住、epoch/选臂保持和RT可用；机载64脚本增加同版持续限位、变步长输出动力学及直接反向验收。ARM、真实反馈shadow和现场动作结果分别记录，打包时不宣称后续验收已通过。

新增启动源码/配置身份：启动行、ACK及journal binding保存实际source/release/config哈希。powered要求accepted配置绑定当前不可变发布manifest；旧v12/v13配置不能用于新代码。最终稳定入口 `/home/user/Bruno/DAgger/start_f1_teleop.sh` 仅在机载验收通过后安装，入口不远程启动运动。Windows仅更新F1包装入口以解析ACK版本字段，reader/mapper和协议不变；相机和4242基础启动不修改。

最终发布 `/home/user/Bruno/DAgger/releases/20261009-f1-hgdagger-v14-r2/DAgger`，97文件逐项校验；包SHA256 `b82f9b26c1b472535eae98c78900016cfb58b193fd921d928b2df88b6e1a5945`，manifest SHA256 `a29d77b90a9e9022fed6617b1b703a13ef789df60acd732674fd05ed2a92eda4`。最终完整离线回归 **325 passed, 3 skipped**（25.61秒）；3项依赖环境跳过，不作为本轮导出重验。与v13发布逐文件对比，G2运行源码、Windows公共reader/mapper/协议均未变化。v14及r1是保留的中间候选，稳定入口指向r2。

最终F1 ARM模型/IK/碰撞验证通过；持续限位验证包括左右臂六方向各10秒顶住后直接反向，下一次20ms IK求解即反向；该数字不是物理末端延迟。输出验证三组各2400步，包括普通关节、耦合腕部及贴边切向，后者40次使用相位同步；全部通过原速度/加速度/jerk约束及反向收敛。相位同步仍不可行时沿既有安全轨迹制动，不把全程零制动作为验收条件。

最终真实反馈shadow约200秒：5956组观察，**29.932909Hz**，23055条control，6127次机内ACK，最大12.405ms；无target_rejected、publication_failed或写盘拒绝，3次SAFE_HOLD均为探针主动断连/换会话。所有submitted/publications为空，审计valid=true。头1280×720、双腕640×480；原始相机stamp重复及淘汰仍按已有规则记录，不能声称零丢帧。原始记录为 `/home/user/Bruno/DAgger-runtime/shadow-soak-20261009-v14-r2/raw/f1_hil_20261009T022725Z_e3915c59`。

独立配置 `/home/user/Bruno/DAgger-runtime/f1-09-accepted-v14-r2.json` SHA256 `a5d3d0db89826a8fcb42897e33492ead86034a7f5df355f574e494053ab7ace2` 已生成。稳定入口 `/home/user/Bruno/DAgger/start_f1_teleop.sh` SHA256 `5c4ff97ea0002fcb43d4492b5c3684582d9b50e613a6503db40e79af164f44f8` 已安装，`--check`通过，报告实际r2版本、软件IK验收true、hardware_initialized=false；未启动实际运动。最终7906/19398无监听，现场可按操作说明启动。入口此前不存在，未覆盖已有入口；v13发布及accepted配置保留，可在现场结束新会话后按v13历史入口回退，回退不恢复机器人姿态。

Windows只更新 `03_windows_f1_sender.py`，SHA256 `b4a65812e2251e530e13054d6dcf008e735cd18b075c41237cbede3dccd9ba82`，解析并显示ACK版本字段；用户Windows实际更新与物理触限/反向手感仍待现场验证。所有机载报告、安装事务和最终audit保存在 `results/deployments/20261009-f1-hgdagger-v14-r2/`，其中 `final-evidence.json` 绑定最终源身份、配置及证据。不可变发布文档为打包时快照，本段及机器accepted配置记录打包后验收。相机/4242启动引用继续v8，未改驱动或30Hz数据合同。

## v13：触限保持与末段规划器修复（2026-10-09 已部署验证）

2026-10-08 用户报告 v12 已基本能够遥操，但触及限位后经常需要重新按 START/选侧。现场会话 `f1_hil_20261008T094231Z_75c83319` 记录了 HAL 关节限位拒绝及 Pink 配置越界；原运行端把这类候选边界当作断线清空接收时间，同一周期又将根因覆盖成 watchdog，随后要求释放 RB。末段 Home 的退出是另一项故障：现场 Ruckig 报 `error in step 1, dof: 2`，旧最终异常只显示 `F1 output fault latched`。原日志和 `.partial` 保留在 `results/input_diagnosis_20261008/powered_094231/`，不能把这次退出标记为 Home 完成或采集完整。

F1 候选修复将已分类的关节/耦合腕部边界单独处理：丢弃越限 IK 候选和积累目标，清空输出中的旧推进，保持最后实际发布的合法命令，同时保留 HIL、选侧、session、epoch 和夹爪基准；新的合法反向目标仍通过正常碰撞、反馈、时效及发布检查后执行。虚拟求解参考取合法 `last_submitted`，真实反馈原样保留，参考与反馈差异单独记录。未知求解异常、非有限数据、真实断线、输入过期、反馈失效、碰撞和输出故障仍进入 SAFE_HOLD 或停止，不自动恢复；已进入 SAFE_HOLD 后清空接收时间也不再制造第二个假 watchdog。边界进入、原因变化和恢复有独立事件，连续触限不逐周期刷事件。

末段输出规划错误使用现场完整位置/速度/加速度状态进行了离线复现：直接以度数求解失败，目标取整或只平移坐标原点不能解决；相同物理状态及全部动力学限制等比例转换到弧度后可求解。新增 F1 单位适配层仅在底层轨迹规划中使用弧度，外部 HAL 命令及限值继续使用度数；提交保留原生弧度 proposal，未活动关节保留原命令的精确值。原 `ApprovedArmOutput` 的 watchdog、保持、故障锁存、速度/加速度/jerk 限制不放宽；失败证据保留原规划器原因及清空前状态。对应现场复现与单位说明见 `results/input_diagnosis_20261008/powered_094231/output_failure_analysis.json`，runtime 的失败/修复回归摘要见 `results/input_diagnosis_20261008/arm_boundary_runtime_fix/`。

发布路径 `/home/user/Bruno/DAgger/releases/20261008-f1-hgdagger-v13/DAgger` 已部署，94文件逐项哈希通过；包SHA256 `da9e94891829bf503323b5c3897d9221400fa1ac90692b694acf7f262d312396`。本次完整离线回归 **297 passed, 3 skipped**（22.97 s），样本路径调整后相关50项再通过。F1 ARM 的57模型/IK/碰撞验证及64现场边界反向、原始规划故障、533步Home回放均通过。真实三图/反馈 shadow 约200秒完成：5958组观察，实测29.9553 Hz，23109条control，6127次机内ACK、最大13.283 ms；写盘拒绝0、target_rejected和publication_failed均0；3次SAFE_HOLD对应探针主动断连/换会话。所有submitted及publications为空，审计valid=true，不能作为专家纠正集。Windows sender、30 Hz 数据合同以及相机/4242 已验证的 v8 启动引用继续沿用；不为此次运行端升级重启相机或本体驱动。下方保留 v12/v11 的部署与验证历史。

独立现场配置 `/home/user/Bruno/DAgger-runtime/f1-09-accepted-v13.json`（SHA256 `4d72e2c65dba133b6e9fffeba9056f777f6c7f1fade63ffeeb6621c8f49b1c28`）绑定上述v13发布、两份ARM报告和本轮shadow报告，软件IK验收门开启；物理触限反向恢复、Home到位仍待用户现场复核。首次preflight在本次开机后因motor_ctl_node未启动失败；用户从4242本地基础启动后，同一版本preflight通过。本轮不远程启动驱动或运动。机载证据和失败首次检查均保留在 `results/deployments/20261008-f1-hgdagger-v13/`；成功shadow原始记录为 `/home/user/Bruno/DAgger-runtime/shadow-soak-20261008-v13/raw/f1_hil_20261009T004823Z_cbd83633`。不可变发布中的文档是打包时快照，当前验收结论以本文及机器accepted配置为准。

## v12 已部署基线（2026-10-08）

运行入口为 `/home/user/Bruno/DAgger/releases/20261008-f1-hgdagger-v12/DAgger`，91文件逐项哈希通过；包SHA256 `7c2cf6760802096d27b44e3c0f47ac6fcd24e74dadd1fa05537d7382c9415b99`。独立配置 `/home/user/Bruno/DAgger-runtime/f1-09-accepted-v12.json`（SHA256 `f23a4e21eb44cd19ddb55ecbca03947741774d65b89a847b0c2e673ba28aa679`）已inspect通过，启动合同preflight通过。发布仅改变GripperIntent；ARM纯内存RT/旧锁存检查与本地44项相关回归通过。IK/Home类AST及其他机器人运行源码与v11一致；v12配置明确继承下方v11数值和持续shadow证据，不宣称重新执行过v12持续实机shadow。

用户确认错误USB连接已纠正、Windows系统测试通过；随后确认MODE互换已纠正，左圆摇杆恢复X/Y输入。v11现场Y已有反馈到位证据；修复后实际夹爪、非零摇杆运动及真实策略仍待现场验收。当前命令见[操作说明](F1_HG_DAGGER.md)，补丁部署证据在 `results/deployments/20261008-f1-hgdagger-v12/`。

## v11 已验证基线（2026-10-08）

运行版本 `/home/user/Bruno/DAgger/releases/20261008-f1-hgdagger-v11/DAgger`，90个文件已逐一校验，包SHA256 `12fd128307d804c37aba5d0421a158f8ffbc9f67ee43b25a91f69f542ff16c38`。正式7906和测试19398均已释放，现场启动命令见 [操作说明](F1_HG_DAGGER.md)。相机当前配置、持久入口及回退事实见 [F1系统配置](../../../F1/F1系统配置.md)；运行端升级不需再次重启相机/4242。

| 原计划要求 | 最新证据与结论 |
|---|---|
| 16D映射、FK、IK方向、Home夹爪保持及碰撞检查 | 本次部署版本的ARM数值验证通过；模型/软件验收不替代实物方向及到位检查 |
| G2兼容与最少公共改动 | 公共扩展保持G2默认，冻结G2Teleop及生产脚本不变；F1绝对采集循环单独覆盖，无共享采集调度改动 |
| 30Hz真实三图与反馈 | 约200秒、5959组完整观察，实测29.96165Hz；头1280×720，两腕640×480 |
| 图像新鲜度与异常追溯 | 接收至观察可用年龄中位13.785ms、p99 82.102ms、最大103.044ms；不是曝光延迟测量 |
| 持续录制及ACK | 写盘拒绝0、完整payload覆盖5959组；6127次机内ACK全部返回，最大RTT8.057ms；不能代替Windows无线实测 |
| 相机源时间异常 | 197个重复头部stamp按真实callback身份保留；2个回退头帧明确丢弃并恢复；两腕各14帧因skew淘汰，15个断流边界通过审计；不宣称原始回调零丢帧 |
| 接管、断连及恢复 | 合成意图结合真实反馈通过，26995条control记录含human_arm/human_hold及SAFE_HOLD；所有实际发布目标为空，无动作输出 |
| 完整回归和训练导出 | 239 passed / 3 skipped；固定LeRobot环境32 passed，覆盖writer、reader、身份及边界；真实纠正数据仍需现场采集与审阅 |
| 现场纯遥操入口 | 已生成并只读inspect通过 `/home/user/Bruno/DAgger-runtime/f1-09-accepted-v11.json`，软件IK验收true；文件含本轮数值/持续shadow及profile哈希证据 |
| 尚未完成的现场项 | Windows真实F710、实际双臂方向/夹爪/Home到位、整机重启后相机恢复；模型端点为空，真实策略闭环待模型服务 |

现场后续会话 `f1_hil_20261008T082740Z_be98a028` 已确认 `execution_mode=powered`，采集2654组、约29.957Hz，正常关闭；但没有 `operator_start_authorized` 事件，control=0，不能作为实际动作通过。16:32 CST只读检查时已无DAgger进程及7906监听。Windows旧提示在POWERED下也可能追加SHADOW通用说明；仅文案补丁 `20261008-f1-windows-mode-labels-v1` 已单独发布、23项测试通过，运行端及按钮/通信逻辑不变。证据在 `results/deployments/20261008-f1-windows-mode-labels-v1/`。

2026-10-08 16:41后再次排查：现场会话 `f1_hil_20261008T083810Z_2518ed8c` 为POWERED、正常关闭，2041组观察、control=0。用户提供的Windows日志证明16:38:55后generation13稳定获得1998个应用ACK，最终ACK序号1997、RTT p50 46ms/p95 63ms，接收端持续报告waiting_for_teleop_start；所给日志没有本地START检测或选臂/RB变化。这证明稳定时段传输和F1回执通畅，不能证明物理按键已被读到。服务端7种离线启动/重连场景通过。已交付单独、无网络/无机器人输出的 `04_windows_f1_input_probe.py`，正在等待Windows真实SDL/reader/mapper/XInput对照结果；此前设备名称识别成功不等于按键输入验收。证据归 `results/input_diagnosis_20261008/`。

2026-10-08 17:04 CST Windows真实探针已回传：28秒1400样本、reader连接1400、读取错误0、START边沿0、mapper toggle=0；SDL全部原始状态无变化，XInput槽0始终API成功但没有任何按键，其他槽均未连接。实际reader/mapper及sender哈希与交付版本一致。若用户在采样期确实操作，单纯START索引或mapper错误不足以解释结果；尚待前台Windows游戏控制器属性测试，不能据此认定手柄损坏或无线断连。摘要未汇总XInput轴/packet变化，不能声称其恒定。证据摘要：`results/input_diagnosis_20261008/windows_probe_090423_facts.json`。

2026-10-08 后续现场核对：用户已打开 `joy.cpl` → 属性 → 测试，并明确报告“完全不动没反应”。截图只显示测试界面、轴居中及当时无按键激活；持续无响应来自用户的动态操作报告，不能由静态截图单独证明。当前输入在 Windows 系统控制器测试中也未响应，下一步检查 F710 供电、USB 接收器、无线连接及 Windows 驱动；具体故障仍未区分，不能宣告硬件损坏，也不能将此作为 F710 输入或机器人运动验收通过。本次仅保存新证据并补记文档，未修改控制代码、未连接机器人；此前 probe 证据保持原样。独立事实记录：`results/input_diagnosis_20261008/windows_joy_cpl_no_response_facts.json`。

2026-10-08 用户随后确认“好了 我发现刚刚连错usb 现在好了 测试通过了”：用户纠正 USB 连接后，Windows 系统手柄测试已通过，前述系统测试无响应已有用户确认的恢复结果。具体接错的是哪个接收器、设备或端口未说明，不作推断。本项仅确认 Windows 系统输入测试恢复；DAgger 的 START 授权、端到端遥操及双臂/夹爪/Home 实际运动仍需现场验收。此前无响应和 probe 证据原样保留；独立恢复事实记录：`results/input_diagnosis_20261008/windows_usb_correction_resolution_facts.json`。

完整报告及失败候选证据保留在 `results/deployments/20261008-f1-hgdagger-v11/`、同级v10目录和 `logs/f1_online_20261008/`。本轮成功raw为 `/home/user/Bruno/DAgger-runtime/shadow-soak-20261008-v11/raw/f1_hil_20261008T080537Z_afff79c7`，`training_ready=false`；模拟输入及shadow不得作为专家纠正。模板 `ik.verified=false` 保持，机器accepted配置仅表示软件门槛通过，不表示物理标定完成。旧失败partial和发布版本保留，没有用新结果覆盖旧记录。

## v7 上线前审计（历史，已由上述v11结果替代）

2026-10-08 用户恢复上线测试后，v7已部署到 `/home/user/Bruno/DAgger/releases/20260928-f1-hgdagger-v7/DAgger`，86文件、117个外部资产及ARM依赖通过核对；inspect和现场驱动preflight通过。独立端口19397零输出shadow探针用实际三图/反馈完成双臂合成输入、200ACK、971条控制与原始审计，随后正常退出，未创建动作输出。真实Windows F710与物理运动仍未验收，配置 `ik.verified=false`，模型端点为空。

本轮发现新的现场采集限制：配置30Hz，但三图实采约10.09Hz；独立ROS回调统计头部约29.98Hz、两腕约10.01Hz。左右D405实际选择848×480×10；现连接设备枚举中848×480没有30fps档，不能靠DAgger配置或补帧修复。正式30Hz采集还需选择受支持的相机模式并验证图像合同/链路，未改相机配置。Wi-Fi本轮检查无队列积压；长期相机内网启动候选已随包部署，但尚未激活。下一步为用户按三个终端说明进行真实F710 shadow；shadow始终零运动。

| 原计划要求 | 当前证据与结论 |
|---|---|
| 16D 顺序/单位/夹爪、F1 模型映射及限位 | 已实现；逐轴往返、独立工作台/Pink/MuJoCo FK 和 ARM 数值验收通过 |
| 尽量复用 G2 主链、默认值和旧格式兼容 | 仅3个公共实现文件添加参数/扩展点；G2 回归通过，冻结 G2Teleop/生产脚本未改 |
| 单一 owner、共享锁、外部命令检测、50/250Hz 调度 | 已实现；离线拒绝/互斥测试、现场只读合同与短 shadow 通过；持续实机调度待现场 |
| F710 交互、RT、Y、保持、断连与恢复 | 软件和合成输入验证通过；实际 Windows F710 与物理行为待用户现场 |
| 首次 START、旧响应作废、退出后新观测恢复 | 本地真实 multipart HTTP 假策略端到端通过；检查三图、state16、任务、模型/request/epoch，确认旧响应拒绝、新图新 epoch 恢复 |
| 可审计录制、真实反馈/候选/实际发布目标分别保存 | schema3、逐字段来源及哈希审计通过；F1 真实三图 shadow 原始记录通过，实际发布字段均为空 |
| 训练导出、片段边界、三视频及 reader 回读 | 固定 LeRobot v3 的合成审阅 fixture 实际写入/回读通过；真实纠正数据质量验收仍待现场样本 |
| F1 配置、依赖/发布清单、三个终端操作说明 | 已交付；117个F1资产、78个v3发布文件校验，独立 ARM64 环境与 ROS preflight 通过 |
| 真实策略接管闭环 | 模型端点及权重身份未提供，尚未验收 |

v3 本地测试：**117 passed, 1 skipped**；跳过的真实 LeRobot 测试已在独立 Python 3.12 环境通过（6 passed）。新增 HTTP 测试为 `tests/test_f1_http_handoff.py`。本次只新增测试和验收说明，v3 运行代码未再修改，无需再次部署相同运行代码。

v3 现场开始前的历史状态：现场 F710/运动验收需要用户在机器人旁操作，真实策略验收需要模型端点。最后只读核对确认没有活动的 F1 DAgger 进程、没有新的现场人工 runtime；此前 shadow 以退出码0结束，不存在等待中的后台验收任务。当前不能宣称整个计划完成。

## 初版本地验收（历史记录）

| 验证 | 结果 |
|---|---|
| DAgger 完整测试，含 G2 回归、F1 合同/状态机/输入/输出/审计及真实数值链路 | **115 passed, 1 skipped**（4.75 s） |
| 固定 LeRobot 版本，独立 Python 3.12 CPU 环境 | **6 passed**（1.69 s），含上项中跳过的真实 writer/readback |
| `50_f1_hil.sh inspect` | 合同与所有 F1 资产绑定通过，hardware_initialized=false，policy_configured=false，ik_verified=false |
| F1 模块独立导入 | 无 G2 SDK 路径也可导入 CLI/runtime/policy，无机器人初始化 |
| 源码差异检查 | G2 默认 34D、50 步、30 Hz 和 schema 2 保留；冻结 G2Teleop 与 G2 生产脚本无改动 |

测试命令（本次临时环境保留在 /tmp，后续可按 requirements 重建）：

```bash
cd /home/xza_100850113/projects/DAgger
PYTHONPATH=src:../g2_robot /tmp/f1-dagger-check/bin/python -m pytest -q tests
PYTHONPATH=src:../g2_robot \
F1_TEST_LEROBOT_ROOT=/home/xza_100850113/projects/third_party/lerobot \
/tmp/f1-export-check/bin/python -m pytest -q tests/test_f1_recording.py
```

`../g2_robot` 仅为原 G2 回归测试所需，F1 运行脚本不依赖它。数值环境 Python 3.10 / Pinocchio 3.7.0 / Pink 4.3.0 / ProxSuite 0.7.3 / Ruckig 0.19.4 / MuJoCo 3.3.7。导出环境 Python 3.12 / CPU torch 2.8.0 / PyAV 15.1.0，固定源码 LeRobot commit `713a409faedd73bb5597481b8885f17fbee23330`，完整文件 SHA 另存配置清单。

## 数值与运行细节

- HAL 度数与模型弧度逐轴往返；左 J7、右 J5/J6 负号正确；身体 `body_j1..5 → body_joint5..1` 与当前 F1 joint contract 一致。
- 两臂 reduced FK 与工作台 FK 在零位及非零身体/头部扰动下吻合（测试容差 1e-9）；同姿态 MuJoCo FK 与 Pink 吻合（2e-6 m 容差）。
- 两臂 XYZ 正负方向的小幅真实 Pink 求解、关节步长/限位、未活动臂及夹爪保持通过。该测试不代表完整工作空间或物理方向验收。
- 当前 URDF 与旧 Pico 资产不完全相同。碰撞适配使用当前映射绑定 URDF，仅在 run-local 缓存改写 mesh 路径，保留哈希记录；Pink 和碰撞坐标一致。真实 CoM/Collision 初始化、通过案例和手臂/头部碰撞拒绝案例通过。
- MuJoCo 3.14 的枚举与 NumPy 整数反向比较不兼容旧碰撞解析器，导致“无可控关节”；已固定并验证 3.3.7，版本不匹配启动直接报错。
- 首次 START、新观测/epoch 恢复、旧推理响应丢弃、RB 松开、200 ms 断流、慢 IK 到发布边界的 watchdog、重连不自动恢复、Y 保持夹爪和等待新反馈到位、TCP ACK/重放拒绝均有测试。
- 写盘复用有界队列和 `.partial`，审计校验流/图像哈希、时间/序号、实际发布者及逐字段命令来源。测试反馈与动作故意不同，验证 action 不取反馈。
- 短合成 episode 含人工、保持、Home 边界，导出为两个连续 HIL episode。真实 LeRobot 写入、每帧三视频解码、state/action 回读及结构检查通过；保留原始 observation ID、控制流索引和时间。
- 导出固定 H.264 / CRF 18 / PyAV / 单编码线程、顺序编码。默认 AV1 在 16×16 合成帧上崩溃，已避开；生产分辨率视频尚待现场样本验收。

## 尚待现场验收

1. 实际 Windows F710 输入、双臂物理轴向、夹爪意图、身体参考变化、奇异点阈值与长期调度；现有合成输入 shadow 不替代这些现场检查。
2. 用户现场纯 F710 遥操、Y 双臂零位/夹爪保持、主动断连与复连恢复。
3. 提供三图+state16 的模型服务端点/权重身份后，真实策略与人工接管闭环。
4. 短实机 episode 的图像、来源/边界人工质量审阅及 LeRobot 导出验收。

`ik.verified=false` 尚未放开，发布清单 `deployment_verified=false`。相机曝光时钟与物理几何仍沿用 F1 现有未标定范围；当前导出明确使用主机接收对齐。训练属于后续任务。

## 现场只读核对补记

已通过现有 F1-09 SSH 入口只读核对机器，身份与配置一致，系统为 ARM64 / Python 3.12.3。工作台实际根为 `/home/user/Bruno/F1/F1-4242`，current 指向 `20260928-f1-4242-v22-ui`。所有绑定的 F1 源文件和资产哈希与开发适配清单一致。

使用已部署 verifier 核验：machine-id、22 轴配置、实际运行控制器二进制与当前参数均通过，`control_publishers_created=false`、`mode_changes=false`。首次临时检查误覆盖 ROS setup 的 PYTHONPATH，导致 rclpy 导入失败；保留 ROS 路径后重跑通过。正式 DAgger 启动脚本已保留原 PYTHONPATH，不受该临时探针问题影响。

现场 profile 为 `/home/user/Bruno/F1/F1-4242/runtime/robot-v22.json`；共用锁为 `/home/user/Bruno/F1/runtime/control.lock`；DDS 为 `rmw_cyclonedds_cpp`、domain 0。该次只读核对时 `/home/user/Bruno/DAgger` 尚不存在；后续源码暂存结果见下方。

已有 Pico Python 3.12.13 环境含 pin 3.8.0、Ruckig 0.19.4、MuJoCo 3.11.0；缺少 Pink、qpsolvers 和 ProxSuite，与当前离线验证版本不同。本次没有修改它或系统包。HG-DAgger 应使用独立数值环境，仍复用现有 ROS setup 和已绑定 F1 模块。

只读证据：`logs/f1_readonly_20260928/inventory.json`、`runtime_contract_with_ros_path.json`；保留失败探针 `runtime_contract.json`。可用 `scripts/54_f1_readonly_inventory.py --help` 查看重跑方式。该证据不包含动作验收或 ROS 反馈连续采集通过结论。

### 真实观测适配核验

只读核对发现 F1 profile/StateStore 的相机键是 head/left/right，而模型及录制合同使用 head/left_wrist/right_wrist。已在 observation 边界显式转换，并修正测试为 F1 实际回调键；没有改动 F1-infra 或模型合同。随后通过 stdin 临时加载观测适配，在机器人上订阅真实 ROS 三图、22 轴和双夹爪，连续取得 3 组全新观测，`control_publishers_created=false`；只取回时间/维度/反馈元数据，没有写入机器人文件或发布动作。证据为 `logs/f1_readonly_20260928/real_observation_probe.json`，包含本次流式加载源码 SHA。

ARM64/Python 3.12 依赖已在6000D解析并保存哈希锁 `configs/f1_runtime_arm64.lock`。纯 wheel 解析明确发现 Ruckig 0.19.4 无适用 wheel，允许目标架构构建后解析通过。解析通过不是 ARM 安装/导入验收；该次解析时独立 DAgger 环境尚待创建；后续 ARM 安装结果见下方。

## ARM64 独立环境与源码暂存

2026-09-28 已在 `/home/user/Bruno/DAgger-runtime/f1-09-venv` 创建独立系统 Python 3.12 venv，按 ARM64 锁安装成功，pip check 和 Pinocchio 3.7.0 / Pink 4.3.0 / MuJoCo 3.3.7 / NumPy 2.2.6 导入通过。Ruckig 0.19.4 在目标机低优先级、单线程编译成功，wheel SHA256 为 `176ed6b0790e9eb2809c28de577b1579347b7cb69a4822b751ab079e6dbce675`。没有修改原 Pico、ROS 或系统包。安装曾发生网络重试，原进程自行继续并完成，没有重复启动。

源码已经暂存到 `/home/user/Bruno/DAgger/releases/20260928-f1-hgdagger-v1/DAgger`，77 个文件逐一验哈希通过。此前无 DAgger 部署；没有旧文件被覆盖，没有创建 current 入口或启动 DAgger 动作。环境准备器、只读 inventory、不可变源码暂存器及独立数值验收器均在 scripts/ 中，可逐步重跑并留存新证据。

### ARM 数值、ROS preflight 与完整零输出 shadow

最新暂存且通过下述验证的版本为 `/home/user/Bruno/DAgger/releases/20260928-f1-hgdagger-v3/DAgger`，78 个文件哈希通过，包 SHA256 `79c3313e51a1f869c1eeeec568679b6b06abd100062c3073af45100fe859b3d3`。v1/v2 均保留；未创建 current 或修改工作台入口。不可变包中的本文是打包时快照，最新验收结论以6000D本文件及证据为准。

- 目标 ARM 环境真实数值验收通过：双臂平移/旋转 XYZ 正负方向（24 组）、工作台/Pink/MuJoCo FK 对照、Home 渐进与夹爪保持、输出保持和碰撞拒绝。求解中位数 0.409ms、P95 0.564ms、最大 1.448ms；这是合成姿态短测，不是全工作空间或硬实时保证。
- 使用独立 venv 和正式 `50_f1_hil.sh preflight`，ROS runtime contract 通过，未创建动作 publisher 或改变模式。
- 第一次 v2 shadow 暴露首次 RB 的延迟初始化超过 watchdog：进入 SAFE_HOLD，未输出。v3 在等待 START 前用两臂零 twist 做纯数值预热，随后丢弃参考；本次预热左侧 212.0ms、右侧 2.33ms，未放宽 watchdog 或修改 G2。
- v3 正式 shadow 使用真实 ROS 反馈/图像、独立19396端口及明确标记的合成手柄意图。发送 START 前核对 socket 归属当前 shadow 子进程，避免向其它运行端发送输入。60 条 ACK、15 组观测/完整三图、329 条控制记录通过；包含 human_arm、human_hold、SAFE_HOLD，所有 submitted 为空、publications 为空。断流保护出现于探针主动结束通信后；原始审计 valid=true、training_ready=false，正常退出0。
- 控制记录间隔中位数 4.079ms、最大15.076ms，来自这次短 shadow，不等于已完成长期250Hz实机调度验收。
- 预热修复后完整本地回归 **116 passed, 1 skipped**；独立 LeRobot 视频回读此前已通过。

证据集中在 `logs/f1_environment_20260928/`：环境结果、ARM 数值报告、ROS preflight、v2失败探针、v3成功探针及 warmup/间隔统计均分别保存。机器人对应运行数据在 `DAgger-runtime/shadow-smoke-20260928-v3/`；该数据包含合成输入且无实际动作，不能作为专家纠正集。

**下一步为用户现场 F710 shadow 和纯遥操。** 合成输入验证不能代替 Windows F710 实际输入、物理方向、夹爪/Home 到位及现场中止验收；`ik.verified=false` 保持，模型端点仍待提供。


## 现场 F710 无 ACK：v4 修复

2026-09-28 20:30–20:31，Windows 正确识别 F710，但持续 `acked=0`、约 516 ms 后重连。F1 v3 进程仍在运行，记录 Broken pipe / operator_disconnected，未收到 START。20:36 只读 socket 快照显示被接受的连接超过 264 秒无数据，另外两个连接在等待接收。原 server 在 recv timeout 后无限 continue，单个空连接或未完成 JSON 即可长期占用唯一接收线程。

真实 TCP 测试在修复前复现“空连接之后的正常 sender 收不到 ACK”。修复仅触及 F1 network：超过 1 秒没有完整 intent 即释放连接，下一连接沿用原协议/会话验证。200 ms 运动 watchdog、500 ms sender ACK timeout、START 接管及重连不自动恢复均不变。新测试覆盖空连接、半包及持续输入超过清理时限。

这确认了接收端阻塞缺陷；最初连接为什么未及时送达数据尚未确定，仍需直连隧道现场复测，不能把本次代码修复当成完整 Windows 链路已验收。sender 中部分 G2 提示来自复用启动文案，目的地和实际运行模式以 F1 端及 ACK 为准。

v4 正式本地回归：**120 passed, 1 skipped**（已有 LeRobot 独立环境项）；使用 `/tmp/f1-dagger-check/bin/python` 和 `PYTHONPATH=src:../g2_robot`。首次系统 Python 运行缺失 g2_sort 路径，纠正为原验收环境后通过。

证据：`logs/f1_network_20260928/` 保存用户 sender 日志、F1 只读快照及回归结果。v3 代码与用户原始记录保留；v4 使用新发布目录，不终止或替换用户正在运行的 shadow。

ARM 上 v4 独立 TCP 验证通过：空连接/半包各约 1 秒释放，之后各连续 14 条 ACK（共 28）通过，线程正常结束；使用随机端口，不连接现场 7906、不导入 ROS、不产生机器人命令。v4 共 78 个文件哈希已核验，包 SHA256 `fd6009c71d0d4f69e6649556ff4f82a1af7b6e384bd6e98b338b96b3f9426580`。用户需现场结束 v3 并启动 v4，Windows F710 端到端仍待复测。


## 30 Hz合同纠正与DDS根因

用户指出现有训练数据为30Hz。核对原G2生产脚本 `scripts/40_g2_powered_hil.py` 的 `--capture-hz` 默认及 `START_HERE.md` 实际命令均为30，`PoweredHILPolicy` 动作waypoint也默认30；旧shadow类的10Hz不是生产默认。F1 v1–v4误沿用了10Hz新适配默认，本次将capture、HTTP action frequency、离线export默认一起纠正到30。H=50对应约1.67秒。数据合同不随网页预览降频；历史10Hz数据仅可显式按10fps处理，不能补缓存帧声称30fps。

本地频率修正测试包括真实30Hz时间戳、实际action与反馈不同、10Hz不得假装30Hz、独立LeRobot 10/30fps写入及全帧视频/reader回读。125 passed / 2 skipped；两个LeRobot跳过项已在独立环境9 passed覆盖。后续sender变更的最终回归另记。配置30不等于现场真实新帧已达到30，须统计新采集结果。

2026-09-28 5秒报文头统计证实：原head相机PID32374/sport59332、右腕PID35219/sport52404、左腕PID38221/sport57993向wlan0多播 `239.255.0.1:7401`，含IP分片约32Mbps，head socket积压约5.6MB。4242网页仅约1.33fps JPEG快照，TCP流量远小于此UDP流量；关闭网页不停止该外发。实际相机FastDDS 2.14.6，DAgger和工作台Cyclone均已绑定内部enP3p1s0f1，所以不把DAgger遗漏DDS变量当本次根因。

首次iptables TTL候选未被内核接受(exit4)，未记录到stderr，不能准确断言是哪一个扩展缺失；该规则未生效。之后用户现场运行原生nft版本成功：仅上述三相机源端口、Wi-Fi、指定DDS多播数据端口TTL=0，保留本机多播和其他通信；安装后新鲜三图+16状态观察11835通过。没有降采集/相机频率，没有重启驱动，没有动作输出。证据目录 `/home/user/Bruno/DAgger-runtime/network-guard/20260928T130052_790532Z`，本地报文头/前后计数在 `logs/f1_network_20260928/traffic/`。规则按当前端口绑定，相机重启/系统重启不能依赖它；长期修复为相机专用FastDDS内部UDP白名单+SHM及受版本控制启动入口。

Windows后续日志251条发送/251条ACK，RTT中位31ms、最大94ms；采样另出现28秒停顿和初始化先连TCP后识别手柄的首次断连。后者已定位代码顺序；QuickEdit导致长停顿尚未实证，不把关闭QuickEdit当全部Windows问题已解决。现场最后一轮日志在 `.partial`，原因待终端核对。


### 本地待发布v5汇总

修正后的采集/action时间轴/export默认30Hz；CLI manifest记录目标频率，未捕获异常写入运行目录failure.txt，便于现场诊断。F1 Windows入口沿用冻结主循环，先初始化手柄再启动TCP，传统Win32控制台可用时关闭QuickEdit并在退出恢复；启动提示明确shadow零输出，F1包装不修改G2模块全局或冻结脚本。Windows仅需更新 `windows_client/scripts/03_windows_f1_sender.py`；真实Windows验证待现场。

最新本地全套：**134 passed, 2 skipped**（两个LeRobot实测在独立环境9 passed）。三相机内网XML、包装器和启动副本生成器已准备；长期入口未在实机应用，受维护SSH中断及当前源核验未完成约束。未修改现役profile、未重启相机或base-start、未启动真实动作。

v5本地包已生成：`/tmp/f1-dagger-20260928-stage-v5.tar.gz`，86文件，SHA256 `4447e8dd38813c0eecf4155bd9f15270d9dafcbac594c5e88cbf02a13013de21`。维护SSH banner仍超时，未暂存/切换；该阶段后用户要求转为离线修复，后续不再要求恢复隧道或操作终端。长期相机启动候选包含原脚本来源SHA与5行差异，未改现役profile。


## v6 离线完整链路与故障审计

此阶段按用户要求只在6000D离线工作，没有SSH、ROS节点或机器人输出。发布出口在读取反馈前即建立本次独立证据，避免反馈过期、目标过期、滤波器故障或限位拒绝误带上上次成功结果；中途发布失败记录准确topic、已完成发布、目标及字段时间，不生成完整16D动作标签。控制循环保留原有异常退出行为，没有放宽watchdog或保护阈值。

新增 `tests/test_f1_offline_pipeline.py` 联调使用真实loopback TCP、F1 StateStore回调、RosObservationSource、Pink、Ruckig、CoM/碰撞检查、统一owner、异步录制与导出筛选。只有输入传感器及ROS发布出口被合成/内存替代：覆盖释放后的首次START、左臂RB、保持、Y及夹爪保持、暂停/恢复、断连SAFE_HOLD、重连不自启与新START。shadow分支没有prepare/publish，submitted全为空且拒绝专家导出。模型HTTP旧响应/新epoch回归仍独立覆盖。

保留的一次合成owner运行：92组三图观测、314条控制、134条ACK；合成新帧实测 **29.33 Hz**，不是实机30Hz验收。原始哈希/逐字段发布审计通过，筛选66帧、5段；段内不跨Home/等待/断连及epoch边界，反馈与动作保持分别来源。完整合成记录通过固定LeRobot v3的30fps写入、全部三视频解码及state/action reader回读。带 `binding.synthetic=true` 的数据即使结构回读通过，也保留 `training_ready=false`、`human_review_accepted=false`，不能将模拟验证当成现场专家纠正质量验收。

最终回归 **141 passed, 3 skipped**（20.20s）；三个跳过项是独立LeRobot环境测试，已在该环境 **10 passed**（2.11s）覆盖。证据集中在 `logs/f1_offline_20260928/v6/`，最终文件为 `pytest_final.txt`、`lerobot_pytest.txt`、`export_final_report.json`、`synthetic_export_verified/` 和 `source_binding_final.json`。初次export输出保留为历史证据；其synthetic验收标记已由最终版本纠正，以带 `_final` / `_verified` 的证据为准。原始记录未改写。

待发布包：`/tmp/f1-dagger-20260928-stage-v6.tar.gz`，清单及SHA保存为本证据目录 `prepared_release.json`；启动候选同步绑定v6目录。包尚未暂存或激活，F1最后已暂存版本仍为v4。下次统一上线先核对版本及源脚本哈希、验证相机内网配置与真实三图/网络，再做F710 shadow和现场动作验收；真实模型端点提供后才进行策略闭环。当前没有完成现场遥操的结论。


## v7 交接新反馈合同核对

离线完成审计发现：v6虽然保证策略使用交接后采集的新图，但state16来自独立ROS回调，可能仍在反馈年龄阈值内却早于START／交接时刻。修复前八组测试全部复现错误发起请求。F1策略适配现要求全部16个接收时间严格晚于 `minimum_observation_ns`；等待期间仍连续录制，不伪造反馈时间、不降低采集频率，不修改公共G2调度默认值。

测试覆盖首次START／退出人工，以及左臂、右臂、左夹爪、右夹爪其中一项仍旧的八个组合。另通过真实multipart HTTP服务器验证：三图持续更新而夹爪反馈冻结时保持HANDOFF_HOLD，第二次模型请求未发出；新反馈到达后使用新state16、新观测和epoch发出请求，旧响应仍被丢弃。专项15 passed；最终完整回归 **150 passed, 3 skipped**（20.71s）。跳过项是独立LeRobot环境，沿用v6最终10 passed及实际66帧/5段全视频回读证据；本轮没有修改导出代码。

相机包装器通过原有RMW检查后显式导出 `RMW_IMPLEMENTATION=rmw_fastrtps_cpp`，防止变量未设置时构建默认选择其他实现而忽略FastDDS XML。启动副本仅重新绑定v7路径，仍待现场原脚本SHA核对、安装及新鲜三图/网络验收，没有改变相机频率，也未应用工作台profile。Shell语法检查通过，不把该检查当成实机DDS互通证明。

证据：`logs/f1_offline_20260928/v7/state_boundary_before.txt`、`state_boundary_after.txt`、`pytest_final.txt`、`source_binding.json`。新不可变候选为 `/tmp/f1-dagger-20260928-stage-v7.tar.gz`，逐文件及包SHA清单为同目录 `prepared_release.json`。v6包和全部历史原始记录保留；v7未部署。

离线交付核对：16D合同/映射、数值IK/碰撞、F710协议/状态机、单一输出与故障记录、30Hz录制/可审计导出、固定依赖与不可变清单、Windows和F1三个终端说明均有对应代码及离线证据。剩余完成条件仍是实际Windows设备输入、ARM真实30Hz/250Hz运行、长期相机网络配置、物理控臂/夹爪/Home及真实模型接管与实录质量审阅。离线模拟不能证明这些条件；按用户要求保持不连F1、不要求终端操作，统一留待下次现场测试。


## 2026-10-08 v7 上线部署

用户重新授权上线，已经原F1-09反向SSH只读核对 machine-id、192.168.31.58 Wi-Fi、工作台v22、profile、驱动二进制及声明参数。本次部署新增v7目录，无同名旧目录；旧v1–v4和工作台入口保留，不切换或终止其他任务。包SHA256 `d6990790ed43762531a9dd3f065edf3161eede885e387fe21acc34da8da82fb2`，86文件远端逐项通过。当前代码与包一致，Windows相对v4只需更新 `scripts/03_windows_f1_sender.py`。

真实ROS零输出shadow为 `/home/user/Bruno/DAgger-runtime/shadow-smoke-20261008-v7/`，专用19397端口归属启动进程核验后才发送合成START/双臂意图。200ACK、46组观测、971控制记录；audit valid=true，所有submitted为空；HIL/SAFE_HOLD及human_arm/human_hold均存在；退出码0。此探针不是Windows手柄或物理运动验收。控制间隔中位4.088ms、最大16.279ms；三图实际10.087Hz，不能将配置30Hz当实测通过。

独立5秒订阅统计确认头部ROS回调29.976Hz，两腕10.011/10.011Hz；腕部源时间戳也是10Hz，排除DAgger采集限速为本次10Hz来源。相机日志和参数表明默认0,0,0自动选择848×480×10；现枚举共同支持640×480×30、480×270×30，848×480只支持5/10。本次没有动态改参或重启相机，没有修改原启动脚本或工作台profile；30fps正式采集待模式与图像合同确认。

证据在 `logs/f1_online_20261008/`：`before_deploy.json`、`inventory.json`、`stage_v7.json`、`preflight.txt`、`shadow_report_v7.json`、`shadow_rates.json`、`callback_probe_console.txt`、`workbench_idle.json`。发布内文档为打包快照，本地本文补记实际部署结果。


相机只读核对补充：左右D405均以USB2.1/480Mbps连接（实际启动日志及sysfs 1-2、1-3.2），当前枚举限制848×480为5/10fps。保持848×480需先处理USB3链路再验证；另一候选是640×480×30，但会改变原始图像合同，尚未应用。最初Cyclone只读参数探针产生FastCDR Get/DescribeResponse警告，同款FastDDS查询成功；未SetParameters或重启进程。

## 2026-10-08 持续录制、相机与采集相位修复（v8/v9）

现场 v7 的 `f1_hil_20261008T063818Z_e30c6c4b` 在等待 START 时持续录图，约42秒后图片在途队列达到256项，第421条观察的图片被拒绝。journal无丢失、图片writer无I/O错误、磁盘约869GB空闲；最终保留partial、关闭7906。旧200ACK短探针未覆盖这个持续写盘场景。

v8独立F1原始pack按15观察/0.5秒分组同步，保持原JPEG/PNG字节、偏移、长度、哈希、真实时间，192MiB/256项上限不变；故障仍不发布完整记录。新旧记录均能审计/导出，新增离线同步审阅HTML。Windows ACK标注真实SHADOW/POWERED，F1每5秒输出输入状态和队列，异常打印原始运行目录及具体拒绝原因。

2026-10-08已部署v8（90文件哈希一致，包SHA `cae9d042f3def3466fc99d0353ce7104afa55a3427940a7d427c386fdc2be14d`）。用户确认后，两腕彩色/深度显式640×480×30；仅逐路停止原相机父/子进程并启动相机专用内部FastDDS。头部原1280×720×30不变。10秒真实回调左30.032Hz、右30.030Hz、头29.983Hz；三路正在使用内部UDP白名单+SHM，Wi-Fi qdisc无积压。4242启动profile仅两个字段改为v8 Bruno副本并读回script_verified=true，原厂脚本未修改；HTTP空闲重载，整组本体服务及电机没有重启。配置已保存，整机重启/下一次基础启动未实际执行。备份在 `results/deployments/20261008-f1-hgdagger-v8/backup/`，F1在 `DAgger-runtime/camera-startup-backup-20261008-v8/`。

v8持续零输出shadow约200秒：75秒START前录制、120秒模拟输入及断连重连，5689组完整三图、6127ACK、最大ACK RTT 8.66ms、图片队列峰值16项/6.12MB、drop=0；审计valid且无实际submitted/publications。控制间隔中位4.10ms、最大18.91ms，目标250Hz不是硬实时保证。原始运行在 `DAgger-runtime/shadow-soak-20261008-v8b/`；证据在 `logs/f1_online_20261008/v8/`。第一次soak调用遗漏ROS环境，没有创建原始episode；该失败日志另留，不覆盖成功证据。

v8采集实测28.606Hz进一步暴露轮询相位漏取：刚在循环开始前到达、尚未消费的新图被原 `receipt>=started` 条件丢弃。v9按每相机源时间与接收时间均严格递增接纳新帧，保留真实采集窗口，不把缓存重读改成新采集；策略原有 `capture_started > minimum_observation` 继续保证START/交接后新图，state16边界也保留。导出补齐窗口起点已生效的同人工epoch/intervention控制证据，防止跨交接图进入纠正集。大pack哈希改为有界流式读取，SHA值不变。

v9离线完整200 passed/3 skipped；独立固定LeRobot环境13 passed，包含新pack三视频和state/action回读；新审阅入口6项已纳入全套。确定性120周期错相测试逐帧无跳、重复receipt/source_stamp拒绝、真实HTTP混合新旧三图拒绝、首次START及epoch/intervention导出边界测试均通过，并经独立审阅。v9现场频率对照与发布结果在后续补记，不将离线结果当实机结论。

### 2026-10-08 v10 候选：真实回调身份与三图有界队列

v9 持续实机 shadow 记录 5717 个完整三图观察，6127 个 ACK，最大 RTT 8.822 ms；写盘零拒绝、审计通过且无机器人动作，但实测 28.7466 Hz 未达到本轮 29.5 Hz 门槛，故在线验收失败，未降低门槛。

两轮各 20 秒只读采样确认头相机 600/602 次回调中出现 7/16 次重复源时间戳；第二轮全部 16 对重复时间戳对应不同 JPEG 和不同解码 RGB 像素，CameraInfo 同样重复。SDK global 时间戳不能独立充当新帧身份。证据保存在 `logs/f1_online_20261008/v9/head_timing_audit/`。

v10 使用 F1 专用有界回调队列；真实 ROS 回调序号与本机接收时间严格递增，原始时间戳保留，显式身份声明下允许源时间戳重复，回退仍拒绝。队列最多每路 32 帧、合计 64 MiB；age/skew 淘汰计数和断流标记进入审计及导出边界，溢出明确失败。START/恢复策略仍只接受交接后收到的新帧。

打包前完整离线串行回归 227 passed / 3 skipped；固定 LeRobot 环境 32 passed（覆盖 writer、reader 和身份边界）。初次同时跑媒体测试时，一项真实墙钟集成测试因 output gap 68.7 ms 触发保护；串行完整复测通过，保留初次失败日志，未放宽看门狗。此处仍是候选，在线持续 shadow 结果另记于本节后续。

### v10 在线失败与 v11 后续候选

v10 已校验部署 90 个文件（包 SHA256 `7bcf179570e997591b611dc91ef7fee640c8aebc969a4b9dfcba5165411616bf`），ARM 数值验收通过；但约144秒实际 shadow 在观察4297、ACK3448后因头相机 callback clock invalid/regressed 停止。此前头部重复源时间戳169次；写盘拒绝0、在途峰值16组、约6.09MB，最大ACK RTT8.734ms。没有真实动作、未生成accepted配置。完整失败证据保存在 `results/deployments/20261008-f1-hgdagger-v10/`，原始partial保持不变。

另观察到相对采集等待累积唤醒延迟，约29.8Hz下相机队列逐渐达到5–6帧并出现age淘汰。v11候选将F1改为绝对节拍；G2默认等待不变。单独源header回退帧明确drop并保留old/new时钟诊断和断流标记，正常后续帧恢复；不放宽审计时间顺序，不将坏帧重标为新帧。host receipt时钟回退或无效header仍失败；持续无合格三图仍由2秒采集超时终止。v11通过情况以随后在线报告为准。

200秒只读头部探针确认5993回调、175个重复stamp及1次真实源时间戳回退：相邻callback间隔33.418ms、header回退0.255ms，JPEG与解码RGB均不同；下一帧33.456ms后恢复正常。证据 `logs/f1_online_20261008/v11/head_regression_probe/summary.json`。这支持只丢弃回退帧并保留边界，不将全部后续回调锁死。v11完整离线串行回归239 passed / 3 skipped；新增采集节拍确定性测试覆盖1000轮唤醒延迟、延迟追赶、中断和记录失败。G2共享采集循环未修改，F1独立覆盖循环并保留录制与epoch绑定次序。

## 2026-10-08 v12 输入修复（已部署，修复后动作待现场验收）

现场 `f1_hil_20261008T092037Z_54565d3b` 完整保存4295组观察、16115条控制；Y两次有 `home_completed_feedback`，首次关节反馈确实到位。323个RB有效包的平移/旋转方向全为0，用户随后在joy.cpl确认圆形左摇杆驱动POV；按MODE后确认恢复X/Y，解释控臂输入未到达。人工状态外又按START会进入manual_hold暂停。

另外，右侧首个RT在packet762从None变为1，持续93包，但双夹爪目标/发布/反馈全程0。F1 `GripperIntent` 混淆“未知侧”与“已观察None基线”，导致第一次新RT被吞。v12只修此区别；未知侧旧锁存、接管/暂停旧值仍不回放。Windows协议、G2映射、IK和输出保护不变。原始日志及MODE确认在 `results/input_diagnosis_20261008/powered_092037/`；修复回归在 `results/input_diagnosis_20261008/gripper_fix/`。修复后实机夹爪与非零摇杆运动尚待验收。

发布前扩展回归最初出现两项Home夹爪保持失败：旧离线fixture在无RT操作的场景中，Home期间发送None，结束后自行改成0，伪造了真实mapper不会产生的新夹爪意图。实测冻结mapper确认该场景应持续None；仅纠正fixture默认输入，原保持断言未改。最终同组44 passed（11.36s），ARM验证通过；原始失败及定向37项通过证据保留在 `results/input_diagnosis_20261008/gripper_fix/`。


## 2026-10-09 目录重排时的状态摘要（历史原文）

以下保留原 STATUS 的完整事实与测试数字；后续部署已替代其中版本描述，现役入口关系见 [STATUS](STATUS.md)。这段不是本次新增验收。

# F1 状态

更新：2026-10-09。

- 现役机器人：v15，用户已确认顿挫消失；右腕相机已由用户修复。此轮项目整理没有替换控制 release。
- 开发源码：`DAgger/F1`、`DAgger/common`。新平台入口回归 329 passed / 3 skipped；F1 独立包通过解压、发布清单和禁止 G2 导入检查。3 个跳过项需要独立导出环境，本次未重复导出验收。
- 工作台 HIL：v23 已在 F1-09 上线，三端启动指引、低频状态与诊断摘要已可用；目标 ARM 25 项相关测试通过，线上资源 SHA 已核验。此次仅更新工作台，现场控制仍为 v15；实时遥操状态需由现场启动后确认。
- 真实模型端点与权重尚待配置，当前现场入口为人工纠正采集。目录变更不替代下一 release 的 ARM/shadow 验收。

[历史验证与完整证据](F1_VALIDATION.md) · [操作说明](../README.md)

## HIL 改良候选（未切换现役）

已实现彩色三图工作台、结构化状态、会话用途、模型服务合同/试推理、policy-shadow、旧推理异常隔离及会话后服务器视频回传。软件和候选包先交付，用户已选择现场测试另约。详见 [操作手册](HIL_WORKBENCH.md) 与 [验证和发布边界](HIL_RELEASE.md)。
