# F1 当前状态

**11月4日14:52正式激活F1 RTC＋局部RLT V47，保留V46回退。** 四条豆包短语音已部署，正常六音保持；Windows继续V46完整包。38项本地／38项ARM、三模式身份及190文件核验通过，没有启动动作。

RLT预取等待、人工边界恢复与连续采集验收仍未完成。详见[发布与未完成项](../../../tasks/F1_ComplexyToys/inference/experiments/20261104-voice-V47.md)。以下保留历史版本记录。

## V45：连续性与待审协议

适用范围：2026-11-03控制V45已部署；Windows V45整包已上传待安装，现场验收待完成。历史版本说明保留，勿覆盖新客户端。

**11月3日V43已部署局部RLT版本显示。** 仅新增只读状态身份及终端呈现，实际控制版本、局部actor与Windows启动器分别显示；Windows V43包已上传待安装。ARM14项和正式零输出通过，RTC／保护／网络期限／音效沿用V42。普通rollout／hil实际客户端未随装配升级；见[V43发布范围](../../../tasks/F1_ComplexyToys/inference/experiments/20261103-terminal-identity-V43.md)。12:44条推理自动恢复后因操作输入失鲜再次暂停，通信与重连问题仍待修；运行和训练事实统一看[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)。

**此前V42已部署局部RLT入口。** VLA候选边界拒绝保持原START授权，修正误播接管音及首条暂停事件的requires_start标记；RLT保留完整请求时延历史，只有覆盖足够才预取，覆盖不足执行原有效计划至原到期后重规划。没有放宽碰撞、时基、epoch或动作有效期。

ARM219项、正式真实反馈零输出及三模式身份校验通过；旧V41可回退，Windows V38与GPU3服务组件保持。实际连续性和声音待现场，不能据此宣称C16已经无停顿；[V42复盘](../../../tasks/F1_ComplexyToys/inference/experiments/20261103-rlt-rtc-V42.md)。训练撤回与当前学习器统一看[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)。以下为历史发布时点。

**11月3日V41局部RLT RTC已部署F1与H20。** ARM 201项、正式真实反馈零输出及候选读取通过；执行旧承诺前缀时异步计算新后缀，复用原RTC时钟、唯一owner与epoch取消。纯时序迟到保持后用新观测重规划，同条录制继续。actor只在下一次→切换，START／↑／Y操作不变。

训练记录与RTC计划解耦：只保存真实执行证据，局部成功终态在交VLA／Home前冻结，未执行尾段不当RL动作。优先包和新1024步Q学习分支已接通；Windows V38继续复用。无线延迟长尾仍存在，真实运动连续性、首条RTC replay与连续采集待现场；详见[V41部署边界](../../../tasks/F1_ComplexyToys/training/experiments/20261102-rlt-rtc-V41.md#deployment-20261103)。以下保留历史时点。

**11月2日V39：首次START输入抖动恢复已部署。** 首策略发布前同会话新鲜证明允许沿原START重新观测启动，真实断连／会话变化和首策略发布后撤销；ARM160项、正式真实反馈零输出通过，现场待验。局部RLT仍同步，训练权重未改；[实现及回退](../../../tasks/F1_ComplexyToys/inference/experiments/20261102-rlt-repair-V39.md)。

V38客户端已现场使用，日志回传正常；最新条仍因启动前输入失鲜经过START接管／RB恢复／START交VLA才输出。原件完整，C16末点修复本条真实生效；晚按↑纳入撤离尾段，需要审核裁切，见[最新逐段审计](../../../tasks/F1_ComplexyToys/inference/experiments/20261102-latest-rollout-audit.md)。

Windows V38已打包上传，修复V37中文乱码及ssh.log文件锁阻断同步；32项本地检查通过，Windows安装和现场验收待完成。机器人保持V37。[本轮证据](../../../tasks/F1_ComplexyToys/inference/experiments/20261102-session-unicode-V38.md)。

V37已部署局部RLT常驻音频，Windows独立手柄隧道／非阻塞显示包已上传待安装。本次START已收到，卡在输入失鲜恢复；软件与正式零输出通过，现场长尾和声学时序仍待验。见[本轮结果](../../../tasks/F1_ComplexyToys/inference/experiments/20261102-session-audio-V37.md)。

**11月2日F1 SSH＋手柄会话V36已提供Windows更新包。** 修复Ctrl+C退出循环及sender持续重连，新增独立日志续传到F1／6000D；25项及真实日志链路通过，Windows现场安装待验。机器人V34不变；[命令与说明](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#single-window)。

**11月2日F1 RTC＋局部RLT V34已部署。** C16取样结束后继续原末点，通过平滑、发布与反馈到位检查才进入下一周期；START随时撤销，超时保留同条供人工接管。本地／ARM各153项、装配48项及正式零输出通过，真实运动仍待现场短条。普通rollout／HIL显式旧覆盖保留；[执行与异步数据整改](../../../tasks/F1_ComplexyToys/inference/experiments/20261102-framework-repair.md)。

局部优先上传、H20保全恢复与W&B回填已完成；训练新动作语义尚未生产切换，当前事实统一见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)。以下保留各历史版本记录。

**10月30日晚：VLA时序重规划与非阻塞ACK已发布为`20261030-f1-rlt-loop-repair-r1`，本地／ARM各109项及正式SHADOW通过；现场新条待验。** RLT入口显式启用纯RTC迟到后的撤旧轨迹、保持发布、新观测bootstrap；START／Y／输入或硬件异常仍撤销自动路径。手柄ACK与接收解耦，200ms新鲜度保护保留，不能据此认定历史229ms就是ACK阻塞。详见[四步修复](../../../tasks/F1_ComplexyToys/inference/experiments/20261030-four-rollouts.md#loop-repair-20261030)。

下面为此前已部署事实；最新候选与训练进度统一见任务STATUS。

**10月30日：单窗口r4与机器人终端装配已发布。** 就绪信号独立于tmux屏幕，关闭绿色装饰，区分手柄未连接与等待START；61项检查通过，Windows实机待验。控制客户端现为manual-compute-r3，计算进程隔离已上线，本地／ARM各89项及正式RLT零输出通过，真实运动待验。见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)与[更新命令](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#single-window)。

**2026-10-30：B1250共用客户端已升级为manual-step-r1，RLT人工阶段增加安全小步候选检查。** 15次真实拒绝已复算，当前姿态安全而IK领先目标跨界；只缩短原人工方向并重验，无法找到安全替代仍按原RB暂停恢复。原硬保护、录制／SSH隔离保留，RB恢复不误播START接管音。双端102项、15事件ARM数值及正式RLT零输出通过，已空闲激活；实际手感与长时稳定性待现场。见[版本证据](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#manual-step-20261030)。

本文以下保留此前版本证据。当前任务操作、完整门槛和训练闭环统一看[任务STATUS](../../../tasks/F1_ComplexyToys/STATUS.md)、[推理技术路线](../../../tasks/F1_ComplexyToys/INFERENCE_RECIPE.md#fault-supervision)与[启动说明](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#single-window)。本轮不改变G2或独立人工示教的发布版本。

## 此前部署记录

**10月29日：RLT提示音提前至操作受理，去除每次400ms静音，已部署。** 录制和阶段提示共用音频队列；过时提示撤销，↑成功不被紧随的VLA交还音覆盖；音频异常不影响控制或封存。本地173／ARM173、真实输入零输出通过；客户端`20261029-f1-rlt-cue-order-r1`，原单窗口／START流程保持。实际可听起点仍需现场确认；现有保护及尚会退出的故障见[本轮验收与门槛](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#cue-order-20261029)。

**后续故障传播审计已完成，生产代码未改。** 确认临时反馈／采集错误退出、人工IK dt结束整条、手柄波动先关闭RLT局部、guard永久关闭，以及打印／回执／SSH寿命耦合；普通rollout与HIL处理也不同。证据和优先级统一见[任务审计](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#fault-lifecycle-20261029)，不能宣称所有故障均可RB恢复。

**10月29日15:38复现已修正：上一版漏掉RLT等待状态，现补齐策略保持恢复并隔离路径预检进程。** 同输入ARM对照中，控制采样最大间隔由并发线程约21.5ms降至独立进程约9ms；全部安全检查与50ms门槛保持。新客户端`20261029-f1-rlt-process-guard-r2`已激活，本地156／ARM156、真实模型进程及正式零输出通过；原单窗口、START唯一开条保持。931帧原件已按中断封存，长期实机稳定性未验，先短条核对。见[复现、修复及证据](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#process-guard-20261029)。

**10月29日：接管后退出已定位为静止输出时序超时，非限位拒绝；有限恢复与异常封存修复已部署。** 两臂已停、RB松开且真实姿态安全时保留同条，输出节奏稳定后回中、新RB恢复；运动／碰撞／发布硬故障仍停。RLT取消Enter，真实START唯一开条；新客户端`20261029-f1-rlt-stationary-hold-r1`，本地138／ARM138及正式真实输入零输出通过，现场恢复待验。此次1538帧及6037控制原件哈希通过，仍为中断待审，不自动入训。见[核查与边界](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#stationary-output-20261029)。

**10月29日：RLT的Y已补齐右夹爪张开及反馈到位门槛。** 右臂先沿原路径回位，然后张开右夹爪，两者实际反馈稳定两秒才封存；原件和模型保持。已发布客户端`20261029-f1-rlt-y-gripper-r1`，本地56／ARM100项通过，现场动作待验；详见[验收与边界](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#y-gripper-20261029)。

**10月29日：RLT在线Q客户端已发布，原单窗口命令与手柄流程保持。** 新客户端记录候选探索身份／种子，修正Y策略暂停指导及保存后待机断流误提示；未带入其他任务的Home或runtime改动。ARM107、真实相机Q请求、正式SHADOW通过，无动作发布；用户已确认现场允许恢复运动，下一条由现场Enter／START执行。完整训练、版本和回退只看[任务当前状态](../../../tasks/F1_ComplexyToys/STATUS.md)与[本轮证据](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#online-q-20261029)。

以下为此前版本快照，Q状态以任务STATUS为准。

**10月28日16:53现场回归已定位并修复部署，连续动作待复验。** START和VLA返回正常，整段预检额外耗时381～752ms，导致RTC约467ms接续期限超时。当前改为VLA衔接点预检，后续目标及平滑输出仍逐次保护；RLT保留整段检查。当前装配`unified-rlt-rtc-guard-20261028-r1`、客户端`20261028-f1-rlt-rtc-guard-r1`；ARM计算衔接检查约7.5ms、零输出通过。下一步重启原入口先验VLA；训练和Q状态不变。[时序与修复证据](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#rtc-guard-latency-20261028)。

**2026-10-28：RLT前置路径保护与暂停接管版本已激活。** 装配`unified-rlt-preventive-20261028-r2`、客户端`20261028-f1-rlt-preventive-r1`。候选越界但当前实测姿态和保持目标均安全时，策略暂停后START接管，再松键回中、等待新ACK并重新RB；人工暂停后回中、新RB。同条录制及局部任务保持；当前不安全、已接触、反馈或发布异常仍硬停，Y不能绕过。

201项ARM离线及正式入口零输出通过，未发机器人动作，实物停止余量与恢复待现场。事故回放确认第一RLT候选已跨过代理模型边界，新预检会更早拒绝；代理间隙不等于实物距离保证。后台已升级，暂停保持不入训练，Q仍关闭；先现场短条验收，再讨论连续训练，见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)、[本轮证据](../../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md#q-safety-20261028)及[原单窗口命令](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#single-window)。

## 此前已部署状态与其他主线

**10月27日20:27：RLT单右爪录制修复已发布。** 首帧按实际硬件要求joint＋right，双爪仍要求完整三topic；有观测却无control会明确失败并保留原件。本地83／ARM59／装配64及真实反馈零输出通过，现场修复后短条待验。旧4条RLT记录均缺control，不能补造入训；仅RLT冻结客户端切换到`20261027-f1-rlt-record-r1`，原B／人工HIL冻结组件未替换。[验收与边界](../../../tasks/F1_ComplexyToys/inference/experiments/20261026-rlt-runtime.md#record-fix-20261027)。

**2026-10-27：保留原B1250入口，新增固定RLT BC400全手柄模式并已部署。** START开条先B、→切actor、START接管／交还当前阶段、↑保存；旧B／人工路线的START行为不变。首次需全手柄sender，已更新者无需重复安装；真实手柄闭环和任务效果待验。

人工示教暂停恢复修订已部署：保留限位与碰撞检查，将人工候选碰撞和手柄链路暂停统一为松RB回中、确认输入后重新RB，继续同一条录制。恢复修复的软件、ARM与真实反馈下等待START的零输出检查通过；后续自主收尾保存修订已部署，离线和ARM通过，真实手柄恢复与保存仍待现场；不能把所有保护状态都当成可恢复暂停。

用户已明确要求删除这三段，删除事务及未选记录保持已核验；分析证据保留。拆除左夹爪的后续数据处理由用户另行处理，本轮不修改人工入口的左夹爪行为，也不将该适配作为本次恢复修复的阻塞条件。


10月27日17:49最新RLT现场：START开条成功，B首请求校验时观测约2.43秒超2秒被拒绝，尚未进入actor。已匹配H20仅147ms，主要长尾在传输／建连；冻结源码复现人工回中提示早于有效RB握手，以及能力协商成功后sender旧提示残留。原件已中断保存、程序已退出；现已发布`20261027-f1-rlt-start-r1`／`unified-rlt-start-20261027-r1`修复：START前准备连接、回中后往返确认再提示RB、新session安全协商和清除旧提示。ARM116／装配58／正式SHADOW及真实B与actor提案通过，未发动作；网络对照及现场恢复待验，见[现场诊断](../../../tasks/F1_ComplexyToys/inference/experiments/20261026-rlt-runtime.md#start-latency-20261027)。

## 2026-10-27 RLT-HIL全手柄已部署

**首次现场的H20权限故障已恢复。** 用户恢复本人目录树属主后，真实B／actor请求和日志写入通过；服务堆栈确认此前PermissionError导致断连。错误提示修订经本地／ARM39项通过，已部署`20261027-f1-rlt-errors-r1`客户端和`unified-rlt-errors-20261027-r1`装配。原手柄及Windows包保持，现场任务效果仍待验；[恢复记录](../../../tasks/F1_ComplexyToys/inference/experiments/20261026-rlt-runtime.md#permissions-restored-20261027)。

此前全手柄客户端`20261027-f1-rlt-gamepad-r2`、装配`unified-rlt-gamepad-20261027-r3`的验收保留，当前仅错误提示由上方新版替代；本地控制101、ARM168、装配及显示58项通过，真实相机／反馈actor提案有效，正式shadow零输出、无新增录制。原B模式保持，工作台RLT目录已登记、未重启服务。

用户需更新Windows sender并验一条→切换／START交还／↑保存。↑只保存，↓不打分；Q和在线更新未开启，reader尚需适配同步保持与截断。完整[三端命令](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#rlt-gamepad)与[同版报告](../../../tasks/F1_ComplexyToys/inference/experiments/20261026-rlt-runtime.md#gamepad-20261027)。此前键盘b、连接阻塞与暂停结论已被本段替代。

## 2026-10-26 策略推理仅保留B1250（已部署）

按用户后续要求，统一装配切为`unified-b1250-only-20261026-r1`，rollout与HIL均只接受B1250。默认模型与显式`--model stageb1250`一致，父1K、A1250及旧Full别名已从活动登记移除；早前多模型选择机制和旧装配保留，恢复其他模型须重新部署及核验，不能运行中热换。完整三端命令、日志位置与当前可选模型统一见[任务STARTUP](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#rtc-hilfull1k真实手柄接管)。

B继续使用H20 GPU1的8138与F1的8153，模型进程及启动身份、rollout／HIL两份配置保持；另外三套GPU推理服务及F1隧道已停止，权重与日志未删除。工作台`installed_policy_models`只保留B，policy路线及runtime保持，无需重启工作台。部署未发机器人命令，也未新增真实HIL接管验收；显存回收及实时资源以[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)为准。证据见[机器人配置回执](../../../tasks/F1_ComplexyToys/artifacts/20261026-b1250-only/robot-activation.json)与[H20停止回执](../../../tasks/F1_ComplexyToys/artifacts/20261026-b1250-only/h20-stop.json)。

## 2026-10-26 策略HIL默认切B1250（较早部署；现仅保留B1250）

以下保留较早多模型部署事实，父1K已不再是当前可选模型。当时机器人统一装配切到`unified-hil-b1250-20261026-r1`，策略HIL默认B1250，启动时可显式选择`--model stageb1250`或`--model dagger1000`；先保存并退出当前运行端，再启动所选模型的新会话，不支持活动录制／RTC执行中途热换。继续使用已验收的`20261023-f1-hil-save-optimized-r1`客户端、原IK、控制保护及START接管／↑保存流程；Windows sender无需更新。完整三端命令与日志位置见[任务STARTUP](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#rtc-hilfull1k真实手柄接管)。

B1250使用原GPU1服务，F1本地8153转发H20的8138；父1K保留8154转8139。工作台新增显式`installed_policy_models`登记，两种模型各自runtime的状态与收尾均能识别，未知路径继续拒绝；部署细节见[工作台状态](../../../F1-4242/docs/STATUS.md)。本次不修改模型权重、不启动训练，也不重启模型服务器。

本地79项与ARM状态19项通过，6种模型／模式身份及默认／显式B／显式父1K公开入口检查通过；现场115条目录与备注、网页和安装配置保持，机器人动作0。此结论只覆盖装配与软件，不代表B1250真实HIL接管、暂停恢复或保存已完成现场验收。证据见[部署回执](../../../tasks/F1_ComplexyToys/artifacts/20261026-hil-b1250/activation.json)与[配置对照](../../../tasks/F1_ComplexyToys/artifacts/20261026-hil-b1250/config-validation.json)。

## 2026-10-23 自主收尾保存（已部署，现场待验）

人工示教与RTC-HIL统一取消保存前的回位／两秒门槛。操作者自行收尾，松RB与START、等夹爪操作完成后按↑；Y仍是可选快捷回位，执行中等动作结束即可，不再额外计时。Windows已安装的连续分条版本无需更新，刷新F1infra并新启动运行端使用。

人工包`20261023-f1-manual-save-r1`、HIL包`20261023-f1-hil-save-optimized-r1`及装配`unified-rlinf-save-20261023-r1`已空闲持锁切换；保留刚上线的推理优化、原IK、保护与左夹爪基线。四个网页提示文件热更新，后端与模型服务未重启。本地保存／恢复48项、反馈5项、工作台13项，三桌面两路线6项显示及63项恢复浏览器回归通过；ARM人工43项通过（23项RTC相关未选），最新HIL66项通过。该修订未跑新shadow、未发动作或启动训练，首条新数据尚待现场与审核。[分层验收](../../../tasks/F1_ComplexyToys/artifacts/20261023-manual-finish/acceptance.json) · [部署回执](../../../tasks/F1_ComplexyToys/artifacts/20261023-manual-finish/activate-optimized-receipt.json)。

## 2026-10-23 人工暂停恢复统一（已部署，真实手柄恢复待现场）

START只开新条或从策略转人工接管；人工候选碰撞、输入超时、TCP重连或手柄断开后保留同条，松RB、松其他按键并回中，等待新鲜输入确认后重新RB，再换安全方向。关节限位继续允许向内退离；恢复还要求新鲜实测反馈与合法起点，持续按住RB不能在条件稍后变好时自动恢复。暂停期间继续真实观测录制并标记质量边界，丢弃旧输入，不回放积压命令。

已发布姿态异常、反馈／owner／发布／协议等硬故障仍保护封存中断条，排障且保存完成后START新开条；原有策略请求／IK失败但仍在录制时的START转人工接管同条例外保持。该恢复版当时正常完成仍要求Y回位后保持两秒，现已由上方自主收尾保存修订替代。F1infra状态、终端提示和RB教程同步，不改布局、限位、控制频率或左夹爪适配。

ARM人工包95项通过、18项RTC相关测试因该包不含RTC而未选；HIL包123项通过，含两个真实WebSocket用例。工作台相关19项、反馈提示5项及推理终端7项本地通过，真实浏览器覆盖三桌面尺寸／三路线63种暂停组合及硬停、策略故障、限位、RB教程，脚本异常和网络写请求为0。真实反馈shadow在等待START状态分别收到人工237／HIL241组观测，提交与发布均为0，无新录制；未操作真实F710，不能将其记为暂停恢复现场通过。[本轮验收产物](../../../tasks/F1_ComplexyToys/artifacts/20261023-manual-recovery/) · [操作说明](HIL_WORKBENCH.md)。

已空闲持共享锁、备份并切换人工／自由接入到`20261023-f1-manual-recovery-r1`，策略HIL到`20261023-f1-hil-recovery-r1`；统一装配为`unified-ab-full-recovery-20261023-r2`。工作台部署`20261023-manual-recovery-r1`，三路线ready、五个文件HTTP哈希通过；只重启工作台，模型服务及独立推理／IK保持，Windows无需更新。没有机器人动作或新训练。[部署回执](../../../tasks/F1_ComplexyToys/artifacts/20261023-manual-recovery/activate-receipt.json)。

三条指定原件及关联预览／视频／审核／转换任务已按用户授权删除，目录117→114；三条均不存在，其他记录身份保持，约666MB原件清除。未启动训练；原始分析报告保留为历史取证，不将其改写为新版本运行结果。[删除回执](../../../tasks/F1_ComplexyToys/artifacts/20261023-manual-recovery/data-deletion.json)。

## 2026-10-23 人工示教保护断段与缺左夹爪适配缺口

14:43这次操作实际为20.66／4.36／30.36秒三段，前两段右J2贴下界，最终由右臂Link5与躯干Link5碰撞风险检查拒绝新目标并封存；未证明发生实物碰撞。再次START新开一段，两个未录间隙13.92／1.78秒。第三段入孔、释放、撤离及Home有画面/反馈与用户确认，保持12.79秒后↑正常结束；期间12次START被忽略，保持仍录制。

三段全量原件审计通过，共1649组三图，无写盘丢失；但人工仍使用旧schema3，拆除左爪被记录为带新鲜时间戳的0值，实际还有4次左爪0命令发布。策略missing-left-r3的适配没有覆盖此人工入口。下一步补人工端能力声明/发布隔离，再验完整录制和有效性mask导出；本轮仅分析，未改源码、部署、原件或训练。[数据与停止原因核查](../../../tasks/F1_ComplexyToys/DataPrehandle/reports/20261023-manual-demo.md)。

## 2026-10-22 DAgger四模型与缺左夹爪客户端

统一策略入口改为新DAgger250/500/625/1K，HIL默认新1K；客户端冻结到missing-left-r3。仅明确缺失左夹爪，不使用驱动缓存、不向左爪发布，控制边界等待其他15个真实字段；全身/右侧保护不变。schema4原件可审计，新训练导出尚待适配；人工示教独立旧入口未据此宣称完成硬件缺失兼容。命令与验收见[任务STATUS](../../../tasks/F1_ComplexyToys/STATUS.md)。

## 2026-10-22 shadow不录制（当前入口）

十个策略入口及infra人工示教入口已切换七个新冻结客户端，继承SIGINT退出修复；shadow只保留普通程序日志和内存计数，不产生图片、逐帧journal、episode或视频。真实rollout/HIL及控制参数不变。稳定框架日常无需先shadow，按输入/通信/控制变化定向验收。

七组ARM录制隔离通过；Full625真实相机5秒、150观测/9动作块、零提交/发布，无新录制，last-session未变。备份与详细证据见[任务推理recipe](../../../tasks/F1_ComplexyToys/INFERENCE_RECIPE.md)。以下SIGINT条目为先前版本的验收快照。

## 2026-10-22 推理Ctrl+C退出修复（已部署）

网络IPC子进程原先一起收到SIGINT，在等待管道时抛出KeyboardInterrupt；主进程仍保存，但终端多出堆栈。现子进程忽略SIGINT，主进程沿原路径停止、封存并关闭子进程，其他异常不隐藏。

十个入口已切到六个新的`-sigint-20261022`冻结客户端，14份外部配置绑定新manifest，旧版本/备份保留。六组ARM退出测试与Full625真实相机Ctrl+C零输出验证通过：退出码0、完整保存、无堆栈。没有执行真机动作；[任务recipe与验收](../../../tasks/F1_ComplexyToys/INFERENCE_RECIPE.md)。

## 2026-10-21 统一连续分条（初始版本；保存门槛已由10月23日修订替代）

示教v33与RTC-HIL r6现统一：START开新条，策略中START接管，人工阶段START不交还也不结束；松RB、Y实际回稳至少2秒后↑保存。保存完成后整理场景再START。默认右臂，原Home、IK与运动参数保留；自由接入不录制。F1infra状态、教程与入口已同步。

203项本地、ARM策略25项/示教27项通过；Windows新包已确认安装。后续r6真实F710连续九条真机纠正已完成，原件审计、人工来源隔离和Home实际保持通过；用户确认全部成功、全程手柄纠正。新九条仅统计6288候选窗，最终逐段审核包与实际reader待完成，旧首条700窗验收仍独立保留。用户选择直接真机，新版分条shadow未执行，示教v33真机仍待。↑共13次接收、9次保存，4次提前按因实际Home后不足2秒被拒；拒绝无声音、需重按，已定位且本轮只分析未改代码。本任务未启动训练。[完整三端流程与摇操教程](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#rtc-hilfull1k真实手柄接管) · [九条分析](../../../tasks/F1_ComplexyToys/inference/experiments/20261021-rtc-hil.md#九条连续纠正分析与保存反馈2026-10-21)。

以下日期记录保留当时版本语义，START结束/交还已由上述新入口替代。

## 2026-10-21 F710三路线默认右臂

Windows sender局部adapter默认右臂，Y/断连后无需重新选侧；保留neutral/RB/RT及START保护，shared/G2不变。102项相关回归与隔离9文件更新包检查通过，包已放F1，用户确认Windows安装成功、9文件校验及两次加载检查通过；新映射真实F710验收待现场；[完整更新命令](../../../tasks/F1_ComplexyToys/inference/STARTUP.md)。F1infra和RTC r5终端提示已同步，原r4确认音量不变。当时统一↑保存尚未实施；现已由上方v33/r6替代。

## 2026-10-21 Full1K RTC-HIL独立接入

新增冻结`20261021-f1-toys-rtc-hil-r1`和独立runtime，显式RTC-HIL复用真实F710、唯一owner、原HIL录制；支持continuous/定时，接管清旧epoch、实测起点人工控制、交还新观测空前缀。原standalone及独立IK候选保留；HIL故障由真实START进入人工并显式交还，不以本地Enter自动恢复。

155项受影响回归、2项真实WebSocket合成输入集成、43项ARM测试通过。用户真实F710零输出会话完成一次策略→人工→交还→新RTC：2330组三图、12请求、4183控制记录、零发布，原件审计与Ctrl+C封存通过；随后用户完成真实纠正：三个START一一对应，72次推理、人工epoch无policy发布；首条700窗（含65步真实末尾保持）已审核并通过reader。r4仅改音效，不改START或控制：首次开始原音、人工接管C下行双音、交还生效上行双音，保存独立；C提高约4.6dB后用户确认与原音响度接近。真实状态触发留待下次正常采集顺带核验。F1infra策略HIL路线已统一命令、当前runtime与保存绑定。完整[三端命令](../../../tasks/F1_ComplexyToys/inference/STARTUP.md#rtc-hilfull1k真实手柄接管)和[分层验收](../../../tasks/F1_ComplexyToys/inference/experiments/20261021-rtc-hil.md)在任务维护，本轮不启动训练。

## 2026-10-20 RTC三图并行client-v5已上线

仅H20 RTC独立入口切换client-v5，三路冻结预处理/PNG1并行；同帧150组字节一致，原控制和IK保护保留。H20同时启用原IK数学加速；50K180秒、5K90秒完整录制共714请求，零RTC超时/零发布，当前静止场景IK拒绝仍多。正常接续客户端约225/222ms，现场闭环待验；入口与完整限制见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)、[启动说明](../../../tasks/F1_ComplexyToys/inference/STARTUP.md)。

## 2026-10-20 积木RTC推理迁H20

按用户授权，client-v4保持，5K默认入口迁H20 GPU0并增加50K/GPU1独立入口，6000D保留回退。CUDA Graph后5K完整录制350秒526请求、零RTC超时/零发布，50K新版90秒135/135成功、零发布；跨环境夹爪过渡差异与更早IK拒绝仍须现场核验。命令、模型身份和数值边界统一见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)、[启动说明](../../../tasks/F1_ComplexyToys/inference/STARTUP.md)。

## 2026-10-20 积木RTC v4部署，长尾仍需资源隔离复测

空闲持锁切换client-v4与编译加速service-v3，独立SSH/5K身份/ARM清单通过；真实相机hil-record的800ms迟到注入验证暂停保持、旧结果丢弃、模拟Enter后新bootstrap及正常封存，零命令发布。首轮长测第303请求模型段约1380ms，触发保持后因SSH测试stdin关闭退出；当次6000D八卡有其他训练共享、F1无线省电重启恢复。不能宣布长尾消失或实物成功率提高，当前指标、复测证据与现场命令以[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)为准。

## 2026-10-19 积木5K RTC独立入口完成零动作验收

新增独立RTC v3客户端和GPU2服务，原同步/数采入口保留；350秒完整Control shadow正常结束，525请求、513计划采纳、12次IK拒绝均保持后恢复，零发布、零RTC超时。84项相关回归通过，144幅ARM/x86像素一致；下一步由现场操作者验证真实动作与任务效果。当前配置、完整证据与启动命令统一见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)、[RTC记录](../../../tasks/F1_ComplexyToys/inference/experiments/20261019-rtc.md)、[启动说明](../../../tasks/F1_ComplexyToys/inference/STARTUP.md)。

## 2026-10-16 积木telemetry v5上线，IK研究暂停

独立策略入口空闲持锁切到v5，只增加请求时延/全量预测/退出原因/会话摘要；73项回归、ARM inspect与10秒真实模型shadow通过，9次请求、296观测、2250控制记录均零发布。数采v32不变；下位内部数据仍未接入并显式标记缺口。IK边界问题按用户要求暂停，15条成功V1进入训练配方讨论。见[任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)、[telemetry记录](../../../tasks/F1_ComplexyToys/inference/experiments/20261016-telemetry.md)。

## 2026-10-16 积木IK恢复v4已上线，零输出验收通过

首次持续rollout第37请求出现IK拒绝后退出；已补保持、新观测最多额外3次重试、耗尽暂停/Enter恢复。102项回归与ARM inspect通过；用户退出teleop后空闲持锁切换v4，未强抢/终止会话。真实反馈故障注入完成4次拒绝→3次重试→暂停→Enter恢复；真实模型20秒20次推理、596观测通过，两轮均零动作发布，现场运动恢复尚待验证。原v32数采入口未改。J6约54.8°跟随平台仍待定位，当前事实与证据见 [任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)、[恢复记录](../../../tasks/F1_ComplexyToys/inference/experiments/20261016-ik-recovery.md)。

## 2026-10-16 积木π0.5独立策略入口已交付

本节保留v3首次交付证据，当前v4见上节。新增独立 `start_f1_toys_policy.sh`，当时绑定 `20261016-f1-toys-eef-v3-sync` 和6000D最终1000步模型；原v32数采/复位入口保留。按用户要求新增独立sync：现场一条命令＋Enter，`--duration-seconds`/`--continuous`，不需要Windows sender或F710；旧HIL的START/watchdog不变。79项相关回归、20秒实际Control零输出shadow（21次推理、598观测）通过，定时退出。ARM客户端、运行合同、持续真实取帧500次零动作shadow及相同载荷通信对照已通过；真机动作尚未执行。当前模型/recipe/测量只维护在 [积木任务状态](../../../tasks/F1_ComplexyToys/STATUS.md)，完整现场命令见 [启动说明](../../../tasks/F1_ComplexyToys/inference/STARTUP.md)。

## 2026-10-16 开始录制约4秒输入暂停：ACK超时重连已确认

最新一条11:16:32开始后出现watchdog，Windows最老未确认703号包等待515ms主动重连；F1已消费706，Windows采样/发送未记录半秒停顿，初始隧道/网络/ACK停顿位置仍未知。11:16:36新RB恢复同条；1837组约29.93Hz，落盘无丢弃或错误，Y反馈完成。恢复提示反复切换及未识别reconnect提示已记录。新视频提速版11:25才上线；旧worker当时已运行，缺少逐块暂停历史，不能完全排除旧后台流量影响。只读核查，未改代码/门槛、未压测。见[输入卡顿调查](incidents/20261016-start-input-lag.md)。

## 2026-10-16 第二条START被相机故障阻止：左腕USB实际断开

补证：用户重插后，三次只读采样中三路帧序号均递增、帧龄均小于200ms，实际画面已恢复。仍需后续连续录制确认连接稳定性；无需更改START或Y逻辑。

第一条10:50:29已正常保存1141组三图，随后10:50:33左腕D405的USB口1-3.2报-71并断开；START事件持续收到但缺左腕新帧，新条未创建。v32没有在此事件自动切录，控制/录制代码未改。网页已热更新具体相机故障提示。重插后已恢复真实新帧（见上方补证）；证据、保存与验证范围见[本次记录](incidents/20261016-next-episode-left-usb-disconnect.md)。

## 2026-10-16 v32 已部署：Y 直接回4242积木demo-V1，断线不自动结束示教

已切换正式三路线入口与独立 `reset_right_demo.sh` 至 `20261015-f1-hil-v32-direct-home-transport`，配置 `f1-09-v32-direct-home-transport.json`。Y取消自动上抬/右移，不增加单独转腕；操作者先手动避箱、松RB，再按Y直接回同一V1。复用4242的 `SmoothBodyPlan`、30°/s与75°/s²；只改变右臂目标，左臂/夹爪/身体/头保持。保留唯一owner、碰撞/限位/新鲜反馈和到位检查，不承诺任意起点都能三秒到位。

普通TCP断开、重连改为同条示教内暂停，清旧目标；重新建立session、新鲜输入与反馈、释放回中后新RB边沿才恢复，不重放旧动作，不播放完成音。START仍显式结束当前条，保存完成后的下一START才开始新条。协议/碰撞/反馈/发布等硬故障仍独立处理。200ms输入保护、500ms ACK门槛与30Hz配置未改，Windows v31 sender无需再次更新。

ARM离线回归最终 **183 passed / 3 skipped**：首轮180通过、3个LeRobot依赖项跳过、3个独立脚本测试因隔离路径适配缺失未运行；补正路径后仅重跑这3项，全部通过。最初缺少共享packet夹具的收集错误亦保留记录。未修改保护参数或断言。含真实数值/碰撞栈的4个历史起点及合成反馈平台检查、连续条目/暂停/恢复/音效/START和Y取消。inspect为hardware_initialized=false。网页精确发布候选6状态及直接回位教学检查无脚本异常、无非GET请求。

部署持共享锁、确认无控制/录制任务，原地备份后切换入口与4项静态资源；未重启驱动/服务、未远程动作、未改原始数据。2026-10-16 09:24北京时间再读回全部manifest、开发/部署五个控制模块、两入口；通过只读4242预设接口逐项核对V1关节完全一致，route=direct、无clearance，HIL空闲。现场Y实际到位与连续采集仍待用户验证，不将软件检查写成长期稳定性保证。

manifest SHA256 `c2be2c33fdc0af143b4059ee612a46bdf5b7e1b06e1980eb924bc61de4c296a8`；config SHA256 `d92a0023b94e531475c374dc6aab3afe52e5bf974071016fabe3ad8cd71b8cf3`。回退备份 `/home/user/Bruno/DAgger-runtime/deployment-backups/20261015-v32-direct-home-transport` 保留旧入口、配置、manifest和静态文件；deployment.json记录精确恢复路径与发布后哈希，回退代码不恢复姿态或删除数据。证据位于 `artifacts/results/20261015-two-failures/`，完整前因后果见[事故与实现记录](incidents/20261015-transport-close-and-y-repeat.md)。原上抬J6跟随平台仍为独立未决问题，新Y明确取消该路线，不宣称硬件根因已修好。

## 2026-10-15 v31 最新Y上抬现场失败：J6持续跟随偏差未解决

最新 `f1_hil_20261015T065555Z_1b4d77b8` 的Y已启动lift，4.759秒后J6目标58.1877°/实测54.7796°、差3.4081°停止，其他右臂轴误差小于0.33°。这次无输入暂停或掉线，不是v31短时恢复逻辑；目标输出已静止近3秒、ROS反馈持续更新。用户确认悬空无可见碰撞/拉扯。与10月13日J6约54.8°平台相似，旧真实跟随问题没有被此前软件修复证明消除，不能再把回归通过写成复位稳定。

只读核对现役驱动与SDK客户端未发现55°裁剪；J6配置±60°，下位内环受理目标尚不在journal中，不能猜55°硬限位。Y只有return/align复用4242，lift/right仍是独立IK，当前失败尚未到return。原版配合合成平台的离线负例在4.76秒复现停止；候选软代价虽能绕过本次姿态，但历史8起点中仍有1个低腕角起点触发姿态保护，因此未上线。真实平台原因与合格路线仍待证据，现役维持v31，不改驱动/容差/速度，无远程动作。

1074观测约29.993Hz，journal三流哈希一致、保存无writer_error；最大观测间隔122.733ms，不能称零丢帧。Home仍排除训练标签。详细根因边界、逐轴证据、历史遗漏及后续采证见[本次审查](incidents/20261015-y-lift-j6-following.md)；独立证据保存在 `artifacts/results/20261015-y-lift-audit/`，不随F1网页删除条目丢失。 已部署独立被动采证工具（60秒/16MiB、无SDK/发包），仅完成离线过滤与语法/哈希验证；现场sudo采证及DCSS解码尚未完成。

## 2026-10-15 v31 已部署：输入短暂停顿保持，同条继续录制

正式三路线与独立复位入口已切换至 `20261015-f1-hil-v31-input-pause`，配置 `f1-09-v31-input-pause.json`。普通人工输入watchdog不再提交/结束录制、不播放完成音；松RB、回中取得新鲜输入证明后重新按RB恢复同条。旧包、持续按住RB、Y/RT不会自动恢复；真实断连、换session、相机/反馈/发布异常保留独立保护，前缀可保存则标记interrupted，不冒充正常完成。START仍显式开始/结束示教条。原30Hz、200ms输入watchdog、500ms ACK、Y路线/速度/保护未调整。

**Windows 首次更新失败，已发布 v31-r2 安装修订，等待用户重试确认。** 原5文件增量包未兼容缺少 `src/dagger_core` 的旧客户端，Copy-Item 因父目录不存在中断；不能把下载成功当安装成功。r2包含两个入口及全部7个公共模块，自动建立父目录，安装前后独立进程检查加载、逐文件校验并备份/失败回退。9文件与机器人v31配套源码哈希一致，隔离旧目录及相关离线回归53项通过；本机无PowerShell，Windows实际安装仍待验证。修订包已上传F1并读回校验，不改机器人release或保护参数。新RB恢复必须更新sender后才能用；隧道不变。更新命令与现场验收步骤见[实施说明](incidents/20261015-input-pause-recovery.md#部署与操作)。本轮未远程发动作，尚未现场验证输入暂停恢复或长期稳定性。

ARM主回归191 passed / 3 skipped，随后线程一致性及交接锚点修订后的受影响回归91 passed / 3 skipped（两轮重叠，不相加）；只读inspect为hardware_initialized=false。最终开发机97 passed / 3 skipped，跳过项需锁定LeRobot环境，未声称本轮正式v3视频reader完整回归通过。浏览器5种暂停状态均显示黄色等待、正确恢复提示与继续录制；无脚本错误、无非GET调用。新增保守质量区间、epoch切分及最多500样本分段计时；未开展新的主动链路/采集负载测试。

发布时确认无控制/采集进程、命令活动或动作任务，持共享锁备份切换；所有清单与HTTP静态资源哈希验证通过。4242仍v36后端，仅热更新既有HIL状态提示，不改布局、不重启服务。现役Y/owner/teleop源码与v30逐文件相同；配置除accepted manifest哈希外完全相同。原始录制未改写。

manifest SHA256 `241e7519183d41883d3cb78fa4b487e431fa133e62556499a7baf81a550e7e99`；config SHA256 `ac025ee908837ba5aa6026b628e44adcb93e8f968b844d461061c51090c3e5ae`。回退备份 `/home/user/Bruno/DAgger-runtime/deployment-backups/20261015-input-pause-v31`，保留v30及旧配置；回退代码不回姿态、不删除新数据。开发机证据 `artifacts/results/20261015-input-pause-v31/`。后台转换扫描服务仅排队，正式导出须选择v31导出代码；未启动重处理或重生成已有数据集。

## 2026-10-15 Pico对照与30Hz补证（仅讨论）

现存9条人工示教10312组三图观测，平均29.676–29.981Hz，汇总约29.916Hz；仍有个别147.9–399.7ms观测间隔，不能称零丢帧。meaxiao固定1.4.4快照具有250ms XR失鲜保持、释放并稳定后重新握持、独立录制；LXL可读桥/录制也分离，但实际dist加密，不能证明全链路无保护。建议借鉴恢复与录制分离，保留当前保护并补分段计时；未改现役或执行新链路测试。源码、历史延迟数字限制及最终决策见[对照记录](incidents/20261015-pico-input-comparison.md)。

## 2026-10-15 历史输入统计与数据利用讨论

只读核对27份现存journal及先前保留的3条证据，其中22份可统计111816对新输入消费间隔；均值23.639ms/P99 76.305ms，最近3条均值31.817ms/P99 119.066ms。此指标不是端到端延迟；超时即封档还会截掉最终断档的后一包，不能只凭相邻包均小于200ms认定无超时。原始中断前有效段可由已有筛选出口利用，但缺少输入失鲜/恢复质量边界。方案、样本缺失及统计限制见[历史分析](incidents/20261015-input-history-analysis.md)。未改现役、未继续主动链路测试，录制与控制暂停解耦尚未实现。

## 2026-10-15 三条录制收尾核对：两次输入超时，完成音误导

现役仍为v30。10:57最新三条中，前两条因operator_watchdog自动收尾（397/44组），第三条收到新START正常保存（1184组）；均提交完整，不能等同三条成功任务。前两条没有重复START，保护状态输入年龄217.7/211.3ms。输入中断未标为interrupted、仍播放完成音的代码路径已定位。第三条Y全流程实测反馈完成约8.914秒，仅证明此次成功，不代表长期稳定。详见[事件与代码证据](incidents/20261015-unexpected-episode-end.md)。本次只读取证、保留日志和更新待办，未改现役、未执行主动链路测试或机器人动作。

## 2026-10-14 v30 已上线：V1预设与4242回程规划器

正式三路线Y及独立`reset_right_demo.sh`已切换至`20261014-f1-hil-v30-workbench-home`，配置`f1-09-v30-workbench-home.json`。使用4242实际“积木demo-V1”快照；旧V0与V1右臂最大差约0.4853°，本次已准确统一。回程/重试起点对齐直接导入已绑定4242的`SmoothBodyPlan`，速度30°/s、加速度75°/s²；仍走原唯一owner及连续输出门控，相同曲线参数不代表实际发布节拍或总时长完全相同。

避让仍为上抬6cm→右移10cm、4cm/s，不增加独立转腕。超过1°的中间段跟随偏差先等待原profile的3秒收敛预算，不在150ms后立即报失败、不追赶追加补偿；持续偏差仍停止，最终回程同样有有界到位等待。中间段不能仅凭末端接近而忽略关节跟随。明确非法的右臂候选在批准前转为局部Home停止，原已提交非法、发布失败、碰撞和watchdog故障仍保留全局保护。

最终ARM回归 **126 passed，253.16秒**，含逐样本4242曲线一致性、1.52°暂态等待/持续偏差停止、V1历史起点完整三段及延迟输出、候选与已提交非法的分类、Y/RB/START、独立脚本及连续录制。首次90通过/1失败源于新增测试夹具只改了当前目标却没改原定目标，修正夹具后重新完整验证，未放宽断言。inspect为hardware_initialized=false。本轮未执行机器人动作，V1现场按Y验收仍待进行，不能承诺所有起点一定成功。

切换前核对无控制进程/活动命令并取得共享锁；部署后两入口、配置及全部清单哈希读回通过，代码变化仅配置、独立脚本提示、clearance_home与teleop四文件。备份`/home/user/Bruno/DAgger-runtime/deployment-backups/20261014-y-workbench-home`，保留v29。发布manifest SHA256 `dce1f4ad84c89a5aa52fce9bb4fe655b54176099ff6ff46643a616ee71d2a12d`；配置SHA256 `8e3ff0bb549413298ead967e290e9957017f83d01bbf50f00335d477d5d47f47`。未重启驱动、相机或工作台，未修改录制数据。下次启动正式运行端即使用v30，Windows sender/隧道无需更新。说明见[复用记录](incidents/20261014-home-workbench-reuse.md)，产物在私有`artifacts/results/20261014-y-v30-workbench/`。

## 2026-10-14 v29 现场再次失败，尚未通过稳定性验收

17:15:53条目完整Y复位成功8.106秒；随后17:17:12条目先后发生上抬跟随差1.351°、1.519°，重试后组合腕限位异常进入SAFE_HOLD。最终停止时输入仍新鲜；后续断连不是本次起因。该条保存摘要为1312组、errors=[]，17:20:30经网页删除，逐关节日志已不可用，不能认定具体故障轴或超限量。本轮只读取证和代码审查，未更改现役、放宽保护或远程动作。证据、异常分支与后续要求见[本次事故记录](incidents/20261014-y-v29-following.md)。

## 2026-10-14 v29 已上线：取消额外起点腕角限制，保留三段复位

正式三路线及独立`reset_right_demo.sh`已从v28切换为`20261014-f1-hil-v29-home-clearance`，配置`f1-09-v29-home-clearance.json`。唯一运行源码改动是`src/f1dagger/clearance_home.py`：删除“J6绝对角不得超过本次起始角”的额外任务限制，七关节按原硬件和腕部组合范围协调运动。路径仍为上抬6cm→向右10cm→原右臂Home；不增加独立转腕，不改4cm/s、10°姿态预算、到位、反馈、碰撞、输出动态或录制逻辑。

本版ARM回归84项通过，217.62秒。新增此次两个低腕角真实起点的完整三段路线（无滞后/40ms合成跟随滞后，候选及250Hz输出均过原碰撞检查）、每输出步朝向/动态/不活动轴检查及腕部不跟随必须停止而不能报成功。旧高腕角起点保留带延迟/偏置的完整路线检查；去掉与被删除任务限制绑定的“腕角不能增大”断言和假定55°饱和的正常跟随模型，另以明确不跟随测试及原大偏差/失鲜测试检查故障保护。回归还覆盖Y重试、RB/START取消、连续示教提交和输出连续限位。

发布时确认无HIL/独立复位进程、4242无活动命令，持共享锁备份并切换。部署后两入口、配置、全部发布清单哈希读回一致，源码差异仅上述模块，inspect为hardware_initialized=false。未远程发动作、未重启任何服务或驱动、未修改录制数据；旧v15及其他候选入口没有切换。下次按原流程启动正式三路线即使用v29，Windows sender/隧道无需更新。

manifest SHA256 `e199c6b41f7fc24c79cc14d677237ecfefa98b6759ca8e6878c6753fbdf773dc`；config SHA256 `a45577f56f910dbe74830086240da5a6644ecd8a5865e6c56bf2578610102352`。回退备份为`/home/user/Bruno/DAgger-runtime/deployment-backups/20261014-y-home-clearance`，保留v28发布与旧配置；回退只恢复代码入口，不恢复姿态或数据。测试/部署/读回证据在私有`artifacts/results/20261014-y-v29/`。

还需现场按Y验证实际作业起点的完整回位及下一条录制。此次84项是无动作软件/数值回归，不是实机到位或长期稳定证明；旧腕部真实跟随异常、输入间断和USB相机问题仍按各自事项处理。此次定位与补证见[姿态预算审查](incidents/20261014-y-orientation-budget.md)。

## 2026-10-14 Y复位再次现场失败，额外腕部限制主因已定位，现役未改

当天14:45最近三条录制均实际运行v28。14:45:17和14:45:27两条共收到11次Y，全部lift阶段orientation budget exceeded；首次各约1.23/1.27秒，重试67–108ms即停，均未完成复位。另一条14:45:06因operator_watchdog收尾，是独立问题。三条记录均提交完成，无writer_error，training_ready仍为false。13:00独立脚本确有另一姿态的完整实机成功10.389秒，不能覆盖此次两个失败起点。

用记录的真实右臂起点做8组ARM数值对照：原版1.24/1.30秒复现10°候选朝向预算停止；仅增大朝向权重仍上抬超时；仅取消“J6不得超过起始绝对角”的额外任务限制、恢复原硬件范围，两例均完成三段，朝向仍在原10°内。再启用原碰撞检查及40ms合成反馈延迟，数值完成6.42/6.62秒。所有结果均无动作输出，不能当作实机修复。

现役仍为`20261014-f1-hil-v28-home-ready`，本轮只做取证、隔离诊断和文档更新。额外腕部约束、预算事后判停、同检查点重试无法推进以及测试起点覆盖不足须一起整改；旧高J6实物跟随偏差仍未解释。未改保护、未部署实验参数、未重启服务或修改录制数据。完整证据、实验边界和修复验收要求见[此次审查](incidents/20261014-y-orientation-budget.md)。

## 2026-10-14 复位 v28 已部署，两个预演故障修复，完整实机动作待验

正式三路线和独立脚本现为 `20261014-f1-hil-v28-home-ready`，配置 `f1-09-v28-home-ready.json`。上抬6cm、向右10cm、4cm/s及原到位/限位/碰撞/动态保护不变，不新增独立转腕动作。旧v15遥操和其他候选入口未切换，正在运行的旧进程不会热更新。

12:40用户v26独立脚本在预演阶段失败，未确认Enter、未发动作。日志完整姿态离线复现为上抬57.722658mm后输出卡住；认证轨迹结束后的浮点速度/加速度残值使Phase同步退化，在腕部组合边界反复刹停。v27仅在已完整结束的认证轨迹采用原定精确零终态，保留位置和全部保护，76项ARM回归通过（189.15秒），包含原失败姿态完整预演和碰撞/连续动态检查。

v27部署后--check又发现当前右臂已在Home 1度到位范围内，但旧起点0.01度条件仍要求绕行并触发朝向预算。v28使已提交参考及实测都在原1度范围内时只做短回位收敛，指令终点收敛、实测与稳定时间要求仍保留。追加54项复位/录制ARM回归通过（171.32秒），包含0.999/1.001度判定边界、当前已到位姿态仅return、原失败姿态仍需lift/right/return。两轮测试有重叠，不合并计为130个独立用例。

两次发布均空闲持共享锁。v27第一次因4242占锁退出，自然释放后才切换，没有停止其他会话。最终源码清单、配置绑定、两入口SHA与只读inspect通过，hardware_initialized=false。v28随后对当前真实姿态做--check：`right-reset/20261014T045539.215504Z`预演通过，仅return、模拟0.64秒，commands_sent=false、physical_arrival_verified=false。这个当前起点已在Home范围内，不能当作完整避让的现场验收。本轮没有远程动作、重启驱动/服务或修改采集数据。

manifest SHA256 `6db76613ab8ce9267d0486854e3dacedd69dec01ee6406aa32133eb7af4b5d1d`；config SHA256 `f444263241c2f39b7e3ed903b056db428310026999fc9759cd20bac2502be717`。备份分别在 `DAgger-runtime/deployment-backups/20261014-y-home-terminal`（保存v26）和 `20261014-y-home-ready`（保存v27），保留所有旧release。回退只恢复入口/配置引用，不恢复姿态或数据。原始事件、四组同步对照、测试、发布和读回证据在私有 `artifacts/results/20261014-reset-preview/`，详细见[预演故障记录](incidents/20261014-reset-preview-terminal.md)。

还需用户现场从正常作业姿态确认完整回位及连续采集。此前1.71度真实腕部跟随偏差和内部网卡挂起诱因仍是独立未解决事项；本次软件修复不代表这些硬件问题消失。

## 2026-10-14 Y 复位 v26 已部署，现场动作待验收

F1恢复连接后，正式三路线入口和独立复位脚本已在空闲、取得共享控制锁时切到 `20261013-f1-hil-v26-home-retry`，配置 `f1-09-v26-home-retry.json`；版本名沿用10月13日候选，实际部署为10月14日。旧v15遥操和其他候选入口未切换。仍为上抬6cm→向机器人右侧10cm→右臂积木demo-V0，避让4cm/s；不增加独立转腕动作，保留原最多10度小幅姿态变化及所有反馈/限位/碰撞保护。

新的Y请求遇到实测与旧目标不一致时，先通过原有界输出平滑对齐，不覆盖真实发布历史；同一次失败Home保留原几何目标和未完成段，重试不会再次累计6cm/10cm。RB接管、START/模式变化和断连清除检查点；失败不自动重试。大幅持续跟随差仍会停止本次Home，录制可继续，松RB后START仍能结束并保存。4242同步显示起点对齐、上抬、向右避让、回位与停止提示。操作见[HIL_WORKBENCH](HIL_WORKBENCH.md)。

ARM离线回归 **50 passed，162.44秒**，覆盖记录失败起点经对齐→原右移→回程、延迟反馈、失鲜/持续偏差、原有边界、Y重新触发、RB/START取消及真实临时录制器连续保存。只读inspect为hardware_initialized=false。实际部署网页8种浏览器状态通过，0脚本错误、0写请求；源码清单、配置绑定、两入口与HTTP实际返回文件哈希均读回一致。未启动真机动作、未重启服务或驱动、未修改采集数据。

manifest SHA256 `509647e9fe3421f4e02872f4348a5e50e2674ab4d3641d3fe4dd5557dfab657e`；config SHA256 `ea12173d8d19527b0cfb401a5b281b58d94af435d433e710ea24261598f3b9dc`。原地备份为 `/home/user/Bruno/DAgger-runtime/deployment-backups/20261013-y-home-retry`，保留v25；备份包含正式入口、独立脚本、旧配置和静态文件，恢复这些入口/静态文件即可代码回退，不恢复姿态或删除数据。私有验证记录见 `artifacts/results/20261013-y-retry-speed/{final-tests-r2.log,deployment.json,verification.log}`。

还差现场按Y完成一整套回位，以及复位后START保存、再START开启下一条。之前右腕1.71度持续偏差的硬件原因仍未知；数值模拟不等于该偏差已消失，也不承诺所有起点成功。若持续停止，保留本条日志继续定位，不放宽1度保护。速度优化按用户要求暂停。

## 2026-10-13 Y 重试修复候选，远程中断，尚未上线

用户明确选择稳定优先：保持4cm/s避让速度，不增加独立转腕动作，允许上抬/右移时少量姿态变化（原10度预算）。v25两次Y现场完成分别9.624秒、8.605秒；最近失败是右移末端腕关节1.709度跟随差，后续Y虽收到，却因旧指令与实测差约1.68度被起点检查拒绝。具体硬件偏差原因未知，详见[事故补证](incidents/20261013-y-clearance-stall.md)。

开发树与未发布v26候选增加新Y起点平滑对齐、同一次失败Home保留几何检查点并继续未完成段；RB接管、START/模式变化及断连清除检查点。无独立转腕阶段，不放宽到位保护。最初仅对齐后重走全路径的候选在完整测试中暴露重复追加避让距离导致不可达，已改为原检查点重规划，但最终ARM全轨迹回归尚未取得结果。

本地运行端/真实临时录制器回归22通过、1跳过（独立脚本测试缺数值依赖），含Y失败后保持同条录制、START保存和下一条开始；测试替身补齐新增retry_context接口。浏览器8种状态此前通过，0脚本错误、0写请求，静态修改尚未上线。最终ARM任务回传275秒超时，随后SSH banner超时、网页转发拒绝连接；不能据此认定远端测试通过或完成。正式入口最后核对仍为v25；部署脚本未执行，未重启服务或发动作。联网恢复后先读取远端结果，再同步更新后的测试替身、完成全轨迹回归与只读inspect，全部通过才在空闲持锁时切换。现场完整Y仍需用户验收，不能承诺所有起点零故障。

候选/测试/未执行部署脚本在私有`artifacts/results/20261013-y-retry-speed/`。本节覆盖下方历史“当前入口”描述的最新验证边界。

## 2026-10-13 22:00 独立三段复位首次现场完成，DAgger Y已绑定同一实现

用户现场执行`right-reset/20261013T135859.624227Z/events.jsonl`并确认动作可用；日志依次记录lift、right、return、completed，Enter至完成7.504秒，feedback_verified=true。起点右臂J6约6.115度，预演最大转腕8.812度；此成功仅覆盖本次起点，未覆盖此前J6约55度的失败作业姿态。上一条13:58:42Z因共享控制锁占用在预检停止，没有发动作。

按用户要求核对DAgger Y接入：现役`start_f1_hil_route.sh`已指向v25及`f1-09-v25-wrist-clearance.json`，与成功独立脚本完全相同的ClearanceHomeMotion、home_target及6cm/10cm/最多10度配置；runtime在人工模式收到松RB后的Y上升沿创建该运动。发布清单全量哈希通过，ARM现役runtime/reset_script/demonstration离线替身测试23项通过，覆盖右臂限定、有效RB取消、START取消与示教保存。最初直接使用venv执行pytest因无该包失败，随后沿已验证系统dist-packages入口运行成功，未安装依赖。

无需再部署重复实现或在Y处理里调用bash；当前无DAgger运行进程，下次按原三端流程启动即使用已核验实现。人工示教中松RB按Y回位，完成后START保存当前条，再START开始下一条；Y后继续操控需重选右臂。独立脚本现场完成与Y端到端现场验收分开记录：本轮Y真实按键尚待用户验证，未远程发动作。

## 2026-10-13 21:56 现场确认运动恢复，Y避桶路径仍待单独验收

用户确认解除急停/现场操作后实物可以移动。4242动作`a55b64c95dd74dbf892d23c8dc9df186`（13:56:29Z）在3.536秒完成，status=succeeded、feedback_verified=true，右臂7轴到位时最大ROS反馈误差约0.308度；左臂/身体/头部也报告到位。检查时无活动命令，enP3p1s0f2发送队列0，本次启动内核仍无Tx Unit Hang。由此确认本次重启恢复后的基本动作可执行，不能认定驱动挂起诱因或Y分段路径已修好。

当前右臂已接近复位预设；直接从此姿态测试Y可能触发“已在目标”跳过绕行，不能当作上抬/右移验收。下一次现场需从有净空且偏离Home的正常作业姿态验证独立脚本或Y，记录lift/right/return与实际到位；不从SSH发动作。

## 2026-10-13 21:51 重启后内部发送链路恢复，动作尚未复验

21:54用户在4242执行基础启动，网页报systemd-run等待5秒超时；后台实际启动成功：unit为active/exited、Result=success、ExecMainStatus=0，motor_ctl PID99463及运动学/三路相机进程存在。关节消息序号1秒内8506→8656，接收年龄约11–15ms，右臂读数有微小变化；仅证明观测链路恢复更新，未验收运动。实际命令活动均false、无活动action，内网发送队列为0，本次启动未见Tx Unit Hang。已定位网页等待方式缺陷：oneshot systemd-run默认等待启动结束，而调用仅允许5秒，报错不等于后台失败；不要重复基础启动。该问题由4242状态页登记，独立于Y轨迹。

用户保持实体急停并执行重启，新的boot ID为`8d8c6361-b804-4798-855b-0dc05675f745`，核验时启动约100秒。Wi-Fi重新取得192.0.2.3，4242服务自动启动，基础启动服务inactive，未发现motor_ctl_node、f1dagger、独立复位或whole_body_ctl/gamepad进程。本轮没有远程启动驱动或动作。

enP3p1s0f2向192.0.2.4的4次ICMP全部返回，RTT 0.131–0.260ms，TX包24→29；enP3p1s0f1同期TX包2322→2431。两网卡发送队列均为0，本次启动内核日志未见Detected Tx Unit Hang。证据保存于私有`artifacts/results/20261013-reset-tx-hang/reboot-check.json`。说明当前内部发送链路恢复，不代表挂起诱因已修复或长期稳定；此前J6偏差、Y三段避桶与真实到位仍待现场验证。下一步保持急停由现场执行基础启动，再核验反馈和队列，之后安排人工小幅动作与Y验收。

## 2026-10-13 21:40 补证：内部有线网卡发送挂起，脚本与4242均无法运动

用户确认独立复位和手动控制实物均不动，4242积木demo-V1同样跟踪失败。13:35:52Z独立脚本36个采样的右臂7轴反馈逐值不变，最终上抬误差18.607度。最新只读检查：igb PCI 0003:01:00.1/.2持续报Detected Tx Unit Hang；enP3p1s0f2积压约5118包，motor_ctl在192.0.2.5的UDP发送队列约2.4MB。两块内网网卡1秒对照RX继续增加，TX字节/包均完全不变。当前故障已定位到内部发送链路挂起，具体驱动/固件/PCIe诱因未确定；不能继续将这次失败归为避桶IK参数。

独立脚本已经复用4242的RosTransport和同一joint_command话题，换HTTP接口不能绕开底层故障。此前20:55的J6约5度跟随偏差与本次整臂不动是不同证据，不能用当前网卡挂起覆盖之前的问题。v25避桶路径仍未实机验收。

现场已确认实体急停按下且手臂稳定。只读确认无活动动作/采集/遥操后，已通过systemd停止本工作台f1-console-4242与其f1-4242-base-start，旧motor_ctl PID263150及launch PID262914退出；两服务inactive。没有重置网卡、重启驱动、放宽容差或执行试探动作。当前无免密sudo，等待用户在F1本机保持急停执行受控重启，之后先只读核验通信，不自动恢复运动。原厂stop_all.sh含start_whole_body_ctl调用，本次没有使用。不能直接拔插网线或恢复发送队列，以免旧命令恢复发送。原始证据只读保留在私有`artifacts/results/20261013-reset-tx-hang/evidence.json`。


## 2026-10-13 21:30 独立复位现场未完成：命令变化、右臂反馈完全不变

用户执行独立脚本`right-reset/20261013T133025.957146Z`。预演通过并确认Enter后7.532秒，上抬阶段以最大跟踪误差21.976度停止。48个日志采样点中右臂7轴反馈逐值完全相同，最大误差发生在J5；不是此前J6约5度偏差的重复证据。motor_ctl PID263150同期日志有收到命令及Right arm command sent，无WARN/ERROR，但该文本不是硬件执行ACK。ROS receipt/source时间持续更新，仅能证明消息刷新，不能证明底层硬件反馈更新。

尚待现场确认实物是否移动、实体急停/使能状态，以区分未执行与反馈冻结；尚无足够证据指定SDK/控制器具体根因。本轮未改轨迹或容差、未重启驱动、未执行试探动作。21:26只读预演通过不能覆盖此现场失败，新自动复位尚未通过实机验收。只读证据及数值摘要：私有`artifacts/results/20261013-reset-no-response/`。

## 2026-10-13 Y 允许小幅转腕，新路径与独立复位脚本已部署（现场动作待验）

当前入口：正式三路线`20261013-f1-hil-v25-wrist-clearance`、旧遥操`20261013-f1-teleop-v15-wrist-clearance`、候选`20261013-f1-candidate-v16-wrist-clearance`。v24的20:55:53与20:56:44（北京时间）两次现场Y仍停在上抬：J6指令约60度、实测约54.76度；START均正常保存结束，并未再被Home阻塞。停止后运行端始终收到未选臂/无有效RB控臂意图；Y清空选臂，先前“可直接接管”的描述漏了重选臂步骤。不能把v24标为自动回位已修好。

用户允许小幅转腕。新版保留上抬6cm→右移10cm→右臂积木demo-V0；前两段软约束朝向，硬性保留相对初始朝向最多10度、J6绝对角度不超过起始命令的任务约束。没有猜测55度硬件限位、没有改驱动/增益/原关节保护。反馈2mm、实测对命令朝向2度、关节1度、稳定150ms及原限位/自碰撞门槛保留；已在预设时不再绕行。有效RB可取消Home并立即重新接管，Y后的选臂流程见[操作文档](HIL_WORKBENCH.md)。网页现已显示Home停止原因。

本地F1/shared 342通过、3跳过；独立脚本另2项通过。三套ARM数值测试分别39/30/30项，独立脚本ARM另2项通过，均无真实动作。四个历史失败起点覆盖输出平滑、延迟及模拟腕响应限制；测试中55度限幅只是假传输模型，不是实测硬件合同。三入口在空闲持锁时切换，manifest/config、inspect读回通过，原v22相机中断修复等非本次模块保留。备份/回退：`DAgger-runtime/deployment-backups/20261013-y-wrist-clearance/`；证据：私有`artifacts/results/20261013-y-wrist-clearance/`。

按用户要求安装独立现场入口`/home/user/Bruno/reset_right_demo.sh`：无需采集进程/Windows sender，复用不可变v25控制库，先预演，现场Enter后仅发布右臂7轴；只读模式`--check`不创建动作发布端。当前停住实测姿态的只读预演已通过三段，模拟8.04秒、最大转腕2.046度，真实反馈shadow不等于动作验收。日志位于`DAgger-runtime/right-reset/`。没有远程执行复位、重启服务或改原始数据。底层J6约55度停滞原因仍未知，新路径避免继续向外弯，并不宣称所有起点或桶沿净空已经验证。

## 2026-10-13 Y 反馈修正与 START 取消已部署，腕部大偏差仍待查明

当前三个入口分别为 `20261013-f1-hil-v24-clearance-feedback`、`20261013-f1-teleop-v15-clearance-feedback`、`20261013-f1-candidate-v16-clearance-feedback`。各自仅修改 runtime 与 clearance_home 两个模块，其余相机中断处理、录制、数据目录、配置目标、夹爪和控制保护保持各自原版本。空闲持共享锁切换，三入口inspect与清单绑定再次通过，hardware_initialized=false；没有触发动作、重启服务或改原始数据。旧运行进程不会热更新，需现场重新打开F1运行端；Windows sender/隧道不需更新。

START不再把Home当作夹爪pending：松RB后可取消Home进入HOLD，并在人工示教中保存当前条。真正未完成的夹爪指令、RB和策略新鲜交接条件仍保留。稳态小偏差采用停稳后、取得新鲜反馈再重新计算的有界补偿，非误差积分；仍要求实际笛卡尔到位2mm/2度，未放宽关节/腕部/碰撞保护。补偿只接受既有1度关节到位容差内的误差；超出容差或Home超时仅停止本次Home、清空待执行轨迹，保留HIL/本条采集，需新的Y才会再开始。终端/结构状态增加home_stop_reason。

本地F1/shared 335 passed、3 skipped；F1 ARM正式入口32项、旧遥操/候选各23项通过。新增失败姿态与记录稳态偏差的延迟输出模拟：不到0.33度的偏差可完成三段并满足原实测切段条件；约4.7度腕偏差在上抬停稳后明确停止、不右移、不拖到19秒、不终止录制。真实writer验证停止Home后同条仍可继续写入、START保存、下一条可重开，进行中Home也可被START取消。以上是离线与ARM数值/替身传输验证，尚未现场动作验收；不能宣称4.7度底层偏差已修好或所有起点必能自动回位。

备份/哈希保护回退：机器人 `DAgger-runtime/deployment-backups/20261013-y-clearance-feedback/`；本地证据 `artifacts/results/20261013-y-clearance-fix/`。回退只恢复包装器，旧v23有已知停滞，不会恢复姿态或数据。事故与修复细节见[记录](incidents/20261013-y-clearance-stall.md)。下节保留修复前现场失败事实。

## 2026-10-13 Y 避桶复位现场失败，尚未修复

17:02–17:03的三条新记录已只读核对：两次Y均未完成，一次上抬19秒超时进入SAFE_HOLD，另一次上抬约15秒后才右移、进入return后断连；Home pending也拒绝了两次START结束录制。指令约2秒到位，但真实反馈有2–4mm稳定偏差，第一条腕J6还持续差约4.7度，无法满足新增2mm/2度切段条件。此前完美/延迟反馈测试漏掉实际伺服偏差，不能据此宣称现场可用。

三条648/301/648组三图已提交，journal哈希核对通过，未见写盘错误；未改原始数据，未批准训练。本轮没有改阈值、部署新代码或执行动作，v23仍在入口上；问题尚未修复，暂不使用Y自动避桶。用户确认卡住时已完全悬空；运行中驱动启动配置为关节阻抗，J6范围[-60,60]度，未发现55度配置上限。用真实反馈已离线复现两次不切段、换理想反馈均通过；第三条各关节偏差不到0.33度也会卡住。J6大偏差的底层原因仍未证实。根因、采集影响与证据见[现场失败记录](incidents/20261013-y-clearance-stall.md)。下节“已部署”仅是历史发布事实，不能覆盖本次失败结论。

## 2026-10-13 离线训练导出过度切段已修正

开发机F710导出已改为固定30Hz因果采样：只取当时已到达的图像/state，action从该时刻实际发布日志独立取值；短暂沿用观测明确保留来源时间、年龄与标记。原有相机/控制连续性、Home/接管/epoch及新鲜度门槛保留，不拼接真实断点、不外推末尾。统一数据适配保留新来源字段，审核绑定分段指纹，避免旧片段编号自动批准新分段；不同导出重复来源仍被拒绝。

四条原件只读复制并完整哈希审计。三条任务候选的旧48/41/21段变为13/7/11段，完整50步窗口合计1089→1564（重叠窗口，不是独立示教）；仅保持/Home的3.9秒条目仍不计任务成功。073059真实候选官方v3导出862样本/7段、逐帧三图/state/action回读、完整50步窗口与末尾padding mask均通过，`training_ready=false`、未人工批准。旧原件/候选不覆盖。

本地F1/shared在LeRobot环境273 passed/7 skipped（缺pinocchio、ruckig、pink的可选数值模块）；统一数据相关21 passed，包含官方v3合成回读。最初沙箱socket限制导致的11项失败，在允许本机回环后全部通过；HF缓存改用独立可写测试目录。未验证RLinf reader、未开始训练/真机rollout，机器人现役release及控制/采集不变。完整原因、方案和数据表见 [重采样修正](incidents/20261013-training-resampling.md)，证据在`artifacts/results/20261013-resampling-fix/`。

## 2026-10-13 Y 右臂分段避桶复位已部署

用户确认仅右臂蹭桶、左臂在此任务不动，并指定上抬五六厘米、向机器人右侧10厘米。Y现为：先保持夹爪朝向上抬6cm（base_link +Z），再向右10cm（-Y），最后右臂回积木demo-V0；左臂、腰、头、夹爪保留当前命令。两段笛卡尔运动使用原受约束IK、发布领先约束及唯一输出owner；每段须目标/实测到位并取得新的反馈稳定150ms后才能切段，超时或原有停止条件不会跳过避桶直接回目标。桶没有接入碰撞模型，6/10cm为本任务指定路径，尚未现场验证桶沿净空。

正式三路线切换到 `20261013-f1-hil-v23-clearance-home`，基于现役v22相机中断修复；demonstration/lifecycle/observation/policy等既有代码SHA保持。旧遥操与候选入口分别切到 `20261013-f1-teleop-v15-clearance-home`、`20261013-f1-candidate-v16-clearance-home`，各自保留原依赖、采集和手动控制栈。仅替换各版runtime的Home分支、contract与配置，新增clearance_home模块。v22-clearance-home是最初基于v21的未启用候选；最终上线的是v23，不得用前者覆盖相机修复。

本地F1/shared回归318 passed、3 skipped；三套ARM release各10项真实数值测试通过，覆盖积木预设/只读捕获的当前姿态、上抬/右移顺序、朝向、固定左臂/夹爪、关节与自碰撞、真实输出平滑/延迟反馈、失鲜不切段和超时。三个已切换入口inspect与身份绑定通过、hardware_initialized=false。空闲持共享锁切换，未启动动作、未重启驱动或4242；原配置/release和回退保留。现场按Y及桶沿净空仍需用户验收，启动新的运行端后生效。

网页Y教学同步为三段右臂路径；首帧动画时间下界已修复。备份与哈希保护回退在机器人 `DAgger-runtime/deployment-backups/20261013-y-clearance/`；本地源码、ARM、只读状态与浏览器证据在私有 `artifacts/results/20261013-y-clearance/`。代码回退不恢复实物姿态或删除录制数据。

## 2026-10-13 v22右腕相机中断处理已部署

16:24右腕D405发生USB传输错误并持续断流，v21将采集异常升级为进程退出，638组三图留在partial。用户重插后驱动恢复；已验证哈希并在独立runtime/recovery保留638组恢复副本，原件未改。前条934组正常保存未受影响。

正式三路线现为`20261013-f1-hil-v22-camera-interruption`：缺图后SAFE_HOLD并提交中断前缀，标明interrupted、不播放正常完成音；采集线程继续等待真实三图，恢复后不自动运动或开录，需新START。其他异常保持原有失败保护，所有阈值、输出代码及音量不变。

本地全回归257 passed/9 skipped，另增运动清空测试通过；ARM48 passed。真实观测shadow缺图注入、恢复后重新分条58/55组均审计通过，零实机动作。空闲持锁切换、release SHA与回退校验通过。USB线材/接头/供电具体原因和长期运动稳定性仍待验证。完整前因、恢复位置、测试及回退见 [右腕USB中断记录](incidents/20261013-right-wrist-usb-interruption.md)。

## 2026-10-13 连续三条正式示教核查

用户现场v21同进程完成三条START分条：876/118/904组三图，实际29.884/30.015/29.924Hz，均正常提交且哈希审计通过，无写盘错误、无缺失payload、无SAFE_HOLD或发布失败。第二条仅保持/自动Home，不应计作成功任务示教。第一、第三条有真实右臂和夹爪动作；仍存在最大245/170ms组间隔、头相机源时间戳重复和训练导出切段偏碎，尚未批准训练或长时间稳定性。

三条视频都已ready，保存至就绪分别19分35秒、2分18秒、10分43秒。当前整进程active会阻止视频任务发现/传输，单任务串行传输为主要瓶颈；服务器缓存独立编码复测22.019秒。默认原图是网页现有选择，视频在折叠区。F1硬件编码插件存在但未做并发负载验收。本轮仅审计、服务器复测与文档记录，未改生产配置或网页。详见 [连续三条与视频等待核查](incidents/20261013-continuous-three-audit.md)。

## 2026-10-13 用户调音确认与连续采集复验

用户确认浏览器调音已试听、保存成功；只读核对正式配置为开始53、完成50。正式人工示教入口已指向v21，并使用同一runtime下的recording_audio.json，每次播放重新读取。现有集成已生效，本轮不重复替换release、不覆盖音量；清单全部SHA通过。

新增回归覆盖三次连续提交、音量保存后热读取，以及音频线程阻塞/报错时仍能保存并开始下一条。本地14项通过，F1 ARM共44项通过。另用现役v21、真实ROS观测、独立端口执行三条shadow录制：依次60/59/59组三图，journal与payload均complete=true、writer_error=null，三条原始审计valid=true且无缺失payload；未录制期不建pack，条间pack不增长，进程正常退出。未创建实机动作出口；测试结束后HIL/Pico/录制均空闲。

本次不是零丢帧或现场运动验收：三条头相机源时间戳重复分别8/3/1次，后两条的逐路累计camera_drop_counts均为1，保留camera_discontinuity证据。测试数据均training_ready=false，仅在 `DAgger-runtime/offline-checks/v21-continuous-shadow-audio-check`，未写入正式data或训练集；音频回归使用替身播放器，现场可闻性依据用户上述确认。网络延迟排查仍暂停。

证据：`artifacts/results/20261013-continuous-audio-check/{shadow.log,arm-tests.log}`；机器人回归目录 `DAgger-runtime/offline-checks/v21-continuous-audio-tests`。正常操作仍为START开始→松RB并等待Home/夹爪完成→START结束→等完成音或已保存/未录制→START下一条。调音工具可Ctrl+C关闭，设置保留；不要在正式采集时另开手动试听，避免与录制提示音重叠。

## 2026-10-13 调音入口修复为 SSH 浏览器版

用户现场确认SSH无桌面，Tk启动失败；现已用独立浏览器旋钮替换tune_recording_audio.sh的目标，不再要求NoMachine。现役v21控制release不修改。用户启动后通过终端打印的完整链接访问4243，声音仍由F1播放，保存到同一volume配置。可在下一次采集提示音生效，原始1KHZ文件保留。

本地接口/音频5项、ARM接口1项通过；浏览器旋钮/播放桩/保存验证通过，F1无DISPLAY实启并只读读取页面/状态通过。未现场发声或改变配置，扬声器可闻性仍待用户确认。备份位于DAgger-runtime/deployment-backups/20261013-audio-tuner-web，证据位于artifacts/results/20261013-audio-tuner-web。

## 2026-10-13 v21 录制提示音与桌面调音

已部署并切三路线入口到20261013-f1-hil-v21-recording-audio：用户选开始A/完成B，仅powered人工示教接入录制成功边界；两个原有文件仅cli和demonstration变化，控制/保护、相机和原始写盘模块相对v20保持。新增有界音频线程、CC0音频资产及桌面56_recording_audio.py，稳定入口tune_recording_audio.sh。软件增益配置每次播放读取；不会调整系统总音量。

本地12项、ARM42项测试通过；release清单与配置绑定核验通过。空闲持锁切换，未启动动作/采集或重启驱动/4242。备份 `DAgger-runtime/deployment-backups/20261013-recording-audio`，本地证据 `artifacts/results/20261013-recording-audio`。原始1KHZ文件保留。

试播试听文件aplay均退出0，但用户报告无声，实际可闻性尚未确认。F1已具备Tkinter；SSH桌面权限导致GUI隐藏检查未完成，需要NoMachine桌面终端运行调音脚本，用户确认声音后再进行正式连续录制。操作见HIL_WORKBENCH。

## 2026-10-13 连续分条确认与音效候选

已只读核对部署v20：人工示教保存完成回到idle，可再次START建立独立下一条；功能已存在，本轮不改状态机。8项既有离线测试通过；保存中START不排队的语义保持。操作步骤补入HIL_WORKBENCH。4242新增独立音效试听页，CC0素材已降低音量，仅用户点击浏览器试听；原机器人音效未替换，未接入录制事件。

## 2026-10-13 新人工示教已核对

START分条现场记录已提交：848组三图、约29.676Hz，结构审计通过、写盘无丢弃；有最大399.75ms观测间隔、头相机源时间戳重复与末尾Home。技术筛选695帧/48段，尚需质量审核及正式v3导出验证。三路审阅视频已生成，服务器有编码缓存。详见 [数据审计与位置](incidents/20261013-demonstration-audit.md)。

## 2026-10-13 输入延迟排查暂停

用户要求暂不继续测试。原200ms输入保护及500ms ACK阈值未改变；短时停顿免START恢复仅为待验证设计。完整前因、测试结果与未证实项移入 [事故记录](incidents/20261013-input-latency.md)，后续由 [TODO](TODO.md) 跟踪。

## 2026-10-12 人工示教 START 分条已部署

三路线入口现为 `20261012-f1-hil-v20-start-recording-r2`。仅人工示教新增 START 开始/结束一条：先释放 RB 并等待夹爪/Home 完成再结束；后台保存完成后可直接录下一条。进程空闲和保存期不写训练三图、观测及逐帧动作；反馈与相机健康接收继续运行。自由接入和策略HIL保留各自原START语义，旧v15入口未变。输出owner、50/250Hz控制和30Hz采集保持；每条独立建立真实夹爪发布来源，禁止借用上一条action证据。

原始记录仍在 `/home/user/Bruno/data/hil_data/demonstration`，模型/进程诊断改放 runtime/sessions，不占训练目录。既有 DATA 与转换扫描在每条提交后可发现，未增加上传或视频编码。断线收尾并进入SAFE_HOLD，重连不自动录制；保存中START不排队恢复。Ctrl+C或网页结束运行端会提交当前条并退出。

本地247 passed / 9 skipped；ARM38 passed。真实ROS观测、专用端口shadow连续两条分别59/60组三图，原始审计valid=true，停止后pack大小不变、无丢弃、无动作输出；这是无动作短检，不等同现场powered或10分钟负载验收。首轮shadow发现状态摘要漏传phase字段，r2已修复并重测。线上网页1440/390宽度共8种录制状态夹具与真实START教学通过，JS异常和非GET请求0。

启动包装器与网页已切换；4242仍v29-r3、服务PID699697未重启。运动学、配置解析、映射、相机接收、原始payload writer和硬件owner等9个模块与v19逐字节相同，积木demo-V0目标保留。备份/回退在机器人 `DAgger-runtime/deployment-backups/20261012-start-recording-v20-r2`，本地证据 `artifacts/results/20261012-f1-start-recording/`。现场F710按键及实际运动录制还需用户验收；现有Windows sender协议兼容无需更新，新版Windows脚本仅补充状态提示。

## 2026-10-12 Y 复位目标已部署

用户指定所有 F1 手柄入口的 Y 改为「积木demo-V0」，并确认仅改变双臂，腰、头和夹爪保持现状。目标取自现场预设 `81bda04dd141438e8a1ae2a6df2d3fdb`，以配置快照绑定，不跟随网页后续编辑。原预设及 F1-infra 机器 profile 不变。

三路线切换到 `20261012-f1-hil-v19-demo-home`；旧手柄及保留的候选入口分别指向 `20261012-f1-teleop-v15-demo-home`、`20261012-f1-candidate-v16-demo-home`。后两者仅修改 Home 配置/校验/事件与其发布清单，其他控制、数采和输出代码保持原版；候选入口默认仍为 shadow。三个旧 release 和运行配置全部保留。

本地 F1/shared 回归 288 passed / 3 skipped；真实轨迹和碰撞数值测试覆盖旧零位到新目标、标准及积木腰部两种姿态。三个版本均完成 ARM 配置/清单验证及从当前反馈、旧双臂零位出发的无输出轨迹/碰撞计算；现场两旧入口 `--check`、三路线 session 的 inspect 均读取同一新目标。线上浏览器三路线 Y 文案、模型动画及目标数值一致，桌面/手机布局通过、JS异常和非GET请求均为0。未通过 SSH 启动 powered 或触发 Y 动作，现场按键和到位仍待用户验收。

切换前确认工作台/HIL/录制空闲并持有共享锁；电机、运动学及4242服务PID不变。网页 Y 说明和右臂教学终点同步热更新，current仍为v28-r5；原用户预设和机器profile SHA保持不变。回退备份在机器人 `DAgger-runtime/deployment-backups/20261012-y-home/`，本地证据在本项目私有 `artifacts/results/20261012-f1-y-home/`。

以下为2026-10-12较早的源码整理记录，依据本项目既有发布记录与 [F1-infra 状态记录](../../../F1-4242/docs/STATUS.md) 汇总；当时仅整理本地源码和文档。后续 Y 目标部署见上节，仍不代表新的现场动作验收。

## Y 更新前的入口与版本

| 部件 | 最后已有证据 |
|---|---|
| 4242 三路线包装器 | 2026-10-10 已切换 `20261010-f1-hil-v18-save`；绑定 v18 配置/manifest，增加会话结束请求和独立数据根 |
| 原稳定遥操入口 | `/home/user/Bruno/DAgger/start_f1_teleop.sh` 的 v15 保留，用户此前已确认顿挫消失；不是三路线当前唯一版本 |
| F1-infra 工作台 | `20261012-f1-4242-v29-unified-data-r3`；统一数据出口与本地根目录已部署，未更换v18动作release |
| 三路线 | 自由接入保留诊断、不写训练图片；人工示教录制；策略 HIL 仍需真实模型配置与验收 |
| 原始数据 | 新示教/策略分别在 `/home/user/Bruno/data/hil_data/{demonstration,policy_hil}`；旧lxl/dagger_data与runtime/raw保留索引，原件未移动 |
| 视频 | 服务器持久 worker 和独立隧道已有安装、回传及真实历史视频证据 |

v18 沿用 v17 的机器人绑定和8个控制/保护模块，START、手柄合同与30Hz采集未改变。结束并保存按 run_id/PID/start_ticks 校验会话，正常提交 journal/payload 后才标为 saved；仍兼容现场 Ctrl+C。软件/shadow 通过不等于全部手柄/运动场景完成验收。

## 已有现场证据与边界

- 2026-10-10 人工示教 `f1_hil_20261010T074359Z_977e0872` 正常提交，4071组三图、16676条控制记录，135.870秒观测覆盖约29.955Hz；右臂和右夹爪存在真实反馈变化。结构审计 valid=true，写盘错误/丢弃0。
- 同条数据已生成约135.93秒的三路回放，原始图像保持不变。历史视频播放通过不代表新一轮实时预览负载或实机动作验收。
- 该记录含 Home、末尾保护保持以及相机来源时间戳/配对问题，`training_ready=false`。完整提交不能替代人工审核、片段筛选、对齐与训练 reader 验证。
- 10-12 浏览器记录确认三路实时 JPEG 和三路历史视频可显示，工作台进程未重启。此前 Wi-Fi 接口绑定故障有恢复记录；这里不推断此刻网络、驱动或相机状态。
- 真实模型权重/端点、现场 F710 结束按钮体验、多个可见预览的操作负载与完整闭环训练仍需各自验收。未知项保留为未知。

完整后续依据在 [F1-infra STATUS](../../../F1-4242/docs/STATUS.md)，DAgger 自身历史软件/ARM/shadow 证据见 [F1_VALIDATION](F1_VALIDATION.md)、[首次 HIL 候选](HIL_RELEASE.md)、[三路线交付](HIL_ROUTES_RELEASE.md)。外部 F1-infra 是独立项目，不随 DAgger 收编。

## 本次源码整理

开发入口统一为 `F1/`、`G2/`、`shared/`；文档在 `docs/`，本地产物在 `artifacts/`。此次迁移与回归结果由 [布局迁移清单](../history/LAYOUT_20261012.json) 和本轮结果记录追溯，不将历史329/333等测试数字冒充本轮重测。源代码位置变化未切换机器人 release、配置、服务或运行数据。

## 2026-10-12 数据出口补充

F1-infra统一出口已对真实20261010 F710记录完成官方v3转换与全帧reader回读：1834帧、74连续片段、30fps，仍为待审核候选。候选已校验回传F1的 `/home/user/Bruno/data/datasets/f710_20261010_candidate`。本地归档上传关闭，转换扫描只排队；实现及验收细节以F1-infra STATUS为准。三端操作方式和START语义不变，新机器人现场包装器只增加data-root。
